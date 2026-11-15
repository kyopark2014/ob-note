"""Vault file tree CRUD."""

from __future__ import annotations

import logging
import mimetypes
import re
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

from application.api.routes_auth import require_user_id
from application import (
    note_store,
    notes_db,
    vault_backend,
    vault_clearing,
    vault_compress,
    vault_companion_assets,
    vault_index,
    vault_order,
    vault_share,
    vault_sync,
    viewer_html,
)

logger = logging.getLogger("routes_files")

router = APIRouter(prefix="/api/files", tags=["files"])

HIDDEN_SKIP = {".git", ".keep", ".gitkeep"}
# Empty folders need a marker object so they survive S3 sync / tree rebuild.
FOLDER_KEEP_NAME = ".keep"
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_VIDEO_UPLOAD_BYTES = 200 * 1024 * 1024
TEXT_VIEWER_EXTENSIONS = {
    ".md",
    ".markdown",
    ".txt",
    ".csv",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".html",
    ".htm",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".rst",
}
INLINE_BINARY_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".pdf",
    ".mp4",
    ".m4v",
    ".webm",
    ".mp3",
    ".wav",
}
VIDEO_EXTENSIONS = {
    ".mp4",
    ".m4v",
    ".webm",
}
VIDEO_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
}
TEXT_VIEWER_MAX_BYTES = 2 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
}
ALLOWED_UPLOAD_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".md",
    ".markdown",
    ".txt",
    ".csv",
    ".json",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".yml",
    ".yaml",
    ".xml",
    ".html",
    ".htm",
    ".rst",
    ".dxf",
    ".pdf",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".zip",
    ".mp4",
    ".m4v",
    ".webm",
}


def media_type_for_name(name: str) -> str:
    """MIME type for inline playback. mp4 must not fall through to application/mp4."""
    ext = Path(name).suffix.lower()
    if ext in VIDEO_MEDIA_TYPES:
        return VIDEO_MEDIA_TYPES[ext]
    media, _ = mimetypes.guess_type(name)
    return media or "application/octet-stream"


class WriteBody(BaseModel):
    path: str = Field(..., min_length=1, max_length=1024)
    content: str = ""
    # Editor save: H1 becomes the filename. Same note_id, old path removed.
    sync_filename: bool = False


class RenderBody(BaseModel):
    """Markdown → HTML for MCP App preview (same pipeline as public /s/{token})."""

    content: str = ""
    path: str = ""  # optional note path (reserved for relative-link context)


class AppendBody(BaseModel):
    path: str = Field(..., min_length=1, max_length=1024)
    content: str = ""
    create: bool = True
    separator: str = "\n"


class MkdirBody(BaseModel):
    path: str = Field(..., min_length=1, max_length=1024)


class RenameBody(BaseModel):
    from_path: str = Field(..., min_length=1, max_length=1024)
    to_path: str = Field(..., min_length=1, max_length=1024)
    # Drag-and-drop of a note moves the markdown but copies sibling images.
    copy_companions: bool = False


class CopyBody(BaseModel):
    from_path: str = Field(..., min_length=1, max_length=1024)
    to_path: str = Field(..., min_length=1, max_length=1024)


class DeleteBody(BaseModel):
    path: str = Field(..., min_length=1, max_length=1024)


class DuplicateBody(BaseModel):
    path: str = Field(..., min_length=1, max_length=1024)


class CompressBody(BaseModel):
    path: str = Field("", max_length=1024)
    scope: str = Field("folder", max_length=16)


class CompressItemBody(BaseModel):
    id: str = Field(..., min_length=8, max_length=64)


class ClearingPathsBody(BaseModel):
    paths: list[str] = Field(default_factory=list, max_length=500)
    all: bool = False


class ReorderBody(BaseModel):
    folder: str = Field("", max_length=1024, description="Parent folder path; empty = vault root")
    names: list[str] = Field(..., min_length=1, max_length=2000)


def _tree_node(path: Path, root: Path) -> dict[str, Any]:
    rel = path.relative_to(root).as_posix()
    if path.is_dir():
        children = []
        for child in path.iterdir():
            if child.name in HIDDEN_SKIP:
                continue
            if child.name == ".vault":
                continue
            children.append(_tree_node(child, root))
        children = vault_order.apply_order(rel, children)
        return {"name": path.name, "path": rel, "type": "folder", "children": children}
    node: dict[str, Any] = {
        "name": path.name,
        "path": rel,
        "type": "file",
        "ext": path.suffix.lower().lstrip("."),
    }
    if path.suffix.lower() == ".md":
        row = notes_db.get_by_path(rel)
        if row:
            node["note_id"] = row["note_id"]
            node["title"] = row["title"]
            node["size_bytes"] = row["size_bytes"]
            node["created_at"] = row["created_at"]
            node["updated_at"] = row["updated_at"]
    return node


def _local_tree_children() -> list[dict[str, Any]]:
    """Build the file panel tree from the on-disk vault."""
    # Keep SQLite registry aligned before serving the local tree.
    try:
        notes_db.ensure_db()
        if notes_db.is_empty():
            notes_db.sync_from_filesystem()
    except Exception:
        logger.exception("notes_db sync before tree failed")
    root = vault_backend.vault_root()
    children = []
    for child in root.iterdir():
        if child.name in HIDDEN_SKIP or child.name == ".vault":
            continue
        children.append(_tree_node(child, root))
    # Linux keeps NFC and NFD Hangul as different directories. Show one folder.
    return vault_sync.collapse_nfc_nodes(vault_order.apply_order("", children))


def _mcp_note_html(content: str, rel: str) -> str:
    """MCP App HTML; media use short-lived signed URLs (nothing is shared or persisted)."""
    from application import preview_sign, utils

    base = utils.sharing_url()
    user = vault_backend.current_user_id() or ""
    if not (rel and base.startswith("https://") and user):
        return viewer_html.build_mcp_note_html(content)
    return viewer_html.build_mcp_note_html(
        content,
        note_path=rel,
        url_for_asset=lambda doc, path: preview_sign.build_url(base, user, doc, path),
    )


# ---------------------------------------------------------------------------
# DynamoDB mode: ``note_store`` is the source of truth; S3 and the local working
# copy are derived from it (see docs/DYNAMODB_NOTES.md).
# ---------------------------------------------------------------------------


def _ddb_attach_meta(nodes: list[dict[str, Any]], meta: dict[str, dict[str, Any]]) -> None:
    for node in nodes:
        if node.get("type") == "folder":
            _ddb_attach_meta(node.get("children") or [], meta)
            continue
        row = meta.get(node.get("path") or "")
        if row:
            node["note_id"] = row.get("note_id")
            node["title"] = row.get("title") or ""
            node["size_bytes"] = int(row.get("size_bytes") or 0)
            node["created_at"] = row.get("created_at")
            node["updated_at"] = row.get("updated_at")


