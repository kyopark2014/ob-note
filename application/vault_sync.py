"""Durable pending S3 vault ops + flush / incremental pull.

Pending ops survive process restart via ``.vault/pending_s3_ops.json`` (also
mirrored to S3 so ECS task replacement can resume). Sync-from-S3 is blocked
until the pending upload/delete queue is empty.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Optional

from application import vault_backend

logger = logging.getLogger("vault_sync")

_PENDING_NAME = "pending_s3_ops.json"
_queue_lock = threading.RLock()
_flush_lock = threading.Lock()
# Hide from S3-backed file tree (case-sensitive key listing)
HIDDEN_SKIP_NAMES = {".git", ".vault"}
# Marker files that represent empty folders (hidden from UI tree)
FOLDER_KEEP_NAMES = {".keep", ".gitkeep"}


def _pending_local_path() -> Path:
    return vault_backend.settings_dir() / _PENDING_NAME


def _pending_s3_key() -> str:
    return vault_backend.s3_prefix() + ".vault/" + _PENDING_NAME


def _empty_queue() -> dict[str, Any]:
    return {"version": 1, "ops": []}


def _load_local_queue() -> dict[str, Any]:
    path = _pending_local_path()
    if not path.is_file():
        return _empty_queue()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("ops"), list):
            return data
    except Exception:
        logger.exception("Failed to read pending queue %s", path)
    return _empty_queue()


def _save_local_queue(data: dict[str, Any]) -> None:
    path = _pending_local_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    raw = json.dumps(data, ensure_ascii=False, indent=2)
    tmp.write_text(raw + "\n", encoding="utf-8")
    tmp.replace(path)


def _mirror_pending_to_s3(data: dict[str, Any]) -> None:
    """Best-effort durable mirror of the pending queue itself."""
    if vault_backend.backend_mode() != "s3":
        return
    bucket, region = vault_backend.s3_bucket_and_region()
    if not bucket:
        return
    try:
        client = vault_backend._s3_client(region)
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        client.put_object(
            Bucket=bucket,
            Key=_pending_s3_key(),
            Body=body,
            ContentType="application/json",
        )
    except Exception:
        logger.exception("Failed to mirror pending queue to S3")


def _load_pending_from_s3() -> dict[str, Any]:
    if vault_backend.backend_mode() != "s3":
        return _empty_queue()
    bucket, region = vault_backend.s3_bucket_and_region()
    if not bucket:
        return _empty_queue()
    try:
        from botocore.exceptions import ClientError

        client = vault_backend._s3_client(region)
        obj = client.get_object(Bucket=bucket, Key=_pending_s3_key())
        raw = obj["Body"].read().decode("utf-8")
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("ops"), list):
            return data
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in {"NoSuchKey", "404", "NotFound"}:
            return _empty_queue()
        logger.debug("Remote pending queue read failed: %s", e)
    except Exception as e:
        logger.debug("No remote pending queue (or read failed): %s", e)
    return _empty_queue()


def _merge_ops(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge ops; later ops for the same path win. put_all collapses queue."""
    by_path: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    put_all: Optional[dict[str, Any]] = None
    for op in a + b:
        kind = op.get("op")
        if kind == "put_all":
            put_all = op
            by_path.clear()
            order.clear()
            continue
        path = (op.get("path") or "").strip()
        if not path or kind not in {"put", "delete"}:
            continue
        if path not in by_path:
            order.append(path)
        by_path[path] = op
    ops = [by_path[p] for p in order]
    if put_all is not None and not ops:
        return [put_all]
    if put_all is not None:
        # explicit path ops after put_all still matter; drop bare put_all
        return ops
    return ops


def load_queue(*, hydrate_from_s3: bool = False) -> dict[str, Any]:
    with _queue_lock:
        local = _load_local_queue()
        if hydrate_from_s3 and vault_backend.backend_mode() == "s3":
            remote = _load_pending_from_s3()
            merged_ops = _merge_ops(remote.get("ops") or [], local.get("ops") or [])
            data = {"version": 1, "ops": merged_ops}
            _save_local_queue(data)
            return data
        return local


def pending_count() -> int:
    return len(load_queue().get("ops") or [])


def _enqueue(op: dict[str, Any], *, mirror: bool = True) -> dict[str, Any]:
    with _queue_lock:
        data = _load_local_queue()
        ops = list(data.get("ops") or [])
        kind = op.get("op")
        if kind == "put_all":
            data = {"version": 1, "ops": [op]}
        else:
            path = (op.get("path") or "").strip()
            ops = [o for o in ops if not (o.get("path") == path and o.get("op") in {"put", "delete"})]
            ops = [o for o in ops if o.get("op") != "put_all"]
            ops.append(op)
            data = {"version": 1, "ops": ops}
        _save_local_queue(data)
        if mirror:
            _mirror_pending_to_s3(data)
        return data


def enqueue_put(rel_path: str, *, mirror: bool = True) -> dict[str, Any]:
    rel = (rel_path or "").replace("\\", "/").lstrip("/")
    if not rel or rel.startswith(".vault/pending"):
        return load_queue()
    return _enqueue(
        {
            "id": uuid.uuid4().hex[:12],
            "op": "put",
            "path": rel,
            "ts": time.time(),
        },
        mirror=mirror,
    )


