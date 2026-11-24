"""UI prefs: DynamoDB META#ui + /api/prefs routes."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.conftest import USER


@pytest.fixture()
def client(aws, monkeypatch):
    from application import vault_backend
    from application.api import routes_prefs

    def fake_user(request) -> str:
        vault_backend.set_current_user_id(USER)
        return USER

    monkeypatch.setattr(routes_prefs, "require_user_id", fake_user)
    app = FastAPI()
    app.include_router(routes_prefs.router)
    with TestClient(app) as c:
        c.ns = aws
        yield c


def test_prefs_empty_then_roundtrip(client):
    empty = client.get("/api/prefs")
    assert empty.status_code == 200
    assert empty.json() == {}

    body = {
        "pinnedPaths": ["a.md", "folder/b.md"],
        "treeSort": "name",
        "theme": "light",
        "layoutMode": "desktop",
        "showImages": True,
        "agentModel": "Claude 5.0 Sonnet",
        "lastNotePath": "a.md",
        "openFolders": ["folder"],
        "sidebarWidth": 320,
        "agentWidth": 400,
        "pinnedListOpen": False,
        "recentListOpen": True,
        "skipConfirm": {"delete-file": True},
    }
    put = client.put("/api/prefs", json=body)
    assert put.status_code == 200, put.text
    saved = put.json()["prefs"]
    assert saved["pinnedPaths"] == ["a.md", "folder/b.md"]
    assert saved["treeSort"] == "name"
    assert saved["theme"] == "light"
    assert saved["showImages"] is True
    assert saved["skipConfirm"]["delete-file"] is True
    assert "updated_at" in saved

    got = client.get("/api/prefs").json()
    assert got["pinnedPaths"] == ["a.md", "folder/b.md"]
    assert got["treeSort"] == "name"
    assert got["updated_at"] == saved["updated_at"]

    item = client.ns._table().get_item(
        Key={"pk": f"USER#{USER}", "sk": "META#ui"},
        ConsistentRead=True,
    ).get("Item")
    assert item is not None
    assert item["kind"] == "prefs"
    assert item["prefs"]["pinnedPaths"] == ["a.md", "folder/b.md"]


def test_prefs_replace_clears_fields(client):
    client.put(
        "/api/prefs",
        json={"pinnedPaths": ["keep.md"], "lastNotePath": "keep.md", "theme": "dark"},
    )
    client.put("/api/prefs", json={"theme": "light", "pinnedPaths": []})
    got = client.get("/api/prefs").json()
    assert got["theme"] == "light"
    assert got["pinnedPaths"] == []
    assert "lastNotePath" not in got


def test_note_store_helpers(aws):
    assert aws.get_ui_prefs() == {}
    saved = aws.put_ui_prefs({"pinnedPaths": ["x.md"], "treeSort": "modified"})
    assert saved["pinnedPaths"] == ["x.md"]
    assert aws.get_ui_prefs()["pinnedPaths"] == ["x.md"]
