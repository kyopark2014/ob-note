"""Notes (vault markdown) knowledge graph — Sync / Rebuild / HTML viewer.

Adapted from agentic-work Wiki graph jobs, scoped to the vault as Source of Truth.
Labels use Notes (not Wiki). Graph JSON for skills remains vault_index.get_graph().
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from application import vault_backend, vault_index

logger = logging.getLogger("notes_graph")

_CONFIG_NAME = "notes_graph.json"
_STATUS_NAME = "notes_graph_status.json"
# graphify-compatible artifacts (agentic-work Wiki Graph HTML)
_OUT_DIR_NAME = "notes-graphify"
_HTML_NAME = "app-graph.html"
_GRAPH_JSON_NAME = "graph.json"
_GRAPH_DIR = Path(__file__).resolve().parent.parent / "graph"
_GRAPH_HTML_CURRENT_MARKER = 'data-doc-search="1"'
_NOTES_OPEN_SNIPPET = """
<script>
(function () {
  function bindNotesOpen() {
    if (typeof network === "undefined") {
      setTimeout(bindNotesOpen, 200);
      return;
    }
    network.on("doubleClick", function (params) {
      if (!params.nodes || !params.nodes.length) return;
      const id = String(params.nodes[0] || "");
      if (!id || id.startsWith("missing:")) return;
      try {
        parent.postMessage({ type: "notes-open", path: id }, "*");
      } catch (e) {}
    });
  }
  bindNotesOpen();
})();
</script>
"""

GRAPH_PATTERNS = ("pattern1", "pattern2", "pattern3")
DEFAULT_PATTERN = "pattern1"

_lock = threading.Lock()
_state: dict[str, Any] = {
    "status": "idle",
    "error": None,
    "message": None,
    "last_success_at": None,
    "started_at": None,
    "finished_at": None,
    "progress": None,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _config_path() -> Path:
    return vault_backend.settings_dir() / _CONFIG_NAME


def _status_path() -> Path:
    return vault_backend.cache_dir() / _STATUS_NAME


def graphify_out_dir() -> Path:
    path = vault_backend.cache_dir() / _OUT_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _html_path() -> Path:
    return graphify_out_dir() / _HTML_NAME


def _graph_json_path() -> Path:
    return graphify_out_dir() / _GRAPH_JSON_NAME


def _ensure_graph_lib_path() -> None:
    root = str(_GRAPH_DIR)
    if root not in sys.path:
        sys.path.insert(0, root)


def _default_config() -> dict[str, Any]:
    return {
        "folders": [],  # empty = entire vault
        "include_missing": True,
        "pattern": DEFAULT_PATTERN,
    }


def load_config() -> dict[str, Any]:
    path = _config_path()
    cfg = _default_config()
    if not path.is_file():
        return cfg
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return cfg
    if not isinstance(raw, dict):
        return cfg
    folders = raw.get("folders") or []
    if isinstance(folders, list):
        cleaned: list[str] = []
        for item in folders:
            s = str(item or "").strip().replace("\\", "/").strip("/")
            if s and s not in cleaned and ".." not in s.split("/"):
                cleaned.append(s)
        cfg["folders"] = cleaned[:20]
    cfg["include_missing"] = bool(raw.get("include_missing", True))
    pattern = str(raw.get("pattern") or DEFAULT_PATTERN).strip().lower()
    cfg["pattern"] = pattern if pattern in GRAPH_PATTERNS else DEFAULT_PATTERN
    return cfg


def save_config(
    *,
    folders: list[str] | None = None,
    include_missing: bool | None = None,
    pattern: str | None = None,
) -> dict[str, Any]:
    cfg = load_config()
    if folders is not None:
        cleaned: list[str] = []
        for item in folders:
            s = str(item or "").strip().replace("\\", "/").strip("/")
            if s and s not in cleaned and ".." not in s.split("/"):
                cleaned.append(s)
        cfg["folders"] = cleaned[:20]
    if include_missing is not None:
        cfg["include_missing"] = bool(include_missing)
    if pattern is not None:
        p = str(pattern).strip().lower()
        cfg["pattern"] = p if p in GRAPH_PATTERNS else DEFAULT_PATTERN
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return cfg


def set_pattern(pattern: str) -> str:
    cfg = save_config(pattern=pattern)
    return str(cfg["pattern"])


def get_pattern() -> str:
    return str(load_config().get("pattern") or DEFAULT_PATTERN)


def list_vault_folders() -> list[dict[str, str]]:
    """Top-level vault folders for Notes Configure."""
    from application import note_store

    if note_store.enabled():
        names = sorted(
            {
                i["path"].split("/", 1)[0]
                for i in note_store.list_items()
                if "/" in i["path"] or i.get("kind") == note_store.KIND_FOLDER
            },
            key=str.lower,
        )
        return [{"name": n, "path": n} for n in names if not n.startswith(".")]
    root = vault_backend.vault_root()
    folders: list[dict[str, str]] = []
    try:
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir():
                continue
            if child.name.startswith("."):
                continue
            folders.append({"name": child.name, "path": child.name})
    except OSError:
        logger.exception("Failed to list vault folders")
    return folders


def _persist_state() -> None:
    try:
        path = _status_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(_state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        logger.exception("Failed to persist notes graph status")


def _load_persisted_state() -> None:
    path = _status_path()
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    with _lock:
        for key in (
            "status",
            "error",
            "message",
            "last_success_at",
            "started_at",
            "finished_at",
            "progress",
        ):
            if key in data:
                _state[key] = data[key]
        if _state.get("status") in ("queued", "running"):
            _state["status"] = "idle"
            _state["error"] = None
            _state["message"] = "Notes sync interrupted (server restart)."


_state_loaded = False


def _ensure_state_loaded() -> None:
    global _state_loaded
    if _state_loaded:
        return
    try:
        _load_persisted_state()
    except RuntimeError:
        # No vault user bound yet (import / health check).
        return
    _state_loaded = True


def get_job_status() -> dict[str, Any]:
    _ensure_state_loaded()
    with _lock:
        return dict(_state)


def graph_json_exists() -> bool:
    return _graph_json_path().is_file()


def graph_exists() -> bool:
    return graph_json_exists() and _html_path().is_file()


def html_file_path() -> Path:
    return _html_path()


def status_payload() -> dict[str, Any]:
    cfg = load_config()
    job = get_job_status()
    exists = graph_exists()
    status = job.get("status") or "idle"
    if status in ("idle", "unchanged") and exists:
        status = "ready"
    return {
        "notes_dir": str(vault_backend.vault_root()),
        "folders": cfg.get("folders") or [],
        "include_missing": bool(cfg.get("include_missing", True)),
        "available_folders": list_vault_folders(),
        "exists": exists,
        "path": _HTML_NAME if _html_path().is_file() else None,
        "storage": str(vault_backend.cache_dir()),
        "status": status,
        "pattern": cfg.get("pattern") or DEFAULT_PATTERN,
        "error": job.get("error"),
        "message": job.get("message"),
        "last_success_at": job.get("last_success_at"),
        "progress": job.get("progress"),
    }


def _filter_graph(
    payload: dict[str, Any],
    *,
    folders: list[str],
    include_missing: bool,
) -> dict[str, Any]:
    nodes = list(payload.get("nodes") or [])
    edges = list(payload.get("edges") or [])
    if folders:
        prefixes = tuple(f"{f}/" for f in folders)
        names = set(folders)

        def in_scope(path: str | None) -> bool:
            if not path:
                return False
            if path in names:
                return True
            return any(path.startswith(p) for p in prefixes)

        keep_ids: set[str] = set()
        filtered_nodes = []
        for n in nodes:
            path = n.get("path")
            nid = n.get("id")
            missing = bool(n.get("missing"))
            if missing:
                continue
            if in_scope(path if isinstance(path, str) else None):
                filtered_nodes.append(n)
                if isinstance(nid, str):
                    keep_ids.add(nid)
        if include_missing:
            for n in nodes:
                if not n.get("missing"):
                    continue
                nid = n.get("id")
                # keep dangling targets only if an in-scope edge points here
                linked = any(
                    e.get("target") == nid and e.get("source") in keep_ids for e in edges
                )
                if linked and isinstance(nid, str):
                    filtered_nodes.append(n)
                    keep_ids.add(nid)
        nodes = filtered_nodes
        edges = [
            e
            for e in edges
            if e.get("source") in keep_ids and e.get("target") in keep_ids
        ]
    elif not include_missing:
        keep_ids = {n["id"] for n in nodes if not n.get("missing") and n.get("id")}
        nodes = [n for n in nodes if n.get("id") in keep_ids]
        edges = [
            e
            for e in edges
            if e.get("source") in keep_ids and e.get("target") in keep_ids
        ]
    return {"nodes": nodes, "edges": edges}


def build_notes_graph() -> dict[str, Any]:
    cfg = load_config()
    raw = vault_index.get_graph()
    return _filter_graph(
        raw,
        folders=list(cfg.get("folders") or []),
        include_missing=bool(cfg.get("include_missing", True)),
    )


def _payload_to_nx(payload: dict[str, Any]):
    """Convert vault wiki-link payload → networkx Graph (graphify attrs)."""
    import networkx as nx

    G = nx.Graph()
    for node in payload.get("nodes") or []:
        nid = str(node.get("id") or "").strip()
        if not nid:
            continue
        label = str(node.get("label") or nid)
        path = node.get("path")
        missing = bool(node.get("missing"))
        source_file = ""
        if isinstance(path, str) and path.strip() and not missing:
            # vault-relative path — graph_query resolves under allowed_roots
            source_file = path.strip().replace("\\", "/")
        G.add_node(
            nid,
            label=label,
            source_file=source_file,
            vault_path=source_file,
            missing=missing,
            tags=list(node.get("tags") or []),
            author="vault",
        )
    for edge in payload.get("edges") or []:
        src = str(edge.get("source") or "").strip()
        tgt = str(edge.get("target") or "").strip()
        if not src or not tgt:
            continue
        if src not in G:
            G.add_node(src, label=src, source_file="", missing=True, author="vault")
        if tgt not in G:
            G.add_node(tgt, label=tgt, source_file="", missing=True, author="vault")
        G.add_edge(
            src,
            tgt,
            relation="wikilink",
            confidence="EXTRACTED",
            confidence_score=1.0,
            _src=src,
            _tgt=tgt,
        )
    return G


def _write_pattern_html_file(
    G,
    communities: dict[int, list[str]],
    *,
    pattern: str,
    html_path: Path,
) -> str:
    _ensure_graph_lib_path()
    from lib.patterns import write_pattern_html

    pid = write_pattern_html(
        pattern,
        G,
        communities,
        html_path,
        title="Notes Graph",
        subtitle=(
            "vault markdown 지식 그래프 · 검색·범례·패턴은 agentic-work Wiki Graph와 동일합니다. "
            f"({G.number_of_nodes()} nodes / {G.number_of_edges()} edges) · "
            "더블클릭으로 노트 열기"
        ),
        query_url="/api/graph/query",
    )
    # Append Notes open hook (double-click → parent postMessage)
    try:
        text = html_path.read_text(encoding="utf-8")
        if "notes-open" not in text:
            if "</body>" in text:
                text = text.replace("</body>", _NOTES_OPEN_SNIPPET + "\n</body>", 1)
            else:
                text += _NOTES_OPEN_SNIPPET
            tmp = html_path.with_suffix(html_path.suffix + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, html_path)
    except OSError:
        logger.exception("Failed to append notes-open hook")
    return pid


def write_graph_artifacts(graph: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build graphify JSON + agentic-work pattern HTML from vault wiki-links."""
    from graphify.cluster import cluster
    from graphify.export import to_json

    payload = graph if graph is not None else build_notes_graph()
    # If caller passed already-graphify JSON (has links), rebuild from vault payload instead
    if "links" in payload and "edges" not in payload:
        payload = build_notes_graph()

    pattern = get_pattern()
    G = _payload_to_nx(payload)
    if G.number_of_nodes() == 0:
        raise ValueError("그래프에 노드가 없습니다. vault에 마크다운 노트를 추가하세요.")

    communities = cluster(G)
    for cid, members in communities.items():
        for nid in members:
            if nid in G.nodes:
                G.nodes[nid]["community"] = int(cid)

    out = graphify_out_dir()
    json_path = _graph_json_path()
    html_path = _html_path()

    tmp_json = json_path.with_suffix(json_path.suffix + ".tmp")
    to_json(G, communities, str(tmp_json))
    os.replace(tmp_json, json_path)

    pid = _write_pattern_html_file(
        G, communities, pattern=pattern, html_path=html_path
    )

    # Keep skill-facing vault graph.json in settings (wiki-link format)
    settings_graph = vault_backend.settings_dir() / "graph.json"
    settings_graph.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    return {
        "notes": G.number_of_nodes(),
        "links": G.number_of_edges(),
        "pattern": pid,
        "html": str(html_path),
        "json": str(json_path),
    }


