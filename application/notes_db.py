"""Per-user SQLite registry for markdown notes.

Plain ``.md`` files remain the content source of truth. This DB tracks durable
metadata (note_id, title, path, size, timestamps) so note_id can also serve as
the Open Agent ``session_id`` later.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from application import vault_backend

logger = logging.getLogger("notes_db")

DB_NAME = "notes.db"
_MARKDOWN_SUFFIXES = {".md", ".markdown"}
_lock = threading.RLock()

_TITLE_HEADING_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_FM_RE = re.compile(r"^---\s*\n([\s\S]*?)\n---\s*\n?", re.MULTILINE)


def new_note_id() -> str:
    """Generate a session-safe id (matches harness SESSION_ID_RE)."""
    return "n" + uuid.uuid4().hex


def is_markdown_path(path: str) -> bool:
    return Path(_norm_rel(path)).suffix.lower() in _MARKDOWN_SUFFIXES


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _db_path() -> Path:
    settings = vault_backend.settings_dir()
    settings.mkdir(parents=True, exist_ok=True)
    return settings / DB_NAME


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS notes (
            note_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            path TEXT NOT NULL UNIQUE,
            size_bytes INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_notes_path ON notes(path);
        CREATE INDEX IF NOT EXISTS idx_notes_updated ON notes(updated_at DESC);
        """
    )


