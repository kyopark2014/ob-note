"""MCP Apps (interactive UI) for the use-vault MCP server.

Six read tools carry ``_meta.ui.resourceUri`` -> ``ui://vault/viewer.html``. Each returns
the same JSON text as before (what the model reads) plus a ``structuredContent`` object the
UI draws from: ``{"ok": true, "kind": "search|note|list|tree|graph|backlinks", ...payload}``
with a deep link ``url`` on every note.

The viewer supports Preview / Edit / Save (calls ``vault_write`` via the host proxy).
HTML is built from ``ui/`` (``python3 MCP/use-vault/ui/build.py``).
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any
from urllib.parse import quote_plus

import vault_client as vault
from mcp.server.apps import Apps, ResourceCsp
from mcp.types import CallToolResult, TextContent

logging.basicConfig(
    level=logging.INFO,
    format="%(filename)s:%(lineno)d | %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("use-vault-apps")

VIEW_URI = "ui://vault/viewer.html"
_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui", "dist", "vault-viewer.html")

apps = Apps()


def _load_html() -> str:
    if not os.path.isfile(_HTML_PATH):
        raise FileNotFoundError(
            f"{_HTML_PATH} is missing; run `python3 MCP/use-vault/ui/build.py` and commit the output"
        )
    with open(_HTML_PATH, "r", encoding="utf-8") as f:
        return f.read()


def note_url(path: str) -> str:
    """Login-gated deep link to a note in ob-note (same shape the skills return)."""
    cleaned = (path or "").replace("\\", "/").lstrip("/").strip()
    return f"{vault.vault_base_url().rstrip('/')}/?note={quote_plus(cleaned)}"


def with_urls(value: Any) -> Any:
    """Add deep-link ``url`` / ``from_url`` fields (same shape as my-vaults)."""
    if isinstance(value, list):
        return [with_urls(v) for v in value]
    if not isinstance(value, dict):
        return value

    out = {k: with_urls(v) for k, v in value.items()}
    if out.get("type") == "folder":
        return out

    path = out.get("path")
    if isinstance(path, str) and path.strip() and "url" not in out:
        out["url"] = note_url(path)

    to_val = out.get("to") or out.get("to_path")
    if isinstance(to_val, str) and to_val.strip():
        out["url"] = note_url(to_val)

    from_val = out.get("from") or out.get("from_path")
    if isinstance(from_val, str) and from_val.strip() and "from_url" not in out:
        out["from_url"] = note_url(from_val)

    return out


def _require_actor(actor_id: str) -> str:
    value = (actor_id or "").strip()
    if not value:
        raise ValueError("actor_id is required (account login id / email)")
    return value


def _result(kind: str, payload: Any, text: str | None = None) -> CallToolResult:
    body = with_urls(payload) if isinstance(payload, dict) else {"data": payload}
    # Text + structuredContent both include deep-link ``url`` (my-vaults parity).
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=text if text is not None else vault.to_json(body),
            )
        ],
        structured_content={"ok": True, "kind": kind, **body},
    )


def _error(exc: Exception) -> CallToolResult:
    logger.error("%s", exc)
    return CallToolResult(
        content=[TextContent(type="text", text=vault.to_json({"ok": False, "error": str(exc)}))],
        structured_content={"ok": False, "error": str(exc)},
    )


@apps.tool(resource_uri=VIEW_URI, meta={"ui/resourceUri": VIEW_URI})
def vault_tree(actor_id: str) -> CallToolResult:
    """
    Return the full file tree for the caller's vault (GET /api/files/tree).

    actor_id: account login id / email from the system prompt.
    return: JSON tree; each note includes deep-link url — summarize as a linked TOC
    """
    try:
        uid = _require_actor(actor_id)
        logger.info("vault_tree actor_id=%s", uid)
        return _result("tree", vault.api_request("GET", "/api/files/tree", user_id=uid))
    except Exception as e:
        return _error(e)


@apps.tool(resource_uri=VIEW_URI, meta={"ui/resourceUri": VIEW_URI})
def vault_list(actor_id: str, prefix: str = "", ext: str = "md") -> CallToolResult:
    """
    List files under a vault folder prefix (GET /api/files/list).

    actor_id: account login id / email from the system prompt.
    prefix: folder prefix, e.g. Meeting or notes (empty = vault root).
    ext: extension filter — md | txt | * (default md).
    return: JSON file list with deep-link url per note — reply with a linked TOC
    """
    try:
        uid = _require_actor(actor_id)
        if prefix:
            prefix = vault.normalize_vault_path(prefix)
        logger.info("vault_list actor_id=%s prefix=%s ext=%s", uid, prefix, ext)
        return _result(
            "list",
            vault.api_request(
                "GET", "/api/files/list", user_id=uid, query={"prefix": prefix or "", "ext": ext}
            ),
        )
    except Exception as e:
        return _error(e)


@apps.tool(resource_uri=VIEW_URI, meta={"ui/resourceUri": VIEW_URI})
def vault_read(actor_id: str, path: str, raw: bool = False) -> CallToolResult:
    """
    Read a vault note (GET /api/files/read).

    actor_id: account login id / email from the system prompt.
    path: vault-relative path, e.g. Meeting/Weekly-Sync.md
    raw: if true, return markdown body only; otherwise full JSON.
    return: note JSON (includes deep-link url) or markdown body
    """
    try:
        uid = _require_actor(actor_id)
        path = vault.normalize_vault_path(path)
        logger.info("vault_read actor_id=%s path=%s raw=%s", uid, path, raw)
        # html=1: MCP App Preview needs content_html; the web UI omits it for speed.
        payload = vault.api_request(
            "GET",
            "/api/files/read",
            user_id=uid,
            query={"path": path, "html": "1"},
        )
        text = str(payload.get("content") or "") if raw else None
        return _result("note", payload, text)
    except Exception as e:
        return _error(e)


# Viewer helpers (no MCP App UI of their own).
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".bmp"}
_MAX_ASSET_BYTES = 2 * 1024 * 1024


@apps.tool(resource_uri=VIEW_URI, visibility=["app"])
def vault_render(actor_id: str, content: str, path: str = "") -> CallToolResult:
    """
    Render markdown to HTML for the MCP App preview (POST /api/files/render).

    Uses the same pipeline as public ``/s/{token}`` share pages (not the client kit).
    actor_id: account login id / email from the system prompt.
    content: full markdown body.
    path: optional vault-relative note path (context only).
    return: JSON with html (structuredContent; not for model text).
    """
    try:
        uid = _require_actor(actor_id)
        if content is None:
            raise ValueError("content is required")
        if path:
            path = vault.normalize_vault_path(path)
        logger.info("vault_render actor_id=%s path=%s bytes=%s", uid, path or "-", len(content))
        payload = vault.api_request(
            "POST",
            "/api/files/render",
            user_id=uid,
            body={"content": content, "path": path or ""},
        )
        html = str((payload or {}).get("html") or "")
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=vault.to_json({"ok": True, "path": path or None, "html_bytes": len(html)}),
                )
            ],
            structured_content={"ok": True, "kind": "render", "path": path or None, "html": html},
        )
    except Exception as e:
        return _error(e)


@apps.tool(resource_uri=VIEW_URI, visibility=["app"])
def vault_asset(actor_id: str, path: str) -> CallToolResult:
    """
    Read a vault attachment for the MCP App viewer (GET /api/files/raw).

    actor_id: account login id / email from the system prompt.
    path: vault-relative file path, e.g. AI/Drawing/img_abc.png
    return: JSON with data_url for images (used by the vault viewer; not for model text).
    """
    import base64
    import mimetypes
    from pathlib import PurePosixPath

    try:
        uid = _require_actor(actor_id)
        path = vault.normalize_vault_path(path)
        ext = PurePosixPath(path).suffix.lower()
        if ext not in _IMAGE_EXTS:
            raise ValueError(f"unsupported asset type: {ext or '(none)'}")
        logger.info("vault_asset actor_id=%s path=%s", uid, path)
        raw, ctype = vault.api_request_bytes(
            "GET",
            "/api/files/raw",
            user_id=uid,
            query={"path": path},
            max_bytes=_MAX_ASSET_BYTES,
        )
        if not ctype or ctype == "application/octet-stream":
            guessed, _ = mimetypes.guess_type(path)
            ctype = guessed or "application/octet-stream"
        data_url = f"data:{ctype};base64,{base64.b64encode(raw).decode('ascii')}"
        payload = {
            "path": path,
            "mime": ctype,
            "bytes": len(raw),
            "data_url": data_url,
        }
        # Short text for logs/model; full data_url lives in structuredContent only.
        return CallToolResult(
            content=[TextContent(type="text", text=vault.to_json({"ok": True, "path": path, "bytes": len(raw)}))],
            structured_content={"ok": True, "kind": "asset", **payload},
        )
    except Exception as e:
        return _error(e)


@apps.tool(resource_uri=VIEW_URI, meta={"ui/resourceUri": VIEW_URI})
def vault_search(actor_id: str, query: str, limit: int = 50) -> CallToolResult:
    """
    Full-text search over the vault index (GET /api/search).

    actor_id: account login id / email from the system prompt.
    query: search query text.
    limit: max results (1–200, default 50).
    return: JSON {query, results} with deep-link url per hit — reply as a linked TOC
    """
    try:
        uid = _require_actor(actor_id)
        q = (query or "").strip()
        if not q:
            raise ValueError("query is required")
        lim = max(1, min(int(limit or 50), 200))
        logger.info("vault_search actor_id=%s q=%s limit=%s", uid, q, lim)
        return _result(
            "search", vault.api_request("GET", "/api/search", user_id=uid, query={"q": q, "limit": lim})
        )
    except Exception as e:
        return _error(e)


@apps.tool(resource_uri=VIEW_URI, meta={"ui/resourceUri": VIEW_URI})
def vault_graph(actor_id: str) -> CallToolResult:
    """
    Return the wiki-link graph for the vault (GET /api/graph).

    actor_id: account login id / email from the system prompt.
    return: JSON graph (nodes/links)
    """
    try:
        uid = _require_actor(actor_id)
        logger.info("vault_graph actor_id=%s", uid)
        return _result("graph", vault.api_request("GET", "/api/graph", user_id=uid))
    except Exception as e:
        return _error(e)


@apps.tool(resource_uri=VIEW_URI, meta={"ui/resourceUri": VIEW_URI})
def vault_backlinks(actor_id: str, path: str) -> CallToolResult:
    """
    Return notes that link to the given note (GET /api/graph/backlinks).

    actor_id: account login id / email from the system prompt.
    path: vault-relative note path.
    return: JSON backlinks
    """
    try:
        uid = _require_actor(actor_id)
        path = vault.normalize_vault_path(path)
        logger.info("vault_backlinks actor_id=%s path=%s", uid, path)
        return _result(
            "backlinks",
            vault.api_request("GET", "/api/graph/backlinks", user_id=uid, query={"path": path}),
        )
    except Exception as e:
        return _error(e)


def _ob_note_origins() -> list[str]:
    try:
        base = vault.vault_base_url().rstrip("/")
    except Exception:
        return []
    return [base] if base.startswith("https://") else []


apps.add_html_resource(
    VIEW_URI,
    _load_html(),
    title="Vault viewer",
    description="ob-note vault viewer: search results, notes, file tree, backlinks and graph",
    # Note images/videos load straight from ob-note's public-share route
    # (``{ob-note}/s/{token}/raw``), so the iframe may fetch from that origin only.
    csp=ResourceCsp(resource_domains=_ob_note_origins()) if _ob_note_origins() else None,
    prefers_border=False,
)
