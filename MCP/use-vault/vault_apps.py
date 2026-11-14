"""MCP Apps (interactive UI) for the read tools of the use-vault MCP server.

Six read tools carry ``_meta.ui.resourceUri`` -> ``ui://vault/viewer.html``. Each returns
the same JSON text as before (what the model reads) plus a ``structuredContent`` object the
UI draws from: ``{"ok": true, "kind": "search|note|list|tree|graph|backlinks", ...payload}``
with a deep link ``url`` on every note.

The viewer HTML is built from ``ui/`` (``python3 MCP/use-vault/ui/build.py``).
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any
from urllib.parse import quote_plus

import vault_client as vault
from mcp.server.apps import Apps
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
    return f"{vault.vault_base_url().rstrip('/')}/?note={quote_plus(path)}"


def with_urls(value: Any) -> Any:
    """Add a deep-link ``url`` to every note-like dict (has a string ``path``, not a folder)."""
    if isinstance(value, list):
        return [with_urls(v) for v in value]
    if isinstance(value, dict):
        out = {k: with_urls(v) for k, v in value.items()}
        path = out.get("path")
        if isinstance(path, str) and path and out.get("type") != "folder" and "url" not in out:
            out["url"] = note_url(path)
        return out
    return value


def _require_actor(actor_id: str) -> str:
    value = (actor_id or "").strip()
    if not value:
        raise ValueError("actor_id is required (account login id / email)")
    return value


def _result(kind: str, payload: Any, text: str | None = None) -> CallToolResult:
    body = with_urls(payload) if isinstance(payload, dict) else {"data": payload}
    return CallToolResult(
        # Same text the tools returned before: the model's view of the result is unchanged.
        content=[TextContent(type="text", text=text if text is not None else vault.to_json(payload))],
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
    return: JSON tree
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
    return: JSON file list
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
    return: note JSON or markdown body
    """
    try:
        uid = _require_actor(actor_id)
        path = vault.normalize_vault_path(path)
        logger.info("vault_read actor_id=%s path=%s raw=%s", uid, path, raw)
        payload = vault.api_request("GET", "/api/files/read", user_id=uid, query={"path": path})
        text = str(payload.get("content") or "") if raw else None
        return _result("note", payload, text)
    except Exception as e:
        return _error(e)


@apps.tool(resource_uri=VIEW_URI, meta={"ui/resourceUri": VIEW_URI})
def vault_search(actor_id: str, query: str, limit: int = 50) -> CallToolResult:
    """
    Full-text search over the vault index (GET /api/search).

    actor_id: account login id / email from the system prompt.
    query: search query text.
    limit: max results (1–200, default 50).
    return: JSON {query, results}
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


apps.add_html_resource(
    VIEW_URI,
    _load_html(),
    title="Vault viewer",
    description="ob-note vault viewer: search results, notes, file tree, backlinks and graph",
    # Self-contained: no external origins are needed or allowed.
    prefers_border=False,
)
