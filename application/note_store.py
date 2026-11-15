"""DynamoDB-backed note store with S3 as the durable original.

DynamoDB is the source of truth for every note operation (read / write / list /
rename / delete). S3 and the local working copy are *derived*:

    API ──► DynamoDB ──► S3 mirror   (durable outbox stored on the item itself)
                    └──► local cache (write-through, rebuilt on demand)

There is no S3 → local pull/merge step, which removes the race that used to
clobber files while another operation ran. See ``docs/DYNAMODB_NOTES.md``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.config import Config
from botocore.exceptions import ClientError

from application import utils, vault_backend

logger = logging.getLogger("note_store")

DEFAULT_TABLE = "ob-note"
NOTE_ID_INDEX = "note_id-index"
OUTBOX_INDEX = "outbox-index"
# Local secondary index: metadata only (no body), strongly consistent. Listing the tree
# through the base table would read every note body (up to 1MB per page).
PATH_INDEX = "path-index"

# DynamoDB items are capped at 400KB; keep headroom for attributes.
INLINE_MAX_BYTES = 300_000
FILE_PREFIX = "F#"
TOMBSTONE_PREFIX = "X#"
HIDDEN_FOLDER_MARKERS = {".keep", ".gitkeep"}
FOLDER_MARKER = ".keep"

MARKDOWN_SUFFIXES = {".md", ".markdown"}
TEXT_SUFFIXES = MARKDOWN_SUFFIXES | {
    ".txt", ".csv", ".json", ".yaml", ".yml", ".xml", ".html", ".htm",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".rst",
}

KIND_NOTE = "note"
KIND_FILE = "file"
KIND_FOLDER = "folder"

_TRANSACT_PAIR_BATCH = 30  # 3 actions per moved item, 100 actions per transaction
_FLUSH_INTERVAL_SECONDS = 15.0
_MAX_BACKOFF_SECONDS = 300

# Attributes returned when we do not need the (potentially large) body.
_META_ATTRS = (
    "pk", "sk", "path", "kind", "note_id", "title", "size_bytes", "md5", "inline",
    "version", "created_at", "updated_at", "s3_key", "s3_version", "s3_state",
    "outbox", "outbox_at", "attempts", "last_error",
)


class NoteStoreError(RuntimeError):
    pass


class VersionConflict(NoteStoreError):
    """``expected_version`` did not match the stored version."""

    def __init__(self, path: str, expected: Optional[int], actual: Optional[int]):
        super().__init__(f"version conflict on {path}: expected={expected} actual={actual}")
        self.path = path
        self.expected = expected
        self.actual = actual


# ---------------------------------------------------------------------------
# configuration / clients
# ---------------------------------------------------------------------------

_local = threading.local()


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def table_name() -> str:
    env = (os.environ.get("NOTES_DDB_TABLE") or "").strip()
    if env:
        return env
    return str(utils.load_config().get("dynamodb_table") or "").strip()


def enabled() -> bool:
    """DynamoDB mode: a table is configured and S3 vault mode (or explicit opt-in) is on."""
    if _env_flag("NOTES_DDB_DISABLE"):
        return False
    if not table_name():
        return False
    return _env_flag("NOTES_DDB_ENABLE") or _env_flag("VAULT_S3_ENABLE")


def _region() -> str:
    cfg = utils.load_config()
    return (cfg.get("region") or os.environ.get("AWS_REGION") or "us-west-2").strip()


def _resource():
    res = getattr(_local, "resource", None)
    if res is None:
        res = boto3.resource(
            "dynamodb",
            region_name=_region(),
            config=Config(
                connect_timeout=5,
                read_timeout=15,
                retries={"max_attempts": 4, "mode": "standard"},
            ),
        )
        _local.resource = res
    return res


def _table():
    return _resource().Table(table_name() or DEFAULT_TABLE)


def _s3():
    client = getattr(_local, "s3", None)
    if client is None:
        client = boto3.client(
            "s3",
            region_name=_region(),
            config=Config(
                connect_timeout=5,
                read_timeout=60,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        _local.s3 = client
    return client


def _bucket() -> str:
    bucket, _ = vault_backend.s3_bucket_and_region()
    if not bucket:
        raise NoteStoreError("S3 bucket is not configured")
    return bucket


def reset_clients() -> None:
    """Drop cached boto3 clients (tests / credential refresh)."""
    for name in ("resource", "s3"):
        if hasattr(_local, name):
            delattr(_local, name)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def new_note_id() -> str:
    """Session-safe id (matches harness SESSION_ID_RE)."""
    return "n" + uuid.uuid4().hex


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _utc_after(seconds: float) -> str:
    when = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return when.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def nfc(path: str) -> str:
    """Canonical path key: NFC, forward slashes, no leading/trailing slash."""
    cleaned = unicodedata.normalize("NFC", (path or "").replace("\\", "/"))
    return cleaned.strip("/")


def _validate(path: str) -> str:
    rel = nfc(path)
    if not rel:
        raise ValueError("path is required")
    if ".." in rel.split("/"):
        raise ValueError("Path traversal is not allowed")
    return rel


def is_markdown_path(path: str) -> bool:
    return Path(nfc(path)).suffix.lower() in MARKDOWN_SUFFIXES


def is_text_path(path: str) -> bool:
    return Path(nfc(path)).suffix.lower() in TEXT_SUFFIXES


_TITLE_HEADING_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_FM_RE = re.compile(r"^---\s*\n([\s\S]*?)\n---\s*\n?", re.MULTILINE)


def extract_title(path: str, content: str = "") -> str:
    """Title from frontmatter / first H1 / filename stem."""
    text = content or ""
    fm = _FM_RE.match(text)
    if fm:
        for line in fm.group(1).splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            if key.strip().lower() == "title":
                title = val.strip().strip("\"'")
                if title:
                    return title
        body = text[fm.end():]
    else:
        body = text
    m = _TITLE_HEADING_RE.search(body)
    if m:
        return m.group(1).strip()
    return Path(path).stem or "Untitled"


def _pk(user_id: Optional[str] = None) -> str:
    return "USER#" + vault_backend.user_segment(user_id)


def _sk(rel: str) -> str:
    return FILE_PREFIX + rel


def _s3_key_for(rel: str, user_id: Optional[str] = None) -> str:
    return f"{vault_backend.user_segment(user_id)}/{vault_backend.S3_USER_VAULT_DIR}/{rel}"


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()  # noqa: S324 - matches S3 ETag, not a security use


def _proj(attrs: tuple[str, ...] = _META_ATTRS) -> dict[str, Any]:
    names = {f"#a{i}": a for i, a in enumerate(attrs)}
    return {
        "ProjectionExpression": ", ".join(names),
        "ExpressionAttributeNames": names,
    }


def _public(item: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Plain-dict view for API/registry consumers (no outbox internals)."""
    if not item:
        return None
    out: dict[str, Any] = {
        "path": item.get("path"),
        "kind": item.get("kind"),
        "note_id": item.get("note_id"),
        "title": item.get("title") or "",
        "size_bytes": _int(item.get("size_bytes")),
        "version": _int(item.get("version")),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "s3_state": item.get("s3_state"),
    }
    if "content" in item:
        out["content"] = item["content"]
    return out


# ---------------------------------------------------------------------------
# table lifecycle
# ---------------------------------------------------------------------------


