"""Vault storage backend: S3 Files mount, optional S3 API sync, or local disk.

Local working copy: ``data/{user}/vault/``. Opt-in S3 sync with VAULT_S3_ENABLE=1.

Per-user isolation:
  local working copy: data/{sanitize(user_id)}/vault/
  S3 notes:           {sanitize(user_id)}/vault/…
  public share index: data/vault/_public/  and  s3 vault/_public/
"""

from __future__ import annotations

import contextvars
import logging
import os
import shutil
import unicodedata
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import boto3

from application import utils

logger = logging.getLogger("vault_backend")

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DATA = _ROOT / "data"
_DEFAULT_MOUNT = Path("/mnt/vault")
# Notes live at s3://{bucket}/{user}/vault/ — not under a leading vault/ prefix.
S3_USER_VAULT_DIR = "vault"
S3_PUBLIC_PREFIX = "vault/_public/"
PUBLIC_SEGMENT = "_public"

_sync_lock = threading.RLock()
_last_sync_at: dict[str, float] = {}
_SYNC_INTERVAL_SECONDS = 60.0

_user_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "ob_note_vault_user", default=None
)


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def current_user_id() -> Optional[str]:
    return _user_id_var.get()


def set_current_user_id(user_id: Optional[str]) -> contextvars.Token:
    return _user_id_var.set(user_id)


def reset_current_user_id(token: contextvars.Token) -> None:
    _user_id_var.reset(token)


@contextmanager
def user_scope(user_id: Optional[str]) -> Iterator[None]:
    """Bind vault paths to ``user_id`` for the duration of the block."""
    token = set_current_user_id(user_id)
    try:
        yield
    finally:
        reset_current_user_id(token)


def user_segment(user_id: Optional[str] = None) -> str:
    """Sanitize email/user id into a single path segment (required)."""
    uid = user_id if user_id is not None else current_user_id()
    segment = utils.sanitize_user_path_segment(uid)
    if not segment:
        raise RuntimeError("vault user_id is required for per-user storage")
    return segment


def mount_dir() -> Path:
    raw = (os.environ.get("VAULT_MOUNT") or "").strip()
    if raw:
        return Path(raw)
    cfg = utils.load_config()
    cfg_path = (cfg.get("s3_files_vault_mount_path") or "").strip()
    return Path(cfg_path) if cfg_path else _DEFAULT_MOUNT


def data_dir() -> Path:
    """Root for ``{user}/vault`` and ``vault/_public``.

    ``VAULT_DIR`` is that root (``data``). A value ending in ``vault`` is the
    previous layout (``data/vault``) and is treated as the parent data root.
    """
    raw = (os.environ.get("VAULT_DIR") or "").strip()
    if not raw:
        return _DEFAULT_DATA
    path = Path(raw)
    if path.name == "vault":
        return path.parent
    return path


def local_dir() -> Path:
    """Data root. Per-user notes live in ``{data}/{user}/vault``."""
    return data_dir()


def vault_base() -> Path:
    """Data root that holds ``{user}/vault``."""
    root = data_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def user_vault_dir(user_id: Optional[str] = None) -> Path:
    """``data/{user}/vault`` (or the mount equivalent)."""
    segment = user_segment(user_id)
    if mount_available():
        return mount_dir() / segment / S3_USER_VAULT_DIR
    return data_dir() / segment / S3_USER_VAULT_DIR


def mount_available() -> bool:
    """True only for a real S3 Files mount (or VAULT_USE_MOUNT=1).

    Dockerfile may create an empty /mnt/vault directory; that alone must not
    switch the backend into mount mode or the seeded local vault is skipped.
    """
    if _env_flag("VAULT_USE_MOUNT"):
        path = mount_dir()
        return path.is_dir() and os.access(path, os.W_OK)
    path = mount_dir()
    if not path.is_dir() or not os.access(path, os.W_OK):
        return False
    try:
        return os.path.ismount(str(path))
    except OSError:
        return False


def s3_bucket_and_region() -> tuple[Optional[str], str]:
    cfg = utils.load_config()
    bucket = (
        (os.environ.get("VAULT_S3_BUCKET") or "").strip()
        or (cfg.get("s3_bucket") or "").strip()
        or None
    )
    region = (
        (cfg.get("region") or os.environ.get("AWS_REGION") or "us-west-2").strip()
    )
    return bucket, region


def s3_prefix(user_id: Optional[str] = None) -> str:
    """Per-user S3 prefix: ``{user}/vault/``."""
    return f"{user_segment(user_id)}/{S3_USER_VAULT_DIR}/"


def s3_public_prefix() -> str:
    """Global share index: ``vault/_public/`` (not inside a user vault)."""
    prefix = S3_PUBLIC_PREFIX.strip()
    return prefix if prefix.endswith("/") else prefix + "/"


