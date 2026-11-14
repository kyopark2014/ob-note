// Shared helpers for MCP App UIs. Plain functions; no framework. Attached as `kit`.
const kit = (() => {
  const { applyDocumentTheme, applyHostStyleVariables, applyHostFonts } = window.__ext;

  const esc = (s) =>
    String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const safeUrl = (u) => (typeof u === "string" && /^https?:\/\//i.test(u.trim()) ? u.trim() : "");

  /** Apply theme, host style variables, fonts and safe-area insets. Safe to call per update. */
  function applyHost(ctx) {
    if (!ctx) return;
    if (ctx.theme) applyDocumentTheme(ctx.theme);
    if (ctx.styles && ctx.styles.variables) applyHostStyleVariables(ctx.styles.variables);
    if (ctx.styles && ctx.styles.css && ctx.styles.css.fonts) applyHostFonts(ctx.styles.css.fonts);
    const s = ctx.safeAreaInsets;
    if (s) {
      const r = document.documentElement.style;
      r.setProperty("--safe-top", (s.top || 0) + "px");
      r.setProperty("--safe-right", (s.right || 0) + "px");
      r.setProperty("--safe-bottom", (s.bottom || 0) + "px");
      r.setProperty("--safe-left", (s.left || 0) + "px");
    }
  }

  const skeleton = (n, h) =>
    Array.from({ length: n }, () => `<div class="k-skel" style="height:${h || 16}px;margin:8px 0"></div>`).join("");

  function fmtDate(s) {
    if (!s) return "";
    const d = new Date(s);
    return isNaN(d.getTime()) ? "" : d.toLocaleDateString("ko-KR", { year: "numeric", month: "short", day: "numeric" });
  }

  /**
   * Minimal, safe markdown -> HTML. Everything is escaped first; only a fixed set of
   * constructs is turned back into markup, and links must be http(s). Note content is
   * untrusted, so raw HTML in the source is shown as text, never executed.
   */
  function renderMarkdown(src) {
    const lines = String(src == null ? "" : src).replace(/\r\n?/g, "\n").split("\n");
    const out = [];
    let i = 0, listType = null;
    const closeList = () => { if (listType) { out.push(`</${listType}>`); listType = null; } };
    const inline = (t) => {
      let s = esc(t);
      s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
      s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
      s = s.replace(/\[\[([^\]\n]+)\]\]/g, (_, name) => `<a data-wiki="${name}">${name}</a>`);
      s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, (_, label, url) => `<a data-href="${url}">${label}</a>`);
      return s;
    };
    while (i < lines.length) {
      const line = lines[i];
      const fence = /^```(.*)$/.exec(line);
      if (fence) {
        closeList(); const buf = []; i++;
        while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
        i++; out.push(`<pre><code>${esc(buf.join("\n"))}</code></pre>`); continue;
      }
      const h = /^(#{1,4})\s+(.*)$/.exec(line);
      if (h) { closeList(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); i++; continue; }
      if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) { closeList(); out.push("<hr>"); i++; continue; }
      const task = /^\s*[-*]\s+\[( |x|X)\]\s+(.*)$/.exec(line);
      const ul = /^\s*[-*]\s+(.*)$/.exec(line);
      const ol = /^\s*\d+[.)]\s+(.*)$/.exec(line);
      if (task || ul || ol) {
        const want = ol ? "ol" : "ul";
        if (listType !== want) { closeList(); out.push(`<${want}>`); listType = want; }
        if (task) out.push(`<li class="k-task"><input type="checkbox" disabled ${/x/i.test(task[1]) ? "checked" : ""}> ${inline(task[2])}</li>`);
        else out.push(`<li>${inline((ul || ol)[1])}</li>`);
        i++; continue;
      }
      if (/^>\s?/.test(line)) { closeList(); const buf = []; while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, "")); out.push(`<blockquote>${inline(buf.join(" "))}</blockquote>`); continue; }
      if (!line.trim()) { closeList(); i++; continue; }
      closeList(); const buf = [line]; i++;
      while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|```|>|\s*[-*]\s|\s*\d+[.)]\s)/.test(lines[i])) buf.push(lines[i++]);
      out.push(`<p>${inline(buf.join(" "))}</p>`);
    }
    closeList();
    return out.join("\n");
  }

  /** First ~n characters of readable text from markdown (for inline excerpts). */
  function excerpt(md, n) {
    const t = String(md || "").replace(/```[\s\S]*?```/g, " ").replace(/<[^>]+>/g, " ").replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
      .replace(/^#{1,6}\s*/gm, "").replace(/[*_`>#-]/g, "").replace(/\s+/g, " ").trim();
    return t.length > n ? t.slice(0, n).trimEnd() + "…" : t;
  }

  return { esc, safeUrl, applyHost, skeleton, fmtDate, renderMarkdown, excerpt };
})();
