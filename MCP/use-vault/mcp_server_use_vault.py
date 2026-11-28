"""Streamable-HTTP MCP server for ob-note vault (use-vault).

Exposes vault HTTP API tools and Skills over MCP (``skills/use-vault/SKILL.md``).
Always pass actor_id (account login id / email) so requests are scoped to
that user's vault.
"""

from __future__ import annotations

import base64
import logging
import sys

import vault_client as vault
from mcp.server.mcpserver import MCPServer
from vault_apps import apps, with_urls
from vault_skills import skills

logging.basicConfig(
    level=logging.INFO,
    format="%(filename)s:%(lineno)d | %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("use-vault-mcp")

INSTRUCTIONS = """\
You manage markdown notes in an ob-note vault (Obsidian-like).
.md files are the source of truth. Each account has an isolated vault.
Default actor_id is kyopark2014@gmail.com (tools apply this when omitted or
when a nickname without @ is passed). Prefer the real Google login email;
never use a display name, OS username, or local-dev.

Critical rules:
1. Paths are vault-relative, e.g. Meeting/Weekly-Sync.md
2. Note body: do NOT use YAML frontmatter; start with a single # Title line
3. Never create new notes at the vault root — only under a topic folder
   (Category/Note.md)
4. Prefer vault_read / vault_search before writing. Mid-note edits:
   vault_str_replace (old_string → new_string). End additions: vault_append.
   Full rewrite only: vault_write when the user asks to save/replace the whole note.
5. Tool JSON includes deep-link ``url`` (login-gated ``{vault}/?note=...``).
   Whenever you mention a note path to the user, ALWAYS include a clickable
   markdown link: [Note Title](url). Never show a bare path or bare URL alone.
6. For list/search/tree summaries, present notes as a markdown TOC/table with
   linked titles, e.g.:
   - [guardrail 활용 방법](https://vault.../?note=Guardrail%2F...)
   Even when an interactive MCP App UI is shown, still include this linked TOC
   in the chat reply so users can open notes from the message.
7. After vault_write/vault_append/vault_str_replace/vault_rename success,
   confirm with path + clickable ``url`` (and ok: true). Do not omit the link.
8. Long notes: keep one markdown file when possible; put a ## 목차 under the
   title. Same-note sections use [heading](#anchor); references to other vault
   notes use [title](url) from tool results (not public /s/ shares).
9. Note body language: formal Korean 합니다체 (not 한다체). No CloudFront
   /artifacts URLs inside notes — upload with vault_upload, then link
   vault-relative image paths only (same folder as the note).
"""

try:
    mcp = MCPServer(
        name="use-vault",
        instructions=INSTRUCTIONS,
        # Apps: interactive vault viewer. Skills: SEP-2640 skill://use-vault/...
        extensions=[apps, skills],
    )
    logger.info("MCP server initialized successfully (apps + skills extensions)")
except Exception as e:
    logger.info("Error: %s", e)
    raise


def _require_actor(actor_id: str = "") -> str:
    # Empty or nickname (no @) → DEFAULT_ACTOR_ID (kyopark2014@gmail.com).
    return vault.resolve_actor_id(actor_id)


def _ok(payload) -> str:
    # Same deep-link ``url`` fields as my-vaults / vault_apps read tools.
    return vault.to_json(with_urls(payload))


def _err(exc: Exception) -> str:
    logger.error("%s", exc)
    return vault.to_json({"ok": False, "error": str(exc)})


@mcp.tool()
def vault_health(actor_id: str = vault.DEFAULT_ACTOR_ID) -> str:
    """
    Check ob-note vault API health (GET /api/health).

    actor_id: Google login email (default kyopark2014@gmail.com; nicknames without @ ignored).
    return: JSON health payload
    """
    try:
        uid = _require_actor(actor_id)
        logger.info("vault_health actor_id=%s", uid)
        return _ok(vault.api_request("GET", "/api/health", user_id=uid))
    except Exception as e:
        return _err(e)


@mcp.tool()
def vault_write(path: str, content: str, actor_id: str = vault.DEFAULT_ACTOR_ID) -> str:
    """
    Create or overwrite a vault note (PUT /api/files/write).

    actor_id: Google login email (default kyopark2014@gmail.com; nicknames without @ ignored).
    path: vault-relative path under a topic folder, e.g. Meeting/Note.md
          (do not write to vault root).
    content: full markdown body. No YAML frontmatter; start with # Title.
          For long notes include ## 목차 with markdown links.
    return: JSON write result including path and deep-link url
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
    path: str,
    content: str,
    actor_id: str = vault.DEFAULT_ACTOR_ID,
    create: bool = True,
    separator: str = "\n",
) -> str:
    """
    Append text to a vault note (POST /api/files/append).

    actor_id: Google login email (default kyopark2014@gmail.com; nicknames without @ ignored).
    path: vault-relative path.
    content: text to append.
    create: if true, create the file when missing (default true).
    separator: inserted when the file does not end with a newline (default \\n).
    return: JSON append result including path and deep-link url
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
def vault_str_replace(
    path: str,
    old_string: str,
    new_string: str,
    actor_id: str = vault.DEFAULT_ACTOR_ID,
    replace_all: bool = False,
) -> str:
    """
    Partially edit a vault note by replacing exact text (read → replace → write).

    Prefer this over vault_write when only a section/paragraph changes.
    For end-of-file additions use vault_append instead.

    actor_id: Google login email (default kyopark2014@gmail.com; nicknames without @ ignored).
    path: vault-relative path, e.g. Meeting/Note.md
    old_string: exact substring to find (must be unique unless replace_all).
    new_string: replacement text (may be empty to delete the match).
    replace_all: if true, replace every occurrence; if false (default),
          require exactly one match.
    return: JSON with path, url, replacements count, and write metadata
    """
    try:
        uid = _require_actor(actor_id)
        path = vault.normalize_vault_path(path)
        if old_string is None or old_string == "":
            raise ValueError("old_string is required and must be non-empty")
        if new_string is None:
            raise ValueError("new_string is required (use empty string to delete)")
        if old_string == new_string:
            raise ValueError("old_string and new_string are identical; nothing to change")

        logger.info(
            "vault_str_replace actor_id=%s path=%s old_len=%s new_len=%s replace_all=%s",
            uid,
            path,
            len(old_string),
            len(new_string),
            replace_all,
        )
        current = vault.api_request(
            "GET",
            "/api/files/read",
            user_id=uid,
            query={"path": path},
        )
        if not isinstance(current, dict):
            raise ValueError(f"unexpected read response for {path}")
        content = current.get("content")
        if content is None:
            raise ValueError(f"no content returned for {path}")
        text = str(content)
        count = text.count(old_string)
        if count == 0:
            raise ValueError(
                "old_string not found in note; re-read with vault_read and "
                "copy the exact text to replace"
            )
        if count > 1 and not replace_all:
            raise ValueError(
                f"old_string matched {count} times; provide more surrounding "
                "context for a unique match, or set replace_all=true"
            )

        if replace_all:
            updated = text.replace(old_string, new_string)
            replacements = count
        else:
            updated = text.replace(old_string, new_string, 1)
            replacements = 1

        result = vault.api_request(
            "PUT",
            "/api/files/write",
            user_id=uid,
            body={"path": path, "content": updated},
        )
        if not isinstance(result, dict):
            result = {"ok": True, "path": path}
        result["replacements"] = replacements
        result["replace_all"] = bool(replace_all)
        return _ok(result)
    except Exception as e:
        return _err(e)


@mcp.tool()
def vault_upload(
    path: str,
    actor_id: str = vault.DEFAULT_ACTOR_ID,
    content_base64: str = "",
    source_url: str = "",
    content_type: str = "",
    filename: str = "",
) -> str:
    """
    Upload a binary file (image, pdf, …) into the vault (POST /api/files/upload).

    Put images in the **same folder as the note**, then link with a relative
    path in markdown: ``![alt](diagram.png)``. Do NOT leave CloudFront
    ``/artifacts/*`` URLs in notes.

    Provide exactly one of ``content_base64`` or ``source_url``.

    actor_id: Google login email (default kyopark2014@gmail.com; nicknames without @ ignored).
    path: vault-relative destination under a topic folder,
          e.g. Meeting/diagram.png (not vault root).
    content_base64: raw file bytes encoded as standard base64 (not a data: URL).
    source_url: http(s) URL to fetch and upload (when bytes are not in-context).
    content_type: optional MIME type (guessed from filename when omitted).
    filename: optional multipart filename (defaults to path basename).
    return: JSON upload result including path and deep-link url
    """
    try:
        uid = _require_actor(actor_id)
        path = vault.normalize_vault_path(path)
        if "/" not in path:
            raise ValueError(
                "uploads must live under a topic folder "
                "(e.g. Meeting/diagram.png), not vault root"
            )

        b64 = (content_base64 or "").strip()
        url = (source_url or "").strip()
        if b64 and url:
            raise ValueError("provide only one of content_base64 or source_url")
        if not b64 and not url:
            raise ValueError("content_base64 or source_url is required")

        ctype = (content_type or "").strip()
        if b64:
            # Allow accidental data-URL prefix from models.
            if b64.startswith("data:") and "," in b64:
                header, b64 = b64.split(",", 1)
                if not ctype and ";base64" in header:
                    mime = header[5:].split(";", 1)[0].strip()
                    if mime:
                        ctype = mime
            try:
                data = base64.b64decode(b64, validate=False)
            except Exception as exc:
                raise ValueError(f"invalid content_base64: {exc}") from exc
        else:
            data, fetched_ctype = vault.fetch_bytes(url)
            if not ctype:
                ctype = fetched_ctype

        if not data:
            raise ValueError("empty file")

        name = (filename or "").strip() or path.rsplit("/", 1)[-1]
        logger.info(
            "vault_upload actor_id=%s path=%s bytes=%s via=%s",
            uid,
            path,
            len(data),
            "base64" if b64 else "url",
        )
        return _ok(
            vault.api_upload(
                path,
                data,
                filename=name,
                content_type=ctype or None,
                user_id=uid,
            )
        )
    except Exception as e:
        return _err(e)


@mcp.tool()
def vault_mkdir(path: str, actor_id: str = vault.DEFAULT_ACTOR_ID) -> str:
    """
    Create a folder in the vault (POST /api/files/mkdir).

    actor_id: Google login email (default kyopark2014@gmail.com; nicknames without @ ignored).
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
def vault_rename(from_path: str, to_path: str, actor_id: str = vault.DEFAULT_ACTOR_ID) -> str:
    """
    Rename or move a vault file/folder (POST /api/files/rename).

    actor_id: Google login email (default kyopark2014@gmail.com; nicknames without @ ignored).
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
def vault_delete(path: str, actor_id: str = vault.DEFAULT_ACTOR_ID) -> str:
    """
    Delete a vault file or folder (POST /api/files/delete).

    actor_id: Google login email (default kyopark2014@gmail.com; nicknames without @ ignored).
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
def vault_rebuild(actor_id: str = vault.DEFAULT_ACTOR_ID) -> str:
    """
    Rebuild the vault wiki-link graph index (POST /api/graph/rebuild).

    actor_id: Google login email (default kyopark2014@gmail.com; nicknames without @ ignored).
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
