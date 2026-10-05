"""Persist per-user notes.db via S3 Files mount (/mnt/app-data).

Working copy stays under ``data/{user}/vault/.vault/notes.db`` (local disk).
On ECS, durable copy lives at ``/mnt/app-data/{user}/notes.db``.

Same working + persist pattern as agentic-work ``task_store_persistence`` —
never open SQLite directly on the NFS mount.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import threading
import time
from typing import Optional

from application import app_data_backend as backend
from application import vault_backend

logger = logging.getLogger("vault_db_persistence")

_PERSIST_DEBOUNCE_SECONDS = 20.0

_persist_lock = threading.Lock()
_persist_timer: threading.Timer | None = None
_dirty_users: set[str] = set()
_restored_users: set[str] = set()


def persistence_enabled() -> bool:
    return backend.backend_mode() == "mount"


def _user_segment(user_id: Optional[str] = None) -> str:
    return vault_backend.user_segment(user_id)


def working_notes_db_path(user_id: Optional[str] = None) -> str:
    if user_id:
        with vault_backend.user_scope(user_id):
            settings = vault_backend.settings_dir()
            settings.mkdir(parents=True, exist_ok=True)
            return str(settings / "notes.db")
    settings = vault_backend.settings_dir()
    settings.mkdir(parents=True, exist_ok=True)
    return str(settings / "notes.db")


def durable_notes_db_path(user_id: Optional[str] = None) -> str:
    return str(backend.durable_user_notes_db_path(_user_segment(user_id)))

def _db_ready(path: str) -> bool:
    return os.path.isfile(path) and os.path.getsize(path) > 0


def _copy_db_files(source: str, destination: str) -> None:
    """Copy DB bytes only (no metadata/xattrs).

    S3 Files / NFS rejects os.setxattr with Errno 524; shutil.copy2 would fail
    after a successful content copy. Use shutil.copy instead.
    """
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    shutil.copy(source, destination)
    for suffix in ("-wal", "-shm"):
        src = source + suffix
        dst = destination + suffix
        if os.path.isfile(src):
            shutil.copy(src, dst)
        elif os.path.isfile(dst):
            os.remove(dst)


def _checkpoint_sqlite(db_path: str) -> None:
    if not os.path.isfile(db_path):
        return
    try:
        conn = sqlite3.connect(db_path, timeout=5)
    except sqlite3.Error as exc:
        logger.warning("Failed to open DB for checkpoint %s: %s", db_path, exc)
        return
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    finally:
        conn.close()


def _remove_db_files(path: str) -> None:
    for candidate in (path, path + "-wal", path + "-shm"):
        try:
            if os.path.isfile(candidate):
                os.remove(candidate)
        except OSError as exc:
            logger.warning("Could not remove %s: %s", candidate, exc)


def restore_user_db(
    user_id: Optional[str] = None,
    *,
    retries: int = 1,
    delay_sec: float = 0.4,
    force: bool = False,
) -> bool:
    """Copy durable S3 Files notes.db → working vault .vault/notes.db.

    ``force=True`` (login) always re-reads the mount, ignoring the in-process
    cache — same as toons-viewer ``load_user_db_from_s3_files``.
    """
    if not persistence_enabled():
        return False

    uid = user_id or vault_backend.current_user_id()
    if not uid:
        return False

    segment = _user_segment(uid)
    if not force and segment in _restored_users:
        return True

    working = working_notes_db_path(uid)
    durable = durable_notes_db_path(uid)
    attempts = max(1, int(retries))

    for attempt in range(attempts):
        if _db_ready(durable):
            os.makedirs(os.path.dirname(working), exist_ok=True)
            _remove_db_files(working)
            _copy_db_files(durable, working)
            _restored_users.add(segment)
            logger.info(
                "Restored notes.db from S3 Files: %s -> %s", durable, working
            )
            return True
        if attempt + 1 < attempts:
            logger.info(
                "Durable notes.db not ready yet (%s), retry %d/%d",
                durable,
                attempt + 1,
                attempts,
            )
            time.sleep(max(0.0, delay_sec))

    logger.info("No durable notes.db at %s (using local working copy)", durable)
    # Remember the attempt so subsequent non-force requests do not re-poll.
    _restored_users.add(segment)
    return False


def load_user_db_from_s3_files(user_id: str) -> bool:
    """Force-refresh working notes.db from S3 Files mount (login path)."""
    return restore_user_db(user_id, retries=5, delay_sec=0.5, force=True)


def ensure_user_notes_db(user_id: str, *, load_from_durable: bool = False) -> bool:
    """Ensure per-user notes.db exists; on login pull from S3 Files first.

    Mirrors toons-viewer ``ensure_user_db(load_from_durable=…)``:
    - login: always refresh working copy from ``/mnt/app-data/{user}/notes.db``
    - if durable existed → skip immediate persist (avoid racing empty over good DB)
    - if no durable yet → flush working copy to seed the mount
    """
    uid = (user_id or "").strip()
    if not uid:
        return False

    with vault_backend.user_scope(uid):
        vault_backend.ensure_user_vault(uid)
        # Open/create schema on the working path after optional restore.
        from application import notes_db

        restored = False
        if load_from_durable:
            restored = load_user_db_from_s3_files(uid)
        else:
            restore_user_db(uid, retries=1, force=False)

        notes_db.ensure_db()

        if not persistence_enabled():
            return restored

        if load_from_durable:
            if restored:
                logger.info(
                    "Using S3 Files notes.db for %s (skip immediate persist)", uid
                )
            else:
                # First login / no durable yet — seed the mount right away.
                flush_persist(uid)
                logger.info("Seeded S3 Files notes.db for %s (first login)", uid)
        return restored


def ensure_restored(user_id: Optional[str] = None) -> None:
    """Idempotent restore for authenticated API requests (not a full login pull)."""
    try:
        uid = user_id or vault_backend.current_user_id()
        if not uid:
            return
        ensure_user_notes_db(uid, load_from_durable=False)
    except Exception:
        logger.exception("notes.db restore failed for %s", user_id)


def _persist_user(user_id: str) -> None:
    if not persistence_enabled():
        return
    working = working_notes_db_path(user_id)
    durable = durable_notes_db_path(user_id)
    if not _db_ready(working):
        logger.warning("Working notes.db missing, skip persist: %s", working)
        return

    # Guard: never replace a populated durable DB with a tiny fresh working copy.
    if _db_ready(durable):
        try:
            w_size = os.path.getsize(working)
            d_size = os.path.getsize(durable)
            if d_size > max(w_size * 2, w_size + 4096) and w_size < 16_384:
                logger.warning(
                    "Skip persist for %s: durable (%d B) looks more complete "
                    "than working (%d B)",
                    user_id,
                    d_size,
                    w_size,
                )
                return
        except OSError:
            pass

    _checkpoint_sqlite(working)
    _copy_db_files(working, durable)
    logger.info("Persisted notes.db: %s -> %s", working, durable)


def persist_notes_db(*, user_id: Optional[str] = None, force: bool = False) -> None:
    with _persist_lock:
        if user_id is not None:
            users = (user_id,)
        else:
            users = tuple(_dirty_users)

        for uid in users:
            try:
                _persist_user(uid)
            except Exception:
                logger.exception("Failed to persist notes.db for %s", uid)
            _dirty_users.discard(uid)


def _start_persist_timer_locked() -> None:
    global _persist_timer

    def _run() -> None:
        persist_notes_db(force=True)

    if _persist_timer is not None:
        _persist_timer.cancel()
    _persist_timer = threading.Timer(_PERSIST_DEBOUNCE_SECONDS, _run)
    _persist_timer.daemon = True
    _persist_timer.start()


def schedule_persist(user_id: Optional[str] = None) -> None:
    """Debounced persist after notes.db mutations."""
    if not persistence_enabled():
        return
    uid = user_id or vault_backend.current_user_id()
    if not uid:
        return
    with _persist_lock:
        _dirty_users.add(uid)
        _start_persist_timer_locked()


def flush_persist(user_id: Optional[str] = None) -> None:
    """Cancel pending debounce and persist immediately."""
    global _persist_timer

    if not persistence_enabled():
        return

    with _persist_lock:
        if _persist_timer is not None:
            _persist_timer.cancel()
            _persist_timer = None

    if user_id is not None:
        persist_notes_db(force=True, user_id=user_id)
        with _persist_lock:
            if _dirty_users:
                _start_persist_timer_locked()
        return

    persist_notes_db(force=True)