def enqueue_delete(rel_path: str, *, mirror: bool = True) -> dict[str, Any]:
    rel = (rel_path or "").replace("\\", "/").lstrip("/")
    if not rel:
        return load_queue()
    return _enqueue(
        {
            "id": uuid.uuid4().hex[:12],
            "op": "delete",
            "path": rel,
            "ts": time.time(),
        },
        mirror=mirror,
    )


def enqueue_put_tree(rel_dir: str = "") -> dict[str, Any]:
    """Enqueue put for every file under rel_dir (or whole vault)."""
    root = vault_backend.vault_root()
    base = root / rel_dir if rel_dir else root
    if not base.exists():
        return load_queue()
    if base.is_file():
        return enqueue_put(base.relative_to(root).as_posix())
    data = load_queue()
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel == f".vault/{_PENDING_NAME}":
            continue
        data = enqueue_put(rel, mirror=False)
    _mirror_pending_to_s3(data)
    return data


def enqueue_delete_tree(rel_dir: str) -> dict[str, Any]:
    root = vault_backend.vault_root()
    base = root / rel_dir
    if not base.exists():
        return enqueue_delete(rel_dir)
    if base.is_file():
        return enqueue_delete(rel_dir)
    data = load_queue()
    for path in base.rglob("*"):
        if path.is_file():
            data = enqueue_delete(path.relative_to(root).as_posix(), mirror=False)
    # also mark the directory prefix (no-op on S3 for empty dirs)
    data = enqueue_delete(rel_dir, mirror=False)
    _mirror_pending_to_s3(data)
    return data


def _upload_file(
    client,
    bucket: str,
    prefix: str,
    root: Path,
    rel: str,
    *,
    casing_map: Optional[dict[str, str]] = None,
) -> bool:
    """Upload local file to S3 using S3's existing folder casing when present."""
    path = root / rel
    if not path.is_file():
        alt = _find_local_file_ci(root, rel)
        if alt is None:
            logger.warning("Pending put skipped; local missing: %s", rel)
            return True  # drop from queue
        path = alt
    if rel == f".vault/{_PENDING_NAME}":
        return True
    if casing_map is None:
        casing_map = _s3_top_level_casing_map(client, bucket, prefix)
    cleaned = rel.replace("\\", "/").lstrip("/")
    canon_rel = _remap_rel_to_s3_casing(cleaned, casing_map)
    key = prefix + canon_rel
    client.upload_file(str(path), bucket, key)
    # Drop accidental duplicate key with the other casing (Mac rename artifact).
    if canon_rel != cleaned:
        try:
            client.delete_object(Bucket=bucket, Key=prefix + cleaned)
        except Exception:
            pass
    return True


def _find_local_file_ci(root: Path, rel: str) -> Optional[Path]:
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    cur = root
    for part in parts:
        if not cur.exists():
            return None
        if cur.is_file():
            return None
        hit = None
        try:
            for child in cur.iterdir():
                if child.name.lower() == part.lower():
                    hit = child
                    break
        except OSError:
            return None
        if hit is None:
            return None
        cur = hit
    return cur if cur.is_file() else None


def _s3_top_level_casing_map(client: Any, bucket: str, prefix: str) -> dict[str, str]:
    """Map lowercase top-level name → exact casing as stored on S3.

    If both ``agent/`` and ``Agent/`` exist, prefer the casing with the newest object.
    """
    scores: dict[str, dict[str, float]] = {}  # low -> {Exact: newest_ts}
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents") or []:
            key = obj["Key"]
            if key.endswith("/"):
                continue
            rel = key[len(prefix) :] if key.startswith(prefix) else key
            if not rel or rel.startswith(".vault/"):
                continue
            top = rel.split("/", 1)[0]
            if not top:
                continue
            ts = 0.0
            lm = obj.get("LastModified")
            if lm is not None:
                ts = float(lm.timestamp())
            low = top.lower()
            prev = scores.setdefault(low, {})
            prev[top] = max(prev.get(top, 0.0), ts)
    out: dict[str, str] = {}
    for low, variants in scores.items():
        # newest casing wins; tie-break: lexicographic larger (Agent > agent often)
        best = max(variants.items(), key=lambda kv: (kv[1], kv[0]))
        out[low] = best[0]
    return out


def _remap_rel_to_s3_casing(rel: str, casing_map: dict[str, str]) -> str:
    cleaned = (rel or "").replace("\\", "/").lstrip("/")
    if not cleaned or cleaned.startswith(".vault/"):
        return cleaned
    parts = cleaned.split("/")
    top = parts[0]
    canon = casing_map.get(top.lower())
    if canon and canon != top:
        parts[0] = canon
        return "/".join(parts)
    return cleaned