def _row_to_dict(row: sqlite3.Row | None) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    return {
        "note_id": row["note_id"],
        "title": row["title"],
        "path": row["path"],
        "size_bytes": int(row["size_bytes"] or 0),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def extract_title(path: str, content: str = "") -> str:
    """Title from frontmatter / first H1 / filename stem."""
    text = content or ""
    fm = _FM_RE.match(text)
    if fm:
        block = fm.group(1)
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            if key.strip().lower() == "title":
                title = val.strip().strip("\"'")
                if title:
                    return title
        body = text[fm.end() :]
    else:
        body = text
    m = _TITLE_HEADING_RE.search(body)
    if m:
        return m.group(1).strip()
    return Path(path).stem or "Untitled"


def _norm_rel(path: str) -> str:
    return (path or "").replace("\\", "/").strip("/")


def _nfc_rel(path: str) -> str:
    """NFC path key. macOS and S3 often store the same folder as NFD."""
    return unicodedata.normalize("NFC", _norm_rel(path))


def _same_or_under(path: str, root: str) -> bool:
    target = _nfc_rel(path)
    base = _nfc_rel(root)
    return bool(base) and (target == base or target.startswith(base + "/"))


def _select_note(conn: sqlite3.Connection, rel: str) -> Optional[sqlite3.Row]:
    """Find a note row even when the stored spelling is NFC and the lookup is NFD."""
    row = conn.execute("SELECT * FROM notes WHERE path = ?", (rel,)).fetchone()
    if row:
        return row
    key = _nfc_rel(rel)
    for candidate in conn.execute("SELECT * FROM notes").fetchall():
        if _nfc_rel(candidate["path"]) == key:
            return candidate
    return None


def ensure_db() -> None:
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            conn.commit()
        finally:
            conn.close()


def is_empty() -> bool:
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            n = conn.execute("SELECT COUNT(*) AS c FROM notes").fetchone()
            return int(n["c"] if n else 0) == 0
        finally:
            conn.close()


def get_by_path(path: str) -> Optional[dict[str, Any]]:
    rel = _norm_rel(path)
    if not rel:
        return None
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            row = _select_note(conn, rel)
            return _row_to_dict(row)
        finally:
            conn.close()


def get_by_id(note_id: str) -> Optional[dict[str, Any]]:
    nid = (note_id or "").strip()
    if not nid:
        return None
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            row = conn.execute("SELECT * FROM notes WHERE note_id = ?", (nid,)).fetchone()
            return _row_to_dict(row)
        finally:
            conn.close()


def list_notes(*, order: str = "updated_at") -> list[dict[str, Any]]:
    col = "updated_at" if order == "updated_at" else "path"
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            if col == "updated_at":
                rows = conn.execute(
                    "SELECT * FROM notes ORDER BY updated_at DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM notes ORDER BY path COLLATE NOCASE ASC"
                ).fetchall()
            return [d for r in rows if (d := _row_to_dict(r)) is not None]
        finally:
            conn.close()


def upsert_note(
    path: str,
    *,
    content: Optional[str] = None,
    size_bytes: Optional[int] = None,
    title: Optional[str] = None,
    note_id: Optional[str] = None,
    touch_created: bool = False,
) -> dict[str, Any]:
    """Insert (new file) or update (existing file) a note row. Preserves note_id / created_at."""
    rel = _norm_rel(path)
    if not is_markdown_path(rel):
        raise ValueError("notes_db only tracks markdown notes")

    text = content
    if text is None:
        try:
            target = vault_backend.resolve_vault_path(rel)
            if target.is_file():
                text = target.read_text(encoding="utf-8", errors="replace")
                if size_bytes is None:
                    size_bytes = target.stat().st_size
            else:
                text = ""
        except Exception:
            text = content or ""

    if size_bytes is None:
        size_bytes = len((text or "").encode("utf-8"))

    resolved_title = (title or "").strip() or extract_title(rel, text or "")
    now = _utc_now()

    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            existing = _select_note(conn, rel)
            created = False
            if existing:
                nid = existing["note_id"]
                created_at = existing["created_at"]
                if touch_created:
                    created_at = now
                conn.execute(
                    """
                    UPDATE notes
                    SET path = ?, title = ?, size_bytes = ?, updated_at = ?, created_at = ?
                    WHERE note_id = ?
                    """,
                    (rel, resolved_title, int(size_bytes), now, created_at, nid),
                )
                logger.info(
                    "notes_db update path=%s note_id=%s size=%s",
                    rel,
                    nid,
                    size_bytes,
                )
            else:
                created = True
                nid = (note_id or "").strip() or new_note_id()
                clash = conn.execute(
                    "SELECT path FROM notes WHERE note_id = ?", (nid,)
                ).fetchone()
                if clash and clash["path"] != rel:
                    nid = new_note_id()
                conn.execute(
                    """
                    INSERT INTO notes (note_id, title, path, size_bytes, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (nid, resolved_title, rel, int(size_bytes), now, now),
                )
                logger.info(
                    "notes_db register path=%s note_id=%s size=%s",
                    rel,
                    nid,
                    size_bytes,
                )
            conn.commit()
            row = conn.execute("SELECT * FROM notes WHERE path = ?", (rel,)).fetchone()
            out = _row_to_dict(row)
            assert out is not None
            out["created"] = created
            _schedule_persist()
            return out
        finally:
            conn.close()


def _schedule_persist() -> None:
    try:
        from application import vault_db_persistence

        vault_db_persistence.schedule_persist()
    except Exception:
        logger.debug("notes.db persist schedule skipped", exc_info=True)


def on_note_written(path: str, content: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Register or update after a markdown file create/save. No-op for non-markdown."""
    if not is_markdown_path(path):
        return None
    try:
        return upsert_note(path, content=content)
    except Exception:
        logger.exception("notes_db on_note_written failed for %s", path)
        return None


def on_note_deleted(path: str) -> int:
    """Remove registry rows after a file/folder delete."""
    try:
        n = delete_note(path)
        if n:
            logger.info("notes_db delete path=%s removed=%d", path, n)
        return n
    except Exception:
        logger.exception("notes_db on_note_deleted failed for %s", path)
        return 0


def on_note_renamed(from_path: str, to_path: str) -> Optional[dict[str, Any]]:
    """Update path (and folder descendants) after rename/move."""
    try:
        row = rename_note(from_path, to_path)
        logger.info("notes_db rename %s -> %s", from_path, to_path)
        return row
    except Exception:
        logger.exception("notes_db on_note_renamed failed %s -> %s", from_path, to_path)
        return None


def _drop_note_ids(conn: sqlite3.Connection, note_ids: list[str]) -> list[str]:
    """Delete registry rows by id. Returns the ids that were removed."""
    dropped: list[str] = []
    for nid in note_ids:
        if not nid:
            continue
        cur = conn.execute("DELETE FROM notes WHERE note_id = ?", (nid,))
        if cur.rowcount:
            dropped.append(nid)
    return dropped


def _forget_chat(note_ids: list[str]) -> None:
    if not note_ids:
        return
    try:
        from application import agent_chat_db

        agent_chat_db.delete_messages_for_notes(note_ids)
    except Exception:
        logger.exception("agent_chat cleanup after note drop failed")


def rename_note(from_path: str, to_path: str) -> Optional[dict[str, Any]]:
    """Rename a single note path, or remap all notes under a folder prefix.

    The original ``note_id`` is kept. A second row already registered at the
    destination (the duplicate that appears when a retitle inserts a new note
    instead of moving Untitled) is removed so only one row remains.
    """
    src = _norm_rel(from_path)
    dst = _norm_rel(to_path)
    if not src or not dst or _nfc_rel(src) == _nfc_rel(dst):
        return get_by_path(dst) if dst else None

    now = _utc_now()
    dropped: list[str] = []
    moved: Optional[dict[str, Any]] = None
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            row = _select_note(conn, src)
            if row:
                dest = conn.execute(
                    "SELECT * FROM notes WHERE path = ?", (dst,)
                ).fetchone()
                if dest is not None and dest["note_id"] != row["note_id"]:
                    logger.info(
                        "notes_db drop duplicate path=%s note_id=%s (keep %s)",
                        dst,
                        dest["note_id"],
                        row["note_id"],
                    )
                    dropped.extend(_drop_note_ids(conn, [dest["note_id"]]))
                title = row["title"]
                if Path(src).stem != Path(dst).stem and title == Path(src).stem:
                    title = Path(dst).stem
                conn.execute(
                    """
                    UPDATE notes
                    SET path = ?, title = ?, updated_at = ?
                    WHERE note_id = ?
                    """,
                    (dst, title, now, row["note_id"]),
                )
                # Path is UNIQUE, so this only hits a raced extra row.
                extra = conn.execute(
                    "SELECT note_id FROM notes WHERE path = ? AND note_id != ?",
                    (src, row["note_id"]),
                ).fetchall()
                dropped.extend(_drop_note_ids(conn, [r["note_id"] for r in extra]))
                conn.commit()
                moved = _row_to_dict(
                    conn.execute(
                        "SELECT * FROM notes WHERE note_id = ?", (row["note_id"],)
                    ).fetchone()
                )
                _schedule_persist()
            else:
                # Folder rename: remap descendants. Compare NFC so an NFD
                # folder name still matches the stored row.
                src_key = _nfc_rel(src)
                dst_key = _nfc_rel(dst)
                rows = [
                    r
                    for r in conn.execute("SELECT note_id, path FROM notes").fetchall()
                    if _nfc_rel(r["path"]).startswith(src_key + "/")
                ]
                for r in rows:
                    old = r["path"]
                    suffix = _nfc_rel(old)[len(src_key) :]
                    new_path = dst_key + suffix
                    clash = conn.execute(
                        "SELECT note_id FROM notes WHERE path = ?", (new_path,)
                    ).fetchone()
                    if clash is not None and clash["note_id"] != r["note_id"]:
                        dropped.extend(_drop_note_ids(conn, [clash["note_id"]]))
                    conn.execute(
                        "UPDATE notes SET path = ?, updated_at = ? WHERE note_id = ?",
                        (new_path, now, r["note_id"]),
                    )
                conn.commit()
                if rows:
                    _schedule_persist()
        finally:
            conn.close()
    _forget_chat(dropped)
    return moved


def rebind_note_path(
    from_path: str,
    to_path: str,
    *,
    content: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Move the note row from ``from_path`` to ``to_path`` and refresh its title.

    Used when the editor retitles a note (Untitled → real name). The same
    ``note_id`` is preserved and any leftover row on the old path is removed.
    """
    src = _norm_rel(from_path)
    dst = _norm_rel(to_path)
    if not dst or not is_markdown_path(dst):
        return None
    if src and src != dst:
        rename_note(src, dst)
    try:
        row = upsert_note(dst, content=content)
    except Exception:
        logger.exception("notes_db rebind failed %s -> %s", src, dst)
        row = get_by_path(dst)
    if src and src != dst:
        leftover = get_by_path(src)
        kept = (row or {}).get("note_id")
        if leftover and leftover.get("note_id") != kept:
            delete_note(src)
    return row


def delete_note(path: str) -> int:
    """Delete a note or all notes under a folder. Returns removed row count."""
    rel = _norm_rel(path)
    if not rel:
        return 0
    note_ids: list[str] = []
    deleted = 0
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            note_ids = [
                r["note_id"]
                for r in conn.execute("SELECT note_id, path FROM notes").fetchall()
                if _same_or_under(r["path"], rel)
            ]
            deleted = 0
            for nid in note_ids:
                cur = conn.execute("DELETE FROM notes WHERE note_id = ?", (nid,))
                deleted += cur.rowcount
            conn.commit()
        finally:
            conn.close()
    if note_ids:
        try:
            from application import agent_chat_db

            agent_chat_db.delete_messages_for_notes(note_ids)
        except Exception:
            logger.exception("agent_chat cleanup after note delete failed")
    if deleted:
        _schedule_persist()
    return int(deleted)


def sync_from_filesystem() -> dict[str, int]:
    """Reconcile DB with on-disk ``*.md`` (add missing, refresh, drop orphans)."""
    root = vault_backend.vault_root()
    on_disk: dict[str, Path] = {}
    for path in root.rglob("*.md"):
        if ".vault" in path.parts:
            continue
        rel = path.relative_to(root).as_posix()
        on_disk[rel] = path

    added = updated = removed = 0
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            existing_by_key: dict[str, list[sqlite3.Row]] = {}
            for row in conn.execute("SELECT * FROM notes").fetchall():
                existing_by_key.setdefault(_nfc_rel(row["path"]), []).append(row)
            now = _utc_now()
            for rel, path in on_disk.items():
                try:
                    st = path.stat()
                    text = path.read_text(encoding="utf-8", errors="replace")
                    title = extract_title(rel, text)
                    size = int(st.st_size)
                    mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
                    mtime_iso = mtime.replace(microsecond=0).isoformat().replace("+00:00", "Z")
                    ctime = datetime.fromtimestamp(st.st_ctime, tz=timezone.utc)
                    ctime_iso = ctime.replace(microsecond=0).isoformat().replace("+00:00", "Z")
                except Exception:
                    logger.exception("notes_db sync failed for %s", rel)
                    continue

                rows = existing_by_key.pop(_nfc_rel(rel), [])
                if not rows:
                    conn.execute(
                        """
                        INSERT INTO notes (note_id, title, path, size_bytes, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (new_note_id(), title, rel, size, ctime_iso, mtime_iso),
                    )
                    added += 1
                    continue
                # Same file stored under another Unicode spelling keeps its note_id.
                rows.sort(key=lambda item: (item["path"] != rel, item["created_at"] or ""))
                row = rows[0]
                for extra in rows[1:]:
                    conn.execute("DELETE FROM notes WHERE note_id = ?", (extra["note_id"],))
                    removed += 1
                prev_size = int(row["size_bytes"] or 0)
                prev_title = row["title"] or ""
                if row["path"] != rel or prev_size != size or prev_title != title:
                    conn.execute(
                        """
                        UPDATE notes
                        SET path = ?, title = ?, size_bytes = ?, updated_at = ?
                        WHERE note_id = ?
                        """,
                        (rel, title, size, mtime_iso or now, row["note_id"]),
                    )
                    updated += 1

            for rows in existing_by_key.values():
                for row in rows:
                    conn.execute("DELETE FROM notes WHERE note_id = ?", (row["note_id"],))
                    removed += 1

            conn.commit()
        finally:
            conn.close()

    if added or updated or removed:
        _schedule_persist()

    logger.info(
        "notes_db sync: added=%d updated=%d removed=%d total=%d",
        added,
        updated,
        removed,
        len(on_disk),
    )
    return {"added": added, "updated": updated, "removed": removed, "total": len(on_disk)}


def ensure_note_for_path(path: str) -> Optional[dict[str, Any]]:
    """Return DB row for a markdown path, creating one if the file exists."""
    rel = _norm_rel(path)
    if not is_markdown_path(rel):
        return None
    row = get_by_path(rel)
    if row:
        return row
    try:
        target = vault_backend.resolve_vault_path(rel)
    except ValueError:
        return None
    if not target.is_file():
        return None
    return upsert_note(rel)
