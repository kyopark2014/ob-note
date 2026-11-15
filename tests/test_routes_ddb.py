"""End-to-end: the real /api/files router running in DynamoDB mode (moto AWS)."""

from __future__ import annotations

import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import USER, s3_keys

PREFIX = "tester@example.com/vault/"


@pytest.fixture()
def client(aws, monkeypatch):
    from application import vault_backend
    from application.api import routes_files

    def fake_user(request) -> str:
        vault_backend.set_current_user_id(USER)
        return USER

    monkeypatch.setattr(routes_files, "require_user_id", fake_user)
    app = FastAPI()
    app.include_router(routes_files.router)
    with TestClient(app) as c:
        c.ns = aws
        yield c


def flush(client):
    res = client.ns.flush_outbox(include_future=True)
    assert res["ok"], res


def tree_paths(client) -> list[str]:
    out: list[str] = []

    def walk(nodes):
        for n in nodes:
            if n["type"] == "folder":
                out.append(n["path"] + "/")
                walk(n["children"])
            else:
                out.append(n["path"])

    walk(client.get("/api/files/tree").json()["children"])
    return sorted(out)


def test_write_read_tree_and_s3_mirror(client):
    r = client.put("/api/files/write", json={"path": "notes/hello.md", "content": "# Hello\n\nworld [[Other]]"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] is True and body["title"] == "Hello" and body["note_id"].startswith("n")

    got = client.get("/api/files/read", params={"path": "notes/hello.md"}).json()
    assert got["content"] == "# Hello\n\nworld [[Other]]"
    assert got["note_id"] == body["note_id"] and got["word_count"] >= 2

    tree = client.get("/api/files/tree").json()
    assert tree["source"] == "dynamodb"
    node = tree["children"][0]["children"][0]
    assert node["path"] == "notes/hello.md" and node["note_id"] == body["note_id"]

    assert s3_keys(PREFIX) == {}  # S3 follows DynamoDB asynchronously...
    flush(client)
    assert s3_keys(PREFIX)[PREFIX + "notes/hello.md"] == b"# Hello\n\nworld [[Other]]"  # ...but always arrives


def test_editor_retitle_moves_untitled_and_keeps_note_id(client):
    first = client.put("/api/files/write", json={"path": "Untitled.md", "content": "# Untitled\n"}).json()
    flush(client)
    second = client.put(
        "/api/files/write",
        json={"path": "Untitled.md", "content": "# My Real Title\n\nbody", "sync_filename": True},
    ).json()
    assert second["path"] == "My Real Title.md" and second["renamed_from"] == "Untitled.md"
    assert second["note_id"] == first["note_id"]
    assert tree_paths(client) == ["My Real Title.md"]
    assert client.get("/api/files/read", params={"path": "Untitled.md"}).status_code == 404
    flush(client)
    assert set(s3_keys(PREFIX)) == {PREFIX + "My Real Title.md"}


def test_append_creates_and_extends(client):
    assert client.post("/api/files/append", json={"path": "log.md", "content": "one"}).status_code == 200
    client.post("/api/files/append", json={"path": "log.md", "content": "two"})
    assert client.get("/api/files/read", params={"path": "log.md"}).json()["content"] == "one\ntwo"
    missing = client.post("/api/files/append", json={"path": "nope.md", "content": "x", "create": False})
    assert missing.status_code == 404


def test_mkdir_rename_folder_copy_duplicate_delete(client):
    assert client.post("/api/files/mkdir", json={"path": "proj/empty"}).status_code == 200
    client.put("/api/files/write", json={"path": "proj/a.md", "content": "# A"})
    client.put("/api/files/write", json={"path": "proj/sub/b.md", "content": "# B"})
    note_a = client.get("/api/files/read", params={"path": "proj/a.md"}).json()["note_id"]
    assert tree_paths(client) == ["proj/", "proj/a.md", "proj/empty/", "proj/sub/", "proj/sub/b.md"]

    r = client.post("/api/files/rename", json={"from_path": "proj", "to_path": "renamed"})
    assert r.status_code == 200, r.text
    assert tree_paths(client) == ["renamed/", "renamed/a.md", "renamed/empty/", "renamed/sub/", "renamed/sub/b.md"]
    assert client.get("/api/files/read", params={"path": "renamed/a.md"}).json()["note_id"] == note_a

    c = client.post("/api/files/copy", json={"from_path": "renamed/a.md", "to_path": "renamed/a-copy.md"}).json()
    assert c["overwritten"] is False
    d = client.post("/api/files/duplicate", json={"path": "renamed/a.md"}).json()
    assert d["to"] == "renamed/a 2.md"
    dup = client.post("/api/files/duplicate", json={"path": "renamed/sub"}).json()
    assert dup["to"] == "renamed/sub 2"
    assert "renamed/sub 2/b.md" in tree_paths(client)
    ids = {
        client.get("/api/files/read", params={"path": p}).json()["note_id"]
        for p in ("renamed/a.md", "renamed/a-copy.md", "renamed/a 2.md")
    }
    assert len(ids) == 3  # copies/duplicates are new notes

    flush(client)
    assert PREFIX + "renamed/a.md" in s3_keys(PREFIX) and PREFIX + "proj/a.md" not in s3_keys(PREFIX)

    assert client.post("/api/files/delete", json={"path": "renamed"}).status_code == 200
    assert tree_paths(client) == []
    assert client.post("/api/files/delete", json={"path": "renamed"}).status_code == 404
    flush(client)
    assert s3_keys(PREFIX) == {}


def test_rename_note_syncs_h1_to_new_filename(client):
    client.put("/api/files/write", json={"path": "old.md", "content": "# Old Name\n\ntext"})
    client.post("/api/files/rename", json={"from_path": "old.md", "to_path": "New Name.md"})
    got = client.get("/api/files/read", params={"path": "New Name.md"}).json()
    assert got["content"].startswith("# New Name") and got["title"] == "New Name"


def test_upload_binary_then_raw_and_list(client):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 128
    r = client.post(
        "/api/files/upload",
        data={"path": "attachments/pic.png"},
        files={"file": ("pic.png", png, "image/png")},
    )
    assert r.status_code == 200, r.text
    raw = client.get("/api/files/raw", params={"path": "attachments/pic.png"})
    assert raw.status_code == 200 and raw.content == png
    # binaries go straight to S3 (their home); no outbox needed
    assert s3_keys(PREFIX)[PREFIX + "attachments/pic.png"] == png

    client.put("/api/files/write", json={"path": "n.md", "content": "# N\n\npreview text"})
    listing = client.get("/api/files/list", params={"ext": "md"}).json()
    assert listing["count"] == 1 and listing["files"][0]["path"] == "n.md"
    assert "preview text" in listing["files"][0]["body_preview"]
    everything = client.get("/api/files/list", params={"ext": "*"}).json()
    assert {f["path"] for f in everything["files"]} == {"n.md", "attachments/pic.png"}


def test_search_and_backlinks_use_dynamodb(client):
    client.put("/api/files/write", json={"path": "a.md", "content": "# Alpha\n\nlinks to [[Beta]]"})
    client.put("/api/files/write", json={"path": "b.md", "content": "# Beta\n\nunique-needle-xyz"})
    from application import vault_index

    hits = vault_index.search("unique-needle-xyz")
    assert [h["path"] for h in hits] == ["b.md"]
    assert client.get("/api/files/read", params={"path": "b.md"}).json()["backlinks"][0]["path"] == "a.md"
    client.post("/api/files/delete", json={"path": "b.md"})
    assert vault_index.search("unique-needle-xyz") == []


def test_notes_registry_endpoint(client):
    client.put("/api/files/write", json={"path": "x.md", "content": "# X"})
    notes = client.get("/api/files/notes").json()
    assert notes["count"] == 1 and notes["notes"][0]["path"] == "x.md"


def test_sync_endpoint_flushes_then_imports_s3_console_edits(client):
    client.put("/api/files/write", json={"path": "s.md", "content": "# S"})
    import boto3

    s3 = boto3.client("s3", region_name="us-west-2")
    # Edits made directly in the AWS console while the app is running.
    s3.put_object(Bucket="storage-for-ob-note-test", Key=PREFIX + "from-console.md", Body=b"# Console note")
    started = client.post("/api/files/sync").json()
    assert started["ok"] is True
    for _ in range(100):
        status = client.get("/api/files/sync").json()
        if not status["busy"]:
            break
        time.sleep(0.1)
    assert status["status"] == "ready", status
    assert "S3 직접수정 반영 추가 1" in status["message"], status["message"]
    assert tree_paths(client) == ["from-console.md", "s.md"]
    assert client.get("/api/files/read", params={"path": "from-console.md"}).json()["content"] == "# Console note"
    assert client.ns.pending_count() == 0  # imported, and nothing queued back to S3


def test_reads_are_never_torn_by_concurrent_writes(client):
    """The failure that motivated this change: readers must see whole notes only."""
    versions = [f"# V{i}\n" + ("line\n" * 400) + f"END{i}\n" for i in range(25)]
    client.put("/api/files/write", json={"path": "busy.md", "content": versions[0]})
    stop = threading.Event()
    bad: list[str] = []

    def reader():
        from application import vault_backend

        while not stop.is_set():
            vault_backend.set_current_user_id(USER)
            r = client.get("/api/files/read", params={"path": "busy.md"})
            if r.status_code != 200:
                bad.append(f"status {r.status_code}")
                continue
            text = r.json()["content"]
            head = text.split("\n", 1)[0]  # '# V12'
            if not text.endswith(f"END{head[3:]}\n"):
                bad.append(f"torn read: {head!r} ... {text[-8:]!r}")

    threads = [threading.Thread(target=reader) for _ in range(3)]
    [t.start() for t in threads]
    for v in versions[1:]:
        client.put("/api/files/write", json={"path": "busy.md", "content": v})
    stop.set()
    [t.join() for t in threads]
    assert bad == []
    assert client.get("/api/files/read", params={"path": "busy.md"}).json()["content"] == versions[-1]
