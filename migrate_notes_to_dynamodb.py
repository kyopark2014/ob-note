#!/usr/bin/env python3
"""One-time migration: copy every note in S3 into the DynamoDB note table.

Safe to re-run. S3 is only ever *read* (objects are never modified or deleted);
DynamoDB items are only created, and an existing item whose content differs is
reported as a conflict unless ``--overwrite`` is given.

    python migrate_notes_to_dynamodb.py --dry-run          # plan only
    python migrate_notes_to_dynamodb.py --create-table     # create table, then copy
    python migrate_notes_to_dynamodb.py --user a@b.com     # one account
    python migrate_notes_to_dynamodb.py --verify           # DynamoDB vs S3 report only

Existing ``note_id`` values are carried over from ``s3://bucket/{user}/notes.db``
so agent chat history (keyed by note_id) stays attached to the right note.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import tempfile
import unicodedata
from datetime import timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from botocore.exceptions import ClientError  # noqa: E402

from application import note_store, utils, vault_backend  # noqa: E402

logger = logging.getLogger("migrate_notes")

# Top-level prefixes in the bucket that are not user vaults.
NON_USER_PREFIXES = {"agentcore-sessions", "artifacts", "session-uploads", "vault", "documents"}
SETTINGS_DIR = ".vault/"


def _iso(dt) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def discover_users(s3, bucket: str) -> list[str]:
    users: list[str] = []
    resp = s3.list_objects_v2(Bucket=bucket, Delimiter="/")
    for common in resp.get("CommonPrefixes") or []:
        name = common["Prefix"].rstrip("/")
        if name in NON_USER_PREFIXES:
            continue
        probe = s3.list_objects_v2(Bucket=bucket, Prefix=f"{name}/vault/", MaxKeys=1)
        if probe.get("KeyCount"):
            users.append(name)
    return users


def load_registry(s3, bucket: str, seg: str) -> dict[str, dict[str, Any]]:
    """``{nfc_path: row}`` from the user's SQLite notes registry (for note_id continuity)."""
    candidates = [f"{seg}/notes.db"]
    rows: dict[str, dict[str, Any]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for key in candidates:
            dest = Path(tmp) / "notes.db"
            try:
                s3.download_file(bucket, key, str(dest))
            except ClientError:
                continue
            try:
                conn = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
                conn.row_factory = sqlite3.Row
                for r in conn.execute("SELECT note_id, title, path, created_at, updated_at FROM notes"):
                    rows[note_store.nfc(r["path"])] = dict(r)
                conn.close()
                break
            except sqlite3.Error as exc:
                logger.warning("could not read %s: %s", key, exc)
    return rows


def migrate_user(
    s3,
    bucket: str,
    seg: str,
    *,
    dry_run: bool = False,
    overwrite: bool = False,
    log=print,
) -> dict[str, Any]:
    prefix = f"{seg}/{vault_backend.S3_USER_VAULT_DIR}/"
    registry = load_registry(s3, bucket, seg)
    report: dict[str, Any] = {
        "user": seg,
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "conflicts": [],
        "folders": 0,
        "notes": 0,
        "files": 0,
        "bytes_inline": 0,
        "nfc_duplicates": [],
        "skipped_settings": 0,
        "registry_rows": len(registry),
        "registry_orphans": [],
        "errors": [],
    }

    # Gather objects; when NFC/NFD spellings collide keep the newest one.
    chosen: dict[str, dict[str, Any]] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents") or []:
            rel_raw = obj["Key"][len(prefix):]
            if not rel_raw or rel_raw.endswith("/"):
                continue
            if rel_raw.startswith(SETTINGS_DIR):
                report["skipped_settings"] += 1
                continue
            rel = note_store.nfc(rel_raw)
            if ".." in rel.split("/"):
                report["errors"].append(f"unsafe key skipped: {obj['Key']}")
                continue
            prev = chosen.get(rel)
            if prev is not None:
                report["nfc_duplicates"].append(
                    {"path": rel, "kept": prev["Key"] if prev["LastModified"] >= obj["LastModified"] else obj["Key"],
                     "ignored": obj["Key"] if prev["LastModified"] >= obj["LastModified"] else prev["Key"]}
                )
                if prev["LastModified"] >= obj["LastModified"]:
                    continue
            chosen[rel] = obj

    with vault_backend.user_scope(seg):
        for rel in sorted(chosen):
            obj = chosen[rel]
            name = Path(rel).name
            try:
                if name in note_store.HIDDEN_FOLDER_MARKERS:
                    parent = note_store.nfc(str(Path(rel).parent))
                    if parent and parent != ".":
                        out = note_store.import_folder(
                            parent, s3_key=obj["Key"], created_at=_iso(obj["LastModified"]), dry_run=dry_run
                        )
                        report["folders"] += 1
                        if out == "created":
                            report["created"] += 1
                        else:
                            report["unchanged"] += 1
                    continue

                text: Optional[str] = None
                size = int(obj["Size"])
                if note_store.is_text_path(rel) and size <= note_store.INLINE_MAX_BYTES:
                    body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
                    try:
                        text = body.decode("utf-8")
                    except UnicodeDecodeError:
                        text = None  # keep as a binary-style file; body stays in S3
                reg = registry.get(rel) or {}
                result = note_store.import_object(
                    rel,
                    s3_key=obj["Key"],
                    size=size,
                    text=text,
                    etag=str(obj.get("ETag") or ""),
                    note_id=reg.get("note_id"),
                    title=reg.get("title"),
                    created_at=reg.get("created_at") or _iso(obj["LastModified"]),
                    updated_at=reg.get("updated_at") or _iso(obj["LastModified"]),
                    overwrite=overwrite,
                    dry_run=dry_run,
                )
                if result == "conflict":
                    report["conflicts"].append(rel)
                else:
                    report[result] += 1
                if text is not None:
                    report["notes"] += 1
                    report["bytes_inline"] += len(text.encode("utf-8"))
                else:
                    report["files"] += 1
            except Exception as exc:  # noqa: BLE001 - keep migrating the rest
                logger.exception("failed to migrate %s", rel)
                report["errors"].append(f"{rel}: {exc}")

    report["registry_orphans"] = sorted(p for p in registry if p not in chosen)
    log(
        f"[{seg}] created={report['created']} updated={report['updated']} "
        f"unchanged={report['unchanged']} conflicts={len(report['conflicts'])} "
        f"notes={report['notes']} files={report['files']} folders={report['folders']} "
        f"errors={len(report['errors'])}" + (" (dry-run)" if dry_run else "")
    )
    return report


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", action="append", help="user folder in S3 (repeatable). Default: all vault users")
    ap.add_argument("--table", help="DynamoDB table (default: config.json dynamodb_table)")
    ap.add_argument("--bucket", help="S3 bucket (default: config.json s3_bucket)")
    ap.add_argument("--region", help="AWS region (default: config.json region)")
    ap.add_argument("--create-table", action="store_true", help="create the table first if missing")
    ap.add_argument("--dry-run", action="store_true", help="report what would be copied; write nothing")
    ap.add_argument("--overwrite", action="store_true", help="replace DynamoDB items whose content differs")
    ap.add_argument("--verify", action="store_true", help="only compare DynamoDB with S3 (no writes)")
    ap.add_argument("--report", help="write the JSON report to this path")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = utils.load_config()
    if args.table:
        os.environ["NOTES_DDB_TABLE"] = args.table
    if args.bucket:
        os.environ["VAULT_S3_BUCKET"] = args.bucket
    if args.region:
        os.environ["AWS_REGION"] = args.region
        cfg["region"] = args.region
    if not note_store.table_name():
        os.environ["NOTES_DDB_TABLE"] = note_store.DEFAULT_TABLE
    note_store.reset_clients()

    bucket = note_store._bucket()
    s3 = note_store._s3()
    print(f"bucket={bucket} table={note_store.table_name()} region={note_store._region()}")

    if args.create_table and not args.dry_run and not args.verify:
        print("table:", note_store.ensure_table())
    else:
        try:
            note_store._resource().meta.client.describe_table(TableName=note_store.table_name())
        except ClientError as exc:
            if args.dry_run:
                print("table does not exist yet (dry-run continues; use --create-table to create it)")
            else:
                print(f"table {note_store.table_name()} not found: {exc}. Re-run with --create-table.")
                return 2

    users = args.user or discover_users(s3, bucket)
    print("users:", users)
    reports: list[dict[str, Any]] = []
    exit_code = 0
    for seg in users:
        if args.verify:
            with vault_backend.user_scope(seg):
                v = note_store.verify()
            v["user"] = seg
            reports.append(v)
            print(
                f"[{seg}] verify ok={v['ok']} ddb={v['dynamodb_items']} s3={v['s3_objects']} "
                f"missing_in_s3={len(v['missing_in_s3'])} size={len(v['size_mismatch'])} "
                f"md5={len(v['md5_mismatch'])} only_in_s3={len(v['only_in_s3'])}"
            )
            exit_code |= 0 if v["ok"] else 1
            continue
        rep = migrate_user(s3, bucket, seg, dry_run=args.dry_run, overwrite=args.overwrite)
        if not args.dry_run:
            with vault_backend.user_scope(seg):
                v = note_store.verify()
            rep["verify"] = {k: v[k] for k in ("ok", "dynamodb_items", "s3_objects")}
            rep["verify"]["problems"] = {
                k: v[k] for k in ("missing_in_s3", "size_mismatch", "md5_mismatch", "only_in_s3") if v[k]
            }
            print(f"[{seg}] verify ok={v['ok']} ddb={v['dynamodb_items']} s3={v['s3_objects']}")
            exit_code |= 0 if v["ok"] and not rep["errors"] and not rep["conflicts"] else 1
        reports.append(rep)

    if args.report:
        Path(args.report).write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
        print("report written:", args.report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
