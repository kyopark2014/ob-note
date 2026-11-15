from __future__ import annotations

import threading
import unicodedata

import pytest

from tests.conftest import USER, s3_keys

PREFIX = "tester@example.com/vault/"


def test_create_read_update_keeps_note_id(aws):
    ns = aws
    first = ns.put_text("notes/a.md", "# Alpha\n\nbody")
    assert first["created"] is True and first["version"] == 1
    assert first["title"] == "Alpha"
    second = ns.put_text("notes/a.md", "# Alpha 2")
    assert second["created"] is False and second["version"] == 2
    assert second["note_id"] == first["note_id"]
    assert second["created_at"] == first["created_at"]
    assert ns.read_text("notes/a.md") == "# Alpha 2"
    assert ns.get_by_note_id(first["note_id"])["path"] == "notes/a.md"


def test_s3_is_updated_from_dynamodb_outbox(aws):
    ns = aws
    ns.put_text("n/a.md", "hello 안녕")
    assert s3_keys(PREFIX) == {}  # nothing mirrored yet, DynamoDB already authoritative
    assert ns.pending_count() == 1
    assert ns.read_text("n/a.md") == "hello 안녕"

    res = ns.flush_outbox()
    assert res["ok"] and res["flushed"] == 1 and res["remaining"] == 0
    assert s3_keys(PREFIX) == {PREFIX + "n/a.md": "hello 안녕".encode()}
    item = ns.get("n/a.md")
    assert item["s3_state"] == "synced" and "outbox" not in item
    assert int(item["s3_version"]) == int(item["version"])


def test_delete_removes_s3_original_via_tombstone(aws):
    ns = aws
    ns.put_text("d/a.md", "a")
    ns.put_text("d/sub/b.md", "b")
    ns.put_text("keep.md", "k")
    ns.flush_outbox()
    assert len(s3_keys(PREFIX)) == 3

    removed = ns.delete("d")
    assert {r["path"] for r in removed} == {"d/a.md", "d/sub/b.md"}
    assert ns.read_text("d/a.md") is None
    assert ns.pending_count() == 2  # two tombstones
    ns.flush_outbox()
    assert set(s3_keys(PREFIX)) == {PREFIX + "keep.md"}
    assert ns.pending_count() == 0


def test_delete_then_recreate_same_path_is_not_deleted_by_old_tombstone(aws):
    ns = aws
    ns.put_text("x.md", "old")
    ns.flush_outbox()
    ns.delete("x.md")
    ns.put_text("x.md", "new")  # tombstone for the same key is still queued
    ns.flush_outbox()
    assert s3_keys(PREFIX) == {PREFIX + "x.md": b"new"}
    assert ns.read_text("x.md") == "new"


def test_rename_file_preserves_note_id_and_moves_s3(aws):
    ns = aws
    created = ns.put_text("Untitled.md", "# Real Title")
    ns.flush_outbox()
    res = ns.rename("Untitled.md", "Real Title.md")
    assert res["moved"] == [("Untitled.md", "Real Title.md")]
    assert res["item"]["note_id"] == created["note_id"]
    assert ns.get("Untitled.md") is None
    assert ns.read_text("Real Title.md") == "# Real Title"
    ns.flush_outbox()
    assert set(s3_keys(PREFIX)) == {PREFIX + "Real Title.md"}


def test_rename_folder_moves_everything_and_binary_via_s3_copy(aws):
    ns = aws
    ns.put_text("old/a.md", "a")
    ns.put_text("old/deep/b.md", "b")
    ns.put_bytes("old/img.png", b"\x89PNG\r\n" + b"0" * 64)
    ns.mkdir("old/empty")
    ns.flush_outbox()

    res = ns.rename("old", "new")
    assert len(res["moved"]) == 4
    assert ns.list_paths(include_markers=False) == ["new/a.md", "new/deep/b.md", "new/img.png"]
    assert ns.read_bytes("new/img.png").startswith(b"\x89PNG")
    ns.flush_outbox()
    keys = set(s3_keys(PREFIX))
    assert keys == {
        PREFIX + "new/a.md",
        PREFIX + "new/deep/b.md",
        PREFIX + "new/img.png",
        PREFIX + "new/empty/.keep",
    }


