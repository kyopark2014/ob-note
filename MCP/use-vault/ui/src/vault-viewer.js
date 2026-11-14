// ob-note vault viewer (MCP App) for the read tools of the use-vault MCP server.
// Claude MCP Apps design guidelines: inline carousel / card, fullscreen explorer, text entry
// stays in chat (the only input here filters the tree you are already looking at), no menus,
// host style tokens, skeleton loading, 44px targets.
const { App } = window.__ext;
const root = document.getElementById("root");
const MAX_INLINE = 8;

const app = new App(
  { name: "Vault viewer", version: "1.0.0" },
  { availableDisplayModes: ["inline", "fullscreen"] },
);

const S = {
  ctx: {},
  input: null,          // arguments the model passed (carries actor_id)
  result: undefined,    // structuredContent of the tool result; undefined = waiting
  tab: "notes",         // fullscreen: "notes" | "graph"
  tree: null, treeLoading: false, treeError: null,
  note: null, noteLoading: false, noteError: null,
  graph: null, graphLoading: false, graphError: null,
  filter: "",
  pane: "tree",         // compact fullscreen: "tree" | "reader"
};

// ---------------------------------------------------------------- data
function structured(res) {
  if (!res) return null;
  if (res.structuredContent && typeof res.structuredContent === "object") return res.structuredContent;
  for (const b of res.content || []) {
    if (b && typeof b.text === "string") { try { return JSON.parse(b.text); } catch (_) { /* fall through */ } }
  }
  return null;
}
const actor = () => (S.input && S.input.actor_id) || "";
const isFs = () => S.ctx.displayMode === "fullscreen";
const width = () => (S.ctx.containerDimensions && S.ctx.containerDimensions.width) || window.innerWidth;
const compact = () => width() < 700;
const canFullscreen = () => (S.ctx.availableDisplayModes || []).includes("fullscreen") && !isFs();
const kind = () => (S.result && S.result.kind) || "none";
const stem = (p) => String(p || "").split("/").pop().replace(/\.[^.]+$/, "");
const folder = (p) => { const i = String(p || "").lastIndexOf("/"); return i > 0 ? p.slice(0, i) : ""; };

async function call(name, args) {
  const res = await app.callServerTool({ name, arguments: { actor_id: actor(), ...args } });
  const data = structured(res);
  if (!data) throw new Error("응답을 읽을 수 없습니다.");
  if (data.ok === false) throw new Error(data.error || "요청에 실패했습니다.");
  return data;
}

// Notes reachable from the tree (for [[wiki links]] and counts).
function flatNotes(nodes, out = []) {
  for (const n of nodes || []) {
    if (n.type === "folder") flatNotes(n.children, out);
    else if (/\.(md|txt)$/i.test(n.name || "")) out.push(n);
  }
  return out;
}
function resolveWiki(name) {
  const key = String(name).toLowerCase().trim();
  const notes = flatNotes(S.tree && S.tree.children);
  const hit = notes.find((n) => stem(n.path).toLowerCase() === key || String(n.title || "").toLowerCase() === key);
  return hit && hit.path;
}

// ---------------------------------------------------------------- inline views
const openBtn = (url, label) => (kit.safeUrl(url) ? `<button class="k-btn" data-act="open" data-url="${kit.esc(url)}" aria-label="${kit.esc(label || "열기")}">열기</button>` : "");

function noteCard(n) {
  const title = n.title || stem(n.path);
  const meta = [folder(n.path) || "(루트)", n.updated_at ? kit.fmtDate(n.updated_at) : ""].filter(Boolean).join(" · ");
  return `<article class="vv-card" role="listitem">
    <div class="vv-title">${kit.esc(title)}</div>
    <div class="k-caption k-faint">${kit.esc(meta)}</div>
    ${n.snippet ? `<div class="vv-snip k-caption k-muted">${kit.esc(String(n.snippet).slice(0, 200))}</div>` : ""}
    ${(n.tags || []).length ? `<div>${n.tags.slice(0, 3).map((t) => `<span class="vv-chip">${kit.esc(t)}</span>`).join(" ")}</div>` : ""}
    ${openBtn(n.url, title + " 열기")}</article>`;
}

