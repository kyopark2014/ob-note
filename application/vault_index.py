"""Vault metadata cache: wiki-links, frontmatter, search, graph (derived data)."""

from __future__ import annotations

import json
import logging
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from application import vault_backend

logger = logging.getLogger("vault_index")

# Obsidian-style: [[Note]], [[Note|alias]], [[Note#Heading]], [[#Heading]]
WIKI_LINK_RE = re.compile(r"(!)?\[\[([^\]|]*?)(?:\|([^\]]+))?\]\]")
WORD_RE = re.compile(r"\S+")
FENCE_RE = re.compile(r"```[\s\S]*?```")
INLINE_CODE_RE = re.compile(r"`[^`]+`")


def _wiki_note_target(raw: str) -> str:
    """Note path/title from a wiki target; empty for heading-only ``[[#…]]``."""
    value = (raw or "").strip()
    if not value or value.startswith("#"):
        return ""
    if "#" in value:
        value = value.split("#", 1)[0].strip()
    return value


def _strip_code(text: str) -> str:
    text = FENCE_RE.sub(" ", text)
    return INLINE_CODE_RE.sub(" ", text)


def _norm_key(name: str) -> str:
    """NFC + casefold key for wiki targets / filenames (strip trailing .md)."""
    s = unicodedata.normalize("NFC", (name or "").strip()).replace("\\", "/")
    if s.lower().endswith(".md"):
        s = s[:-3]
    return s.casefold()


def _posix_join(parent: str, rel: str) -> str:
    """Join vault-relative paths and normalize . / .. segments."""
    parent = parent.replace("\\", "/").strip("/")
    rel = unicodedata.normalize("NFC", rel).replace("\\", "/").strip()
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


_lock = threading.RLock()
_index: dict[str, "NoteMeta"] = {}
_name_map: dict[str, str] = {}  # normalized name/alias -> relative path
_built = False


@dataclass
class NoteMeta:
    path: str
    title: str
    aliases: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)  # target names (as written)
    embeds: list[str] = field(default_factory=list)
    word_count: int = 0
    char_count: int = 0
    mtime: float = 0.0
    body_preview: str = ""


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    raw = text[3:end].strip()
    body = text[end + 4 :].lstrip("\n")
    try:
        data = yaml.safe_load(raw) or {}
        if not isinstance(data, dict):
            return {}, text
        return data, body
    except Exception:
        return {}, text


def _parse_note(rel: str, path: Path) -> NoteMeta:
    text = path.read_text(encoding="utf-8", errors="replace")
    fm, body = _split_frontmatter(text)
    title = str(fm.get("title") or "").strip()
    if not title:
        for line in body.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
    if not title:
        title = Path(rel).stem
    aliases_raw = fm.get("aliases") or []
    if isinstance(aliases_raw, str):
        aliases = [aliases_raw]
    elif isinstance(aliases_raw, list):
        aliases = [str(a) for a in aliases_raw]
    else:
        aliases = []
    tags_raw = fm.get("tags") or []
    if isinstance(tags_raw, str):
        tags = [tags_raw]
    elif isinstance(tags_raw, list):
        tags = [str(t) for t in tags_raw]
    else:
        tags = []

    links: list[str] = []
    embeds: list[str] = []
    for m in WIKI_LINK_RE.finditer(_strip_code(text)):
        is_embed = bool(m.group(1))
        target = _wiki_note_target(m.group(2) or "")
        if not target:
            continue
        if is_embed:
            embeds.append(target)
        else:
            links.append(target)

    words = WORD_RE.findall(body)
    preview = body.strip().replace("\n", " ")[:240]
    return NoteMeta(
        path=rel,
        title=title,
        aliases=aliases,
        tags=tags,
        links=links,
        embeds=embeds,
        word_count=len(words),
        char_count=len(text),
        mtime=path.stat().st_mtime,
        body_preview=preview,
    )


def _register_names(meta: NoteMeta) -> None:
    stem = Path(meta.path).stem
    for name in {stem, meta.title, *meta.aliases}:
        key = _norm_key(name)
        if key:
            _name_map[key] = meta.path


