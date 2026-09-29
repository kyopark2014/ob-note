"""Public share viewer — no session cookie required."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

from application.api.routes_auth import require_user_id
from application import vault_backend, vault_share, viewer_html

# Authenticated create endpoint lives under /api
api_router = APIRouter(prefix="/api/files", tags=["share"])

# Public pages under /s/… (no auth)
public_router = APIRouter(prefix="/s", tags=["share-public"])

_NO_STORE_HEADERS = {
    "Cache-Control": "private, no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
    "X-Robots-Tag": "noindex",
}


class ShareBody(BaseModel):
    path: str = Field(..., min_length=1, max_length=1024)


def _share_asset_rel(note_path: str, rel: str) -> str:
    cleaned = unquote((rel or "").replace("\\", "/")).strip()
    if (
        not cleaned
        or cleaned.startswith("/")
        or cleaned.startswith("http:")
        or cleaned.startswith("https:")
        or cleaned.startswith("data:")
        or ".." in cleaned.split("/")
    ):
        raise HTTPException(status_code=400, detail="Invalid asset path")
    note_dir = str(Path(note_path).parent).replace("\\", "/")
    if note_dir in {".", ""}:
        joined = cleaned
    else:
        joined = f"{note_dir}/{cleaned}"
    joined = joined.replace("\\", "/").lstrip("/")
    try:
        target = vault_backend.resolve_vault_path(joined)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if target.is_file():
        return joined
    # Obsidian-style basename fallback on local vault
    found = vault_backend.find_vault_file(cleaned)
    if found is not None:
        root = vault_backend.vault_root()
        try:
            return found.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return joined
    # Fall through — S3 lookup may still succeed for the joined path
    return joined


def _html_response(page: str) -> HTMLResponse:
    return HTMLResponse(
        content=page,
        media_type="text/html; charset=utf-8",
        headers=_NO_STORE_HEADERS,
    )


@api_router.post("/share")
def create_share(request: Request, body: ShareBody) -> dict:
    """Create (or reuse) a public share link for a markdown note or folder."""
    require_user_id(request)
    try:
        entry = vault_share.create_or_get_share(body.path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    token = entry["token"]
    url_path = vault_share.public_share_path(token)
    share_type = vault_share.share_entry_type(entry)
    return {
        "ok": True,
        "token": token,
        "path": entry["path"],
        "title": entry.get("title")
        or (
            Path(entry["path"]).name
            if share_type == "folder"
            else Path(entry["path"]).stem
        ),
        "type": share_type,
        "created_at": entry.get("created_at"),
        "url_path": url_path,
        "url": vault_share.public_share_url(token),
    }


@api_router.get("/shares")
def list_shares(request: Request) -> dict:
    require_user_id(request)
    items = vault_share.list_shares()
    return {"ok": True, "count": len(items), "shares": items}


class DeleteShareBody(BaseModel):
    token: str = Field(..., min_length=16, max_length=64)


@api_router.post("/share/delete")
def delete_share(request: Request, body: DeleteShareBody) -> dict:
    require_user_id(request)
    ok = vault_share.delete_share(body.token)
    if not ok:
        raise HTTPException(status_code=404, detail="Share not found")
    return {"ok": True, "token": body.token}


class SharePermissionBody(BaseModel):
    permission: str = Field(..., min_length=1, max_length=32)


@api_router.get("/share/permission")
def get_share_permission(request: Request) -> dict:
    """Folder-share wiki scope: current | one_hop | folder | vault."""
    require_user_id(request)
    permission = vault_share.get_share_permission()
    return {
        "ok": True,
        "permission": permission,
        "options": list(vault_share.SHARE_PERMISSIONS),
        "default": vault_share.DEFAULT_SHARE_PERMISSION,
    }


@api_router.put("/share/permission")
def put_share_permission(request: Request, body: SharePermissionBody) -> dict:
    require_user_id(request)
    permission = vault_share.set_share_permission(body.permission)
    return {"ok": True, "permission": permission}


@public_router.get("/{token}")
def view_share(token: str) -> HTMLResponse:
    entry = vault_share.get_share(token, refresh=True)
    if not entry:
        raise HTTPException(status_code=404, detail="Share not found")
    owner = entry.get("user_id")
    if not owner:
        raise HTTPException(status_code=404, detail="Share not found")
    share_path = entry.get("path") or ""
    share_type = vault_share.share_entry_type(entry)

    if share_type == "folder":
        with vault_backend.user_scope(owner):
            if not vault_share.vault_folder_exists(share_path):
                try:
                    vault_share.delete_share(token)
                except Exception:
                    pass
                raise HTTPException(status_code=404, detail="Shared folder no longer exists")
            names = vault_share.list_folder_share_notes(share_path)
        title = entry.get("title") or Path(share_path).name
        notes = [
            {
                "name": Path(n).stem,
                "url": vault_share.public_folder_note_path(token, n),
            }
            for n in names
        ]
        page = viewer_html.build_folder_share_page(
            title,
            notes,
            topbar_right_html='<span style="color:#8b949e;font-size:12px">Public share</span>',
        )
        return _html_response(page)

    with vault_backend.user_scope(owner):
        text = vault_share.read_vault_text(share_path)
        if text is None:
            try:
                vault_share.revoke_share_if_missing(token, share_path)
            except Exception:
                pass
            raise HTTPException(status_code=404, detail="Shared note no longer exists")
        allowed = vault_share.note_share_allowed_paths(share_path, text)
        text = vault_share.prepare_note_share_markdown(
            text,
            token,
            note_path=share_path,
            root_path=share_path,
            allowed_paths=allowed,
        )
    title = entry.get("title") or Path(share_path).stem
    page = viewer_html.build_markdown_viewer_page(
        title,
        text,
        topbar_right_html='<span style="color:#8b949e;font-size:12px">Public share</span>',
    )
    return _html_response(page)


@public_router.get("/{token}/w/{note_path:path}")
def view_share_wiki_note(token: str, note_path: str) -> HTMLResponse:
    """Render a one-hop wiki-linked note under a note or folder share token."""
    entry = vault_share.get_share(token, refresh=True)
    if not entry:
        raise HTTPException(status_code=404, detail="Share not found")
    owner = entry.get("user_id")
    if not owner:
        raise HTTPException(status_code=404, detail="Share not found")
    share_path = (entry.get("path") or "").replace("\\", "/").lstrip("/")
    share_type = vault_share.share_entry_type(entry)
    decoded = unquote(note_path or "").replace("\\", "/").lstrip("/")
    if not decoded or ".." in decoded.split("/"):
        raise HTTPException(status_code=404, detail="Shared note not found")

    with vault_backend.user_scope(owner):
        if share_type == "folder":
            siblings = vault_share.list_folder_share_notes(share_path)
            allowed = vault_share.folder_share_allowed_paths(share_path, siblings)
            if not vault_share.is_share_path_allowed(decoded, allowed):
                raise HTTPException(status_code=404, detail="Shared note not found")
            text = vault_share.read_vault_text(decoded)
            if text is None:
                raise HTTPException(status_code=404, detail="Shared note not found")
            text = vault_share.prepare_folder_share_note_markdown(
                text,
                token,
                note_path=decoded,
                folder_path=share_path,
                sibling_names=siblings,
                allowed_paths=allowed,
            )
            back_title = entry.get("title") or Path(share_path).name
        else:
            root_text = vault_share.read_vault_text(share_path)
            if root_text is None:
                raise HTTPException(status_code=404, detail="Shared note no longer exists")
            allowed = vault_share.note_share_allowed_paths(share_path, root_text)
            if not vault_share.is_path_in_note_share_allowed(decoded, allowed):
                raise HTTPException(status_code=404, detail="Shared note not found")
            text = vault_share.read_vault_text(decoded)
            if text is None:
                raise HTTPException(status_code=404, detail="Shared note not found")
            text = vault_share.prepare_note_share_markdown(
                text,
                token,
                note_path=decoded,
                root_path=share_path,
                allowed_paths=allowed,
            )
            back_title = entry.get("title") or Path(share_path).stem

    note_title = Path(decoded).stem
    back = vault_share.public_share_path(token)
    topbar = (
        f'<a class="action" href="{html.escape(back, quote=True)}">'
        f"← {html.escape(back_title)}</a>"
        '<span style="color:#8b949e;font-size:12px">Public share</span>'
    )
    page = viewer_html.build_markdown_viewer_page(
        note_title,
        text,
        topbar_right_html=topbar,
    )
    return _html_response(page)


@public_router.get("/{token}/n/{name}")
def view_folder_share_note(token: str, name: str) -> HTMLResponse:
    """Render a direct .md under a folder share (siblings + one-hop wiki links)."""
    entry = vault_share.get_share(token, refresh=True)
    if not entry:
        raise HTTPException(status_code=404, detail="Share not found")
    if vault_share.share_entry_type(entry) != "folder":
        raise HTTPException(status_code=404, detail="Not a folder share")
    owner = entry.get("user_id")
    if not owner:
        raise HTTPException(status_code=404, detail="Share not found")
    folder_path = entry.get("path") or ""
    decoded = unquote(name or "")
    with vault_backend.user_scope(owner):
        note_path = vault_share.resolve_folder_note(folder_path, decoded)
        if not note_path:
            raise HTTPException(status_code=404, detail="Shared note not found")
        text = vault_share.read_vault_text(note_path)
        if text is None:
            raise HTTPException(status_code=404, detail="Shared note not found")
        siblings = vault_share.list_folder_share_notes(folder_path)
        allowed = vault_share.folder_share_allowed_paths(folder_path, siblings)
        text = vault_share.prepare_folder_share_note_markdown(
            text,
            token,
            note_path=note_path,
            folder_path=folder_path,
            sibling_names=siblings,
            allowed_paths=allowed,
        )
    folder_title = entry.get("title") or Path(folder_path).name
    note_title = Path(note_path).stem
    back = vault_share.public_share_path(token)
    topbar = (
        f'<a class="action" href="{html.escape(back, quote=True)}">'
        f"← {html.escape(folder_title)}</a>"
        '<span style="color:#8b949e;font-size:12px">Public share</span>'
    )
    page = viewer_html.build_markdown_viewer_page(
        note_title,
        text,
        topbar_right_html=topbar,
    )
    return _html_response(page)


@public_router.get("/{token}/raw")
def share_raw_asset(
    token: str,
    path: str,
    note: Optional[str] = Query(None),
    doc: Optional[str] = Query(None),
):
    entry = vault_share.get_share(token, refresh=True)
    if not entry:
        raise HTTPException(status_code=404, detail="Share not found")
    owner = entry.get("user_id")
    if not owner:
        raise HTTPException(status_code=404, detail="Share not found")
    share_path = entry.get("path") or ""
    share_type = vault_share.share_entry_type(entry)
    headers = {
        "Cache-Control": "private, no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
    }
    with vault_backend.user_scope(owner):
        if share_type == "folder":
            siblings = vault_share.list_folder_share_notes(share_path)
            allowed = vault_share.folder_share_allowed_paths(share_path, siblings)
            if doc:
                doc_path = unquote(doc).replace("\\", "/").lstrip("/")
                if not vault_share.is_share_path_allowed(doc_path, allowed):
                    raise HTTPException(status_code=404, detail="Asset not found")
                note_path = doc_path
            elif note:
                note_path = vault_share.resolve_folder_note(share_path, unquote(note))
                if not note_path:
                    raise HTTPException(status_code=404, detail="Shared note not found")
            else:
                raise HTTPException(status_code=400, detail="note or doc query required")
        else:
            if doc:
                doc_path = unquote(doc).replace("\\", "/").lstrip("/")
                root_text = vault_share.read_vault_text(share_path) or ""
                allowed = vault_share.note_share_allowed_paths(share_path, root_text)
                if not vault_share.is_path_in_note_share_allowed(doc_path, allowed):
                    raise HTTPException(status_code=404, detail="Asset not found")
                note_path = doc_path
            else:
                note_path = share_path
        rel = _share_asset_rel(note_path, path)
        if share_type == "folder":
            # Assets must stay under the note's directory; for out-of-folder
            # one-hop notes that is the linked note's folder, not the share root.
            pass
        try:
            target = vault_backend.resolve_vault_path(rel)
            if target.is_file():
                return FileResponse(
                    target,
                    media_type=viewer_html.media_type_for_name(target.name),
                    headers=headers,
                )
        except ValueError:
            pass
        data = vault_share.read_vault_bytes(rel)
        if data is None:
            raise HTTPException(status_code=404, detail="Asset not found")
    return Response(
        content=data,
        media_type=viewer_html.media_type_for_name(rel),
        headers=headers,
    )
