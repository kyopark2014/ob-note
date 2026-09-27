import {
  Children,
  isValidElement,
  useRef,
  type CSSProperties,
  type ReactElement,
  type ReactNode,
} from "react";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Components } from "react-markdown";
import { api } from "../api";
import { MermaidBlock } from "./MermaidBlock";

/**
 * Obsidian-style wiki links:
 *   [[Note]] · [[Note|alias]] · [[Note#Heading]] · [[#Heading]] · [[#Heading|alias]]
 * Target may be empty only when a heading fragment is present (`[[#…]]`).
 */
const WIKI_RE = /(!)?\[\[([^\]|]*?)(?:\|([^\]]+))?\]\]/g;

/** Hash prefix — relative URLs survive react-markdown's defaultUrlTransform
 *  (custom schemes like wiki:// are stripped to ""). */
export const WIKI_HASH_PREFIX = "#__wiki__/";

function expandWikiLinks(text: string): string {
  return text.replace(WIKI_RE, (_m, embed, rawTarget, alias) => {
    const target = (rawTarget || "").trim();
    if (!target) return _m as string;

    // Same-document heading: [[#Heading]] or [[#Heading|alias]]
    if (target.startsWith("#")) {
      const heading = target.slice(1).trim();
      if (!heading) return _m as string;
      const label = ((alias as string | undefined) || heading).trim();
      if (embed) return `*(embed: ${label})*`;
      const slug = slugifyHeading(heading);
      // Angle brackets keep spaces/parens safe for CommonMark.
      return `[${label}](<#${encodeURIComponent(slug)}>)`;
    }

    const hashIdx = target.indexOf("#");
    const note =
      hashIdx >= 0 ? target.slice(0, hashIdx).trim() : target;
    if (!note) return _m as string;

    const label = ((alias as string | undefined) || note).trim();
    if (embed) return `*(embed: ${label})*`;

    // Cross-note: open via wiki resolver. Heading fragment is ignored for now
    // (same-doc TOC uses [[#Heading]] → #slug path above).
    const dest = `${WIKI_HASH_PREFIX}${encodeURIComponent(note)}`;
    return `[${label}](<${dest}>)`;
  });
}

/** GitHub / Obsidian-style heading slug for in-doc TOC anchors. */
export function slugifyHeading(text: string): string {
  return text
    .trim()
    .toLowerCase()
    .replace(/\s+/g, "-")
    .replace(/[^\p{L}\p{N}\p{M}-]/gu, "");
}

function nodeText(node: ReactNode): string {
  return Children.toArray(node)
    .map((child) => {
      if (typeof child === "string" || typeof child === "number") return String(child);
      if (isValidElement(child)) {
        return nodeText((child.props as { children?: ReactNode }).children);
      }
      return "";
    })
    .join("");
}

