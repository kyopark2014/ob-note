"""Sync's one-way S3 -> DynamoDB import (edits made in the AWS console)."""

from __future__ import annotations

import boto3

from tests.conftest import BUCKET, USER, s3_keys

PREFIX = "tester@example.com/vault/"


def s3():
    return boto3.client("s3", region_name="us-west-2")


def put(rel, body):
    s3().put_object(Bucket=BUCKET, Key=PREFIX + rel, Body=body if isinstance(body, bytes) else body.encode())


def settled(ns):
    assert ns.flush_outbox(include_future=True)["ok"]


def test_echo_of_our_own_writes_is_a_noop_and_never_loops(aws):
    ns = aws
    ns.put_text("a.md", "one")
    ns.put_text("b/c.md", "two")
    ns.mkdir("empty")
    settled(ns)
    versions = {i["path"]: i["version"] for i in ns.list_items()}
    before = s3_keys(PREFIX)
    for _ in range(3):  # repeated syncs / duplicate S3 events change nothing
        rep = ns.import_from_s3()
        assert (rep["added"], rep["updated"], rep["deleted"]) == (0, 0, 0)
        assert rep["conflicts"] == [] and rep["errors"] == []
    assert {i["path"]: i["version"] for i in ns.list_items()} == versions
    assert ns.pending_count() == 0
    assert s3_keys(PREFIX) == before


def test_new_edited_and_deleted_objects_are_imported(aws):
    ns = aws
    keep = ns.put_text("keep.md", "stay")
    edit = ns.put_text("edit.md", "v1")
    ns.put_text("gone.md", "bye")
    settled(ns)

    put("new.md", "# From console")
    put("img/pic.png", b"\x89PNG" + b"1" * 20)
    put("edit.md", "v2 edited in console")
    s3().delete_object(Bucket=BUCKET, Key=PREFIX + "gone.md")

    rep = ns.import_from_s3()
    assert (rep["added"], rep["updated"], rep["deleted"]) == (2, 1, 1), rep
    assert ns.read_text("new.md") == "# From console"
    assert ns.get("new.md")["note_id"].startswith("n")
    assert ns.read_bytes("img/pic.png").startswith(b"\x89PNG")
    assert ns.read_text("edit.md") == "v2 edited in console"
    assert ns.get("edit.md")["note_id"] == edit["note_id"]  # same note, same chat
    assert ns.get("edit.md")["version"] == 2
    assert ns.get("gone.md") is None
    assert ns.get("keep.md")["note_id"] == keep["note_id"]
    assert ns.pending_count() == 0, "imported changes must not be queued back to S3"
    assert ns.verify()["ok"]
    assert rep["deleted_note_ids"] and len(rep["deleted_note_ids"]) == 1


def test_app_edit_pending_in_outbox_wins_over_older_s3(aws):
    ns = aws
    ns.put_text("n.md", "v1")
    settled(ns)
    ns.put_text("n.md", "v2 typed in the app")  # not mirrored yet
    put("n.md", "console edit based on v1")
    rep = ns.import_from_s3()
    assert rep["updated"] == 0 and ns.read_text("n.md") == "v2 typed in the app"
    settled(ns)  # the app's version reaches S3, replacing the console edit
    assert s3_keys(PREFIX)[PREFIX + "n.md"] == b"v2 typed in the app"


def test_unmirrored_new_note_is_not_mistaken_for_a_console_delete(aws):
    ns = aws
    ns.put_text("fresh.md", "created just now")  # in DynamoDB, not in S3 yet
    rep = ns.import_from_s3()
    assert rep["deleted"] == 0 and ns.read_text("fresh.md") == "created just now"


def test_note_created_after_the_snapshot_is_not_deleted(aws):
    ns = aws
    ns.put_text("old.md", "x")
    settled(ns)

    def app_creates_and_mirrors_during_import():
        ns.put_text("racy.md", "typed during sync")
        settled(ns)

    rep = ns.import_from_s3(_after_snapshot=app_creates_and_mirrors_during_import)
    assert rep["deleted"] == 0 and rep["errors"] == []
    assert ns.read_text("racy.md") == "typed during sync"


def test_edit_during_import_makes_a_conflict_not_an_overwrite(aws):
    ns = aws
    ns.put_text("c.md", "v1")
    settled(ns)
    put("c.md", "console v2")

    def app_edits_between_snapshot_and_write():
        ns.put_text("c.md", "app v2")

    rep = ns.import_from_s3(_after_snapshot=app_edits_between_snapshot_and_write)
    assert rep["conflicts"] == ["c.md"] and rep["updated"] == 0
    assert ns.read_text("c.md") == "app v2"


def test_console_delete_of_folder_removes_its_notes_and_marker(aws):
    ns = aws
    ns.mkdir("proj")
    ns.put_text("proj/a.md", "a")
    ns.put_text("top.md", "t")
    settled(ns)
    for k in [PREFIX + "proj/a.md", PREFIX + "proj/.keep"]:
        s3().delete_object(Bucket=BUCKET, Key=k)
    rep = ns.import_from_s3()
    assert rep["deleted"] == 2
    assert ns.list_paths(include_markers=False) == ["top.md"] and not ns.is_dir("proj")


def test_delete_application_can_be_switched_off(aws):
    ns = aws
    ns.put_text("x.md", "x")
    settled(ns)
    s3().delete_object(Bucket=BUCKET, Key=PREFIX + "x.md")
    assert ns.import_from_s3(apply_deletes=False)["deleted"] == 0
    assert ns.read_text("x.md") == "x"


def test_console_created_nfd_key_lands_on_nfc_path(aws):
    import unicodedata

    ns = aws
    nfc = unicodedata.normalize("NFC", "한글/노트.md")
    nfd_key = PREFIX + unicodedata.normalize("NFD", nfc)
    s3().put_object(Bucket=BUCKET, Key=nfd_key, Body="본문".encode())
    assert ns.import_from_s3()["added"] == 1
    item = ns.get(nfc, content=True)
    assert item["content"] == "본문" and item["s3_key"] == nfd_key
    assert ns.import_from_s3()["unchanged"] == 1  # stable on the next pass