def reconcile_s3_folder_casing(
    *,
    on_progress: Optional[Any] = None,
) -> dict[str, Any]:
    """If S3 has both ``agent/`` and ``Agent/``, keep one casing and merge objects.

    Winner = top-level prefix with the newest object (S3 vault truth after Mac
    case-insensitive sync artifacts). Loser keys are copied into the winner
    path when missing, then deleted.
    """
    if vault_backend.backend_mode() != "s3":
        return {"ok": False, "reason": "not s3", "merged": 0, "deleted": 0}
    bucket, region = vault_backend.s3_bucket_and_region()
    if not bucket:
        return {"ok": False, "reason": "no bucket", "merged": 0, "deleted": 0}
    prefix = vault_backend.s3_prefix()
    client = vault_backend._s3_client(region)

    if callable(on_progress):
        on_progress(
            {
                "phase": "flush",
                "message": "S3 폴더명 대소문자를 정리합니다…",
                "pct": 0,
            }
        )

    # Group objects by lowercase top-level folder
    groups: dict[str, dict[str, list[tuple[str, str, float]]]] = {}
    # low -> exact_top -> [(rel, key, ts)]
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents") or []:
            key = obj["Key"]
            if key.endswith("/"):
                continue
            rel = key[len(prefix) :] if key.startswith(prefix) else key
            if not rel or rel.startswith(".vault/"):
                continue
            top = rel.split("/", 1)[0]
            ts = 0.0
            lm = obj.get("LastModified")
            if lm is not None:
                ts = float(lm.timestamp())
            groups.setdefault(top.lower(), {}).setdefault(top, []).append((rel, key, ts))

    merged = 0
    deleted = 0
    for low, variants in groups.items():
        if len(variants) < 2:
            continue
        scored = []
        for top, items in variants.items():
            newest = max(t for _, _, t in items)
            scored.append((newest, top, items))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        keep_top = scored[0][1]
        keep_rels = {rel for rel, _, _ in scored[0][2]}
        logger.info(
            "S3 folder casing reconcile: keep %r discard %s",
            keep_top,
            [t for _, t, _ in scored[1:]],
        )
        for _newest, lose_top, items in scored[1:]:
            for rel, key, _ts in items:
                rest = rel[len(lose_top) :]  # includes leading / or empty
                dest_rel = keep_top + rest
                dest_key = prefix + dest_rel
                if dest_rel not in keep_rels:
                    try:
                        client.copy_object(
                            Bucket=bucket,
                            CopySource={"Bucket": bucket, "Key": key},
                            Key=dest_key,
                        )
                        keep_rels.add(dest_rel)
                        merged += 1
                    except Exception:
                        logger.exception("Failed to merge s3://%s/%s → %s", bucket, key, dest_key)
                try:
                    client.delete_object(Bucket=bucket, Key=key)
                    deleted += 1
                except Exception:
                    logger.exception("Failed to delete duplicate-case key %s", key)

    return {"ok": True, "merged": merged, "deleted": deleted}


def _delete_object(client, bucket: str, prefix: str, rel: str) -> bool:
    key = prefix + rel
    try:
        client.delete_object(Bucket=bucket, Key=key)
    except Exception:
        logger.exception("Failed to delete s3://%s/%s", bucket, key)
        return False
    return True


def _queued_delete_for(rel: str) -> bool:
    """True when the live queue already has a delete for this path.

    A flush snapshot can still contain an older put. Uploading it after a
    rename queued a delete is what resurrected ``Untitled.md`` on S3.
    """
    cleaned = (rel or "").replace("\\", "/").lstrip("/")
    if not cleaned:
        return False
    with _queue_lock:
        ops = list(_load_local_queue().get("ops") or [])
    return any(
        op.get("op") == "delete" and (op.get("path") or "").strip() == cleaned for op in ops
    )


def _commit_flushed_ops(snapshot: list[dict[str, Any]], failed: list[dict[str, Any]]) -> None:
    """Drop ops this flush finished, and keep anything enqueued while it ran.

    Replacing the queue with ``failed`` alone used to erase a rename's delete
    of the old filename when that delete landed mid-flush.
    """
    failed_ids = {id(op) for op in failed}
    succeeded = [op for op in snapshot if id(op) not in failed_ids]
    succ_ids = {op.get("id") for op in succeeded if op.get("id")}
    succ_idless = [op for op in succeeded if not op.get("id")]

    def _same(a: dict[str, Any], b: dict[str, Any]) -> bool:
        return (
            a.get("op") == b.get("op")
            and (a.get("path") or "") == (b.get("path") or "")
            and a.get("ts") == b.get("ts")
        )

    with _queue_lock:
        current = list(_load_local_queue().get("ops") or [])
        kept: list[dict[str, Any]] = []
        kept_ids: set[str] = set()
        for op in current:
            oid = op.get("id")
            if oid and oid in succ_ids:
                continue
            if not oid and any(_same(op, done) for done in succ_idless):
                continue
            kept.append(op)
            if oid:
                kept_ids.add(oid)
        for op in failed:
            oid = op.get("id")
            if oid and oid not in kept_ids:
                kept.append(op)
                kept_ids.add(oid)
            elif not oid and not any(_same(op, item) for item in kept):
                kept.append(op)
        data = {"version": 1, "ops": kept}
        _save_local_queue(data)
        _mirror_pending_to_s3(data)


