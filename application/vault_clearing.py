"""Find media files that no markdown in the same folder references.

A png, jpg, jpeg, wav, mp3, or mp4 is used only when a ``.md`` file sitting
in that same directory mentions the file name (extension included). References
from other folders do not count. Kept paths are stored in
``.vault/clearing_kept.json`` and omitted from later scans.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import unquote

from application import vault_backend, vault_index

logger = logging.getLogger("vault_clearing")

MEDIA_SUFFIXES = {".png", ".jpg", ".jpeg", ".wav", ".mp3", ".mp4"}
SKIP_DIR_NAMES = {".git", ".vault", ".obsidian", "__MACOSX"}
KEPT_NAME = "clearing_kept.json"

_MD_DEST_RE = re.compile(r"(?:!\[[^\]]*\]|\[[^\]]*\])\(([^)\n]+)\)")
_HTML_SRC_RE = re.compile(
    r"""<(?:img|audio|video|source)\b[^>]*\bsrc=["']([^"']+)["']""",
    re.IGNORECASE,
)


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value or "")


def normalize_rel(path: str) -> str:
    """Vault-relative path. Rejects traversal and empty paths."""
    cleaned = _nfc((path or "").replace("\\", "/")).strip().lstrip("/")
    if not cleaned or cleaned.startswith("/") or ".." in cleaned.split("/"):
        raise ValueError("invalid path")
    if any(part in SKIP_DIR_NAMES or part.startswith(".") for part in cleaned.split("/")):
        raise ValueError("invalid path")
    return cleaned


def normalize_media_rel(path: str) -> str:
    rel = normalize_rel(path)
    if Path(rel).suffix.lower() not in MEDIA_SUFFIXES:
        raise ValueError("not a media file")
    return rel


def _same_filename(left: str, right: str) -> bool:
    """NFC/NFD and case-insensitive filename match (macOS upload vs Linux ECS)."""
    return unicodedata.normalize("NFC", left).casefold() == unicodedata.normalize("NFC", right).casefold()


def locate_media_file(rel: str) -> Path:
    """Find a media file even when the stored spelling differs from the request.

    Scans report NFC paths. S3 objects created on macOS are often NFD, and the
    ECS working copy keeps that spelling. ``root / nfc_path`` then misses the
    file on Linux, so a delete used to count as success without unlinking.
    """
    cleaned = normalize_media_rel(rel)
    root = vault_backend.vault_root()
    for form in (cleaned, unicodedata.normalize("NFD", cleaned)):
        try:
            target = vault_backend.resolve_vault_path(form)
        except ValueError:
            continue
        if target.is_file():
            return target
    parts = [part for part in cleaned.split("/") if part]
    cur = root
    for part in parts:
        if not cur.is_dir():
            raise FileNotFoundError(cleaned)
        hit = None
        try:
            children = list(cur.iterdir())
        except OSError as exc:
            raise FileNotFoundError(cleaned) from exc
        for child in children:
            if _same_filename(child.name, part):
                hit = child
                break
        if hit is None:
            raise FileNotFoundError(cleaned)
        cur = hit
    if not cur.is_file():
        raise FileNotFoundError(cleaned)
    root_resolved = root.resolve()
    resolved = cur.resolve()
    if root_resolved != resolved and root_resolved not in resolved.parents:
        raise ValueError("invalid path")
    return resolved


def _kept_file() -> Path:
    path = vault_backend.vault_root() / ".vault" / KEPT_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def load_kept() -> set[str]:
    path = _kept_file()
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("clearing keep list unreadable: %s", path)
        return set()
    raw = data.get("paths") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return set()
    kept: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        try:
            kept.add(normalize_media_rel(item))
        except ValueError:
            continue
    return kept


def save_kept(paths: Iterable[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in paths:
        try:
            rel = normalize_media_rel(item)
        except ValueError:
            continue
        key = rel.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(rel)
    cleaned.sort(key=str.casefold)
    path = _kept_file()
    path.write_text(
        json.dumps({"paths": cleaned}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return cleaned


def remember_kept(paths: Iterable[str]) -> list[str]:
    current = load_kept()
    for item in paths:
        current.add(normalize_media_rel(item))
    return save_kept(current)


def forget_kept(paths: Iterable[str], *, all_paths: bool = False) -> list[str]:
    if all_paths:
        return save_kept([])
    drop = {normalize_media_rel(item).casefold() for item in paths}
    return save_kept(rel for rel in load_kept() if rel.casefold() not in drop)


def _strip_code(text: str) -> str:
    text = vault_index.FENCE_RE.sub(" ", text or "")
    return vault_index.INLINE_CODE_RE.sub(" ", text)


def _is_remote_or_absolute(ref: str) -> bool:
    low = ref.lower()
    return (
        not ref
        or low.startswith(("http://", "https://", "data:", "blob:", "mailto:"))
        or ref.startswith("/")
        or ref.startswith("#")
    )


def _clean_destination(raw: str) -> str:
    dest = (raw or "").strip()
    if dest.startswith("<"):
        end = dest.find(">")
        if end > 1:
            dest = dest[1:end].strip()
    else:
        match = re.match(r"^(\S+)", dest)
        if match:
            dest = match.group(1)
    try:
        dest = unquote(dest)
    except Exception:
        pass
    dest = _nfc(dest).replace("\\", "/").strip()
    dest = dest.split("?", 1)[0].split("#", 1)[0]
    while dest.startswith("./"):
        dest = dest[2:]
    return dest


def _join_rel(parent: str, rel: str) -> str:
    parts = [*(parent.split("/") if parent else []), *rel.split("/")]
    out: list[str] = []
    for part in parts:
        if not part or part == ".":
            continue
        if part == "..":
            if out:
                out.pop()
            continue
        out.append(part)
    return "/".join(out)


def _media_key(path: str) -> str:
    """NFC + casefold. macOS and S3 often store Korean folders as NFD."""
    return _nfc(path).casefold()


def _same_folder_media(ref: str, folder: str) -> str | None:
    """Return the vault-relative media path when ``ref`` stays inside ``folder``."""
    folder = _nfc(folder)
    dest = _clean_destination(ref)
    if _is_remote_or_absolute(dest):
        return None
    if Path(dest).suffix.lower() not in MEDIA_SUFFIXES:
        return None
    resolved = _nfc(_join_rel(folder, dest))
    parent = resolved.rsplit("/", 1)[0] if "/" in resolved else ""
    if parent != folder:
        return None
    return resolved


def referenced_media_keys(text: str, folder: str) -> set[str]:
    """Casefold vault paths of same-folder media mentioned in ``text``."""
    folder = _nfc(folder)
    body = _strip_code(text)
    found: set[str] = set()

    def add(raw: str) -> None:
        rel = _same_folder_media(raw, folder)
        if rel:
            found.add(_media_key(rel))

    for match in _MD_DEST_RE.finditer(body):
        add(match.group(1))
    for match in vault_index.WIKI_LINK_RE.finditer(body):
        target = (match.group(2) or "").strip()
        if "#" in target:
            target = target.split("#", 1)[0].strip()
        add(target)
    for match in _HTML_SRC_RE.finditer(body):
        add(match.group(1))
    return found


def scan_tree(
    root: Path,
    kept: Iterable[str] | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Scan ``root`` for same-folder media that markdown does not reference."""
    root = root.resolve()

    def report(info: dict[str, Any]) -> None:
        if on_progress is None:
            return
        try:
            on_progress(info)
        except Exception:
            logger.debug("clearing progress callback failed", exc_info=True)

    kept_keys: set[str] = set()
    for item in kept or []:
        try:
            kept_keys.add(normalize_media_rel(item).casefold())
        except ValueError:
            continue
    media: list[dict[str, Any]] = []
    markdown: dict[str, list[Path]] = {}
    seen = 0
    note_count = 0

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in SKIP_DIR_NAMES and not name.startswith(".")
        ]
        folder_path = Path(dirpath)
        try:
            folder = folder_path.resolve().relative_to(root).as_posix()
        except ValueError:
            continue
        if folder == ".":
            folder = ""
        folder = _nfc(folder)
        for name in filenames:
            if name.startswith("."):
                continue
            path = folder_path / name
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            rel = f"{folder}/{name}" if folder else name
            rel = _nfc(rel)
            seen += 1
            if suffix == ".md":
                note_count += 1
                markdown.setdefault(folder, []).append(path)
            elif suffix in MEDIA_SUFFIXES:
                try:
                    stat = path.stat()
                    size = stat.st_size
                    mtime = stat.st_mtime
                except OSError:
                    size = 0
                    mtime = 0.0
                media.append(
                    {
                        "path": rel,
                        "name": _nfc(name),
                        "folder": folder,
                        "size": size,
                        "mtime": mtime,
                        "suffix": suffix,
                    }
                )
            if seen == 1 or seen % 40 == 0:
                report(
                    {
                        "phase": "walk",
                        "file": rel,
                        "file_i": seen,
                        "pct": 20,
                        "message": (
                            f"폴더를 살펴보는 중… 미디어 {len(media)} · 노트 {note_count}"
                        ),
                    }
                )

    report(
        {
            "phase": "walk",
            "file": None,
            "file_i": seen,
            "pct": 30,
            "message": f"파일 목록을 모았습니다. 미디어 {len(media)} · 노트 {note_count}",
        }
    )

    referenced: set[str] = set()
    notes = [(folder, path) for folder, paths in markdown.items() for path in paths]
    note_total = len(notes)
    for index, (folder, note) in enumerate(notes, start=1):
        rel = f"{folder}/{note.name}" if folder else note.name
        rel = _nfc(rel)
        pct = 30 + int(round(index / note_total * 65)) if note_total else 95
        report(
            {
                "phase": "read",
                "file": rel,
                "file_i": index,
                "file_n": note_total,
                "pct": pct,
                "message": f"마크다운에서 미디어 참조를 확인하는 중… ({index}/{note_total})",
            }
        )
        try:
            text = note.read_text(encoding="utf-8", errors="replace")
        except OSError:
            logger.warning("clearing skipped unreadable note %s", note)
            continue
        referenced.update(referenced_media_keys(text, folder))

    items: list[dict[str, Any]] = []
    referenced_count = 0
    kept_count = 0
    kept_existing: list[str] = []
    for item in media:
        key = _media_key(item["path"])
        if key in referenced:
            referenced_count += 1
            continue
        if key in kept_keys:
            kept_count += 1
            kept_existing.append(item["path"])
            continue
        items.append(item)

    items.sort(key=lambda item: item["path"].casefold())
    return {
        "ok": True,
        "media_count": len(media),
        "referenced_count": referenced_count,
        "kept_count": kept_count,
        "kept_paths": sorted(kept_existing, key=str.casefold),
        "items": items,
    }


