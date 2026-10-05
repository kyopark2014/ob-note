import { InputRule, Node, mergeAttributes } from "@tiptap/core";
import CodeBlock from "@tiptap/extension-code-block";
import Image from "@tiptap/extension-image";
import { Table, TableCell, TableHeader, TableRow } from "@tiptap/extension-table";
import { TaskItem } from "@tiptap/extension-task-item";
import { TaskList } from "@tiptap/extension-task-list";
import { Markdown } from "@tiptap/markdown";
import {
  NodeViewContent,
  NodeViewWrapper,
  ReactNodeViewRenderer,
  type NodeViewProps,
} from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import { useState, type MouseEvent, type MutableRefObject } from "react";
import { api } from "../../api";
import { MermaidBlock } from "../MermaidBlock";
import {
  isVideoAssetPath,
  resolveNoteAssetPath,
  slugifyHeading,
  splitGithubImageSrc,
} from "../MarkdownPreview";

export type VaultBridge = {
  notePath: string | null;
  onWikiClick: (target: string) => void;
  scrollToHeading: (id: string) => void;
};

const WIKI_SRC = /^(!)?\[\[([^\]|\n]*?)(?:\|([^\]\n]+))?\]\]/;

function wikiLabel(target: string, alias: string | null | undefined): string {
  if (alias) return alias;
  if (target.startsWith("#")) return target.slice(1).trim() || target;
  return target;
}

function cssLength(value: string | null | undefined): string | undefined {
  const raw = (value || "").trim();
  if (!raw) return undefined;
  if (/^\d+(?:\.\d+)?$/.test(raw)) return `${raw}px`;
  if (/^\d+(?:\.\d+)?px$/i.test(raw)) return `${raw.slice(0, -2)}px`;
  if (/^\d+(?:\.\d+)?%$/.test(raw)) return raw;
  return undefined;
}

function attrText(value: unknown): string {
  return value == null ? "" : String(value);
}

function escapeAttr(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/"/g, "&quot;");
}

function displayAssetSrc(notePath: string | null, src: string): string {
  const sized = splitGithubImageSrc(src);
  const realSrc = sized.src;
  if (
    realSrc.startsWith("http://") ||
    realSrc.startsWith("https://") ||
    realSrc.startsWith("data:") ||
    realSrc.startsWith("/")
  ) {
    return realSrc;
  }
  const resolved = notePath ? resolveNoteAssetPath(notePath, realSrc) : realSrc;
  return api.rawUrl(resolved);
}

const boundBridge: { current: MutableRefObject<VaultBridge> | null } = { current: null };

function currentBridge(): VaultBridge {
  const bridge = boundBridge.current?.current;
  if (bridge) return bridge;
  return {
    notePath: null,
    onWikiClick: () => {},
    scrollToHeading: () => {},
  };
}

function WikiLinkView({ node }: NodeViewProps) {
  const bridge = currentBridge();
  const target = attrText(node.attrs.target);
  const alias = node.attrs.alias ? attrText(node.attrs.alias) : null;
  const embed = Boolean(node.attrs.embed);
  const label = wikiLabel(target, alias);
  const filePart = target.split("#")[0].trim();

  if (embed && filePart && isVideoAssetPath(filePart)) {
    return (
      <NodeViewWrapper as="span" className="wiki-embed">
        <video
          className="note-video"
          controls
          playsInline
          preload="metadata"
          src={displayAssetSrc(bridge.notePath, filePart)}
          title={label}
        />
      </NodeViewWrapper>
    );
  }

  if (embed) {
    return (
      <NodeViewWrapper as="span" className="wiki-embed">
        <em>{`embed: ${label}`}</em>
      </NodeViewWrapper>
    );
  }

  return (
    <NodeViewWrapper
      as="span"
      className="wiki-link"
      onMouseDown={(event: MouseEvent) => event.preventDefault()}
      onClick={(event: MouseEvent) => {
        event.preventDefault();
        event.stopPropagation();
        if (target.startsWith("#")) {
          const heading = target.slice(1).trim();
          bridge.scrollToHeading(slugifyHeading(heading) || heading);
          return;
        }
        if (target) bridge.onWikiClick(target);
      }}
    >
      {label}
    </NodeViewWrapper>
  );
}