def flush_pending_to_s3(
    *,
    on_progress: Optional[Any] = None,
) -> dict[str, Any]:
    """Apply all pending put/delete ops to S3. Safe across restarts."""
    if vault_backend.backend_mode() != "s3":
        return {"ok": False, "reason": f"backend={vault_backend.backend_mode()}", "flushed": 0}

    with _flush_lock:
        bucket, region = vault_backend.s3_bucket_and_region()
        if not bucket:
            return {"ok": False, "reason": "no bucket", "flushed": 0}
        prefix = vault_backend.s3_prefix()
        root = vault_backend.vault_root()
        client = vault_backend._s3_client(region)
        casing_map = _s3_top_level_casing_map(client, bucket, prefix)

        # Hydrate queue from S3 mirror in case local disk was wiped mid-flight
        data = load_queue(hydrate_from_s3=True)
        ops = list(data.get("ops") or [])
        if not ops:
            return {"ok": True, "flushed": 0, "remaining": 0}

        total = len(ops)
        flushed = 0
        remaining: list[dict[str, Any]] = []
        for i, op in enumerate(ops, start=1):
            kind = op.get("op")
            rel = (op.get("path") or "").strip()
            label = rel or (kind or "op")
            if callable(on_progress):
                on_progress(
                    {
                        "phase": "flush",
                        "file": label,
                        "file_i": i,
                        "file_n": total,
                        "pct": int(round((i - 1) / total * 100)),
                        "message": f"로컬 변경분을 S3에 업로드 중… ({i}/{total})",
                    }
                )
            try:
                if kind == "put_all":
                    files = [
                        p
                        for p in root.rglob("*")
                        if p.is_file()
                        and not p.relative_to(root).as_posix().startswith(".vault/")
                    ]
                    for j, path in enumerate(files, start=1):
                        rel_f = path.relative_to(root).as_posix()
                        if callable(on_progress):
                            on_progress(
                                {
                                    "phase": "flush",
                                    "file": rel_f,
                                    "file_i": j,
                                    "file_n": len(files),
                                    "pct": int(round((j - 1) / max(len(files), 1) * 100)),
                                    "message": f"전체 업로드 중… ({j}/{len(files)})",
                                }
                            )
                        _upload_file(
                            client, bucket, prefix, root, rel_f, casing_map=casing_map
                        )
                        flushed += 1
                    continue
                if not rel:
                    continue
                if kind == "put":
                    if _queued_delete_for(rel):
                        logger.info("Skipping put superseded by delete: %s", rel)
                        ok = True
                    else:
                        ok = _upload_file(
                            client, bucket, prefix, root, rel, casing_map=casing_map
                        )
                elif kind == "delete":
                    # Delete the requested path, plus casing and NFC/NFD spellings.
                    # A Korean folder stored under the other normalization kept
                    # Untitled.md after the editor retitled the note.
                    ok = True
                    seen: set[str] = set()
                    for cand in _path_key_candidates(rel):
                        variants = [cand]
                        canon = _remap_rel_to_s3_casing(cand, casing_map)
                        if canon not in variants:
                            variants.append(canon)
                        for variant in variants:
                            if variant in seen:
                                continue
                            seen.add(variant)
                            if not _delete_object(client, bucket, prefix, variant):
                                ok = False
                else:
                    ok = True
                if ok:
                    flushed += 1
                else:
                    remaining.append(op)
            except Exception:
                logger.exception("Pending op failed: %s", op)
                remaining.append(op)

        _commit_flushed_ops(ops, remaining)

        logger.info(
            "Flushed %d pending S3 ops (%d remaining)", flushed, len(remaining)
        )
        return {
            "ok": len(remaining) == 0,
            "flushed": flushed,
            "remaining": len(remaining),
            "pending": remaining,
        }


def list_remote_vault_rels(*, include_vault_meta: bool = False) -> list[str]:
    """Return case-sensitive vault-relative object keys from S3 (files only)."""
    if vault_backend.backend_mode() != "s3":
        return []
    bucket, region = vault_backend.s3_bucket_and_region()
    if not bucket:
        return []
    prefix = vault_backend.s3_prefix()
    client = vault_backend._s3_client(region)
    rels: list[str] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents") or []:
            key = obj["Key"]
            if key.endswith("/"):
                continue
            rel = key[len(prefix) :] if key.startswith(prefix) else key
            if not rel:
                continue
            if not include_vault_meta and (
                rel.startswith(".vault/") or rel.split("/", 1)[0] in HIDDEN_SKIP_NAMES
            ):
                continue
            if rel == f".vault/{_PENDING_NAME}":
                continue
            rels.append(rel)
    return rels


def _path_key_candidates(rel: str) -> list[str]:
    """Unique path spellings to try against local disk / S3 (NFC + NFD)."""
    cleaned = (rel or "").replace("\\", "/").lstrip("/")
    if not cleaned:
        return []
    out: list[str] = []
    for form in (cleaned, unicodedata.normalize("NFC", cleaned), unicodedata.normalize("NFD", cleaned)):
        if form and form not in out:
            out.append(form)
    return out


def ensure_local_file(rel: str) -> Optional[Path]:
    """Return an on-disk file for ``rel``, downloading from S3 when needed.

    In S3 mode the file tree is built from remote keys, so a note can appear in
    the sidebar before the working copy has been mirrored. Call this before
    read/view so the UI does not 404 on a file that only exists remotely yet.
    """
    candidates = _path_key_candidates(rel)
    if not candidates:
        return None

    for cand in candidates:
        found = vault_backend.find_vault_file(cand)
        if found is not None and found.is_file():
            return found

    if vault_backend.backend_mode() != "s3":
        return None

    bucket, region = vault_backend.s3_bucket_and_region()
    if not bucket:
        return None
    prefix = vault_backend.s3_prefix()
    client = vault_backend._s3_client(region)

    from botocore.exceptions import ClientError

    for cand in candidates:
        key = prefix + cand
        try:
            target = vault_backend.resolve_vault_path(cand)
        except ValueError:
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(bucket, key, str(target))
        except ClientError as exc:
            code = str((exc.response or {}).get("Error", {}).get("Code") or "")
            if code in {"404", "NoSuchKey", "NotFound"}:
                continue
            logger.warning("ensure_local_file download failed for %s: %s", cand, exc)
            continue
        except Exception as exc:
            logger.warning("ensure_local_file download failed for %s: %s", cand, exc)
            continue
        if target.is_file():
            logger.info("On-demand S3→local download: %s", cand)
            return target

    return None