def republish_html(*, pattern: str | None = None) -> bool:
    """Re-render pattern HTML from existing graphify graph.json (no re-index)."""
    if pattern:
        set_pattern(pattern)
    json_path = _graph_json_path()
    if not json_path.is_file():
        return False

    _ensure_graph_lib_path()
    from networkx.readwrite import json_graph
    from graphify.cluster import cluster

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False

    try:
        G = json_graph.node_link_graph(data, edges="links")
    except TypeError:
        G = json_graph.node_link_graph(data)
    if "hyperedges" in data:
        G.graph["hyperedges"] = data["hyperedges"]
    if G.number_of_nodes() == 0:
        return False

    communities: dict[int, list[str]] = {}
    for nid, ndata in G.nodes(data=True):
        cid = ndata.get("community")
        if cid is None:
            continue
        communities.setdefault(int(cid), []).append(nid)
    if not communities:
        communities = cluster(G)

    pid = get_pattern()
    _write_pattern_html_file(
        G, communities, pattern=pid, html_path=_html_path()
    )
    return True


def _run_sync(*, full: bool) -> None:
    with _lock:
        _state["status"] = "running"
        _state["error"] = None
        _state["message"] = (
            "Notes 전체 재빌드를 실행 중입니다…"
            if full
            else "Notes 그래프를 동기화하는 중입니다…"
        )
        _state["started_at"] = _now_iso()
        _state["finished_at"] = None
        _state["progress"] = {
            "phase": "index",
            "file": "vault markdown",
            "pct": 10,
        }
        _persist_state()
    try:
        if full:
            # Drop derived HTML so clients see rebuild clearly
            for path in (_html_path(), _graph_json_path()):
                try:
                    if path.is_file():
                        path.unlink()
                except OSError:
                    pass
        with _lock:
            _state["progress"] = {
                "phase": "index",
                "file": "rebuild index",
                "pct": 40,
            }
            _persist_state()
        stats = vault_index.rebuild_index()
        with _lock:
            _state["progress"] = {
                "phase": "render",
                "file": "notes-graph.html",
                "pct": 75,
            }
            _persist_state()
        artifact = write_graph_artifacts()
        with _lock:
            _state["status"] = "ready"
            _state["error"] = None
            _state["message"] = (
                f"Notes 그래프 준비 완료 · nodes {artifact['notes']} · "
                f"edges {artifact['links']}"
            )
            _state["last_success_at"] = _now_iso()
            _state["finished_at"] = _state["last_success_at"]
            _state["progress"] = {
                "phase": "done",
                "file": None,
                "pct": 100,
                "notes": artifact["notes"],
                "links": artifact["links"],
                **stats,
            }
            _persist_state()
    except Exception as exc:
        logger.exception("Notes graph sync failed")
        with _lock:
            _state["status"] = "error"
            _state["error"] = str(exc)
            _state["message"] = f"Notes 동기화 실패: {exc}"
            _state["finished_at"] = _now_iso()
            _state["progress"] = None
            _persist_state()


