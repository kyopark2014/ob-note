// ob-note vault viewer (MCP App) for use-vault: browse, preview, and edit notes.
// Claude MCP Apps design guidelines: inline carousel / card, fullscreen explorer, no menus,
// host style tokens, skeleton loading, 44px targets. Note editing mirrors ob-note's
// Preview / Edit / Save toolbar (source markdown textarea + share-style HTML preview).
const { App } = window.__ext;
const root = document.getElementById("root");
const MAX_INLINE = 8;

const app = new App(
  { name: "Vault viewer", version: "1.0.0" },
  { availableDisplayModes: ["inline", "fullscreen"] },
);

const ICONS = {
  book: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M4 5a2 2 0 0 1 2-2h12v18H6a2 2 0 0 0-2 2V5z"/><path d="M8 7h8M8 11h8"/></svg>`,
  edit: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>`,
  save: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15.2 3a2 2 0 0 1 1.4.6l3.8 3.8a2 2 0 0 1 .6 1.4V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/><path d="M17 21v-7a1 1 0 0 0-1-1H8a1 1 0 0 0-1 1v7"/><path d="M7 3v4a1 1 0 0 0 1 1h7"/></svg>`,
};

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
  fsMode: "explorer",   // fullscreen: "explorer" (tree+reader) | "focus" (reader only)
  pendingPath: null,    // open this note once fullscreen is granted
  pendingFocus: false,  // enter fullscreen as focus (열기) rather than explorer (전체 보기)
  viewMode: "preview",  // "preview" | "edit" (ob-note toolbar)
  draft: "",            // markdown being edited
  savedContent: "",     // last loaded/saved content (dirty baseline)
  dirty: false,
  saving: false,
  saveError: null,
  previewLoading: false, // re-render via vault_render (server HTML)
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