function decodeHashTarget(href: string): string | null {
  if (!href.startsWith("#") || href.startsWith(WIKI_HASH_PREFIX)) return null;
  // Same-document hash only (not "#__wiki__/...")
  if (href.length <= 1) return null;
  const raw = href.slice(1);
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

function makeUniqueSlugger() {
  const counts = new Map<string, number>();
  return (text: string): string => {
    const base = slugifyHeading(text) || "section";
    const n = counts.get(base) ?? 0;
    counts.set(base, n + 1);
    return n === 0 ? base : `${base}-${n}`;
  };
}

/** Resolve a markdown image/link path relative to the note file. */
export function resolveNoteAssetPath(notePath: string, src: string): string {
  let cleaned = src.replace(/\\/g, "/").trim();
  try {
    cleaned = decodeURIComponent(cleaned);
  } catch {
    /* keep raw */
  }
  if (
    !cleaned ||
    cleaned.startsWith("/") ||
    cleaned.startsWith("http:") ||
    cleaned.startsWith("https:") ||
    cleaned.startsWith("data:")
  ) {
    return cleaned;
  }
  const dir = notePath.includes("/") ? notePath.slice(0, notePath.lastIndexOf("/")) : "";
  const parts = [...(dir ? dir.split("/") : []), ...cleaned.split("/")];
  const out: string[] = [];
  for (const part of parts) {
    if (!part || part === ".") continue;
    if (part === "..") {
      out.pop();
      continue;
    }
    out.push(part);
  }
  return out.join("/");
}

/** Size carried on a GitHub `<img>` after it is turned into a markdown image. */
const IMAGE_SIZE_HINT = "#__obimg=";

/** `700` → `700px`. Percent and `px` are kept. Other values are ignored. */
function cssLength(value: string | null | undefined): string | null {
  const raw = (value || "").trim();
  if (!raw) return null;
  if (/^\d+(?:\.\d+)?$/.test(raw)) return `${raw}px`;
  if (/^\d+(?:\.\d+)?px$/i.test(raw)) return `${raw.slice(0, -2)}px`;
  if (/^\d+(?:\.\d+)?%$/.test(raw)) return raw;
  return null;
}

function decodeBasicEntities(value: string): string {
  return value
    .replace(/&quot;/g, '"')
    .replace(/&#39;|&apos;/g, "'")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&");
}

function readHtmlAttr(attrs: string, name: string): string | null {
  const re = new RegExp(
    `\\b${name}\\s*=\\s*(?:"([^"]*)"|'([^']*)'|([^\\s"'=<>\\\`]+))`,
    "i",
  );
  const match = attrs.match(re);
  if (!match) return null;
  const value = match[1] ?? match[2] ?? match[3];
  return value == null ? null : decodeBasicEntities(value);
}

export function splitGithubImageSrc(src: string): {
  src: string;
  width?: string;
  height?: string;
} {
  const at = src.lastIndexOf(IMAGE_SIZE_HINT);
  if (at < 0) return { src };
  const params = new URLSearchParams(src.slice(at + IMAGE_SIZE_HINT.length));
  const width = cssLength(params.get("w"));
  const height = cssLength(params.get("h"));
  return {
    src: src.slice(0, at),
    width: width || undefined,
    height: height || undefined,
  };
}

/** Even segments are outside `delimiter` and are passed to `fn`. */
function mapOutsideDelimiter(
  text: string,
  delimiter: string,
  fn: (chunk: string) => string,
): string {
  return text
    .split(delimiter)
    .map((part, index) => (index % 2 === 0 ? fn(part) : part))
    .join(delimiter);
}

/** Odd segments are matches of `pattern` (must include a capturing group) and are kept. */
function mapOutsidePattern(
  text: string,
  pattern: RegExp,
  fn: (chunk: string) => string,
): string {
  const flags = pattern.flags.includes("g") ? pattern.flags : `${pattern.flags}g`;
  const splitter = new RegExp(`(${pattern.source})`, flags);
  return text
    .split(splitter)
    .map((part, index) => (index % 2 === 0 ? fn(part) : part))
    .join("");
}

/**
 * GitHub-style images:
 *   <img width="700" alt="image" src="img.png" />
 * Raw HTML is not rendered; convert these tags to markdown images and keep
 * width/height on a fragment the img component strips before loading.
 */
export function expandGithubImages(text: string): string {
  const convert = (chunk: string) =>
    chunk.replace(/<img\b([^>]*?)\/?>/gi, (full, attrs: string) => {
      const src = readHtmlAttr(attrs, "src")?.trim();
      if (!src) return full;
      const alt = (readHtmlAttr(attrs, "alt") || "").replace(/[\[\]]/g, "");
      const width = cssLength(readHtmlAttr(attrs, "width"));
      const height = cssLength(readHtmlAttr(attrs, "height"));
      let dest = src;
      if (width || height) {
        const params = new URLSearchParams();
        if (width) params.set("w", width);
        if (height) params.set("h", height);
        dest = `${src}${IMAGE_SIZE_HINT}${params.toString()}`;
      }
      const href = /[\s()]/.test(dest) ? `<${dest}>` : dest;
      return `![${alt}](${href})`;
    });

  return mapOutsideDelimiter(text, "```", (outsideFence) =>
    mapOutsideDelimiter(outsideFence, "`", (outsideInline) =>
      mapOutsidePattern(outsideInline, /<!--[\s\S]*?-->/, convert),
    ),
  );
}

/** CommonMark rejects destinations with raw spaces unless wrapped in <...>. */
function normalizeMdMediaDestinations(text: string): string {
  return text.replace(/!\[([^\]]*)\]\(([^)\n]+)\)/g, (full, alt: string, dest: string) => {
    const d = dest.trim();
    if (d.startsWith("<") && d.endsWith(">")) return full;
    if (/\s/.test(d) || /[()]/.test(d)) {
      return `![${alt}](<${d.replace(/^<|>$/g, "")}>)`;
    }
    return full;
  });
}