def _path_without_md(rel: str) -> str:
    rel = rel.replace("\\", "/")
    return rel[:-3] if rel.lower().endswith(".md") else rel


def _lookup_exact_path(raw: str) -> str | None:
    """Match vault-relative path with or without .md (NFC / casefold)."""
    needle = _norm_key(raw)
    if not needle:
        return None
    for rel in _index:
        if _norm_key(_path_without_md(rel)) == needle:
            return rel
    return None


def _parent_key(path: str) -> str:
    parent = str(Path(path).parent).replace("\\", "/")
    if parent == ".":
        parent = ""
    return unicodedata.normalize("NFC", parent)


def _prefer_same_folder(candidates: list[str], from_path: str | None) -> str | None:
    if not candidates:
        return None
    if len(candidates) == 1 or not from_path:
        return candidates[0]
    from_parent = _parent_key(from_path)
    same = [p for p in candidates if _parent_key(p) == from_parent]
    return same[0] if same else candidates[0]


# Separators that may follow a short wiki title when the real stem is longer
# (e.g. ``Context Engineering 개요`` → ``…개요 (LangChain)``).
_PREFIX_BOUNDARY_CHARS = frozenset(" \t(-—–:|/[]·•")


def _is_prefix_title_match(name: str, key: str) -> bool:
    """True when ``name`` starts with ``key`` at a title-like boundary.

    Exact equality is handled by the name map; here we only accept longer titles
    that continue with whitespace/punctuation (not mid-word, e.g. note↛notebook).
    """
    if not key or not name or name == key:
        return False
    if not name.startswith(key):
        return False
    return name[len(key)] in _PREFIX_BOUNDARY_CHARS


def _is_numbered_copy_stem(stem: str) -> bool:
    """Obsidian-style duplicate filename: ``Note 2``, ``Note 3``, …"""
    return bool(re.search(r" \d+$", stem or ""))


def _resolve_prefix_match(key: str, *, from_path: str | None) -> str | None:
    """Unique Obsidian-style prefix match on stem / title / alias.

    Ambiguous prefixes (e.g. ``Context Engineering`` → several notes) return
    ``None``. Numbered copies (``… 2.md``) yield to the unnumbered original.
    """
    if not key:
        return None
    hits: list[str] = []
    for rel, meta in _index.items():
        names = {_norm_key(Path(rel).stem), _norm_key(meta.title)}
        names.update(_norm_key(a) for a in meta.aliases)
        if any(_is_prefix_title_match(n, key) for n in names if n):
            hits.append(rel)
    if not hits:
        return None
    # Prefer notes in the same folder as the source.
    if from_path:
        from_parent = _parent_key(from_path)
        same = [p for p in hits if _parent_key(p) == from_parent]
        if same:
            hits = same
    if len(hits) == 1:
        return hits[0]
    # Drop ``Name 2.md`` / ``Name 3.md`` when an unnumbered sibling exists.
    non_numbered = [p for p in hits if not _is_numbered_copy_stem(Path(p).stem)]
    if len(non_numbered) == 1:
        return non_numbered[0]
    if len(non_numbered) > 1:
        return None
    # Only numbered copies left — pick the lowest number / shortest stem.
    return sorted(
        hits,
        key=lambda p: (
            int(m.group(1)) if (m := re.search(r" (\d+)$", Path(p).stem)) else 10**9,
            len(Path(p).stem),
            p.casefold(),
        ),
    )[0]


