import { EditorContent, useEditor, type Editor } from "@tiptap/react";
import { useEffect, useRef, type DragEvent as ReactDragEvent } from "react";
import {
  getActiveVaultDrag,
  hasExternalFileDrag,
  isMarkdownNotePath,
  parseVaultDrag,
} from "../FileTree";
import { isVideoFileName } from "../../viewSettings";
import { slugifyHeading } from "../MarkdownPreview";
import { wikiLinkMarkdown } from "../../wikiLink";
import {
  createVaultExtensions,
  headingDomId,
  type VaultBridge,
} from "./extensions";

type Props = {
  content: string;
  notePath?: string | null;
  onChange: (markdown: string) => void;
  onWikiClick: (target: string) => void;
  onUploadImage?: (file: File) => Promise<string>;
  onUploadVideos?: (files: File[]) => Promise<string>;
};

function isVideoUpload(file: File): boolean {
  return (
    isVideoFileName(file.name) ||
    file.type === "video/mp4" ||
    file.type === "video/webm" ||
    file.type === "video/x-m4v"
  );
}

function scrollToHeading(root: HTMLElement, id: string) {
  let decoded = id;
  try {
    decoded = decodeURIComponent(id);
  } catch {
    /* keep raw */
  }
  const el =
    root.querySelector<HTMLElement>(`#${CSS.escape(decoded)}`) ||
    Array.from(root.querySelectorAll<HTMLElement>("h1,h2,h3,h4,h5,h6")).find(
      (heading) =>
        heading.id === decoded ||
        slugifyHeading(heading.textContent || "") === decoded ||
        (heading.textContent || "").trim() === decoded,
    );
  el?.scrollIntoView({ behavior: "smooth", block: "start" });
}