def ensure_table(*, wait: bool = True) -> dict[str, Any]:
    """Create the table + GSIs if missing (idempotent). Returns a status dict."""
    name = table_name() or DEFAULT_TABLE
    client = _resource().meta.client
    try:
        desc = client.describe_table(TableName=name)["Table"]
        return {"created": False, "table": name, "status": desc.get("TableStatus")}
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise
    client.create_table(
        TableName=name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "note_id", "AttributeType": "S"},
            {"AttributeName": "outbox", "AttributeType": "S"},
            {"AttributeName": "outbox_at", "AttributeType": "S"},
            {"AttributeName": "path", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        LocalSecondaryIndexes=[
            {
                "IndexName": PATH_INDEX,
                "KeySchema": [
                    {"AttributeName": "pk", "KeyType": "HASH"},
                    {"AttributeName": "path", "KeyType": "RANGE"},
                ],
                "Projection": {
                    "ProjectionType": "INCLUDE",
                    "NonKeyAttributes": [
                        "kind", "note_id", "title", "size_bytes", "md5", "inline", "version",
                        "created_at", "updated_at", "s3_key", "s3_version", "s3_state",
                        "outbox", "outbox_at", "attempts", "last_error",
                    ],
                },
            }
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": NOTE_ID_INDEX,
                "KeySchema": [{"AttributeName": "note_id", "KeyType": "HASH"}],
                "Projection": {
                    "ProjectionType": "INCLUDE",
                    "NonKeyAttributes": [
                        "path", "kind", "title", "size_bytes", "created_at", "updated_at",
                    ],
                },
            },
            {
                "IndexName": OUTBOX_INDEX,
                "KeySchema": [
                    {"AttributeName": "outbox", "KeyType": "HASH"},
                    {"AttributeName": "outbox_at", "KeyType": "RANGE"},
                ],
                "Projection": {
                    "ProjectionType": "INCLUDE",
                    "NonKeyAttributes": ["path", "del_path", "kind", "version", "s3_key", "attempts"],
                },
            },
        ],
        DeletionProtectionEnabled=True,
        Tags=[{"Key": "project", "Value": utils.project_name()}],
    )
    if wait:
        client.get_waiter("table_exists").wait(TableName=name)
        try:
            client.update_continuous_backups(
                TableName=name,
                PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True},
            )
        except ClientError:
            logger.warning("Could not enable PITR on %s", name, exc_info=True)
    return {"created": True, "table": name, "status": "ACTIVE" if wait else "CREATING"}


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def get(
    path: str,
    *,
    content: bool = False,
    consistent: bool = True,
    user_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Raw item for ``path`` (exact NFC match) or ``None``."""
    rel = nfc(path)
    if not rel:
        return None
    kwargs: dict[str, Any] = {
        "Key": {"pk": _pk(user_id), "sk": _sk(rel)},
        "ConsistentRead": consistent,
    }
    if not content:
        kwargs.update(_proj())
    return _table().get_item(**kwargs).get("Item")


def _get_or_none_if_no_table(rel: str, *, user_id: Optional[str], dry_run: bool) -> Optional[dict[str, Any]]:
    """``get`` that treats a missing table as empty, but only for a dry run."""
    try:
        return get(rel, user_id=user_id)
    except ClientError as exc:
        if dry_run and exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return None
        raise


def exists(path: str, *, user_id: Optional[str] = None) -> bool:
    return get(path, user_id=user_id) is not None


def is_dir(path: str, *, user_id: Optional[str] = None) -> bool:
    """True when a folder marker exists or any item lives under ``path/``."""
    rel = nfc(path)
    if not rel:
        return True
    item = get(rel, user_id=user_id)
    if item is not None:
        return item.get("kind") == KIND_FOLDER
    resp = _table().query(
        KeyConditionExpression=Key("pk").eq(_pk(user_id)) & Key("sk").begins_with(_sk(rel + "/")),
        Limit=1,
        ProjectionExpression="#s",
        ExpressionAttributeNames={"#s": "sk"},
    )
    return bool(resp.get("Items"))


_lsi_missing = False


def _mark_lsi_missing() -> None:
    global _lsi_missing
    if not _lsi_missing:
        logger.warning(
            "DynamoDB table %s has no %s index; listing falls back to the slower base-table "
            "query. Recreate the table (see docs/DYNAMODB_NOTES.md) for faster tree loads.",
            table_name(),
            PATH_INDEX,
        )
    _lsi_missing = True


def list_items(
    prefix: str = "",
    *,
    content: bool = False,
    user_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """All items under ``prefix/`` (or the whole vault), sorted by path."""
    rel = nfc(prefix)
    pk_cond = Key("pk").eq(_pk(user_id))

    def base_kwargs(with_content: bool) -> dict[str, Any]:
        sk_prefix = _sk(rel + "/") if rel else FILE_PREFIX
        kw: dict[str, Any] = {
            "KeyConditionExpression": pk_cond & Key("sk").begins_with(sk_prefix),
            "ConsistentRead": True,
        }
        if not with_content:
            kw.update(_proj())
        return kw

    def drain(kwargs: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        table = _table()
        while True:
            resp = table.query(**kwargs)
            out.extend(resp.get("Items") or [])
            last = resp.get("LastEvaluatedKey")
            if not last:
                return out
            kwargs["ExclusiveStartKey"] = last

    if content or _lsi_missing:
        items = drain(base_kwargs(content))
    else:
        # Metadata-only listing: small index items instead of reading every note body.
        lsi_kwargs: dict[str, Any] = {
            "IndexName": PATH_INDEX,
            "KeyConditionExpression": (pk_cond & Key("path").begins_with(rel + "/")) if rel else pk_cond,
            "ConsistentRead": True,
        }
        try:
            items = drain(lsi_kwargs)
        except ClientError as exc:
            err = exc.response.get("Error", {})
            msg = str(err.get("Message", "")).lower()
            # AWS: ValidationException "table does not have the specified index";
            # moto: ResourceNotFoundException "Invalid index". A missing *table* has no
            # "index" in its message and is re-raised below.
            if err.get("Code") in {"ValidationException", "ResourceNotFoundException"} and "index" in msg:
                # Table created before the path index existed: still correct, just slower.
                _mark_lsi_missing()
                items = drain(base_kwargs(False))
            else:
                raise
    items.sort(key=lambda it: it.get("path") or "")
    return items


def list_paths(*, include_markers: bool = True, user_id: Optional[str] = None) -> list[str]:
    """Vault-relative file keys, like the old S3 key listing (folders → ``dir/.keep``)."""
    out: list[str] = []
    for item in list_items(user_id=user_id):
        path = item.get("path") or ""
        if item.get("kind") == KIND_FOLDER:
            if include_markers:
                out.append(f"{path}/{FOLDER_MARKER}")
        else:
            out.append(path)
    return out


def find(path: str, *, user_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Exact match, then Obsidian-style unique basename fallback. Metadata only."""
    item = get(path, user_id=user_id)
    if item is not None and item.get("kind") != KIND_FOLDER:
        return item
    name = Path(nfc(path)).name
    if not name:
        return None
    hits = [
        it for it in list_items(user_id=user_id)
        if it.get("kind") != KIND_FOLDER and Path(it.get("path") or "").name == name
    ]
    if not hits:
        return None
    hits.sort(key=lambda it: (len((it.get("path") or "").split("/")), it.get("path") or ""))
    return hits[0]


def batch_get_text(paths: list[str], *, user_id: Optional[str] = None) -> dict[str, str]:
    """Bodies of many inline notes at once (``{path: text}``); large/binary items are skipped."""
    pk = _pk(user_id)
    keys = [{"pk": pk, "sk": _sk(nfc(p))} for p in dict.fromkeys(paths) if nfc(p)]
    out: dict[str, str] = {}
    name = table_name() or DEFAULT_TABLE
    client = _resource()
    for start in range(0, len(keys), 100):
        request = {name: {"Keys": keys[start:start + 100], "ConsistentRead": True}}
        for _ in range(6):
            resp = client.batch_get_item(RequestItems=request)
            for item in (resp.get("Responses") or {}).get(name, []):
                if "content" in item:
                    out[item["path"]] = str(item["content"])
            request = resp.get("UnprocessedKeys") or {}
            if not request:
                break
            time.sleep(0.1)
    return out


def get_by_note_id(note_id: str, *, user_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    nid = (note_id or "").strip()
    if not nid:
        return None
    pk = _pk(user_id)
    resp = _table().query(IndexName=NOTE_ID_INDEX, KeyConditionExpression=Key("note_id").eq(nid))
    for item in resp.get("Items") or []:
        if item.get("pk") == pk:
            return get(item.get("path") or "", user_id=user_id)
    return None


def read_bytes(path: str, *, user_id: Optional[str] = None) -> Optional[bytes]:
    """File body from DynamoDB (inline) or S3 (large / binary). ``None`` if missing."""
    item = get(path, content=True, user_id=user_id)
    if item is None or item.get("kind") == KIND_FOLDER:
        return None
    if "content" in item:
        return str(item["content"]).encode("utf-8")
    return _s3_read(item)


def read_text(path: str, *, user_id: Optional[str] = None) -> Optional[str]:
    data = read_bytes(path, user_id=user_id)
    return None if data is None else data.decode("utf-8", errors="replace")


def _s3_read(item: dict[str, Any]) -> Optional[bytes]:
    key = item.get("s3_key")
    if not key:
        return None
    try:
        return _s3().get_object(Bucket=_bucket(), Key=key)["Body"].read()
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}:
            return None
        raise


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------


def _outbox_attrs() -> dict[str, Any]:
    return {"outbox": "1", "outbox_at": utc_now(), "s3_state": "pending"}


def put_text(
    path: str,
    content: str,
    *,
    note_id: Optional[str] = None,
    created_at: Optional[str] = None,
    expected_version: Optional[int] = None,
    user_id: Optional[str] = None,
    cache: bool = True,
) -> dict[str, Any]:
    """Create or overwrite a text note. Returns the public item plus ``created``.

    Last writer wins unless ``expected_version`` is given, in which case a
    different stored version raises :class:`VersionConflict`.
    """
    rel = _validate(path)
    text = content or ""
    data = text.encode("utf-8")
    size = len(data)
    inline = size <= INLINE_MAX_BYTES
    markdown = is_markdown_path(rel)
    pk = _pk(user_id)
    table = _table()

    for _attempt in range(6):
        existing = get(rel, user_id=user_id)
        if existing is not None and existing.get("kind") == KIND_FOLDER:
            raise IsADirectoryError(f"{rel} is a folder")
        if expected_version is not None and _int(existing.get("version") if existing else 0) != expected_version:
            raise VersionConflict(
                rel, expected_version, _int(existing.get("version")) if existing else None
            )
        version = _int(existing.get("version")) + 1 if existing else 1
        now = utc_now()
        s3_key = (existing or {}).get("s3_key") or _s3_key_for(rel, user_id)
        item: dict[str, Any] = {
            "pk": pk,
            "sk": _sk(rel),
            "path": rel,
            "kind": KIND_NOTE,
            "size_bytes": size,
            "md5": _md5(data),
            "inline": inline,
            "version": version,
            "created_at": (existing or {}).get("created_at") or created_at or now,
            "updated_at": now,
            "s3_key": s3_key,
        }
        if markdown:
            item["note_id"] = (existing or {}).get("note_id") or (note_id or "").strip() or new_note_id()
            item["title"] = extract_title(rel, text)
        if inline:
            item["content"] = text
            item.update(_outbox_attrs())
            item["s3_version"] = _int((existing or {}).get("s3_version"))
        else:
            # Too big for an item: S3 holds the body, written first.
            _s3_put(s3_key, data, rel)
            item["s3_version"] = version
            item["s3_state"] = "synced"
        try:
            if existing is not None:
                table.put_item(
                    Item=item,
                    ConditionExpression=Attr("version").eq(_int(existing.get("version"))),
                )
            else:
                table.put_item(Item=item, ConditionExpression=Attr("pk").not_exists())
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            if expected_version is not None:
                cur = get(rel, user_id=user_id)
                raise VersionConflict(rel, expected_version, _int(cur.get("version")) if cur else None)
            continue  # concurrent writer: re-read and retry (last writer wins)
        if cache:
            _cache_write(rel, data, now, version=version, user_id=user_id)
        if inline:
            schedule_flush()
        out = _public(item) or {}
        out["created"] = existing is None
        out.pop("content", None)
        return out
    raise NoteStoreError(f"could not write {rel}: too much contention")


def put_bytes(
    path: str,
    data: bytes,
    *,
    user_id: Optional[str] = None,
    cache: bool = True,
) -> dict[str, Any]:
    """Store arbitrary bytes. Small UTF-8 text goes inline, everything else lives in S3."""
    rel = _validate(path)
    if is_text_path(rel) and len(data) <= INLINE_MAX_BYTES:
        try:
            return put_text(rel, data.decode("utf-8"), user_id=user_id, cache=cache)
        except UnicodeDecodeError:
            pass
    return _put_s3_backed(rel, data=data, user_id=user_id, cache=cache)


def put_file(
    path: str,
    local_path: Path,
    *,
    user_id: Optional[str] = None,
) -> dict[str, Any]:
    """Ingest a local file (streams large binaries to S3 instead of loading them)."""
    rel = _validate(path)
    size = local_path.stat().st_size
    if is_text_path(rel) and size <= INLINE_MAX_BYTES:
        raw = local_path.read_bytes()
        current = get(rel, user_id=user_id)
        if current is not None and current.get("md5") == _md5(raw) and current.get("kind") == KIND_NOTE:
            out = _public(current) or {}
            out["created"] = False
            return out  # already stored: no new version, no extra S3 write
        try:
            return put_text(rel, raw.decode("utf-8"), user_id=user_id, cache=False)
        except UnicodeDecodeError:
            return _put_s3_backed(rel, data=raw, user_id=user_id, cache=False)
    return _put_s3_backed(rel, local_path=local_path, user_id=user_id, cache=False)


def _put_s3_backed(
    rel: str,
    *,
    data: Optional[bytes] = None,
    local_path: Optional[Path] = None,
    user_id: Optional[str] = None,
    cache: bool = True,
) -> dict[str, Any]:
    """Body → S3 first (synchronously), then the metadata item."""
    table = _table()
    pk = _pk(user_id)
    for _attempt in range(6):
        existing = get(rel, user_id=user_id)
        if existing is not None and existing.get("kind") == KIND_FOLDER:
            raise IsADirectoryError(f"{rel} is a folder")
        version = _int(existing.get("version")) + 1 if existing else 1
        s3_key = (existing or {}).get("s3_key") or _s3_key_for(rel, user_id)
        if data is not None:
            size = len(data)
            md5 = _md5(data)
            _s3_put(s3_key, data, rel)
        else:
            assert local_path is not None
            size = local_path.stat().st_size
            md5 = ""
            _s3().upload_file(str(local_path), _bucket(), s3_key)
        now = utc_now()
        item: dict[str, Any] = {
            "pk": pk,
            "sk": _sk(rel),
            "path": rel,
            "kind": KIND_FILE,
            "size_bytes": size,
            "inline": False,
            "version": version,
            "created_at": (existing or {}).get("created_at") or now,
            "updated_at": now,
            "s3_key": s3_key,
            "s3_version": version,
            "s3_state": "synced",
        }
        if md5:
            item["md5"] = md5
        try:
            if existing is not None:
                table.put_item(
                    Item=item,
                    ConditionExpression=Attr("version").eq(_int(existing.get("version"))),
                )
            else:
                table.put_item(Item=item, ConditionExpression=Attr("pk").not_exists())
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            continue
        if cache and data is not None:
            _cache_write(rel, data, now, version=version, user_id=user_id)
        out = _public(item) or {}
        out["created"] = existing is None
        return out
    raise NoteStoreError(f"could not write {rel}: too much contention")


def import_object(
    path: str,
    *,
    s3_key: str,
    size: int,
    text: Optional[str] = None,
    etag: str = "",
    note_id: Optional[str] = None,
    title: Optional[str] = None,
    created_at: Optional[str] = None,
    updated_at: Optional[str] = None,
    overwrite: bool = False,
    dry_run: bool = False,
    expect_version: Optional[int] = None,
    user_id: Optional[str] = None,
) -> str:
    """Register an object that already exists in S3 (migration / Sync import).

    ``expect_version`` makes the write conditional: it only applies if the item is still
    at that version with no S3 mirror pending, otherwise ``conflict`` is returned.

    The S3 object is *not* touched: ``s3_key`` records where the original really
    lives (it may use NFD Hangul). ``text`` is the body for inline notes; ``None``
    registers a binary / oversized object whose body stays in S3.

    Returns ``created`` | ``updated`` | ``unchanged`` | ``conflict``.
    """
    rel = _validate(path)
    existing = _get_or_none_if_no_table(rel, user_id=user_id, dry_run=dry_run)
    md5 = _md5(text.encode("utf-8")) if text is not None else ""
    if not md5 and etag and "-" not in etag:
        md5 = etag.strip('"')
    if existing is not None:
        same = bool(md5) and existing.get("md5") == md5 and existing.get("kind") != KIND_FOLDER
        if same:
            return "unchanged"
        if not overwrite:
            return "conflict"
    if dry_run:
        return "created" if existing is None else "updated"

    now = utc_now()
    version = _int(existing.get("version")) + 1 if existing else 1
    item: dict[str, Any] = {
        "pk": _pk(user_id),
        "sk": _sk(rel),
        "path": rel,
        "kind": KIND_NOTE if text is not None else KIND_FILE,
        "size_bytes": size,
        "inline": text is not None,
        "version": version,
        "created_at": (existing or {}).get("created_at") or created_at or now,
        "updated_at": updated_at or now,
        "s3_key": s3_key,
        "s3_version": version,
        "s3_state": "synced",
    }
    if md5:
        item["md5"] = md5
    if text is not None:
        item["content"] = text
    if is_markdown_path(rel):
        item["note_id"] = (existing or {}).get("note_id") or (note_id or "").strip() or new_note_id()
        item["title"] = (title or "").strip() or extract_title(rel, text or "")
    kwargs: dict[str, Any] = {}
    if expect_version is not None:
        kwargs["ConditionExpression"] = (
            Attr("pk").not_exists()
            if existing is None
            else Attr("version").eq(expect_version) & Attr("outbox").not_exists()
        )
    try:
        _table().put_item(Item=item, **kwargs)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return "conflict"
        raise
    return "created" if existing is None else "updated"


def import_folder(
    path: str,
    *,
    s3_key: str,
    created_at: Optional[str] = None,
    dry_run: bool = False,
    user_id: Optional[str] = None,
) -> str:
    """Register an empty-folder marker (``dir/.keep``) that already exists in S3."""
    rel = _validate(path)
    if _get_or_none_if_no_table(rel, user_id=user_id, dry_run=dry_run) is not None:
        return "unchanged"
    if dry_run:
        return "created"
    now = utc_now()
    _table().put_item(
        Item={
            "pk": _pk(user_id),
            "sk": _sk(rel),
            "path": rel,
            "kind": KIND_FOLDER,
            "size_bytes": 0,
            "inline": False,
            "version": 1,
            "created_at": created_at or now,
            "updated_at": created_at or now,
            "s3_key": s3_key,
            "s3_version": 1,
            "s3_state": "synced",
        },
        ConditionExpression=Attr("pk").not_exists(),
    )
    return "created"


def append_text(
    path: str,
    chunk: str,
    *,
    create: bool = True,
    separator: str = "\n",
    user_id: Optional[str] = None,
) -> dict[str, Any]:
    """Read-modify-write with an optimistic lock, so concurrent appends do not drop text."""
    rel = _validate(path)
    for _attempt in range(8):
        existing = get(rel, content=True, user_id=user_id)
        if existing is None and not create:
            raise FileNotFoundError(rel)
        if existing is not None and existing.get("kind") == KIND_FOLDER:
            raise IsADirectoryError(rel)
        current = ""
        if existing is not None:
            if "content" in existing:
                current = str(existing["content"])
            else:
                body = _s3_read(existing)
                current = (body or b"").decode("utf-8", errors="replace")
        piece = chunk or ""
        if current and piece and not current.endswith(("\n", "\r")):
            merged = current + (separator if separator is not None else "\n") + piece
        else:
            merged = current + piece
        try:
            return put_text(
                rel,
                merged,
                expected_version=_int(existing.get("version")) if existing else 0,
                user_id=user_id,
            )
        except VersionConflict:
            continue
    raise NoteStoreError(f"could not append to {rel}: too much contention")


def mkdir(path: str, *, user_id: Optional[str] = None) -> dict[str, Any]:
    """Persist an (empty) folder as an item; mirrored to S3 as ``dir/.keep``."""
    rel = _validate(path)
    existing = get(rel, user_id=user_id)
    if existing is not None:
        if existing.get("kind") != KIND_FOLDER:
            raise FileExistsError(f"{rel} is a file")
        return _public(existing) or {}
    now = utc_now()
    item = {
        "pk": _pk(user_id),
        "sk": _sk(rel),
        "path": rel,
        "kind": KIND_FOLDER,
        "size_bytes": 0,
        "inline": False,
        "version": 1,
        "created_at": now,
        "updated_at": now,
        "s3_key": _s3_key_for(f"{rel}/{FOLDER_MARKER}", user_id),
        "s3_version": 0,
        **_outbox_attrs(),
    }
    try:
        _table().put_item(Item=item, ConditionExpression=Attr("pk").not_exists())
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
    _cache_mkdir(rel, user_id=user_id)
    schedule_flush()
    return _public(item) or {}


def _tombstone(item: dict[str, Any]) -> Optional[dict[str, Any]]:
    key = item.get("s3_key")
    if not key:
        return None
    return {
        "pk": item["pk"],
        "sk": f"{TOMBSTONE_PREFIX}{item.get('path')}#{uuid.uuid4().hex[:8]}",
        "del_path": item.get("path"),  # not "path": tombstones must stay out of the path index
        "kind": "tombstone",
        "s3_key": key,
        "attempts": 0,
        **_outbox_attrs(),
    }


def _transact(actions: list[dict[str, Any]]) -> None:
    if actions:
        _resource().meta.client.transact_write_items(TransactItems=actions)


def delete(path: str, *, user_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Delete a file, or a folder and everything under it. Returns the removed items.

    Each item is removed together with an S3 tombstone in one transaction, so the
    original is deleted from S3 even if this process dies right after.
    """
    rel = nfc(path)
    if not rel:
        return []
    victims: list[dict[str, Any]] = []
    exact = get(rel, user_id=user_id)
    if exact is not None:
        victims.append(exact)
    victims.extend(list_items(rel, user_id=user_id))
    seen: set[str] = set()
    unique = []
    for item in victims:
        if item["sk"] in seen:
            continue
        seen.add(item["sk"])
        unique.append(item)
    if not unique:
        return []

    name = table_name() or DEFAULT_TABLE
    for start in range(0, len(unique), _TRANSACT_PAIR_BATCH):
        actions: list[dict[str, Any]] = []
        for item in unique[start:start + _TRANSACT_PAIR_BATCH]:
            actions.append(
                {"Delete": {"TableName": name, "Key": {"pk": item["pk"], "sk": item["sk"]}}}
            )
            tomb = _tombstone(item)
            if tomb:
                actions.append({"Put": {"TableName": name, "Item": tomb}})
        _transact(actions)
    _cache_delete(rel, user_id=user_id)
    schedule_flush()
    return [p for p in (_public(i) for i in unique) if p]


def rename(
    src: str,
    dst: str,
    *,
    user_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Move a file or folder. ``note_id``/``created_at`` are preserved.

    Returns ``{"moved": [(from, to), ...], "dropped_note_ids": [...], "item": public|None}``
    or ``None`` when the source does not exist.
    """
    s = nfc(src)
    d = _validate(dst)
    if not s:
        return None
    if s == d:
        return {"moved": [], "dropped_note_ids": [], "item": _public(get(d, user_id=user_id))}
    if d == s or d.startswith(s + "/"):
        raise ValueError("Cannot move a folder into itself")

    exact = get(s, content=True, user_id=user_id)
    if exact is not None and exact.get("kind") != KIND_FOLDER:
        sources = [(s, d, exact)]
        single = True
    else:
        children = list_items(s, content=True, user_id=user_id)
        if exact is not None:  # explicit folder marker
            children = [exact] + [c for c in children if c["sk"] != exact["sk"]]
        if not children:
            return None
        sources = []
        for child in children:
            old = child["path"]
            new = d + old[len(s):]
            sources.append((old, new, child))
        single = False

    name = table_name() or DEFAULT_TABLE
    pk = _pk(user_id)
    dropped: list[str] = []
    moved: list[tuple[str, str]] = []
    new_public: Optional[dict[str, Any]] = None
    source_sks = {it["sk"] for _o, _n, it in sources}

    for start in range(0, len(sources), _TRANSACT_PAIR_BATCH):
        actions: list[dict[str, Any]] = []
        for old, new, item in sources[start:start + _TRANSACT_PAIR_BATCH]:
            clash = get(new, user_id=user_id)
            if clash is not None and _sk(new) not in source_sks:
                if clash.get("note_id") and clash.get("note_id") != item.get("note_id"):
                    dropped.append(clash["note_id"])
            now = utc_now()
            version = _int(item.get("version")) + 1
            new_item = {k: v for k, v in item.items() if k not in {"outbox", "outbox_at", "attempts", "last_error"}}
            new_item.update(
                {
                    "pk": pk,
                    "sk": _sk(new),
                    "path": new,
                    "version": version,
                    "updated_at": now,
                }
            )
            kind = item.get("kind")
            if kind == KIND_FOLDER:
                new_item["s3_key"] = _s3_key_for(f"{new}/{FOLDER_MARKER}", user_id)
                new_item.update(_outbox_attrs())
                new_item["s3_version"] = 0
            else:
                new_item["s3_key"] = _s3_key_for(new, user_id)
                if single and kind == KIND_NOTE and is_markdown_path(new):
                    if Path(old).stem != Path(new).stem:
                        new_item["title"] = Path(new).stem
                if "content" in item:
                    new_item.update(_outbox_attrs())
                    new_item["s3_version"] = 0
                else:
                    # Body only lives in S3: server-side copy before the metadata moves.
                    _s3_copy(item.get("s3_key"), new_item["s3_key"])
                    new_item["s3_version"] = version
                    new_item["s3_state"] = "synced"
            put: dict[str, Any] = {"TableName": name, "Item": new_item}
            actions.append({"Put": put})
            actions.append(
                {"Delete": {"TableName": name, "Key": {"pk": item["pk"], "sk": item["sk"]}}}
            )
            tomb = _tombstone(item)
            if tomb and tomb["s3_key"] != new_item.get("s3_key"):
                actions.append({"Put": {"TableName": name, "Item": tomb}})
            moved.append((old, new))
            if single:
                new_public = _public(new_item)
        _transact(actions)

    _cache_move(s, d, user_id=user_id)
    schedule_flush()
    return {"moved": moved, "dropped_note_ids": dropped, "item": new_public}


def copy(src: str, dst: str, *, user_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Copy one file. Notes get a fresh ``note_id``. Returns the new public item."""
    s = nfc(src)
    d = _validate(dst)
    item = get(s, content=True, user_id=user_id)
    if item is None or item.get("kind") == KIND_FOLDER:
        return None
    if "content" in item:
        return put_text(d, str(item["content"]), user_id=user_id)
    body = _s3_read(item)
    if body is None:
        return None
    return _put_s3_backed(d, data=body, user_id=user_id)


# ---------------------------------------------------------------------------
# S3 mirror (DynamoDB is the durable outbox)
# ---------------------------------------------------------------------------


def _content_type(rel_or_key: str) -> str:
    import mimetypes

    ext = Path(rel_or_key).suffix.lower()
    if ext in MARKDOWN_SUFFIXES:
        return "text/markdown; charset=utf-8"
    guess, _ = mimetypes.guess_type(rel_or_key)
    return guess or "application/octet-stream"


def _s3_put(key: str, data: bytes, rel: str) -> None:
    _s3().put_object(Bucket=_bucket(), Key=key, Body=data, ContentType=_content_type(rel))


def _s3_copy(src_key: Optional[str], dst_key: str) -> None:
    if not src_key:
        raise NoteStoreError("missing s3_key for copy")
    _s3().copy_object(
        Bucket=_bucket(), Key=dst_key, CopySource={"Bucket": _bucket(), "Key": src_key}
    )


def _s3_delete(key: str) -> None:
    _s3().delete_object(Bucket=_bucket(), Key=key)


def _backoff(item_key: dict[str, Any], attempts: int, err: Exception) -> None:
    delay = min(_MAX_BACKOFF_SECONDS, 2 ** min(attempts + 1, 9))
    try:
        _table().update_item(
            Key=item_key,
            UpdateExpression="SET attempts = :a, outbox_at = :t, last_error = :e, s3_state = :s",
            ConditionExpression=Attr("outbox").exists(),
            ExpressionAttributeValues={
                ":a": attempts + 1,
                ":t": _utc_after(delay),
                ":e": str(err)[:300],
                ":s": "error",
            },
        )
    except ClientError:
        pass


def _owns_key(pk: str, rel: str, key: str) -> bool:
    live = _table().get_item(
        Key={"pk": pk, "sk": _sk(rel)}, ConsistentRead=True, **_proj(("s3_key",))
    ).get("Item")
    return bool(live and live.get("s3_key") == key)


def _flush_one(entry: dict[str, Any]) -> bool:
    """Mirror one outbox entry to S3. Returns True when it is settled."""
    pk = entry["pk"]
    sk = entry["sk"]
    table = _table()
    if sk.startswith(TOMBSTONE_PREFIX):
        key = entry.get("s3_key")
        if key and not _owns_key(pk, entry.get("del_path") or "", key):
            _s3_delete(key)
        table.delete_item(Key={"pk": pk, "sk": sk})
        return True

    item = table.get_item(Key={"pk": pk, "sk": sk}, ConsistentRead=True).get("Item")
    if item is None:
        return True
    version = _int(item.get("version"))
    key = item.get("s3_key")
    kind = item.get("kind")
    if kind == KIND_FOLDER:
        _s3().put_object(Bucket=_bucket(), Key=key, Body=b"")
    elif "content" in item:
        _s3_put(key, str(item["content"]).encode("utf-8"), item.get("path") or key)
    # else: body already lives in S3 (large/binary); just settle the flag.

    try:
        table.update_item(
            Key={"pk": pk, "sk": sk},
            UpdateExpression=(
                "SET s3_version = :v, s3_state = :s REMOVE outbox, outbox_at, attempts, last_error"
            ),
            ConditionExpression=Attr("version").eq(version),
            ExpressionAttributeValues={":v": version, ":s": "synced"},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        # Changed (or removed/renamed) while uploading. A newer write already carries
        # its own outbox; if the item is gone, do not leave an orphaned object behind.
        if key and not _owns_key(pk, item.get("path") or "", key):
            _s3_delete(key)
    return True


def flush_outbox(
    *,
    limit: int = 500,
    user_id: Optional[str] = None,
    on_progress: Optional[Callable[[dict[str, Any]], None]] = None,
    include_future: bool = False,
) -> dict[str, Any]:
    """Apply pending S3 puts/deletes. Safe to run from several tasks at once."""
    if not enabled() and not table_name():
        return {"ok": True, "flushed": 0, "remaining": 0, "skipped": True}
    pk = _pk(user_id) if user_id or vault_backend.current_user_id() else None
    cond = Key("outbox").eq("1")
    if not include_future:
        cond = cond & Key("outbox_at").lte(utc_now())
    kwargs: dict[str, Any] = {"IndexName": OUTBOX_INDEX, "KeyConditionExpression": cond}
    if pk:
        kwargs["FilterExpression"] = Attr("pk").eq(pk)
    entries: list[dict[str, Any]] = []
    table = _table()
    while len(entries) < limit:
        resp = table.query(**kwargs)
        entries.extend(resp.get("Items") or [])
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    entries = entries[:limit]

    flushed = failed = 0
    total = len(entries)
    for i, entry in enumerate(entries, start=1):
        if callable(on_progress):
            on_progress(
                {
                    "phase": "flush",
                    "file": entry.get("path") or entry.get("del_path"),
                    "file_i": i,
                    "file_n": total,
                    "pct": int(round((i - 1) / max(total, 1) * 100)),
                    "message": f"S3에 원본을 반영하는 중… ({i}/{total})",
                }
            )
        try:
            _flush_one(entry)
            flushed += 1
        except Exception as exc:  # noqa: BLE001 - keep draining the rest
            failed += 1
            logger.warning("S3 mirror failed for %s: %s", entry.get("path") or entry.get("del_path"), exc)
            _backoff({"pk": entry["pk"], "sk": entry["sk"]}, _int(entry.get("attempts")), exc)
    remaining = pending_count(user_id=user_id, include_future=True)
    return {"ok": failed == 0 and remaining == 0, "flushed": flushed, "failed": failed, "remaining": remaining}


def pending_count(*, user_id: Optional[str] = None, include_future: bool = True) -> int:
    """Number of items still waiting to be mirrored to S3."""
    cond = Key("outbox").eq("1")
    if not include_future:
        cond = cond & Key("outbox_at").lte(utc_now())
    kwargs: dict[str, Any] = {"IndexName": OUTBOX_INDEX, "KeyConditionExpression": cond, "Select": "COUNT"}
    if user_id or vault_backend.current_user_id():
        kwargs["FilterExpression"] = Attr("pk").eq(_pk(user_id))
    total = 0
    table = _table()
    while True:
        resp = table.query(**kwargs)
        total += _int(resp.get("Count"))
        last = resp.get("LastEvaluatedKey")
        if not last:
            return total
        kwargs["ExclusiveStartKey"] = last


def pending_items(*, user_id: Optional[str] = None, limit: int = 50) -> list[dict[str, Any]]:
    kwargs: dict[str, Any] = {
        "IndexName": OUTBOX_INDEX,
        "KeyConditionExpression": Key("outbox").eq("1"),
    }
    if user_id or vault_backend.current_user_id():
        kwargs["FilterExpression"] = Attr("pk").eq(_pk(user_id))
    items = _table().query(**kwargs).get("Items") or []
    return [
        {
            "op": "delete" if str(i.get("sk", "")).startswith(TOMBSTONE_PREFIX) else "put",
            "path": i.get("path") or i.get("del_path"),
        }
        for i in items[:limit]
    ]


_flush_lock = threading.Lock()
_flush_pending = threading.Event()
_executor: Optional[ThreadPoolExecutor] = None
_auto_flush = True  # tests switch this off to flush deterministically
_loop_thread: Optional[threading.Thread] = None
_loop_stop = threading.Event()


def _flush_worker() -> None:
    # One worker drains the outbox; requests that arrive meanwhile coalesce into one more pass.
    while _flush_pending.is_set():
        _flush_pending.clear()
        if not _flush_lock.acquire(blocking=False):
            _flush_pending.set()
            return
        try:
            flush_outbox()
        except Exception:  # noqa: BLE001
            logger.exception("background S3 mirror pass failed")
        finally:
            _flush_lock.release()


def schedule_flush() -> None:
    """Kick a background S3 mirror pass; never blocks the request."""
    global _executor
    if not _auto_flush:
        return
    _flush_pending.set()
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="note-s3-mirror")
    _executor.submit(_flush_worker)


def start_background_mirror() -> None:
    """Periodic drain, so retries and tasks that died mid-flush are picked up."""
    global _loop_thread
    if _loop_thread is not None and _loop_thread.is_alive():
        return

    def _loop() -> None:
        while not _loop_stop.wait(_FLUSH_INTERVAL_SECONDS):
            try:
                if _flush_lock.acquire(blocking=False):
                    try:
                        flush_outbox()
                    finally:
                        _flush_lock.release()
            except Exception:  # noqa: BLE001
                logger.exception("periodic S3 mirror pass failed")

    _loop_stop.clear()
    _loop_thread = threading.Thread(target=_loop, name="note-s3-mirror-loop", daemon=True)
    _loop_thread.start()


def stop_background_mirror() -> None:
    _loop_stop.set()


# ---------------------------------------------------------------------------
# local cache (derived, disposable)
# ---------------------------------------------------------------------------

_cache_versions: dict[tuple[str, str], int] = {}
_cache_lock = threading.RLock()
_PRUNE_GRACE_SECONDS = 120


def _seg(user_id: Optional[str]) -> str:
    return vault_backend.user_segment(user_id)


def _cache_path(rel: str, user_id: Optional[str]) -> Optional[Path]:
    try:
        with vault_backend.user_scope(user_id if user_id else vault_backend.current_user_id()):
            return vault_backend.resolve_vault_path(rel)
    except Exception:  # noqa: BLE001
        return None


def _cache_write(
    rel: str,
    data: bytes,
    updated_at: str,
    *,
    version: Optional[int] = None,
    user_id: Optional[str] = None,
) -> None:
    target = _cache_path(rel, user_id)
    if target is None:
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:6]}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)  # atomic: readers never see a half-written note
        try:
            ts = datetime.strptime(updated_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            os.utime(target, (ts, ts))
        except ValueError:
            pass
        if version is not None:
            with _cache_lock:
                _cache_versions[(_seg(user_id), rel)] = version
    except OSError:
        logger.warning("local cache write failed for %s", rel, exc_info=True)


def _cache_mkdir(rel: str, *, user_id: Optional[str] = None) -> None:
    target = _cache_path(rel, user_id)
    if target is not None:
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("local cache mkdir failed for %s", rel, exc_info=True)


def _cache_delete(rel: str, *, user_id: Optional[str] = None) -> None:
    import shutil

    target = _cache_path(rel, user_id)
    seg = _seg(user_id)
    with _cache_lock:
        for key in [k for k in _cache_versions if k[0] == seg and (k[1] == rel or k[1].startswith(rel + "/"))]:
            _cache_versions.pop(key, None)
    if target is None:
        return
    try:
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    except OSError:
        logger.warning("local cache delete failed for %s", rel, exc_info=True)


def _cache_move(src: str, dst: str, *, user_id: Optional[str] = None) -> None:
    import shutil

    a = _cache_path(src, user_id)
    b = _cache_path(dst, user_id)
    seg = _seg(user_id)
    with _cache_lock:
        for key in [k for k in _cache_versions if k[0] == seg and (k[1] == src or k[1].startswith(src + "/"))]:
            _cache_versions.pop(key, None)
    if a is None or b is None or not a.exists():
        return
    try:
        b.parent.mkdir(parents=True, exist_ok=True)
        if b.is_dir() and a.is_dir():
            shutil.rmtree(b)
        shutil.move(str(a), str(b))
    except OSError:
        logger.warning("local cache move failed %s -> %s", src, dst, exc_info=True)


def warm_cache(
    path: str,
    data: bytes,
    *,
    updated_at: str,
    version: Optional[int] = None,
    user_id: Optional[str] = None,
) -> None:
    """Write bytes into the local working copy without another DynamoDB round-trip."""
    rel = nfc(path)
    if not rel:
        return
    _cache_write(rel, data, updated_at or utc_now(), version=version, user_id=user_id)


def materialize(path: str, *, user_id: Optional[str] = None) -> Optional[Path]:
    """Make sure the local cache holds the current ``path``; return its location."""
    # Prefer one GetItem with content for the common exact-path case (tree click).
    rel_hint = nfc(path)
    item = get(rel_hint, content=True, user_id=user_id) if rel_hint else None
    if item is None or item.get("kind") == KIND_FOLDER:
        found = find(path, user_id=user_id)
        if found is None:
            return None
        # Basename fallback returned metadata only — fetch body once.
        if "content" not in found and found.get("kind") != KIND_FILE:
            item = get(found["path"], content=True, user_id=user_id)
            if item is None:
                return None
        else:
            item = found
    rel = item["path"]
    target = _cache_path(rel, user_id)
    if target is None:
        return None
    version = _int(item.get("version"))
    key = (_seg(user_id), rel)
    with _cache_lock:
        fresh = target.is_file() and _cache_versions.get(key) == version
    if fresh:
        return target
    if item.get("kind") == KIND_FILE:
        if target.is_file() and target.stat().st_size == _int(item.get("size_bytes")):
            with _cache_lock:
                _cache_versions[key] = version
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:6]}.tmp")
        _s3().download_file(_bucket(), item["s3_key"], str(tmp))
        os.replace(tmp, target)
        with _cache_lock:
            _cache_versions[key] = version
        return target
    data = str(item["content"]).encode("utf-8") if "content" in item else (_s3_read(item) or b"")
    _cache_write(
        rel,
        data,
        item.get("updated_at") or utc_now(),
        version=version,
        user_id=user_id,
    )
    return target if target.is_file() else None


def refresh_cache(
    prefix: str = "",
    *,
    include_files: bool = False,
    prune: bool = True,
    user_id: Optional[str] = None,
    on_progress: Optional[Callable[[dict[str, Any]], None]] = None,
) -> dict[str, Any]:
    """Bring the local cache in line with DynamoDB (one direction only).

    ``include_files`` also downloads binaries (needed before zipping a folder).
    Local files DynamoDB does not know about are pruned after a grace period.
    """
    items = list_items(prefix, user_id=user_id)
    seg = _seg(user_id)
    written = skipped = 0
    wanted: set[str] = set()
    total = len(items)
    for i, item in enumerate(items, start=1):
        rel = item["path"]
        if item.get("kind") == KIND_FOLDER:
            _cache_mkdir(rel, user_id=user_id)
            wanted.add(rel)
            continue
        wanted.add(rel)
        if item.get("kind") == KIND_FILE and not include_files:
            continue
        if callable(on_progress):
            on_progress(
                {
                    "phase": "pull",
                    "file": rel,
                    "file_i": i,
                    "file_n": total,
                    "pct": int(round((i - 1) / max(total, 1) * 100)),
                    "message": f"로컬 캐시를 갱신하는 중… ({i}/{total})",
                }
            )
        target = _cache_path(rel, user_id)
        with _cache_lock:
            cached = _cache_versions.get((seg, rel))
        if target is not None and target.is_file() and cached == _int(item.get("version")):
            skipped += 1
            continue
        if materialize(rel, user_id=user_id) is not None:
            written += 1
    pruned = 0
    if prune:
        pruned = _prune_cache(prefix, wanted, user_id=user_id)
    return {"written": written, "unchanged": skipped, "pruned": pruned, "total": total}


def _prune_cache(prefix: str, wanted: set[str], *, user_id: Optional[str]) -> int:
    """Remove cache files DynamoDB no longer has (cache only; never touches DynamoDB/S3)."""
    vault_root = _vault_root(user_id)
    if vault_root is None:
        return 0
    rel_prefix = nfc(prefix)
    root = vault_root / rel_prefix if rel_prefix else vault_root
    if not root.exists():
        return 0
    removed = 0
    now = time.time()
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        try:
            rel = path.relative_to(vault_root).as_posix()
        except ValueError:
            continue
        if rel == ".vault" or rel.startswith(".vault/") or path.name.startswith("."):
            continue
        key = nfc(rel)
        if path.is_file():
            if key in wanted:
                continue
            if now - path.stat().st_mtime < _PRUNE_GRACE_SECONDS:
                continue  # a legacy writer may not have ingested this yet
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        elif path.is_dir():
            if key in wanted or any(w.startswith(key + "/") for w in wanted):
                continue
            try:
                path.rmdir()
            except OSError:
                pass
    return removed


def _vault_root(user_id: Optional[str]) -> Optional[Path]:
    try:
        with vault_backend.user_scope(user_id if user_id else vault_backend.current_user_id()):
            return vault_backend.vault_root()
    except Exception:  # noqa: BLE001
        return None


_settings_restored: set[str] = set()
_SETTINGS_SKIP = {"pending_s3_ops.json", "notes.db", "sync_status.json"}


def restore_settings(*, user_id: Optional[str] = None) -> int:
    """Once per process and user: fetch missing ``.vault/*.json`` settings from S3.

    Settings (share list, folder order, app.json …) are not notes and stay plain S3
    objects. A fresh ECS task starts with an empty working dir, so bring back what
    is missing. Existing local files are never overwritten.
    """
    seg = _seg(user_id)
    if seg in _settings_restored:
        return 0
    prefix = f"{seg}/{vault_backend.S3_USER_VAULT_DIR}/.vault/"
    restored = 0
    try:
        with vault_backend.user_scope(user_id if user_id else vault_backend.current_user_id()):
            settings = vault_backend.settings_dir()
        paginator = _s3().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=_bucket(), Prefix=prefix):
            for obj in page.get("Contents") or []:
                name = obj["Key"][len(prefix):]
                if not name or "/" in name or name in _SETTINGS_SKIP:
                    continue
                dest = settings / name
                if dest.exists():
                    continue
                settings.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_name(f".{name}.{uuid.uuid4().hex[:6]}.tmp")
                _s3().download_file(_bucket(), obj["Key"], str(tmp))
                os.replace(tmp, dest)
                restored += 1
        _settings_restored.add(seg)
    except Exception:  # noqa: BLE001 - retried on the next request
        logger.warning("settings restore failed for %s", seg, exc_info=True)
    return restored


_cache_refreshed_at: dict[str, float] = {}


def ensure_cache_fresh(
    *,
    max_age_seconds: float = 30.0,
    include_files: bool = False,
    user_id: Optional[str] = None,
) -> None:
    """For consumers that read the working copy from disk (graph query, media scan)."""
    key = f"{_seg(user_id)}:{int(include_files)}"
    now = time.time()
    with _cache_lock:
        if now - _cache_refreshed_at.get(key, 0.0) < max_age_seconds:
            return
        _cache_refreshed_at[key] = now
    try:
        refresh_cache(include_files=include_files, prune=True, user_id=user_id)
    except Exception:  # noqa: BLE001 - stale cache beats a failed request
        logger.warning("cache refresh failed", exc_info=True)
        with _cache_lock:
            _cache_refreshed_at.pop(key, None)


def _iso(dt) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _remote_md5(obj: dict[str, Any], key: str) -> str:
    """md5 of an S3 object: the ETag, or a download when it is a multipart ETag."""
    etag = str(obj.get("ETag") or "").strip('"')
    if etag and "-" not in etag:
        return etag
    body = _s3().get_object(Bucket=_bucket(), Key=key)["Body"].read()
    return _md5(body)


def _forget_item(item: dict[str, Any]) -> bool:
    """Drop an item whose S3 object is already gone (no tombstone needed)."""
    try:
        _table().delete_item(
            Key={"pk": item["pk"], "sk": item["sk"]},
            ConditionExpression=Attr("version").eq(_int(item.get("version"))) & Attr("outbox").not_exists(),
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    if item.get("kind") != KIND_FOLDER:
        _cache_delete(item["path"])
    return True


def import_from_s3(
    *,
    apply_deletes: bool = True,
    user_id: Optional[str] = None,
    on_progress: Optional[Callable[[dict[str, Any]], None]] = None,
    _after_snapshot: Optional[Callable[[], None]] = None,
) -> dict[str, Any]:
    """Bring edits made directly in S3 (console, CLI) into DynamoDB. One direction only.

    Safe against the app's own mirror: imported items are marked synced and never get an
    outbox, so nothing is written back to S3 (no echo loop). Every write is conditional
    on the version seen in the snapshot, so a concurrent app edit wins and the S3 change
    is reported as a conflict instead of overwriting it.
    """
    seg = _seg(user_id)
    prefix = f"{seg}/{vault_backend.S3_USER_VAULT_DIR}/"
    report: dict[str, Any] = {
        "added": 0, "updated": 0, "deleted": 0, "unchanged": 0,
        "conflicts": [], "twins": [], "errors": [], "deleted_note_ids": [], "changed": [],
    }

    # DynamoDB first, then S3: anything the app created after this point is simply not in
    # the snapshot, so it can never be mistaken for "deleted in S3".
    snapshot = {i["path"]: i for i in list_items(user_id=user_id)}
    if _after_snapshot:
        _after_snapshot()

    remote: dict[str, dict[str, Any]] = {}
    for page in _s3().get_paginator("list_objects_v2").paginate(Bucket=_bucket(), Prefix=prefix):
        for obj in page.get("Contents") or []:
            raw = obj["Key"][len(prefix):]
            if not raw or raw.endswith("/") or raw.startswith(".vault/"):
                continue
            rel = nfc(raw)
            if ".." in rel.split("/"):
                continue
            prev = remote.get(rel)
            if prev is not None:  # NFC/NFD twin: keep the newest, report the other
                report["twins"].append(rel)
                if prev["LastModified"] >= obj["LastModified"]:
                    continue
            remote[rel] = obj

    present_dirs: set[str] = set()
    items = list(remote.items())
    for n, (rel, obj) in enumerate(items, start=1):
        if callable(on_progress) and n % 20 == 0:
            on_progress({"phase": "import", "file_i": n, "file_n": len(items),
                         "pct": int(n / max(len(items), 1) * 100),
                         "message": f"S3 변경을 확인하는 중… ({n}/{len(items)})"})
        key = obj["Key"]
        try:
            if Path(rel).name in HIDDEN_FOLDER_MARKERS:
                folder = nfc(str(Path(rel).parent))
                if folder and folder != ".":
                    present_dirs.add(folder)
                    if folder not in snapshot:
                        import_folder(folder, s3_key=key, created_at=_iso(obj["LastModified"]), user_id=user_id)
                        report["added"] += 1
                continue
            item = snapshot.get(rel)
            if item is not None and item.get("kind") == KIND_FOLDER:
                continue
            if item is not None and item.get("outbox"):
                # The app has an edit that is not in S3 yet: S3 is older, not newer.
                continue
            if item is not None:
                if item.get("s3_key") != key:
                    continue  # a twin spelling of a tracked key; the tracked one is handled
                same = (
                    item.get("md5") == _remote_md5(obj, key)
                    if item.get("md5")
                    else _int(item.get("size_bytes")) == _int(obj.get("Size"))
                )
                if same:
                    report["unchanged"] += 1
                    continue
            size = _int(obj.get("Size"))
            text: Optional[str] = None
            if is_text_path(rel) and size <= INLINE_MAX_BYTES:
                body = _s3().get_object(Bucket=_bucket(), Key=key)["Body"].read()
                try:
                    text = body.decode("utf-8")
                except UnicodeDecodeError:
                    text = None
            when = _iso(obj["LastModified"])
            result = import_object(
                rel,
                s3_key=key,
                size=size,
                text=text,
                etag=str(obj.get("ETag") or ""),
                created_at=when,
                updated_at=when,
                overwrite=True,
                expect_version=_int(item.get("version")) if item is not None else 0,
                user_id=user_id,
            )
            if result == "conflict":
                report["conflicts"].append(rel)
            elif result == "created":
                report["added"] += 1
                report["changed"].append(rel)
            else:
                report["updated"] += 1
                report["changed"].append(rel)
        except Exception as exc:  # noqa: BLE001 - keep importing the rest
            logger.warning("S3 import failed for %s: %s", rel, exc)
            report["errors"].append(f"{rel}: {exc}")

    if apply_deletes:
        for rel, item in snapshot.items():
            if item.get("outbox"):
                continue  # not mirrored yet: S3 simply has not caught up
            if item.get("kind") == KIND_FOLDER:
                if rel in present_dirs or any(r.startswith(rel + "/") for r in remote):
                    continue
            elif rel in remote:
                continue
            if item.get("s3_state") != "synced" or _int(item.get("s3_version")) != _int(item.get("version")):
                continue  # never confirmed in S3, so its absence proves nothing
            if _forget_item(item):
                report["deleted"] += 1
                if item.get("note_id"):
                    report["deleted_note_ids"].append(item["note_id"])
            else:
                report["conflicts"].append(rel)
    return report


def prepare_export(
    prefix: str = "",
    *,
    user_id: Optional[str] = None,
    on_progress: Optional[Callable[[dict[str, Any]], None]] = None,
) -> dict[str, Any]:
    """Before zipping/downloading: finish the S3 mirror, then complete the local copy."""
    flush = flush_outbox(user_id=user_id, include_future=True, on_progress=on_progress)
    cache = refresh_cache(prefix, include_files=True, prune=True, user_id=user_id, on_progress=on_progress)
    return {"ok": bool(flush.get("ok")), "flush": flush, "cache": cache}


def ingest_local(rel: str, *, user_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Bridge for legacy writers that wrote the local working copy first."""
    cleaned = _validate(rel)
    target = _cache_path(cleaned, user_id)
    if target is None or not target.is_file():
        return None
    name = Path(cleaned).name
    if name in HIDDEN_FOLDER_MARKERS:
        parent = nfc(str(Path(cleaned).parent))
        return mkdir(parent, user_id=user_id) if parent and parent != "." else None
    return put_file(cleaned, target, user_id=user_id)


def ingest_local_tree(rel_dir: str = "", *, user_id: Optional[str] = None) -> int:
    """Ingest every file under ``rel_dir`` from the local working copy."""
    root = _vault_root(user_id)
    if root is None:
        return 0
    base = root / nfc(rel_dir) if nfc(rel_dir) else root
    if not base.exists():
        return 0
    paths = [base] if base.is_file() else [p for p in base.rglob("*") if p.is_file()]
    count = 0
    for path in paths:
        rel = path.relative_to(root).as_posix()
        if rel.startswith(".vault/") or path.name.startswith(".") and path.name not in HIDDEN_FOLDER_MARKERS:
            continue
        if ingest_local(rel, user_id=user_id) is not None:
            count += 1
    return count


def requeue(paths: list[str], *, user_id: Optional[str] = None) -> int:
    """Mark items for another S3 mirror pass (repair after ``verify`` finds drift)."""
    count = 0
    for rel in paths:
        item = get(rel, user_id=user_id)
        if item is None or item.get("kind") == KIND_FILE:
            continue  # binaries live only in S3; nothing to re-push from DynamoDB
        try:
            _table().update_item(
                Key={"pk": item["pk"], "sk": item["sk"]},
                UpdateExpression="SET outbox = :o, outbox_at = :t, s3_state = :s",
                ConditionExpression=Attr("version").eq(_int(item.get("version"))),
                ExpressionAttributeValues={":o": "1", ":t": utc_now(), ":s": "pending"},
            )
            count += 1
        except ClientError:
            continue
    if count:
        schedule_flush()
    return count


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------


def verify(*, user_id: Optional[str] = None, deep: bool = True) -> dict[str, Any]:
    """Compare DynamoDB with the S3 originals (key set, size, md5 vs ETag)."""
    seg = _seg(user_id)
    prefix = f"{seg}/{vault_backend.S3_USER_VAULT_DIR}/"
    s3_objects: dict[str, dict[str, Any]] = {}
    paginator = _s3().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=_bucket(), Prefix=prefix):
        for obj in page.get("Contents") or []:
            rel = obj["Key"][len(prefix):]
            if not rel or rel.endswith("/") or rel.startswith(".vault/"):
                continue
            s3_objects[obj["Key"]] = obj

    items = list_items(user_id=user_id)
    missing_in_s3: list[str] = []
    size_mismatch: list[str] = []
    md5_mismatch: list[str] = []
    pending: list[str] = []
    claimed: set[str] = set()
    for item in items:
        key = item.get("s3_key")
        claimed.add(key)
        if item.get("outbox"):
            pending.append(item["path"])
            continue
        obj = s3_objects.get(key)
        if obj is None:
            missing_in_s3.append(item["path"])
            continue
        if item.get("kind") == KIND_FOLDER:
            continue
        if _int(obj.get("Size")) != _int(item.get("size_bytes")):
            size_mismatch.append(item["path"])
            continue
        etag = str(obj.get("ETag") or "").strip('"')
        if deep and item.get("md5") and "-" not in etag and etag != item["md5"]:
            md5_mismatch.append(item["path"])
    only_in_s3 = sorted(
        k[len(prefix):] for k in s3_objects if k not in claimed and not k.endswith("/" + FOLDER_MARKER)
    )
    ok = not (missing_in_s3 or size_mismatch or md5_mismatch or pending)
    return {
        "ok": ok,
        "dynamodb_items": len(items),
        "s3_objects": len(s3_objects),
        "pending": pending,
        "missing_in_s3": missing_in_s3,
        "size_mismatch": size_mismatch,
        "md5_mismatch": md5_mismatch,
        "only_in_s3": only_in_s3,
    }