def test_rename_onto_existing_note_reports_dropped_note_id(aws):
    ns = aws
    a = ns.put_text("a.md", "a")
    b = ns.put_text("b.md", "b")
    res = ns.rename("a.md", "b.md")
    assert res["dropped_note_ids"] == [b["note_id"]]
    assert ns.get("b.md")["note_id"] == a["note_id"]
    assert ns.read_text("b.md") == "a"


def test_rename_missing_returns_none(aws):
    assert aws.rename("nope.md", "x.md") is None


def test_edit_during_s3_upload_is_not_lost(aws, monkeypatch):
    """A write that lands while the mirror is uploading must still reach S3."""
    ns = aws
    ns.put_text("r.md", "v1")
    real_put = ns._s3_put
    state = {"first": True}

    def racing_put(key, data, rel):
        real_put(key, data, rel)
        if state["first"]:
            state["first"] = False
            ns.put_text("r.md", "v2")  # concurrent user edit

    monkeypatch.setattr(ns, "_s3_put", racing_put)
    ns.flush_outbox()
    # v1 was uploaded but the version check kept the newer outbox alive
    assert ns.pending_count() == 1
    ns.flush_outbox()
    assert ns.pending_count() == 0
    assert s3_keys(PREFIX) == {PREFIX + "r.md": b"v2"}


def test_delete_during_upload_does_not_resurrect_object(aws, monkeypatch):
    ns = aws
    ns.put_text("gone.md", "x")
    real_put = ns._s3_put

    def racing_put(key, data, rel):
        real_put(key, data, rel)
        ns.delete("gone.md")

    monkeypatch.setattr(ns, "_s3_put", racing_put)
    ns.flush_outbox()
    ns.flush_outbox()
    assert s3_keys(PREFIX) == {}
    assert ns.pending_count() == 0


def test_expected_version_conflict(aws):
    ns = aws
    ns.put_text("c.md", "1")
    ns.put_text("c.md", "2")
    with pytest.raises(ns.VersionConflict):
        ns.put_text("c.md", "stale", expected_version=1)
    assert ns.read_text("c.md") == "2"
    ns.put_text("c.md", "3", expected_version=2)
    assert ns.read_text("c.md") == "3"


def test_concurrent_appends_do_not_drop_text(aws):
    ns = aws
    ns.put_text("log.md", "start")
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            with __import__("application.vault_backend", fromlist=["x"]).user_scope(USER):
                ns.append_text("log.md", f"line{i}")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    text = ns.read_text("log.md")
    for i in range(8):
        assert f"line{i}" in text


def test_large_note_lives_in_s3_and_reads_back(aws):
    ns = aws
    big = "가" * 200_000  # ~600KB UTF-8, over the inline limit
    out = ns.put_text("big.md", big)
    assert out["size_bytes"] > ns.INLINE_MAX_BYTES
    item = ns.get("big.md", content=True)
    assert "content" not in item and item["s3_state"] == "synced"
    assert ns.pending_count() == 0
    assert ns.read_text("big.md") == big
    assert s3_keys(PREFIX)[PREFIX + "big.md"] == big.encode()


def test_nfd_and_nfc_paths_are_the_same_note(aws):
    ns = aws
    nfc = unicodedata.normalize("NFC", "도면/설계.md")
    nfd = unicodedata.normalize("NFD", nfc)
    ns.put_text(nfd, "one")
    ns.put_text(nfc, "two")
    assert ns.list_paths() == [nfc]
    assert ns.read_text(nfd) == "two"


def test_tree_listing_and_folder_markers(aws):
    ns = aws
    ns.mkdir("empty")
    ns.put_text("a/b.md", "b")
    assert ns.is_dir("empty") and ns.is_dir("a") and not ns.is_dir("a/b.md")
    assert ns.list_paths(include_markers=True) == ["a/b.md", "empty/.keep"]