def ensure_sync(*, full: bool = False) -> dict[str, Any]:
    with _lock:
        if _state.get("status") in ("queued", "running"):
            return dict(_state)
        _state["status"] = "queued"
        _state["error"] = None
        _state["message"] = (
            "Notes 전체 재빌드를 대기열에 넣었습니다…"
            if full
            else "Notes 동기화를 대기열에 넣었습니다…"
        )
        _state["started_at"] = _now_iso()
        _persist_state()

    user_id = vault_backend.current_user_id()

    def worker() -> None:
        with vault_backend.user_scope(user_id):
            _run_sync(full=full)

    threading.Thread(target=worker, name="notes-graph-sync", daemon=True).start()
    return get_job_status()


def ensure_html_current() -> Path | None:
    """Ensure app-graph.html exists with search UI for the current pattern."""
    json_path = _graph_json_path()
    if not json_path.is_file():
        return None
    html_path = _html_path()
    pid = get_pattern()
    need = True
    if html_path.is_file():
        try:
            sample = html_path.read_text(encoding="utf-8", errors="ignore")
            has_search = _GRAPH_HTML_CURRENT_MARKER in sample
            active_ok = (
                f'class="ctrl-btn pattern-btn active" data-pattern="{pid}"' in sample
            )
            stale_json = html_path.stat().st_mtime < json_path.stat().st_mtime
            need = (not has_search) or (not active_ok) or stale_json
        except OSError:
            need = True
    if need:
        try:
            republish_html(pattern=pid)
        except Exception:
            logger.exception("Failed to ensure Notes graph HTML")
            return html_path if html_path.is_file() else None
    return html_path if html_path.is_file() else None


def query_notes_graph(
    question: str,
    *,
    mode: str = "bfs",
    budget: int = 2000,
) -> dict[str, Any]:
    from application.graph_query import query_user_graph

    graph_json = _graph_json_path()
    if not graph_json.is_file():
        raise FileNotFoundError("Notes 그래프가 아직 없습니다.")
    from application import note_store

    if note_store.enabled():
        note_store.ensure_cache_fresh()  # the query reads note files from disk
    root = vault_backend.vault_root()
    return query_user_graph(
        graph_json,
        question,
        mode=mode,
        budget=budget,
        allowed_roots=[root],
        use_embeddings=False,
    )