function carousel(label, list) {
  if (!list.length) return `<div class="k-empty k-muted">표시할 노트가 없습니다.</div>`;
  const more = list.length > MAX_INLINE;
  return `<div class="vv-head"><div><span class="k-strong">Vault</span> <span class="k-caption k-faint">· ${kit.esc(label)} · ${list.length}개</span></div>
      ${more || canFullscreen() ? `<button class="k-btn" data-act="expand">${more ? `전체 ${list.length}개 보기` : "전체 화면"}</button>` : ""}</div>
    <div class="vv-carousel" role="list" aria-label="${kit.esc(label)}">${list.slice(0, MAX_INLINE).map(noteCard).join("")}</div>`;
}

function inlineNote(n) {
  const facts = [folder(n.path) || "(루트)", n.updated_at ? "수정 " + kit.fmtDate(n.updated_at) : "", n.word_count ? n.word_count + "단어" : "",
    (n.backlinks || []).length ? "백링크 " + n.backlinks.length : ""].filter(Boolean);
  return `<div class="vv-note">
    <div class="vv-title">${kit.esc(n.title || stem(n.path))}</div>
    <div class="vv-facts k-caption k-faint">${facts.map((f) => `<span>${kit.esc(f)}</span>`).join("")}</div>
    <div class="vv-excerpt k-muted">${kit.esc(kit.excerpt(n.content, 320))}</div>
    <div class="vv-actions">${kit.safeUrl(n.url) ? `<button class="k-btn k-primary" data-act="open" data-url="${kit.esc(n.url)}">ob-note에서 열기</button>` : ""}
      ${canFullscreen() ? `<button class="k-btn" data-act="expand">전체 보기</button>` : ""}</div></div>`;
}

function countTree(nodes) {
  let folders = 0, notes = 0;
  const walk = (list) => { for (const n of list || []) { if (n.type === "folder") { folders++; walk(n.children); } else if (/\.(md|txt)$/i.test(n.name || "")) notes++; } };
  walk(nodes); return { folders, notes };
}
function inlineSummary(title, facts) {
  return `<div class="vv-note"><div class="vv-title">${kit.esc(title)}</div>
    <div class="vv-facts k-caption k-faint">${facts.map((f) => `<span>${kit.esc(f)}</span>`).join("")}</div>
    <div class="vv-actions">${canFullscreen() ? `<button class="k-btn k-primary" data-act="expand">전체 보기</button>` : ""}</div></div>`;
}

function inline() {
  const r = S.result;
  switch (kind()) {
    case "search": return carousel(`검색: ${r.query || ""}`, r.results || []);
    case "list": return carousel(r.prefix ? `폴더: ${r.prefix}` : "노트 목록", (r.files || []).map((f) => ({ ...f, title: stem(f.path) })));
    case "backlinks": return carousel(`백링크: ${stem(r.path)}`, r.backlinks || []);
    case "note": return inlineNote(r);
    case "tree": { const c = countTree(r.children); return inlineSummary("Vault 파일 트리", [`폴더 ${c.folders}`, `노트 ${c.notes}`]); }
    case "graph": return inlineSummary("노트 그래프", [`노트 ${(r.nodes || []).filter((n) => !n.missing).length}`, `링크 ${(r.edges || []).length}`]);
    default: return `<div class="k-empty k-muted">표시할 내용이 없습니다.</div>`;
  }
}

// ---------------------------------------------------------------- fullscreen
function treeHtml(nodes, q) {
  const out = [];
  for (const n of nodes || []) {
    if (n.type === "folder") {
      const inner = treeHtml(n.children, q);
      if (inner) out.push(`<details${q ? " open" : ""}><summary>📁 ${kit.esc(n.name)}</summary><div>${inner}</div></details>`);
    } else if (/\.(md|txt)$/i.test(n.name || "")) {
      const label = n.title || stem(n.path);
      if (q && !(label + " " + n.path).toLowerCase().includes(q)) continue;
      out.push(`<button class="vv-item" data-act="select" data-path="${kit.esc(n.path)}" aria-current="${S.note && S.note.path === n.path}">${kit.esc(label)}</button>`);
    }
  }
  return out.join("");
}