def _ddb_tree() -> dict:
    note_store.restore_settings()
    items = note_store.list_items()
    rels = [
        f"{i['path']}/{FOLDER_KEEP_NAME}" if i.get("kind") == note_store.KIND_FOLDER else i["path"]
        for i in items
    ]
    children = vault_sync.build_tree_from_rels(rels)
    _ddb_attach_meta(children, {i["path"]: i for i in items if i.get("note_id")})
    return {
        "root": ".",
        "mode": vault_backend.backend_mode(),
        "source": "dynamodb",
        "children": children,
    }


def _iso_ts(value: Any) -> float:
    from datetime import datetime, timezone

    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def _ddb_list_files(prefix: str, ext: str) -> dict:
    cleaned = note_store.nfc(prefix)
    if ".." in cleaned.split("/"):
        raise HTTPException(status_code=400, detail="Path traversal is not allowed")
    vault_index.ensure_index()
    exact = note_store.get(cleaned) if cleaned else None
    single = exact is not None and exact.get("kind") != note_store.KIND_FOLDER
    items = [exact] if single else note_store.list_items(cleaned)
    want_ext = (ext or "md").lower().lstrip(".")
    all_ext = want_ext in {"*", "all", ""}
    files: list[dict[str, Any]] = []
    for it in items:
        if it.get("kind") == note_store.KIND_FOLDER:
            continue
        rel = it["path"]
        parts = rel.split("/")
        if any(part in HIDDEN_SKIP or part == ".vault" for part in parts):
            continue
        suffix = Path(rel).suffix.lower()
        if not single and not all_ext and suffix.lstrip(".") != want_ext:
            continue
        entry: dict[str, Any] = {
            "path": rel,
            "name": parts[-1],
            "size": int(it.get("size_bytes") or 0),
            "mtime": _iso_ts(it.get("updated_at")),
        }
        if suffix == ".md" and it.get("note_id"):
            entry["note_id"] = it["note_id"]
            entry["title"] = it.get("title") or ""
            entry["size_bytes"] = int(it.get("size_bytes") or 0)
            entry["created_at"] = it.get("created_at")
            entry["updated_at"] = it.get("updated_at")
            meta = vault_index.get_meta(rel)
            preview = (meta.body_preview if meta else "") or ""
            if preview:
                entry["body_preview"] = preview
                entry["snippet"] = preview
        files.append(entry)
    if single:
        return {"prefix": cleaned, "ext": ext, "files": files}
    return {"prefix": cleaned, "ext": ext, "count": len(files), "files": files}