def rebuild_index() -> dict[str, Any]:
    global _built
    root = vault_backend.vault_root()
    with _lock:
        _index.clear()
        _name_map.clear()
        for path in root.rglob("*.md"):
            if ".vault" in path.parts:
                continue
            rel = path.relative_to(root).as_posix()
            try:
                meta = _parse_note(rel, path)
                _index[rel] = meta
                _register_names(meta)
            except Exception:
                logger.exception("Failed to index %s", rel)
        _built = True
        graph = build_graph_payload()
        cache = vault_backend.cache_dir()
        (cache / "index.json").write_text(
            json.dumps({k: meta.__dict__ for k, meta in _index.items()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (cache / "graph.json").write_text(
            json.dumps(graph, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        settings = vault_backend.settings_dir() / "graph.json"
        settings.write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            from application import notes_db

            notes_db.sync_from_filesystem()
        except Exception:
            logger.exception("notes_db sync after index rebuild failed")
        return {"notes": len(_index), "links": len(graph.get("edges", []))}


def ensure_index() -> None:
    global _built
    with _lock:
        if _built and _index:
            return
    rebuild_index()


def update_note(rel: str) -> None:
    root = vault_backend.vault_root()
    path = root / rel
    with _lock:
        # drop old name mappings for this path
        to_del = [k for k, v in _name_map.items() if v == rel]
        for k in to_del:
            del _name_map[k]
        if path.is_file() and path.suffix.lower() in {".md", ".markdown"}:
            meta = _parse_note(rel, path)
            _index[rel] = meta
            _register_names(meta)
        else:
            _index.pop(rel, None)
        _built = True
    # Keep SQLite registry in sync on create/save (outside index lock).
    if path.is_file() and path.suffix.lower() in {".md", ".markdown"}:
        try:
            from application import notes_db

            notes_db.on_note_written(rel)
        except Exception:
            logger.exception("notes_db update after index update_note failed for %s", rel)


def remove_note(rel: str) -> None:
    """Drop in-memory index entries only. Call notes_db.on_note_deleted for file deletes."""
    with _lock:
        _index.pop(rel, None)
        to_del = [k for k, v in _name_map.items() if v == rel]
        for k in to_del:
            del _name_map[k]


def resolve_link(name: str, *, from_path: str | None = None) -> str | None:
    """Resolve an Obsidian-style wiki target to a vault-relative .md path.

    Order (closest to Obsidian):
    1. Vault-absolute path (with/without .md)
    2. Path relative to the source note's folder
    3. Unique path suffix match
    4. Basename / title / alias (prefer same folder when ambiguous)
    5. Same-folder basename fallback for path-style links
    6. Unique title prefix (``[[Short]]`` → ``Short (subtitle).md``)
    """
    ensure_index()
    raw = unicodedata.normalize("NFC", (name or "").strip()).replace("\\", "/")
    if not raw:
        return None
    key = _norm_key(raw)
    basename_key = _norm_key(Path(raw).name)

    with _lock:
        # 1. Exact vault-relative path
        hit = _lookup_exact_path(raw)
        if hit:
            return hit

        # 2. Relative to current note directory
        if from_path:
            parent = str(Path(from_path).parent).replace("\\", "/")
            if parent == ".":
                parent = ""
            joined = _posix_join(parent, raw)
            hit = _lookup_exact_path(joined)
            if hit:
                return hit

        # 3. Path suffix (e.g. aws-services/Note matches folder/aws-services/Note.md)
        if "/" in raw.strip("/"):
            suffix_hits = [
                rel
                for rel in _index
                if _norm_key(_path_without_md(rel)) == key
                or _norm_key(_path_without_md(rel)).endswith("/" + key)
            ]
            preferred = _prefer_same_folder(suffix_hits, from_path)
            if preferred:
                return preferred

        # 4. Basename / title / alias map
        if key in _name_map:
            mapped = _name_map[key]
            # If multiple notes share the basename, prefer same folder via scan
            stem_hits = [
                rel for rel in _index if _norm_key(Path(rel).stem) == key
            ]
            if len(stem_hits) > 1:
                return _prefer_same_folder(stem_hits, from_path) or mapped
            return mapped

        if basename_key and basename_key != key and basename_key in _name_map:
            mapped = _name_map[basename_key]
            stem_hits = [
                rel for rel in _index if _norm_key(Path(rel).stem) == basename_key
            ]
            if from_path and stem_hits:
                return _prefer_same_folder(stem_hits, from_path) or mapped
            return mapped

        # 5. Same-folder basename when path prefix was wrong (e.g. [[aws-services/Note]]
        #    but Note.md lives beside the source)
        if from_path and basename_key:
            stem_hits = [
                rel for rel in _index if _norm_key(Path(rel).stem) == basename_key
            ]
            same = _prefer_same_folder(stem_hits, from_path)
            if same and _parent_key(same) == _parent_key(from_path):
                return same
            if len(stem_hits) == 1:
                return stem_hits[0]

        # 6. Unique prefix on stem / title / alias (short wiki → longer note title)
        prefix_keys = [key]
        if basename_key and basename_key != key:
            prefix_keys.append(basename_key)
        for pk in prefix_keys:
            hit = _resolve_prefix_match(pk, from_path=from_path)
            if hit:
                return hit

        return None


def get_meta(rel: str) -> NoteMeta | None:
    ensure_index()
    with _lock:
        return _index.get(rel)


def list_metas() -> list[NoteMeta]:
    ensure_index()
    with _lock:
        return list(_index.values())


def backlinks(rel: str) -> list[dict[str, str]]:
    ensure_index()
    stem = _norm_key(Path(rel).stem)
    meta = get_meta(rel)
    names = {stem}
    if meta:
        names.add(_norm_key(meta.title))
        names.update(_norm_key(a) for a in meta.aliases)
    results: list[dict[str, str]] = []
    with _lock:
        others = list(_index.values())
    for other in others:
        if other.path == rel:
            continue
        for link in other.links:
            resolved = resolve_link(link, from_path=other.path)
            if resolved == rel or _norm_key(link) in names:
                results.append({"path": other.path, "title": other.title})
                break
    return results


def search(query: str, *, limit: int = 50) -> list[dict[str, Any]]:
    ensure_index()
    q = (query or "").strip().lower()
    if not q:
        return []
    hits: list[dict[str, Any]] = []
    root = vault_backend.vault_root()
    with _lock:
        metas = list(_index.values())
    for meta in metas:
        score = 0
        hay_title = f"{meta.title} {' '.join(meta.aliases)} {' '.join(meta.tags)} {meta.path}".lower()
        if q in hay_title:
            score += 10
        if q in meta.body_preview.lower():
            score += 3
        # full text scan for stronger match
        try:
            text = (root / meta.path).read_text(encoding="utf-8", errors="replace").lower()
            if q in text:
                score += 5
                idx = text.find(q)
                snippet = text[max(0, idx - 40) : idx + len(q) + 60].replace("\n", " ")
            else:
                snippet = meta.body_preview
        except OSError:
            snippet = meta.body_preview
        if score > 0:
            hits.append(
                {
                    "path": meta.path,
                    "title": meta.title,
                    "tags": meta.tags,
                    "score": score,
                    "snippet": snippet,
                }
            )
    hits.sort(key=lambda h: (-h["score"], h["title"].lower()))
    return hits[:limit]


def build_graph_payload() -> dict[str, Any]:
    nodes = []
    edges = []
    seen_edges: set[tuple[str, str]] = set()
    with _lock:
        metas = list(_index.values())
    for meta in metas:
        nodes.append(
            {
                "id": meta.path,
                "label": meta.title or Path(meta.path).stem,
                "path": meta.path,
                "tags": meta.tags,
            }
        )
        for link in meta.links:
            target = resolve_link(link, from_path=meta.path)
            if not target:
                # unresolved — still show dangling node id as name
                dangling_id = f"missing:{link}"
                if not any(n["id"] == dangling_id for n in nodes):
                    nodes.append(
                        {
                            "id": dangling_id,
                            "label": link,
                            "path": None,
                            "tags": [],
                            "missing": True,
                        }
                    )
                target = dangling_id
            key = (meta.path, target)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edges.append({"source": meta.path, "target": target})
    return {"nodes": nodes, "edges": edges}


def get_graph() -> dict[str, Any]:
    ensure_index()
    with _lock:
        return build_graph_payload()
