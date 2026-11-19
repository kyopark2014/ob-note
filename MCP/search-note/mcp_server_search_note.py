"""Streamable-HTTP MCP server for searching Notes knowledge graph (search-note).

Exposes the same document-search path as Notes Graph UI / agentic-work
``recall_graph_memory`` via vault ``POST /api/graph/query``.
Always pass actor_id (account login id / email) so requests are scoped to
that user's vault graph.
"""

from __future__ import annotations

import logging
import sys
from typing import Dict, Literal, Optional

import search_note as search_note_lib
from mcp.server.mcpserver import MCPServer

logging.basicConfig(
    level=logging.INFO,
    format="%(filename)s:%(lineno)d | %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("search-note-mcp")

INSTRUCTIONS = """\
Search the user's OB Note vault Knowledge Graph (Notes Graph) for past notes
related to a natural-language question.

Same behavior as Notes Graph document search (POST /api/graph/query):
find matching nodes (label / source text), traverse neighbors (BFS or DFS),
and return source-text excerpts. Prefer this over full-text vault_search when
the answer depends on topics, entities, or relationships in the graph.

Critical rules:
1. Always pass actor_id from the system prompt (account login id / email) —
   never a nickname or display name.
2. When excerpts include ``url``, cite notes as markdown links [title](url).
3. Empty ``text`` means no graph matches — say so; do not invent notes.
4. If the graph is missing, tell the user to run Graph → Sync in OB Note first.
"""

try:
    mcp = MCPServer(
        name="search-note",
        instructions=INSTRUCTIONS,
    )
    logger.info("MCP server initialized successfully")
except Exception as e:
    logger.info("Error: %s", e)
    raise


@mcp.tool()
def search_note(
    question: str,
    actor_id: str,
    mode: Optional[Literal["bfs", "dfs"]] = "bfs",
    budget: Optional[int] = 2000,
) -> Dict:
    """
    Search the user's Notes Knowledge Graph for note content related to the question.

    Same behavior as the Notes Graph document search (POST /api/graph/query):
    finds matching nodes (label / source text), traverses neighbors (BFS or DFS),
    and returns source-text excerpts from vault notes.

    Call this when the user asks about something stored in their notes graph, or
    when topical/relational context may live in the knowledge graph rather than
    full-text search alone (Korean: 노트 그래프, 지식 그래프, 예전에 적어둔, 관련 노트).

    Args:
        question: Natural-language search query (required), e.g. "온톨로지", "스프린트 회고"
        actor_id: Account login id / email from the system prompt (required)
        mode: Traversal mode — "bfs" (default) or "dfs"
        budget: Soft size budget for node/edge listing (200–8000, default 2000)

    Returns:
        On success (same shape as graph memory recall):
            {"text": [
                {"type": "excerpt", "source": "...", "path": "...", "url": "...",
                 "related_topics": [...], "text": "..."},
                ...
            ]}
        On error:
            {"status": "error", "content": [{"text": "..."}]}
    """
    logger.info("###### search_note ######")
    logger.info(
        "question=%r actor_id=%s mode=%s budget=%s",
        question,
        actor_id,
        mode,
        budget,
    )
    return search_note_lib.search_note(
        question,
        actor_id=actor_id,
        mode=mode,
        budget=budget,
    )


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=8000,
        stateless_http=True,
    )
