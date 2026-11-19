"""Knowledge Graph note search for MCP.

Wraps vault ``POST /api/graph/query`` — the same BFS/DFS + excerpt path used by
Notes Graph document search — so the agent can recall content from notes stored
in the user's vault knowledge graph.

Return shape mirrors agentic-work ``mcp_graph_memory.recall_graph_memory``:
  success → {"text": [<excerpt items LLM can cite>]}
  error   → {"status": "error", "content": [{"text": "..."}]}
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import quote_plus

import vault_client as vault

logging.basicConfig(
    level=logging.INFO,
    format="%(filename)s:%(lineno)d | %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("search-note")

_MAX_EXCERPTS = 12


def _error(message: str) -> Dict[str, Any]:
    return {"status": "error", "content": [{"text": message}]}


def _note_url(path: str) -> str:
    cleaned = (path or "").replace("\\", "/").lstrip("/").strip()
    if not cleaned:
        return vault.vault_base_url().rstrip("/")
    return f"{vault.vault_base_url().rstrip('/')}/?note={quote_plus(cleaned)}"


def _vault_relative_path(raw: str) -> str:
    """Best-effort vault-relative path from graph source_file absolute/relative paths."""
    text = (raw or "").replace("\\", "/").strip()
    if not text:
        return ""
    marker = "/vault/"
    idx = text.lower().find(marker)
    if idx >= 0:
        return text[idx + len(marker) :].lstrip("/")
    # Already vault-relative (Category/Note.md)
    if not text.startswith("/") and ":" not in text[:3]:
        return text.lstrip("./")
    return Path(text).name


def _extract_contents(result: dict[str, Any]) -> List[Any]:
    """
    Flatten graph query output into LLM-ready excerpt items.

    Topic labels and relations are omitted: they add tokens without
    citable source text. Related entity names stay on each excerpt as
    ``related_topics``. Deep-link ``url`` is added when a vault path is known.
    """
    contents: List[Any] = []

    if result.get("message") and not result.get("nodes") and not result.get("sources"):
        logger.info("search-note empty: %s", result.get("message"))
        return contents

    excerpt_count = 0
    for source in result.get("sources") or []:
        if not source.get("readable", True):
            continue
        raw_path = str(source.get("path") or "")
        rel = _vault_relative_path(raw_path)
        name = source.get("name") or Path(raw_path or rel or "").name or "unknown"
        labels = [str(lb) for lb in (source.get("matched_labels") or []) if lb][:8]
        for excerpt in source.get("excerpts") or []:
            text = str(excerpt).strip()
            if not text:
                continue
            item: Dict[str, Any] = {
                "type": "excerpt",
                "source": name,
                "text": text,
            }
            if rel:
                item["path"] = rel
                item["url"] = _note_url(rel)
            if labels:
                item["related_topics"] = labels
            contents.append(item)
            excerpt_count += 1
            if excerpt_count >= _MAX_EXCERPTS:
                break
        if excerpt_count >= _MAX_EXCERPTS:
            break

    logger.info("extracted contents: excerpts=%s", excerpt_count)
    return contents


def search_note(
    question: str,
    *,
    actor_id: str,
    mode: Optional[Literal["bfs", "dfs"]] = "bfs",
    budget: Optional[int] = 2000,
) -> Dict[str, Any]:
    """
    Search the actor's Notes knowledge graph for content related to ``question``.

    Same semantics as Notes Graph document search (POST /api/graph/query).
    On success returns ``{"text": [...]}`` like graph memory retrieve.
    """
    uid = (actor_id or "").strip()
    if not uid:
        return _error("actor_id is required (account login id / email)")

    question = (question or "").strip()
    if not question:
        return _error("question is required")

    traversal = "dfs" if str(mode or "bfs").lower() == "dfs" else "bfs"
    try:
        soft_budget = max(200, min(int(budget or 2000), 8000))
    except (TypeError, ValueError):
        soft_budget = 2000

    logger.info(
        "###### search_note ###### actor_id=%s question=%r mode=%s budget=%s",
        uid,
        question,
        traversal,
        soft_budget,
    )

    try:
        result = vault.api_request(
            "POST",
            "/api/graph/query",
            user_id=uid,
            body={
                "question": question,
                "mode": traversal,
                "budget": soft_budget,
            },
            timeout=90.0,
        )
    except RuntimeError as e:
        msg = str(e)
        # Surface vault 404 as a clear "graph missing" hint
        if "404" in msg and ("그래프" in msg or "graph" in msg.lower()):
            return _error(
                "Notes 그래프가 아직 없습니다. Graph → Sync 후 다시 검색하세요."
            )
        logger.exception("search-note vault query failed")
        return _error(msg)
    except Exception as e:  # noqa: BLE001
        logger.exception("search-note failed")
        return _error(f"query failed: {e}")

    if not isinstance(result, dict):
        return _error(f"unexpected response from /api/graph/query: {type(result)}")

    contents = _extract_contents(result)
    return {"text": contents}