const WikiLink = Node.create({
  name: "wikiLink",
  inline: true,
  group: "inline",
  atom: true,
  selectable: true,
  addAttributes() {
    return {
      target: { default: "" },
      alias: { default: null },
      embed: { default: false },
    };
  },
  parseHTML() {
    return [
      {
        tag: "span[data-wiki-target]",
        getAttrs(dom) {
          const el = dom as HTMLElement;
          return {
            target: el.getAttribute("data-wiki-target") || "",
            alias: el.getAttribute("data-wiki-alias"),
            embed: el.getAttribute("data-wiki-embed") === "1",
          };
        },
      },
    ];
  },
  renderHTML({ node, HTMLAttributes }) {
    const target = attrText(node.attrs.target);
    const alias = node.attrs.alias ? attrText(node.attrs.alias) : null;
    return [
      "span",
      mergeAttributes(HTMLAttributes, {
        class: "wiki-link",
        "data-wiki-target": target,
        "data-wiki-alias": alias,
        "data-wiki-embed": node.attrs.embed ? "1" : "0",
      }),
      wikiLabel(target, alias),
    ];
  },
  markdownTokenizer: {
    name: "wikiLink",
    level: "inline",
    start: (src) => {
      const embedAt = src.indexOf("![[");
      const linkAt = src.indexOf("[[");
      if (embedAt < 0) return linkAt;
      if (linkAt < 0) return embedAt;
      return Math.min(embedAt, linkAt);
    },
    tokenize: (src) => {
      const match = WIKI_SRC.exec(src);
      if (!match) return undefined;
      const target = (match[2] || "").trim();
      if (!target) return undefined;
      return {
        type: "wikiLink",
        raw: match[0],
        embed: Boolean(match[1]),
        target,
        alias: match[3]?.trim() || null,
      };
    },
  },
  parseMarkdown: (token, helpers) => {
    const target = attrText(token.target).trim();
    if (!target) return helpers.createTextNode(token.raw || "");
    return helpers.createNode("wikiLink", {
      target,
      alias: token.alias ? attrText(token.alias) : null,
      embed: Boolean(token.embed),
    });
  },
  renderMarkdown: (node) => {
    const target = attrText(node.attrs?.target);
    const alias = node.attrs?.alias ? attrText(node.attrs.alias) : "";
    const bang = node.attrs?.embed ? "!" : "";
    if (!target) return "";
    return alias ? `${bang}[[${target}|${alias}]]` : `${bang}[[${target}]]`;
  },
  addNodeView() {
    return ReactNodeViewRenderer(WikiLinkView, { as: "span" });
  },
  addInputRules() {
    return [
      new InputRule({
        find: /(!)?\[\[([^\]|\n]+?)(?:\|([^\]\n]+))?\]\]$/,
        handler: ({ state, range, match }) => {
          const target = (match[2] || "").trim();
          const type = state.schema.nodes.wikiLink;
          if (!target || !type) return null;
          state.tr.replaceWith(
            range.from,
            range.to,
            type.create({
              target,
              alias: match[3]?.trim() || null,
              embed: Boolean(match[1]),
            }),
          );
        },
      }),
    ];
  },
});

function VaultImageView({ node }: NodeViewProps) {
  const bridge = currentBridge();
  const rawSrc = attrText(node.attrs.src);
  const sized = splitGithubImageSrc(rawSrc);
  const src = displayAssetSrc(bridge.notePath, sized.src || rawSrc);
  const width = cssLength(attrText(node.attrs.width) || sized.width);
  const height = cssLength(attrText(node.attrs.height) || sized.height);
  const style = width || height ? { width, height } : undefined;
  const alt = attrText(node.attrs.alt);
  const video = isVideoAssetPath(sized.src || rawSrc);
  if (video) {
    return (
      <NodeViewWrapper as="span" className="vault-image">
        <video className="note-video" controls playsInline preload="metadata" src={src} title={alt} />
      </NodeViewWrapper>
    );
  }
  return (
    <NodeViewWrapper as="span" className="vault-image">
      <img src={src} alt={alt} style={style} />
    </NodeViewWrapper>
  );
}

