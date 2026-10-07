import {
  ClipboardEvent,
  DragEvent,
  FormEvent,
  KeyboardEvent,
  CompositionEvent,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";
import { api } from "../api";
import { isVaultMoveDrag, parseVaultDrag } from "./FileTree";

export type AgentNoteChip = {
  path: string;
  name: string;
  size: number;
};

export type AgentAttachedImage = {
  path: string;
  name: string;
  previewUrl: string;
};

export type AgentLoadedFile = {
  path: string;
  name: string;
  size: number;
};

export type AgentSendPayload = {
  text: string;
  imagePaths: string[];
  filePaths: string[];
};

type Props = {
  disabled?: boolean;
  waiting?: boolean;
  note: AgentNoteChip | null;
  /** Extra notes added via Open agent while the panel was already open. */
  extraNotes?: AgentNoteChip[];
  notePath?: string | null;
  onRemoveNote?: () => void;
  onRemoveExtraNote?: (path: string) => void;
  onSend: (payload: AgentSendPayload) => void;
  onStop?: () => void;
};

const MIN_INPUT_HEIGHT = 24;
const MAX_INPUT_HEIGHT = 160;
const MENU_VERTICAL_OFFSET = 8;
const IMAGE_ACCEPT =
  "image/png,image/jpeg,image/webp,image/gif,.png,.jpg,.jpeg,.webp,.gif";
const LOAD_ACCEPT =
  ".pdf,.txt,.md,.markdown,.csv,.doc,.docx,.ppt,.pptx,.xls,.xlsx,.html,.htm,.json,.py,.js,.ts,.tsx,.jsx,.yml,.yaml,.xml,.rst,.dxf,.png,.jpg,.jpeg,.webp,.gif";

function formatFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function isImageFile(file: File): boolean {
  if (file.type.startsWith("image/")) return true;
  return /\.(png|jpe?g|gif|webp)$/i.test(file.name);
}

function extensionFromMime(mime: string): string {
  if (mime === "image/jpeg") return ".jpg";
  if (mime === "image/webp") return ".webp";
  if (mime === "image/gif") return ".gif";
  return ".png";
}

function normalizeImageFile(
  file: File,
  fallbackName = "pasted_screenshot",
): File {
  if (!isImageFile(file)) return file;
  const mime = file.type || "image/png";
  const ext = extensionFromMime(mime);
  const hasUsefulName =
    file.name &&
    file.name !== "image.png" &&
    file.name !== "image.jpg" &&
    file.name !== "blob";
  if (hasUsefulName) return file;
  return new File([file], `${fallbackName}${ext}`, { type: mime });
}

function collectClipboardImages(clipboardData: DataTransfer | null): File[] {
  if (!clipboardData) return [];
  const files: File[] = [];
  const seen = new Set<string>();
  const pushUnique = (file: File) => {
    const key = `${file.size}:${file.type || "image/png"}`;
    if (seen.has(key)) return;
    seen.add(key);
    files.push(normalizeImageFile(file));
  };
  for (const item of Array.from(clipboardData.items ?? [])) {
    if (!item.type.startsWith("image/")) continue;
    const blob = item.getAsFile();
    if (blob) pushUnique(blob);
  }
  if (files.length === 0) {
    for (const file of Array.from(clipboardData.files ?? [])) {
      if (isImageFile(file)) pushUnique(file);
    }
  }
  return files;
}

function safeFileName(name: string): string {
  const base = (name || "file").trim() || "file";
  return base.replace(/[^\w.\-가-힣]+/g, "_");
}

function agentUploadPath(notePath: string | null | undefined, fileName: string): string {
  const parent =
    notePath && notePath.includes("/")
      ? notePath.slice(0, notePath.lastIndexOf("/"))
      : "";
  const stamp = Date.now().toString(36);
  const safe = safeFileName(fileName);
  // Same folder as the open note (vault root if the note is at root).
  return parent ? `${parent}/${stamp}-${safe}` : `${stamp}-${safe}`;
}

export function AgentChatInput({
  disabled,
  waiting = false,
  note,
  extraNotes = [],
  notePath,
  onRemoveNote,
  onRemoveExtraNote,
  onSend,
  onStop,
}: Props) {
  const [value, setValue] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);
  const [menuPosition, setMenuPosition] = useState<{
    left: number;
    top: number;
    width: number;
  } | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [attachments, setAttachments] = useState<AgentAttachedImage[]>([]);
  const [loadedFiles, setLoadedFiles] = useState<AgentLoadedFile[]>([]);
  const [dragOver, setDragOver] = useState(false);
  const attachmentsRef = useRef<AgentAttachedImage[]>([]);
  const uploadingRef = useRef(false);
  const dragDepthRef = useRef(0);
  const loadedFilesRef = useRef<AgentLoadedFile[]>([]);

  const addWrapRef = useRef<HTMLDivElement>(null);
  const menuPortalRef = useRef<HTMLDivElement>(null);
  const inputWrapRef = useRef<HTMLFormElement>(null);
  const imageInputRef = useRef<HTMLInputElement>(null);
  const loadInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const isComposingRef = useRef(false);
  const submitAfterCompositionRef = useRef(false);

  attachmentsRef.current = attachments;
  uploadingRef.current = uploading;
  loadedFilesRef.current = loadedFiles;

  useEffect(() => {
    return () => {
      for (const item of attachmentsRef.current) {
        if (item.previewUrl.startsWith("blob:")) {
          URL.revokeObjectURL(item.previewUrl);
        }
      }
    };
  }, []);

  useEffect(() => {
    if (!uploadError) return;
    const timer = window.setTimeout(() => setUploadError(null), 5000);
    return () => window.clearTimeout(timer);
  }, [uploadError]);

  function adjustInputHeight() {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    const next = Math.min(
      Math.max(el.scrollHeight, MIN_INPUT_HEIGHT),
      MAX_INPUT_HEIGHT,
    );
    el.style.height = `${next}px`;
  }

  useLayoutEffect(() => {
    adjustInputHeight();
  }, [value]);

  function updateMenuPosition() {
    const rect = inputWrapRef.current?.getBoundingClientRect();
    if (!rect) return;
    setMenuPosition({
      left: rect.left,
      top: rect.top - MENU_VERTICAL_OFFSET,
      width: rect.width,
    });
  }

  useEffect(() => {
    if (!menuOpen) {
      setMenuPosition(null);
      return;
    }
    updateMenuPosition();
    window.addEventListener("resize", updateMenuPosition);
    window.addEventListener("scroll", updateMenuPosition, true);

    function onPointerDown(e: MouseEvent) {
      const target = e.target as Node;
      if (addWrapRef.current?.contains(target)) return;
      if (menuPortalRef.current?.contains(target)) return;
      setMenuOpen(false);
    }
    function onKeyDown(e: globalThis.KeyboardEvent) {
      if (e.key === "Escape") setMenuOpen(false);
    }

    document.addEventListener("mousedown", onPointerDown);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("resize", updateMenuPosition);
      window.removeEventListener("scroll", updateMenuPosition, true);
      document.removeEventListener("mousedown", onPointerDown);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [menuOpen]);

  async function uploadImageFiles(files: File[]) {
    if (!files.length || disabled || uploadingRef.current) return;
    setUploading(true);
    setUploadError(null);
    try {
      for (const raw of files) {
        await attachImageFile(normalizeImageFile(raw, "uploaded_image"));
      }
    } catch (err) {
      console.error("Image upload failed", err);
      setUploadError(
        err instanceof Error ? err.message : "이미지 업로드에 실패했습니다.",
      );
    } finally {
      setUploading(false);
    }
  }

  async function attachImageFile(file: File) {
    const previewUrl = URL.createObjectURL(file);
    try {
      const vaultPath = agentUploadPath(notePath ?? note?.path, file.name.normalize("NFC"));
      const result = await api.uploadFile(vaultPath, file, file.name.normalize("NFC"));
      setAttachments((prev) => [
        ...prev,
        {
          path: result.path || vaultPath,
          name: file.name,
          previewUrl,
        },
      ]);
    } catch (err) {
      URL.revokeObjectURL(previewUrl);
      throw err;
    }
  }

  async function loadWorkspaceFiles(files: File[]) {
    if (!files.length || disabled || uploadingRef.current) return;
    setUploading(true);
    setUploadError(null);
    try {
      for (const file of files) {
        if (isImageFile(file)) {
          await attachImageFile(normalizeImageFile(file, "uploaded_image"));
          continue;
        }
        const vaultPath = agentUploadPath(notePath ?? note?.path, file.name.normalize("NFC"));
        const result = await api.uploadFile(vaultPath, file, file.name.normalize("NFC"));
        const path = result.path || vaultPath;
        setLoadedFiles((prev) => {
          const next = prev.filter((item) => item.path !== path);
          return [
            ...next,
            { path, name: file.name.normalize("NFC"), size: result.size ?? file.size },
          ];
        });
      }
    } catch (err) {
      console.error("Load file failed", err);
      setUploadError(
        err instanceof Error ? err.message : "파일 로드에 실패했습니다.",
      );
    } finally {
      setUploading(false);
    }
  }

  function isAlreadyAttached(path: string): boolean {
    if (note?.path === path) return true;
    if (extraNotes.some((n) => n.path === path)) return true;
    if (loadedFilesRef.current.some((f) => f.path === path)) return true;
    if (attachmentsRef.current.some((a) => a.path === path)) return true;
    return false;
  }

  /** Attach an existing vault note/file (no re-upload) — same chip as Load files. */
  async function loadVaultPaths(paths: string[]) {
    const unique = [...new Set(paths.map((p) => p.trim()).filter(Boolean))];
    if (!unique.length || disabled || uploadingRef.current) return;
    setUploading(true);
    setUploadError(null);
    try {
      for (const path of unique) {
        if (isAlreadyAttached(path)) continue;
        try {
          const meta = await api.agentNoteMeta(path);
          const chip = {
            path: meta.path || path,
            name: meta.name || path.split("/").pop() || path,
            size: meta.size ?? 0,
          };
          if (/\.(png|jpe?g|gif|webp)$/i.test(chip.name)) {
            setAttachments((prev) => {
              if (prev.some((a) => a.path === chip.path)) return prev;
              return [
                ...prev,
                {
                  path: chip.path,
                  name: chip.name,
                  previewUrl: api.rawUrl(chip.path),
                },
              ];
            });
          } else {
            setLoadedFiles((prev) => {
              const next = prev.filter((item) => item.path !== chip.path);
              return [...next, chip];
            });
          }
        } catch (err) {
          console.error("Vault path load failed", err);
          const name = path.split("/").pop() || path;
          setLoadedFiles((prev) => {
            if (prev.some((item) => item.path === path)) return prev;
            return [...prev, { path, name, size: 0 }];
          });
        }
      }
    } finally {
      setUploading(false);
    }
  }

  function canAcceptAgentDrop(e: DragEvent): boolean {
    if (disabled || uploadingRef.current) return false;
    if (isVaultMoveDrag(e)) return true;
    return Array.from(e.dataTransfer.types || []).includes("Files");
  }

  function onDragEnter(e: DragEvent) {
    if (!canAcceptAgentDrop(e)) return;
    e.preventDefault();
    e.stopPropagation();
    dragDepthRef.current += 1;
    setDragOver(true);
  }

  function onDragOver(e: DragEvent) {
    if (!canAcceptAgentDrop(e)) return;
    e.preventDefault();
    e.stopPropagation();
    e.dataTransfer.dropEffect = "copy";
  }

  function onDragLeave(e: DragEvent) {
    if (!dragOver && dragDepthRef.current === 0) return;
    e.preventDefault();
    e.stopPropagation();
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) setDragOver(false);
  }

  async function onDrop(e: DragEvent) {
    e.preventDefault();
    e.stopPropagation();
    dragDepthRef.current = 0;
    setDragOver(false);
    if (disabled || uploadingRef.current) return;

    const vault = parseVaultDrag(e);
    if (vault) {
      if (vault.kind === "folder") {
        setUploadError("폴더는 첨부할 수 없습니다. 노트를 선택해 주세요.");
        return;
      }
      await loadVaultPaths([vault.path]);
      return;
    }

    const files = Array.from(e.dataTransfer.files ?? []);
    if (files.length) {
      await loadWorkspaceFiles(files);
    }
  }

  function clearAttachments() {
    for (const item of attachmentsRef.current) {
      if (item.previewUrl.startsWith("blob:")) {
        URL.revokeObjectURL(item.previewUrl);
      }
    }
    setAttachments([]);
    setLoadedFiles([]);
  }

  function submit(textOverride?: string) {
    const text = (textOverride ?? value).trim();
    const imagePaths = attachments.map((item) => item.path);
    const filePaths = loadedFiles.map((item) => item.path);
    if (
      (!text && imagePaths.length === 0 && filePaths.length === 0 && !note) ||
      disabled ||
      waiting ||
      uploading
    ) {
      return;
    }
    if (!text && imagePaths.length === 0 && filePaths.length === 0) return;
    onSend({ text, imagePaths, filePaths });
    setValue("");
    clearAttachments();
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key !== "Enter" || e.shiftKey) return;
    if (
      e.nativeEvent.isComposing ||
      e.keyCode === 229 ||
      isComposingRef.current
    ) {
      submitAfterCompositionRef.current = true;
      return;
    }
    if (submitAfterCompositionRef.current) {
      e.preventDefault();
      return;
    }
    e.preventDefault();
    submit();
  }

  function onCompositionStart() {
    isComposingRef.current = true;
  }

  function onCompositionEnd(e: CompositionEvent<HTMLTextAreaElement>) {
    isComposingRef.current = false;
    if (!submitAfterCompositionRef.current) return;
    submitAfterCompositionRef.current = false;
    submit(e.currentTarget.value);
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (isComposingRef.current || submitAfterCompositionRef.current) return;
    submit();
  }

  async function onPaste(e: ClipboardEvent<HTMLTextAreaElement>) {
    if (disabled || waiting || uploading) return;
    const images = collectClipboardImages(e.clipboardData);
    if (!images.length) return;
    e.preventDefault();
    await uploadImageFiles(images);
  }

  function openImageUpload() {
    setMenuOpen(false);
    setUploadError(null);
    imageInputRef.current?.click();
  }

  function openLoadFiles() {
    setMenuOpen(false);
    setUploadError(null);
    loadInputRef.current?.click();
  }

  const inputDisabled = disabled || waiting;
  const addDisabled = !!disabled || uploading;
  const canSend =
    !inputDisabled &&
    !uploading &&
    (value.trim().length > 0 ||
      attachments.length > 0 ||
      loadedFiles.length > 0);

  const menu =
    menuOpen && menuPosition
      ? createPortal(
          <div
            ref={menuPortalRef}
            className="agent-add-menu agent-add-menu-portal"
            role="menu"
            style={{
              left: menuPosition.left,
              top: menuPosition.top,
              width: menuPosition.width,
            }}
          >
            <button
              type="button"
              className="agent-add-menu-item"
              role="menuitem"
              onClick={openImageUpload}
            >
              <span className="agent-add-menu-icon" aria-hidden="true">
                <svg width="16" height="16" viewBox="0 0 16 16">
                  <rect
                    x="2.5"
                    y="3.5"
                    width="11"
                    height="9"
                    rx="1.5"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.2"
                  />
                  <circle cx="6" cy="7" r="1.2" fill="currentColor" />
                  <path
                    d="M4.5 11.5 7 9l2 1.5 2.5-3 2 4"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </span>
              <span className="agent-add-menu-text">
                <span className="agent-add-menu-label">사진 첨부</span>
                <span className="agent-add-menu-desc">
                  이미지를 첨부하거나 Ctrl/⌘+V로 붙여넣기
                </span>
              </span>
            </button>
            <button
              type="button"
              className="agent-add-menu-item"
              role="menuitem"
              onClick={openLoadFiles}
            >
              <span className="agent-add-menu-icon" aria-hidden="true">
                <svg width="16" height="16" viewBox="0 0 16 16">
                  <path
                    d="M3.5 6.5 8 2l4.5 4.5M8 2v8.5"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                  <path
                    d="M2.5 11.5v1a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1v-1"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.2"
                    strokeLinecap="round"
                  />
                </svg>
              </span>
              <span className="agent-add-menu-text">
                <span className="agent-add-menu-label">Load files</span>
                <span className="agent-add-menu-desc">
                  현재 노트 폴더에 올리고 질문과 함께 전달
                </span>
              </span>
            </button>
          </div>,
          document.body,
        )
      : null;

  return (
    <div className="agent-chat-input-area">
      {uploadError && (
        <div className="agent-upload-error" role="alert">
          {uploadError}
        </div>
      )}
      {uploading && (
        <div className="agent-upload-status" role="status">
          업로드 중...
        </div>
      )}
      <form
        ref={inputWrapRef}
        className={`agent-chat-input-wrap${dragOver ? " is-dragover" : ""}`}
        onSubmit={onSubmit}
        onDragEnter={onDragEnter}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={(e) => void onDrop(e)}
      >
        {(note || extraNotes.length > 0) && (
          <div className="agent-loaded-files" aria-label="선택된 노트">
            {note && (
              <div className="agent-loaded-file" title={note.path}>
                <a
                  className="agent-loaded-file-open"
                  href={api.viewUrl(note.path)}
                  target="_blank"
                  rel="noopener noreferrer"
                  title={`${note.path}\n클릭하여 새 탭에서 열기`}
                >
                  <span className="agent-loaded-file-icon" aria-hidden="true">
                    <svg width="14" height="14" viewBox="0 0 16 16">
                      <path
                        d="M4 2.5h5.5L12 5v8.5a.5.5 0 0 1-.5.5H4a.5.5 0 0 1-.5-.5v-11a.5.5 0 0 1 .5-.5Z"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="1.2"
                      />
                      <path
                        d="M9.5 2.5V5H12"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="1.2"
                      />
                    </svg>
                  </span>
                  <span className="agent-loaded-file-meta">
                    <span className="agent-loaded-file-name">{note.name}</span>
                    {note.size > 0 && (
                      <span className="agent-loaded-file-size">
                        {formatFileSize(note.size)}
                      </span>
                    )}
                  </span>
                </a>
                <button
                  type="button"
                  className="agent-loaded-file-remove"
                  aria-label={`${note.name} 제거`}
                  onClick={() => onRemoveNote?.()}
                  disabled={inputDisabled}
                >
                  ×
                </button>
              </div>
            )}
            {extraNotes.map((item) => (
              <div key={item.path} className="agent-loaded-file" title={item.path}>
                <a
                  className="agent-loaded-file-open"
                  href={api.viewUrl(item.path)}
                  target="_blank"
                  rel="noopener noreferrer"
                  title={`${item.path}\n클릭하여 새 탭에서 열기`}
                >
                  <span className="agent-loaded-file-icon" aria-hidden="true">
                    <svg width="14" height="14" viewBox="0 0 16 16">
                      <path
                        d="M4 2.5h5.5L12 5v8.5a.5.5 0 0 1-.5.5H4a.5.5 0 0 1-.5-.5v-11a.5.5 0 0 1 .5-.5Z"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="1.2"
                      />
                      <path
                        d="M9.5 2.5V5H12"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="1.2"
                      />
                    </svg>
                  </span>
                  <span className="agent-loaded-file-meta">
                    <span className="agent-loaded-file-name">{item.name}</span>
                    {item.size > 0 && (
                      <span className="agent-loaded-file-size">
                        {formatFileSize(item.size)}
                      </span>
                    )}
                  </span>
                </a>
                <button
                  type="button"
                  className="agent-loaded-file-remove"
                  aria-label={`${item.name} 제거`}
                  onClick={() => onRemoveExtraNote?.(item.path)}
                  disabled={inputDisabled}
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )}
        {attachments.length > 0 && (
          <div className="agent-attachments" aria-label="첨부 이미지">
            {attachments.map((item) => (
              <div key={item.path} className="agent-attachment" title={item.path}>
                <a
                  className="agent-attachment-open"
                  href={api.viewUrl(item.path)}
                  target="_blank"
                  rel="noopener noreferrer"
                  title={`${item.name}\n클릭하여 새 탭에서 열기`}
                >
                  <img src={item.previewUrl} alt={item.name} />
                </a>
                <button
                  type="button"
                  className="agent-attachment-remove"
                  aria-label={`${item.name} 제거`}
                  disabled={inputDisabled || uploading}
                  onClick={() => {
                    setAttachments((prev) => {
                      const next: AgentAttachedImage[] = [];
                      for (const att of prev) {
                        if (att.path === item.path) {
                          if (att.previewUrl.startsWith("blob:")) {
                            URL.revokeObjectURL(att.previewUrl);
                          }
                          continue;
                        }
                        next.push(att);
                      }
                      return next;
                    });
                  }}
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )}
        {loadedFiles.length > 0 && (
          <div className="agent-loaded-files" aria-label="첨부 파일">
            {loadedFiles.map((file) => (
              <div key={file.path} className="agent-loaded-file" title={file.path}>
                <a
                  className="agent-loaded-file-open"
                  href={api.viewUrl(file.path)}
                  target="_blank"
                  rel="noopener noreferrer"
                  title={`${file.path}\n클릭하여 새 탭에서 열기`}
                >
                  <span className="agent-loaded-file-icon" aria-hidden="true">
                    <svg width="14" height="14" viewBox="0 0 16 16">
                      <path
                        d="M4 2.5h5.5L12 5v8.5a.5.5 0 0 1-.5.5H4a.5.5 0 0 1-.5-.5v-11a.5.5 0 0 1 .5-.5Z"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="1.2"
                      />
                      <path
                        d="M9.5 2.5V5H12"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="1.2"
                      />
                    </svg>
                  </span>
                  <span className="agent-loaded-file-meta">
                    <span className="agent-loaded-file-name">{file.name}</span>
                    {file.size > 0 && (
                      <span className="agent-loaded-file-size">
                        {formatFileSize(file.size)}
                      </span>
                    )}
                  </span>
                </a>
                <button
                  type="button"
                  className="agent-loaded-file-remove"
                  aria-label={`${file.name} 제거`}
                  disabled={inputDisabled || uploading}
                  onClick={() =>
                    setLoadedFiles((prev) =>
                      prev.filter((item) => item.path !== file.path),
                    )
                  }
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )}
        <textarea
          ref={textareaRef}
          className="agent-chat-input"
          rows={1}
          placeholder="메시지를 입력하거나 노트·파일을 끌어다 놓으세요..."
          value={value}
          disabled={inputDisabled}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={onKeyDown}
          onCompositionStart={onCompositionStart}
          onCompositionEnd={onCompositionEnd}
          onPaste={(e) => void onPaste(e)}
        />
        <div className="agent-chat-input-toolbar">
          <div className="agent-chat-add-wrap" ref={addWrapRef}>
            <button
              type="button"
              className="agent-chat-add-btn"
              aria-label="추가"
              aria-expanded={menuOpen}
              disabled={addDisabled}
              title="첨부"
              onClick={() => setMenuOpen((open) => !open)}
            >
              <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
                <path
                  d="M8 3v10M3 8h10"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                />
              </svg>
            </button>
          </div>
          <input
            ref={imageInputRef}
            className="agent-file-input"
            type="file"
            accept={IMAGE_ACCEPT}
            multiple
            onChange={(e) => {
              const files = Array.from(e.target.files ?? []).filter(isImageFile);
              e.target.value = "";
              void uploadImageFiles(files);
            }}
          />
          <input
            ref={loadInputRef}
            className="agent-file-input"
            type="file"
            accept={LOAD_ACCEPT}
            multiple
            onChange={(e) => {
              const files = Array.from(e.target.files ?? []);
              e.target.value = "";
              void loadWorkspaceFiles(files);
            }}
          />
          {waiting ? (
            <button
              className="agent-chat-send-btn is-waiting"
              type="button"
              aria-label="응답 중지"
              aria-busy="true"
              onClick={() => onStop?.()}
            >
              <span className="agent-chat-send-progress" aria-hidden="true" />
              <span className="agent-chat-send-stop" aria-hidden="true" />
            </button>
          ) : (
            <button
              className="agent-chat-send-btn"
              type="submit"
              aria-label="전송"
              disabled={!canSend}
            >
              <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
                <path
                  d="M8 12.5V3.5M4.5 7 8 3.5 11.5 7"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.6"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </button>
          )}
        </div>
      </form>
      {menu}
    </div>
  );
}
