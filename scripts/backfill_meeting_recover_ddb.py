#!/usr/bin/env python3
"""Upsert S3 '(복구)' Meeting notes into DynamoDB for the vault UI."""

from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

for key in list(os.environ):
    if "proxy" in key.lower():
        os.environ.pop(key, None)

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")

import boto3  # noqa: E402
from application import note_store, utils  # noqa: E402

USER = "kyopark2014@gmail.com"


def main() -> int:
    try:
        note_store.mkdir("Meeting", user_id=USER)
        print("mkdir Meeting ok")
    except FileExistsError:
        print("Meeting exists")
    except Exception as exc:  # noqa: BLE001
        print(f"mkdir note: {type(exc).__name__}: {exc}")

    s3 = boto3.client("s3", region_name="us-west-2")
    bucket = utils.load_config()["s3_bucket"]
    prefix = f"{USER}/vault/Meeting/"
    keys: list[str] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents") or []:
            name = obj["Key"].split("/")[-1]
            if "(복구)" in name and name.endswith(".md"):
                keys.append(obj["Key"])

    print(f"recover keys {len(keys)}")
    for key in keys:
        rel = key.split("/vault/", 1)[-1]
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
        out = note_store.put_text(rel, body, user_id=USER, cache=False)
        print(
            f"upsert {rel} created={out.get('created')} size={out.get('size_bytes')}"
        )

    flushed = note_store.flush_outbox(user_id=USER)
    print(f"flushed {flushed}")

    items = note_store.list_items(prefix="Meeting/", user_id=USER)
    md = [i for i in items if str(i.get("path", "")).endswith(".md")]
    print(f"Meeting md notes in DDB: {len(md)}")
    for item in sorted(md, key=lambda x: x.get("path") or ""):
        print(f"  {item.get('size_bytes')} {item.get('path')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