def _ddb_note_fields(row: dict[str, Any] | None) -> dict[str, Any]:
    row = row or {}
    return {
        "note_id": row.get("note_id"),
        "title": row.get("title"),
        "size_bytes": row.get("size_bytes"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "created": row.get("created"),
    }


def _ddb_reindex(*paths: str) -> None:
    for rel in paths:
        if notes_db.is_markdown_path(rel):
            vault_index.update_note(rel)
    vault_index.rebuild_index()


def _ddb_write(body: "WriteBody") -> dict:
    src_rel = note_store.nfc(body.path)
    if not src_rel or ".." in src_rel.split("/"):
        raise HTTPException(status_code=400, detail="Path traversal is not allowed")
    text = body.content
    final_rel = _retitle_rel(src_rel, text) if body.sync_filename else src_rel
    renamed_from: str | None = None
    if final_rel != src_rel:
        renamed_from = src_rel
        # Untitled -> real title: the row moves, so note_id (and its chat) is kept.
        moved = note_store.rename(src_rel, final_rel)
        if moved:
            notes_db._forget_chat(moved.get("dropped_note_ids") or [])
    try:
        row = note_store.put_text(final_rel, text)
    except IsADirectoryError as e:
        raise HTTPException(status_code=400, detail="Path is a directory") from e
    if notes_db.is_markdown_path(final_rel):
        if renamed_from:
            vault_index.remove_note(renamed_from)
        _ddb_reindex(final_rel)
    if renamed_from:
        vault_share.rewrite_share_paths(src_rel, final_rel)
        vault_order.notify_renamed(src_rel, final_rel)
    meta = vault_index.get_meta(final_rel)
    return {
        "ok": True,
        "path": final_rel,
        "renamed_from": renamed_from,
        "word_count": meta.word_count if meta else None,
        "char_count": meta.char_count if meta else None,
        **_ddb_note_fields(row),
    }


def _ddb_append(body: "AppendBody") -> dict:
    rel = note_store.nfc(body.path)
    try:
        row = note_store.append_text(
            rel, body.content or "", create=body.create, separator=body.separator
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail="File not found") from e
    except IsADirectoryError as e:
        raise HTTPException(status_code=400, detail="Path is a directory") from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if notes_db.is_markdown_path(rel):
        _ddb_reindex(rel)
    meta = vault_index.get_meta(rel)
    return {
        "ok": True,
        "path": body.path,
        "appended": len(body.content or ""),
        "word_count": meta.word_count if meta else None,
        "char_count": meta.char_count if meta else None,
        **_ddb_note_fields(row),
    }


def _ddb_rename(body: "RenameBody") -> dict:
    from_rel = note_store.nfc(body.from_path)
    to_rel = note_store.nfc(body.to_path)
    for rel in (from_rel, to_rel):
        if not rel or ".." in rel.split("/"):
            raise HTTPException(status_code=400, detail="Path traversal is not allowed")
    if from_rel == to_rel:
        return {"ok": True, "from": from_rel, "to": to_rel, "companion_images": None}
    source = note_store.get(from_rel)
    if source is None and not note_store.is_dir(from_rel):
        found = note_store.find(from_rel)  # Obsidian-style unique basename fallback
        if found is None:
            raise HTTPException(status_code=404, detail="Source not found")
        from_rel = found["path"]
        source = found
    was_file = source is not None and source.get("kind") != note_store.KIND_FOLDER
    try:
        res = note_store.rename(from_rel, to_rel)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if res is None:
        raise HTTPException(status_code=404, detail="Source not found")
    notes_db._forget_chat(res.get("dropped_note_ids") or [])
    # Filename -> H1 (mirrors H1 -> filename on save).
    if was_file and notes_db.is_markdown_path(to_rel) and Path(from_rel).stem != Path(to_rel).stem:
        try:
            text = note_store.read_text(to_rel) or ""
            synced = _replace_or_set_h1(text, Path(to_rel).stem)
            if synced != text:
                note_store.put_text(to_rel, synced)
        except Exception:
            logger.exception("H1 sync after rename failed for %s", to_rel)
    for old, new in res.get("moved") or []:
        if notes_db.is_markdown_path(old):
            vault_index.remove_note(old)
    for _old, new in res.get("moved") or []:
        if notes_db.is_markdown_path(new):
            vault_index.update_note(new)
    vault_index.rebuild_index()
    vault_share.rewrite_share_paths(from_rel, to_rel)
    vault_order.notify_renamed(from_rel, to_rel)
    companion_images: str | None = None
    if (
        was_file
        and notes_db.is_markdown_path(to_rel)
        and vault_companion_assets.parents_differ(from_rel, to_rel)
    ):
        schedule = (
            vault_companion_assets.schedule_copy_companion_images
            if body.copy_companions
            else vault_companion_assets.schedule_move_companion_images
        )
        if schedule(from_rel, to_rel):
            companion_images = "queued"
    return {"ok": True, "from": from_rel, "to": to_rel, "companion_images": companion_images}


def _ddb_copy(body: "CopyBody") -> dict:
    src = note_store.nfc(body.from_path)
    dst = note_store.nfc(body.to_path)
    for rel in (src, dst):
        if not rel or ".." in rel.split("/"):
            raise HTTPException(status_code=400, detail="Path traversal is not allowed")
    item = note_store.get(src)
    if item is None and not note_store.is_dir(src):
        raise HTTPException(status_code=404, detail="Source not found")
    if item is None or item.get("kind") == note_store.KIND_FOLDER:
        raise HTTPException(status_code=400, detail="Only files can be copied")
    if src == dst:
        return {"ok": True, "from": src, "to": src, "overwritten": False, "companion_images": None}
    existing = note_store.get(dst)
    if existing is not None and existing.get("kind") == note_store.KIND_FOLDER:
        raise HTTPException(status_code=400, detail="Destination is a directory")
    note_store.copy(src, dst)
    if notes_db.is_markdown_path(dst):
        _ddb_reindex(dst)
    vault_order.notify_copied(dst)
    companion_images: str | None = None
    if notes_db.is_markdown_path(dst) and vault_companion_assets.parents_differ(src, dst):
        if vault_companion_assets.schedule_copy_companion_images(src, dst):
            companion_images = "queued"
    return {
        "ok": True,
        "from": src,
        "to": dst,
        "overwritten": existing is not None,
        "companion_images": companion_images,
    }


def _ddb_delete(body: "DeleteBody") -> dict:
    rel = note_store.nfc(body.path)
    if not rel or ".." in rel.split("/"):
        raise HTTPException(status_code=400, detail="Path traversal is not allowed")
    if note_store.get(rel) is None and not note_store.is_dir(rel):
        raise HTTPException(status_code=404, detail="Not found")
    # Revoke public shares first so no token is left pointing at a deleted note.
    vault_share.remove_shares_for_path(body.path)
    removed = note_store.delete(rel)
    notes_db._forget_chat([r["note_id"] for r in removed if r.get("note_id")])
    for r in removed:
        if r.get("path") and notes_db.is_markdown_path(r["path"]):
            vault_index.remove_note(r["path"])
    vault_index.rebuild_index()
    vault_order.notify_deleted(body.path)
    return {"ok": True, "path": body.path}


def _ddb_unique_copy_path(src: str, *, folder: bool) -> str:
    parent = str(Path(src).parent).replace("\\", "/")
    parent = "" if parent == "." else parent
    base = Path(src).name if folder else Path(src).stem
    suffix = "" if folder else Path(src).suffix
    n = 2
    while True:
        name = f"{base} {n}{suffix}"
        candidate = f"{parent}/{name}" if parent else name
        if note_store.get(candidate) is None and not note_store.is_dir(candidate):
            return candidate
        n += 1


def _ddb_duplicate(body: "DuplicateBody") -> dict:
    src = note_store.nfc(body.path)
    if not src or ".." in src.split("/"):
        raise HTTPException(status_code=400, detail="Path traversal is not allowed")
    item = note_store.get(src)
    is_folder = item is None and note_store.is_dir(src) or (
        item is not None and item.get("kind") == note_store.KIND_FOLDER
    )
    if item is None and not is_folder:
        raise HTTPException(status_code=404, detail="Not found")
    dst = _ddb_unique_copy_path(src, folder=is_folder)
    if is_folder:
        note_store.mkdir(dst)
        for it in note_store.list_items(src):
            target = dst + it["path"][len(src):]
            if it.get("kind") == note_store.KIND_FOLDER:
                note_store.mkdir(target)
            else:
                note_store.copy(it["path"], target)
    else:
        note_store.copy(src, dst)  # a duplicate gets a fresh note_id
    vault_index.rebuild_index()
    return {"ok": True, "from": body.path, "to": dst}


@router.get("/tree")
def get_tree(request: Request) -> dict:
    require_user_id(request)
    if note_store.enabled():
        return _ddb_tree()
    notes_db.ensure_db()
    mode = vault_backend.backend_mode()
    if mode == "s3":
        # While local→S3 ops are in flight, local disk is ahead of remote keys.
        # Serving S3-only here made renames/moves briefly disappear from the sidebar
        # (delete lands before put finishes). Prefer local until the queue drains.
        try:
            if vault_sync.pending_count() > 0:
                return {
                    "root": ".",
                    "mode": mode,
                    "source": "local-pending",
                    "children": _local_tree_children(),
                }
            # Tree from S3 object keys only — never await sync/flush here.
            # A stuck S3 sync was freezing the UI on "Loading vault…".
            rels = vault_sync.list_remote_vault_rels()
            children = vault_sync.build_tree_from_rels(rels)
            return {
                "root": ".",
                "mode": mode,
                "source": "s3",
                "children": children,
            }
        except Exception:
            logger.exception("S3 tree list failed; falling back to local disk")
    return {
        "root": ".",
        "mode": mode,
        "source": "local" if mode != "s3" else "local-fallback",
        "children": _local_tree_children(),
    }


@router.get("/notes")
def list_notes_registry(request: Request) -> dict:
    """List all markdown notes tracked in the per-user SQLite registry."""
    require_user_id(request)
    try:
        notes_db.sync_from_filesystem()
    except Exception:
        logger.exception("notes_db sync before list failed")
    notes = notes_db.list_notes(order="updated_at")
    return {"count": len(notes), "notes": notes}


@router.get("/list")
def list_files(
    request: Request,
    prefix: str = "",
    ext: str = "md",
) -> dict:
    """Flat list of vault files (default: ``*.md``). Use ``ext=*`` for all files."""
    require_user_id(request)
    if note_store.enabled():
        return _ddb_list_files(prefix, ext)
    if vault_backend.backend_mode() == "s3":
        vault_backend.sync_from_s3()
    root = vault_backend.vault_root()
    cleaned = (prefix or "").replace("\\", "/").strip("/")
    if ".." in cleaned.split("/"):
        raise HTTPException(status_code=400, detail="Path traversal is not allowed")
    base = root / cleaned if cleaned else root
    if not base.exists():
        return {"prefix": cleaned, "ext": ext, "files": []}
    if base.is_file():
        rel = base.relative_to(root).as_posix()
        return {
            "prefix": cleaned,
            "ext": ext,
            "files": [
                {
                    "path": rel,
                    "name": base.name,
                    "size": base.stat().st_size,
                    "mtime": base.stat().st_mtime,
                }
            ],
        }

    want_ext = (ext or "md").lower().lstrip(".")
    all_ext = want_ext in {"*", "all", ""}
    files: list[dict[str, Any]] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(part in HIDDEN_SKIP or part == ".vault" for part in rel_parts):
            continue
        if not all_ext and path.suffix.lower().lstrip(".") != want_ext:
            continue
        rel = path.relative_to(root).as_posix()
        st = path.stat()
        item: dict[str, Any] = {
            "path": rel,
            "name": path.name,
            "size": st.st_size,
            "mtime": st.st_mtime,
        }
        if path.suffix.lower() == ".md":
            row = notes_db.get_by_path(rel) or notes_db.ensure_note_for_path(rel)
            if row:
                item["note_id"] = row["note_id"]
                item["title"] = row["title"]
                item["size_bytes"] = row["size_bytes"]
                item["created_at"] = row["created_at"]
                item["updated_at"] = row["updated_at"]
            # Same preview shape as vault_search cards (MCP App / vault_list UI).
            meta = vault_index.get_meta(rel)
            preview = (meta.body_preview if meta else "") or ""
            if preview:
                item["body_preview"] = preview
                item["snippet"] = preview
        files.append(item)
    return {"prefix": cleaned, "ext": ext, "count": len(files), "files": files}


def _forget_missing_note(path: str) -> None:
    """Drop registry / index entries when a note is gone locally and on S3."""
    cleaned = (path or "").replace("\\", "/").lstrip("/")
    if not cleaned:
        return
    try:
        notes_db.on_note_deleted(cleaned)
    except Exception:
        logger.exception("notes_db cleanup failed for missing %s", cleaned)
    try:
        vault_index.remove_note(cleaned)
    except Exception:
        logger.exception("vault_index cleanup failed for missing %s", cleaned)
    try:
        vault_order.notify_deleted(cleaned)
    except Exception:
        logger.debug("vault_order cleanup skipped for %s", cleaned, exc_info=True)


def _require_local_file(path: str) -> Path:
    """Resolve ``path`` to an on-disk file, pulling from S3 on demand.

    If the file is missing both locally and remotely, purge stale note metadata
    and raise 404 so the UI can drop tabs/pins for ghost entries.
    """
    target = vault_sync.ensure_local_file(path)
    if target is not None and target.is_file():
        return target
    purged = False
    if notes_db.is_markdown_path(path) and not vault_sync.remote_file_exists(path):
        _forget_missing_note(path)
        purged = True
    raise HTTPException(
        status_code=404,
        detail={"error": "file_not_found", "path": path, "purged": purged},
    )


def _read_payload(
    *,
    rel: str,
    content: str,
    suffix: str,
    note_row: dict[str, Any] | None,
    size_bytes: int,
    html: bool,
) -> dict[str, Any]:
    is_md = suffix in {".md", ".markdown"}
    meta = vault_index.get_meta(rel) if is_md else None
    backlinks = vault_index.backlinks(rel) if meta else []
    title = (
        (note_row or {}).get("title")
        or (meta.title if meta else Path(rel).stem)
    )
    payload: dict[str, Any] = {
        "path": rel,
        "content": content,
        "word_count": meta.word_count if meta else len(content.split()),
        "char_count": meta.char_count if meta else len(content),
        "backlinks": backlinks,
        "title": title,
        "tags": meta.tags if meta else [],
        "note_id": (note_row or {}).get("note_id"),
        "size_bytes": (note_row or {}).get("size_bytes", size_bytes),
        "created_at": (note_row or {}).get("created_at"),
        "updated_at": (note_row or {}).get("updated_at"),
    }
    # HTML is for MCP/share consumers only — the web editor renders TipTap client-side.
    if html and is_md:
        try:
            payload["content_html"] = _mcp_note_html(content, rel)
        except Exception:
            logger.exception("content_html render failed for %s", rel)
    return payload


def _ddb_read_file(path: str, *, html: bool = False) -> dict:
    """Read a note from DynamoDB in one GetItem when possible (no unused HTML)."""
    cleaned = note_store.nfc(path)
    if not cleaned or ".." in cleaned.split("/"):
        raise HTTPException(status_code=400, detail="Path traversal is not allowed")
    suffix = Path(cleaned).suffix.lower()
    if suffix not in {".md", ".markdown", ".txt", ".json", ".csv", ".yaml", ".yml"}:
        raise HTTPException(status_code=400, detail="Use /raw for binary files")

    item = note_store.get(cleaned, content=True)
    if item is None or item.get("kind") == note_store.KIND_FOLDER:
        found = note_store.find(cleaned)
        if found is not None and found.get("path") != cleaned:
            item = note_store.get(found["path"], content=True)
        else:
            item = None
    if item is None or item.get("kind") == note_store.KIND_FOLDER:
        purged = False
        if notes_db.is_markdown_path(cleaned) and not vault_sync.remote_file_exists(cleaned):
            _forget_missing_note(cleaned)
            purged = True
        raise HTTPException(
            status_code=404,
            detail={"error": "file_not_found", "path": path, "purged": purged},
        )

    rel = str(item.get("path") or cleaned)
    if "content" in item:
        content = str(item["content"])
        # Keep the disposable local cache warm for graph/share without a second GetItem.
        try:
            note_store.warm_cache(
                rel,
                content.encode("utf-8"),
                updated_at=str(item.get("updated_at") or note_store.utc_now()),
                version=int(item.get("version") or 0) or None,
            )
        except Exception:
            logger.debug("warm_cache after ddb read failed for %s", rel, exc_info=True)
    else:
        target = note_store.materialize(rel)
        if target is None or not target.is_file():
            raise HTTPException(
                status_code=404,
                detail={"error": "file_not_found", "path": path, "purged": False},
            )
        content = target.read_text(encoding="utf-8", errors="replace")

    note_row = {
        "note_id": item.get("note_id"),
        "title": item.get("title") or "",
        "size_bytes": int(item.get("size_bytes") or len(content.encode("utf-8"))),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }
    return _read_payload(
        rel=rel,
        content=content,
        suffix=Path(rel).suffix.lower(),
        note_row=note_row,
        size_bytes=int(note_row["size_bytes"] or 0),
        html=html,
    )


@router.get("/read")
def read_file(
    request: Request,
    path: str,
    html: int = Query(0, description="Include content_html (MCP/share); off for web UI"),
) -> dict:
    require_user_id(request)
    want_html = bool(html)
    if note_store.enabled():
        return _ddb_read_file(path, html=want_html)
    try:
        target = _require_local_file(path)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if target.suffix.lower() not in {".md", ".txt", ".json", ".csv", ".yaml", ".yml"}:
        raise HTTPException(status_code=400, detail="Use /raw for binary files")
    content = target.read_text(encoding="utf-8", errors="replace")
    # Prefer the on-disk relative path (handles NFC/NFD spelling differences).
    try:
        rel = target.resolve().relative_to(vault_backend.vault_root().resolve()).as_posix()
    except Exception:
        rel = path
    note_row = (
        notes_db.ensure_note_for_path(rel) if target.suffix.lower() in {".md", ".markdown"} else None
    )
    return _read_payload(
        rel=rel,
        content=content,
        suffix=target.suffix.lower(),
        note_row=note_row,
        size_bytes=int((note_row or {}).get("size_bytes") or target.stat().st_size),
        html=want_html,
    )


@router.post("/render")
def render_markdown(request: Request, body: RenderBody) -> dict:
    """Render markdown with the public-share HTML pipeline (MCP App Preview)."""
    require_user_id(request)
    text = body.content if body.content is not None else ""
    if len(text) > 1_500_000:
        raise HTTPException(status_code=413, detail="content too large")
    try:
        html = _mcp_note_html(text, (body.path or "").replace("\\", "/").strip().lstrip("/"))
    except Exception as exc:
        logger.exception("render_markdown failed")
        raise HTTPException(status_code=500, detail="markdown render failed") from exc
    return {"ok": True, "html": html, "path": (body.path or "").strip() or None}


@router.get("/raw")
def raw_file(request: Request, path: str) -> Response:
    require_user_id(request)
    target = _require_local_file(path)
    return FileResponse(target, media_type=media_type_for_name(target.name))


@router.get("/view")
def view_vault_file(
    request: Request,
    path: str = Query(..., min_length=1, max_length=1024),
    download: int = Query(0),
):
    """Open a vault attachment in a new browser tab (agentic-work Load-files viewer).

    Markdown/text → HTML viewer; images/PDF → inline; other → download.
    """
    require_user_id(request)
    try:
        target = _require_local_file(path)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    name = target.name
    ext = target.suffix.lower()
    force_download = bool(download)
    encoded = quote(path, safe="")
    download_href = f"/api/files/view?path={encoded}&download=1"

    if force_download or (
        ext not in TEXT_VIEWER_EXTENSIONS and ext not in INLINE_BINARY_EXTENSIONS
    ):
        return FileResponse(
            target,
            media_type=media_type_for_name(name),
            filename=name,
            content_disposition_type="attachment",
        )

    if ext in INLINE_BINARY_EXTENSIONS:
        return FileResponse(
            target,
            media_type=media_type_for_name(name),
            filename=name,
            content_disposition_type="inline",
        )

    data = target.read_bytes()
    if len(data) > TEXT_VIEWER_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large for viewer (max {TEXT_VIEWER_MAX_BYTES} bytes)",
        )
    text = data.decode("utf-8", errors="replace")
    as_markdown = ext in {".md", ".markdown"}
    page = viewer_html.build_text_viewer_page(
        name,
        text,
        as_markdown=as_markdown,
        download_href=download_href,
    )
    return HTMLResponse(content=page, media_type="text/html; charset=utf-8")


