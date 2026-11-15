"""Sync's unreferenced-media count must agree with the real Clearing scan."""

from __future__ import annotations

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


def seed(ns):
    ns.put_text("proj/a.md", "# A\n\n![x](used.png) and ![[wiki.png]] <img src=\"html.png\">")
    ns.put_text("proj/b.md", "see [other folder](../img/elsewhere.png)")  # other folder: not a reference
    ns.put_text("proj/.hidden/skip.md", "ignored")
    for name in ("used.png", "wiki.png", "html.png", "orphan.png", "kept.mp4", "clip.wav"):
        ns.put_bytes(f"proj/{name}", PNG)
    ns.put_bytes("img/elsewhere.png", PNG)
    ns.put_bytes("proj/.hidden/h.png", PNG)
    ns.put_text("loose.md", "![r](root.png)")
    ns.put_bytes("root.png", PNG)
    ns.put_bytes("notes.pdf", PNG)  # not a media suffix


def test_store_count_matches_scan_tree_exactly(aws):
    ns = aws
    from application import vault_backend, vault_clearing

    seed(ns)
    kept = ["proj/kept.mp4"]
    store = vault_clearing.count_unreferenced_from_store(kept)

    ns.refresh_cache(include_files=True, prune=False)  # complete disk copy for the real scan
    disk = vault_clearing.scan_tree(vault_backend.vault_root(), kept)

    assert store["unreferenced_count"] == len(disk["items"])
    assert store["media_count"] == disk["media_count"]
    assert store["kept_count"] == disk["kept_count"]
    assert sorted(i["path"] for i in disk["items"]) == ["img/elsewhere.png", "proj/clip.wav", "proj/orphan.png"]


def test_counting_downloads_nothing_and_deletes_nothing(aws, monkeypatch):
    ns = aws
    from application import vault_clearing

    seed(ns)
    before = {i["path"] for i in ns.list_items()}
    monkeypatch.setattr(ns, "_s3_read", lambda *_: (_ for _ in ()).throw(AssertionError("downloaded media")))
    monkeypatch.setattr(ns, "materialize", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("materialized")))
    vault_clearing.count_unreferenced_from_store([])
    assert {i["path"] for i in ns.list_items()} == before


def test_sync_message_reports_the_count_without_deleting(aws):
    ns = aws
    from application import vault_sync

    seed(ns)
    result = vault_sync.sync_now()
    assert result["ok"] is True
    assert result["clearing"]["unreferenced_count"] == 4  # nothing marked "kept" here: kept.mp4 counts
    assert "참조되지 않은 미디어 4개 — Clearing에서 확인하세요" in result["message"]
    assert ns.get("proj/orphan.png") is not None  # Sync only reports