function parseWikiHref(href: string | undefined): string | null {
  if (!href) return null;
  // react-markdown may leave "#__wiki__/..." or resolve against page as full URL
  const hashIdx = href.indexOf(WIKI_HASH_PREFIX);
  if (hashIdx >= 0) {
    const encoded = href.slice(hashIdx + WIKI_HASH_PREFIX.length);
    try {
      return decodeURIComponent(encoded);
    } catch {
      return encoded;
    }
  }
  if (href.startsWith("wiki://")) {
    try {
      return decodeURIComponent(href.slice("wiki://".length));
    } catch {
      return href.slice("wiki://".length);
    }
  }
  return null;
}

function codeText(children: ReactNode): string {
  return Children.toArray(children)
    .map((child) => (typeof child === "string" || typeof child === "number" ? String(child) : ""))
    .join("")
    .replace(/\n$/, "");
}

function isMermaidCode(node: ReactNode): node is ReactElement<{
  className?: string;
  children?: ReactNode;
}> {
  if (!isValidElement(node)) return false;
  const className = String(
    (node.props as { className?: string }).className || "",
  );
  return /language-mermaid\b/.test(className);
}

type Props = {
  content: string;
  notePath?: string | null;
  onWikiClick: (target: string) => void;
};

export function MarkdownPreview({ content, notePath, onWikiClick }: Props) {
  const expanded = normalizeMdMediaDestinations(
    expandWikiLinks(expandGithubImages(content)),
  );
  const paneRef = useRef<HTMLDivElement>(null);
  const uniqueSlug = makeUniqueSlugger();

  const scrollToHeading = (id: string) => {
    const root = paneRef.current;
    if (!root) return;
    const el =
      root.querySelector<HTMLElement>(`#${CSS.escape(id)}`) ||
      // Fallback: match slugified heading text if TOC used a slightly different form
      Array.from(root.querySelectorAll<HTMLElement>("h1,h2,h3,h4,h5,h6")).find(
        (h) => h.id === id || slugifyHeading(h.textContent || "") === id,
      );
    el?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const heading =
    (Tag: "h1" | "h2" | "h3" | "h4" | "h5" | "h6"): NonNullable<Components["h1"]> =>
    ({ children }) => {
      const id = uniqueSlug(nodeText(children));
      return <Tag id={id}>{children}</Tag>;
    };

  const components: Components = {
    h1: heading("h1"),
    h2: heading("h2"),
    h3: heading("h3"),
    h4: heading("h4"),
    h5: heading("h5"),
    h6: heading("h6"),
    a({ href, children }) {
      const wikiTarget = parseWikiHref(href);
      if (wikiTarget !== null) {
        return (
          <span
            className="wiki-link"
            role="link"
            tabIndex={0}
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              onWikiClick(wikiTarget);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onWikiClick(wikiTarget);
              }
            }}
          >
            {children}
          </span>
        );
      }
      const hashId = href ? decodeHashTarget(href) : null;
      if (hashId !== null) {
        return (
          <a
            href={`#${hashId}`}
            className="toc-link"
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              scrollToHeading(hashId);
            }}
          >
            {children}
          </a>
        );
      }
      return (
        <a href={href} target="_blank" rel="noreferrer">
          {children}
        </a>
      );
    },
    img({ src, alt }) {
      if (!src) return null;
      const sized = splitGithubImageSrc(src);
      const realSrc = sized.src;
      const style: CSSProperties = {};
      if (sized.width) style.width = sized.width;
      if (sized.height) style.height = sized.height;
      const styleProp = sized.width || sized.height ? style : undefined;
      if (
        realSrc.startsWith("http://") ||
        realSrc.startsWith("https://") ||
        realSrc.startsWith("data:") ||
        realSrc.startsWith("/")
      ) {
        return <img src={realSrc} alt={alt || ""} style={styleProp} />;
      }
      const resolved = notePath ? resolveNoteAssetPath(notePath, realSrc) : realSrc;
      return <img src={api.rawUrl(resolved)} alt={alt || ""} style={styleProp} />;
    },
    pre({ children }) {
      const only = Children.count(children) === 1 ? Children.only(children) : null;
      if (only && isMermaidCode(only)) {
        return <MermaidBlock chart={codeText(only.props.children)} />;
      }
      return <pre>{children}</pre>;
    },
  };

  return (
    <div className="preview-pane" ref={paneRef}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        urlTransform={(url) => {
          if (url.includes(WIKI_HASH_PREFIX) || url.startsWith("wiki:")) return url;
          return defaultUrlTransform(url);
        }}
        components={components}
      >
        {expanded}
      </ReactMarkdown>
    </div>
  );
}
