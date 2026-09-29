"""Move images referenced by a note when the note changes folders.

Companion assets live alongside the note (same directory, relative refs like
``![alt](img.png)`` or ``![[img.png]]``). Moving the note alone would leave
broken image links; this module relocates those siblings in a background
thread so the rename/move API stays fast.
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote

from application import vault_backend, vault_sync

logger = logging.getLogger("vault_companion_assets")

IMAGE_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".bmp",
    ".heic",
    ".avif",
}
VIDEO_SUFFIXES = {
    ".mp4",
    ".m4v",
    ".webm",
}
MEDIA_SUFFIXES = IMAGE_SUFFIXES | VIDEO_SUFFIXES

_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\n]+)\)")
_WIKI_IMAGE_RE = re.compile(r"!\[\[([^\]|#]+)(?:\|[^\]]+)?\]\]")
_HTML_IMG_RE = re.compile(r"""<img[^>]+src=["']([^"']+)["']""", re.IGNORECASE)
_HTML_VIDEO_RE = re.compile(
    r"""<video\b[^>]*\bsrc=["']([^"']+)["']""",
    re.IGNORECASE,
)
_HTML_SOURCE_RE = re.compile(
    r"""<source\b[^>]*\bsrc=["']([^"']+)["']""",
    re.IGNORECASE,
)


def _norm_rel(path: str) -> str:
    return (path or "").replace("\\", "/").strip().lstrip("/")


def _parent_rel(path: str) -> str:
    cleaned = _norm_rel(path)
    if "/" not in cleaned:
        return ""
    return cleaned.rsplit("/", 1)[0]


def _is_remote_or_absolute(ref: str) -> bool:
    s = (ref or "").strip()
    if not s:
        return True
    low = s.lower()
    return (
        low.startswith("http://")
        or low.startswith("https://")
        or low.startswith("data:")
        or low.startswith("blob:")
        or s.startswith("/")
        or s.startswith("#")
    )


def extract_relative_image_refs(text: str) -> list[str]:
    """Return unique relative image destinations from markdown / wiki / HTML."""
    found: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        dest = (raw or "").strip()
        if dest.startswith("<") and dest.endswith(">"):
            dest = dest[1:-1].strip()
        try:
            dest = unquote(dest)
        except Exception:
            pass
        dest = dest.strip().replace("\\", "/")
        if not dest or _is_remote_or_absolute(dest):
            return
        # Strip optional title: path "title"
        if " " in dest and not dest.startswith("./"):
            # CommonMark: destination then optional title in quotes
            m = re.match(r'^(\S+)\s+["\'].*', dest)
            if m:
                dest = m.group(1)
        key = dest
        if key in seen:
            return
        suffix = Path(dest.split("?", 1)[0].split("#", 1)[0]).suffix.lower()
        if suffix and suffix not in MEDIA_SUFFIXES:
            return
        if not suffix:
            # Wiki embeds sometimes omit extension; still allow basename lookups later
            # only if it looks like a file name without path tricks.
            pass
        seen.add(key)
        found.append(dest)

    for match in _MD_IMAGE_RE.finditer(text or ""):
        add(match.group(2))
    for match in _WIKI_IMAGE_RE.finditer(text or ""):
        add(match.group(1))
    for match in _HTML_IMG_RE.finditer(text or ""):
        add(match.group(1))
    for match in _HTML_VIDEO_RE.finditer(text or ""):
        add(match.group(1))
    for match in _HTML_SOURCE_RE.finditer(text or ""):
        add(match.group(1))
    return found


def _same_folder_basename(ref: str) -> Optional[str]:
    """If ``ref`` is a same-directory relative path, return its basename."""
    cleaned = (ref or "").replace("\\", "/").strip()
    if not cleaned or _is_remote_or_absolute(cleaned):
        return None
    cleaned = cleaned.split("?", 1)[0].split("#", 1)[0]
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if not cleaned or cleaned.startswith("../") or "/" in cleaned:
        return None
    if cleaned in {".", ".."}:
        return None
    suffix = Path(cleaned).suffix.lower()
    if suffix and suffix not in MEDIA_SUFFIXES:
        return None
    if not suffix:
        return None
    return cleaned


def companion_image_pairs(from_note: str, to_note: str, text: str) -> list[tuple[str, str]]:
    """Build (src_rel, dst_rel) for images that should follow ``from_note`` → ``to_note``."""
    from_rel = _norm_rel(from_note)
    to_rel = _norm_rel(to_note)
    from_parent = _parent_rel(from_rel)
    to_parent = _parent_rel(to_rel)
    if from_parent == to_parent:
        return []

    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for ref in extract_relative_image_refs(text):
        name = _same_folder_basename(ref)
        if not name:
            continue
        src = f"{from_parent}/{name}" if from_parent else name
        dst = f"{to_parent}/{name}" if to_parent else name
        if src == dst or src in seen:
            continue
        seen.add(src)
        pairs.append((src, dst))
    return pairs


def move_companion_images(from_note: str, to_note: str) -> dict[str, Any]:
    """Move same-folder images referenced by the note to the new folder.

    The note must already exist at ``to_note``. Relative markdown links stay
    valid because basenames do not change.
    """
    to_rel = _norm_rel(to_note)
    from_rel = _norm_rel(from_note)
    if _parent_rel(from_rel) == _parent_rel(to_rel):
        return {"ok": True, "moved": [], "skipped": [], "reason": "same_folder"}

    try:
        target = vault_backend.resolve_vault_path(to_rel)
    except ValueError as exc:
        return {"ok": False, "moved": [], "skipped": [], "error": str(exc)}

    if not target.is_file():
        # Note may still be hydrating from S3.
        vault_sync.ensure_local_file(to_rel)
        try:
            target = vault_backend.resolve_vault_path(to_rel)
        except ValueError as exc:
            return {"ok": False, "moved": [], "skipped": [], "error": str(exc)}
    if not target.is_file():
        return {"ok": False, "moved": [], "skipped": [], "error": "note_missing"}

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return {"ok": False, "moved": [], "skipped": [], "error": str(exc)}

    pairs = companion_image_pairs(from_rel, to_rel, text)
    if not pairs:
        return {"ok": True, "moved": [], "skipped": [], "reason": "no_refs"}

    moved: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    s3_mode = vault_backend.backend_mode() == "s3"

    for src_rel, dst_rel in pairs:
        try:
            src = vault_backend.resolve_vault_path(src_rel)
        except ValueError:
            skipped.append({"from": src_rel, "to": dst_rel, "reason": "invalid_src"})
            continue
        if not src.is_file():
            found = vault_sync.ensure_local_file(src_rel)
            if found is None or not found.is_file():
                skipped.append({"from": src_rel, "to": dst_rel, "reason": "missing"})
                continue
            src = found

        try:
            dst = vault_backend.resolve_vault_path(dst_rel)
        except ValueError:
            skipped.append({"from": src_rel, "to": dst_rel, "reason": "invalid_dst"})
            continue
        if dst.exists():
            skipped.append({"from": src_rel, "to": dst_rel, "reason": "dest_exists"})
            continue

        try:
            if s3_mode:
                vault_sync.enqueue_delete(src_rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            if s3_mode:
                vault_sync.enqueue_put(dst_rel)
            moved.append({"from": src_rel, "to": dst_rel})
        except Exception as exc:
            logger.exception("Companion image move failed %s → %s", src_rel, dst_rel)
            skipped.append({"from": src_rel, "to": dst_rel, "reason": str(exc)})

    if s3_mode and moved:
        vault_sync.schedule_flush_pending()

    logger.info(
        "Companion images for %s → %s: moved=%d skipped=%d",
        from_rel,
        to_rel,
        len(moved),
        len(skipped),
    )
    return {"ok": True, "moved": moved, "skipped": skipped}


def parents_differ(from_path: str, to_path: str) -> bool:
    """True when renaming/moving changes the parent folder."""
    return _parent_rel(from_path) != _parent_rel(to_path)


def schedule_move_companion_images(from_note: str, to_note: str) -> bool:
    """Queue companion image moves on a daemon thread. Returns True if queued."""
    from_rel = _norm_rel(from_note)
    to_rel = _norm_rel(to_note)
    if not from_rel or not to_rel:
        return False
    if _parent_rel(from_rel) == _parent_rel(to_rel):
        return False
    if Path(to_rel).suffix.lower() not in {".md", ".markdown"}:
        return False

    user_id = vault_backend.current_user_id()
    if not user_id:
        logger.warning("Cannot schedule companion image move: no vault user")
        return False

    def worker() -> None:
        try:
            with vault_backend.user_scope(user_id):
                move_companion_images(from_rel, to_rel)
        except Exception:
            logger.exception(
                "Background companion image move failed %s → %s", from_rel, to_rel
            )

    threading.Thread(
        target=worker,
        name=f"vault-companion-images-{Path(to_rel).name}",
        daemon=True,
    ).start()
    return True