def scan_vault(
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Scan the current user's vault and drop stale keep entries."""
    root = vault_backend.vault_root()
    kept = load_kept()
    result = scan_tree(root, kept, on_progress=on_progress)
    live = {path.casefold() for path in result["kept_paths"]}
    if {path.casefold() for path in kept} != live:
        save_kept(result["kept_paths"])
    return result


_job_lock = threading.Lock()
_job_threads: dict[str, threading.Thread] = {}
_job_states: dict[str, dict[str, Any]] = {}


def _idle_state() -> dict[str, Any]:
    return {
        "job_id": None,
        "status": "idle",
        "message": None,
        "error": None,
        "progress": None,
        "scan": None,
        "updated_at": 0.0,
    }


def _set_job(user_id: str, **kwargs: Any) -> None:
    with _job_lock:
        state = _job_states.setdefault(user_id, _idle_state())
        state.update(kwargs)
        state["updated_at"] = time.time()


def _public_state(state: dict[str, Any]) -> dict[str, Any]:
    status = state.get("status") or "idle"
    busy = status in {"queued", "running"}
    payload: dict[str, Any] = {
        "ok": status != "error",
        "job_id": state.get("job_id"),
        "status": status,
        "busy": busy,
        "message": state.get("message"),
        "error": state.get("error"),
        "progress": state.get("progress"),
        "updated_at": state.get("updated_at") or 0.0,
    }
    if status == "ready":
        payload["scan"] = state.get("scan")
    return payload


def get_clearing_status() -> dict[str, Any]:
    user_id = vault_backend.current_user_id() or ""
    with _job_lock:
        thread = _job_threads.get(user_id)
        state = dict(_job_states.get(user_id) or _idle_state())
        if state.get("status") in {"queued", "running"} and (
            thread is None or not thread.is_alive()
        ):
            state.update(
                {
                    "status": "error",
                    "message": "검사가 중단되었습니다. 다시 Clearing 해 주세요.",
                    "error": "clearing_worker_dead",
                    "busy": False,
                }
            )
            state["updated_at"] = time.time()
            _job_states[user_id] = state
    return _public_state(state)


def _run_clearing_job(user_id: str) -> None:
    def on_progress(info: dict[str, Any]) -> None:
        _set_job(
            user_id,
            status="running",
            message=info.get("message") or "미디어 참조를 검사하고 있습니다…",
            error=None,
            progress={
                "file": info.get("file"),
                "file_i": info.get("file_i"),
                "file_n": info.get("file_n"),
                "pct": info.get("pct"),
                "phase": info.get("phase"),
            },
        )

    try:
        with vault_backend.user_scope(user_id):
            _set_job(
                user_id,
                status="running",
                message="미디어 참조 검사를 시작합니다…",
                error=None,
                progress={"pct": 0, "phase": "start"},
                scan=None,
            )
            if vault_backend.backend_mode() == "s3":
                _set_job(
                    user_id,
                    status="running",
                    message="S3 vault를 확인하는 중…",
                    progress={"pct": 5, "phase": "sync"},
                )
                vault_backend.try_sync_from_s3()
            result = scan_vault(on_progress=on_progress)
            orphan_count = len(result.get("items") or [])
            _set_job(
                user_id,
                status="ready",
                message=f"검사를 마쳤습니다. 참조되지 않은 미디어 {orphan_count}개.",
                error=None,
                progress={"pct": 100, "phase": "done"},
                scan=result,
            )
    except Exception as exc:
        logger.exception("Clearing job failed")
        _set_job(
            user_id,
            status="error",
            message="미디어 참조 검사에 실패했습니다.",
            error=str(exc),
            scan=None,
        )
    finally:
        with _job_lock:
            current = _job_threads.get(user_id)
            if current is threading.current_thread():
                _job_threads.pop(user_id, None)


def start_clearing_job() -> dict[str, Any]:
    """Queue a background clearing scan. Returns the current job if one is running."""
    user_id = vault_backend.current_user_id()
    if not user_id:
        return {
            "ok": False,
            "status": "error",
            "busy": False,
            "message": "vault user_id is required",
            "error": "missing_user",
        }
    with _job_lock:
        thread = _job_threads.get(user_id)
        if thread is not None and thread.is_alive():
            state = dict(_job_states.get(user_id) or _idle_state())
            payload = _public_state(state)
            payload["message"] = state.get("message") or "이미 검사를 진행하고 있습니다."
            return payload
        job_id = uuid.uuid4().hex
        _job_states[user_id] = {
            "job_id": job_id,
            "status": "queued",
            "message": "미디어 참조 검사를 백그라운드에서 시작합니다…",
            "error": None,
            "progress": {"pct": 0, "phase": "queued"},
            "scan": None,
            "updated_at": time.time(),
        }
        worker = threading.Thread(
            target=_run_clearing_job,
            args=(user_id,),
            name=f"vault-clearing-{job_id[:8]}",
            daemon=True,
        )
        _job_threads[user_id] = worker
        worker.start()
        return _public_state(dict(_job_states[user_id]))


def delete_one_media(rel: str) -> str:
    """Remove one media file. Caller rebuilds the index and flushes S3 after a batch."""
    from application import vault_order, vault_share, vault_sync

    target = locate_media_file(rel)
    root = vault_backend.vault_root()
    cleaned = target.relative_to(root).as_posix()
    if cleaned != rel:
        logger.info("Clearing delete path %s resolved to %s", rel, cleaned)
    vault_share.remove_shares_for_path(cleaned)
    if vault_backend.backend_mode() == "s3":
        vault_sync.enqueue_delete_tree(cleaned)
    else:
        try:
            vault_share.delete_vault_tree_from_s3(cleaned)
        except Exception:
            logger.debug("clearing S3 delete skipped for %s", cleaned, exc_info=True)
    try:
        target.unlink()
    except FileNotFoundError:
        pass
    vault_order.notify_deleted(cleaned)
    return cleaned


_delete_lock = threading.Lock()
_delete_threads: dict[str, threading.Thread] = {}
_delete_states: dict[str, dict[str, Any]] = {}


def _idle_delete_state() -> dict[str, Any]:
    return {
        "job_id": None,
        "status": "idle",
        "message": None,
        "error": None,
        "progress": None,
        "deleted": [],
        "errors": [],
        "updated_at": 0.0,
    }


def _set_delete(user_id: str, **kwargs: Any) -> None:
    with _delete_lock:
        state = _delete_states.setdefault(user_id, _idle_delete_state())
        state.update(kwargs)
        state["updated_at"] = time.time()


def _public_delete_state(state: dict[str, Any]) -> dict[str, Any]:
    status = state.get("status") or "idle"
    return {
        "ok": status != "error",
        "job_id": state.get("job_id"),
        "status": status,
        "busy": status in {"queued", "running"},
        "message": state.get("message"),
        "error": state.get("error"),
        "progress": state.get("progress"),
        "deleted": list(state.get("deleted") or []),
        "errors": list(state.get("errors") or []),
        "updated_at": state.get("updated_at") or 0.0,
    }


def get_delete_status() -> dict[str, Any]:
    user_id = vault_backend.current_user_id() or ""
    with _delete_lock:
        thread = _delete_threads.get(user_id)
        state = dict(_delete_states.get(user_id) or _idle_delete_state())
        if state.get("status") in {"queued", "running"} and (
            thread is None or not thread.is_alive()
        ):
            state.update(
                {
                    "status": "error",
                    "message": "삭제가 중단되었습니다. 다시 시도해 주세요.",
                    "error": "clearing_delete_worker_dead",
                }
            )
            state["updated_at"] = time.time()
            _delete_states[user_id] = state
    return _public_delete_state(state)


def _run_delete_job(user_id: str, paths: list[str]) -> None:
    from application import vault_index, vault_sync

    deleted: list[str] = []
    errors: list[dict[str, str]] = []
    total = len(paths)
    try:
        with vault_backend.user_scope(user_id):
            for index, raw in enumerate(paths, start=1):
                label = raw.rsplit("/", 1)[-1]
                _set_delete(
                    user_id,
                    status="running",
                    message=f"삭제 중… ({index}/{total})",
                    error=None,
                    progress={
                        "phase": "delete",
                        "file": label,
                        "file_i": index,
                        "file_n": total,
                        "pct": int(round((index - 1) / total * 100)) if total else 0,
                    },
                    deleted=list(deleted),
                    errors=list(errors),
                )
                try:
                    cleaned = delete_one_media(raw)
                except FileNotFoundError:
                    logger.warning("Clearing delete file not found: %s", raw)
                    try:
                        normalize_media_rel(raw)
                    except ValueError as exc:
                        errors.append({"path": raw, "error": str(exc)})
                        continue
                    deleted.append(raw)
                except ValueError as exc:
                    errors.append({"path": raw, "error": str(exc)})
                    continue
                except Exception as exc:
                    logger.exception("Clearing delete failed for %s", raw)
                    errors.append({"path": raw, "error": str(exc)})
                    continue
                else:
                    # Report the path the list sent. The on-disk spelling can
                    # differ (NFC vs NFD), which left deleted rows on screen.
                    deleted.append(raw)
                _set_delete(
                    user_id,
                    status="running",
                    message=f"삭제 중… ({index}/{total})",
                    progress={
                        "phase": "delete",
                        "file": label,
                        "file_i": index,
                        "file_n": total,
                        "pct": int(round(index / total * 100)) if total else 100,
                    },
                    deleted=list(deleted),
                    errors=list(errors),
                )
            if deleted:
                try:
                    forget_kept(deleted)
                except ValueError:
                    logger.debug("clearing keep cleanup skipped", exc_info=True)
            try:
                vault_index.rebuild_index()
            except Exception:
                logger.exception("Index rebuild after clearing delete failed")
            if vault_backend.backend_mode() == "s3" and deleted:
                def on_flush(info: dict[str, Any]) -> None:
                    file_i = info.get("file_i") or 0
                    file_n = info.get("file_n") or 0
                    _set_delete(
                        user_id,
                        status="running",
                        message=(
                            f"S3에서 삭제하는 중… ({file_i}/{file_n})"
                            if file_n
                            else "S3에서 삭제하는 중…"
                        ),
                        error=None,
                        progress={
                            "phase": "delete",
                            "file": info.get("file"),
                            "file_i": file_i,
                            "file_n": file_n,
                            "pct": info.get("pct") if info.get("pct") is not None else 0,
                        },
                        deleted=list(deleted),
                        errors=list(errors),
                    )

                flush = vault_sync.flush_pending_to_s3(on_progress=on_flush)
                logger.info(
                    "Clearing delete S3 flush ok=%s flushed=%s remaining=%s",
                    flush.get("ok"),
                    flush.get("flushed"),
                    flush.get("remaining"),
                )
                if not flush.get("ok"):
                    errors.append(
                        {
                            "path": "",
                            "error": flush.get("reason") or "S3 삭제에 실패했습니다.",
                        }
                    )
            _set_delete(
                user_id,
                status="ready" if not errors else "error",
                message=(
                    f"삭제했습니다. {len(deleted)}개."
                    if not errors
                    else f"{len(deleted)}개를 삭제했고 {len(errors)}개는 실패했습니다."
                ),
                error=None if not errors else errors[0]["error"],
                progress={"phase": "done", "pct": 100, "file_i": total, "file_n": total},
                deleted=list(deleted),
                errors=list(errors),
            )
    except Exception as exc:
        logger.exception("Clearing delete job failed")
        _set_delete(
            user_id,
            status="error",
            message="삭제에 실패했습니다.",
            error=str(exc),
            deleted=list(deleted),
            errors=list(errors),
        )
    finally:
        with _delete_lock:
            current = _delete_threads.get(user_id)
            if current is threading.current_thread():
                _delete_threads.pop(user_id, None)


def start_delete_job(paths: list[str]) -> dict[str, Any]:
    """Queue background deletion. Returns the running job if one is already active."""
    user_id = vault_backend.current_user_id()
    if not user_id:
        return {
            "ok": False,
            "status": "error",
            "busy": False,
            "message": "vault user_id is required",
            "error": "missing_user",
            "deleted": [],
            "errors": [],
        }
    if not paths:
        return {
            "ok": False,
            "status": "error",
            "busy": False,
            "message": "paths is required",
            "error": "paths is required",
            "deleted": [],
            "errors": [],
        }
    with _delete_lock:
        thread = _delete_threads.get(user_id)
        if thread is not None and thread.is_alive():
            state = dict(_delete_states.get(user_id) or _idle_delete_state())
            payload = _public_delete_state(state)
            payload["message"] = state.get("message") or "이미 삭제를 진행하고 있습니다."
            return payload
        job_id = uuid.uuid4().hex
        total = len(paths)
        _delete_states[user_id] = {
            "job_id": job_id,
            "status": "queued",
            "message": f"삭제를 시작합니다… (0/{total})",
            "error": None,
            "progress": {"pct": 0, "phase": "queued", "file_i": 0, "file_n": total},
            "deleted": [],
            "errors": [],
            "updated_at": time.time(),
        }
        worker = threading.Thread(
            target=_run_delete_job,
            args=(user_id, list(paths)),
            name=f"vault-clearing-delete-{job_id[:8]}",
            daemon=True,
        )
        _delete_threads[user_id] = worker
        worker.start()
        return _public_delete_state(dict(_delete_states[user_id]))
