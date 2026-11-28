#!/usr/bin/env python3
"""HTTP client helpers for ob-note vault API (MCP package).

Auth order:
  1. VAULT_AGENT_TOKEN / Secrets Manager ``{project}/vault-agent-token``
     → ``Authorization: VaultAgent v1.<payload>.<sig>``
  2. SESSION_SIGNING_KEY (local / app ECS) → Bearer session cookie token
  3. Loopback + no key → unauthenticated (ob-note ALLOW_LOCAL_AUTH_BYPASS)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("use-vault-mcp")

COOKIE_NAME = "agent_user_id"
COOKIE_VERSION = "v1"
VAULT_AGENT_SCHEME = "VaultAgent"
VAULT_AGENT_VERSION = "v1"
DEFAULT_MAX_AGE_SECONDS = 60 * 60 * 24 * 30
VAULT_AGENT_MAX_AGE_SECONDS = 60 * 60


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_package_config() -> dict[str, Any]:
    """Sidecar config shipped next to this module."""
    here = Path(__file__).resolve().parent
    for path in (here / "config.json",):
        if path.is_file():
            return _load_json(path)
    return {}


def load_app_config() -> dict[str, Any]:
    env_json = (os.environ.get("APP_CONFIG_JSON") or "").strip()
    if env_json:
        try:
            data = json.loads(env_json)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    pkg = load_package_config()
    if pkg:
        return {
            "s3_bucket": pkg.get("s3_bucket"),
            "sharing_url": pkg.get("sharing_url") or pkg.get("ob_docs_url"),
            "region": pkg.get("region"),
            "projectName": pkg.get("project_name") or "ob-note",
        }

    candidates: list[Path] = []
    for key in ("OB_DOCS_ROOT", "WORKING_DIR", "APP_ROOT"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            candidates.append(Path(raw) / "config.json")
            candidates.append(Path(raw) / "application" / "config.json")

    here = Path(__file__).resolve()
    # MCP/use-vault → ob-note root is parents[2]
    try:
        ob_note_root = here.parents[2]
        candidates.append(ob_note_root / "config.json")
    except IndexError:
        pass
    candidates.extend(
        [
            Path.cwd() / "config.json",
            Path.cwd() / "application" / "config.json",
        ]
    )
    for path in candidates:
        if path.is_file():
            return _load_json(path)
    return {}


def _project_name(cfg: Optional[dict[str, Any]] = None) -> str:
    cfg = cfg if cfg is not None else load_app_config()
    pkg = load_package_config()
    return (
        (
            os.environ.get("PROJECT_NAME")
            or os.environ.get("SHARED_PROJECT_NAME")
            or pkg.get("project_name")
            or cfg.get("projectName")
            or "ob-note"
        )
        .strip()
        or "ob-note"
    )


def _region(cfg: Optional[dict[str, Any]] = None) -> str:
    cfg = cfg if cfg is not None else load_app_config()
    pkg = load_package_config()
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or pkg.get("region")
        or (cfg.get("region") or "us-west-2")
    )


def vault_base_url() -> str:
    explicit = (
        (os.environ.get("OB_DOCS_URL") or "").strip()
        or (os.environ.get("VAULT_API_URL") or "").strip()
    )
    if explicit:
        return explicit.rstrip("/")

    pkg = load_package_config()
    pkg_url = ((pkg.get("ob_docs_url") or pkg.get("sharing_url") or "").strip())
    if pkg_url:
        return pkg_url.rstrip("/")

    cfg = load_app_config()
    sharing = (
        (cfg.get("sharing_url") or "").strip()
        or (os.environ.get("SHARING_URL") or "").strip()
    )
    if sharing:
        return sharing.rstrip("/")

    return "http://127.0.0.1:8502"


# Default vault owner for MCP tools when actor_id is omitted or a nickname.
# Override with USE_VAULT_DEFAULT_ACTOR_ID (AgentCore env) if needed.
DEFAULT_ACTOR_ID = (
    (
        os.environ.get("USE_VAULT_DEFAULT_ACTOR_ID")
        or os.environ.get("DEFAULT_ACTOR_ID")
        or ""
    ).strip()
    or "kyopark2014@gmail.com"
)


def resolve_actor_id(actor_id: Optional[str] = None) -> str:
    """Resolve MCP ``actor_id`` to a vault account email.

    - Empty / omitted → ``DEFAULT_ACTOR_ID``
    - Nickname / non-email (no ``@``) → ``DEFAULT_ACTOR_ID`` (with warning)
    - Real email → as given
    """
    value = (actor_id or "").strip()
    if not value:
        return DEFAULT_ACTOR_ID
    if "@" not in value:
        logger.warning(
            "actor_id %r is not an email; using default %s",
            value,
            DEFAULT_ACTOR_ID,
        )
        return DEFAULT_ACTOR_ID
    return value


def resolve_user_id(cli_user_id: Optional[str] = None) -> str:
    for candidate in (
        cli_user_id,
        os.environ.get("USER_ID"),
        os.environ.get("CURRENT_USER_ID"),
        os.environ.get("AGENT_USER_ID"),
        os.environ.get("ACTOR_ID"),
    ):
        value = (candidate or "").strip()
        if value:
            return value
    return DEFAULT_ACTOR_ID


def normalize_vault_path(path: str) -> str:
    """Reject absolute paths and ``..`` segments; return vault-relative path."""
    raw = (path or "").strip().replace("\\", "/")
    if not raw:
        raise ValueError("path is required")
    if raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":"):
        raise ValueError(f"absolute paths are not allowed: {path}")
    parts: list[str] = []
    for seg in raw.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise ValueError(f"path traversal is not allowed: {path}")
        parts.append(seg)
    if not parts:
        raise ValueError(f"invalid vault path: {path}")
    return "/".join(parts)


def _session_max_age() -> int:
    raw = (os.environ.get("SESSION_MAX_AGE_SECONDS") or "").strip()
    if not raw:
        return DEFAULT_MAX_AGE_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_AGE_SECONDS
    return value if value > 0 else DEFAULT_MAX_AGE_SECONDS


def _get_secret_string(secret_id: str) -> tuple[Optional[str], Optional[str]]:
    try:
        import boto3
    except Exception as exc:
        return None, f"boto3 unavailable: {exc}"
    try:
        client = boto3.client("secretsmanager", region_name=_region())
        response = client.get_secret_value(SecretId=secret_id)
        secret = (response.get("SecretString") or "").strip()
        if secret:
            return secret, None
        return None, f"empty secret: {secret_id}"
    except Exception as exc:
        return None, f"{secret_id}: {exc}"


def _vault_agent_token() -> tuple[Optional[bytes], list[str]]:
    errors: list[str] = []
    env = (os.environ.get("VAULT_AGENT_TOKEN") or "").strip()
    if env:
        return env.encode("utf-8"), errors

    secret_id = f"{_project_name()}/vault-agent-token"
    value, err = _get_secret_string(secret_id)
    if value:
        return value.encode("utf-8"), errors
    if err:
        errors.append(err)
    return None, errors


def _session_signing_key() -> tuple[Optional[bytes], list[str]]:
    errors: list[str] = []
    env_key = (os.environ.get("SESSION_SIGNING_KEY") or "").strip()
    if env_key:
        return env_key.encode("utf-8"), errors

    secret_id = f"{_project_name()}/session-signing-key"
    value, err = _get_secret_string(secret_id)
    if value:
        return value.encode("utf-8"), errors
    if err:
        errors.append(err)

    for path in (
        Path.cwd() / "application" / "data" / ".session_signing_key",
        Path.cwd() / "data" / ".session_signing_key",
    ):
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text.encode("utf-8"), errors
    return None, errors


def _sign_vault_agent(user_id: str, token: bytes) -> str:
    exp = int(time.time()) + VAULT_AGENT_MAX_AGE_SECONDS
    payload = json.dumps(
        {"uid": user_id, "exp": exp}, separators=(",", ":"), ensure_ascii=False
    )
    payload_b64 = _b64encode(payload.encode("utf-8"))
    sig = hmac.new(token, payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{VAULT_AGENT_VERSION}.{payload_b64}.{_b64encode(sig)}"


def _sign_session(user_id: str, key: bytes) -> str:
    exp = int(time.time()) + _session_max_age()
    payload = json.dumps(
        {"uid": user_id, "exp": exp}, separators=(",", ":"), ensure_ascii=False
    )
    payload_b64 = _b64encode(payload.encode("utf-8"))
    sig = hmac.new(key, payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"{COOKIE_VERSION}.{payload_b64}.{_b64encode(sig)}"


def _is_loopback(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    return host in {"localhost", "127.0.0.1", "::1"}


def _auth_headers(user_id: str, *, require_auth: bool = True) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    errors: list[str] = []

    agent_token, agent_errors = _vault_agent_token()
    errors.extend(agent_errors)
    if agent_token:
        cred = _sign_vault_agent(user_id, agent_token)
        headers["Authorization"] = f"{VAULT_AGENT_SCHEME} {cred}"
        return headers

    session_key, session_errors = _session_signing_key()
    errors.extend(session_errors)
    if session_key:
        token = _sign_session(user_id, session_key)
        headers["Authorization"] = f"Bearer {token}"
        headers["X-Vault-Session"] = token
        headers["Cookie"] = f"{COOKIE_NAME}={token}"
        return headers

    if not require_auth or _is_loopback(vault_base_url()):
        return headers

    detail = "; ".join(errors) if errors else "no credentials resolved"
    raise RuntimeError(
        "Vault auth unavailable. Need VAULT_AGENT_TOKEN "
        f"(Secrets Manager `{_project_name()}/vault-agent-token`) "
        "for AgentCore, or SESSION_SIGNING_KEY for local/app. "
        f"Details: {detail}"
    )


def api_request(
    method: str,
    path: str,
    *,
    user_id: Optional[str] = None,
    query: Optional[dict[str, Any]] = None,
    body: Optional[dict[str, Any]] = None,
    timeout: float = 60.0,
) -> Any:
    uid = resolve_user_id(user_id)
    base = vault_base_url()
    url = f"{base}{path}"
    if query:
        filtered = {k: v for k, v in query.items() if v is not None and v != ""}
        if filtered:
            url = f"{url}?{urllib.parse.urlencode(filtered)}"

    require_auth = path.rstrip("/") != "/api/health"
    data = None
    headers = _auth_headers(uid, require_auth=require_auth)
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return {"ok": True, "status": resp.status}
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
        except Exception:
            parsed = detail
        raise RuntimeError(f"HTTP {exc.code} {method.upper()} {path}: {parsed}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to reach ob-note at {base}: {exc}") from exc


def api_request_bytes(
    method: str,
    path: str,
    *,
    user_id: Optional[str] = None,
    query: Optional[dict[str, Any]] = None,
    timeout: float = 60.0,
    max_bytes: int = 2 * 1024 * 1024,
) -> tuple[bytes, str]:
    """GET binary vault content (e.g. ``/api/files/raw``). Returns ``(body, content_type)``."""
    uid = resolve_user_id(user_id)
    base = vault_base_url()
    url = f"{base}{path}"
    if query:
        filtered = {k: v for k, v in query.items() if v is not None and v != ""}
        if filtered:
            url = f"{url}?{urllib.parse.urlencode(filtered)}"

    headers = _auth_headers(uid, require_auth=True)
    headers["Accept"] = "*/*"
    req = urllib.request.Request(url, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "application/octet-stream").split(";")[0].strip()
            chunks: list[bytes] = []
            total = 0
            while True:
                block = resp.read(64 * 1024)
                if not block:
                    break
                total += len(block)
                if total > max_bytes:
                    raise RuntimeError(f"Asset too large (max {max_bytes} bytes)")
                chunks.append(block)
            return b"".join(chunks), ctype
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
        except Exception:
            parsed = detail
        raise RuntimeError(f"HTTP {exc.code} {method.upper()} {path}: {parsed}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to reach ob-note at {base}: {exc}") from exc


def fetch_bytes(
    url: str,
    *,
    timeout: float = 60.0,
    max_bytes: int = 15 * 1024 * 1024,
) -> tuple[bytes, str]:
    """Download bytes from an http(s) URL. Returns ``(body, content_type)``."""
    cleaned = (url or "").strip()
    if not cleaned:
        raise ValueError("source_url is required")
    parsed = urllib.parse.urlparse(cleaned)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"unsupported URL scheme: {parsed.scheme or '(none)'}")

    req = urllib.request.Request(
        cleaned,
        headers={"Accept": "*/*", "User-Agent": "use-vault-mcp/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = (
                resp.headers.get("Content-Type") or "application/octet-stream"
            ).split(";")[0].strip()
            chunks: list[bytes] = []
            total = 0
            while True:
                block = resp.read(64 * 1024)
                if not block:
                    break
                total += len(block)
                if total > max_bytes:
                    raise RuntimeError(f"Download too large (max {max_bytes} bytes)")
                chunks.append(block)
            data = b"".join(chunks)
            if not data:
                raise RuntimeError("Empty download")
            return data, ctype
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} GET {cleaned}: {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to fetch {cleaned}: {exc}") from exc


def api_upload(
    vault_path: str,
    data: bytes,
    *,
    filename: Optional[str] = None,
    content_type: Optional[str] = None,
    user_id: Optional[str] = None,
    timeout: float = 120.0,
) -> Any:
    """POST multipart ``/api/files/upload`` (images / binary beside a note)."""
    import mimetypes

    uid = resolve_user_id(user_id)
    cleaned = normalize_vault_path(vault_path)
    if not data:
        raise ValueError("empty file")

    name = filename or Path(cleaned).name or "upload.bin"
    ctype = (
        (content_type or "").strip()
        or mimetypes.guess_type(name)[0]
        or "application/octet-stream"
    )
    boundary = f"----VaultUpload{int(time.time() * 1000)}{os.getpid()}"
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="path"\r\n\r\n'
            f"{cleaned}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode("utf-8")
        + data
        + f"\r\n--{boundary}--\r\n".encode("utf-8")
    )

    headers = _auth_headers(uid, require_auth=True)
    headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    url = f"{vault_base_url()}/api/files/upload"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {"ok": True, "path": cleaned}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
        except Exception:
            parsed = detail
        raise RuntimeError(f"HTTP {exc.code} POST /api/files/upload: {parsed}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to reach ob-note at {vault_base_url()}: {exc}") from exc


def to_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