def remote_file_exists(rel: str) -> bool:
    """True if any NFC/NFD spelling of ``rel`` exists as an S3 object."""
    if vault_backend.backend_mode() != "s3":
        return False
    bucket, region = vault_backend.s3_bucket_and_region()
    if not bucket:
        return False
    prefix = vault_backend.s3_prefix()
    client = vault_backend._s3_client(region)
    from botocore.exceptions import ClientError

    for cand in _path_key_candidates(rel):
        try:
            client.head_object(Bucket=bucket, Key=prefix + cand)
            return True
        except ClientError as exc:
            code = str((exc.response or {}).get("Error", {}).get("Code") or "")
            if code in {"404", "NoSuchKey", "NotFound", "403", "AccessDenied"}:
                continue
            logger.debug("remote_file_exists head failed for %s: %s", cand, exc)
            continue
        except Exception as exc:
            logger.debug("remote_file_exists head failed for %s: %s", cand, exc)
            continue
    return False


def build_tree_from_rels(rels: list[str]) -> list[dict[str, Any]]:
    """Build nested folder/file nodes from vault-relative keys (case-sensitive).

    Allows ``agent/`` and ``Agent/`` to appear as separate folders even when the
    local disk is case-insensitive (macOS APFS).
    """
    # node: {"name", "path", "type", "children"?: dict[name, node], "ext"?}
    root: dict[str, Any] = {"children": {}}

    for rel in rels:
        parts = [p for p in rel.replace("\\", "/").split("/") if p]
        if not parts:
            continue
        if parts[0] in HIDDEN_SKIP_NAMES:
            continue
        # ``Folder/.keep`` → create Folder node, skip the marker file itself
        skip_leaf = parts[-1] in FOLDER_KEEP_NAMES
        if skip_leaf and len(parts) == 1:
            continue
        cur_children: dict[str, Any] = root["children"]
        path_acc: list[str] = []
        for i, part in enumerate(parts):
            path_acc.append(part)
            path = "/".join(path_acc)
            is_last = i == len(parts) - 1
            if is_last:
                if skip_leaf:
                    break
                # file
                cur_children[part] = {
                    "name": part,
                    "path": path,
                    "type": "file",
                    "ext": Path(part).suffix.lower().lstrip("."),
                }
            else:
                if part not in cur_children or cur_children[part].get("type") != "folder":
                    cur_children[part] = {
                        "name": part,
                        "path": path,
                        "type": "folder",
                        "children": {},
                    }
                elif "children" not in cur_children[part]:
                    cur_children[part]["children"] = {}
                cur_children = cur_children[part]["children"]

    def finalize(children_map: dict[str, Any], folder_rel: str = "") -> list[dict[str, Any]]:
        from application import vault_order

        nodes = list(children_map.values())
        nodes = vault_order.apply_order(folder_rel, nodes)
        out: list[dict[str, Any]] = []
        for n in nodes:
            if n.get("type") == "folder":
                raw_kids = n.get("children") or {}
                child_rel = n.get("path") or ""
                out.append(
                    {
                        "name": n["name"],
                        "path": n["path"],
                        "type": "folder",
                        "children": finalize(raw_kids, child_rel) if isinstance(raw_kids, dict) else [],
                    }
                )
            else:
                out.append(
                    {
                        "name": n["name"],
                        "path": n["path"],
                        "type": "file",
                        "ext": n.get("ext") or "",
                    }
                )
        return out

    return finalize(root["children"], "")


