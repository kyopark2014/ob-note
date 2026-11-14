"""Streamable-HTTP MCP server for ob-note vault (use-vault).

Exposes the same read/write surface as skills/use-vault via vault HTTP API.
Always pass actor_id (account login id / email) so requests are scoped to
that user's vault.
"""

from __future__ import annotations

import logging
import sys

import vault_client as vault
from mcp.server.mcpserver import MCPServer
from vault_apps import apps

logging.basicConfig(
    level=logging.INFO,
    format="%(filename)s:%(lineno)d | %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("use-vault-mcp")

INSTRUCTIONS = """\
You manage markdown notes in an ob-note vault (Obsidian-like).
.md files are the source of truth. Each account has an isolated vault;
always pass actor_id from the system prompt (account login id / email) —
never a nickname or display name.

Critical rules:
1. Paths are vault-relative, e.g. Meeting/Weekly-Sync.md
2. Note body: do NOT use YAML frontmatter; start with a single # Title line
3. Never create new notes at the vault root — only under a topic folder
   (Category/Note.md)
4. Prefer vault_read / vault_search before writing; overwrite with vault_write
   only when the user asks to save or update a note
"""

try:
    mcp = MCPServer(
        name="use-vault",
        instructions=INSTRUCTIONS,
        extensions=[apps],  # read tools with the interactive vault viewer (see vault_apps.py)
    )
    logger.info("MCP server initialized successfully")
except Exception as e:
    logger.info("Error: %s", e)
    raise


def _require_actor(actor_id: str) -> str:
    value = (actor_id or "").strip()
    if not value:
        raise ValueError("actor_id is required (account login id / email)")
    return value


def _ok(payload) -> str:
    return vault.to_json(payload)


def _err(exc: Exception) -> str:
    logger.error("%s", exc)
    return vault.to_json({"ok": False, "error": str(exc)})


@mcp.tool()
def vault_health(actor_id: str = "") -> str:
    """
    Check ob-note vault API health (GET /api/health).

    actor_id: optional for health; pass account login id when available.
    return: JSON health payload
    """
    try:
        uid = (actor_id or "").strip() or "local-dev"
        logger.info("vault_health actor_id=%s", uid)
        return _ok(vault.api_request("GET", "/api/health", user_id=uid))
    except Exception as e:
        return _err(e)


@mcp.tool()
def vault_write(actor_id: str, path: str, content: str) -> str:
    """
    Create or overwrite a vault note (PUT /api/files/write).

    actor_id: account login id / email from the system prompt.
    path: vault-relative path under a topic folder, e.g. Meeting/Note.md
          (do not write to vault root).
    content: full markdown body. No YAML frontmatter; start with # Title.
    return: JSON write result
    """
    try:
        uid = _require_actor(actor_id)
        path = vault.normalize_vault_path(path)
        if "/" not in path:
            raise ValueError(
                "new notes must live under a topic folder "
                "(e.g. Meeting/Note.md), not vault root"
            )
        if content is None:
            raise ValueError("content is required")
        logger.info("vault_write actor_id=%s path=%s bytes=%s", uid, path, len(content))
        return _ok(
            vault.api_request(
                "PUT",
                "/api/files/write",
                user_id=uid,
                body={"path": path, "content": content},
            )
        )
    except Exception as e:
        return _err(e)


@mcp.tool()
def vault_append(
    actor_id: str,
    path: str,
    content: str,
    create: bool = True,
    separator: str = "\n",
) -> str:
    """
    Append text to a vault note (POST /api/files/append).

    actor_id: account login id / email from the system prompt.
    path: vault-relative path.
    content: text to append.
    create: if true, create the file when missing (default true).
    separator: inserted when the file does not end with a newline (default \\n).
    return: JSON append result
    """
    try:
        uid = _require_actor(actor_id)
        path = vault.normalize_vault_path(path)
        if content is None:
            raise ValueError("content is required")
        logger.info("vault_append actor_id=%s path=%s", uid, path)
        return _ok(
            vault.api_request(
                "POST",
                "/api/files/append",
                user_id=uid,
                body={
                    "path": path,
                    "content": content,
                    "create": bool(create),
                    "separator": separator if separator is not None else "\n",
                },
            )
        )
    except Exception as e:
        return _err(e)


@mcp.tool()
def vault_mkdir(actor_id: str, path: str) -> str:
    """
    Create a folder in the vault (POST /api/files/mkdir).

    actor_id: account login id / email from the system prompt.
    path: vault-relative folder path, e.g. Meeting/projects
    return: JSON mkdir result
    """
    try:
        uid = _require_actor(actor_id)
        path = vault.normalize_vault_path(path)
        logger.info("vault_mkdir actor_id=%s path=%s", uid, path)
        return _ok(
            vault.api_request(
                "POST",
                "/api/files/mkdir",
                user_id=uid,
                body={"path": path},
            )
        )
    except Exception as e:
        return _err(e)


@mcp.tool()
def vault_rename(actor_id: str, from_path: str, to_path: str) -> str:
    """
    Rename or move a vault file/folder (POST /api/files/rename).

    actor_id: account login id / email from the system prompt.
    from_path: current vault-relative path.
    to_path: destination vault-relative path.
    return: JSON rename result
    """
    try:
        uid = _require_actor(actor_id)
        from_path = vault.normalize_vault_path(from_path)
        to_path = vault.normalize_vault_path(to_path)
        logger.info("vault_rename actor_id=%s %s → %s", uid, from_path, to_path)
        return _ok(
            vault.api_request(
                "POST",
                "/api/files/rename",
                user_id=uid,
                body={"from_path": from_path, "to_path": to_path},
            )
        )
    except Exception as e:
        return _err(e)


@mcp.tool()
def vault_delete(actor_id: str, path: str) -> str:
    """
    Delete a vault file or folder (POST /api/files/delete).

    actor_id: account login id / email from the system prompt.
    path: vault-relative path to delete.
    return: JSON delete result
    """
    try:
        uid = _require_actor(actor_id)
        path = vault.normalize_vault_path(path)
        logger.info("vault_delete actor_id=%s path=%s", uid, path)
        return _ok(
            vault.api_request(
                "POST",
                "/api/files/delete",
                user_id=uid,
                body={"path": path},
            )
        )
    except Exception as e:
        return _err(e)


@mcp.tool()
def vault_rebuild(actor_id: str) -> str:
    """
    Rebuild the vault wiki-link graph index (POST /api/graph/rebuild).

    actor_id: account login id / email from the system prompt.
    return: JSON rebuild / sync status
    """
    try:
        uid = _require_actor(actor_id)
        logger.info("vault_rebuild actor_id=%s", uid)
        return _ok(vault.api_request("POST", "/api/graph/rebuild", user_id=uid))
    except Exception as e:
        return _err(e)


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=8000,
        stateless_http=True,
    )
