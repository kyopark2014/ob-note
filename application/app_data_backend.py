"""ECS app-data mount helpers (S3 Files → /mnt/app-data).

Locally the mount is absent; vault markdown still uses S3 API sync.
Only a real NFS/S3 Files mount (or APP_DATA_USE_MOUNT=1) enables persistence.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("app_data_backend")

_DEFAULT_MOUNT = "/mnt/app-data"
# Empty: /mnt/app-data maps to s3://{bucket}/.
S3_FILES_PREFIX = ""


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _load_config() -> dict:
    try:
        from application import utils

        cfg = utils.load_config()
        return cfg if isinstance(cfg, dict) else {}
    except Exception as e:
        logger.debug("Could not load config for app data backend: %s", e)
        return {}


def mount_dir() -> str:
    env = (os.environ.get("APP_DATA_MOUNT") or os.environ.get("TASK_DB_MOUNT") or "").strip()
    if env:
        return env
    cfg = _load_config()
    cfg_path = (cfg.get("s3_files_app_data_mount_path") or "").strip()
    return cfg_path or _DEFAULT_MOUNT


def mount_available() -> bool:
    """True only for a real S3 Files mount (or APP_DATA_USE_MOUNT=1).

    Dockerfile may create an empty /mnt/app-data; that alone must not enable
    mount mode.
    """
    path = mount_dir()
    if _env_flag("APP_DATA_USE_MOUNT"):
        return os.path.isdir(path) and os.access(path, os.W_OK)
    if not os.path.isdir(path) or not os.access(path, os.W_OK):
        return False
    try:
        return os.path.ismount(path)
    except OSError:
        return False


def project_name() -> str:
    env_name = (os.environ.get("TASK_DB_PROJECT") or os.environ.get("PROJECT_NAME") or "").strip()
    if env_name:
        return env_name
    cfg = _load_config()
    name = cfg.get("projectName")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return "ob-note"


def backend_mode() -> str:
    """Return 'mount' | 'local'."""
    if mount_available():
        return "mount"
    return "local"


def durable_user_notes_db_path(user_segment: str) -> Path:
    """Persistent notes.db on the S3 Files mount.

    Layout (bucket-root S3 Files mount):
      /mnt/app-data/{user}/notes.db
      → s3://{bucket}/{user}/notes.db
    """
    segment = (user_segment or "").strip()
    if not segment or "/" in segment or "\\" in segment or ".." in segment:
        raise ValueError(f"Invalid user segment: {user_segment!r}")
    return Path(mount_dir()) / segment / "notes.db"