const VaultImage = Image.extend({
  addAttributes() {
    return {
      ...this.parent?.(),
      html: {
        default: null,
        rendered: false,
      },
    };
  },
  parseHTML() {
    return [
      {
        tag: "img[src]",
        getAttrs(dom) {
          const el = dom as HTMLElement;
          const width = el.getAttribute("width");
          const height = el.getAttribute("height");
          return {
            src: el.getAttribute("src"),
            alt: el.getAttribute("alt"),
            title: el.getAttribute("title"),
            width,
            height,
            html: width || height ? "img" : null,
          };
        },
      },
      {
        tag: "video",
        getAttrs(dom) {
          const el = dom as HTMLElement;
          const src =
            el.getAttribute("src") || el.querySelector("source")?.getAttribute("src");
          if (!src) return false;
          return {
            src,
            alt: "video",
            title: null,
            width: null,
            height: null,
            html: "video",
          };
        },
      },
    ];
  },
  parseMarkdown: (token, helpers) => {
    const sized = splitGithubImageSrc(attrText(token.href));
    return helpers.createNode("image", {
      src: sized.src,
      alt: token.text || "",
      title: token.title || null,
      width: sized.width || null,
      height: sized.height || null,
      html: sized.width || sized.height ? "img" : null,
    });
  },
  renderMarkdown: (node) => {
    const src = attrText(node.attrs?.src);
    const alt = attrText(node.attrs?.alt);
    const title = attrText(node.attrs?.title);
    const width = attrText(node.attrs?.width);
    const height = attrText(node.attrs?.height);
    const html = attrText(node.attrs?.html);
    if (html === "video") return `<video src="${escapeAttr(src)}"></video>`;
    if (width || height) {
      const w = width ? ` width="${escapeAttr(width)}"` : "";
      const h = height ? ` height="${escapeAttr(height)}"` : "";
      return `<img${w}${h} alt="${escapeAttr(alt)}" src="${escapeAttr(src)}" />`;
    }
    return title ? `![${alt}](${src} "${title}")` : `![${alt}](${src})`;
  },
  addNodeView() {
    return ReactNodeViewRenderer(VaultImageView, { as: "span" });
  },
}).configure({ inline: true, allowBase64: true });

function CodeBlockView({ node }: NodeViewProps) {
  const language = attrText(node.attrs.language);
  const mermaid = language === "mermaid";
  const [sourceOpen, setSourceOpen] = useState(false);
  return (
    <NodeViewWrapper className="vault-code-block">
      {mermaid ? <MermaidBlock chart={node.textContent} /> : null}
      <pre className={mermaid && !sourceOpen ? "mermaid-source-collapsed" : undefined}>
        <NodeViewContent<"code"> as="code" />
      </pre>
      {mermaid ? (
        <button
          type="button"
          className="mermaid-source-toggle"
          onMouseDown={(event: MouseEvent) => event.preventDefault()}
          onClick={() => setSourceOpen((open) => !open)}
        >
          {sourceOpen ? "다이어그램" : "원문"}
        </button>
      ) : null}
    </NodeViewWrapper>
  );
}

const VaultCodeBlock = CodeBlock.extend({
  addNodeView() {
    return ReactNodeViewRenderer(CodeBlockView);
  },
});

const RAW_SKIP_TAGS = new Set([
  "p",
  "em",
  "strong",
  "a",
  "code",
  "br",
  "img",
  "video",
  "source",
  "ul",
  "ol",
  "li",
  "blockquote",
  "table",
  "thead",
  "tbody",
  "tr",
  "td",
  "th",
  "pre",
  "span",
  "del",
  "s",
  "hr",
  "input",
  "label",
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
]);

