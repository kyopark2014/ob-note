"""Shared fixtures: moto-backed DynamoDB + S3 and an isolated vault directory."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BUCKET = "storage-for-ob-note-test"
TABLE = "dynamodb-for-ob-note-test"
USER = "tester@example.com"


def _make_moto_writes_atomic() -> None:
    """Emulate DynamoDB's atomic conditional writes inside moto.

    Real DynamoDB evaluates a ConditionExpression and applies the write as one
    atomic step. moto checks the condition and then stores the item as two steps
    without a lock, so two threads can both pass ``version = 1`` and the second
    silently overwrites the first (verified: concurrent appends lose text under
    moto but never when the write path is serialised). Without this the
    optimistic-locking tests would be flaky because of the mock, not the code.
    """
    import threading

    from moto.dynamodb.models import DynamoDBBackend

    if getattr(DynamoDBBackend, "_ob_note_atomic", False):
        return
    lock = threading.RLock()
    for name in ("put_item", "update_item", "delete_item", "transact_write_items"):
        original = getattr(DynamoDBBackend, name)

        def locked(self, *args, __orig=original, **kwargs):
            with lock:
                return __orig(self, *args, **kwargs)

        setattr(DynamoDBBackend, name, locked)
    DynamoDBBackend._ob_note_atomic = True


@pytest.fixture()
def aws(monkeypatch, tmp_path):
    """Fresh moto DynamoDB table + S3 bucket, vault dir under tmp_path."""
    from moto import mock_aws

    _make_moto_writes_atomic()
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("NOTES_DDB_TABLE", TABLE)
    monkeypatch.setenv("VAULT_S3_BUCKET", BUCKET)
    monkeypatch.setenv("VAULT_S3_ENABLE", "1")
    monkeypatch.setenv("VAULT_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("NOTES_DDB_DISABLE", raising=False)
    monkeypatch.delenv("VAULT_USE_MOUNT", raising=False)

    from application import note_store, utils, vault_backend

    monkeypatch.setattr(utils, "_config_cache", {"region": "us-west-2", "s3_bucket": BUCKET})
    # Mount detection must not pick up a real /mnt/vault on the dev machine.
    monkeypatch.setattr(vault_backend, "mount_available", lambda: False)
    monkeypatch.setattr(note_store, "_auto_flush", False)
    monkeypatch.setattr(note_store, "_lsi_missing", False)
    note_store.reset_clients()

    with mock_aws():
        import boto3

        boto3.client("s3", region_name="us-west-2").create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
        )
        note_store.reset_clients()
        note_store.ensure_table()
        with vault_backend.user_scope(USER):
            yield note_store
        note_store.reset_clients()


def s3_keys(prefix: str = "") -> dict[str, bytes]:
    import boto3

    client = boto3.client("s3", region_name="us-west-2")
    out: dict[str, bytes] = {}
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        for obj in page.get("Contents") or []:
            out[obj["Key"]] = client.get_object(Bucket=BUCKET, Key=obj["Key"])["Body"].read()
    return out
