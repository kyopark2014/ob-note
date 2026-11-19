#!/usr/bin/env python3
"""Manual / integration test for search-note (vault Notes graph query).

Calls ``search_note.search_note`` against the live vault API with question
``OIDC``. Default actor is the vault account from workspace rules.

Usage:
  cd MCP/search-note
  python test_search_note.py
  python test_search_note.py --question OIDC
  USER_ID=other@example.com python test_search_note.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

import search_note as search_note_lib
import vault_client as vault

DEFAULT_ACTOR_ID = "kyopark2014@gmail.com"
DEFAULT_QUESTION = "OIDC"


def _resolve_actor_id(cli: str | None) -> str:
    for candidate in (
        cli,
        os.environ.get("USER_ID"),
        os.environ.get("CURRENT_USER_ID"),
        os.environ.get("ACTOR_ID"),
    ):
        value = (candidate or "").strip()
        if value:
            return value
    return DEFAULT_ACTOR_ID


def _print_result(result: dict) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))


def test_health(actor_id: str) -> bool:
    print("\n=== 1) vault health ===")
    print(f"OB_DOCS_URL / base: {vault.vault_base_url()}")
    try:
        payload = vault.api_request("GET", "/api/health", user_id=actor_id)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return True
    except Exception as e:
        print(f"FAIL health: {e}")
        return False


def test_fulltext_compare(actor_id: str, question: str) -> None:
    """Show GET /api/search hits so empty graph results are easier to interpret."""
    print("\n=== 2b) full-text /api/search (compare) ===")
    try:
        payload = vault.api_request(
            "GET",
            "/api/search",
            user_id=actor_id,
            query={"q": question, "limit": 5},
        )
        results = (payload or {}).get("results") or []
        print(f"full-text hits: {len(results)}")
        for i, hit in enumerate(results, 1):
            path = hit.get("path") or "?"
            title = hit.get("title") or Path(str(path)).stem
            snippet = (hit.get("snippet") or "").replace("\n", " ")[:120]
            print(f"  [{i}] {title} ({path})")
            if snippet:
                print(f"      {snippet}…")
        if results:
            print(
                "NOTE: full-text found notes; empty graph search usually means "
                "Notes Graph labels/corpus are stale — run Graph → Sync/Rebuild."
            )
    except Exception as e:
        print(f"WARN full-text compare skipped: {e}")


def test_search_oidc(
    *,
    actor_id: str,
    question: str,
    mode: str,
    budget: int,
) -> bool:
    print("\n=== 2) search_note (Notes graph /api/graph/query) ===")
    print(f"actor_id={actor_id!r} question={question!r} mode={mode} budget={budget}")

    result = search_note_lib.search_note(
        question,
        actor_id=actor_id,
        mode=mode,  # type: ignore[arg-type]
        budget=budget,
    )
    _print_result(result)

    if result.get("status") == "error":
        detail = result.get("content") or result
        print(f"FAIL search_note returned error: {detail}")
        return False

    if "text" not in result or not isinstance(result["text"], list):
        print("FAIL expected success shape {\"text\": [...]}")
        return False

    excerpts = result["text"]
    print(f"\nexcerpt count: {len(excerpts)}")
    for i, item in enumerate(excerpts, 1):
        source = item.get("source") or item.get("path") or "?"
        preview = (item.get("text") or "").replace("\n", " ")[:160]
        url = item.get("url") or ""
        print(f"  [{i}] {source}")
        if url:
            print(f"      url: {url}")
        print(f"      text: {preview}{'…' if len(item.get('text') or '') > 160 else ''}")

    # Soft check: when graph has hits for OIDC, body or topics should mention it
    needle = question.strip().lower()
    if excerpts:
        joined = " ".join(
            f"{x.get('text', '')} {' '.join(x.get('related_topics') or [])} {x.get('source', '')}"
            for x in excerpts
        ).lower()
        if needle and needle not in joined:
            print(
                f"WARN: no excerpt text/source/topics contain {question!r} "
                "(graph may still have returned related neighbors)"
            )
        else:
            print(f"OK: at least one excerpt mentions {question!r}")
    else:
        print(
            f"WARN: empty text[] for {question!r} — "
            "graph may have no matching nodes (Sync Notes Graph first)"
        )
        test_fulltext_compare(actor_id, question)

    print("OK: search_note success shape")
    return True


def test_validation(actor_id: str) -> bool:
    print("\n=== 3) validation (empty question / actor) ===")
    empty_q = search_note_lib.search_note("", actor_id=actor_id)
    empty_a = search_note_lib.search_note("OIDC", actor_id="")
    ok = (
        empty_q.get("status") == "error"
        and empty_a.get("status") == "error"
    )
    print(f"empty question → error: {empty_q.get('status') == 'error'}")
    print(f"empty actor_id → error: {empty_a.get('status') == 'error'}")
    if not ok:
        print("FAIL validation helpers")
        return False
    print("OK: validation")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Test search-note against live vault")
    parser.add_argument(
        "--question",
        default=DEFAULT_QUESTION,
        help=f'search query (default: "{DEFAULT_QUESTION}")',
    )
    parser.add_argument(
        "--user-id",
        "--actor-id",
        dest="actor_id",
        default=None,
        help=f"vault owner email (default: {DEFAULT_ACTOR_ID} or USER_ID)",
    )
    parser.add_argument(
        "--mode",
        choices=("bfs", "dfs"),
        default="bfs",
        help="graph traversal mode",
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=2000,
        help="soft node/edge budget (200–8000)",
    )
    parser.add_argument(
        "--skip-health",
        action="store_true",
        help="skip GET /api/health",
    )
    args = parser.parse_args(argv)

    os.chdir(_script_dir)
    actor_id = _resolve_actor_id(args.actor_id)

    print("=" * 60)
    print("search-note integration test")
    print("=" * 60)

    ok = True
    if not args.skip_health:
        ok = test_health(actor_id) and ok
    ok = (
        test_search_oidc(
            actor_id=actor_id,
            question=args.question,
            mode=args.mode,
            budget=args.budget,
        )
        and ok
    )
    ok = test_validation(actor_id) and ok

    print("\n" + "=" * 60)
    if ok:
        print("ALL CHECKS PASSED")
        return 0
    print("SOME CHECKS FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