function treePane() {
  if (S.treeLoading) return `<div class="vv-tree" aria-busy="true">${kit.skeleton(6, 28)}</div>`;
  if (S.treeError) return `<div class="vv-tree"><div class="k-error" role="alert">${kit.esc(S.treeError)}</div></div>`;
  const q = S.filter.trim().toLowerCase();
  const body = treeHtml(S.tree && S.tree.children, q);
  return `<div class="vv-tree"><input class="vv-filter" data-act="filter" type="search" placeholder="제목 필터" value="${kit.esc(S.filter)}" aria-label="트리에서 노트 필터">
    ${body || `<div class="k-empty k-muted">노트가 없습니다.</div>`}</div>`;
}

// Vault notes always start with "# Title" (house rule); the reader header already shows it.
function withoutTitle(n) {
  const body = String(n.content || "");
  const m = /^\s*#\s+(.+?)\s*\n/.exec(body);
  return m && m[1].trim() === String(n.title || stem(n.path)).trim() ? body.slice(m[0].length) : body;
}

function readerPane() {
  if (S.noteLoading) return `<div class="vv-reader" aria-busy="true">${kit.skeleton(1, 28)}${kit.skeleton(5, 16)}</div>`;
  if (S.noteError) return `<div class="vv-reader"><div class="k-error" role="alert">${kit.esc(S.noteError)}</div></div>`;
  const n = S.note;
  if (!n) return `<div class="vv-reader k-empty k-muted">왼쪽에서 노트를 선택하세요.</div>`;
  const back = (n.backlinks || []).map((b) => `<button class="vv-item" data-act="select" data-path="${kit.esc(b.path)}">${kit.esc(b.title || stem(b.path))}</button>`).join("");
  return `<div class="vv-reader">
    <div class="vv-head"><h2>${kit.esc(n.title || stem(n.path))}</h2>${kit.safeUrl(n.url) ? `<button class="k-btn" data-act="open" data-url="${kit.esc(n.url)}">ob-note에서 열기</button>` : ""}</div>
    <div class="vv-facts k-caption k-faint"><span>${kit.esc(n.path)}</span>${n.updated_at ? `<span>수정 ${kit.esc(kit.fmtDate(n.updated_at))}</span>` : ""}
      ${(n.tags || []).map((t) => `<span class="vv-chip">${kit.esc(t)}</span>`).join(" ")}</div>
    <div class="k-md">${kit.renderMarkdown(withoutTitle(n))}</div>
    ${back ? `<div class="vv-back"><div class="k-strong">백링크 ${n.backlinks.length}</div>${back}</div>` : ""}</div>`;
}

// ---- graph (small force layout, no dependencies)
function layoutGraph(g, W, H) {
  const deg = new Map();
  for (const e of g.edges) { deg.set(e.source, (deg.get(e.source) || 0) + 1); deg.set(e.target, (deg.get(e.target) || 0) + 1); }
  let nodes = g.nodes.filter((n) => !n.missing).map((n) => ({ ...n, deg: deg.get(n.id) || 0 }));
  nodes.sort((a, b) => b.deg - a.deg);
  nodes = nodes.slice(0, 150);                       // keep it legible and fast
  const idx = new Map(nodes.map((n, i) => [n.id, i]));
  const edges = g.edges.filter((e) => idx.has(e.source) && idx.has(e.target)).map((e) => [idx.get(e.source), idx.get(e.target)]);
  nodes.forEach((n, i) => { const a = (i / nodes.length) * Math.PI * 2; n.x = W / 2 + Math.cos(a) * W * 0.3; n.y = H / 2 + Math.sin(a) * H * 0.3; n.vx = 0; n.vy = 0; });
  const k = Math.sqrt((W * H) / Math.max(nodes.length, 1)) * 0.6;
  for (let it = 0; it < 220; it++) {
    const cool = 1 - it / 220;
    for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
      let dx = nodes[i].x - nodes[j].x, dy = nodes[i].y - nodes[j].y; const d2 = dx * dx + dy * dy + 0.01, d = Math.sqrt(d2);
      const f = ((k * k) / d2) * 0.9; dx = (dx / d) * f; dy = (dy / d) * f;
      nodes[i].vx += dx; nodes[i].vy += dy; nodes[j].vx -= dx; nodes[j].vy -= dy;
    }
    for (const [a, b] of edges) {
      const dx = nodes[b].x - nodes[a].x, dy = nodes[b].y - nodes[a].y, d = Math.sqrt(dx * dx + dy * dy) + 0.01, f = ((d - k) / k) * 0.5;
      nodes[a].vx += (dx / d) * f * 6; nodes[a].vy += (dy / d) * f * 6; nodes[b].vx -= (dx / d) * f * 6; nodes[b].vy -= (dy / d) * f * 6;
    }
    for (const n of nodes) {
      n.vx += (W / 2 - n.x) * 0.01; n.vy += (H / 2 - n.y) * 0.01;
      const s = Math.min(1, 8 * cool + 0.5); n.x += Math.max(-s * 12, Math.min(s * 12, n.vx)); n.y += Math.max(-s * 12, Math.min(s * 12, n.vy)); n.vx *= 0.5; n.vy *= 0.5;
      n.x = Math.max(16, Math.min(W - 16, n.x)); n.y = Math.max(16, Math.min(H - 16, n.y));
    }
  }
  return { nodes, edges };
}