def sync_from_s3_incremental(
    *,
    force: bool = False,
    on_progress: Optional[Any] = None,
) -> dict[str, Any]:
    """Make local vault match s3://…/{user}/vault/ (download + prune).

    S3 is the source of truth for object contents. Folder-name casing conflicts
    (``agent/`` vs ``Agent/``) are reconciled in ``sync_now`` before pull.
    """
    if vault_backend.backend_mode() != "s3":
        return {"ok": False, "reason": f"backend={vault_backend.backend_mode()}"}

    pending = pending_count()
    if pending:
        return {
            "ok": False,
            "reason": "pending_uploads",
            "pending": pending,
            "message": "Finish local→S3 pending ops before pulling from S3",
        }

    if not vault_backend._sync_lock.acquire(timeout=3.0):
        return {
            "ok": False,
            "reason": "sync_busy",
            "skipped": True,
            "message": "Another vault sync is already running",
        }
    try:
        bucket, region = vault_backend.s3_bucket_and_region()
        if not bucket:
            return {"ok": False, "reason": "no bucket"}
        prefix = vault_backend.s3_prefix()
        root = vault_backend.vault_root()
        root.mkdir(parents=True, exist_ok=True)
        client = vault_backend._s3_client(region)

        # Exact S3 relative paths (case-sensitive). agent/ and Agent/ stay separate.
        remote: dict[str, dict[str, Any]] = {}
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents") or []:
                key = obj["Key"]
                if key.endswith("/"):
                    continue
                rel = key[len(prefix) :] if key.startswith(prefix) else key
                if not rel or rel == f".vault/{_PENDING_NAME}":
                    continue
                # Do not mirror .vault settings/cache from S3 into the working copy
                # (Notes Graph HTML lives under .vault/cache/notes-graphify/).
                if rel.startswith(".vault/"):
                    continue
                remote[rel] = obj

        candidates: list[tuple[str, dict[str, Any]]] = []
        for rel, obj in remote.items():
            dest = root / rel
            s3_size = int(obj.get("Size") or 0)
            s3_mtime = obj.get("LastModified")
            s3_ts = s3_mtime.timestamp() if s3_mtime is not None else 0.0
            if not force and dest.is_file():
                st = dest.stat()
                if st.st_size == s3_size and st.st_mtime >= s3_ts - 1.0:
                    continue
            candidates.append((rel, obj))

        downloaded = 0
        total = max(len(candidates), 1)
        if not candidates:
            if callable(on_progress):
                on_progress(
                    {
                        "phase": "pull",
                        "file": None,
                        "file_i": 0,
                        "file_n": 0,
                        "pct": 50,
                        "message": "원격과 동일한 파일은 건너뛰고 정리합니다…",
                    }
                )
        for i, (rel, obj) in enumerate(candidates, start=1):
            if callable(on_progress):
                on_progress(
                    {
                        "phase": "pull",
                        "file": rel,
                        "file_i": i,
                        "file_n": len(candidates),
                        "pct": int(round((i - 1) / total * 70)),
                        "message": f"S3 → 로컬 미러링 중… ({i}/{len(candidates)})",
                    }
                )
            dest = root / rel
            s3_mtime = obj.get("LastModified")
            s3_ts = s3_mtime.timestamp() if s3_mtime is not None else 0.0
            dest.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(bucket, obj["Key"], str(dest))
            if s3_ts:
                try:
                    import os

                    os.utime(dest, (s3_ts, s3_ts))
                except OSError:
                    pass
            downloaded += 1

        if callable(on_progress):
            on_progress(
                {
                    "phase": "pull",
                    "file": None,
                    "pct": 90,
                    "message": "S3에 없는 로컬 파일을 정리합니다…",
                }
            )
        # Prune only paths that have no exact S3 key. Keep case variants separate:
        # if S3 has Agent/x.md, do not delete local agent/x.md when that exact key
        # also exists on S3.
        # Never wipe a populated local vault when the remote prefix is empty —
        # that usually means wrong user segment / fresh account, not "delete all".
        if remote:
            pruned = _prune_local_not_in_remote(root, set(remote.keys()))
        else:
            pruned = 0

        vault_backend.mark_synced()
        logger.info(
            "Mirror sync from s3://%s/%s downloaded=%d pruned=%d remote=%d",
            bucket,
            prefix,
            downloaded,
            pruned,
            len(remote),
        )
        if callable(on_progress):
            on_progress(
                {
                    "phase": "pull",
                    "file": None,
                    "file_i": downloaded,
                    "file_n": downloaded,
                    "pct": 100,
                    "message": (
                        f"미러 완료 · 내려받기 {downloaded}"
                        + (f" · 로컬삭제 {pruned}" if pruned else "")
                    ),
                }
            )
        return {
            "ok": True,
            "downloaded": downloaded,
            "pruned": pruned,
            "skipped": 0,
            "last_sync_at": vault_backend.last_sync_at(),
            "remote_files": len(remote),
        }
    finally:
        vault_backend._sync_lock.release()