def public_dir() -> Path:
    path = data_dir() / S3_USER_VAULT_DIR / PUBLIC_SEGMENT
    path.mkdir(parents=True, exist_ok=True)
    return path


def s3_available() -> bool:
    if mount_available():
        return False
    bucket, _ = s3_bucket_and_region()
    if not bucket:
        return False
    if _env_flag("VAULT_S3_DISABLE"):
        return False
    return _env_flag("VAULT_S3_ENABLE")


def backend_mode() -> str:
    if mount_available():
        return "mount"
    if s3_available():
        return "s3"
    return "local"


def vault_root(user_id: Optional[str] = None) -> Path:
    """Active per-user vault root for file I/O: ``data/{user}/vault``."""
    root = user_vault_dir(user_id)
    root.mkdir(parents=True, exist_ok=True)
    settings = root / ".vault"
    settings.mkdir(parents=True, exist_ok=True)
    (settings / "cache").mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _nfc_rel(rel: str) -> str:
    """Vault-relative path with composed Hangul (NFC)."""
    return unicodedata.normalize("NFC", (rel or "").replace("\\", "/").lstrip("/"))


def resolve_vault_path(rel: str) -> Path:
    """Resolve a vault-relative path; reject traversal outside vault.

    New files are addressed as NFC. macOS uploads often arrive as NFD.
    """
    root = vault_root()
    cleaned = _nfc_rel(rel)
    if ".." in cleaned.split("/"):
        raise ValueError("Path traversal is not allowed")
    target = (root / cleaned).resolve()
    if root != target and root not in target.parents:
        raise ValueError("Path escapes vault root")
    return target


def retire_nfd_duplicate(rel: str) -> Optional[str]:
    """Remove a decomposed-Hangul file once the NFC spelling has been written.

    Returns the vault-relative path that was removed, if any.
    """
    nfc_rel = _nfc_rel(rel)
    nfd_rel = unicodedata.normalize("NFD", nfc_rel)
    if not nfd_rel or nfd_rel == nfc_rel or ".." in nfd_rel.split("/"):
        return None
    root = vault_root()
    try:
        nfc_path = resolve_vault_path(nfc_rel)
    except ValueError:
        return None
    if not nfc_path.is_file():
        return None
    nfd_path = (root / nfd_rel).resolve()
    if root != nfd_path and root not in nfd_path.parents:
        return None
    if not nfd_path.is_file() or nfd_path == nfc_path:
        return None
    nfd_path.unlink()
    return nfd_rel


def find_vault_file(rel: str) -> Optional[Path]:
    """Resolve an existing file; fall back to unique basename match (Obsidian-style)."""
    try:
        target = resolve_vault_path(rel)
    except ValueError:
        return None
    if target.is_file():
        return target
    # Files saved before NFC normalization may still use decomposed Hangul.
    nfd_rel = unicodedata.normalize("NFD", _nfc_rel(rel))
    if nfd_rel and nfd_rel != _nfc_rel(rel):
        root = vault_root()
        if ".." not in nfd_rel.split("/"):
            nfd_target = (root / nfd_rel).resolve()
            if (root == nfd_target or root in nfd_target.parents) and nfd_target.is_file():
                return nfd_target
    name = unicodedata.normalize("NFC", Path((rel or "").replace("\\", "/")).name)
    if not name or name in {".", ".."}:
        return None
    root = vault_root()
    matches: list[Path] = []
    spellings = [name]
    nfd_name = unicodedata.normalize("NFD", name)
    if nfd_name != name:
        spellings.append(nfd_name)
    for spelling in spellings:
        for path in root.rglob(spelling):
            if not path.is_file():
                continue
            if ".vault" in path.parts:
                continue
            matches.append(path)
        if matches:
            break
    if not matches:
        return None
    matches.sort(key=lambda p: (len(p.relative_to(root).parts), str(p).lower()))
    return matches[0]


def settings_dir() -> Path:
    return vault_root() / ".vault"