def test_copy_gets_new_note_id(aws):
    ns = aws
    a = ns.put_text("a.md", "same")
    b = ns.copy("a.md", "b.md")
    assert b["note_id"] != a["note_id"]
    assert ns.read_text("b.md") == "same"


def test_verify_detects_drift_and_requeue_repairs(aws):
    ns = aws
    ns.put_text("v.md", "good")
    ns.flush_outbox()
    assert ns.verify()["ok"]

    import boto3

    boto3.client("s3", region_name="us-west-2").put_object(
        Bucket="storage-for-ob-note-test", Key=PREFIX + "v.md", Body=b"tampered!!"
    )
    report = ns.verify()
    assert not report["ok"] and report["size_mismatch"] == ["v.md"]

    assert ns.requeue(["v.md"]) == 1
    ns.flush_outbox()
    assert ns.verify()["ok"]
    assert s3_keys(PREFIX)[PREFIX + "v.md"] == b"good"


def test_failed_s3_upload_is_retried_not_lost(aws, monkeypatch):
    ns = aws
    ns.put_text("f.md", "data")
    real_put = ns._s3_put
    monkeypatch.setattr(ns, "_s3_put", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("s3 down")))
    res = ns.flush_outbox()
    assert res["failed"] == 1 and res["remaining"] == 1
    item = ns.get("f.md")
    assert item["s3_state"] == "error" and int(item["attempts"]) == 1
    # backoff pushes outbox_at into the future, so a normal pass skips it...
    assert ns.flush_outbox()["flushed"] == 0
    # ...until it is due (or forced), when it succeeds.
    monkeypatch.setattr(ns, "_s3_put", real_put)
    assert ns.flush_outbox(include_future=True)["ok"]
    assert s3_keys(PREFIX) == {PREFIX + "f.md": b"data"}


def test_users_are_isolated(aws):
    ns = aws
    from application import vault_backend

    ns.put_text("same.md", "mine")
    with vault_backend.user_scope("other@example.com"):
        assert ns.read_text("same.md") is None
        ns.put_text("same.md", "theirs")
        assert ns.read_text("same.md") == "theirs"
    assert ns.read_text("same.md") == "mine"
    ns.flush_outbox(user_id=None)
    ns.flush_outbox(user_id="other@example.com")
    keys = s3_keys()
    assert keys["tester@example.com/vault/same.md"] == b"mine"
    assert keys["other@example.com/vault/same.md"] == b"theirs"


def test_local_cache_is_derived_from_dynamodb(aws):
    ns = aws
    from application import vault_backend

    ns.put_text("c/a.md", "from ddb")
    root = vault_backend.vault_root()
    assert (root / "c/a.md").read_text() == "from ddb"  # write-through
    (root / "c/a.md").unlink()
    path = ns.materialize("c/a.md")
    assert path is not None and path.read_text() == "from ddb"
    # a stale local file is overwritten by DynamoDB, never the other way round
    path.write_text("stale local edit")
    ns._cache_versions.clear()
    ns.refresh_cache()
    assert path.read_text() == "from ddb"


def test_ingest_local_bridges_legacy_writers(aws):
    ns = aws
    from application import vault_backend

    root = vault_backend.vault_root()
    (root / "legacy").mkdir()
    (root / "legacy/n.md").write_text("# legacy", encoding="utf-8")
    (root / "legacy/pic.png").write_bytes(b"\x89PNG" + b"1" * 32)
    (root / "legacy/empty").mkdir()
    (root / "legacy/empty/.keep").write_text("")
    assert ns.ingest_local_tree("legacy") == 3
    assert ns.read_text("legacy/n.md") == "# legacy"
    assert ns.read_bytes("legacy/pic.png").startswith(b"\x89PNG")
    assert ns.is_dir("legacy/empty")
    ns.flush_outbox()
    assert ns.verify()["ok"]