@router.post("/upload")
async def upload_file(
    request: Request,
    path: str = Form(..., min_length=1, max_length=1024),
    file: UploadFile = File(...),
) -> dict:
    """Save a binary file (e.g. pasted image) into the vault."""
    require_user_id(request)
    path = vault_backend._nfc_rel(path)
    try:
        target = vault_backend.resolve_vault_path(path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    content_type = (file.content_type or "").split(";")[0].strip().lower()
    suffix = Path(path).suffix.lower()
    if content_type and content_type not in ALLOWED_IMAGE_TYPES and not suffix:
        raise HTTPException(status_code=400, detail=f"Unsupported content type: {content_type}")
    if suffix and suffix not in ALLOWED_UPLOAD_SUFFIXES:
        if content_type not in ALLOWED_IMAGE_TYPES and not content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail=f"Unsupported file type: {suffix}")

    data = await file.read()
    size_limit = (
        MAX_VIDEO_UPLOAD_BYTES if suffix in VIDEO_EXTENSIONS else MAX_UPLOAD_BYTES
    )
    if len(data) > size_limit:
        limit_mb = size_limit // (1024 * 1024)
        raise HTTPException(status_code=413, detail=f"File too large (max {limit_mb}MB)")
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    if note_store.enabled():
        saved = note_store.put_bytes(path, data)
        if notes_db.is_markdown_path(path):
            _ddb_reindex(path)
        return {
            "ok": True,
            "path": path,
            "size": len(data),
            "content_type": content_type or media_type_for_name(target.name),
            "note_id": saved.get("note_id"),
            "created": saved.get("created"),
        }

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    retired = vault_backend.retire_nfd_duplicate(path)
    if retired and vault_backend.backend_mode() == "s3":
        vault_sync.enqueue_delete(retired)
        vault_sync.schedule_flush_pending()
    note_row = None
    if notes_db.is_markdown_path(path):
        try:
            text = data.decode("utf-8", errors="replace")
            note_row = notes_db.on_note_written(path, content=text)
            vault_index.update_note(path)
            vault_index.rebuild_index()
        except Exception:
            logger.exception("notes_db upsert after upload failed for %s", path)
    if vault_backend.backend_mode() == "s3":
        vault_backend.sync_to_s3(path)
    return {
        "ok": True,
        "path": path,
        "size": len(data),
        "content_type": content_type or media_type_for_name(target.name),
        "note_id": (note_row or {}).get("note_id"),
        "created": (note_row or {}).get("created"),
    }