/** Resolve a markdown/wiki image path relative to the open note (ob-note semantics). */
function resolveVaultAssetPath(notePath, src) {
  let cleaned = String(src || "").replace(/\\/g, "/").trim();
  try { cleaned = decodeURIComponent(cleaned); } catch (_) { /* keep raw */ }
  cleaned = cleaned.replace(/^<|>$/g, "");
  if (!cleaned) return "";
  if (/^(https?:|data:|blob:)/i.test(cleaned) || cleaned.startsWith("/")) return cleaned;
  const dir = notePath && notePath.includes("/") ? notePath.slice(0, notePath.lastIndexOf("/")) : "";
  const parts = [...(dir ? dir.split("/") : []), ...cleaned.split("/")];
  const out = [];
  for (const part of parts) {
    if (!part || part === ".") continue;
    if (part === "..") { out.pop(); continue; }
    out.push(part);
  }
  return out.join("/");
}

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
  const snip = n.snippet || n.body_preview || "";
  // Prefer in-app fullscreen reader; fall back to external ob-note link when path is missing.
  const open = n.path
    ? `<button class="k-btn" data-act="open-note" data-path="${kit.esc(n.path)}" aria-label="${kit.esc(title + " 전체 화면으로 열기")}">열기</button>`
    : openBtn(n.url, title + " 열기");
  return `<article class="vv-card" role="listitem">
    <div class="vv-title">${kit.esc(title)}</div>
    <div class="k-caption k-faint">${kit.esc(meta)}</div>
    ${snip ? `<div class="vv-snip k-caption k-muted">${kit.esc(String(snip).slice(0, 200))}</div>` : ""}
    ${(n.tags || []).length ? `<div>${n.tags.slice(0, 3).map((t) => `<span class="vv-chip">${kit.esc(t)}</span>`).join(" ")}</div>` : ""}
    ${open}</article>`;
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
  const fsBtn = n.path && canFullscreen()
    ? `<button class="k-btn k-primary" data-act="open-note" data-path="${kit.esc(n.path)}">전체 화면으로 열기</button>`
    : (canFullscreen() ? `<button class="k-btn k-primary" data-act="expand">전체 보기</button>` : "");
  return `<div class="vv-note">
    <div class="vv-title">${kit.esc(n.title || stem(n.path))}</div>
    <div class="vv-facts k-caption k-faint">${facts.map((f) => `<span>${kit.esc(f)}</span>`).join("")}</div>
    <div class="vv-excerpt k-muted">${kit.esc(kit.excerpt(n.content, 320))}</div>
    <div class="vv-actions">${fsBtn}
      ${kit.safeUrl(n.url) ? `<button class="k-btn" data-act="open" data-url="${kit.esc(n.url)}">ob-note에서 열기</button>` : ""}</div></div>`;
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
    <div data-role="tree-list">${body || `<div class="k-empty k-muted">노트가 없습니다.</div>`}</div></div>`;
}

function decodeHtmlEntities(s) {
  return String(s || "")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&nbsp;/g, " ");
}

function bindNote(n, { keepMode = false } = {}) {
  S.note = n || null;
  S.draft = String((n && n.content) || "");
  S.savedContent = S.draft;
  S.dirty = false;
  S.saving = false;
  S.saveError = null;
  if (!keepMode) S.viewMode = "preview";
}

/**
 * Note body uses ob-note's public-share HTML pipeline only (server ``content_html`` /
 * ``vault_render``). The client kit markdown renderer is not used for the reader —
 * TipTap/React cannot run inside this MCP App iframe.
 */
function noteBodyHtml(n) {
  if (S.previewLoading) {
    return `<div aria-busy="true">${kit.skeleton(5, 16)}</div>`;
  }
  const title = String(n.title || stem(n.path)).trim();
  let html = typeof n.content_html === "string" ? n.content_html.trim() : "";
  if (!html) {
    return `<div class="k-empty k-muted">미리보기를 준비하지 못했습니다. Preview를 다시 눌러 주세요.</div>`;
  }
  html = html.replace(/^\s*<h1\b[^>]*>[\s\S]*?<\/h1>/i, (m) => {
    const plain = decodeHtmlEntities(m.replace(/<[^>]+>/g, "")).trim();
    return plain === title ? "" : m;
  });
  return `<article class="markdown-body">${html}</article>`;
}

/** Re-render draft/saved markdown via ob-note ``/api/files/render`` (share pipeline). */
async function refreshPreviewHtml() {
  if (!S.note) return;
  const content = S.draft != null ? S.draft : String(S.note.content || "");
  S.previewLoading = true;
  try {
    const data = await call("vault_render", {
      content,
      path: S.note.path || "",
    });
    if (data && typeof data.html === "string") {
      S.note = { ...S.note, content_html: data.html, content };
    }
  } catch (e) {
    S.saveError = e.message || "미리보기 렌더에 실패했습니다.";
  } finally {
    S.previewLoading = false;
  }
}

function modeToolbar() {
  const previewOn = S.viewMode === "preview";
  const editOn = S.viewMode === "edit";
  const canSave = S.dirty && !S.saving;
  // Icon + short label (ob-note tooltips); labels stay visible even if SVG color tokens fail.
  return `<div class="vv-mode-toolbar" role="toolbar" aria-label="Preview Edit Save">
    <button type="button" class="k-btn vv-mode-btn${previewOn ? " vv-mode-on" : ""}" data-act="mode-preview" aria-label="Preview" aria-pressed="${previewOn}">${ICONS.book}<span>Preview</span></button>
    <button type="button" class="k-btn vv-mode-btn${editOn ? " vv-mode-on" : ""}" data-act="mode-edit" aria-label="Edit" aria-pressed="${editOn}">${ICONS.edit}<span>Edit</span></button>
    <button type="button" class="k-btn vv-mode-btn${canSave ? "" : " is-disabled"}" data-act="mode-save" aria-label="${S.saving ? "Saving" : "Save"}" aria-disabled="${canSave ? "false" : "true"}" style="opacity:${canSave ? "1" : "0.4"}">${ICONS.save}<span>${S.saving ? "Saving…" : "Save"}</span></button>
  </div>`;
}

function updateSaveButton() {
  const btn = root.querySelector("[data-act=mode-save]");
  if (!btn) return;
  const canSave = S.dirty && !S.saving;
  btn.classList.toggle("is-disabled", !canSave);
  btn.style.opacity = canSave ? "1" : "0.4";
  btn.setAttribute("aria-disabled", canSave ? "false" : "true");
  btn.setAttribute("aria-label", S.saving ? "Saving" : "Save");
  const label = btn.querySelector("span");
  if (label) label.textContent = S.saving ? "Saving…" : "Save";
}

function editorPane() {
  return `<div class="vv-editor-pane"><textarea class="vv-editor" data-act="edit" spellcheck="false" aria-label="노트 마크다운 편집">${kit.esc(S.draft)}</textarea></div>`;
}

function readerPane(opts = {}) {
  const focus = opts.focus === true;
  if (S.noteLoading) return `<div class="vv-reader${focus ? " vv-reader-focus" : ""}" aria-busy="true">${kit.skeleton(1, 28)}${kit.skeleton(5, 16)}</div>`;
  if (S.noteError) return `<div class="vv-reader${focus ? " vv-reader-focus" : ""}"><div class="k-error" role="alert">${kit.esc(S.noteError)}</div></div>`;
  const n = S.note;
  if (!n) {
    return `<div class="vv-reader${focus ? " vv-reader-focus" : ""} k-empty k-muted">${
      focus ? "노트를 불러오는 중이거나 표시할 내용이 없습니다." : "왼쪽에서 노트를 선택하세요."
    }</div>`;
  }
  const back = S.viewMode === "preview"
    ? (n.backlinks || []).map((b) => `<button class="vv-item" data-act="select" data-path="${kit.esc(b.path)}">${kit.esc(b.title || stem(b.path))}</button>`).join("")
    : "";
  const actions = [
    modeToolbar(),
    focus ? `<button class="k-btn" data-act="show-explorer" type="button">목록 보기</button>` : "",
    !focus && isFs() && !compact() ? `<button class="k-btn" data-act="hide-list" type="button">목록 숨기기</button>` : "",
    kit.safeUrl(n.url) ? `<button class="k-btn" data-act="open" data-url="${kit.esc(n.url)}">ob-note에서 열기</button>` : "",
  ].filter(Boolean).join("");
  // Toolbar always sits above the title (focus and explorer alike).
  const actionBar = `<div class="vv-head-actions vv-head-actions-top">${actions}</div>`;
  const head = `<div class="vv-head vv-head-stack">${actionBar}<h2>${kit.esc(n.title || stem(n.path))}${S.dirty ? '<span class="vv-dirty" title="수정됨">•</span>' : ""}</h2></div>`;
  const body = S.viewMode === "edit"
    ? editorPane()
    : `${noteBodyHtml(n)}${back ? `<div class="vv-back"><div class="k-strong">백링크 ${n.backlinks.length}</div>${back}</div>` : ""}`;
  return `<div class="vv-reader${focus ? " vv-reader-focus" : ""}">
    ${head}
    <div class="vv-facts k-caption k-faint"><span>${kit.esc(n.path)}</span>${n.updated_at ? `<span>수정 ${kit.esc(kit.fmtDate(n.updated_at))}</span>` : ""}
      ${(n.tags || []).map((t) => `<span class="vv-chip">${kit.esc(t)}</span>`).join(" ")}</div>
    ${S.saveError ? `<div class="k-error" role="alert">${kit.esc(S.saveError)}</div>` : ""}
    ${body}</div>`;
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
  // 열기 → focus: document only (no left tree). 전체 보기 → explorer: tree + reader (no 노트/그래프 tabs).
  if (S.fsMode === "focus") {
    return `<div class="vv-fs vv-fs-focus">${readerPane({ focus: true })}</div>`;
  }
  // vault_graph tool result: show the graph canvas only.
  if (kind() === "graph" || S.tab === "graph") {
    return `<div class="vv-fs"><div style="min-height:0;overflow:auto;flex:1">${graphPane()}</div></div>`;
  }
  let body;
  if (compact()) {
    const panes = `<div class="k-seg" role="group" aria-label="영역" style="margin-bottom:6px"><button data-act="pane" data-p="tree" aria-pressed="${S.pane === "tree"}">목록</button><button data-act="pane" data-p="reader" aria-pressed="${S.pane === "reader"}">본문</button></div>`;
    body = `<div class="vv-body vv-compact"><div style="min-height:0;display:flex;flex-direction:column">${panes}${S.pane === "tree" ? treePane() : readerPane()}</div></div>`;
  } else {
    body = `<div class="vv-body">${treePane()}${readerPane()}</div>`;
  }
  return `<div class="vv-fs">${body}</div>`;
}

function render() {
  if (S.result === undefined) { root.innerHTML = `<div aria-busy="true"><div class="k-skel" style="height:176px"></div></div>`; return; }
  const active = document.activeElement;
  const keepFilter = active && active.dataset && active.dataset.act === "filter";
  const keepEdit = active && active.dataset && active.dataset.act === "edit";
  const editSel = keepEdit ? { start: active.selectionStart, end: active.selectionEnd } : null;
  root.innerHTML = isFs() ? fullscreen() : inline();
  if (keepFilter) {
    const el = root.querySelector("[data-act=filter]");
    if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length); }
  } else if (keepEdit) {
    const el = root.querySelector("[data-act=edit]");
    if (el) {
      el.focus();
      if (editSel) el.setSelectionRange(editSel.start, editSel.end);
    }
  } else if (S.viewMode === "edit" && isFs()) {
    const el = root.querySelector("[data-act=edit]");
    if (el) {
      el.focus();
      // Grow textarea to content (field-sizing fallback).
      el.style.height = "auto";
      el.style.height = `${Math.max(el.scrollHeight, 240)}px`;
    }
  }
  if (S.viewMode === "preview") hydrateVaultImages();
}

/** Load vault-relative images via vault_asset → data URL (iframe cannot hit /api/files/raw). */
async function hydrateVaultImages() {
  const imgs = Array.from(root.querySelectorAll("img[data-vault-src]"));
  if (!imgs.length) return;
  const notePath =
    (S.note && S.note.path) ||
    (kind() === "note" && S.result && S.result.path) ||
    "";
  await Promise.all(
    imgs.map(async (img) => {
      const src = img.getAttribute("data-vault-src") || "";
      const path = resolveVaultAssetPath(notePath, src);
      if (!path || /^(https?:|data:|blob:)/i.test(path)) return;
      if (img.dataset.hydrating === "1") return;
      img.dataset.hydrating = "1";
      try {
        const data = await call("vault_asset", { path });
        if (data && data.data_url) {
          img.src = data.data_url;
          img.removeAttribute("data-vault-src");
          img.removeAttribute("data-hydrating");
        }
      } catch (e) {
        const fallback = document.createElement("span");
        fallback.className = "k-caption k-faint";
        fallback.textContent = `[이미지 로드 실패: ${src}]`;
        img.replaceWith(fallback);
      }
    }),
  );
}

// ---------------------------------------------------------------- actions
const openUrl = (u) => { const url = kit.safeUrl(u); if (url) app.openLink({ url }).catch(() => {}); };

async function loadTree(force = false) {
  if (!force && (S.tree || S.treeLoading)) return;
  if (!force && kind() === "tree") { S.tree = S.result; return; }
  S.treeLoading = true; S.treeError = null; render();
  try { S.tree = await call("vault_tree", {}); } catch (e) { S.treeError = e.message; } finally { S.treeLoading = false; render(); }
}
async function loadGraph(force = false) {
  if (!force && (S.graph || S.graphLoading)) return;
  if (!force && kind() === "graph") { S.graph = S.result; return; }
  S.graphLoading = true; S.graphError = null; render();
  try { S.graph = await call("vault_graph", {}); } catch (e) { S.graphError = e.message; } finally { S.graphLoading = false; render(); }
}
async function selectNote(path) {
  S.tab = "notes"; S.pane = "reader"; S.noteLoading = true; S.noteError = null;
  S.viewMode = "preview"; S.dirty = false; S.saveError = null; S.previewLoading = false;
  render();
  try {
    const data = await call("vault_read", { path });
    bindNote(data);
    // Older API / cache without content_html → render on the server.
    if (S.note && !(typeof S.note.content_html === "string" && S.note.content_html.trim())) {
      S.noteLoading = false;
      render();
      await refreshPreviewHtml();
    }
  } catch (e) {
    S.noteError = e.message;
    bindNote(null);
  } finally {
    S.noteLoading = false;
    render();
  }
}

async function saveNote() {
  if (!S.note || !S.note.path || !S.dirty || S.saving) return;
  S.saving = true;
  S.saveError = null;
  updateSaveButton();
  try {
    const path = S.note.path;
    await call("vault_write", { path, content: S.draft });
    const data = await call("vault_read", { path });
    bindNote(data, { keepMode: true });
    if (kind() === "note" && S.result && S.result.path === path) S.result = data;
    // Keep tree labels fresh after save.
    if (isFs() && S.fsMode === "explorer") loadTree(true).catch(() => {});
  } catch (e) {
    S.saveError = e.message || "저장에 실패했습니다.";
    S.saving = false;
  }
  render();
}

function enterFullscreen() {
  if (S.pendingFocus) {
    S.fsMode = "focus";
    S.pendingFocus = false;
  } else if (S.fsMode !== "focus") {
    S.fsMode = "explorer";
  }
  if (kind() === "note" && !S.note) {
    bindNote(S.result);
    if (S.note && !(typeof S.note.content_html === "string" && S.note.content_html.trim())) {
      refreshPreviewHtml().then(() => render()).catch(() => {});
    }
  }
  if (S.fsMode === "explorer") {
    if (kind() === "graph") {
      S.tab = "graph";
      loadGraph();
    } else {
      S.tab = "notes";
      loadTree();
    }
  }
  if (S.pendingPath) {
    const path = S.pendingPath;
    S.pendingPath = null;
    selectNote(path);
  } else {
    render();
  }
}

/** Open a single note fullscreen without the left tree (card 열기). */
async function openNoteFullscreen(path) {
  if (!path) return;
  S.pendingPath = path;
  S.pendingFocus = true;
  S.tab = "notes";
  S.pane = "reader";
  if (isFs()) {
    S.fsMode = "focus";
    S.pendingPath = null;
    S.pendingFocus = false;
    await selectNote(path);
    return;
  }
  if (canFullscreen()) {
    try {
      await app.requestDisplayMode({ mode: "fullscreen" });
    } catch (_) {
      S.pendingPath = null;
      S.pendingFocus = false;
      await selectNote(path);
    }
    return;
  }
  S.pendingPath = null;
  S.pendingFocus = false;
  await selectNote(path);
}

async function enterExplorerFullscreen() {
  S.fsMode = "explorer";
  S.pendingFocus = false;
  S.tab = "notes";
  if (isFs()) {
    loadTree();
    render();
    return;
  }
  if (canFullscreen()) {
    await app.requestDisplayMode({ mode: "fullscreen" }).catch(() => {});
  }
}

/** Host notifies after vault_write/append/… so the open explorer shows fresh content. */
async function refreshAfterMutation(hintPath) {
  const openPath =
    (S.note && S.note.path) ||
    (kind() === "note" && S.result && S.result.path) ||
    null;
  const targetPath = openPath || hintPath || null;

  // Do not clobber in-progress edits with an external reload.
  if (S.dirty) {
    if (isFs()) await loadTree(true);
    render();
    return;
  }

  if (isFs()) {
    await loadTree(true);
    if (S.tab === "graph") await loadGraph(true);
    if (targetPath) await selectNote(targetPath);
    else render();
    return;
  }

  if (kind() === "note" && S.result && S.result.path) {
    if (!hintPath || hintPath === S.result.path) {
      try {
        const data = await call("vault_read", { path: S.result.path });
        S.result = data;
        bindNote(data, { keepMode: true });
      } catch (e) {
        S.noteError = e.message;
      }
      render();
    }
    return;
  }

  if (kind() === "tree") {
    try {
      const data = await call("vault_tree", {});
      S.result = data;
      S.tree = data;
    } catch (e) {
      S.treeError = e.message;
    }
    render();
    return;
  }

  if (kind() === "list") {
    try {
      const data = await call("vault_list", { prefix: S.result.prefix || "" });
      S.result = data;
    } catch (_) { /* keep previous list */ }
    render();
  }
}

root.addEventListener("click", async (ev) => {
  // Public-share style HTML: in-page anchors (#heading).
  const hashA = ev.target.closest("a[href^='#']");
  if (hashA && root.contains(hashA) && hashA.dataset.wiki === undefined) {
    const raw = (hashA.getAttribute("href") || "").slice(1);
    if (raw) {
      let id = raw;
      try { id = decodeURIComponent(raw); } catch (_) { /* keep raw */ }
      const target = root.querySelector(`#${CSS.escape(id)}`);
      if (target) {
        target.scrollIntoView({ behavior: "smooth", block: "start" });
        ev.preventDefault();
        return;
      }
    }
  }
  const el = ev.target.closest("[data-act],[data-href],[data-wiki],[data-heading]");
  if (!el) return;
  if (el.dataset.href) { openUrl(el.dataset.href); return; }
  if (el.dataset.heading !== undefined) {
    const target = root.querySelector(`#${CSS.escape(el.dataset.heading)}`);
    if (target) {
      target.scrollIntoView({ behavior: "smooth", block: "start" });
      ev.preventDefault();
    }
    return;
  }
  if (el.dataset.wiki !== undefined) {
    ev.preventDefault();
    const p = resolveWiki(el.dataset.wiki);
    if (p) selectNote(p);
    return;
  }
  const act = el.dataset.act;
  if (act === "open") openUrl(el.dataset.url);
  else if (act === "open-note") await openNoteFullscreen(el.dataset.path);
  else if (act === "expand") await enterExplorerFullscreen();
  else if (act === "mode-preview") {
    S.viewMode = "preview";
    // Always use server HTML (ob-note share pipeline), especially after edits.
    if (S.dirty || !(S.note && S.note.content_html)) {
      render();
      await refreshPreviewHtml();
    }
    render();
  }
  else if (act === "mode-edit") {
    S.viewMode = "edit";
    render();
  }
  else if (act === "mode-save") {
    if (el.getAttribute("aria-disabled") === "true") return;
    await saveNote();
  }
  else if (act === "hide-list") {
    S.fsMode = "focus";
    render();
  }
  else if (act === "show-explorer") {
    S.fsMode = "explorer";
    S.tab = "notes";
    S.pane = "reader";
    loadTree();
    render();
  }
  else if (act === "select" || act === "select-graph") {
    if (act === "select-graph") {
      S.fsMode = "focus";
      S.tab = "notes";
    }
    selectNote(el.dataset.path);
  }
  else if (act === "pane") { S.pane = el.dataset.p; render(); }
});
// Preview media use short-lived signed URLs. If one expired (note opened long after the
// tool call), re-render the note once to get fresh links instead of showing broken images.
let previewRefreshedAt = 0;
root.addEventListener("error", async (ev) => {
  const img = ev.target;
  if (!(img instanceof HTMLImageElement) || !String(img.src).includes("/s/_preview/raw")) return;
  if (S.viewMode !== "preview" || !S.note || Date.now() - previewRefreshedAt < 30000) return;
  previewRefreshedAt = Date.now();
  await refreshPreviewHtml();
  render();
}, true);
root.addEventListener("input", (ev) => {
  const t = ev.target;
  if (!t || !t.dataset) return;
  if (t.dataset.act === "filter") {
    // Never re-render the <input> itself: that kills Korean (IME) composition.
    S.filter = t.value;
    if (ev.isComposing) return;
    updateTreeList();
    return;
  }
  if (t.dataset.act === "edit") {
    S.draft = t.value;
    S.dirty = S.draft !== S.savedContent;
    // Keep typing smooth: resize + save affordance only (no full re-render).
    t.style.height = "auto";
    t.style.height = `${Math.max(t.scrollHeight, 240)}px`;
    updateSaveButton();
    const dirtyMark = root.querySelector(".vv-dirty");
    if (S.dirty && !dirtyMark) {
      const h2 = root.querySelector(".vv-reader h2");
      if (h2) h2.insertAdjacentHTML("beforeend", '<span class="vv-dirty" title="수정됨">•</span>');
    } else if (!S.dirty && dirtyMark) {
      dirtyMark.remove();
    }
  }
});
/** Refresh only the note list for the current filter (keeps the input/IME intact). */
function updateTreeList() {
  const list = root.querySelector("[data-role=tree-list]");
  if (!list) return;
  const q = S.filter.trim().toLowerCase();
  list.innerHTML = treeHtml(S.tree && S.tree.children, q) || `<div class="k-empty k-muted">노트가 없습니다.</div>`;
}
root.addEventListener("compositionend", (ev) => {
  const t = ev.target;
  if (t && t.dataset && t.dataset.act === "filter") { S.filter = t.value; updateTreeList(); }
});
root.addEventListener("keydown", (ev) => {
  if ((ev.metaKey || ev.ctrlKey) && String(ev.key || "").toLowerCase() === "s") {
    if (!S.note || !S.dirty) return;
    ev.preventDefault();
    saveNote();
    return;
  }
  const el = ev.target;
  if ((ev.key === "Enter" || ev.key === " ") && el.dataset && el.dataset.act === "select-graph") {
    ev.preventDefault();
    selectNote(el.dataset.path);
  }
});

// ---------------------------------------------------------------- lifecycle
app.ontoolinput = (p) => { S.input = (p && p.arguments) || null; };
app.ontoolresult = (r) => {
  S.result = r && r.isError ? { kind: "none" } : structured(r) || { kind: "none" };
  if (S.result.ok === false) S.result = { kind: "none" };
  S.tree = null; S.graph = null; S.note = null; S.filter = "";
  S.viewMode = "preview"; S.draft = ""; S.savedContent = ""; S.dirty = false; S.saving = false; S.saveError = null;
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

// agentic-work host posts this after mutation tools; App client ignores unknown methods.
window.addEventListener("message", (ev) => {
  const data = ev && ev.data;
  if (!data || data.method !== "ui/notifications/vault-refresh") return;
  const path = data.params && data.params.path;
  refreshAfterMutation(typeof path === "string" ? path : undefined).catch(() => {});
});

render();
await app.connect();
S.ctx = { ...(app.getHostContext() || {}) };
kit.applyHost(S.ctx);
render();