function graphPane() {
  if (S.graphLoading) return `<div aria-busy="true">${kit.skeleton(1, 320)}</div>`;
  if (S.graphError) return `<div class="k-error" role="alert">${kit.esc(S.graphError)}</div>`;
  if (!S.graph) return "";
  const W = Math.max(320, width() - 24), H = Math.max(320, window.innerHeight - 120);
  const { nodes, edges } = layoutGraph(S.graph, W, H);
  if (!nodes.length) return `<div class="k-empty k-muted">연결된 노트가 없습니다.</div>`;
  const sel = S.note && S.note.path;
  const lines = edges.map(([a, b]) => `<line x1="${nodes[a].x.toFixed(1)}" y1="${nodes[a].y.toFixed(1)}" x2="${nodes[b].x.toFixed(1)}" y2="${nodes[b].y.toFixed(1)}" stroke="var(--color-border-secondary, rgba(128,128,128,.4))" stroke-width="1"/>`).join("");
  const dots = nodes.map((n) => {
    const r = 4 + Math.min(n.deg, 10);
    const fill = n.path === sel ? "var(--color-text-info, #3266ad)" : "var(--color-text-primary, #141413)";
    const label = n.deg >= 3 || n.path === sel ? `<text x="${(n.x + r + 3).toFixed(1)}" y="${(n.y + 4).toFixed(1)}">${kit.esc(n.label || stem(n.path))}</text>` : "";
    return `<g><circle cx="${n.x.toFixed(1)}" cy="${n.y.toFixed(1)}" r="${r}" fill="${fill}" opacity=".85" tabindex="0" role="button" data-act="select-graph" data-path="${kit.esc(n.path)}" aria-label="${kit.esc(n.label || stem(n.path))}"><title>${kit.esc(n.label || stem(n.path))}</title></circle>${label}</g>`;
  }).join("");
  return `<svg class="vv-graph" viewBox="0 0 ${W} ${H}" role="img" aria-label="노트 그래프: 노트 ${nodes.length}개, 링크 ${edges.length}개">${lines}${dots}</svg>
    <div class="k-caption k-faint" style="margin-top:6px">노트 ${nodes.length}개 · 링크 ${edges.length}개${S.graph.nodes.length > nodes.length ? " (연결이 많은 노트 위주로 표시)" : ""}</div>`;
}

function fullscreen() {
  const seg = `<div class="k-seg" role="group" aria-label="보기">
    <button data-act="tab" data-t="notes" aria-pressed="${S.tab === "notes"}">노트</button>
    <button data-act="tab" data-t="graph" aria-pressed="${S.tab === "graph"}">그래프</button></div>`;
  let body;
  if (S.tab === "graph") body = `<div style="min-height:0;overflow:auto">${graphPane()}</div>`;
  else if (compact()) {
    const panes = `<div class="k-seg" role="group" aria-label="영역" style="margin-bottom:6px"><button data-act="pane" data-p="tree" aria-pressed="${S.pane === "tree"}">목록</button><button data-act="pane" data-p="reader" aria-pressed="${S.pane === "reader"}">본문</button></div>`;
    body = `<div class="vv-body vv-compact"><div style="min-height:0;display:flex;flex-direction:column">${panes}${S.pane === "tree" ? treePane() : readerPane()}</div></div>`;
  } else body = `<div class="vv-body">${treePane()}${readerPane()}</div>`;
  return `<div class="vv-fs"><div>${seg}</div>${body}</div>`;
}