_FILENAME_BAD_RE = re.compile(r'[\\/:*?"<>|#]')
_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
# Default new-note names from uniqueNamedPath("Untitled") / "Untitled 2" …
_UNTITLED_STEM_RE = re.compile(r"^Untitled(?: \d+)?$")


def _extract_markdown_h1(content: str) -> str | None:
    """First ATX H1, skipping YAML frontmatter. ``None`` when there is no H1."""
    body = content or ""
    if body.startswith("---\n") or body.startswith("---\r\n"):
        end = body.find("\n---", 3)
        if end >= 0:
            after = body.find("\n", end + 4)
            body = body[after + 1 :] if after >= 0 else ""
    match = _H1_RE.search(body)
    if not match:
        return None
    title = match.group(1).strip()
    return title or None


def _sanitize_note_stem(title: str) -> str:
    cleaned = _FILENAME_BAD_RE.sub("", title or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()[:120]
    return cleaned or "Untitled"


def _replace_or_set_h1(content: str, title: str) -> str:
    """Rewrite the first ATX H1 to ``title`` (frontmatter preserved). Prepend when missing."""
    text = content or ""
    safe = (title or "").strip() or "Untitled"
    prefix = ""
    body = text
    if body.startswith("---\n") or body.startswith("---\r\n"):
        end = body.find("\n---", 3)
        if end >= 0:
            after = body.find("\n", end + 4)
            if after >= 0:
                prefix = body[: after + 1]
                body = body[after + 1 :]
            else:
                return text
    match = _H1_RE.search(body)
    if match:
        # ``_H1_RE``'s trailing ``\s*`` can swallow blank lines after the
        # heading; only rewrite through the end of that line.
        start = match.start()
        nl = body.find("\n", start)
        end = nl if nl >= 0 else len(body)
        return prefix + body[:start] + f"# {safe}" + body[end:]
    rest = body.lstrip("\n")
    if rest:
        return prefix + f"# {safe}\n\n" + rest
    return prefix + f"# {safe}\n\n"


def _alloc_sibling_md(rel_path: str, stem: str) -> str:
    """``parent/{stem}.md``, or ``{stem} N.md`` when a different sibling exists."""
    rel = rel_path.replace("\\", "/").strip("/")
    parent = str(Path(rel).parent).replace("\\", "/")
    if parent == ".":
        parent = ""
    ddb = note_store.enabled()
    root = vault_backend.vault_root()
    folder = root / parent if parent else root
    for n in range(1, 501):
        name = f"{stem}.md" if n == 1 else f"{stem} {n}.md"
        candidate = f"{parent}/{name}" if parent else name
        if candidate == rel:
            return candidate
        taken = note_store.get(candidate) is not None if ddb else (folder / name).exists()
        if not taken:
            return candidate
    raise HTTPException(status_code=409, detail="Could not allocate a unique note name")


def _is_untitled_stem(stem: str) -> bool:
    """True for default new-note names that may still follow an H1 retitle."""
    return bool(_UNTITLED_STEM_RE.match(stem or ""))


def _retitle_rel(src_rel: str, content: str) -> str:
    """Return the vault path after an H1 retitle.

    Only Untitled / Untitled N stems are renamed. Once the user (or tree rename)
    has set a real filename, editing the H1 keeps that path.
    """
    if not notes_db.is_markdown_path(src_rel):
        return src_rel
    current = Path(src_rel).stem
    if not _is_untitled_stem(current):
        return src_rel
    title = _extract_markdown_h1(content)
    if not title:
        return src_rel
    stem = _sanitize_note_stem(title)
    if stem == current:
        return src_rel
    return _alloc_sibling_md(src_rel, stem)


@router.put("/write")
def write_file(request: Request, body: WriteBody) -> dict:
    require_user_id(request)
    if note_store.enabled():
        return _ddb_write(body)
    src_rel = vault_backend._nfc_rel(body.path)
    try:
        src_target = vault_backend.resolve_vault_path(src_rel)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    text = body.content
    final_rel = _retitle_rel(src_rel, text) if body.sync_filename else src_rel
    try:
        target = vault_backend.resolve_vault_path(final_rel)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    for rel in {src_rel, final_rel}:
        retired = vault_backend.retire_nfd_duplicate(rel)
        if retired and vault_backend.backend_mode() == "s3":
            vault_sync.enqueue_delete(retired)
            vault_sync.schedule_flush_pending()

    renamed_from: str | None = None
    if final_rel != src_rel:
        renamed_from = src_rel
        if vault_backend.backend_mode() == "s3":
            # Queue the old-key delete before unlinking so a pull cannot
            # download Untitled.md back while this request is still running.
            vault_sync.enqueue_delete(src_rel)
            vault_sync.enqueue_put(final_rel)
            vault_sync.schedule_flush_pending(reconcile_casing=True)
        if src_target.is_file() and src_target.resolve() != target.resolve():
            src_target.unlink()
            logger.info("note retitle removed %s -> %s", src_rel, final_rel)

    note_row = None
    if notes_db.is_markdown_path(final_rel):
        if renamed_from:
            # Move the existing row (Untitled's note_id) onto the new path and
            # drop any duplicate row that was registered under the new name.
            note_row = notes_db.rebind_note_path(src_rel, final_rel, content=text)
            vault_index.remove_note(src_rel)
        else:
            note_row = notes_db.on_note_written(final_rel, content=text)
        vault_index.update_note(final_rel)
        vault_index.rebuild_index()
    if renamed_from:
        vault_share.rewrite_share_paths(src_rel, final_rel)
        vault_order.notify_renamed(src_rel, final_rel)
    elif vault_backend.backend_mode() == "s3":
        vault_backend.sync_to_s3(final_rel)
    meta = vault_index.get_meta(final_rel)
    return {
        "ok": True,
        "path": final_rel,
        "renamed_from": renamed_from,
        "word_count": meta.word_count if meta else None,
        "char_count": meta.char_count if meta else None,
        "note_id": (note_row or {}).get("note_id"),
        "title": (note_row or {}).get("title"),
        "size_bytes": (note_row or {}).get("size_bytes"),
        "created_at": (note_row or {}).get("created_at"),
        "updated_at": (note_row or {}).get("updated_at"),
        "created": (note_row or {}).get("created"),
    }


@router.post("/append")
def append_file(request: Request, body: AppendBody) -> dict:
    """Append text to a note (create parent dirs / file when ``create=true``)."""
    require_user_id(request)
    if note_store.enabled():
        return _ddb_append(body)
    try:
        target = vault_backend.resolve_vault_path(body.path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if target.exists() and not target.is_file():
        raise HTTPException(status_code=400, detail="Path is a directory")
    if not target.exists():
        if not body.create:
            raise HTTPException(status_code=404, detail="File not found")
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = ""
    else:
        existing = target.read_text(encoding="utf-8", errors="replace")
    chunk = body.content or ""
    if existing and chunk and not existing.endswith(("\n", "\r")):
        sep = body.separator if body.separator is not None else "\n"
        content = existing + sep + chunk
    elif existing and chunk:
        content = existing + chunk
    else:
        content = existing + chunk
    target.write_text(content, encoding="utf-8")
    note_row = None
    if notes_db.is_markdown_path(body.path):
        note_row = notes_db.on_note_written(body.path, content=content)
        vault_index.update_note(body.path)
        vault_index.rebuild_index()
    if vault_backend.backend_mode() == "s3":
        vault_backend.sync_to_s3(body.path)
    meta = vault_index.get_meta(body.path)
    return {
        "ok": True,
        "path": body.path,
        "appended": len(chunk),
        "word_count": meta.word_count if meta else None,
        "char_count": meta.char_count if meta else None,
        "note_id": (note_row or {}).get("note_id"),
        "title": (note_row or {}).get("title"),
        "size_bytes": (note_row or {}).get("size_bytes"),
        "created_at": (note_row or {}).get("created_at"),
        "updated_at": (note_row or {}).get("updated_at"),
        "created": (note_row or {}).get("created"),
    }


@router.post("/mkdir")
def mkdir(request: Request, body: MkdirBody) -> dict:
    require_user_id(request)
    if note_store.enabled():
        rel = note_store.nfc(body.path)
        if not rel or ".." in rel.split("/"):
            raise HTTPException(status_code=400, detail="Path traversal is not allowed")
        try:
            note_store.mkdir(rel)
        except FileExistsError as e:
            raise HTTPException(status_code=400, detail="Path is a file") from e
        return {"ok": True, "path": body.path}
    try:
        target = vault_backend.resolve_vault_path(body.path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    target.mkdir(parents=True, exist_ok=True)
    # S3 has no empty directories: a marker file keeps the folder in the tree
    # after sync_from_s3 rebuilds from object keys.
    keep = target / FOLDER_KEEP_NAME
    if not keep.exists():
        keep.write_text("", encoding="utf-8")
    keep_rel = f"{body.path.rstrip('/')}/{FOLDER_KEEP_NAME}"
    if vault_backend.backend_mode() == "s3":
        vault_backend.sync_to_s3(keep_rel)
    return {"ok": True, "path": body.path}


@router.post("/rename")
def rename(request: Request, body: RenameBody) -> dict:
    require_user_id(request)
    if note_store.enabled():
        return _ddb_rename(body)
    # Sidebar paths can be NFD (decomposed Hangul from S3 keys). Storage and
    # the S3 put must use NFC, or Linux writes the file into a twin folder
    # while the upload still looks at the folder the user dropped on.
    from_rel = vault_backend._nfc_rel(body.from_path)
    to_rel = vault_backend._nfc_rel(body.to_path)
    try:
        src = vault_backend.resolve_vault_path(from_rel)
        dst = vault_backend.resolve_vault_path(to_rel)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not src.exists():
        found = vault_backend.find_vault_file(from_rel)
        if found is None:
            raise HTTPException(status_code=404, detail="Source not found")
        src = found
    if src.resolve() == dst.resolve():
        return {"ok": True, "from": from_rel, "to": to_rel, "companion_images": None}
    was_file = src.is_file()
    from_name = Path(from_rel).name
    to_name = Path(to_rel).name
    if vault_backend.backend_mode() == "s3":
        # Queue deletes for old keys before the local move.
        vault_sync.enqueue_delete_tree(from_rel)
    dst.parent.mkdir(parents=True, exist_ok=True)
    # Case-only rename (agent → Agent) needs a temp hop on case-insensitive disks.
    if from_name.lower() == to_name.lower() and from_name != to_name:
        import uuid as _uuid

        tmp = src.with_name(f".__rename_{_uuid.uuid4().hex[:10]}")
        src.rename(tmp)
        tmp.rename(src.parent / to_name)
    else:
        shutil.move(str(src), str(dst))
    # Filename → H1: tree/API rename keeps the note title in sync with the stem
    # (mirrors H1 → filename on save). Do this before notes_db/index so title
    # is taken from the updated heading, not a stale Untitled H1.
    if was_file and notes_db.is_markdown_path(to_rel) and Path(from_rel).stem != Path(to_rel).stem:
        try:
            text = dst.read_text(encoding="utf-8", errors="replace")
            synced = _replace_or_set_h1(text, Path(to_rel).stem)
            if synced != text:
                dst.write_text(synced, encoding="utf-8")
        except Exception:
            logger.exception("H1 sync after rename failed for %s", to_rel)
    notes_db.on_note_renamed(from_rel, to_rel)
    if notes_db.is_markdown_path(from_rel):
        vault_index.remove_note(from_rel)
    if notes_db.is_markdown_path(to_rel):
        # Index only — DB path already updated by on_note_renamed (preserves note_id).
        vault_index.update_note(to_rel)
    vault_index.rebuild_index()
    # Keep public shares pointing at the new path and republish content to S3.
    vault_share.rewrite_share_paths(from_rel, to_rel)
    if vault_backend.backend_mode() == "s3":
        # enqueue_delete_tree (above) + enqueue_put_tree flush together so the
        # file panel never loses the path between remote delete and re-upload.
        vault_sync.enqueue_put_tree(to_rel)
        vault_sync.schedule_flush_pending(reconcile_casing=True)
    else:
        try:
            if was_file:
                vault_share.publish_vault_file_to_s3(to_rel)
                vault_share.delete_vault_from_s3(from_rel)
            else:
                vault_share.delete_vault_tree_from_s3(from_rel)
                root = vault_backend.vault_root()
                target = vault_backend.resolve_vault_path(to_rel)
                if target.is_dir():
                    for p in target.rglob("*.md"):
                        rel = p.resolve().relative_to(root.resolve()).as_posix()
                        vault_share.publish_vault_file_to_s3(rel)
        except Exception:
            pass
    vault_order.notify_renamed(from_rel, to_rel)
    companion_images: str | None = None
    if (
        was_file
        and notes_db.is_markdown_path(to_rel)
        and vault_companion_assets.parents_differ(from_rel, to_rel)
    ):
        schedule = (
            vault_companion_assets.schedule_copy_companion_images
            if body.copy_companions
            else vault_companion_assets.schedule_move_companion_images
        )
        if schedule(from_rel, to_rel):
            companion_images = "queued"
    return {
        "ok": True,
        "from": from_rel,
        "to": to_rel,
        "companion_images": companion_images,
    }


@router.post("/copy")
def copy_path(request: Request, body: CopyBody) -> dict:
    """Copy a file to ``to_path``, overwriting an existing file. Source stays."""
    require_user_id(request)
    if note_store.enabled():
        return _ddb_copy(body)
    try:
        src = vault_backend.resolve_vault_path(body.from_path)
        dst = vault_backend.resolve_vault_path(body.to_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not src.exists():
        raise HTTPException(status_code=404, detail="Source not found")
    if not src.is_file():
        raise HTTPException(status_code=400, detail="Only files can be copied")
    if dst.exists() and not dst.is_file():
        raise HTTPException(status_code=400, detail="Destination is a directory")
    root = vault_backend.vault_root().resolve()
    rel_from = src.resolve().relative_to(root).as_posix()
    if src.resolve() == dst.resolve():
        return {
            "ok": True,
            "from": rel_from,
            "to": rel_from,
            "overwritten": False,
            "companion_images": None,
        }
    overwritten = dst.is_file()
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    rel_to = dst.resolve().relative_to(root).as_posix()
    if notes_db.is_markdown_path(rel_to):
        notes_db.on_note_written(rel_to)
        vault_index.update_note(rel_to)
        vault_index.rebuild_index()
    vault_order.notify_copied(rel_to)
    if vault_backend.backend_mode() == "s3":
        vault_sync.enqueue_put(rel_to)
        vault_sync.schedule_flush_pending()
    else:
        try:
            vault_share.publish_vault_file_to_s3(rel_to)
        except Exception:
            pass
    companion_images: str | None = None
    if (
        notes_db.is_markdown_path(rel_to)
        and vault_companion_assets.parents_differ(rel_from, rel_to)
    ):
        if vault_companion_assets.schedule_copy_companion_images(rel_from, rel_to):
            companion_images = "queued"
    return {
        "ok": True,
        "from": rel_from,
        "to": rel_to,
        "overwritten": overwritten,
        "companion_images": companion_images,
    }


@router.post("/delete")
def delete_path(request: Request, body: DeleteBody) -> dict:
    require_user_id(request)
    if note_store.enabled():
        return _ddb_delete(body)
    try:
        target = vault_backend.resolve_vault_path(body.path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not target.exists():
        raise HTTPException(status_code=404, detail="Not found")
    # Revoke public shares before removing the note so Shared List / CloudFront
    # cannot keep a dangling token → "Shared note no longer exists".
    vault_share.remove_shares_for_path(body.path)
    if vault_backend.backend_mode() == "s3":
        vault_sync.enqueue_delete_tree(body.path)
    else:
        # Mount/local with bucket: still drop S3 objects so share fallback cannot resurrect.
        try:
            vault_share.delete_vault_tree_from_s3(body.path)
        except Exception:
            pass
    if target.is_dir():
        shutil.rmtree(target)
        notes_db.on_note_deleted(body.path)
    else:
        target.unlink()
        if notes_db.is_markdown_path(body.path):
            vault_index.remove_note(body.path)
            notes_db.on_note_deleted(body.path)
    vault_index.rebuild_index()
    vault_order.notify_deleted(body.path)
    if vault_backend.backend_mode() == "s3":
        vault_sync.schedule_flush_pending()
    return {"ok": True, "path": body.path}


@router.put("/order")
def reorder_folder(request: Request, body: ReorderBody) -> dict:
    """Persist custom sibling order for a folder (same-folder drag reorder)."""
    require_user_id(request)
    folder = (body.folder or "").replace("\\", "/").strip("/")
    if folder:
        try:
            target = vault_backend.resolve_vault_path(folder)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if not target.is_dir():
            raise HTTPException(status_code=404, detail="Folder not found")
    names = vault_order.set_order(folder, body.names)
    return {"ok": True, "folder": folder, "names": names}


def _unique_copy_path(src: Path) -> Path:
    """Return sibling path like 'Name 2.md' / 'Name 3' (folders)."""
    parent = src.parent
    if src.is_dir():
        base = src.name
        suffix = ""
    else:
        base = src.stem
        suffix = src.suffix
    n = 2
    while True:
        candidate = parent / f"{base} {n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


@router.post("/duplicate")
def duplicate_path(request: Request, body: DuplicateBody) -> dict:
    require_user_id(request)
    if note_store.enabled():
        return _ddb_duplicate(body)
    try:
        src = vault_backend.resolve_vault_path(body.path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not src.exists():
        raise HTTPException(status_code=404, detail="Not found")
    dst = _unique_copy_path(src)
    root = vault_backend.vault_root()
    if root != dst and root not in dst.parents:
        raise HTTPException(status_code=400, detail="Invalid destination")
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
    vault_index.rebuild_index()
    rel = dst.relative_to(root).as_posix()
    if notes_db.is_markdown_path(rel):
        # Duplicate gets a fresh note_id (new registration).
        notes_db.on_note_written(rel)
    elif dst.is_dir():
        notes_db.sync_from_filesystem()
    if vault_backend.backend_mode() == "s3":
        vault_sync.enqueue_put_tree(rel)
        vault_sync.schedule_flush_pending()
    return {"ok": True, "from": body.path, "to": rel}


@router.post("/compress")
def compress_folder(request: Request, body: CompressBody) -> dict:
    """Start a background zip of a vault folder and return job status."""
    user_id = require_user_id(request)
    scope = (body.scope or "folder").strip().lower()
    try:
        if scope == "vault":
            return vault_compress.start_compress(user_id, entire_vault=True)
        if scope != "folder":
            raise HTTPException(status_code=400, detail="Invalid compress scope")
        return vault_compress.start_compress(user_id, body.path)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=413, detail=str(e)) from e


@router.get("/compress/items")
def compress_items(request: Request) -> dict:
    """List compress jobs recorded in {user}/backup/compress.json."""
    user_id = require_user_id(request)
    return vault_compress.list_items(user_id)


@router.post("/compress/delete")
def compress_delete(request: Request, body: CompressItemBody) -> dict:
    user_id = require_user_id(request)
    try:
        return vault_compress.delete_item(user_id, body.id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@router.post("/compress/refresh")
def compress_refresh(request: Request, body: CompressItemBody) -> dict:
    """Issue a new one-hour download URL for an existing archive."""
    user_id = require_user_id(request)
    try:
        return vault_compress.refresh_download(user_id, body.id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


@router.get("/compress")
def compress_status(request: Request) -> dict:
    """Poll background compress progress (file, count, percent, download URL)."""
    user_id = require_user_id(request)
    return vault_compress.get_status(user_id)


@router.post("/clearing")
def clearing_start(request: Request) -> dict:
    """Start a background scan and return immediately so the UI can poll progress."""
    require_user_id(request)
    return vault_clearing.start_clearing_job()


@router.get("/clearing")
def clearing_status(request: Request) -> dict:
    """Poll clearing progress. ``scan`` is present when status is ready."""
    require_user_id(request)
    return vault_clearing.get_clearing_status()


@router.post("/clearing/keep")
def clearing_keep(request: Request, body: ClearingPathsBody) -> dict:
    """Remember media paths so later scans skip them."""
    require_user_id(request)
    if not body.paths:
        raise HTTPException(status_code=400, detail="paths is required")
    try:
        kept = vault_clearing.remember_kept(body.paths)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "kept": kept}


@router.post("/clearing/unkeep")
def clearing_unkeep(request: Request, body: ClearingPathsBody) -> dict:
    """Put kept media back into the next scan."""
    require_user_id(request)
    if not body.all and not body.paths:
        raise HTTPException(status_code=400, detail="paths is required")
    try:
        kept = vault_clearing.forget_kept(body.paths, all_paths=body.all)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, "kept": kept}


@router.post("/clearing/delete")
def clearing_delete(request: Request, body: ClearingPathsBody) -> dict:
    """Start background deletion so the UI can poll progress."""
    require_user_id(request)
    if not body.paths:
        raise HTTPException(status_code=400, detail="paths is required")
    return vault_clearing.start_delete_job(body.paths)


@router.get("/clearing/delete")
def clearing_delete_status(request: Request) -> dict:
    """Poll clearing delete progress (current file, count, percent)."""
    require_user_id(request)
    return vault_clearing.get_delete_status()


@router.get("/sync")
def sync_status(request: Request) -> dict:
    """Pending outbound ops + background sync job status (for SyncProgressModal)."""
    require_user_id(request)
    return vault_sync.get_sync_status()


@router.post("/sync")
def sync_from_remote(request: Request) -> dict:
    """Start background sync: flush pending local→S3, then incremental pull."""
    require_user_id(request)
    if vault_backend.backend_mode() != "s3" and not note_store.enabled():
        return {
            "ok": False,
            "status": "error",
            "busy": False,
            "mode": vault_backend.backend_mode(),
            "message": (
                "로컬 모드에서는 S3 동기화를 사용할 수 없습니다. "
                "VAULT_S3_ENABLE=1 로 실행한 뒤 다시 시도하세요."
            ),
        }
    return vault_sync.start_sync_job(force_download=False)


@router.get("/settings/{name}")
def get_settings(request: Request, name: str) -> Any:
    require_user_id(request)
    safe = name.replace("..", "").replace("/", "")
    if not safe.endswith(".json"):
        safe += ".json"
    path = vault_backend.settings_dir() / safe
    if not path.is_file():
        return {}
    import json

    return json.loads(path.read_text(encoding="utf-8"))


@router.put("/settings/{name}")
def put_settings(request: Request, name: str, body: dict) -> dict:
    require_user_id(request)
    import json

    safe = name.replace("..", "").replace("/", "")
    if not safe.endswith(".json"):
        safe += ".json"
    # workspace is ephemeral; app.json is allowed
    path = vault_backend.settings_dir() / safe
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "path": f".vault/{safe}"}
