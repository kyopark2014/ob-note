"""Skills over MCP extension for use-vault (SEP-2640).

Serves Agent Skills from ``skills/<name>/`` beside this package via
``skills/list``, ``skills/get``, and ``resources/read`` on ``skill://`` URIs.

Uses the official SDK ``Extension`` surface (same pattern as ``Apps``). The
Python SDK does not yet ship a Skills helper, so handlers follow the Beta
spec in modelcontextprotocol/ext-skills.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

import yaml
from mcp.server.context import ServerRequestContext
from mcp.server.extension import Extension, MethodBinding, ResourceBinding
from mcp.server.mcpserver.resources import TextResource
from mcp_types import PaginatedRequestParams, RequestParams
from pydantic import Field

logger = logging.getLogger("use-vault-mcp.skills")

EXTENSION_ID = "io.modelcontextprotocol/skills"
DEFAULT_TTL_MS = 300_000
DEFAULT_CACHE_SCOPE = "public"

_SKILLS_ROOT = Path(__file__).resolve().parent / "skills"


class GetSkillRequestParams(RequestParams):
    """Params for ``skills/get``."""

    uri: str = Field(..., description="Resource URI of the skill's SKILL.md")


def _sha256_hex(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _parse_frontmatter(raw: str) -> dict[str, Any]:
    if not raw.startswith("---"):
        return {}
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}
    data = yaml.safe_load(parts[1]) or {}
    return data if isinstance(data, dict) else {}


def _mime_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".md", ".markdown"}:
        return "text/markdown"
    if suffix in {".json"}:
        return "application/json"
    if suffix in {".py", ".txt", ".sh", ".yaml", ".yml"}:
        return "text/plain"
    return "application/octet-stream"


class _SkillCatalogEntry:
    """One skill directory loaded into memory with a verified manifest."""

    def __init__(self, skill_dir: Path):
        self.skill_dir = skill_dir
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            raise FileNotFoundError(f"missing SKILL.md in {skill_dir}")

        self.files: dict[str, bytes] = {}
        for path in sorted(skill_dir.rglob("*")):
            if not path.is_file():
                continue
            if path.name.startswith(".") or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(skill_dir).as_posix()
            self.files[rel] = path.read_bytes()

        if "SKILL.md" not in self.files:
            raise FileNotFoundError(f"SKILL.md not readable in {skill_dir}")

        text = self.files["SKILL.md"].decode("utf-8")
        self.frontmatter = _parse_frontmatter(text)
        name = (self.frontmatter.get("name") or skill_dir.name).strip()
        if not name:
            raise ValueError(f"skill name missing in {skill_md}")
        if skill_dir.name != name:
            logger.warning(
                "skill folder %r does not match frontmatter name %r; using folder name",
                skill_dir.name,
                name,
            )
            name = skill_dir.name
        self.name = name
        self.description = str(self.frontmatter.get("description") or "")
        self.uri = f"skill://{self.name}/SKILL.md"
        self.resources = [
            {
                "uri": f"skill://{self.name}/{rel}",
                "digest": _sha256_hex(body),
                "size": len(body),
            }
            for rel, body in self.files.items()
        ]

    def entry_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "frontmatter": dict(self.frontmatter),
            "resources": list(self.resources),
        }

    def body_for(self, uri: str) -> tuple[bytes, str] | None:
        prefix = f"skill://{self.name}/"
        if not uri.startswith(prefix):
            return None
        rel = uri[len(prefix) :]
        body = self.files.get(rel)
        if body is None:
            return None
        return body, _mime_for(Path(rel))


def load_skill_catalog(skills_root: Path | None = None) -> list[_SkillCatalogEntry]:
    root = skills_root or _SKILLS_ROOT
    if not root.is_dir():
        logger.warning("skills root missing: %s", root)
        return []
    entries: list[_SkillCatalogEntry] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if not (child / "SKILL.md").is_file():
            continue
        try:
            entries.append(_SkillCatalogEntry(child))
            logger.info("Loaded MCP skill %s from %s", child.name, child)
        except Exception as exc:
            logger.error("Failed to load skill from %s: %s", child, exc)
    return entries


class Skills(Extension):
    """SEP-2640 Skills over MCP extension."""

    identifier = EXTENSION_ID

    def __init__(self, skills_root: Path | None = None):
        self._entries = load_skill_catalog(skills_root)
        self._by_uri = {e.uri: e for e in self._entries}
        # Also index by any file URI for get lookups of SKILL.md only via entry uri.
        self._resources: list[ResourceBinding] = []
        for entry in self._entries:
            for rel, body in entry.files.items():
                uri = f"skill://{entry.name}/{rel}"
                try:
                    text = body.decode("utf-8")
                except UnicodeDecodeError:
                    # Spec allows blob; TextResource needs text — skip binary for now.
                    logger.warning("Skipping non-UTF-8 skill file %s", uri)
                    continue
                self._resources.append(
                    ResourceBinding(
                        resource=TextResource(
                            uri=uri,
                            name=Path(rel).name,
                            description=f"{entry.name}/{rel}",
                            mime_type=_mime_for(Path(rel)),
                            text=text,
                        )
                    )
                )

    def settings(self) -> dict[str, Any]:
        return {"directoryRead": False}

    def resources(self) -> list[ResourceBinding]:
        return self._resources

    def methods(self) -> list[MethodBinding]:
        return [
            MethodBinding(
                method="skills/list",
                params_type=PaginatedRequestParams,
                handler=self._handle_list,
            ),
            MethodBinding(
                method="skills/get",
                params_type=GetSkillRequestParams,
                handler=self._handle_get,
            ),
        ]

    def _envelope(self, **payload: Any) -> dict[str, Any]:
        return {
            "resultType": "complete",
            "ttlMs": DEFAULT_TTL_MS,
            "cacheScope": DEFAULT_CACHE_SCOPE,
            **payload,
        }

    async def _handle_list(
        self,
        ctx: ServerRequestContext[Any, Any],
        params: PaginatedRequestParams | None,
    ) -> dict[str, Any]:
        # Pagination unused for a single bundled skill; ignore cursor.
        _ = ctx, params
        return self._envelope(skills=[e.entry_dict() for e in self._entries])

    async def _handle_get(
        self,
        ctx: ServerRequestContext[Any, Any],
        params: GetSkillRequestParams,
    ) -> dict[str, Any]:
        _ = ctx
        uri = (params.uri or "").strip()
        entry = self._by_uri.get(uri)
        if entry is None:
            from mcp.shared.exceptions import MCPError
            from mcp_types import INVALID_PARAMS

            raise MCPError(code=INVALID_PARAMS, message=f"Unknown skill URI: {uri}")
        return self._envelope(skill=entry.entry_dict())


# Default singleton for MCPServer(extensions=[..., skills])
skills = Skills()
