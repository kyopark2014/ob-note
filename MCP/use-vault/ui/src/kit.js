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

  /** Match ob-note `slugifyHeading` for in-document [[#Heading]] / TOC links. */
  function slugifyHeading(text) {
    return String(text || "")
      .trim()
      .toLowerCase()
      .replace(/\s+/g, "-")
      .replace(/[^\p{L}\p{N}\p{M}-]/gu, "");
  }

  function splitTableCells(line) {
    let s = String(line || "").trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|")) s = s.slice(0, -1);
    return s.split("|").map((c) => c.trim());
  }

  function isTableSeparator(line) {
    const cells = splitTableCells(line);
    if (cells.length < 2) return false;
    return cells.every((c) => /^:?-{3,}:?$/.test(c));
  }

  function isTableRow(line) {
    const t = String(line || "").trim();
    if (!t || !t.includes("|")) return false;
    if (isTableSeparator(t)) return false;
    return /^\|.*\|$/.test(t) || /^[^|].*\|.*$/.test(t);
  }

  /**
   * Safe markdown -> HTML approximating ob-note TipTap/GFM preview:
   * headings, lists (nested/task), tables, blockquotes, code, images (http/s),
   * wiki / heading links. Raw HTML in the source is never executed.
   */
  function renderMarkdown(src) {
    const lines = String(src == null ? "" : src).replace(/\r\n?/g, "\n").split("\n");
    const out = [];
    let i = 0;
    /** @type {{ type: string, depth: number }[]} */
    const stack = [];

    const closeTo = (depth) => {
      while (stack.length && stack[stack.length - 1].depth >= depth) {
        out.push(`</${stack.pop().type}>`);
      }
    };
    const closeAll = () => closeTo(0);

    const startsBlock = (line) =>
      /^(#{1,6}\s|```|>|\s*[-*]\s|\s*\d+[.)]\s)/.test(line) ||
      isTableRow(line) ||
      isTableSeparator(line) ||
      /^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line);

    const inline = (t) => {
      const slots = [];
      const park = (html) => {
        const key = `\u0000${slots.length}\u0000`;
        slots.push(html);
        return key;
      };
      let raw = String(t == null ? "" : t);
      // Obsidian wiki image embed: ![[img.png]] / ![[img.png|alt]]
      raw = raw.replace(/!\[\[([^\]|#\n]+?)(?:\|([^\]]+))?\]\]/g, (_, target, alias) => {
        const file = String(target || "").trim();
        const alt = String(alias || file.split("/").pop() || "image").trim();
        if (/^https?:\/\//i.test(file)) {
          return park(`<img src="${esc(file)}" alt="${esc(alt)}" loading="lazy">`);
        }
        return park(`<img data-vault-src="${esc(file)}" alt="${esc(alt)}" loading="lazy">`);
      });
      // Markdown images: remote URL or vault-relative path (hydrated by the viewer).
      raw = raw.replace(/!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, (_, alt, url) => {
        const src = String(url || "").trim().replace(/^<|>$/g, "");
        if (/^https?:\/\//i.test(src) || /^data:/i.test(src)) {
          return park(`<img src="${esc(src)}" alt="${esc(alt)}" loading="lazy">`);
        }
        return park(`<img data-vault-src="${esc(src)}" alt="${esc(alt)}" loading="lazy">`);
      });
      raw = raw.replace(/\[\[([^\]\n]+?)(?:\|([^\]]+))?\]\]/g, (_, target, alias) => {
        const dest = String(target || "").trim();
        const label = String(alias || dest.replace(/^#/, "")).trim() || dest;
        if (dest.startsWith("#")) {
          const slug = slugifyHeading(dest.slice(1));
          return park(`<a data-heading="${esc(slug)}">${esc(label)}</a>`);
        }
        return park(`<a data-wiki="${esc(dest)}">${esc(label)}</a>`);
      });
      // Markdown links: http(s), in-doc #anchors, and other non-empty destinations.
      raw = raw.replace(/\[([^\]]+)\]\((<[^>\n]+>|[^)\s]+)(?:\s+"[^"]*")?\)/g, (_, label, dest) => {
        let href = String(dest || "").trim().replace(/^<|>$/g, "");
        if (!href) return park(esc(label));
        if (/^https?:\/\//i.test(href)) {
          return park(`<a data-href="${esc(href)}">${esc(label)}</a>`);
        }
        if (href.startsWith("#")) {
          const slug = href.slice(1);
          try { decodeURIComponent(slug); } catch (_) { /* keep raw */ }
          return park(`<a data-heading="${esc(slug)}">${esc(label)}</a>`);
        }
        // Relative / path-like — open via host when absolute http was rewritten; otherwise show text.
        return park(`<a data-href="${esc(href)}">${esc(label)}</a>`);
      });
      raw = raw.replace(/`([^`]+)`/g, (_, code) => park(`<code>${esc(code)}</code>`));
      let s = esc(raw);
      s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
      s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
      s = s.replace(/__([^_]+)__/g, "<strong>$1</strong>");
      s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
      s = s.replace(/(^|[^_])_([^_\n]+)_/g, "$1<em>$2</em>");
      s = s.replace(/\u0000(\d+)\u0000/g, (_, n) => slots[Number(n)] || "");
      return s;
    };

    while (i < lines.length) {
      const line = lines[i];
      const fence = /^```(.*)$/.exec(line);
      if (fence) {
        closeAll();
        const buf = [];
        i++;
        while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
        i++;
        out.push(`<pre><code>${esc(buf.join("\n"))}</code></pre>`);
        continue;
      }
      const h = /^(#{1,6})\s+(.*)$/.exec(line);
      if (h) {
        closeAll();
        const level = Math.min(h[1].length, 6);
        const title = h[2];
        const id = slugifyHeading(title);
        out.push(`<h${level} id="${esc(id)}">${inline(title)}</h${level}>`);
        i++;
        continue;
      }
      // GFM table: header row + separator + body rows
      if (isTableRow(line) && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
        closeAll();
        const header = splitTableCells(line);
        i += 2;
        const body = [];
        while (i < lines.length && isTableRow(lines[i])) {
          body.push(splitTableCells(lines[i]));
          i++;
        }
        const thead = `<thead><tr>${header.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead>`;
        const tbody = body.length
          ? `<tbody>${body
              .map((row) => {
                const cells = header.map((_, idx) => row[idx] ?? "");
                return `<tr>${cells.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`;
              })
              .join("")}</tbody>`
          : "";
        out.push(`<div class="k-md-table-wrap"><table>${thead}${tbody}</table></div>`);
        continue;
      }
      if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
        closeAll();
        out.push("<hr>");
        i++;
        continue;
      }
      const task = /^(\s*)[-*]\s+\[( |x|X)\]\s+(.*)$/.exec(line);
      const ul = /^(\s*)[-*]\s+(.*)$/.exec(line);
      const ol = /^(\s*)\d+[.)]\s+(.*)$/.exec(line);
      if (task || ul || ol) {
        const indent = (task || ul || ol)[1].replace(/\t/g, "  ").length;
        const depth = Math.floor(indent / 2) + 1;
        const want = ol ? "ol" : "ul";
        closeTo(depth);
        const top = stack[stack.length - 1];
        if (!top || top.depth < depth || top.type !== want) {
          out.push(`<${want}>`);
          stack.push({ type: want, depth });
        }
        if (task) {
          out.push(
            `<li class="k-task"><input type="checkbox" disabled ${/x/i.test(task[2]) ? "checked" : ""}><span>${inline(task[3])}</span></li>`,
          );
        } else {
          out.push(`<li>${inline((ul || ol)[2])}</li>`);
        }
        i++;
        continue;
      }
      if (/^>\s?/.test(line)) {
        closeAll();
        const buf = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, ""));
        out.push(`<blockquote>${buf.map((b) => `<p>${inline(b)}</p>`).join("")}</blockquote>`);
        continue;
      }
      if (!line.trim()) {
        // GFM loose lists: blank lines inside a list must not restart <ol> (or every item shows "1.").
        let j = i + 1;
        while (j < lines.length && !lines[j].trim()) j++;
        const next = j < lines.length ? lines[j] : "";
        const nextList = /^(\s*)(?:[-*]|\d+[.)])\s+/.test(next);
        if (stack.length && nextList) {
          i++;
          continue;
        }
        closeAll();
        i++;
        continue;
      }
      closeAll();
      const buf = [line];
      i++;
      while (i < lines.length && lines[i].trim() && !startsBlock(lines[i])) {
        buf.push(lines[i++]);
      }
      out.push(`<p>${inline(buf.join(" "))}</p>`);
    }
    closeAll();
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