def cache_dir() -> Path:
    path = settings_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _s3_client(region: str):
    from botocore.config import Config

    # Keep vault UI responsive when S3/proxy is slow or stuck.
    return boto3.client(
        "s3",
        region_name=region,
        config=Config(
            connect_timeout=5,
            read_timeout=20,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )


def _sync_key() -> str:
    try:
        return user_segment()
    except RuntimeError:
        return "_none"


def sync_from_s3(*, force: bool = False) -> dict:
    """Download ``{user}/vault/`` objects into the local working copy (s3 mode only).

    Delegates to vault_sync: never pulls while pending local→S3 ops remain.
    Prefer incremental unless ``force=True``.
    """
    from application import vault_sync

    # Always try to finish outbound ops first so a pull cannot clobber them.
    flush = vault_sync.flush_pending_to_s3()
    if not flush.get("ok") and flush.get("remaining"):
        return {
            "ok": False,
            "reason": "pending_uploads",
            "flush": flush,
            "skipped": True,
        }
    key = _sync_key()
    if not force:
        now = time.time()
        with _sync_lock:
            last = _last_sync_at.get(key, 0.0)
            if (now - last) < _SYNC_INTERVAL_SECONDS and last > 0:
                return {
                    "ok": True,
                    "skipped": True,
                    "flush": flush,
                    "last_sync_at": last,
                }
    return vault_sync.sync_from_s3_incremental(force=force)


def try_sync_from_s3(*, force: bool = False) -> dict:
    """Best-effort sync for interactive routes (tree). Never raises."""
    try:
        return sync_from_s3(force=force)
    except Exception as e:
        logger.exception("try_sync_from_s3 failed")
        return {"ok": False, "skipped": True, "reason": str(e)}


def mark_synced() -> None:
    """Record successful sync timestamp for the current user."""
    with _sync_lock:
        _last_sync_at[_sync_key()] = time.time()


def last_sync_at() -> float:
    with _sync_lock:
        return _last_sync_at.get(_sync_key(), 0.0)


def sync_to_s3(rel_path: Optional[str] = None) -> dict:
    """Queue local vault file(s) for S3 upload; flush runs in the background.

    Callers (mkdir/write/upload) return as soon as the local write + pending
    queue entry exist so the UI can refresh without waiting on S3.
    """
    from application import vault_sync

    if backend_mode() != "s3":
        return {"ok": False, "reason": f"backend={backend_mode()}"}
    if rel_path:
        vault_sync.enqueue_put(rel_path)
    else:
        vault_sync.enqueue_put_tree("")
    return vault_sync.schedule_flush_pending()


def ensure_user_vault(user_id: Optional[str] = None) -> Path:
    """Ensure per-user vault dirs exist (empty until the user adds notes)."""
    uid = user_id if user_id is not None else current_user_id()
    with user_scope(uid):
        segment = user_segment()
        root = user_vault_dir()
        if not mount_available():
            try:
                _relocate_legacy_local_vault(segment, root)
            except Exception:
                logger.exception("Legacy local vault relocate failed")
        # Adopt legacy flat vault before vault_root() creates an empty .vault
        # that would block moving the old settings directory.
        if segment == "local-dev" and not mount_available():
            try:
                _maybe_adopt_legacy_flat_vault(root)
            except Exception:
                logger.exception("Legacy vault adopt failed")
        return vault_root()


def _relocate_legacy_local_vault(segment: str, dest: Path) -> None:
    """Move ``data/vault/{user}/`` to ``data/{user}/vault/`` when the new dir is empty."""
    legacy = data_dir() / S3_USER_VAULT_DIR / segment
    try:
        if not legacy.is_dir() or legacy.resolve() == dest.resolve():
            return
    except OSError:
        return
    dest.mkdir(parents=True, exist_ok=True)
    moved = 0
    for child in list(legacy.iterdir()):
        target = dest / child.name
        if target.exists():
            continue
        try:
            shutil.move(str(child), str(target))
            moved += 1
        except OSError:
            logger.exception("Failed to relocate %s", child)
    if moved:
        logger.info("Relocated %d entries from %s to %s", moved, legacy, dest)
    try:
        legacy.rmdir()
    except OSError:
        pass


def _maybe_adopt_legacy_flat_vault(root: Path) -> None:
    """Move pre-multi-tenant notes from data/vault into local-dev (once)."""
    base = data_dir() / S3_USER_VAULT_DIR
    if root.resolve() == base.resolve():
        return
    marker = base / ".vault_migrated_to_users"
    if marker.is_file():
        return
    root.mkdir(parents=True, exist_ok=True)
    moved = 0
    for child in list(base.iterdir()):
        name = child.name
        if name in {PUBLIC_SEGMENT, ".DS_Store", ".vault_migrated_to_users"}:
            continue
        if name.startswith("_"):
            continue
        if "@" in name:
            continue
        if child.resolve() == root.resolve():
            continue
        dest = root / name
        if dest.exists():
            continue
        try:
            shutil.move(str(child), str(dest))
            moved += 1
        except OSError:
            logger.exception("Failed to adopt legacy path %s", child)
    if moved:
        try:
            marker.write_text("ok\n", encoding="utf-8")
        except OSError:
            pass
        logger.info("Adopted %d legacy vault entries into %s", moved, root)


def ensure_seed_vault() -> None:
    """Compatibility: ensure vault base + public dir exist (no global user seed)."""
    vault_base()
    public_dir()
