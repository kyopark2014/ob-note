"""Documents staging helpers for ob-note.

Storage layout (parallel to vault, keeps OCR vault folder clean):
  data/{sanitize(user)}/documents/
    projects/   drawings/   out/   project_list.json   drawings_list.json
    settings.json   (mirror of FMP flags for sync_documents subprocess)
    artifacts/md/   (local markdown publish cache)

User-facing FMP settings also live at:
  {vault_root}/.vault/documents_settings.json

S3 staging uploads use prefix ``{user}/documents/...`` via
``vault_backend.s3_bucket_and_region()``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib import parse
from urllib.parse import quote

import boto3

from application import utils as app_utils

logger = logging.getLogger("documents_support")

_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DATA = _ROOT / "data"
DOCUMENTS_DIR_NAME = "documents"

S3_FILES_SESSION_PREFIX = "agentcore-sessions"
MAX_DOCUMENTS_DOC_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB

_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)


def sanitize_user_path_segment(user_id: str | None) -> str | None:
    return app_utils.sanitize_user_path_segment(user_id)


def documents_storage_base() -> Path:
    """Data root (``data/``). Per-user files live in ``{root}/{user}/documents``.

    ``DOCUMENTS_STORAGE_DIR`` is that root. A value ending in ``documents`` is
    the previous layout (``data/documents``) and is treated as the parent.
    """
    env = (os.environ.get("DOCUMENTS_STORAGE_DIR") or "").strip()
    if not env:
        base = _DEFAULT_DATA
    else:
        path = Path(env)
        base = path.parent if path.name == DOCUMENTS_DIR_NAME else path
    base.mkdir(parents=True, exist_ok=True)
    return base.resolve()


def _relocate_legacy_documents(segment: str, dest: Path) -> None:
    """Move ``data/documents/{user}/`` to ``data/{user}/documents/``."""
    legacy = documents_storage_base() / DOCUMENTS_DIR_NAME / segment
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
            logger.exception("Failed to relocate documents path %s", child)
    if moved:
        logger.info("Relocated %d documents entries from %s to %s", moved, legacy, dest)
    try:
        legacy.rmdir()
    except OSError:
        pass


def _s3_bucket_region() -> tuple[str | None, str]:
    from application import vault_backend

    return vault_backend.s3_bucket_and_region()


def _s3_bucket() -> str | None:
    bucket, _ = _s3_bucket_region()
    return bucket


def _s3_region() -> str:
    _, region = _s3_bucket_region()
    return region or "us-west-2"


def _sharing_url() -> str:
    return app_utils.sharing_url() or ""


def _project_name() -> str:
    return app_utils.project_name() or "ob-note"


def get_contents_type(file_name):
    lower = file_name.lower()
    if lower.endswith((".jpg", ".jpeg")):
        content_type = "image/jpeg"
    elif lower.endswith(".png"):
        content_type = "image/png"
    elif lower.endswith(".webp"):
        content_type = "image/webp"
    elif lower.endswith(".gif"):
        content_type = "image/gif"
    elif lower.endswith(".pdf"):
        content_type = "application/pdf"
    elif lower.endswith(".txt"):
        content_type = "text/plain"
    elif lower.endswith(".csv"):
        content_type = "text/csv"
    elif lower.endswith((".ppt", ".pptx")):
        content_type = "application/vnd.ms-powerpoint"
    elif lower.endswith((".doc", ".docx")):
        content_type = "application/msword"
    elif lower.endswith((".xls", ".xlsx")):
        content_type = "application/vnd.ms-excel"
    elif lower.endswith(".py"):
        content_type = "text/x-python"
    elif lower.endswith(".js"):
        content_type = "application/javascript"
    elif lower.endswith(".md"):
        content_type = "text/markdown"
    elif lower.endswith((".html", ".htm")):
        content_type = "text/html; charset=utf-8"
    else:
        content_type = "no info"
    return content_type



def _documents_settings_path(user_id: str | None) -> Path:
    """Canonical settings: vault ``.vault/documents_settings.json`` when vault is bound."""
    segment = sanitize_user_path_segment(user_id)
    if not segment:
        raise ValueError("Invalid user_id for documents settings")
    try:
        from application import vault_backend

        with vault_backend.user_scope(user_id):
            path = vault_backend.vault_root(user_id) / ".vault" / "documents_settings.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            return path
    except Exception:
        # Fallback when vault not available (CLI): documents staging settings.json
        return Path(get_user_documents_dir(user_id)) / "settings.json"


def _documents_settings_mirror_path(user_id: str | None) -> Path:
    return Path(get_user_documents_dir(user_id)) / "settings.json"


def load_user_settings(user_id: str | None) -> dict[str, object]:
    defaults: dict[str, object] = {
        "documents_foundation_model_parser_enabled": True,
        "documents_parallel_processing_enabled": True,
    }
    path = None
    try:
        path = _documents_settings_path(user_id)
    except Exception:
        path = None
    candidates = []
    if path is not None:
        candidates.append(path)
    try:
        candidates.append(_documents_settings_mirror_path(user_id))
    except Exception:
        pass
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            defaults.update(data)
            break
    return defaults


def save_user_settings(user_id: str | None, **updates: object) -> dict[str, object]:
    settings = load_user_settings(user_id)
    for key, value in updates.items():
        if value is not None:
            settings[key] = value
    payload = json.dumps(settings, ensure_ascii=False, indent=2) + "\n"
    # Write vault settings
    try:
        vault_path = _documents_settings_path(user_id)
        vault_path.write_text(payload, encoding="utf-8")
    except Exception:
        logger.debug("vault documents_settings write skipped", exc_info=True)
    # Mirror for sync_documents subprocess
    try:
        ensure_user_documents_dir(user_id)
        mirror = _documents_settings_mirror_path(user_id)
        mirror.write_text(payload, encoding="utf-8")
    except Exception:
        logger.exception("documents settings mirror write failed")
    return settings


def ensure_user_artifacts_dir(user_id: str | None) -> str:
    root = ensure_user_documents_dir(user_id)
    artifacts = os.path.join(root, "artifacts")
    os.makedirs(os.path.join(artifacts, "md"), exist_ok=True)
    return artifacts



def get_user_documents_dir(user_id: str | None) -> str:
    """Per-user Documents root: ``data/{user_id}/documents/``."""
    segment = sanitize_user_path_segment(user_id)
    if not segment:
        segment = "default"
    return str(documents_storage_base() / segment / DOCUMENTS_DIR_NAME)


def _ensure_documents_on_path() -> str:
    """Put ``ob-note/documents`` on ``sys.path`` so ``doc_list`` is importable."""
    docs_pkg = str(_ROOT / "documents")
    if docs_pkg not in sys.path:
        sys.path.insert(0, docs_pkg)
    return docs_pkg


def ensure_user_documents_dir(user_id: str | None) -> str:
    """Create ``{user}/documents``, ``projects/``, ``drawings/``, ``out/``, …."""
    segment = sanitize_user_path_segment(user_id)
    if not segment:
        raise ValueError(
            "Invalid user_id for documents path; expected a plain user id, "
            "not a signed session cookie"
        )
    docs_dir = get_user_documents_dir(user_id)
    _relocate_legacy_documents(segment, Path(docs_dir))
    for name in (
        "",
        "projects",
        "drawings",
        "out",
        os.path.join("out", "converted"),
        os.path.join("out", "converted", ".pdf_pages"),
    ):
        os.makedirs(os.path.join(docs_dir, name) if name else docs_dir, exist_ok=True)
    try:
        _ensure_documents_on_path()
        from doc_list import (
            DRAWINGS,
            PROJECTS,
            doc_list_path,
            empty_doc_list,
            save_doc_list,
            sync_doc_list_with_filesystem,
        )

        if not doc_list_path(docs_dir, PROJECTS).is_file():
            projects = os.path.join(docs_dir, "projects")
            has_projects = os.path.isdir(projects) and any(
                os.path.isfile(os.path.join(projects, n)) for n in os.listdir(projects)
            )
            if has_projects:
                sync_doc_list_with_filesystem(
                    docs_dir, user_id=segment, registry=PROJECTS
                )
            else:
                save_doc_list(
                    docs_dir, empty_doc_list(user_id=segment), registry=PROJECTS
                )
        if not doc_list_path(docs_dir, DRAWINGS).is_file():
            drawings = os.path.join(docs_dir, "drawings")
            has_drawings = os.path.isdir(drawings) and any(
                os.path.isfile(os.path.join(drawings, n)) for n in os.listdir(drawings)
            )
            if has_drawings:
                sync_doc_list_with_filesystem(
                    docs_dir, user_id=segment, registry=DRAWINGS
                )
            else:
                save_doc_list(
                    docs_dir, empty_doc_list(user_id=segment), registry=DRAWINGS
                )
    except Exception:
        logger.debug("documents doc_list ensure skipped", exc_info=True)
    logger.debug("user documents dir ready: %s", docs_dir)
    return docs_dir


def documents_converted_dir(user_id: str | None = None) -> str:
    return os.path.join(documents_out_dir(user_id), "converted")


def documents_out_dir(user_id: str | None = None) -> str:
    return os.path.join(get_user_documents_dir(user_id), "out")


def documents_projects_dir(user_id: str | None = None) -> str:
    return os.path.join(get_user_documents_dir(user_id), "projects")


def documents_project_list_path(user_id: str | None = None) -> str:
    return os.path.join(get_user_documents_dir(user_id), "project_list.json")


def documents_drawings_dir(user_id: str | None = None) -> str:
    return os.path.join(get_user_documents_dir(user_id), "drawings")


def documents_drawings_list_path(user_id: str | None = None) -> str:
    return os.path.join(get_user_documents_dir(user_id), "drawings_list.json")


def _documents_docs_dest_path(docs_dir: str, filename: str) -> tuple[str, str, str]:
    """Return ``(dest_path, sanitized_name, original_basename)``."""
    original = os.path.basename((filename or "").strip()) or "upload.bin"
    original = original.replace("\x00", "_") or "upload.bin"
    try:
        _ensure_documents_on_path()
        from doc_list import sanitize_documents_filename

        safe = sanitize_documents_filename(original)
    except Exception:
        safe = original.replace(" ", "_")
        safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in safe)
        while "__" in safe:
            safe = safe.replace("__", "_")
        stem, ext = os.path.splitext(safe)
        safe = f"{stem.strip('._-') or 'document'}{ext.lower()}"
    return os.path.join(docs_dir, safe), safe, original


def save_documents_project_upload(
    filename: str,
    data: bytes,
    *,
    user_id: str | None = None,
) -> dict[str, object]:
    """Sanitize filename, write into ``{user}/documents/projects``, update project_list."""
    if data is None or len(data) == 0:
        raise ValueError("저장할 파일이 없습니다.")

    root = ensure_user_documents_dir(user_id)
    projects = os.path.join(root, "projects")
    os.makedirs(projects, exist_ok=True)
    dest, safe_name, original_name = _documents_docs_dest_path(projects, filename)
    overwritten = os.path.isfile(dest)
    with open(dest, "wb") as f:
        f.write(data)

    segment = sanitize_user_path_segment(user_id) or "default"
    try:
        _ensure_documents_on_path()
        from doc_list import PROJECTS, upsert_document

        upsert_document(
            root,
            filename=safe_name,
            source_path=os.path.abspath(dest),
            bytes_size=len(data),
            status="uploaded",
            user_id=segment,
            extra={
                "original_filename": original_name,
                "sanitized": original_name != safe_name,
            },
            registry=PROJECTS,
        )
    except Exception:
        logger.exception("Failed to update documents project_list after upload")

    return {
        "documents_dir": root,
        "projects_dir": projects,
        "docs_dir": projects,
        "raw_dir": projects,
        "saved": {
            "name": safe_name,
            "original_filename": original_name,
            "sanitized": original_name != safe_name,
            "path": dest,
            "bytes": len(data),
            "overwritten": overwritten,
        },
        "count": 1,
        "project_list": documents_project_list_path(user_id),
    }


def save_documents_drawing_upload(
    filename: str,
    data: bytes,
    *,
    user_id: str | None = None,
) -> dict[str, object]:
    """Sanitize filename, write into ``{user}/documents/drawings``, update drawings_list."""
    if data is None or len(data) == 0:
        raise ValueError("저장할 파일이 없습니다.")

    root = ensure_user_documents_dir(user_id)
    drawings = os.path.join(root, "drawings")
    os.makedirs(drawings, exist_ok=True)
    dest, safe_name, original_name = _documents_docs_dest_path(drawings, filename)
    overwritten = os.path.isfile(dest)
    with open(dest, "wb") as f:
        f.write(data)

    segment = sanitize_user_path_segment(user_id) or "default"
    try:
        _ensure_documents_on_path()
        from doc_list import DRAWINGS, upsert_document

        upsert_document(
            root,
            filename=safe_name,
            source_path=os.path.abspath(dest),
            bytes_size=len(data),
            status="uploaded",
            user_id=segment,
            extra={
                "original_filename": original_name,
                "sanitized": original_name != safe_name,
            },
            registry=DRAWINGS,
        )
    except Exception:
        logger.exception("Failed to update documents drawings_list after upload")

    return {
        "documents_dir": root,
        "drawings_dir": drawings,
        "docs_dir": drawings,
        "raw_dir": drawings,
        "saved": {
            "name": safe_name,
            "original_filename": original_name,
            "sanitized": original_name != safe_name,
            "path": dest,
            "bytes": len(data),
            "overwritten": overwritten,
        },
        "count": 1,
        "drawings_list": documents_drawings_list_path(user_id),
    }


def list_documents_project_files(user_id: str | None = None) -> list[dict[str, object]]:
    projects = documents_projects_dir(user_id)
    if not os.path.isdir(projects):
        return []
    out: list[dict[str, object]] = []
    try:
        names = sorted(os.listdir(projects))
    except OSError:
        return []
    for name in names:
        path = os.path.join(projects, name)
        if not os.path.isfile(path):
            continue
        try:
            size = os.path.getsize(path)
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        out.append({"name": name, "path": path, "bytes": size, "mtime": mtime})
    return out


def list_documents_drawing_files(user_id: str | None = None) -> list[dict[str, object]]:
    drawings = documents_drawings_dir(user_id)
    if not os.path.isdir(drawings):
        return []
    out: list[dict[str, object]] = []
    try:
        names = sorted(os.listdir(drawings))
    except OSError:
        return []
    for name in names:
        path = os.path.join(drawings, name)
        if not os.path.isfile(path):
            continue
        try:
            size = os.path.getsize(path)
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        out.append({"name": name, "path": path, "bytes": size, "mtime": mtime})
    return out


def is_documents_foundation_model_parser_enabled(user_id: str | None) -> bool:
    return bool(
        load_user_settings(user_id).get(
            "documents_foundation_model_parser_enabled", True
        )
    )


def set_documents_foundation_model_parser_enabled(
    enabled: bool, *, user_id: str | None
) -> bool:
    settings = save_user_settings(
        user_id, documents_foundation_model_parser_enabled=bool(enabled)
    )
    return bool(settings.get("documents_foundation_model_parser_enabled", True))


def is_documents_parallel_processing_enabled(user_id: str | None) -> bool:
    return bool(
        load_user_settings(user_id).get("documents_parallel_processing_enabled", True)
    )


def set_documents_parallel_processing_enabled(
    enabled: bool, *, user_id: str | None
) -> bool:
    settings = save_user_settings(
        user_id, documents_parallel_processing_enabled=bool(enabled)
    )
    return bool(settings.get("documents_parallel_processing_enabled", True))

_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
)


@contextmanager
def _without_env_proxies():
    """Drop HTTP(S)_PROXY for the block (Cursor agent proxies break local boto3)."""
    saved = {key: os.environ.pop(key, None) for key in _PROXY_ENV_KEYS}
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is not None:
                os.environ[key] = value


def _s3_client_for_presign():
    """S3 client for browser-safe regional, virtual-hosted presigned URLs.

    Global ``*.s3.amazonaws.com`` hosts often 307-redirect to the region
    endpoint; browsers then fail the signed PUT (403/CORS) and our API never
    sees ``/complete``. Prefer virtual-hosted
    ``https://{bucket}.s3.{region}.amazonaws.com/...`` via SigV4 + regional
    endpoint so the browser PUT never follows a TemporaryRedirect.
    """
    from botocore.config import Config

    region = _s3_region() or "us-west-2"
    return boto3.client(
        service_name="s3",
        region_name=region,
        endpoint_url=f"https://s3.{region}.amazonaws.com",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "virtual"},
        ),
    )

def _session_upload_content_type(file_name: str) -> str:
    """Content-Type for session uploads; never returns ``no info``."""
    content_type = get_contents_type(file_name)
    if content_type == "no info":
        return "application/octet-stream"
    return content_type



def documents_projects_s3_key(file_name: str, user_id: str | None = None) -> str:
    """Build ``{user}/documents/projects/{file}`` staging key."""
    segment = sanitize_user_path_segment(user_id) or "default"
    safe_name = os.path.basename(file_name or "").strip() or "upload.bin"
    return f"{segment}/documents/projects/{safe_name}"


def generate_documents_projects_presigned_put(
    file_name: str,
    user_id: str | None = None,
    *,
    expires_in: int = 900,
) -> dict | None:
    if not _s3_bucket():
        logger.error("s3_bucket is not configured")
        return None

    original = os.path.basename(file_name or "").strip() or "upload.bin"
    try:
        _ensure_documents_on_path()
        from doc_list import sanitize_documents_filename

        safe_name = sanitize_documents_filename(original)
    except Exception:
        safe_name = original.replace(" ", "_")

    s3_key = documents_projects_s3_key(safe_name, user_id=user_id)
    content_type = _session_upload_content_type(safe_name)
    headers = {"Content-Type": content_type}
    params: dict = {
        "Bucket": _s3_bucket(),
        "Key": s3_key,
        "ContentType": content_type,
    }
    if content_type == "application/pdf":
        params["ContentDisposition"] = "inline"
        headers["Content-Disposition"] = "inline"

    try:
        with _without_env_proxies():
            s3_client = _s3_client_for_presign()
            upload_url = s3_client.generate_presigned_url(
                ClientMethod="put_object",
                Params=params,
                ExpiresIn=max(60, int(expires_in)),
                HttpMethod="PUT",
            )
        return {
            "file_name": safe_name,
            "original_filename": original,
            "sanitized": original != safe_name,
            "s3_key": s3_key,
            "content_type": content_type,
            "upload_url": upload_url,
            "headers": headers,
            "expires_in": max(60, int(expires_in)),
        }
    except Exception:
        logger.error(
            "Error generating documents projects presign: %s", traceback.format_exc()
        )
        return None


def materialize_documents_projects_from_s3(
    s3_key: str,
    file_name: str,
    user_id: str | None = None,
    *,
    original_filename: str | None = None,
) -> dict | None:
    if not _s3_bucket() or not s3_key:
        return None

    original = (
        os.path.basename(original_filename or file_name or "").strip()
        or "upload.bin"
    )
    try:
        _ensure_documents_on_path()
        from doc_list import PROJECTS, sanitize_documents_filename, upsert_document

        safe_name = sanitize_documents_filename(file_name or original)
    except Exception:
        safe_name = os.path.basename(file_name or original) or "upload.bin"
        upsert_document = None  # type: ignore[assignment]
        PROJECTS = None  # type: ignore[assignment]

    root = ensure_user_documents_dir(user_id)
    projects = os.path.join(root, "projects")
    os.makedirs(projects, exist_ok=True)
    dest_path = os.path.join(projects, safe_name)
    overwritten = os.path.isfile(dest_path)

    try:
        s3_client = boto3.client(service_name="s3", region_name=_s3_region())
        s3_client.download_file(_s3_bucket(), s3_key, dest_path)
        size = os.path.getsize(dest_path) if os.path.isfile(dest_path) else 0
        if size <= 0:
            logger.error("Documents project materialize empty: %s", dest_path)
            return None

        segment = sanitize_user_path_segment(user_id) or "default"
        if upsert_document is not None and PROJECTS is not None:
            try:
                upsert_document(
                    root,
                    filename=safe_name,
                    source_path=os.path.abspath(dest_path),
                    bytes_size=size,
                    status="uploaded",
                    user_id=segment,
                    extra={
                        "original_filename": original,
                        "sanitized": original != safe_name,
                        "s3_key": s3_key,
                    },
                    registry=PROJECTS,
                )
            except Exception:
                logger.exception("Failed to update documents project_list after materialize")

        return {
            "documents_dir": root,
            "projects_dir": projects,
            "docs_dir": projects,
            "raw_dir": projects,
            "saved": {
                "name": safe_name,
                "original_filename": original,
                "sanitized": original != safe_name,
                "path": dest_path,
                "bytes": size,
                "overwritten": overwritten,
            },
            "count": 1,
            "s3_key": s3_key,
            "project_list": documents_project_list_path(user_id),
            "content_type": _session_upload_content_type(safe_name),
            "content_length": size,
        }
    except Exception:
        logger.error(
            "Error materializing documents projects key=%s: %s",
            s3_key,
            traceback.format_exc(),
        )
        return None


def documents_drawings_s3_key(file_name: str, user_id: str | None = None) -> str:
    segment = sanitize_user_path_segment(user_id) or "default"
    safe_name = os.path.basename(file_name or "").strip() or "upload.bin"
    return f"{segment}/documents/drawings/{safe_name}"


def generate_documents_drawings_presigned_put(
    file_name: str,
    user_id: str | None = None,
    *,
    expires_in: int = 900,
) -> dict | None:
    if not _s3_bucket():
        logger.error("s3_bucket is not configured")
        return None

    original = os.path.basename(file_name or "").strip() or "upload.bin"
    try:
        _ensure_documents_on_path()
        from doc_list import sanitize_documents_filename

        safe_name = sanitize_documents_filename(original)
    except Exception:
        safe_name = original.replace(" ", "_")

    s3_key = documents_drawings_s3_key(safe_name, user_id=user_id)
    content_type = _session_upload_content_type(safe_name)
    headers = {"Content-Type": content_type}
    params: dict = {
        "Bucket": _s3_bucket(),
        "Key": s3_key,
        "ContentType": content_type,
    }
    if content_type == "application/pdf":
        params["ContentDisposition"] = "inline"
        headers["Content-Disposition"] = "inline"

    try:
        with _without_env_proxies():
            s3_client = _s3_client_for_presign()
            upload_url = s3_client.generate_presigned_url(
                ClientMethod="put_object",
                Params=params,
                ExpiresIn=max(60, int(expires_in)),
                HttpMethod="PUT",
            )
        return {
            "file_name": safe_name,
            "original_filename": original,
            "sanitized": original != safe_name,
            "s3_key": s3_key,
            "content_type": content_type,
            "upload_url": upload_url,
            "headers": headers,
            "expires_in": max(60, int(expires_in)),
        }
    except Exception:
        logger.error(
            "Error generating documents drawings presign: %s", traceback.format_exc()
        )
        return None


def materialize_documents_drawings_from_s3(
    s3_key: str,
    file_name: str,
    user_id: str | None = None,
    *,
    original_filename: str | None = None,
) -> dict | None:
    if not _s3_bucket() or not s3_key:
        return None

    original = (
        os.path.basename(original_filename or file_name or "").strip()
        or "upload.bin"
    )
    try:
        _ensure_documents_on_path()
        from doc_list import DRAWINGS, sanitize_documents_filename, upsert_document

        safe_name = sanitize_documents_filename(file_name or original)
    except Exception:
        safe_name = os.path.basename(file_name or original) or "upload.bin"
        upsert_document = None  # type: ignore[assignment]
        DRAWINGS = None  # type: ignore[assignment]

    root = ensure_user_documents_dir(user_id)
    drawings = os.path.join(root, "drawings")
    os.makedirs(drawings, exist_ok=True)
    dest_path = os.path.join(drawings, safe_name)
    overwritten = os.path.isfile(dest_path)

    try:
        s3_client = boto3.client(service_name="s3", region_name=_s3_region())
        s3_client.download_file(_s3_bucket(), s3_key, dest_path)
        size = os.path.getsize(dest_path) if os.path.isfile(dest_path) else 0
        if size <= 0:
            logger.error("Documents drawing materialize empty: %s", dest_path)
            return None

        segment = sanitize_user_path_segment(user_id) or "default"
        if upsert_document is not None and DRAWINGS is not None:
            try:
                upsert_document(
                    root,
                    filename=safe_name,
                    source_path=os.path.abspath(dest_path),
                    bytes_size=size,
                    status="uploaded",
                    user_id=segment,
                    extra={
                        "original_filename": original,
                        "sanitized": original != safe_name,
                        "s3_key": s3_key,
                    },
                    registry=DRAWINGS,
                )
            except Exception:
                logger.exception("Failed to update documents drawings_list after materialize")

        return {
            "documents_dir": root,
            "drawings_dir": drawings,
            "docs_dir": drawings,
            "raw_dir": drawings,
            "saved": {
                "name": safe_name,
                "original_filename": original,
                "sanitized": original != safe_name,
                "path": dest_path,
                "bytes": size,
                "overwritten": overwritten,
            },
            "count": 1,
            "s3_key": s3_key,
            "drawings_list": documents_drawings_list_path(user_id),
            "content_type": _session_upload_content_type(safe_name),
            "content_length": size,
        }
    except Exception:
        logger.error(
            "Error materializing documents drawings key=%s: %s",
            s3_key,
            traceback.format_exc(),
        )
        return None


def documents_project_pdf_public_url(
    file_name: str, user_id: str | None = None
) -> str | None:
    if not _sharing_url():
        return None
    safe_name = os.path.basename(file_name or "").strip()
    if not safe_name:
        return None
    segment = sanitize_user_path_segment(user_id) or "default"
    relative = (
        f"{parse.quote(segment)}/documents/projects/"
        f"{parse.quote(safe_name)}"
    )
    return f"{_sharing_url().rstrip('/')}/{relative}"


def documents_drawing_pdf_public_url(
    file_name: str, user_id: str | None = None
) -> str | None:
    if not _sharing_url():
        return None
    safe_name = os.path.basename(file_name or "").strip()
    if not safe_name:
        return None
    segment = sanitize_user_path_segment(user_id) or "default"
    relative = (
        f"{parse.quote(segment)}/documents/drawings/"
        f"{parse.quote(safe_name)}"
    )
    return f"{_sharing_url().rstrip('/')}/{relative}"


def documents_md_artifacts_s3_key(file_name: str, user_id: str | None = None) -> str:
    segment = sanitize_user_path_segment(user_id) or "default"
    safe_name = os.path.basename(file_name or "").strip() or "document.md"
    if not safe_name.lower().endswith(".md"):
        safe_name = f"{os.path.splitext(safe_name)[0]}.md"
    project = (_project_name() or "default").strip().strip("/") or "default"
    return f"artifacts/{project}/{segment}/md/{safe_name}"


def documents_md_runtime_workspace_s3_key(
    file_name: str, user_id: str | None = None
) -> str:
    segment = sanitize_user_path_segment(user_id) or "default"
    safe_name = os.path.basename(file_name or "").strip() or "document.md"
    if not safe_name.lower().endswith(".md"):
        safe_name = f"{os.path.splitext(safe_name)[0]}.md"
    return f"{S3_FILES_SESSION_PREFIX}/{segment}/artifacts/md/{safe_name}"


def documents_md_artifacts_public_url(
    file_name: str, user_id: str | None = None
) -> str | None:
    if not _sharing_url():
        return None
    key = documents_md_artifacts_s3_key(file_name, user_id=user_id)
    parts = [parse.quote(p) for p in key.split("/")]
    return f"{_sharing_url().rstrip('/')}/{'/'.join(parts)}"


def documents_md_local_artifacts_path(
    file_name: str, user_id: str | None = None
) -> str:
    artifacts = ensure_user_artifacts_dir(user_id)
    md_dir = os.path.join(artifacts, "md")
    os.makedirs(md_dir, exist_ok=True)
    safe_name = os.path.basename(file_name or "").strip() or "document.md"
    if not safe_name.lower().endswith(".md"):
        safe_name = f"{os.path.splitext(safe_name)[0]}.md"
    return os.path.join(md_dir, safe_name)


def _documents_head_s3_object_quiet(s3_key: str) -> dict | None:
    if not _s3_bucket() or not s3_key:
        return None
    try:
        s3_client = boto3.client(service_name="s3", region_name=_s3_region())
        response = s3_client.head_object(Bucket=_s3_bucket(), Key=s3_key)
        return {
            "content_length": int(response.get("ContentLength") or 0),
            "content_type": response.get("ContentType"),
        }
    except Exception:
        return None


def publish_documents_markdown_to_artifacts(
    md_path: str,
    user_id: str | None = None,
    *,
    file_name: str | None = None,
) -> dict | None:
    """Copy markdown to artifacts and upload to S3 for CloudFront + Runtime."""
    from pathlib import Path

    src = Path(md_path)
    if not src.is_file():
        logger.warning("Documents md publish skipped; missing file: %s", src)
        return None

    name = os.path.basename(file_name or src.name)
    if not name.lower().endswith(".md"):
        name = f"{os.path.splitext(name)[0]}.md"

    local_dest = documents_md_local_artifacts_path(name, user_id=user_id)
    try:
        src_stat = src.stat()
        if (
            os.path.isfile(local_dest)
            and os.path.getsize(local_dest) == src_stat.st_size
            and os.path.getmtime(local_dest) >= src_stat.st_mtime
            and _s3_bucket()
        ):
            s3_key = documents_md_artifacts_s3_key(name, user_id=user_id)
            runtime_key = documents_md_runtime_workspace_s3_key(name, user_id=user_id)
            public_url = documents_md_artifacts_public_url(name, user_id=user_id)
            head = _documents_head_s3_object_quiet(s3_key)
            runtime_head = _documents_head_s3_object_quiet(runtime_key)
            size_ok = (
                head and int(head.get("content_length") or 0) == src_stat.st_size
            )
            runtime_ok = (
                runtime_head
                and int(runtime_head.get("content_length") or 0) == src_stat.st_size
            )
            if size_ok and runtime_ok:
                return {
                    "file_name": name,
                    "local_path": local_dest,
                    "s3_key": s3_key,
                    "runtime_s3_key": runtime_key,
                    "url": public_url,
                    "uploaded": True,
                    "runtime_mirrored": True,
                    "skipped": True,
                    "bytes": src_stat.st_size,
                }
        if os.path.abspath(str(src)) != os.path.abspath(local_dest):
            import shutil

            shutil.copy2(src, local_dest)
    except Exception:
        logger.exception("Failed to copy documents md to local artifacts: %s", src)
        local_dest = str(src.resolve())

    s3_key = documents_md_artifacts_s3_key(name, user_id=user_id)
    runtime_key = documents_md_runtime_workspace_s3_key(name, user_id=user_id)
    public_url = documents_md_artifacts_public_url(name, user_id=user_id)
    result = {
        "file_name": name,
        "local_path": local_dest,
        "s3_key": s3_key,
        "runtime_s3_key": runtime_key,
        "url": public_url,
        "uploaded": False,
        "runtime_mirrored": False,
    }

    if not _s3_bucket():
        logger.warning("s3_bucket not configured; documents md kept local only")
        return result

    try:
        with _without_env_proxies():
            s3_client = boto3.client(service_name="s3", region_name=_s3_region())
            content_type = get_contents_type(name)
            if content_type == "no info":
                content_type = "text/markdown; charset=utf-8"
            with open(local_dest, "rb") as f:
                body = f.read()
            put_kwargs = {
                "Bucket": _s3_bucket(),
                "Body": body,
                "ContentType": content_type,
                "CacheControl": "no-cache, max-age=0, must-revalidate",
            }
            s3_client.put_object(Key=s3_key, **put_kwargs)
            result["uploaded"] = True
            result["bytes"] = len(body)
            try:
                s3_client.put_object(Key=runtime_key, **put_kwargs)
                result["runtime_mirrored"] = True
            except Exception:
                logger.exception(
                    "Documents md Runtime workspace mirror failed key=%s", runtime_key
                )
        return result
    except Exception:
        logger.error(
            "Error publishing documents md to artifacts: %s", traceback.format_exc()
        )
        return result


def head_documents_pdf_on_s3(
    file_name: str,
    user_id: str | None = None,
    *,
    kind: str = "project",
) -> bool:
    if kind == "drawing":
        key = documents_drawings_s3_key(file_name, user_id=user_id)
    else:
        key = documents_projects_s3_key(file_name, user_id=user_id)
    if not _s3_bucket() or not key:
        return False
    try:
        s3_client = boto3.client(service_name="s3", region_name=_s3_region())
        s3_client.head_object(Bucket=_s3_bucket(), Key=key)
        return True
    except Exception:
        return False


def documents_pdf_s3_key_for_kind(
    file_name: str,
    user_id: str | None = None,
    *,
    kind: str = "project",
) -> str | None:
    safe_name = os.path.basename(file_name or "").strip()
    if not safe_name:
        return None
    if kind == "drawing":
        return documents_drawings_s3_key(safe_name, user_id=user_id)
    return documents_projects_s3_key(safe_name, user_id=user_id)


def _documents_content_disposition(file_name: str, *, disposition: str = "attachment") -> str:
    raw = (file_name or "download").replace('"', "").replace("\r", "").replace("\n", "")
    ascii_name = raw.encode("ascii", "ignore").decode("ascii").strip(" .") or "download"
    ascii_name = re.sub(r"_+", "_", ascii_name).strip("._") or "download"
    _, ext = os.path.splitext(raw)
    if ext and not ascii_name.lower().endswith(ext.lower()):
        base = ascii_name if ascii_name != "download" else "download"
        ascii_name = f"{base}{ext}"
    return (
        f'{disposition}; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(raw)}"
    )


def stream_documents_pdf_from_s3(
    file_name: str,
    user_id: str | None = None,
    *,
    kind: str = "project",
):
    from fastapi.responses import StreamingResponse

    key = documents_pdf_s3_key_for_kind(file_name, user_id=user_id, kind=kind)
    if not _s3_bucket() or not key:
        return None
    safe_name = os.path.basename(file_name or "").strip() or "document.pdf"
    try:
        s3_client = boto3.client(service_name="s3", region_name=_s3_region())
        obj = s3_client.get_object(Bucket=_s3_bucket(), Key=key)
        body = obj["Body"]
        content_type = obj.get("ContentType") or "application/pdf"
        if content_type in ("binary/octet-stream", "no info", "application/octet-stream"):
            content_type = "application/pdf"
        return StreamingResponse(
            body.iter_chunks(chunk_size=1024 * 256),
            media_type=content_type,
            headers={
                "Content-Disposition": _documents_content_disposition(
                    safe_name, disposition="inline"
                ),
                "Cache-Control": "private, max-age=3600",
            },
        )
    except Exception:
        logger.error(
            "Error streaming documents pdf from S3 key=%s: %s",
            key,
            traceback.format_exc(),
        )
        return None


def enrich_documents_for_ui(
    documents: list[dict],
    user_id: str | None = None,
    *,
    publish_md: bool = True,
    kind: str = "project",
) -> list[dict]:
    """Attach pdf/md view URLs for Projects / Drawings UI."""
    if kind == "drawing":
        docs_root = documents_drawings_dir(user_id)
    else:
        docs_root = documents_projects_dir(user_id)
        kind = "project"
    kind_qs = f"?kind={kind}"

    enriched: list[dict] = []
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        item = dict(doc)
        filename = str(item.get("filename") or "").strip()
        md_file = str(item.get("md_file") or item.get("md_path") or "").strip()
        md_name = os.path.basename(md_file) if md_file else ""
        if not md_name and filename:
            stem = os.path.splitext(filename)[0]
            md_name = f"{stem}.md"

        pdf_name = filename if filename.lower().endswith(".pdf") else ""
        if not pdf_name and filename:
            src = str(item.get("source_path") or "")
            if src.lower().endswith(".pdf"):
                pdf_name = os.path.basename(src)

        local_md = str(item.get("md_path") or "").strip()
        if local_md and not os.path.isfile(local_md) and md_name:
            candidate = os.path.join(docs_root, md_name)
            if os.path.isfile(candidate):
                local_md = candidate
        elif not local_md and md_name:
            candidate = os.path.join(docs_root, md_name)
            if os.path.isfile(candidate):
                local_md = candidate

        local_pdf = ""
        if pdf_name:
            candidate = os.path.join(docs_root, pdf_name)
            if os.path.isfile(candidate):
                local_pdf = candidate
            else:
                src = str(item.get("source_path") or "")
                if src and os.path.isfile(src) and src.lower().endswith(".pdf"):
                    local_pdf = src

        if kind == "drawing":
            pdf_cf = (
                documents_drawing_pdf_public_url(pdf_name, user_id=user_id)
                if pdf_name
                else None
            )
        else:
            pdf_cf = (
                documents_project_pdf_public_url(pdf_name, user_id=user_id)
                if pdf_name
                else None
            )
        pdf_on_s3 = bool(
            pdf_name
            and head_documents_pdf_on_s3(pdf_name, user_id=user_id, kind=kind)
        )
        item["pdf_available"] = bool(local_pdf) or pdf_on_s3
        item["pdf_url"] = pdf_cf if pdf_on_s3 else None
        item["pdf_api_url"] = (
            f"/api/documents/documents/{parse.quote(pdf_name)}/pdf{kind_qs}"
            if pdf_name
            else None
        )

        md_url = None
        md_published = False
        if local_md and os.path.isfile(local_md) and publish_md:
            published = publish_documents_markdown_to_artifacts(
                local_md, user_id=user_id, file_name=md_name or None
            )
            if published:
                md_url = published.get("url")
                md_published = bool(published.get("uploaded"))
                item["md_s3_key"] = published.get("s3_key")
                item["md_local_artifacts"] = published.get("local_path")
        elif md_name:
            md_url = documents_md_artifacts_public_url(md_name, user_id=user_id)

        item["md_available"] = bool(local_md and os.path.isfile(local_md))
        item["md_url"] = md_url
        item["md_published"] = md_published
        if local_md and os.path.isfile(local_md):
            try:
                item["md_bytes"] = os.path.getsize(local_md)
            except OSError:
                item["md_bytes"] = None
        else:
            item["md_bytes"] = None
        item["md_viewer_url"] = (
            f"/api/documents/documents/{parse.quote(md_name)}/markdown{kind_qs}"
            if md_name
            else None
        )
        segment = sanitize_user_path_segment(user_id) or "default"
        if md_name:
            item["md_workspace_path"] = (
                f"/mnt/workspace/{segment}/artifacts/md/{md_name}"
            )
        item["display_name"] = (
            str(item.get("original_filename") or "").strip() or filename or md_name
        )
        item["kind"] = kind
        enriched.append(item)
    return enriched


def _documents_unlink_under_roots(path: str, *roots: str) -> bool:
    try:
        resolved = os.path.realpath(path)
    except OSError:
        return False
    allowed = False
    for root in roots:
        try:
            root_real = os.path.realpath(root)
        except OSError:
            continue
        if resolved == root_real or resolved.startswith(root_real + os.sep):
            allowed = True
            break
    if not allowed:
        return False
    try:
        if os.path.isfile(resolved):
            os.unlink(resolved)
            return True
    except OSError:
        return False
    return False


def _documents_delete_s3_key_quiet(s3_key: str | None) -> bool:
    if not _s3_bucket() or not s3_key:
        return False
    try:
        s3_client = boto3.client(service_name="s3", region_name=_s3_region())
        s3_client.delete_object(Bucket=_s3_bucket(), Key=s3_key)
        return True
    except Exception:
        return False



def head_session_upload_object(s3_key: str) -> dict | None:
    """HEAD an object; return ``{content_length, content_type}`` or None."""
    if not _s3_bucket() or not s3_key:
        return None
    try:
        with _without_env_proxies():
            s3_client = _s3_client_for_presign()
            response = s3_client.head_object(Bucket=_s3_bucket(), Key=s3_key)
        return {
            "content_length": int(response.get("ContentLength") or 0),
            "content_type": response.get("ContentType"),
        }
    except Exception:
        logger.error("Error head_object key=%s: %s", s3_key, traceback.format_exc())
        return None


def delete_documents_document(
    user_id: str | None,
    filename: str,
    *,
    kind: str = "project",
) -> dict:
    """Remove one Documents entry: local source + sidecars + list entry (+ S3)."""
    import shutil
    from pathlib import Path as _Path

    name = os.path.basename(filename or "").strip()
    if not name or name in {".", ".."}:
        raise ValueError("Invalid document name")

    kind_norm = (kind or "project").strip().lower()
    if kind_norm not in {"project", "drawing"}:
        raise ValueError(f"Unsupported kind: {kind}")

    _ensure_documents_on_path()
    from doc_list import DRAWINGS, PROJECTS, get_document, remove_document

    registry = {"project": PROJECTS, "drawing": DRAWINGS}[kind_norm]
    root = ensure_user_documents_dir(user_id)
    artifacts_root = ensure_user_artifacts_dir(user_id)
    docs_dir = {
        "project": documents_projects_dir(user_id),
        "drawing": documents_drawings_dir(user_id),
    }[kind_norm]

    entry = get_document(root, filename=name, registry=registry)
    if entry is None:
        stem = os.path.splitext(name)[0]
        entry = {
            "filename": name,
            "source_path": os.path.join(docs_dir, name),
            "md_path": os.path.join(docs_dir, f"{stem}.md"),
            "json_path": os.path.join(docs_dir, f"{stem}.json"),
        }
        exists = any(
            p and os.path.isfile(p)
            for p in (
                entry["source_path"],
                entry.get("md_path"),
                entry.get("json_path"),
            )
        )
        if not exists:
            raise FileNotFoundError(f"Document not found: {name}")

    stem = os.path.splitext(str(entry.get("filename") or name))[0] or os.path.splitext(
        name
    )[0]
    deleted_files: list[str] = []
    allow_roots = (root, artifacts_root, docs_dir)

    paths_to_delete: list[str] = []
    for key in ("source_path", "md_path", "json_path"):
        raw = str(entry.get(key) or "").strip()
        if raw:
            paths_to_delete.append(raw)

    for sibling in (f"{stem}.pdf", f"{stem}.md", f"{stem}.json", name):
        paths_to_delete.append(os.path.join(docs_dir, sibling))
    md_file = str(entry.get("md_file") or "").strip()
    if md_file:
        paths_to_delete.append(os.path.join(docs_dir, os.path.basename(md_file)))
        paths_to_delete.append(documents_md_local_artifacts_path(md_file, user_id=user_id))
    else:
        paths_to_delete.append(
            documents_md_local_artifacts_path(f"{stem}.md", user_id=user_id)
        )

    seen: set[str] = set()
    for path in paths_to_delete:
        try:
            resolved = str(_Path(path).expanduser().resolve())
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if _documents_unlink_under_roots(resolved, *allow_roots):
            deleted_files.append(resolved)

    pages_root = _Path(documents_converted_dir(user_id)) / ".pdf_pages"
    deleted_dirs: list[str] = []
    if pages_root.is_dir() and stem:
        for work in pages_root.iterdir():
            if not work.is_dir():
                continue
            if work.name == stem or work.name.startswith(f"{stem}_"):
                try:
                    shutil.rmtree(work)
                    deleted_dirs.append(str(work))
                except OSError:
                    logger.warning("Failed to remove pdf_pages dir: %s", work)

    s3_deleted: list[str] = []
    entry_s3 = str(entry.get("s3_key") or "").strip()
    if entry_s3 and _documents_delete_s3_key_quiet(entry_s3):
        s3_deleted.append(entry_s3)

    if kind_norm == "drawing":
        pdf_key = documents_drawings_s3_key(f"{stem}.pdf", user_id=user_id)
    else:
        pdf_key = documents_projects_s3_key(f"{stem}.pdf", user_id=user_id)
    if pdf_key and pdf_key not in s3_deleted and _documents_delete_s3_key_quiet(pdf_key):
        s3_deleted.append(pdf_key)

    md_key = documents_md_artifacts_s3_key(f"{stem}.md", user_id=user_id)
    if md_key not in s3_deleted and _documents_delete_s3_key_quiet(md_key):
        s3_deleted.append(md_key)

    removed = remove_document(root, filename=name, registry=registry)
    if not removed and entry.get("source_path"):
        removed = remove_document(
            root, source_path=str(entry.get("source_path")), registry=registry
        )

    if not removed and not deleted_files and not deleted_dirs:
        raise FileNotFoundError(f"Document not found: {name}")

    return {
        "ok": True,
        "filename": name,
        "kind": kind_norm,
        "removed_from_list": bool(removed),
        "deleted_files": deleted_files,
        "deleted_dirs": deleted_dirs,
        "s3_deleted": s3_deleted,
    }


def _sanitize_vault_md_stem(name: str) -> str:
    """Safe markdown stem for OCR/Projects|Drawings filenames."""
    raw = os.path.basename((name or "").strip()) or "document"
    stem, _ext = os.path.splitext(raw)
    stem = stem.strip() or "document"
    try:
        _ensure_documents_on_path()
        from doc_list import sanitize_documents_filename

        safe = sanitize_documents_filename(f"{stem}.md")
        return os.path.splitext(safe)[0] or "document"
    except Exception:
        cleaned = "".join(c if (c.isalnum() or c in "._- ") else "_" for c in stem)
        cleaned = cleaned.replace(" ", "_")
        while "__" in cleaned:
            cleaned = cleaned.replace("__", "_")
        return cleaned.strip("._-") or "document"


def resolve_documents_markdown_path(
    user_id: str | None,
    filename: str,
    *,
    kind: str = "project",
) -> Path | None:
    """Locate extracted ``.md`` next to the source (or under artifacts)."""
    name = os.path.basename((filename or "").strip())
    if not name:
        return None
    kind_norm = (kind or "project").strip().lower()
    if kind_norm == "drawing":
        bases = [documents_drawings_dir(user_id), documents_projects_dir(user_id)]
    else:
        bases = [documents_projects_dir(user_id), documents_drawings_dir(user_id)]

    stem = Path(name).stem
    md_name = name if name.lower().endswith(".md") else f"{stem}.md"

    # Prefer registry md_path when present.
    try:
        _ensure_documents_on_path()
        from doc_list import DRAWINGS, PROJECTS, get_document

        registry = DRAWINGS if kind_norm == "drawing" else PROJECTS
        root = get_user_documents_dir(user_id)
        entry = get_document(root, filename=name, registry=registry)
        if entry is None and name.lower().endswith(".md"):
            # Try matching by md filename via stem.pdf convention
            entry = get_document(root, filename=f"{stem}.pdf", registry=registry)
        if isinstance(entry, dict):
            md_path = str(entry.get("md_path") or "").strip()
            if md_path and os.path.isfile(md_path):
                return Path(md_path)
            md_file = str(entry.get("md_file") or "").strip()
            if md_file:
                md_name = os.path.basename(md_file)
    except Exception:
        logger.debug("documents registry md lookup skipped", exc_info=True)

    for base in bases:
        candidate = Path(base) / md_name
        if candidate.is_file():
            return candidate
    alt = Path(documents_md_local_artifacts_path(md_name, user_id=user_id))
    if alt.is_file():
        return alt
    return None


def unique_vault_rel_path(folder: str, stem: str) -> str:
    """Return ``folder/{stem}.md`` or ``folder/{stem}-N.md`` if collision."""
    from application import vault_backend

    base_name = f"{stem}.md"
    rel = f"{folder.rstrip('/')}/{base_name}"
    target = vault_backend.resolve_vault_path(rel)
    if not target.exists():
        return rel
    n = 2
    while True:
        candidate = f"{folder.rstrip('/')}/{stem}-{n}.md"
        if not vault_backend.resolve_vault_path(candidate).exists():
            return candidate
        n += 1
        if n > 9999:
            raise RuntimeError("Could not allocate unique vault markdown path")


def copy_documents_markdown_to_vault(
    user_id: str | None,
    filename: str,
    *,
    kind: str = "project",
) -> dict[str, Any]:
    """Copy extracted markdown into vault ``OCR/Projects`` or ``OCR/Drawings``.

    Raises:
        FileNotFoundError: source document or markdown missing (caller → 404/400)
        ValueError: invalid kind / empty content
    """
    from application import notes_db, vault_backend, vault_index

    kind_norm = (kind or "project").strip().lower()
    if kind_norm not in {"project", "drawing"}:
        raise ValueError("kind must be project or drawing")

    name = os.path.basename((filename or "").strip())
    if not name or name in {".", ".."}:
        raise ValueError("Invalid document name")

    md_path = resolve_documents_markdown_path(user_id, name, kind=kind_norm)
    if md_path is None or not md_path.is_file():
        raise FileNotFoundError(
            "추출된 Markdown이 없습니다. Documents Sync를 먼저 실행하세요."
        )

    try:
        content = md_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = md_path.read_text(encoding="utf-8", errors="replace")
    if not content.strip():
        raise ValueError("Markdown 내용이 비어 있습니다.")

    # Prefer display/original stem for the vault note name.
    display = ""
    try:
        _ensure_documents_on_path()
        from doc_list import DRAWINGS, PROJECTS, get_document

        registry = DRAWINGS if kind_norm == "drawing" else PROJECTS
        entry = get_document(
            get_user_documents_dir(user_id), filename=name, registry=registry
        )
        if isinstance(entry, dict):
            display = str(
                entry.get("original_filename")
                or entry.get("display_name")
                or entry.get("title")
                or ""
            ).strip()
    except Exception:
        pass
    stem_src = display or name
    stem = _sanitize_vault_md_stem(stem_src)

    folder = "OCR/Drawings" if kind_norm == "drawing" else "OCR/Projects"
    # Ensure OCR folders exist in the vault tree.
    for part in ("OCR", folder):
        folder_path = vault_backend.resolve_vault_path(part)
        folder_path.mkdir(parents=True, exist_ok=True)

    rel = unique_vault_rel_path(folder, stem)
    target = vault_backend.resolve_vault_path(rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    nbytes = target.stat().st_size

    note_row = None
    try:
        if notes_db.is_markdown_path(rel):
            note_row = notes_db.on_note_written(rel, content=content)
            vault_index.update_note(rel)
            vault_index.rebuild_index()
    except Exception:
        logger.exception("notes index update after documents vault copy failed")

    # Enqueue S3 put like writeFile.
    try:
        if vault_backend.backend_mode() == "s3":
            vault_backend.sync_to_s3(rel)
    except Exception:
        logger.exception("vault S3 enqueue after documents copy failed path=%s", rel)

    return {
        "ok": True,
        "path": rel,
        "kind": kind_norm,
        "bytes": nbytes,
        "source_md": str(md_path),
        "note_id": (note_row or {}).get("note_id") if note_row else None,
    }

