"""CSP-safe HTML helpers for public markdown share pages (no inline scripts)."""

from __future__ import annotations

import html
import mimetypes
import re
import unicodedata
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlsplit


def slugify_heading(text: str) -> str:
    """Match web ``slugifyHeading`` (Unicode letters/numbers/marks + hyphen)."""
    s = unicodedata.normalize("NFC", (text or "").strip()).lower()
    s = re.sub(r"\s+", "-", s)
    out: list[str] = []
    for ch in s:
        if ch == "-":
            out.append(ch)
            continue
        cat = unicodedata.category(ch)
        if cat.startswith("L") or cat.startswith("N") or cat in {"Mn", "Mc", "Me"}:
            out.append(ch)
    return "".join(out) or "section"


def _unique_slugger() -> Callable[[str], str]:
    counts: dict[str, int] = {}

    def slug(text: str) -> str:
        base = slugify_heading(text) or "section"
        n = counts.get(base, 0)
        counts[base] = n + 1
        return base if n == 0 else f"{base}-{n}"

    return slug


def _strip_md_inline(text: str) -> str:
    t = text or ""
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t)
    t = re.sub(r"`([^`]+)`", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    return t.strip()


_TOC_TITLE_RE = re.compile(
    r"^(목차|contents|table of contents|toc)$",
    re.IGNORECASE,
)


def linkify_toc_in_markdown(text: str) -> str:
    """Turn plain TOC list items into ``[Heading](#slug)`` when they match a heading."""
    lines = (text or "").splitlines()
    headings: list[str] = []
    for line in lines:
        m = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
        if m:
            headings.append(_strip_md_inline(m.group(2)))

    heading_by_key: dict[str, str] = {}
    for h in headings:
        key = unicodedata.normalize("NFC", h.strip().lower())
        heading_by_key.setdefault(key, h)

    out: list[str] = []
    in_toc = False
    toc_level = 0
    for line in lines:
        stripped = line.strip()
        hm = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if hm:
            level = len(hm.group(1))
            title = _strip_md_inline(hm.group(2))
            if _TOC_TITLE_RE.match(title):
                in_toc = True
                toc_level = level
            elif in_toc and level <= toc_level:
                in_toc = False
            out.append(line)
            continue

        if in_toc:
            lm = re.match(r"^([ \t]*(?:[-*+]|\d+\.))(\s+)(.*)$", line)
            if lm:
                bullet, sp, item = lm.group(1), lm.group(2), lm.group(3).strip()
                # Obsidian same-doc heading: [[#Heading]] or [[#Heading|alias]]
                wiki_heading = re.match(
                    r"^\[\[#([^\]|]+)(?:\|([^\]]+))?\]\]$", item
                )
                if wiki_heading:
                    heading_text = wiki_heading.group(1).strip()
                    alias = (wiki_heading.group(2) or heading_text).strip()
                    key = unicodedata.normalize(
                        "NFC", _strip_md_inline(heading_text).lower()
                    )
                    title = heading_by_key.get(key, heading_text)
                    slug = slugify_heading(title)
                    out.append(f"{bullet}{sp}[{alias}](#{slug})")
                    continue
                if (
                    re.match(r"^\[.+\]\([^)]+\)$", item)
                    or item.startswith("[[")
                    or item.startswith("http://")
                    or item.startswith("https://")
                ):
                    out.append(line)
                    continue
                key = unicodedata.normalize("NFC", _strip_md_inline(item).lower())
                if key in heading_by_key:
                    slug = slugify_heading(heading_by_key[key])
                    out.append(f"{bullet}{sp}[{item}](#{slug})")
                    continue
        out.append(line)
    return "\n".join(out)


def add_heading_ids(html_body: str) -> str:
    """Add unique ``id`` attributes to ``h1``–``h6`` (skip if already present)."""
    slugger = _unique_slugger()

    def repl(match: re.Match[str]) -> str:
        level = match.group(1)
        attrs = match.group(2) or ""
        inner = match.group(3)
        if re.search(r"\bid\s*=", attrs, flags=re.I):
            return match.group(0)
        plain = re.sub(r"<[^>]+>", "", inner)
        plain = html.unescape(plain).strip()
        sid = html.escape(slugger(plain), quote=True)
        return f"<h{level} id=\"{sid}\"{attrs}>{inner}</h{level}>"

    return re.sub(
        r"<h([1-6])(\s[^>]*)?>(.*?)</h\1>",
        repl,
        html_body or "",
        flags=re.IGNORECASE | re.DOTALL,
    )


def _simple_markdown_to_html(text: str) -> str:
    escaped = html.escape(text or "")
    lines = escaped.splitlines()
    out: list[str] = []
    in_code = False
    in_ul = False
    slugger = _unique_slugger()

    def inline_format(line: str) -> str:
        rendered = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        rendered = re.sub(r"`([^`]+)`", r"<code>\1</code>", rendered)
        rendered = re.sub(
            r"!\[([^\]]*)\]\(([^)\n]+)\)",
            r'<img src="\2" alt="\1" />',
            rendered,
        )
        rendered = re.sub(
            r"\[([^\]]+)\]\(([^)\n]+)\)",
            r'<a href="\2">\1</a>',
            rendered,
        )
        return rendered

    for line in lines:
        if line.strip().startswith("```"):
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                if in_ul:
                    out.append("</ul>")
                    in_ul = False
                out.append("<pre><code>")
                in_code = True
            continue
        if in_code:
            out.append(line + "\n")
            continue
        heading = re.match(r"^(#{1,4})\s+(.*)$", line)
        if heading:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            level = len(heading.group(1))
            inner = inline_format(heading.group(2))
            plain = html.unescape(re.sub(r"<[^>]+>", "", inner)).strip()
            sid = html.escape(slugger(plain), quote=True)
            out.append(f'<h{level} id="{sid}">{inner}</h{level}>')
            continue
        if re.match(r"^[-*]\s+", line):
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{inline_format(re.sub(r'^[-*]\s+', '', line))}</li>")
            continue
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if not line.strip():
            out.append("")
            continue
        out.append(f"<p>{inline_format(line)}</p>")
    if in_code:
        out.append("</code></pre>")
    if in_ul:
        out.append("</ul>")
    return "\n".join(out)


_VIDEO_SUFFIXES = {".mp4", ".m4v", ".webm"}
_VIDEO_MEDIA = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm"}
_WIKI_EMBED_RE = re.compile(r"!\[\[([^\]|#]+?)(?:#[^\]|]+)?(?:\|([^\]]+))?\]\]")
_HTML_VIDEO_RE = re.compile(
    r"<video\b([^>]*)>(.*?)</video>|<video\b([^>]*)/?>",
    re.IGNORECASE | re.DOTALL,
)
_HTML_ATTR_RE_TMPL = r"""\b{name}\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>]+))"""


def media_type_for_name(name: str) -> str:
    """MIME type for vault binaries. mp4 is video/mp4 so browsers can play it."""
    ext = Path(name).suffix.lower()
    if ext in _VIDEO_MEDIA:
        return _VIDEO_MEDIA[ext]
    media, _ = mimetypes.guess_type(name)
    return media or "application/octet-stream"


def _html_attr(tag: str, name: str) -> str | None:
    match = re.search(_HTML_ATTR_RE_TMPL.format(name=re.escape(name)), tag, re.IGNORECASE)
    if not match:
        return None
    for group in match.groups():
        if group is not None:
            return html.unescape(group)
    return None


def _video_suffix(ref: str) -> str:
    raw = (ref or "").strip()
    if raw.startswith("<") and raw.endswith(">"):
        raw = raw[1:-1].strip()
    try:
        raw = unquote(raw)
    except Exception:
        pass
    low = raw.lower()
    if low.startswith(("javascript:", "data:", "vbscript:")):
        return ""
    path = raw
    if "://" in raw or raw.startswith("/"):
        split = urlsplit(raw)
        queried = parse_qs(split.query).get("path")
        path = queried[0] if queried else split.path
    path = path.split("?", 1)[0].split("#", 1)[0]
    return Path(path).suffix.lower()


def _map_outside_code(text: str, fn: Callable[[str], str]) -> str:
    pieces = (text or "").split("```")
    out: list[str] = []
    for index, piece in enumerate(pieces):
        if index % 2 == 1:
            out.append(piece)
            continue
        bits = piece.split("`")
        mapped = [fn(bit) if bit_index % 2 == 0 else bit for bit_index, bit in enumerate(bits)]
        out.append("`".join(mapped))
    return "```".join(out)


def expand_video_markdown(text: str) -> str:
    """Turn ``![[clip.mp4]]`` and ``<video src>`` into markdown images.

    Later HTML rendering promotes those images to ``<video controls>``.
    Non-video wiki embeds are left unchanged.
    """

    def convert(chunk: str) -> str:
        def wiki_repl(match: re.Match[str]) -> str:
            target = (match.group(1) or "").strip()
            if _video_suffix(target) not in _VIDEO_SUFFIXES:
                return match.group(0)
            alias = (match.group(2) or Path(target).name).strip()
            alias = alias.replace("[", "").replace("]", "")
            dest = f"<{target}>" if re.search(r"[\s()]", target) else target
            return f"![{alias}]({dest})"

        def video_repl(match: re.Match[str]) -> str:
            attrs = match.group(1) if match.group(1) is not None else (match.group(3) or "")
            inner = match.group(2) or ""
            src = _html_attr(attrs, "src")
            if not src:
                source = re.search(r"<source\b([^>]*)/?>", inner, re.IGNORECASE)
                if source:
                    src = _html_attr(source.group(1), "src")
            if not src or _video_suffix(src) not in _VIDEO_SUFFIXES:
                return match.group(0)
            src = src.strip()
            dest = f"<{src}>" if re.search(r"[\s()]", src) else src
            return f"![video]({dest})"

        converted = _WIKI_EMBED_RE.sub(wiki_repl, chunk)
        return _HTML_VIDEO_RE.sub(video_repl, converted)

    return _map_outside_code(text or "", convert)


def promote_video_embeds(html_body: str) -> str:
    """Replace ``<img>`` tags that point at mp4/webm with a native player."""

    def repl(match: re.Match[str]) -> str:
        tag = match.group(0)
        src = _html_attr(tag, "src")
        if not src or _video_suffix(src) not in _VIDEO_SUFFIXES:
            return tag
        safe = html.escape(src.strip(), quote=True)
        return (
            f'<video controls playsinline preload="metadata" src="{safe}"></video>'
        )

    return re.sub(r"<img\b[^>]*>", repl, html_body or "", flags=re.IGNORECASE)


def markdown_to_safe_html(text: str) -> str:
    prepared = linkify_toc_in_markdown(expand_video_markdown(text or ""))
    try:
        import markdown as md_lib  # type: ignore

        body = md_lib.markdown(
            prepared,
            extensions=["fenced_code", "tables", "nl2br", "sane_lists"],
            output_format="html5",
        )
    except Exception:
        body = _simple_markdown_to_html(prepared)
    return promote_video_embeds(add_heading_ids(body))


_MARKDOWN_BODY_CSS = """
    .markdown-body {
      background: transparent;
      color: #e6edf3;
      line-height: 1.6;
      font-size: 15px;
    }
    .markdown-body h1, .markdown-body h2, .markdown-body h3,
    .markdown-body h4, .markdown-body h5, .markdown-body h6 {
      margin: 1.2em 0 0.5em;
      font-weight: 650;
      border-bottom: 1px solid #30363d;
      padding-bottom: 0.3em;
      scroll-margin-top: 64px;
    }
    .markdown-body p { margin: 0.75em 0; }
    .markdown-body ul, .markdown-body ol { padding-left: 1.5em; }
    .markdown-body img,
    .markdown-body video {
      max-width: 100%;
      height: auto;
      border-radius: 6px;
    }
    .markdown-body video {
      display: block;
      margin: 0.6em 0;
      background: #000;
    }
    .markdown-body code {
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.9em;
      background: rgba(110, 118, 129, 0.2);
      padding: 0.15em 0.4em;
      border-radius: 4px;
    }
    .markdown-body pre {
      overflow-x: auto;
      padding: 12px 14px;
      border-radius: 8px;
      background: rgba(110, 118, 129, 0.15);
      border: 1px solid #30363d;
    }
    .markdown-body pre code {
      background: transparent;
      padding: 0;
    }
    .markdown-body table {
      border-collapse: collapse;
      width: 100%;
      margin: 1em 0;
      font-size: 14px;
    }
    .markdown-body th, .markdown-body td {
      border: 1px solid #30363d;
      padding: 6px 10px;
      text-align: left;
    }
    .markdown-body a { color: #58a6ff; }
    .markdown-body a[href^="#"] {
      text-decoration: none;
    }
    .markdown-body a[href^="#"]:hover {
      text-decoration: underline;
    }
    .markdown-body blockquote {
      margin: 0.75em 0;
      padding: 0 1em;
      border-left: 3px solid #30363d;
      color: #8b949e;
    }
    @media (prefers-color-scheme: light) {
      .markdown-body { color: #1f2328; }
      .markdown-body h1, .markdown-body h2, .markdown-body h3,
      .markdown-body th, .markdown-body td,
      .markdown-body pre, .markdown-body blockquote {
        border-color: #d0d7de;
      }
      .markdown-body blockquote { color: #656d76; }
    }
"""


def build_markdown_viewer_page(
    file_name: str,
    text: str,
    *,
    topbar_right_html: str = "",
) -> str:
    title = html.escape(file_name)
    body_inner = markdown_to_safe_html(text)
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{
      margin: 0;
      background: #0d1117;
      color: #e6edf3;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    }}
    .topbar {{
      position: sticky; top: 0; z-index: 2;
      display: flex; align-items: center; justify-content: space-between; gap: 12px;
      padding: 10px 20px;
      border-bottom: 1px solid #30363d;
      background: rgba(13, 17, 23, 0.92);
      backdrop-filter: blur(8px);
    }}
    .topbar h1 {{
      margin: 0; font-size: 14px; font-weight: 600;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }}
    .topbar-actions {{
      display: flex; align-items: center; gap: 14px; flex-shrink: 0;
    }}
    .topbar a.action {{
      color: #58a6ff; text-decoration: none; font-size: 13px; white-space: nowrap;
    }}
    .topbar a.action:hover {{ text-decoration: underline; }}
    .wrap {{
      box-sizing: border-box;
      max-width: 980px;
      margin: 0 auto;
      padding: 24px 20px 64px;
    }}
    {_MARKDOWN_BODY_CSS}
    @media (prefers-color-scheme: light) {{
      body {{ background: #ffffff; color: #1f2328; }}
      .topbar {{ background: rgba(255,255,255,0.92); border-bottom-color: #d0d7de; }}
    }}
  </style>
</head>
<body>
  <div class="topbar">
    <h1>{title}</h1>
    <div class="topbar-actions">{topbar_right_html}</div>
  </div>
  <div class="wrap">
    <article class="markdown-body">{body_inner}</article>
  </div>
</body>
</html>
"""


def build_folder_share_page(
    folder_title: str,
    notes: list[dict[str, str]],
    *,
    topbar_right_html: str = "",
) -> str:
    """Public index listing direct markdown notes under a shared folder.

    ``notes`` items: ``{"name": display stem or filename, "url": "/s/.../n/..."}``.
    """
    title = html.escape(folder_title)
    if notes:
        items_html = []
        for note in notes:
            name = html.escape(note.get("name") or "")
            href = html.escape(note.get("url") or "", quote=True)
            items_html.append(
                f'<li class="share-note-item">'
                f'<a class="share-note-link" href="{href}">{name}</a>'
                f"</li>"
            )
        list_html = '<ul class="share-note-list">' + "\n".join(items_html) + "</ul>"
    else:
        list_html = (
            '<p class="share-empty">이 폴더에 공유할 마크다운 노트가 없습니다.</p>'
        )
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{
      margin: 0;
      background: #0d1117;
      color: #e6edf3;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    }}
    .topbar {{
      position: sticky; top: 0; z-index: 2;
      display: flex; align-items: center; justify-content: space-between; gap: 12px;
      padding: 10px 20px;
      border-bottom: 1px solid #30363d;
      background: rgba(13, 17, 23, 0.92);
      backdrop-filter: blur(8px);
    }}
    .topbar h1 {{
      margin: 0; font-size: 14px; font-weight: 600;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }}
    .topbar-actions {{
      display: flex; align-items: center; gap: 14px; flex-shrink: 0;
    }}
    .wrap {{
      box-sizing: border-box;
      max-width: 720px;
      margin: 0 auto;
      padding: 28px 20px 64px;
    }}
    .share-intro {{
      margin: 0 0 20px;
      color: #8b949e;
      font-size: 13px;
    }}
    .share-note-list {{
      list-style: none;
      margin: 0;
      padding: 0;
      border: 1px solid #30363d;
      border-radius: 10px;
      overflow: hidden;
    }}
    .share-note-item {{
      border-bottom: 1px solid #30363d;
    }}
    .share-note-item:last-child {{ border-bottom: none; }}
    .share-note-link {{
      display: block;
      padding: 14px 16px;
      color: #58a6ff;
      text-decoration: none;
      font-size: 15px;
      font-weight: 500;
    }}
    .share-note-link:hover {{
      background: rgba(56, 139, 253, 0.08);
      text-decoration: underline;
    }}
    .share-empty {{
      margin: 0;
      padding: 24px 16px;
      color: #8b949e;
      font-size: 14px;
      text-align: center;
      border: 1px dashed #30363d;
      border-radius: 10px;
    }}
    @media (prefers-color-scheme: light) {{
      body {{ background: #ffffff; color: #1f2328; }}
      .topbar {{ background: rgba(255,255,255,0.92); border-bottom-color: #d0d7de; }}
      .share-intro {{ color: #656d76; }}
      .share-note-list {{ border-color: #d0d7de; }}
      .share-note-item {{ border-bottom-color: #d0d7de; }}
      .share-note-link:hover {{ background: rgba(9, 105, 218, 0.06); }}
      .share-empty {{ color: #656d76; border-color: #d0d7de; }}
    }}
  </style>
</head>
<body>
  <div class="topbar">
    <h1>{title}</h1>
    <div class="topbar-actions">{topbar_right_html}</div>
  </div>
  <div class="wrap">
    <p class="share-intro">Shared folder — direct notes only</p>
    {list_html}
  </div>
</body>
</html>
"""


def build_text_viewer_page(
    file_name: str,
    text: str,
    *,
    as_markdown: bool,
    download_href: str = "",
) -> str:
    """CSP-safe text/markdown viewer for Load-files ``/api/files/view``."""
    download_link = ""
    if download_href:
        download_link = (
            f'<a class="action" href="{html.escape(download_href, quote=True)}">'
            "Download</a>"
        )
    if as_markdown:
        return build_markdown_viewer_page(
            file_name, text, topbar_right_html=download_link
        )

    title = html.escape(file_name)
    body_inner = f'<pre class="code">{html.escape(text)}</pre>'
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{
      margin: 0;
      background: #0d1117;
      color: #e6edf3;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    }}
    .topbar {{
      position: sticky; top: 0; z-index: 2;
      display: flex; align-items: center; justify-content: space-between; gap: 12px;
      padding: 10px 20px;
      border-bottom: 1px solid #30363d;
      background: rgba(13, 17, 23, 0.92);
      backdrop-filter: blur(8px);
    }}
    .topbar h1 {{
      margin: 0; font-size: 14px; font-weight: 600;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }}
    .topbar a.action {{
      color: #58a6ff; text-decoration: none; font-size: 13px; white-space: nowrap;
    }}
    .wrap {{
      box-sizing: border-box;
      max-width: 980px;
      margin: 0 auto;
      padding: 24px 20px 64px;
    }}
    pre.code {{
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 13px;
      line-height: 1.5;
    }}
    @media (prefers-color-scheme: light) {{
      body {{ background: #ffffff; color: #1f2328; }}
      .topbar {{ background: rgba(255,255,255,0.92); border-bottom-color: #d0d7de; }}
    }}
  </style>
</head>
<body>
  <div class="topbar">
    <h1>{title}</h1>
    {download_link}
  </div>
  <div class="wrap">
    <article>{body_inner}</article>
  </div>
</body>
</html>
"""