def test_prepare_export_flushes_and_completes_local_copy(aws):
    ns = aws
    from application import vault_backend

    ns.put_text("e/a.md", "a")
    ns.put_bytes("e/img.png", b"\x89PNG" + b"2" * 16)
    root = vault_backend.vault_root()
    (root / "e/img.png").unlink(missing_ok=True)
    res = ns.prepare_export("e")
    assert res["ok"] and ns.pending_count() == 0
    assert (root / "e/img.png").read_bytes().startswith(b"\x89PNG")
    assert s3_keys(PREFIX)[PREFIX + "e/a.md"] == b"a"


def test_batch_get_text_returns_inline_bodies_only(aws):
    ns = aws
    for i in range(130):  # crosses the 100-key BatchGetItem limit
        ns.put_text(f"b/n{i}.md", f"body {i}")
    ns.put_bytes("b/pic.png", b"\x89PNG" + b"0" * 8)
    got = ns.batch_get_text([f"b/n{i}.md" for i in range(130)] + ["b/pic.png", "b/missing.md"])
    assert len(got) == 130 and got["b/n129.md"] == "body 129"


def test_ingest_of_identical_local_file_does_not_bump_version(aws):
    ns = aws
    from application import vault_backend

    ns.put_text("same.md", "# same")
    v1 = ns.get("same.md")["version"]
    assert (vault_backend.vault_root() / "same.md").read_text() == "# same"
    ns.ingest_local("same.md")
    assert ns.get("same.md")["version"] == v1  # idempotent: no churn, no extra S3 upload
    (vault_backend.vault_root() / "same.md").write_text("# changed", encoding="utf-8")
    ns.ingest_local("same.md")
    assert ns.get("same.md")["version"] == v1 + 1


def test_unique_vault_rel_path_checks_dynamodb_not_the_local_cache(aws):
    ns = aws
    from application import documents_support

    ns.put_text("OCR/Projects/Spec.md", "existing note")
    # The local cache has no such file, yet the name must still be treated as taken.
    from application import vault_backend

    (vault_backend.vault_root() / "OCR/Projects/Spec.md").unlink()
    assert documents_support.unique_vault_rel_path("OCR/Projects", "Spec") == "OCR/Projects/Spec-2.md"


def test_listing_is_metadata_only_and_never_shows_tombstones(aws):
    ns = aws
    ns.put_text("a.md", "body " * 50)
    ns.put_text("b/c.md", "x")
    ns.delete("a.md")  # leaves a tombstone item until S3 is mirrored
    listed = ns.list_items()
    assert [i["path"] for i in listed] == ["b/c.md"]
    assert all("content" not in i for i in listed)
    assert [i["path"] for i in ns.list_items("b")] == ["b/c.md"]
    assert "content" in ns.list_items(content=True)[0]
    assert ns.pending_count() >= 1  # the tombstone is still queued, just not listed


def test_listing_falls_back_when_table_predates_the_path_index(aws):
    """The live table was created before the LSI existed: it must keep working (slower)."""
    ns = aws
    client = ns._resource().meta.client
    name = ns.table_name()
    client.update_table(TableName=name, DeletionProtectionEnabled=False)
    client.delete_table(TableName=name)
    client.create_table(
        TableName=name,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "note_id", "AttributeType": "S"},
            {"AttributeName": "outbox", "AttributeType": "S"},
            {"AttributeName": "outbox_at", "AttributeType": "S"},
        ],
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        GlobalSecondaryIndexes=[
            {
                "IndexName": ns.NOTE_ID_INDEX,
                "KeySchema": [{"AttributeName": "note_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": ns.OUTBOX_INDEX,
                "KeySchema": [
                    {"AttributeName": "outbox", "KeyType": "HASH"},
                    {"AttributeName": "outbox_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
    )
    ns.put_text("a/one.md", "1")
    ns.put_text("two.md", "2")
    ns.delete("two.md")
    assert [i["path"] for i in ns.list_items()] == ["a/one.md"]
    assert ns._lsi_missing is True
    assert [i["path"] for i in ns.list_items("a")] == ["a/one.md"]
    assert all("content" not in i for i in ns.list_items())
    assert ns.flush_outbox()["ok"]
    assert set(s3_keys(PREFIX)) == {PREFIX + "a/one.md"}
