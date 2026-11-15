from __future__ import annotations

import sqlite3
import time
import unicodedata

import boto3

from tests.conftest import BUCKET, s3_keys

SEG = "kyopark@example.com"
PREFIX = f"{SEG}/vault/"


def _s3():
    return boto3.client("s3", region_name="us-west-2")


def _seed_notes_db(tmp_path, rows):
    db = tmp_path / "notes.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE notes (note_id TEXT PRIMARY KEY, title TEXT, path TEXT UNIQUE,"
        " size_bytes INTEGER, created_at TEXT, updated_at TEXT)"
    )
    conn.executemany("INSERT INTO notes VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    _s3().upload_file(str(db), BUCKET, f"{SEG}/notes.db")


def _load_script():
    import importlib

    return importlib.import_module("migrate_notes_to_dynamodb")


def test_migration_copies_notes_keeps_ids_and_never_touches_s3(aws, tmp_path):
    ns = aws
    mig = _load_script()
    nfc_name = unicodedata.normalize("NFC", "도면/설계 노트.md")
    nfd_key = PREFIX + unicodedata.normalize("NFD", nfc_name)  # macOS-style key in S3

    s3 = _s3()
    s3.put_object(Bucket=BUCKET, Key=PREFIX + "00-Inbox/a.md", Body="# A\n\n[[B]]".encode())
    s3.put_object(Bucket=BUCKET, Key=PREFIX + "b.md", Body=b"---\ntitle: Bee\n---\nbody")
    s3.put_object(Bucket=BUCKET, Key=nfd_key, Body="한글 본문".encode())
    s3.put_object(Bucket=BUCKET, Key=PREFIX + "attachments/pic.png", Body=b"\x89PNG" + b"9" * 50)
    s3.put_object(Bucket=BUCKET, Key=PREFIX + "empty/.keep", Body=b"")
    s3.put_object(Bucket=BUCKET, Key=PREFIX + "data.json", Body=b'{"k": 1}')
    s3.put_object(Bucket=BUCKET, Key=PREFIX + ".vault/app.json", Body=b"{}")
    s3.put_object(Bucket=BUCKET, Key=f"{SEG}/documents/ignored.pdf", Body=b"%PDF")
    _seed_notes_db(
        tmp_path,
        [
            ("nKEEP1", "Alpha", "00-Inbox/a.md", 10, "2025-01-01T00:00:00Z", "2025-02-02T00:00:00Z"),
            ("nKEEP2", "Bee", "b.md", 10, "2025-01-03T00:00:00Z", "2025-01-04T00:00:00Z"),
            ("nORPHAN", "Gone", "deleted.md", 1, "2025-01-01T00:00:00Z", "2025-01-01T00:00:00Z"),
        ],
    )
    before = s3_keys()

    report = mig.migrate_user(s3, BUCKET, SEG, log=lambda *_: None)
    assert report["errors"] == [] and report["conflicts"] == []
    assert s3_keys() == before, "migration must be read-only for S3"

    from application import vault_backend

    with vault_backend.user_scope(SEG):
        a = ns.get("00-Inbox/a.md", content=True)
        assert a["note_id"] == "nKEEP1" and a["title"] == "Alpha"
        assert a["created_at"] == "2025-01-01T00:00:00Z" and a["updated_at"] == "2025-02-02T00:00:00Z"
        assert a["content"] == "# A\n\n[[B]]" and a["s3_state"] == "synced" and "outbox" not in a
        assert ns.get("b.md")["note_id"] == "nKEEP2"

        # NFD S3 key is addressable by its NFC path, and the real key is remembered.
        hangul = ns.get(nfc_name, content=True)
        assert hangul["content"] == "한글 본문"
        assert hangul["s3_key"] == nfd_key and hangul["note_id"].startswith("n")

        png = ns.get("attachments/pic.png")
        assert png["kind"] == "file" and "content" not in png
        assert ns.read_bytes("attachments/pic.png").startswith(b"\x89PNG")
        assert ns.is_dir("empty")
        assert ns.get("data.json")["kind"] == "note"  # plain text is inline too, no note_id
        assert "note_id" not in ns.get("data.json")
        assert ns.get(".vault/app.json") is None  # app settings are left alone
        assert ns.pending_count() == 0
        assert ns.verify()["ok"], ns.verify()

    assert report["registry_orphans"] == ["deleted.md"]
    assert report["skipped_settings"] == 1


def test_migration_is_idempotent_and_reports_conflicts(aws):
    ns = aws
    mig = _load_script()
    s3 = _s3()
    s3.put_object(Bucket=BUCKET, Key=PREFIX + "n.md", Body=b"original")

    first = mig.migrate_user(s3, BUCKET, SEG, log=lambda *_: None)
    assert first["created"] == 1
    second = mig.migrate_user(s3, BUCKET, SEG, log=lambda *_: None)
    assert second["created"] == 0 and second["unchanged"] == 1

    from application import vault_backend

    with vault_backend.user_scope(SEG):
        item_id = ns.get("n.md")["note_id"]
        ns.put_text("n.md", "edited in app after migration")  # DynamoDB is now ahead of S3
    third = mig.migrate_user(s3, BUCKET, SEG, log=lambda *_: None)
    assert third["conflicts"] == ["n.md"]  # never silently clobbers newer app data
    with vault_backend.user_scope(SEG):
        assert ns.read_text("n.md") == "edited in app after migration"
    forced = mig.migrate_user(s3, BUCKET, SEG, overwrite=True, log=lambda *_: None)
    assert forced["updated"] == 1
    with vault_backend.user_scope(SEG):
        assert ns.read_text("n.md") == "original"
        assert ns.get("n.md")["note_id"] == item_id  # id survives an overwrite


def test_nfc_nfd_twins_keep_the_newest_and_are_reported(aws):
    ns = aws
    mig = _load_script()
    s3 = _s3()
    nfc_key = PREFIX + unicodedata.normalize("NFC", "한글.md")
    nfd_key = PREFIX + unicodedata.normalize("NFD", "한글.md")
    s3.put_object(Bucket=BUCKET, Key=nfd_key, Body="old".encode())
    time.sleep(1.1)  # S3 LastModified has 1s resolution
    s3.put_object(Bucket=BUCKET, Key=nfc_key, Body="new".encode())

    report = mig.migrate_user(s3, BUCKET, SEG, log=lambda *_: None)
    assert len(report["nfc_duplicates"]) == 1
    assert report["nfc_duplicates"][0]["kept"] == nfc_key
    from application import vault_backend

    with vault_backend.user_scope(SEG):
        assert ns.read_text("한글.md") == "new"
        # the ignored twin stays in S3 untouched and is surfaced by verify
        assert ns.verify()["only_in_s3"] == [unicodedata.normalize("NFD", "한글.md")]


def test_dry_run_writes_nothing(aws):
    ns = aws
    mig = _load_script()
    s3 = _s3()
    s3.put_object(Bucket=BUCKET, Key=PREFIX + "x.md", Body=b"x")
    report = mig.migrate_user(s3, BUCKET, SEG, dry_run=True, log=lambda *_: None)
    assert report["created"] == 1
    from application import vault_backend

    with vault_backend.user_scope(SEG):
        assert ns.list_items() == []


def test_discover_users_skips_non_user_prefixes(aws):
    mig = _load_script()
    s3 = _s3()
    for key in (
        f"{SEG}/vault/a.md",
        "other@example.com/vault/b.md",
        "artifacts/vault/x.md",
        "session-uploads/foo.bin",
        "vault/_public/shares_index.json",
        "agentcore-sessions/s/1.json",
        "nobody/notes.db",
    ):
        s3.put_object(Bucket=BUCKET, Key=key, Body=b"x")
    assert sorted(mig.discover_users(s3, BUCKET)) == ["kyopark@example.com", "other@example.com"]


def test_dry_run_works_before_the_table_exists(aws, monkeypatch):
    ns = aws
    mig = _load_script()
    s3 = _s3()
    s3.put_object(Bucket=BUCKET, Key=PREFIX + "x.md", Body=b"x")
    ns._resource().meta.client.update_table(TableName=ns.table_name(), DeletionProtectionEnabled=False)
    ns._resource().meta.client.delete_table(TableName=ns.table_name())
    report = mig.migrate_user(s3, BUCKET, SEG, dry_run=True, log=lambda *_: None)
    assert report["created"] == 1 and report["errors"] == []