export function MarkdownEditor({
  content,
  notePath,
  onChange,
  onWikiClick,
  onUploadImage,
  onUploadVideos,
}: Props) {
  const paneRef = useRef<HTMLDivElement>(null);
  const editorRef = useRef<Editor | null>(null);
  const emittedRef = useRef(content);
  const acceptExternalRef = useRef(false);
  const syncingRef = useRef(false);
  const composingRef = useRef(false);
  const onChangeRef = useRef(onChange);
  const uploadImageRef = useRef(onUploadImage);
  const uploadVideosRef = useRef(onUploadVideos);
  const bridgeRef = useRef<VaultBridge>({
    notePath: notePath ?? null,
    onWikiClick,
    scrollToHeading: (id) => {
      const root = paneRef.current;
      if (root) scrollToHeading(root, id);
    },
  });

  onChangeRef.current = onChange;
  uploadImageRef.current = onUploadImage;
  uploadVideosRef.current = onUploadVideos;
  bridgeRef.current.notePath = notePath ?? null;
  bridgeRef.current.onWikiClick = onWikiClick;
  bridgeRef.current.scrollToHeading = (id) => {
    const root = paneRef.current;
    if (root) scrollToHeading(root, id);
  };

  const extensionsRef = useRef<ReturnType<typeof createVaultExtensions> | null>(null);
  if (!extensionsRef.current) extensionsRef.current = createVaultExtensions(bridgeRef);

  const editor = useEditor(
    {
      immediatelyRender: true,
      extensions: extensionsRef.current,
      content,
      contentType: "markdown",
      editorProps: {
        attributes: {
          spellcheck: "false",
          class: "vault-markdown",
        },
        handleDOMEvents: {
          // macOS Hangul sends the first jamo as a plain Latin key (ㅅ → T)
          // before compositionstart. ProseMirror forceFlush on that keydown
          // commits the Latin character next to the jamo.
          keydown: (_view, event) => {
            if (event.isComposing || event.keyCode === 229) return true;
            if (event.metaKey || event.ctrlKey || event.altKey) return false;
            return event.key.length === 1;
          },
          compositionstart: () => {
            composingRef.current = true;
            return false;
          },
          compositionend: () => {
            composingRef.current = false;
            const publish = () => {
              const current = editorRef.current;
              if (!current || composingRef.current || current.view.composing) return;
              const markdown = current.getMarkdown();
              if (markdown === emittedRef.current) return;
              emittedRef.current = markdown;
              onChangeRef.current(markdown);
            };
            queueMicrotask(publish);
            window.setTimeout(publish, 30);
            return false;
          },
        },
        handlePaste: (_view, event) => {
          const items = Array.from(event.clipboardData?.items || []);
          const imageItem = items.find((item) => item.type.startsWith("image/"));
          const file = imageItem?.getAsFile();
          const upload = uploadImageRef.current;
          if (!file || !upload) return false;
          event.preventDefault();
          void upload(file).then((markdown) => {
            if (!markdown) return;
            editorRef.current
              ?.chain()
              .focus()
              .insertContent(markdown, { contentType: "markdown" })
              .run();
          });
          return true;
        },
        handleDrop: (view, event, _slice, moved) => {
          if (moved) return false;
          const videos = Array.from(event.dataTransfer?.files || []).filter(isVideoUpload);
          if (videos.length && uploadVideosRef.current) {
            const coords = view.posAtCoords({ left: event.clientX, top: event.clientY });
            const upload = uploadVideosRef.current;
            event.stopPropagation();
            void upload(videos).then((markdown) => {
              if (!markdown) return;
              const pos = coords?.pos ?? view.state.selection.from;
              editorRef.current
                ?.chain()
                .focus()
                .insertContentAt(pos, markdown, { contentType: "markdown" })
                .run();
            });
            return true;
          }
          const data =
            parseVaultDrag(event as unknown as ReactDragEvent<HTMLElement>) ||
            getActiveVaultDrag();
          if (!data || data.kind !== "file" || !isMarkdownNotePath(data.path)) return false;
          const link = wikiLinkMarkdown(data.path);
          if (!link) return false;
          const coords = view.posAtCoords({ left: event.clientX, top: event.clientY });
          const pos = coords?.pos ?? view.state.selection.from;
          event.stopPropagation();
          editorRef.current
            ?.chain()
            .focus()
            .insertContentAt(pos, link, { contentType: "markdown" })
            .run();
          return true;
        },
        handleClick: (_view, _pos, event) => {
          const anchor = (event.target as HTMLElement | null)?.closest?.("a");
          if (!anchor) return false;
          const href = anchor.getAttribute("href") || "";
          if (href.startsWith("#")) {
            event.preventDefault();
            const root = paneRef.current;
            if (root) scrollToHeading(root, href.slice(1));
            return true;
          }
          if (/^(https?:|mailto:)/i.test(href)) {
            event.preventDefault();
            window.open(href, "_blank", "noopener,noreferrer");
            return true;
          }
          return false;
        },
      },
      onCreate: ({ editor: created }) => {
        editorRef.current = created;
        queueMicrotask(() => {
          acceptExternalRef.current = true;
        });
      },
      onUpdate: ({ editor: current, transaction }) => {
        if (!acceptExternalRef.current || syncingRef.current) return;
        if (!transaction.docChanged) return;
        if (transaction.getMeta("addToHistory") === false) return;
        if (composingRef.current || current.view.composing) return;
        const markdown = current.getMarkdown();
        emittedRef.current = markdown;
        onChangeRef.current(markdown);
      },
    },
    [],
  );

  useEffect(() => {
    editorRef.current = editor;
  }, [editor]);

  useEffect(() => {
    if (!editor) return;
    let frame = 0;
    const stampHeadings = () => {
      if (composingRef.current || editor.view.composing) return;
      const root = editor.view.dom;
      const counts = new Map<string, number>();
      root.querySelectorAll<HTMLElement>("h1,h2,h3,h4,h5,h6").forEach((heading) => {
        const next = headingDomId(heading.textContent || "", counts);
        if (heading.id !== next) heading.id = next;
      });
    };
    const schedule = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(stampHeadings);
    };
    schedule();
    editor.on("update", schedule);
    return () => {
      cancelAnimationFrame(frame);
      editor.off("update", schedule);
    };
  }, [editor]);

  useEffect(() => {
    if (!editor || !acceptExternalRef.current) return;
    if (content === emittedRef.current) return;
    syncingRef.current = true;
    editor.commands.setContent(content, { contentType: "markdown", emitUpdate: false });
    emittedRef.current = content;
    syncingRef.current = false;
  }, [content, editor]);

  const insertMarkdown = (markdown: string) => {
    const current = editorRef.current;
    if (!current || !markdown) return;
    current.chain().focus().insertContent(markdown, { contentType: "markdown" }).run();
  };

  return (
    <div
      className="preview-pane preview-editor"
      ref={paneRef}
      onDragOver={(event) => {
        if (!hasExternalFileDrag(event) && !getActiveVaultDrag()) return;
        event.preventDefault();
      }}
      onDrop={(event) => {
        if (event.defaultPrevented) return;
        const videos = Array.from(event.dataTransfer.files || []).filter(isVideoUpload);
        if (videos.length && uploadVideosRef.current) {
          event.preventDefault();
          event.stopPropagation();
          void uploadVideosRef.current(videos).then(insertMarkdown);
          return;
        }
        const data = parseVaultDrag(event) || getActiveVaultDrag();
        if (!data || data.kind !== "file" || !isMarkdownNotePath(data.path)) return;
        const link = wikiLinkMarkdown(data.path);
        if (!link) return;
        event.preventDefault();
        event.stopPropagation();
        insertMarkdown(link);
      }}
    >
      <EditorContent editor={editor} />
    </div>
  );
}