function render() {
  if (S.result === undefined) { root.innerHTML = `<div aria-busy="true"><div class="k-skel" style="height:176px"></div></div>`; return; }
  const keepFilter = document.activeElement && document.activeElement.dataset && document.activeElement.dataset.act === "filter";
  root.innerHTML = isFs() ? fullscreen() : inline();
  if (keepFilter) { const el = root.querySelector("[data-act=filter]"); if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); } }
}

// ---------------------------------------------------------------- actions
const openUrl = (u) => { const url = kit.safeUrl(u); if (url) app.openLink({ url }).catch(() => {}); };

async function loadTree() {
  if (S.tree || S.treeLoading) return;
  if (kind() === "tree") { S.tree = S.result; return; }
  S.treeLoading = true; S.treeError = null; render();
  try { S.tree = await call("vault_tree", {}); } catch (e) { S.treeError = e.message; } finally { S.treeLoading = false; render(); }
}
async function loadGraph() {
  if (S.graph || S.graphLoading) return;
  if (kind() === "graph") { S.graph = S.result; return; }
  S.graphLoading = true; S.graphError = null; render();
  try { S.graph = await call("vault_graph", {}); } catch (e) { S.graphError = e.message; } finally { S.graphLoading = false; render(); }
}
async function selectNote(path) {
  S.tab = "notes"; S.pane = "reader"; S.noteLoading = true; S.noteError = null; render();
  try { S.note = await call("vault_read", { path }); } catch (e) { S.noteError = e.message; S.note = null; } finally { S.noteLoading = false; render(); }
}
function enterFullscreen() {
  if (kind() === "note" && !S.note) S.note = S.result;
  loadTree();
  if (S.tab === "graph" || kind() === "graph") { S.tab = kind() === "graph" ? "graph" : S.tab; loadGraph(); }
}

root.addEventListener("click", async (ev) => {
  const el = ev.target.closest("[data-act],[data-href],[data-wiki]");
  if (!el) return;
  if (el.dataset.href) { openUrl(el.dataset.href); return; }
  if (el.dataset.wiki !== undefined) { const p = resolveWiki(el.dataset.wiki); if (p) selectNote(p); return; }
  const act = el.dataset.act;
  if (act === "open") openUrl(el.dataset.url);
  else if (act === "expand") await app.requestDisplayMode({ mode: "fullscreen" }).catch(() => {});
  else if (act === "select" || act === "select-graph") selectNote(el.dataset.path);
  else if (act === "tab") { S.tab = el.dataset.t; render(); if (S.tab === "graph") loadGraph(); else loadTree(); }
  else if (act === "pane") { S.pane = el.dataset.p; render(); }
});
root.addEventListener("input", (ev) => {
  if (ev.target.dataset && ev.target.dataset.act === "filter") { S.filter = ev.target.value; render(); }
});
root.addEventListener("keydown", (ev) => {
  const el = ev.target;
  if ((ev.key === "Enter" || ev.key === " ") && el.dataset && el.dataset.act === "select-graph") { ev.preventDefault(); selectNote(el.dataset.path); }
});

// ---------------------------------------------------------------- lifecycle
app.ontoolinput = (p) => { S.input = (p && p.arguments) || null; };
app.ontoolresult = (r) => {
  S.result = r && r.isError ? { kind: "none" } : structured(r) || { kind: "none" };
  if (S.result.ok === false) S.result = { kind: "none" };
  S.tree = null; S.graph = null; S.note = null; S.filter = "";
  render();
  if (isFs()) enterFullscreen();
};
app.addEventListener("hostcontextchanged", (changed) => {
  const wasFs = isFs();
  S.ctx = { ...S.ctx, ...changed };
  kit.applyHost(changed);
  render();
  if (!wasFs && isFs()) enterFullscreen();
});

render();
await app.connect();
S.ctx = { ...(app.getHostContext() || {}) };
kit.applyHost(S.ctx);
render();