function RawHtmlBlockView({ node }: NodeViewProps) {
  const html = attrText(node.attrs.html);
  if (html.startsWith("<!--")) {
    return <NodeViewWrapper className="raw-html-comment" aria-hidden="true" />;
  }
  return (
    <NodeViewWrapper
      className="raw-html-block"
      dangerouslySetInnerHTML={{ __html: html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "") }}
    />
  );
}

const RawHtmlBlock = Node.create({
  name: "rawHtmlBlock",
  group: "block",
  atom: true,
  selectable: true,
  addAttributes() {
    return { html: { default: "" } };
  },
  renderHTML({ node }) {
    return ["div", { class: "raw-html-block", "data-raw-html": attrText(node.attrs.html) }];
  },
  markdownTokenizer: {
    name: "rawHtmlBlock",
    level: "block",
    start: (src) => (/^<!--/.test(src) || /^[ \t]*<[a-zA-Z]/.test(src) ? 0 : -1),
    tokenize: (src) => {
      const comment = /^<!--[\s\S]*?-->/.exec(src);
      if (comment) {
        const after = src.slice(comment[0].length);
        if (after === "" || after.startsWith("\n")) {
          const nl = after.startsWith("\n") ? 1 : 0;
          return {
            type: "rawHtmlBlock",
            raw: src.slice(0, comment[0].length + nl),
            html: comment[0],
          };
        }
      }
      const line = /^[ \t]*(<([a-zA-Z][\w-]*)\b[^>\n]*>(?:<\/\2\s*>)?)[ \t]*(?:\n|$)/.exec(src);
      if (!line) return undefined;
      const tag = line[2].toLowerCase();
      if (RAW_SKIP_TAGS.has(tag)) return undefined;
      return { type: "rawHtmlBlock", raw: line[0], html: line[1] };
    },
  },
  parseMarkdown: (token, helpers) => {
    const html = attrText(token.html || token.raw);
    if (!html) return helpers.createNode("paragraph");
    return helpers.createNode("rawHtmlBlock", { html });
  },
  renderMarkdown: (node) => attrText(node.attrs?.html),
  addNodeView() {
    return ReactNodeViewRenderer(RawHtmlBlockView);
  },
});

const RawHtmlInline = Node.create({
  name: "rawHtmlInline",
  inline: true,
  group: "inline",
  atom: true,
  selectable: true,
  addAttributes() {
    return { html: { default: "" } };
  },
  renderHTML({ node }) {
    return ["span", { class: "raw-html-comment", "data-raw-html": attrText(node.attrs.html) }];
  },
  markdownTokenizer: {
    name: "rawHtmlInline",
    level: "inline",
    start: (src) => src.indexOf("<!--"),
    tokenize: (src) => {
      const comment = /^<!--[\s\S]*?-->/.exec(src);
      if (!comment) return undefined;
      return { type: "rawHtmlInline", raw: comment[0], html: comment[0] };
    },
  },
  parseMarkdown: (token, helpers) =>
    helpers.createNode("rawHtmlInline", { html: attrText(token.html || token.raw) }),
  renderMarkdown: (node) => attrText(node.attrs?.html),
  addNodeView() {
    return ReactNodeViewRenderer(
      () => <NodeViewWrapper as="span" className="raw-html-comment" aria-hidden="true" />,
      { as: "span" },
    );
  },
});

export function createVaultExtensions(bridge: MutableRefObject<VaultBridge>) {
  boundBridge.current = bridge;
  return [
    StarterKit.configure({
      codeBlock: false,
      trailingNode: false,
      link: {
        openOnClick: false,
        autolink: true,
        HTMLAttributes: { rel: "noreferrer" },
      },
    }),
    VaultCodeBlock,
    VaultImage,
    WikiLink,
    Table.configure({ resizable: false }),
    TableRow,
    TableHeader,
    TableCell,
    TaskList,
    TaskItem.configure({ nested: true }),
    RawHtmlBlock,
    RawHtmlInline,
    Markdown,
  ];
}

export function headingDomId(text: string, counts: Map<string, number>): string {
  const base = slugifyHeading(text) || "section";
  const n = counts.get(base) ?? 0;
  counts.set(base, n + 1);
  return n === 0 ? base : `${base}-${n}`;
}