def _prune_local_not_in_remote(root: Path, remote_exact: set[str]) -> int:
    """Delete local vault files that are not present on S3.

    Matching is exact (case-sensitive) first. If the local path only matches an
    S3 key ignoring case (macOS APFS), keep it so ``agent/`` vs ``Agent/``
    downloads are not deleted when the volume cannot store both spellings.

    Never prune ``.vault/`` (settings, pending queue, Notes Graph cache under
    ``.vault/cache/notes-graphify/``). Those are local/derived and are not
    mirrored as user notes — deleting them after Sync made Graph disappear.
    """
    pruned = 0
    remote_lower = {r.lower() for r in remote_exact}
    files: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        # Keep all local settings / graph artifacts / sync metadata.
        if rel == ".vault" or rel.startswith(".vault/"):
            continue
        if rel in remote_exact:
            continue
        if rel.lower() in remote_lower:
            # Case-insensitive volume: S3 has a differently-cased key for this path.
            continue
        files.append(path)

    for path in files:
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = str(path)
        try:
            path.unlink()
            pruned += 1
            logger.info("Pruned local file missing on S3: %s", rel)
        except OSError:
            logger.debug("Prune failed: %s", path, exc_info=True)

    # Remove empty directories (bottom-up), never remove root / .vault
    dirs = sorted(
        (p for p in root.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    )
    for d in dirs:
        try:
            rel = d.relative_to(root).as_posix()
        except ValueError:
            continue
        if rel == ".vault" or rel.startswith(".vault/"):
            continue
        try:
            next(d.iterdir())
        except StopIteration:
            try:
                d.rmdir()
            except OSError:
                pass
        except OSError:
            pass
    return pruned


def sync_now(
    *,
    force_download: bool = False,
    on_progress: Optional[Any] = None,
) -> dict[str, Any]:
    """Flush pending local→S3 ops, reconcile folder casing, then pull from S3."""
    if vault_backend.backend_mode() != "s3":
        return {
            "ok": False,
            "reason": f"backend={vault_backend.backend_mode()}",
            "message": "S3 sync only available when VAULT_S3_ENABLE=1",
        }
    if callable(on_progress):
        on_progress(
            {
                "phase": "flush",
                "message": "대기 중인 로컬 변경분을 확인합니다…",
                "pct": 0,
            }
        )
    flush = flush_pending_to_s3(on_progress=on_progress)
    if not flush.get("ok"):
        return {
            "ok": False,
            "phase": "flush",
            "flush": flush,
            "message": "Pending uploads not fully flushed; pull blocked",
        }
    # Collapse agent/ vs Agent/ (and similar) so S3 has one casing per folder.
    casing = reconcile_s3_folder_casing(on_progress=on_progress)
    if callable(on_progress):
        on_progress(
            {
                "phase": "pull",
                "message": "S3 vault 기준으로 로컬을 맞춥니다…",
                "pct": 0,
            }
        )
    pull = sync_from_s3_incremental(force=force_download, on_progress=on_progress)
    ok = bool(pull.get("ok"))
    if ok:
        downloaded = pull.get("downloaded") or 0
        pruned = pull.get("pruned") or 0
        flushed = flush.get("flushed") or 0
        parts = [f"동기화 완료 · 업로드 {flushed} · 내려받기 {downloaded}"]
        if pruned:
            parts.append(f"로컬삭제 {pruned}")
        deleted_case = casing.get("deleted") or 0
        if deleted_case:
            parts.append(f"대소문자정리 {deleted_case}")
        msg = " · ".join(parts)
    else:
        msg = pull.get("message") or "동기화에 실패했습니다."
    return {
        "ok": ok,
        "phase": "done",
        "flush": flush,
        "casing": casing,
        "pull": pull,
        "message": msg,
    }


def startup_sync() -> dict[str, Any]:
    """On boot: no global pull — per-user sync runs on first authenticated request."""
    return {
        "ok": True,
        "skipped": True,
        "reason": "per_user_sync",
        "message": "Per-user vault sync runs after sign-in",
    }


def queue_and_flush_put(rel_path: str) -> dict[str, Any]:
    enqueue_put(rel_path)
    schedule_flush_pending()
    return {"ok": True, "queued": True, "pending": pending_count()}


def queue_and_flush_delete(rel_path: str) -> dict[str, Any]:
    enqueue_delete(rel_path)
    schedule_flush_pending()
    return {"ok": True, "queued": True, "pending": pending_count()}


# ---------------------------------------------------------------------------
# Lightweight background flush (mutations return immediately)
# ---------------------------------------------------------------------------

_flush_bg_lock = threading.Lock()
_flush_bg_thread: Optional[threading.Thread] = None
_flush_bg_requested: set[str] = set()
_flush_bg_reconcile: set[str] = set()


def schedule_flush_pending(*, reconcile_casing: bool = False) -> dict[str, Any]:
    """Enqueue a background pending→S3 flush without blocking the request.

    Rapid mkdir/write/rename calls coalesce onto one worker. Local vault +
    pending queue are already durable on disk before this is called; pull from
    S3 stays blocked while pending ops remain.

    Captures the current vault user so the worker thread can restore scope
    (ContextVar does not propagate to bare threads).
    """
    global _flush_bg_thread
    if vault_backend.backend_mode() != "s3":
        return {"ok": False, "reason": f"backend={vault_backend.backend_mode()}"}

    user_id = vault_backend.current_user_id()
    if not user_id:
        return {"ok": False, "reason": "no vault user", "pending": pending_count()}

    with _flush_bg_lock:
        _flush_bg_requested.add(user_id)
        if reconcile_casing:
            _flush_bg_reconcile.add(user_id)
        if _flush_bg_thread is not None and _flush_bg_thread.is_alive():
            return {
                "ok": True,
                "queued": True,
                "pending": pending_count(),
                "message": "S3 flush already running",
            }

        def worker() -> None:
            global _flush_bg_thread
            try:
                while True:
                    with _flush_bg_lock:
                        users = sorted(_flush_bg_requested)
                        _flush_bg_requested.clear()
                        reconcile_users = set(_flush_bg_reconcile)
                        _flush_bg_reconcile.clear()
                    if not users:
                        with _flush_bg_lock:
                            _flush_bg_thread = None
                            return
                    for uid in users:
                        try:
                            with vault_backend.user_scope(uid):
                                flush_pending_to_s3()
                                if uid in reconcile_users:
                                    reconcile_s3_folder_casing()
                        except Exception:
                            logger.exception(
                                "Background S3 flush failed for user=%s", uid
                            )
                    with _flush_bg_lock:
                        if not _flush_bg_requested:
                            _flush_bg_thread = None
                            return
            finally:
                with _flush_bg_lock:
                    if _flush_bg_thread is threading.current_thread():
                        _flush_bg_thread = None

        _flush_bg_thread = threading.Thread(
            target=worker,
            name="vault-s3-flush",
            daemon=True,
        )
        _flush_bg_thread.start()
    return {
        "ok": True,
        "queued": True,
        "pending": pending_count(),
        "message": "S3 flush scheduled",
    }


# ---------------------------------------------------------------------------
# Background sync job + status (agentic-work SyncProgressModal compatible)
# ---------------------------------------------------------------------------

_STATUS_NAME = "sync_status.json"
_job_lock = threading.Lock()
_job_thread: Optional[threading.Thread] = None
_job_state: dict[str, Any] = {
    "status": "idle",
    "message": None,
    "error": None,
    "progress": None,
    "result": None,
    "updated_at": 0.0,
}


def _status_path() -> Path:
    return vault_backend.settings_dir() / _STATUS_NAME


def _persist_job_state() -> None:
    try:
        path = _status_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(_job_state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception:
        logger.debug("Failed to persist sync status", exc_info=True)


def _set_job(**kwargs: Any) -> None:
    with _job_lock:
        _job_state.update(kwargs)
        _job_state["updated_at"] = time.time()
        _persist_job_state()


def get_sync_status() -> dict[str, Any]:
    with _job_lock:
        # Recover stale UI if the worker thread died while status was "running"
        # (e.g. previous deadlock left pct=90 forever).
        if (
            _job_state.get("status") in {"queued", "running"}
            and (_job_thread is None or not _job_thread.is_alive())
        ):
            _job_state.update(
                {
                    "status": "error",
                    "message": "동기화가 중단되었습니다. 다시 Sync 해 주세요.",
                    "error": "sync_worker_dead",
                    "busy": False,
                }
            )
            _job_state["updated_at"] = time.time()
            _persist_job_state()
        state = dict(_job_state)
    state["mode"] = vault_backend.backend_mode()
    state["pending"] = pending_count()
    state["ops"] = (load_queue().get("ops") or [])[:50]
    busy = state.get("status") in {"queued", "running"}
    state["busy"] = busy
    return state


def _run_sync_job(*, force_download: bool = False, user_id: Optional[str] = None) -> None:
    def on_progress(info: dict[str, Any]) -> None:
        progress = {
            "file": info.get("file"),
            "file_i": info.get("file_i"),
            "file_n": info.get("file_n"),
            "pct": info.get("pct"),
            "phase": info.get("phase"),
        }
        _set_job(
            status="running",
            message=info.get("message") or "동기화 진행 중…",
            progress=progress,
            error=None,
        )

    try:
        with vault_backend.user_scope(user_id):
            _set_job(
                status="running",
                message="Vault 동기화를 시작합니다…",
                progress={"pct": 0, "phase": "start"},
                error=None,
                result=None,
            )
            result = sync_now(force_download=force_download, on_progress=on_progress)
            if result.get("ok"):
                _set_job(
                    status="ready",
                    message=result.get("message") or "동기화가 완료되었습니다.",
                    progress={
                        "pct": 100,
                        "phase": "done",
                        "file_i": result.get("pull", {}).get("downloaded"),
                        "file_n": result.get("pull", {}).get("downloaded"),
                    },
                    result=result,
                    error=None,
                )
                try:
                    from application import vault_index

                    vault_index.rebuild_index()
                except Exception:
                    logger.exception("Index rebuild after sync failed")
            else:
                _set_job(
                    status="error",
                    message=result.get("message") or "동기화에 실패했습니다.",
                    error=result.get("message") or result.get("reason") or "sync failed",
                    result=result,
                    progress=_job_state.get("progress"),
                )
    except Exception as e:
        logger.exception("Vault sync job failed")
        _set_job(
            status="error",
            message="동기화에 실패했습니다.",
            error=str(e),
            result=None,
        )
    finally:
        global _job_thread
        with _job_lock:
            _job_thread = None


def start_sync_job(*, force_download: bool = False) -> dict[str, Any]:
    """Queue a background sync job (no-op if already running)."""
    global _job_thread
    if vault_backend.backend_mode() != "s3":
        return {
            "status": "error",
            "ok": False,
            "message": "S3 sync requires VAULT_S3_ENABLE=1",
            "mode": vault_backend.backend_mode(),
        }
    user_id = vault_backend.current_user_id()
    if not user_id:
        return {
            "status": "error",
            "ok": False,
            "message": "vault user_id is required",
            "mode": vault_backend.backend_mode(),
        }
    with _job_lock:
        if _job_thread is not None and _job_thread.is_alive():
            return {
                "status": _job_state.get("status") or "running",
                "ok": True,
                "message": _job_state.get("message") or "이미 동기화가 진행 중입니다.",
                "busy": True,
                "pending": pending_count(),
            }
        _job_state.update(
            {
                "status": "queued",
                "message": "Vault 동기화를 백그라운드에서 시작합니다…",
                "error": None,
                "progress": {"pct": 0, "phase": "queued"},
                "result": None,
                "updated_at": time.time(),
            }
        )
        _persist_job_state()
        _job_thread = threading.Thread(
            target=_run_sync_job,
            kwargs={"force_download": force_download, "user_id": user_id},
            name="vault-s3-sync",
            daemon=True,
        )
        _job_thread.start()
    return {
        "status": "queued",
        "ok": True,
        "busy": True,
        "message": "Vault 동기화를 백그라운드에서 시작합니다…",
        "pending": pending_count(),
    }
