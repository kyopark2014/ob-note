"""Background vault-folder zip jobs with pollable progress.

The file panel starts a job and polls status the same way wiki sync does:
current file, file index, and percent while the archive is built and published.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from application import app_data_backend, vault_backend

logger = logging.getLogger("vault_compress")

MAX_COMPRESS_BYTES = 2 * 1024 * 1024 * 1024
MAX_COMPRESS_FILES = 20_000
COMPRESS_URL_EXPIRES = 3600

_lock = threading.Lock()
_catalog_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}
_catalog_cache: dict[str, list[dict[str, Any]]] = {}
CATALOG_NAME = "compress.json"
CATALOG_STATUSES = {"Processing", "Completed", "Expired", "Failed"}


def _now() -> float:
    return time.time()


def _progress(
    *,
    phase: str,
    message: str,
    file: str | None = None,
    file_i: int | None = None,
    file_n: int | None = None,
    pct: int | None = None,
) -> dict[str, Any]:
    return {
        "phase": phase,
        "file": file,
        "file_i": file_i,
        "file_n": file_n,
        "pct": pct,
    }


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "job_id": job.get("job_id"),
        "status": job.get("status") or "idle",
        "message": job.get("message"),
        "error": job.get("error"),
        "path": job.get("path"),
        "zip_name": job.get("zip_name"),
        "backup_path": job.get("backup_path"),
        "s3_key": job.get("s3_key"),
        "url": job.get("url"),
        "expires_in": job.get("expires_in"),
        "progress": job.get("progress"),
    }


def get_status(user_id: str) -> dict[str, Any]:
    with _lock:
        job = _jobs.get(user_id)
        if not job:
            return {
                "ok": True,
                "job_id": None,
                "status": "idle",
                "message": None,
                "error": None,
                "path": None,
                "zip_name": None,
                "backup_path": None,
                "s3_key": None,
                "url": None,
                "expires_in": None,
                "progress": None,
            }
        return _public_job(job)


def _update(user_id: str, job_id: str, *, persist: bool = False, **fields: Any) -> None:
    snapshot: dict[str, Any] | None = None
    with _lock:
        job = _jobs.get(user_id)
        if not job or job.get("job_id") != job_id:
            return
        job.update(fields)
        job["updated_at"] = _now()
        if persist:
            snapshot = dict(job)
    if snapshot:
        _upsert_from_job(snapshot)


def clean_vault_rel(rel: str) -> str:
    cleaned = (rel or "").replace("\\", "/").strip().strip("/")
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError("Folder path is required")
    parts = [part for part in cleaned.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise ValueError("Path traversal is not allowed")
    if ".vault" in parts:
        raise ValueError("Cannot compress .vault")
    return "/".join(parts)


def _has_user_files(path: Path) -> bool:
    if not path.is_dir():
        return False
    try:
        children = list(path.iterdir())
    except OSError:
        return False
    return any(child.name not in {".vault", ".git"} for child in children)


def resolve_entire_vault() -> Path:
    """Whole per-user vault. ``.vault`` is skipped later while collecting files.

    Prefer the vault the file tree shows. If that copy has no notes, use
    ``/mnt/app-data/{user}/vault/`` when that directory exists.
    """
    live = vault_backend.vault_root()
    if _has_user_files(live):
        return live
    segment = vault_backend.user_segment()
    mounted = Path(app_data_backend.mount_dir()) / segment / "vault"
    if mounted.is_dir():
        return mounted
    if live.is_dir():
        return live
    raise FileNotFoundError("Vault not found")


def resolve_compress_source(rel: str) -> Path:
    """Directory for the folder the user selected.

    The file tree reads the per-user vault. When that folder is absent, use
    ``/mnt/app-data/{user}/vault/`` on the S3 Files mount.
    """
    try:
        vault_path = vault_backend.resolve_vault_path(rel)
    except ValueError as e:
        raise ValueError(str(e)) from e
    if vault_path.is_dir():
        return vault_path
    if vault_path.exists():
        raise ValueError("Only folders can be compressed")

    segment = vault_backend.user_segment()
    base = (Path(app_data_backend.mount_dir()) / segment / "vault").resolve()
    mounted = (base / rel).resolve()
    if base != mounted and base not in mounted.parents:
        raise ValueError("Path escapes vault root")
    if mounted.is_dir():
        return mounted
    raise FileNotFoundError("Folder not found")


def zip_name_for(rel: str) -> str:
    name = Path(rel).name.strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("Invalid folder name")
    return f"{name}.zip"


def _attachment_disposition(filename: str) -> str:
    ascii_name = (
        filename.encode("ascii", "replace").decode("ascii").replace('"', "").replace("\\", "")
    )
    if not ascii_name or set(ascii_name) <= {"?"}:
        ascii_name = "download.zip"
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename)}'


def _backup_destination(segment: str, zip_name: str) -> tuple[Path, bool]:
    """``/mnt/app-data/backup/{user}/{folder}.zip`` when S3 Files is mounted."""
    mounted_dir = Path(app_data_backend.mount_dir()) / "backup" / segment
    if app_data_backend.mount_available():
        mounted_dir.mkdir(parents=True, exist_ok=True)
        return mounted_dir / zip_name, True
    local_dir = Path(__file__).resolve().parents[1] / "data" / "backup" / segment
    local_dir.mkdir(parents=True, exist_ok=True)
    return local_dir / zip_name, False


def _s3_backup_key(segment: str, zip_name: str) -> str:
    prefix = app_data_backend.S3_FILES_PREFIX
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return f"{prefix}backup/{segment}/{zip_name}"


def _backup_dir(segment: str) -> Path:
    mounted_dir = Path(app_data_backend.mount_dir()) / "backup" / segment
    if app_data_backend.mount_available():
        mounted_dir.mkdir(parents=True, exist_ok=True)
        return mounted_dir
    local_dir = Path(__file__).resolve().parents[1] / "data" / "backup" / segment
    local_dir.mkdir(parents=True, exist_ok=True)
    return local_dir


def _catalog_file(segment: str) -> Path:
    """Status file next to the zip archives: ``.../backup/{user}/compress.json``."""
    return _backup_dir(segment) / CATALOG_NAME


def _catalog_s3_key(segment: str) -> str:
    return _s3_backup_key(segment, CATALOG_NAME)


def _unique_zip_name(base_name: str, job_id: str) -> str:
    stem = base_name[:-4] if base_name.lower().endswith(".zip") else base_name
    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime())
    return f"{stem}-{stamp}-{job_id[:4]}.zip"


def _empty_catalog() -> dict[str, Any]:
    return {"items": []}


def _coerce_catalog(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return _empty_catalog()
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        return _empty_catalog()
    items = [item for item in raw_items if isinstance(item, dict) and item.get("id")]
    return {"items": items}


def _read_catalog_disk(segment: str) -> dict[str, Any]:
    path = _catalog_file(segment)
    try:
        return _coerce_catalog(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return _empty_catalog()
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read compress catalog %s: %s", path, exc)
        return _empty_catalog()


def _read_catalog_s3(segment: str) -> dict[str, Any]:
    bucket, _region = vault_backend.s3_bucket_and_region()
    if not bucket:
        return _empty_catalog()
    from application import documents_support

    try:
        with documents_support._without_env_proxies():
            client = documents_support._s3_client_for_presign()
            obj = client.get_object(Bucket=bucket, Key=_catalog_s3_key(segment))
            raw = obj["Body"].read()
        return _coerce_catalog(json.loads(raw.decode("utf-8")))
    except Exception as exc:
        logger.debug("Compress catalog not loaded from S3: %s", exc)
        return _empty_catalog()


def _merge_items(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for item in left + right:
        item_id = str(item.get("id") or "")
        if not item_id:
            continue
        prev = by_id.get(item_id)
        if prev is None or int(item.get("updated_at") or 0) >= int(prev.get("updated_at") or 0):
            by_id[item_id] = item
    items = list(by_id.values())
    items.sort(key=lambda item: int(item.get("created_at") or 0), reverse=True)
    return items[:100]


def _load_catalog_unlocked(segment: str) -> list[dict[str, Any]]:
    cached = _catalog_cache.get(segment)
    if cached is not None:
        return [dict(item) for item in cached]
    disk = _read_catalog_disk(segment)
    remote = _read_catalog_s3(segment)
    items = _merge_items(disk["items"], remote["items"])
    _catalog_cache[segment] = items
    return [dict(item) for item in items]


def _write_catalog_disk(segment: str, items: list[dict[str, Any]]) -> None:
    path = _catalog_file(segment)
    payload = json.dumps({"items": items}, ensure_ascii=False, indent=2)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.debug("Atomic replace failed for %s; writing in place", path)
        path.write_text(payload, encoding="utf-8")
        tmp.unlink(missing_ok=True)


def _write_catalog_s3(segment: str, items: list[dict[str, Any]]) -> None:
    bucket, _region = vault_backend.s3_bucket_and_region()
    if not bucket:
        return
    from application import documents_support

    body = json.dumps({"items": items}, ensure_ascii=False, indent=2).encode("utf-8")
    with documents_support._without_env_proxies():
        client = documents_support._s3_client_for_presign()
        client.put_object(
            Bucket=bucket,
            Key=_catalog_s3_key(segment),
            Body=body,
            ContentType="application/json",
        )


def _save_catalog_unlocked(segment: str, items: list[dict[str, Any]]) -> None:
    stored = _merge_items(items, [])
    _catalog_cache[segment] = [dict(item) for item in stored]
    _write_catalog_disk(segment, stored)
    try:
        _write_catalog_s3(segment, stored)
    except Exception:
        logger.exception("Failed to upload compress catalog for %s", segment)


def _display_status(item: dict[str, Any], now: float | None = None) -> str:
    status = str(item.get("status") or "")
    if status == "Completed":
        expires = item.get("expires_at")
        if expires and int(expires) <= int(now if now is not None else _now()):
            return "Expired"
    if status in CATALOG_STATUSES:
        return status
    return "Failed"


def _job_to_item(job: dict[str, Any]) -> dict[str, Any]:
    internal = job.get("status")
    if internal in {"queued", "running"}:
        status = "Processing"
    elif internal == "ready":
        status = "Completed"
    elif internal == "error":
        status = "Failed"
    else:
        status = "Processing"
    created = int(job.get("created_at") or _now())
    expires = job.get("expires_at")
    item = {
        "id": job.get("job_id"),
        "scope": job.get("scope") or "folder",
        "path": job.get("path") or "",
        "zip_name": job.get("zip_name"),
        "backup_path": job.get("backup_path"),
        "s3_key": job.get("s3_key"),
        "status": status,
        "created_at": created,
        "expires_at": int(expires) if expires else None,
        "url": job.get("url"),
        "message": job.get("message"),
        "error": job.get("error"),
        "size": job.get("size"),
        "updated_at": int(job.get("updated_at") or _now()),
    }
    if _display_status(item) == "Expired":
        item["status"] = "Expired"
        item["url"] = None
    return item


def _upsert_item(items: list[dict[str, Any]], item: dict[str, Any]) -> None:
    item_id = item.get("id")
    for index, prev in enumerate(items):
        if prev.get("id") != item_id:
            continue
        if int(item.get("updated_at") or 0) >= int(prev.get("updated_at") or 0):
            items[index] = item
        return
    items.insert(0, item)


def _upsert_from_job(job: dict[str, Any]) -> None:
    segment = str(job.get("segment") or "")
    item = _job_to_item(job)
    if not segment or not item.get("id"):
        return
    with _catalog_lock:
        items = _load_catalog_unlocked(segment)
        _upsert_item(items, item)
        _save_catalog_unlocked(segment, items)


def _live_job(user_id: str) -> dict[str, Any] | None:
    with _lock:
        job = _jobs.get(user_id)
        if not job or job.get("status") not in {"queued", "running"}:
            return None
        return dict(job)


def _public_item(item: dict[str, Any], *, progress: dict[str, Any] | None = None) -> dict[str, Any]:
    status = _display_status(item)
    url = item.get("url") if status == "Completed" else None
    return {
        "id": item.get("id"),
        "scope": item.get("scope") or "folder",
        "path": item.get("path") or "",
        "zip_name": item.get("zip_name"),
        "backup_path": item.get("backup_path"),
        "s3_key": item.get("s3_key"),
        "status": status,
        "created_at": item.get("created_at"),
        "expires_at": item.get("expires_at"),
        "url": url,
        "message": item.get("message"),
        "error": item.get("error"),
        "size": item.get("size"),
        "progress": progress,
    }


def list_items(user_id: str) -> dict[str, Any]:
    """Compress history from ``compress.json``, with live progress overlaid."""
    segment = vault_backend.user_segment(user_id)
    live = _live_job(user_id)
    live_id = live.get("job_id") if live else None
    now = int(_now())
    changed = False
    with _catalog_lock:
        items = _load_catalog_unlocked(segment)
        for item in items:
            if item.get("status") == "Processing" and item.get("id") != live_id:
                item["status"] = "Failed"
                item["error"] = item.get("error") or "서버에서 압축이 끝나기 전에 중단되었습니다."
                item["message"] = item["error"]
                item["updated_at"] = now
                changed = True
            elif item.get("status") == "Completed" and _display_status(item, now) == "Expired":
                item["status"] = "Expired"
                item["url"] = None
                item["updated_at"] = now
                changed = True
        if changed:
            _save_catalog_unlocked(segment, items)
    public: list[dict[str, Any]] = []
    for item in items:
        progress = None
        message = item.get("message")
        if live and item.get("id") == live_id:
            progress = live.get("progress")
            message = live.get("message") or message
            item = {**item, "message": message}
        public.append(_public_item(item, progress=progress))
    return {"ok": True, "items": public}


def _zip_path_for_delete(segment: str, backup_path: str | None) -> Path | None:
    if not backup_path:
        return None
    root = _catalog_file(segment).parent.resolve()
    try:
        resolved = Path(backup_path).resolve()
    except OSError:
        return None
    if resolved.suffix.lower() != ".zip" or resolved.parent != root:
        return None
    return resolved


def _s3_key_for_delete(segment: str, key: str | None) -> str | None:
    if not key or not key.endswith(".zip"):
        return None
    expected = _s3_backup_key(segment, "")
    if not key.startswith(expected):
        return None
    return key


def delete_item(user_id: str, item_id: str) -> dict[str, Any]:
    segment = vault_backend.user_segment(user_id)
    cleaned = (item_id or "").strip()
    if not cleaned:
        raise ValueError("Compress id is required")
    live = _live_job(user_id)
    if live and live.get("job_id") == cleaned:
        raise RuntimeError("압축이 진행 중이라 삭제할 수 없습니다.")
    removed: dict[str, Any] | None = None
    with _catalog_lock:
        items = _load_catalog_unlocked(segment)
        kept: list[dict[str, Any]] = []
        for item in items:
            if item.get("id") == cleaned and removed is None:
                removed = item
                continue
            kept.append(item)
        if removed is None:
            raise FileNotFoundError("Compress item not found")
        _save_catalog_unlocked(segment, kept)
    zip_path = _zip_path_for_delete(segment, removed.get("backup_path"))
    if zip_path is not None:
        zip_path.unlink(missing_ok=True)
    key = _s3_key_for_delete(segment, removed.get("s3_key"))
    if key:
        bucket, _region = vault_backend.s3_bucket_and_region()
        if bucket:
            from application import documents_support

            try:
                with documents_support._without_env_proxies():
                    client = documents_support._s3_client_for_presign()
                    client.delete_object(Bucket=bucket, Key=key)
            except Exception:
                logger.exception("Failed to delete compress object %s", key)
    return {"ok": True, "id": cleaned}


def refresh_download(user_id: str, item_id: str) -> dict[str, Any]:
    """Mint a new one-hour presigned URL for an existing archive."""
    segment = vault_backend.user_segment(user_id)
    cleaned = (item_id or "").strip()
    if not cleaned:
        raise ValueError("Compress id is required")
    live = _live_job(user_id)
    if live and live.get("job_id") == cleaned:
        raise RuntimeError("압축이 진행 중입니다.")
    with _catalog_lock:
        items = _load_catalog_unlocked(segment)
        item = next((entry for entry in items if entry.get("id") == cleaned), None)
        if item is None:
            raise FileNotFoundError("Compress item not found")
        if item.get("status") == "Processing":
            raise RuntimeError("압축이 진행 중입니다.")
        zip_path = _zip_path_for_delete(segment, item.get("backup_path"))
        key = _s3_key_for_delete(segment, item.get("s3_key"))
        zip_name = str(item.get("zip_name") or "archive.zip")
    if zip_path is None or not zip_path.is_file() or not key:
        raise FileNotFoundError("Archive file not found")
    url, expires = _presign_backup_zip(
        zip_path,
        key,
        on_mount=app_data_backend.mount_available(),
    )
    now = int(_now())
    with _catalog_lock:
        items = _load_catalog_unlocked(segment)
        item = next((entry for entry in items if entry.get("id") == cleaned), None)
        if item is None:
            raise FileNotFoundError("Compress item not found")
        item["status"] = "Completed"
        item["url"] = url
        item["expires_at"] = now + int(expires)
        item["message"] = f"{zip_name} 다운로드 링크를 다시 만들었습니다."
        item["error"] = None
        item["updated_at"] = now
        _save_catalog_unlocked(segment, items)
        public = _public_item(item)
    return {"ok": True, "item": public}


def _collect_entries(src: Path) -> list[tuple[Path | None, str]]:
    root = src.resolve()
    folder_name = src.name
    entries: list[tuple[Path | None, str]] = []
    file_count = 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        dirnames[:] = [name for name in dirnames if name not in {".git", ".vault"}]
        current = Path(dirpath)
        try:
            current_resolved = current.resolve()
        except OSError:
            continue
        if root != current_resolved and root not in current_resolved.parents:
            continue
        rel_dir = current_resolved.relative_to(root).as_posix()
        if not filenames and not dirnames:
            arc_dir = folder_name if rel_dir == "." else f"{folder_name}/{rel_dir}"
            entries.append((None, arc_dir.rstrip("/") + "/"))
        for name in filenames:
            path = current / name
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if root != resolved and root not in resolved.parents:
                continue
            if not resolved.is_file():
                continue
            try:
                total += resolved.stat().st_size
            except OSError:
                continue
            file_count += 1
            if file_count > MAX_COMPRESS_FILES or total > MAX_COMPRESS_BYTES:
                raise RuntimeError("Folder is too large to compress")
            try:
                rel_file = resolved.relative_to(root).as_posix()
            except ValueError:
                continue
            entries.append((resolved, f"{folder_name}/{rel_file}"))
    if not entries:
        entries.append((None, f"{folder_name}/"))
    return entries


def _write_folder_zip(
    src: Path,
    dest: Path,
    entries: list[tuple[Path | None, str]],
    on_file: Callable[[int, int, str], None] | None = None,
) -> int:
    file_total = sum(1 for path, _arc in entries if path is not None)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(prefix="vault-zip-", suffix=".zip")
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)
    try:
        written_files = 0
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            wrote = False
            for path, arc in entries:
                if path is None:
                    archive.writestr(arc, "")
                    wrote = True
                    continue
                written_files += 1
                if on_file:
                    on_file(written_files, file_total, path.name)
                archive.write(path, arc)
                wrote = True
            if not wrote:
                archive.writestr(f"{src.name}/", "")
        shutil.copyfile(tmp_path, dest)
        fd = os.open(dest, os.O_RDONLY)
        try:
            try:
                os.fsync(fd)
            except OSError:
                logger.debug("fsync skipped for %s", dest)
        finally:
            os.close(fd)
        return dest.stat().st_size
    finally:
        tmp_path.unlink(missing_ok=True)


def _presign_backup_zip(
    local_file: Path,
    key: str,
    *,
    on_mount: bool,
    on_upload: Callable[[int], None] | None = None,
) -> tuple[str, int]:
    bucket, _region = vault_backend.s3_bucket_and_region()
    if not bucket:
        raise RuntimeError("S3 bucket is not configured")
    from application import documents_support

    disposition = _attachment_disposition(local_file.name)
    try:
        with documents_support._without_env_proxies():
            client = documents_support._s3_client_for_presign()
            visible = False
            if on_mount:
                for _ in range(6):
                    try:
                        client.head_object(Bucket=bucket, Key=key)
                        visible = True
                        break
                    except Exception:
                        time.sleep(0.4)
            if not visible:
                extra: dict[str, Any] = {"ContentType": "application/zip"}
                kwargs: dict[str, Any] = {"ExtraArgs": extra}
                if on_upload:
                    kwargs["Callback"] = on_upload
                client.upload_file(str(local_file), bucket, key, **kwargs)
            url = client.generate_presigned_url(
                ClientMethod="get_object",
                Params={
                    "Bucket": bucket,
                    "Key": key,
                    "ResponseContentType": "application/zip",
                    "ResponseContentDisposition": disposition,
                },
                ExpiresIn=COMPRESS_URL_EXPIRES,
                HttpMethod="GET",
            )
    except Exception as exc:
        logger.exception("Failed to publish backup zip %s", key)
        raise RuntimeError("Could not create a download link for the archive") from exc
    if not url:
        raise RuntimeError("Could not create a download link")
    return url, COMPRESS_URL_EXPIRES


def _run_job(
    user_id: str,
    job_id: str,
    *,
    cleaned: str,
    src: Path,
    dest: Path,
    on_mount: bool,
    key: str,
    zip_name: str,
) -> None:
    try:
        _update(
            user_id,
            job_id,
            status="running",
            message="파일 목록을 확인하는 중…",
            progress=_progress(phase="scan", message="파일 목록을 확인하는 중…"),
            persist=True,
        )
        entries = _collect_entries(src)
        file_total = sum(1 for path, _arc in entries if path is not None)

        def on_file(index: int, total: int, name: str) -> None:
            _update(
                user_id,
                job_id,
                message=f"압축 중… {name}",
                progress=_progress(
                    phase="zip",
                    message=f"압축 중… {name}",
                    file=name,
                    file_i=index,
                    file_n=total,
                ),
            )

        _update(
            user_id,
            job_id,
            message="압축을 시작하는 중…",
            progress=_progress(
                phase="zip",
                message="압축을 시작하는 중…",
                file=None,
                file_i=0,
                file_n=file_total,
            ),
        )
        size = _write_folder_zip(src, dest, entries, on_file=on_file)
        _update(
            user_id,
            job_id,
            message="다운로드 링크를 만드는 중…",
            progress=_progress(
                phase="upload",
                message="다운로드 링크를 만드는 중…",
                file=zip_name,
                file_i=file_total,
                file_n=file_total,
            ),
        )
        url, expires = _presign_backup_zip(
            dest,
            key,
            on_mount=on_mount,
        )
        logger.info(
            "Compressed vault folder %s -> %s (%s bytes, key=%s)",
            cleaned,
            dest,
            size,
            key,
        )
        _update(
            user_id,
            job_id,
            status="ready",
            error=None,
            message=f"{zip_name} 압축이 완료되었습니다.",
            url=url,
            expires_in=expires,
            expires_at=int(_now()) + int(expires),
            size=size,
            backup_path=str(dest),
            s3_key=key,
            progress=_progress(
                phase="done",
                message=f"{zip_name} 압축이 완료되었습니다.",
                file=zip_name,
                file_i=file_total,
                file_n=file_total,
            ),
            persist=True,
        )
    except Exception as exc:
        logger.exception("Compress failed for %s", cleaned)
        _update(
            user_id,
            job_id,
            status="error",
            error=str(exc),
            message=str(exc),
            progress=_progress(phase="error", message=str(exc)),
            persist=True,
        )


def start_compress(user_id: str, rel: str = "", *, entire_vault: bool = False) -> dict[str, Any]:
    """Validate the folder, then zip it on a background thread.

    ``entire_vault`` zips ``/mnt/app-data/{user}/vault/`` (or the live vault
    copy) and skips the ``.vault`` settings folder.
    """
    segment = vault_backend.user_segment(user_id)
    if entire_vault:
        src = resolve_entire_vault()
        base_name = f"{segment}.zip"
        cleaned = ""
        scope = "vault"
        queued_message = "vault 전체 압축을 백그라운드에서 시작합니다."
    else:
        cleaned = clean_vault_rel(rel)
        src = resolve_compress_source(cleaned)
        base_name = zip_name_for(cleaned)
        scope = "folder"
        queued_message = "압축을 백그라운드에서 시작합니다."
    job_id = uuid.uuid4().hex
    zip_name = _unique_zip_name(base_name, job_id)
    dest, on_mount = _backup_destination(segment, zip_name)
    try:
        dest_resolved = dest.resolve()
        src_resolved = src.resolve()
    except OSError as e:
        raise ValueError("Invalid folder") from e
    if dest_resolved == src_resolved or src_resolved in dest_resolved.parents:
        raise ValueError("Refusing to write the archive inside the source folder")
    key = _s3_backup_key(segment, zip_name)

    created_at = int(_now())
    with _lock:
        current = _jobs.get(user_id)
        if current and current.get("status") in {"queued", "running"}:
            return _public_job(current)
        job = {
            "job_id": job_id,
            "segment": segment,
            "scope": scope,
            "status": "queued",
            "error": None,
            "message": queued_message,
            "path": cleaned,
            "zip_name": zip_name,
            "backup_path": str(dest),
            "s3_key": key,
            "url": None,
            "expires_in": None,
            "expires_at": None,
            "size": None,
            "created_at": created_at,
            "progress": _progress(
                phase="scan",
                message=queued_message,
            ),
            "updated_at": created_at,
        }
        _jobs[user_id] = job
        snapshot = _public_job(job)
        stored = dict(job)

    _upsert_from_job(stored)
    threading.Thread(
        target=_run_job,
        args=(user_id, job_id),
        kwargs={
            "cleaned": cleaned,
            "src": src,
            "dest": dest,
            "on_mount": on_mount,
            "key": key,
            "zip_name": zip_name,
        },
        name=f"vault-compress-{job_id[:8]}",
        daemon=True,
    ).start()
    return snapshot
