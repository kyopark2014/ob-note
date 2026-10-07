import { useCallback, useEffect, useMemo, useRef, useState, type ClipboardEvent, type DragEvent, type PointerEvent as ReactPointerEvent } from "react";
import { api, type ClearingScan, type CompressJobStatus } from "./api";
import { FileTree, acceptDrop, getActiveVaultDrag, hasExternalFileDrag, isMarkdownNotePath, isVaultMoveDrag, parseVaultDrag, vaultDropEffect } from "./components/FileTree";
import {
  AlertDialog,
  ConfirmDialog,
  type ConfirmOptions,
} from "./components/ConfirmDialog";
import { ConfigDrawer } from "./components/ConfigDrawer";
import { SyncProgressModal, type SyncProgressInfo } from "./components/SyncProgressModal";
import { SharedListModal } from "./components/SharedListModal";
import { CompressListModal } from "./components/CompressListModal";
import { ClearingListModal } from "./components/ClearingListModal";
import { GoogleLoginModal } from "./components/GoogleLoginModal";
import { NotesConfigureModal } from "./components/NotesConfigureModal";
import { NotesGraphModal } from "./components/NotesGraphModal";
import { DocumentsConfigureModal } from "./components/DocumentsConfigureModal";
import { DocumentsListModal } from "./components/DocumentsListModal";
import {
  FolderContextMenu,
  type ContextMenuState,
  type FileMenuAction,
  type FolderMenuAction,
  type PanelMenuAction,
} from "./components/FolderContextMenu";
import { TabContextMenu, type TabContextMenuState, type TabMenuAction } from "./components/TabContextMenu";
import { ImagePreview } from "./components/ImagePreview";
import { VideoPreview } from "./components/VideoPreview";
import { MarkdownEditor, type MarkdownEditorHandle } from "./components/markdownEditor/MarkdownEditor";
import {
  AppearanceIcon,
  ArchiveIcon,
  ClearingIcon,
  AgentIcon,
  ExpandIcon,
  CollapseIcon,
  BookIcon,
  DocumentsIcon,
  EditIcon,
  SaveIcon,
  FilesIcon,
  GraphIcon,
  LogoutIcon,
  MicIcon,
  ModelIcon,
  PlusFileIcon,
  PlusFolderIcon,
  RefreshIcon,
  SearchIcon,
  SettingsIcon,
  ShareListIcon,
  SyncIcon,
  ViewIcon,
} from "./components/Icons";
import { MeetingLogSidebar } from "./components/MeetingLogSidebar";
import { AgentPanel } from "./components/AgentPanel";
import {
  DEFAULT_AGENT_MODEL,
  getAgentModel,
  setAgentModel,
} from "./agentModelSettings";
import { MEETING_FOLDER, DEFAULT_MEETING_TITLE } from "./meetingLog/config";
import {
  buildMeetingMarkdown,
  extractTitleFromEntries,
  meetingFileBaseName,
} from "./meetingLog/format";
import { useMeetingLog } from "./meetingLog/useMeetingLog";
import { useTheme } from "./hooks/useTheme";
import type { Theme } from "./theme";
import {
  getLayoutMode,
  labelToLayoutMode,
  LAYOUT_OPTIONS,
  layoutModeToLabel,
  setLayoutMode as persistLayoutMode,
  type LayoutMode,
} from "./layoutSettings";
import {
  getPinnedPaths,
  removePinnedPaths,
  rewritePinnedPaths,
  setPinnedPaths as persistPinnedPaths,
  togglePinnedPath,
} from "./pinSettings";
import {
  ensureAncestorsOpen,
  removeOpenFolders,
  rewriteOpenFolders,
  setFolderOpen,
} from "./treeSettings";
import {
  getShowImages,
  isCompanionMediaFileName,
  isImageFileName,
  isVideoFileName,
  setShowImages as persistShowImages,
} from "./viewSettings";
import {
  SIDEBAR_W_DEFAULT,
  clampSidebarWidth,
  getSidebarWidth,
  setSidebarWidth as persistSidebarWidth,
} from "./sidebarSettings";
import {
  AGENT_W_DEFAULT,
  clampAgentWidth,
  getAgentWidth,
  setAgentWidth as persistAgentWidth,
} from "./agentPanelSettings";
import { resolveWikiTarget, wikiLinkMarkdown } from "./wikiLink";
import type {
  FilePayload,
  OpenTab,
  PanelMode,
  SearchHit,
  TreeNode,
  ViewMode,
} from "./types";

const THEME_OPTIONS = ["Light", "Dark"] as const;
const LAYOUT_MENU = [...LAYOUT_OPTIONS];
const VIEW_OPTIONS = ["Images"] as const;
const GRAPH_OPTIONS = ["Sync", "Rebuild", "Graph", "Configure"] as const;
const DOCUMENTS_OPTIONS = ["Projects", "Drawings", "Configure"] as const;
const SHARE_PERMISSION_OPTIONS = [
  "Current",
  "1-hop",
  "Shared folder",
  "Entire vault",
] as const;

type SharePermission = "current" | "one_hop" | "folder" | "vault";

function sharePermissionToLabel(permission: SharePermission): string {
  switch (permission) {
    case "current":
      return "Current";
    case "folder":
      return "Shared folder";
    case "vault":
      return "Entire vault";
    case "one_hop":
    default:
      return "1-hop";
  }
}

function labelToSharePermission(label: string): SharePermission {
  switch (label) {
    case "Current":
      return "current";
    case "Shared folder":
      return "folder";
    case "Entire vault":
      return "vault";
    case "1-hop":
    default:
      return "one_hop";
  }
}

function themeToLabel(theme: Theme): string {
  return theme === "light" ? "Light" : "Dark";
}

function labelToTheme(label: string): Theme {
  return label === "Light" ? "light" : "dark";
}

function filterTreeForView(nodes: TreeNode[], showImages: boolean): TreeNode[] {
  return nodes
    .map((n) => {
      if (n.type !== "folder") return n;
      return { ...n, children: filterTreeForView(n.children || [], showImages) };
    })
    .filter((n) => {
      if (n.type === "folder") return true;
      if (showImages) return true;
      return !isCompanionMediaFileName(n.name);
    });
}

const SKIP_CONFIRM_PREFIX = "ob-note:skip-confirm:";

function shouldSkipConfirm(key: string): boolean {
  try {
    return localStorage.getItem(`${SKIP_CONFIRM_PREFIX}${key}`) === "1";
  } catch {
    return false;
  }
}

function rememberSkipConfirm(key: string): void {
  try {
    localStorage.setItem(`${SKIP_CONFIRM_PREFIX}${key}`, "1");
  } catch {
    /* ignore */
  }
}

function noteTemplate(title: string): string {
  return `# ${title}\n\n`;
}

/** First ATX H1 in the note body (frontmatter skipped). */
function extractH1(md: string): string | null {
  let body = md;
  if (body.startsWith("---\n") || body.startsWith("---\r\n")) {
    const end = body.indexOf("\n---", 3);
    if (end >= 0) {
      const after = body.indexOf("\n", end + 4);
      body = after >= 0 ? body.slice(after + 1) : "";
    }
  }
  const m = body.match(/^#\s+(.+?)\s*$/m);
  return m ? m[1].trim() : null;
}

function sanitizeFilename(title: string): string {
  const cleaned = title
    .replace(/[\\/:*?"<>|#]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 120);
  return cleaned || "Untitled";
}

function uniqueNamedPath(
  parent: string,
  baseName: string,
  tree: TreeNode[],
  excludePath?: string,
): string {
  const children = (() => {
    if (!parent) return tree;
    const stack: TreeNode[] = [...tree];
    while (stack.length) {
      const n = stack.pop()!;
      if (n.path === parent && n.type === "folder") return n.children || [];
      if (n.children) stack.push(...n.children);
    }
    return [];
  })();

  const names = new Set(
    children.filter((c) => c.path !== excludePath).map((c) => c.name),
  );
  let name = `${baseName}.md`;
  let i = 1;
  while (names.has(name)) {
    i += 1;
    name = `${baseName} ${i}.md`;
  }
  return parent ? `${parent}/${name}` : name;
}

function uniqueNotePath(parent: string, tree: TreeNode[]): string {
  return uniqueNamedPath(parent, "Untitled", tree);
}

const LAST_NOTE_KEY = "ob-note:last-note-path";
/** Viewport width that switches Auto layout to the mobile overlay. Not a device check. */
const NARROW_LAYOUT_MQ = "(max-width: 1024px)";

function flattenMarkdownPaths(nodes: TreeNode[]): string[] {
  const out: string[] = [];
  for (const n of nodes) {
    if (n.type === "file" && /\.md$/i.test(n.name)) out.push(n.path);
    if (n.children?.length) out.push(...flattenMarkdownPaths(n.children));
  }
  return out;
}

function readLastNotePath(): string | null {
  try {
    return localStorage.getItem(LAST_NOTE_KEY);
  } catch {
    return null;
  }
}

function writeLastNotePath(path: string): void {
  try {
    localStorage.setItem(LAST_NOTE_KEY, path);
    ensureAncestorsOpen(path);
  } catch {
    /* ignore */
  }
}

function clearLastNotePath(path?: string): void {
  try {
    if (!path || localStorage.getItem(LAST_NOTE_KEY) === path) {
      localStorage.removeItem(LAST_NOTE_KEY);
    }
  } catch {
    /* ignore */
  }
}

/** Normalize vault-relative note path from a deep-link query value. */
function normalizeVaultNotePath(raw: string): string | null {
  let p = raw.trim();
  if (
    (p.startsWith('"') && p.endsWith('"')) ||
    (p.startsWith("'") && p.endsWith("'"))
  ) {
    p = p.slice(1, -1).trim();
  }
  try {
    // Handle once- or twice-encoded values from copied links.
    p = decodeURIComponent(p);
  } catch {
    /* keep as-is */
  }
  p = p.replace(/\\/g, "/").replace(/^\/+/, "").trim();
  if (!p) return null;
  if (p.split("/").some((seg) => seg === "..")) return null;
  if (!/\.md$/i.test(p)) p = `${p}.md`;
  return p;
}

/**
 * Private deep link: `/?note=AI/Knowledge%20Graph/Note.md`
 * (not a public share — requires login; path stays in the address bar).
 */
function readDeepLinkNotePath(): string | null {
  try {
    const params = new URLSearchParams(window.location.search);
    const raw = params.get("note") ?? params.get("path");
    if (!raw) return null;
    return normalizeVaultNotePath(raw);
  } catch {
    return null;
  }
}

function syncDeepLinkNotePath(path: string | null): void {
  try {
    const url = new URL(window.location.href);
    if (path) {
      url.searchParams.set("note", path);
      url.searchParams.delete("path");
    } else {
      url.searchParams.delete("note");
      url.searchParams.delete("path");
    }
    const next = `${url.pathname}${url.search}${url.hash}`;
    if (`${window.location.pathname}${window.location.search}${window.location.hash}` !== next) {
      window.history.replaceState(null, "", next);
    }
  } catch {
    /* ignore */
  }
}

function noteParentDir(notePath: string): string {
  return notePath.includes("/") ? notePath.slice(0, notePath.lastIndexOf("/")) : "";
}

function flattenAllPaths(nodes: TreeNode[]): string[] {
  const out: string[] = [];
  for (const n of nodes) {
    out.push(n.path);
    if (n.children?.length) out.push(...flattenAllPaths(n.children));
  }
  return out;
}

function findTreeNode(nodes: TreeNode[], path: string): TreeNode | null {
  for (const n of nodes) {
    if (n.path === path) return n;
    if (n.children?.length) {
      const hit = findTreeNode(n.children, path);
      if (hit) return hit;
    }
  }
  return null;
}

function extFromImageMime(mime: string): string {
  const map: Record<string, string> = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
  };
  return map[mime.toLowerCase()] || "png";
}

function safeUploadBaseName(fileName: string, fallback: string): string {
  const base = (fileName.normalize("NFC").split(/[/\\]/).pop() || fallback).replace(
    /[\\/:*?"<>|#\[\]]/g,
    "_",
  );
  return base || fallback;
}

function allocateUploadPath(parent: string, fileName: string, existing: Set<string>): string {
  const safe = safeUploadBaseName(fileName, "video.mp4");
  const join = (name: string) => (parent ? `${parent}/${name}` : name);
  let path = join(safe);
  if (!existing.has(path)) {
    existing.add(path);
    return path;
  }
  const dot = safe.lastIndexOf(".");
  const stem = dot > 0 ? safe.slice(0, dot) : safe;
  const ext = dot > 0 ? safe.slice(dot) : "";
  let i = 2;
  while (existing.has(join(`${stem}-${i}${ext}`))) i += 1;
  path = join(`${stem}-${i}${ext}`);
  existing.add(path);
  return path;
}

function isVideoUpload(file: File): boolean {
  return (
    isVideoFileName(file.name) ||
    file.type === "video/mp4" ||
    file.type === "video/webm" ||
    file.type === "video/x-m4v"
  );
}

function isMarkdownFileName(name: string): boolean {
  return /\.(md|markdown)$/i.test(name);
}

/** Finder/Explorer copy → paste exposes the file on clipboardData. */
function markdownFilesFromClipboard(data: DataTransfer | null): File[] {
  if (!data) return [];
  const out: File[] = [];
  const seen = new Set<string>();
  const push = (file: File | null) => {
    if (!file || !isMarkdownFileName(file.name)) return;
    const key = `${file.name}\0${file.size}\0${file.lastModified}`;
    if (seen.has(key)) return;
    seen.add(key);
    out.push(file);
  };
  for (const file of Array.from(data.files || [])) push(file);
  for (const item of Array.from(data.items || [])) {
    if (item.kind === "file") push(item.getAsFile());
  }
  return out;
}

function clipboardHasFiles(data: DataTransfer | null): boolean {
  if (!data) return false;
  if (data.files?.length) return true;
  return Array.from(data.items || []).some((item) => item.kind === "file");
}

function isTextEntryTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return !!target.closest("input, textarea, select, [contenteditable='true']");
}

/** Character offset under the pointer, using the textarea's current text. */
function textareaDropIndex(ta: HTMLTextAreaElement, clientX: number, clientY: number): number {
  const text = ta.value;
  const fallback = ta.selectionStart ?? text.length;
  if (!text) return 0;
  const rect = ta.getBoundingClientRect();
  if (clientY > rect.bottom) return text.length;
  if (clientY < rect.top) return 0;
  const x = Math.min(rect.right - 1, Math.max(rect.left + 1, clientX));
  const y = Math.min(rect.bottom - 1, Math.max(rect.top + 1, clientY));
  const style = getComputedStyle(ta);
  const mirror = document.createElement("div");
  mirror.textContent = text;
  mirror.style.position = "fixed";
  mirror.style.left = `${rect.left}px`;
  mirror.style.top = `${rect.top}px`;
  mirror.style.width = `${rect.width}px`;
  mirror.style.height = `${rect.height}px`;
  mirror.style.boxSizing = "border-box";
  mirror.style.overflow = "hidden";
  mirror.style.margin = "0";
  mirror.style.opacity = "0";
  mirror.style.zIndex = "2147483646";
  mirror.style.whiteSpace = style.whiteSpace || "pre-wrap";
  mirror.style.overflowWrap = style.overflowWrap;
  mirror.style.wordBreak = style.wordBreak;
  mirror.style.font = style.font;
  mirror.style.letterSpacing = style.letterSpacing;
  mirror.style.lineHeight = style.lineHeight;
  mirror.style.tabSize = style.tabSize;
  mirror.style.padding = style.padding;
  mirror.style.border = style.border;
  mirror.style.textAlign = style.textAlign;
  document.body.appendChild(mirror);
  if (ta.scrollTop) mirror.scrollTop = ta.scrollTop;
  if (ta.scrollLeft) mirror.scrollLeft = ta.scrollLeft;
  try {
    const node = mirror.firstChild;
    if (!node || node.nodeType !== Node.TEXT_NODE) return fallback;
    const textNode = node as Text;
    const range = document.createRange();
    const box = (i: number): DOMRect | null => {
      if (i <= 0) {
        range.setStart(textNode, 0);
        range.setEnd(textNode, Math.min(1, text.length));
      } else if (i >= text.length) {
        range.setStart(textNode, text.length - 1);
        range.setEnd(textNode, text.length);
      } else {
        range.setStart(textNode, i);
        range.setEnd(textNode, i + 1);
      }
      return range.getClientRects()[0] ?? null;
    };
    let lo = 0;
    let hi = text.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      const r = box(mid);
      if (r && r.bottom <= y) lo = mid + 1;
      else hi = mid;
    }
    let i = Math.min(lo, text.length);
    const anchor = box(Math.min(i, Math.max(0, text.length - 1)));
    if (!anchor) return text.length;
    const lineTop = anchor.top;
    while (i > 0) {
      const prev = box(i - 1);
      if (!prev || prev.top < lineTop - 1) break;
      i -= 1;
    }
    let best = i;
    let bestDx = Number.POSITIVE_INFINITY;
    while (i <= text.length) {
      if (i < text.length) {
        const r = box(i);
        if (r && r.top > lineTop + 1) break;
        const dx = Math.abs((r?.left ?? 0) - x);
        if (dx < bestDx) {
          bestDx = dx;
          best = i;
        }
      } else {
        const prev = box(text.length - 1);
        const dx = Math.abs((prev?.right ?? 0) - x);
        if (dx < bestDx) best = text.length;
        break;
      }
      i += 1;
    }
    return best;
  } finally {
    mirror.remove();
  }
}

/** Insert text through the browser so Command-Z can undo it. */
function insertEditorText(
  el: HTMLTextAreaElement,
  start: number,
  end: number,
  text: string,
): void {
  el.focus();
  const max = el.value.length;
  const a = Math.max(0, Math.min(start, max));
  const b = Math.max(a, Math.min(end, max));
  const next = `${el.value.slice(0, a)}${text}${el.value.slice(b)}`;
  el.setSelectionRange(a, b);
  const inserted = document.execCommand("insertText", false, text);
  if (inserted && el.value === next) return;
  el.value = next;
  const caret = a + text.length;
  el.setSelectionRange(caret, caret);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

function uniqueImagePath(parent: string, ext: string, tree: TreeNode[]): string {
  const existing = new Set(flattenAllPaths(tree));
  let path = "";
  do {
    const id =
      typeof crypto !== "undefined" && "randomUUID" in crypto
        ? crypto.randomUUID().replace(/-/g, "")
        : `${Date.now().toString(16)}${Math.random().toString(16).slice(2, 10)}`;
    const name = `img_${id}.${ext}`;
    path = parent ? `${parent}/${name}` : name;
  } while (existing.has(path));
  return path;
}

export default function App() {
  const { theme, setTheme } = useTheme();
  const [ready, setReady] = useState(false);
  const [authError, setAuthError] = useState<{ login_url?: string } | null>(null);
  const [authBusy, setAuthBusy] = useState(false);
  const [loginError, setLoginError] = useState<string | null>(null);
  const [publicConfig, setPublicConfig] = useState<{
    auth_mode: "google" | "cognito";
    google_client_id: string;
    local_auth_bypass: boolean;
    sharing_url: string;
    project_name: string;
    cognito_admin_username: string;
  } | null>(null);
  const [userId, setUserId] = useState<string | null>(null);
  const [panel, setPanel] = useState<PanelMode>("files");
  const [viewportNarrow, setViewportNarrow] = useState(() =>
    typeof window !== "undefined" ? window.matchMedia(NARROW_LAYOUT_MQ).matches : false,
  );
  const [layoutMode, setLayoutMode] = useState<LayoutMode>(() => getLayoutMode());
  const isNarrow =
    layoutMode === "mobile" ? true : layoutMode === "desktop" ? false : viewportNarrow;
  const meeting = useMeetingLog(userId);
  const [tree, setTree] = useState<TreeNode[]>([]);
  const [treeRefreshing, setTreeRefreshing] = useState(false);
  const [tabs, setTabs] = useState<OpenTab[]>([]);
  const [activePath, setActivePath] = useState<string | null>(null);
  const [selectedFolder, setSelectedFolder] = useState<string | null>(null);
  const [treeFocus, setTreeFocus] = useState<"file" | "folder">("file");
  const [file, setFile] = useState<FilePayload | null>(null);
  const [draft, setDraft] = useState("");
  const [viewMode, setViewMode] = useState<ViewMode>("preview");
  const [noteFullscreen, setNoteFullscreen] = useState(false);
  const [fullscreenChrome, setFullscreenChrome] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [searchQ, setSearchQ] = useState("");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [saving, setSaving] = useState(false);
  const [draftFolder, setDraftFolder] = useState<{ parentPath: string } | null>(null);
  const [renamingPath, setRenamingPath] = useState<string | null>(null);
  const [ctxMenu, setCtxMenu] = useState<ContextMenuState | null>(null);
  const [tabMenu, setTabMenu] = useState<TabContextMenuState | null>(null);
  const [confirmState, setConfirmState] = useState<{
    options: ConfirmOptions;
    resolve: (ok: boolean) => void;
  } | null>(null);
  const [alertState, setAlertState] = useState<{
    title?: string;
    message: string;
    link?: { href: string; label: string } | null;
    resolve: () => void;
  } | null>(null);
  const [compressPopupOpen, setCompressPopupOpen] = useState(false);
  const [compressListOpen, setCompressListOpen] = useState(false);
  const [clearingOpen, setClearingOpen] = useState(false);
  const [clearingScan, setClearingScan] = useState<ClearingScan | null>(null);
  const [clearingPopupOpen, setClearingPopupOpen] = useState(false);
  const [clearingBusy, setClearingBusy] = useState(false);
  const [clearingDeletePopupOpen, setClearingDeletePopupOpen] = useState(false);
  const [clearingDeleteBusy, setClearingDeleteBusy] = useState(false);
  const [clearingDeleteMsg, setClearingDeleteMsg] = useState<string | null>(null);
  const [clearingDeleteProgress, setClearingDeleteProgress] = useState<SyncProgressInfo | null>(null);
  const [clearingMsg, setClearingMsg] = useState<string | null>(null);
  const [clearingProgress, setClearingProgress] = useState<SyncProgressInfo | null>(null);
  const [compressBusy, setCompressBusy] = useState(false);
  const [compressMsg, setCompressMsg] = useState<string | null>(null);
  const [compressProgress, setCompressProgress] = useState<SyncProgressInfo | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [appearanceOpen, setAppearanceOpen] = useState(false);
  const [viewOpen, setViewOpen] = useState(false);
  const [layoutOpen, setLayoutOpen] = useState(false);
  const [sharePermissionOpen, setSharePermissionOpen] = useState(false);
  const [sharePermission, setSharePermission] = useState<SharePermission>("one_hop");
  const [graphMenuOpen, setGraphMenuOpen] = useState(false);
  const [sharedListOpen, setSharedListOpen] = useState(false);
  const [notesGraphOpen, setNotesGraphOpen] = useState(false);
  const [notesConfigureOpen, setNotesConfigureOpen] = useState(false);
  const [notesSyncBusy, setNotesSyncBusy] = useState(false);
  const [notesSyncPopupOpen, setNotesSyncPopupOpen] = useState(false);
  const [notesSyncTitle, setNotesSyncTitle] = useState("Notes Sync");
  const [notesSyncMsg, setNotesSyncMsg] = useState<string | null>(null);
  const [notesSyncProgress, setNotesSyncProgress] = useState<SyncProgressInfo | null>(
    null,
  );
  const [documentsMenuOpen, setDocumentsMenuOpen] = useState(false);
  const [documentsConfigureOpen, setDocumentsConfigureOpen] = useState(false);
  const [documentsListOpen, setDocumentsListOpen] = useState(false);
  const [documentsListKind, setDocumentsListKind] = useState<"project" | "drawing">(
    "project",
  );
  const [documentsSyncBusy, setDocumentsSyncBusy] = useState(false);
  const [documentsSyncPopupOpen, setDocumentsSyncPopupOpen] = useState(false);
  const [documentsSyncMsg, setDocumentsSyncMsg] = useState<string | null>(null);
  const [documentsSyncProgress, setDocumentsSyncProgress] =
    useState<SyncProgressInfo | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [syncPopupOpen, setSyncPopupOpen] = useState(false);
  const [syncMsg, setSyncMsg] = useState<string | null>(null);
  const [syncProgress, setSyncProgress] = useState<SyncProgressInfo | null>(null);
  const [pendingSync, setPendingSync] = useState(0);
  const [showImages, setShowImages] = useState(() => getShowImages());
  const [sidebarWidth, setSidebarWidth] = useState(() => getSidebarWidth());
  const [sidebarResizing, setSidebarResizing] = useState(false);
  const sidebarWidthRef = useRef(sidebarWidth);
  sidebarWidthRef.current = sidebarWidth;
  const [agentOpen, setAgentOpen] = useState(false);
  const [agentNotePath, setAgentNotePath] = useState<string | null>(null);
  const [modelMenuOpen, setModelMenuOpen] = useState(false);
  const [agentModel, setAgentModelState] = useState(() => getAgentModel());
  const [agentModels, setAgentModels] = useState<string[]>([DEFAULT_AGENT_MODEL]);
  const [agentWidth, setAgentWidth] = useState(() => getAgentWidth());
  const [agentResizing, setAgentResizing] = useState(false);
  const agentWidthRef = useRef(agentWidth);
  agentWidthRef.current = agentWidth;

  // While Open Agent is open, follow the selected note and load its chat history.
  useEffect(() => {
    if (!agentOpen) return;
    if (activePath && /\.md$/i.test(activePath)) {
      setAgentNotePath(activePath);
    }
  }, [agentOpen, activePath]);

  useEffect(() => {
    const mq = window.matchMedia(NARROW_LAYOUT_MQ);
    const sync = () => setViewportNarrow(mq.matches);
    sync();
    mq.addEventListener("change", sync);
    return () => mq.removeEventListener("change", sync);
  }, []);
  const [pinnedPaths, setPinnedPathsState] = useState<string[]>(() => getPinnedPaths());
  const [settingsFlyoutPos, setSettingsFlyoutPos] = useState<{ left: number; bottom: number } | null>(
    null,
  );
  const [pastingImage, setPastingImage] = useState(false);
  const settingsBtnRef = useRef<HTMLButtonElement>(null);
  const appearanceBtnRef = useRef<HTMLButtonElement>(null);
  const sharePermissionBtnRef = useRef<HTMLButtonElement>(null);
  const viewBtnRef = useRef<HTMLButtonElement>(null);
  const layoutBtnRef = useRef<HTMLButtonElement>(null);
  const graphBtnRef = useRef<HTMLButtonElement>(null);
  const modelBtnRef = useRef<HTMLButtonElement>(null);
  const documentsBtnRef = useRef<HTMLButtonElement>(null);
  const settingsFlyoutRef = useRef<HTMLDivElement>(null);
  const editorRef = useRef<HTMLTextAreaElement | null>(null);
  const markdownEditorRef = useRef<MarkdownEditorHandle | null>(null);
  const editorComposingRef = useRef(false);
  const resizeEditor = useCallback((el: HTMLTextAreaElement | null) => {
    if (!el || editorComposingRef.current) return;
    el.style.height = "auto";
    el.style.height = `${Math.max(el.scrollHeight, 320)}px`;
  }, []);
  const bindEditor = useCallback((el: HTMLTextAreaElement | null) => {
    editorRef.current = el;
    resizeEditor(el);
  }, [resizeEditor]);
  const sidebarBodyRef = useRef<HTMLDivElement | null>(null);
  /** Serializes H1↔filename renames so save never races a half-finished rename. */
  const renameChainRef = useRef(Promise.resolve());
  /** Old path → latest path after H1 auto-rename (follows chains). */
  const renamedFromRef = useRef(new Map<string, string>());
  const draftRef = useRef(draft);
  const activePathRef = useRef(activePath);
  const tabsRef = useRef(tabs);
  /** Bumps on each open so a slower read cannot overwrite a newer tab switch. */
  const openSeqRef = useRef(0);
  /** Tab path already requested while openFile is still reading. */
  const tabNavTargetRef = useRef<string | null>(null);
  const treeRef = useRef(tree);
  const didRestoreNote = useRef(false);
  const loginSyncUserRef = useRef<string | null>(null);
  const compressFinishRef = useRef<string | null>(null);
  const compressSawActiveRef = useRef(false);
  const compressBusyRef = useRef(false);
  const compressJobRef = useRef<string | null>(null);
  const clearingOpenedRef = useRef<string | null>(null);
  draftRef.current = draft;
  activePathRef.current = activePath;
  tabsRef.current = tabs;
  treeRef.current = tree;

  const openSourceEditor = useCallback(() => {
    const path = activePathRef.current;
    const handle = markdownEditorRef.current;
    if (!handle) {
      setViewMode("edit");
      return;
    }
    handle.flushThen(() => {
      if (activePathRef.current !== path) return;
      setViewMode("edit");
    });
  }, []);

  const updatePinnedPaths = useCallback((next: string[]) => {
    setPinnedPathsState(next);
    persistPinnedPaths(next);
  }, []);

  const resolveLatestPath = useCallback((path: string): string => {
    let cur = path;
    const seen = new Set<string>();
    while (renamedFromRef.current.has(cur) && !seen.has(cur)) {
      // The open note occupies this path. Reused names (a new Untitled.md after
      // a retitle) must not follow the old alias or the save overwrites that note.
      if (cur === activePathRef.current) break;
      seen.add(cur);
      cur = renamedFromRef.current.get(cur)!;
    }
    return cur;
  }, []);

  const syncFilenameToH1 = useCallback(
    async (path: string, content: string, currentTree: TreeNode[]): Promise<string> => {
      const title = extractH1(content);
      if (!title) return resolveLatestPath(path);
      const safe = sanitizeFilename(title);
      if (!safe) return resolveLatestPath(path);

      let release!: () => void;
      const prev = renameChainRef.current;
      renameChainRef.current = new Promise<void>((resolve) => {
        release = resolve;
      });
      try {
        await prev;
        // Follow any rename that finished while we waited (stale Old.md → New.md).
        const startPath = resolveLatestPath(path);
        // Server moves the existing note row (Untitled → H1) and deletes the old file.
        const written = await api.writeFile(startPath, content, { syncFilename: true });
        const live = written.path || startPath;
        if (live !== startPath) {
          renamedFromRef.current.set(startPath, live);
          if (
            activePathRef.current === startPath ||
            activePathRef.current === path ||
            !activePathRef.current
          ) {
            activePathRef.current = live;
          }
          return live;
        }
        const parts = live.split("/");
        const parent = parts.slice(0, -1).join("/");
        const currentStem = parts[parts.length - 1]?.replace(/\.md$/i, "") || "";
        if (safe === currentStem) return live;
        const dest = uniqueNamedPath(parent, safe, currentTree, live);
        if (dest === live) return live;
        await api.rename(live, dest);
        renamedFromRef.current.set(startPath, dest);
        if (live !== startPath) renamedFromRef.current.set(live, dest);
        // Sync ref immediately so concurrent save/read sees the new path before React re-renders.
        if (
          activePathRef.current === startPath ||
          activePathRef.current === live ||
          activePathRef.current === path ||
          !activePathRef.current
        ) {
          activePathRef.current = dest;
        }
        return dest;
      } finally {
        release();
      }
    },
    [resolveLatestPath],
  );

  const bootstrap = useCallback(async () => {
    try {
      const cfg = await api.getPublicConfig();
      setPublicConfig(cfg);
    } catch {
      setPublicConfig(null);
    }
    try {
      const session = await api.getSession();
      setUserId(session.user_id);
      setAuthError(null);
      setLoginError(null);
    } catch (err) {
      const e = err as Error & { status?: number; detail?: unknown };
      if (e.status === 401) {
        // Do not auto-create local session — show Google login (or local bypass form).
        const detail = e.detail as { detail?: { login_url?: string } } | undefined;
        setAuthError({ login_url: detail?.detail?.login_url });
        setReady(true);
        return;
      }
      setAuthError({});
      setReady(true);
      return;
    }
    try {
      const t = await api.getTree();
      setTree(t.children);
    } catch {
      setTree([]);
    }
    setReady(true);
  }, []);

  const finishLogin = useCallback(async (user_id: string) => {
    setUserId(user_id);
    setAuthError(null);
    setLoginError(null);
    try {
      const t = await api.getTree();
      setTree(t.children);
    } catch {
      setTree([]);
    }
    setReady(true);
  }, []);

  const handleGoogleAccessToken = useCallback(
    async (accessToken: string) => {
      setAuthBusy(true);
      setLoginError(null);
      try {
        const session = await api.setSessionWithAccessToken(accessToken);
        await finishLogin(session.user_id);
      } catch (err) {
        setLoginError(err instanceof Error ? err.message : String(err));
      } finally {
        setAuthBusy(false);
      }
    },
    [finishLogin],
  );

  const handleLocalUserId = useCallback(
    async (localUserId: string) => {
      setAuthBusy(true);
      setLoginError(null);
      try {
        const session = await api.createLocalSession(localUserId);
        await finishLogin(session.user_id);
      } catch (err) {
        setLoginError(err instanceof Error ? err.message : String(err));
      } finally {
        setAuthBusy(false);
      }
    },
    [finishLogin],
  );

  const handleCognitoLogin = useCallback(
    async (username: string, password: string) => {
      setAuthBusy(true);
      setLoginError(null);
      try {
        const session = await api.loginWithCognito(username, password);
        await finishLogin(session.user_id);
      } catch (err) {
        setLoginError(err instanceof Error ? err.message : String(err));
      } finally {
        setAuthBusy(false);
      }
    },
    [finishLogin],
  );

  const handleLogout = useCallback(async () => {
    setSettingsOpen(false);
    setAppearanceOpen(false);
    setViewOpen(false);
    setLayoutOpen(false);
    setSharePermissionOpen(false);
    setGraphMenuOpen(false);
    setSharedListOpen(false);
    try {
      await api.clearSession();
    } catch {
      /* still clear local UI */
    }
    try {
      window.google?.accounts?.id?.disableAutoSelect();
    } catch {
      /* GSI may not be loaded */
    }
    loginSyncUserRef.current = null;
    setUserId(null);
    setTree([]);
    setTabs([]);
    setActivePath(null);
    setSelectedFolder(null);
    setFile(null);
    setDraft("");
    setDirty(false);
    setHits([]);
    setSearchQ("");
    setPanel("files");
    setViewMode("preview");
    setLoginError(null);
    setAuthBusy(false);
    setAuthError({});
    // Allow restore (incl. ?note= deep link) after the next successful login.
    didRestoreNote.current = false;
    try {
      const cfg = await api.getPublicConfig();
      setPublicConfig(cfg);
    } catch {
      /* keep last public config for Google client id */
    }
  }, []);

  useEffect(() => {
    void bootstrap();
  }, [bootstrap]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const res = await api.agentModels();
        if (cancelled) return;
        const list = res.models?.length ? res.models : [DEFAULT_AGENT_MODEL];
        setAgentModels(list);
        setAgentModelState((prev) => {
          const next = list.includes(prev)
            ? prev
            : res.default_model || DEFAULT_AGENT_MODEL;
          if (next !== prev) setAgentModel(next);
          return next;
        });
      } catch {
        /* keep defaults until auth / harness is ready */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [userId]);

  const refreshTree = useCallback(async () => {
    const t = await api.getTree();
    treeRef.current = t.children;
    setTree(t.children);
  }, []);

  /** Refresh now, then again shortly after (S3 pending / eventual consistency). */
  const refreshTreeAfterMutation = useCallback(async () => {
    await refreshTree();
    window.setTimeout(() => {
      void refreshTree();
    }, 800);
    window.setTimeout(() => {
      void refreshTree();
    }, 2500);
  }, [refreshTree]);

  const removeTreePath = useCallback((targetPath: string, asFolder: boolean) => {
    const prune = (nodes: TreeNode[]): TreeNode[] =>
      nodes
        .filter((n) => {
          if (n.path === targetPath) return false;
          if (asFolder && n.path.startsWith(targetPath + "/")) return false;
          return true;
        })
        .map((n) =>
          n.children ? { ...n, children: prune(n.children) } : n,
        );
    setTree((prev) => prune(prev));
  }, []);

  const handleRefreshTree = useCallback(async () => {
    if (treeRefreshing) return;
    setTreeRefreshing(true);
    try {
      await refreshTree();
    } finally {
      setTreeRefreshing(false);
    }
  }, [refreshTree, treeRefreshing]);

  const refreshSyncStatus = useCallback(async () => {
    try {
      const s = await api.getSyncStatus();
      setPendingSync(s.pending || 0);
      const busy = Boolean(s.busy) || s.status === "queued" || s.status === "running";
      setSyncing(busy);
      if (s.progress) setSyncProgress(s.progress);
      if (s.message) setSyncMsg(s.message);
      else if (s.error) setSyncMsg(s.error);
      return s;
    } catch {
      setPendingSync(0);
      return null;
    }
  }, []);

  const runVaultSync = useCallback(async () => {
    if (syncing) {
      setSyncPopupOpen(true);
      return;
    }
    setSyncPopupOpen(true);
    setSyncing(true);
    setSyncMsg("Vault 동기화를 시작합니다…");
    setSyncProgress({ pct: 0, phase: "queued" });
    try {
      const result = await api.syncVault();
      if (result.status === "error" || result.ok === false) {
        setSyncing(false);
        setSyncMsg(result.message || "Sync 실패");
        await refreshSyncStatus();
        return;
      }
      setSyncMsg(result.message || "Vault 동기화를 백그라운드에서 실행 중입니다.");
      setSyncing(true);
    } catch (e) {
      setSyncing(false);
      setSyncMsg(e instanceof Error ? e.message : "Sync 실패");
    }
  }, [refreshSyncStatus, syncing]);

  // Settings → Sync, once per signed-in user (fresh login and existing session).
  useEffect(() => {
    if (!userId) {
      loginSyncUserRef.current = null;
      return;
    }
    if (!ready || authError) return;
    if (loginSyncUserRef.current === userId) return;
    loginSyncUserRef.current = userId;
    void runVaultSync();
  }, [ready, userId, authError, runVaultSync]);

  useEffect(() => {
    if (!syncing && !syncPopupOpen) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    async function poll() {
      try {
        const next = await api.getSyncStatus();
        if (cancelled) return;
        const busy =
          Boolean(next.busy) ||
          next.status === "queued" ||
          next.status === "running";
        setPendingSync(next.pending || 0);
        setSyncing(busy);
        if (next.progress) setSyncProgress(next.progress);
        if (busy) {
          setSyncMsg(next.message || "Vault 동기화를 진행하고 있습니다…");
          timer = setTimeout(poll, 800);
          return;
        }
        if (next.status === "ready") {
          setSyncMsg(next.message || "동기화가 완료되었습니다.");
          void refreshTree();
          if (activePathRef.current) {
            try {
              const file = await api.readFile(activePathRef.current);
              setDraft(file.content);
              setDirty(false);
            } catch {
              /* remote may have removed the note */
            }
          }
        } else if (next.status === "error") {
          setSyncMsg(next.error || next.message || "동기화에 실패했습니다.");
        }
      } catch {
        if (cancelled) return;
        if (syncing) timer = setTimeout(poll, 2000);
      }
    }

    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [syncing, syncPopupOpen, refreshTree]);

  useEffect(() => {
    if (!settingsOpen) {
      setAppearanceOpen(false);
      setViewOpen(false);
      setLayoutOpen(false);
      setSharePermissionOpen(false);
      setGraphMenuOpen(false);
      setModelMenuOpen(false);
      setSettingsFlyoutPos(null);
      return;
    }
    void refreshSyncStatus();
    void api
      .getSharePermission()
      .then((res) => {
        const p = (res.permission || "one_hop") as SharePermission;
        if (["current", "one_hop", "folder", "vault"].includes(p)) {
          setSharePermission(p);
        }
      })
      .catch(() => {
        /* keep default */
      });

    function updatePos() {
      const btn = settingsBtnRef.current;
      if (!btn) return;
      const rect = btn.getBoundingClientRect();
      setSettingsFlyoutPos({
        left: rect.right + 10,
        bottom: window.innerHeight - rect.bottom,
      });
    }

    updatePos();
    window.addEventListener("resize", updatePos);

    function onPointerDown(e: MouseEvent) {
      const target = e.target as Node;
      if (settingsBtnRef.current?.contains(target)) return;
      if (settingsFlyoutRef.current?.contains(target)) return;
      if ((target as Element).closest?.(".config-popover")) return;
      if (
        (target as Element).closest?.(
          ".knowledge-graph-modal, .notes-configure-modal, .documents-configure-modal, .documents-doc-list-modal, .sync-progress-modal",
        )
      ) {
        return;
      }
      setSettingsOpen(false);
    }

    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setSettingsOpen(false);
    }

    document.addEventListener("mousedown", onPointerDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("resize", updatePos);
      document.removeEventListener("mousedown", onPointerDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [settingsOpen, refreshSyncStatus]);

  const askConfirm = useCallback((options: ConfirmOptions) => {
    if (options.dontAskAgainKey && shouldSkipConfirm(options.dontAskAgainKey)) {
      return Promise.resolve(true);
    }
    return new Promise<boolean>((resolve) => {
      setConfirmState({ options, resolve });
    });
  }, []);

  const showAlert = useCallback(
    (
      message: string,
      title = "Notice",
      link?: { href: string; label: string },
    ) => {
      return new Promise<void>((resolve) => {
        setAlertState({ title, message, link, resolve });
      });
    },
    [],
  );

  const persistNote = useCallback(
    async (path: string, content: string): Promise<{ finalPath: string; content: string }> => {
      const startPath = resolveLatestPath(path);
      let body = content;
      if (!extractH1(body)) {
        const stem = startPath.split("/").pop()?.replace(/\.md$/i, "") || "Untitled";
        body = `# ${stem}\n\n${body.replace(/^\s+/, "")}`;
      }
      const finalPath = await syncFilenameToH1(startPath, body, treeRef.current);
      const safe = sanitizeFilename(extractH1(body) || "Untitled");
      setTabs((prev) =>
        prev.map((t) =>
          t.path === path || t.path === startPath || t.path === finalPath
            ? { ...t, path: finalPath, title: safe }
            : t,
        ),
      );
      await refreshTree();
      return { finalPath, content: body };
    },
    [refreshTree, resolveLatestPath, syncFilenameToH1],
  );

  const openFile = useCallback(
    async (path: string): Promise<boolean> => {
      const seq = ++openSeqRef.current;
      const stale = () => seq !== openSeqRef.current;
      const isMd = /\.md$/i.test(path);
      const isImage = isImageFileName(path);
      const isVideo = isVideoFileName(path);
      // Notes, images, and videos open in the main pane; other binaries stay drag/move-only.
      if (!isMd && !isImage && !isVideo) return false;
      const currentPath = activePathRef.current;
      if (
        currentPath &&
        path !== currentPath &&
        /\.md$/i.test(currentPath) &&
        dirty &&
        draftRef.current !== file?.content
      ) {
        try {
          const { finalPath } = await persistNote(currentPath, draftRef.current);
          if (stale()) return false;
          if (finalPath !== currentPath) {
            setActivePath(finalPath);
            writeLastNotePath(finalPath);
            syncDeepLinkNotePath(finalPath);
          }
        } catch (err) {
          if (tabNavTargetRef.current === path) tabNavTargetRef.current = null;
          void showAlert(err instanceof Error ? err.message : String(err), "Save failed");
          return false;
        }
      }

      if (isImage || isVideo) {
        if (stale()) return false;
        if (tabNavTargetRef.current === path) tabNavTargetRef.current = null;
        setFile(null);
        setDraft("");
        setDirty(false);
        setActivePath(path);
        setTreeFocus("file");
        setTabs((prev) => {
          if (prev.some((t) => t.path === path)) return prev;
          return [
            ...prev,
            {
              path,
              title: path.split("/").pop() || path,
            },
          ];
        });
        if (isNarrow) {
          setPanel("hidden");
        } else {
          setPanel("files");
        }
        setViewMode("preview");
        return true;
      }

      let payload;
      try {
        payload = await api.readFile(path);
        if (stale()) return false;
      } catch (err) {
        if (stale()) return false;
        const status = (err as { status?: number; detail?: unknown } | null)?.status;
        if (status === 404) {
          const body = (err as { detail?: unknown }).detail;
          const inner =
            body && typeof body === "object" && "detail" in (body as object)
              ? (body as { detail: unknown }).detail
              : body;
          const purged =
            Boolean(
              inner &&
                typeof inner === "object" &&
                (inner as { purged?: unknown }).purged,
            );
          await refreshTree();
          if (stale()) return false;
          // Only drop tabs/pins when backend confirmed the note is gone (not a
          // transient S3 download miss while the object still exists remotely).
          if (purged) {
            clearLastNotePath(path);
            setTabs((prev) => prev.filter((t) => t.path !== path));
            updatePinnedPaths(removePinnedPaths(pinnedPaths, path));
            if (activePathRef.current === path) {
              setActivePath(null);
              setFile(null);
              setDraft("");
              setDirty(false);
            }
          } else {
            void showAlert(
              "노트를 아직 로컬에 받지 못했습니다. Sync 후 다시 열어보세요.",
              "Open failed",
            );
          }
          if (tabNavTargetRef.current === path) tabNavTargetRef.current = null;
          return false;
        }
        if (tabNavTargetRef.current === path) tabNavTargetRef.current = null;
        void showAlert(err instanceof Error ? err.message : String(err), "Open failed");
        return false;
      }
      const resolvedPath = payload.path || path;
      if (tabNavTargetRef.current === path || tabNavTargetRef.current === resolvedPath) {
        tabNavTargetRef.current = null;
      }
      setFile(payload);
      setDraft(payload.content);
      setDirty(false);
      setActivePath(resolvedPath);
      activePathRef.current = resolvedPath;
      setTreeFocus("file");
      writeLastNotePath(resolvedPath);
      syncDeepLinkNotePath(resolvedPath);
      setTabs((prev) => {
        const withoutStale =
          resolvedPath === path ? prev : prev.filter((t) => t.path !== path);
        if (withoutStale.some((t) => t.path === resolvedPath)) return withoutStale;
        return [
          ...withoutStale,
          {
            path: resolvedPath,
            title: payload.title || resolvedPath.split("/").pop() || resolvedPath,
          },
        ];
      });
      // Narrow / mobile: dismiss overlay panel so the note view fills the screen.
      if (isNarrow) {
        setPanel("hidden");
      } else {
        setPanel("files");
      }
      setViewMode("preview");
      return true;
    },
    [dirty, file?.content, isNarrow, persistNote, pinnedPaths, refreshTree, showAlert, updatePinnedPaths],
  );

  // Deep link (?note=…) → last note → first markdown. Login gate keeps ?note= until auth succeeds.
  useEffect(() => {
    if (!ready || didRestoreNote.current || authError) return;
    if (activePath) {
      didRestoreNote.current = true;
      syncDeepLinkNotePath(activePath);
      return;
    }

    const openFallback = () => {
      const files = flattenMarkdownPaths(tree);
      if (!files.length) return;
      const last = readLastNotePath();
      const toOpen = last && files.includes(last) ? last : files[0];
      void openFile(toOpen);
    };

    const deep = readDeepLinkNotePath();
    didRestoreNote.current = true;
    if (deep) {
      void (async () => {
        const ok = await openFile(deep);
        if (!ok) {
          void showAlert(`노트를 열 수 없습니다: ${deep}`, "Deep link");
          openFallback();
        }
      })();
      return;
    }
    openFallback();
  }, [ready, tree, activePath, authError, openFile, showAlert]);

  const closeTab = useCallback(
    (path: string) => {
      setTabs((prev) => {
        const next = prev.filter((t) => t.path !== path);
        if (activePath === path) {
          const fallback = next[next.length - 1];
          if (fallback) void openFile(fallback.path);
          else {
            setActivePath(null);
            setFile(null);
            setDraft("");
            setDirty(false);
          }
        }
        return next;
      });
    },
    [activePath, openFile],
  );

  const applyTabSet = useCallback(
    (next: OpenTab[], preferPath?: string | null) => {
      setTabs(next);
      if (!next.length) {
        setActivePath(null);
        setFile(null);
        setDraft("");
        setDirty(false);
        return;
      }
      const stillActive =
        preferPath && next.some((t) => t.path === preferPath)
          ? preferPath
          : activePathRef.current && next.some((t) => t.path === activePathRef.current)
            ? activePathRef.current
            : next[next.length - 1].path;
      if (stillActive !== activePathRef.current) {
        void openFile(stillActive);
      }
    },
    [openFile],
  );

  const onTabMenuAction = useCallback(
    (action: TabMenuAction, path: string) => {
      const idx = tabs.findIndex((t) => t.path === path);
      if (idx < 0) return;
      if (action === "close") {
        closeTab(path);
        return;
      }
      if (action === "close-others") {
        applyTabSet(
          tabs.filter((t) => t.path === path),
          path,
        );
        return;
      }
      if (action === "close-after") {
        applyTabSet(tabs.slice(0, idx + 1), path);
        return;
      }
      if (action === "close-all") {
        applyTabSet([]);
      }
    },
    [applyTabSet, closeTab, tabs],
  );

  const save = useCallback(async () => {
    // Wait out any H1 auto-rename, then use the live path (not a stale closure).
    await renameChainRef.current;
    const path = resolveLatestPath(activePathRef.current || "");
    if (!path || !/\.md$/i.test(path)) return;
    setSaving(true);
    try {
      const { finalPath } = await persistNote(path, draftRef.current);
      activePathRef.current = finalPath;
      if (finalPath !== path) {
        setActivePath(finalPath);
        writeLastNotePath(finalPath);
      }
      const payload = await api.readFile(finalPath);
      setFile(payload);
      setDraft(payload.content);
      setDirty(false);
      setViewMode("preview");
    } catch (err) {
      void showAlert(err instanceof Error ? err.message : String(err), "Save failed");
    } finally {
      setSaving(false);
    }
  }, [persistNote, resolveLatestPath, showAlert]);

  // Live tab label from H1 while editing — filename only changes on Save.
  useEffect(() => {
    if (!activePath || !/\.md$/i.test(activePath)) return;
    const title = extractH1(draft);
    if (!title) return;
    const label = sanitizeFilename(title);
    setTabs((prev) =>
      prev.map((t) => (t.path === activePath ? { ...t, title: label } : t)),
    );
  }, [activePath, draft]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.metaKey || e.ctrlKey) && e.key === "s") {
        e.preventDefault();
        void save();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [save]);

  const exitNoteFullscreen = useCallback(() => {
    setNoteFullscreen(false);
    setFullscreenChrome(false);
  }, []);

  useEffect(() => {
    if (!noteFullscreen) return;
    if (!activePath || !/\.md$/i.test(activePath)) exitNoteFullscreen();
  }, [activePath, exitNoteFullscreen, noteFullscreen]);

  useEffect(() => {
    if (!noteFullscreen) return;
    // One mouse-wheel notch. Key repeat then keeps scrolling the note.
    const WHEEL_STEP_PX = 96;
    function onKey(e: KeyboardEvent) {
      if (e.metaKey || e.ctrlKey || e.altKey || e.isComposing) return;
      if (
        document.querySelector(
          ".modal-backdrop, [role='dialog'], .ctx-menu, .config-popover",
        )
      ) {
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        exitNoteFullscreen();
        return;
      }
      if (e.shiftKey || (e.key !== "ArrowDown" && e.key !== "ArrowUp")) return;
      const scroller = document.querySelector(".app.note-fullscreen .main .content");
      if (!(scroller instanceof HTMLElement)) return;
      e.preventDefault();
      e.stopPropagation();
      scroller.scrollBy({
        top: e.key === "ArrowDown" ? WHEEL_STEP_PX : -WHEEL_STEP_PX,
        behavior: "auto",
      });
    }
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [exitNoteFullscreen, noteFullscreen]);

  useEffect(() => {
    if (!activePath || noteFullscreen) return;
    document
      .querySelector(".tabs .tab.active")
      ?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [activePath, noteFullscreen]);

  useEffect(() => {
    function keepHorizontalCaret(target: EventTarget | null): boolean {
      if (!(target instanceof HTMLElement)) return false;
      if (target.closest(".agent-panel, .sidebar, .modal-card, .config-popover")) return true;
      const field = target.closest("input, textarea, select");
      if (!field) return false;
      if (noteFullscreen && field.closest(".main .content")) return false;
      return true;
    }

    function onKey(e: KeyboardEvent) {
      if (e.metaKey || e.ctrlKey || e.altKey || e.shiftKey || e.isComposing) return;
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      if (
        document.querySelector(
          ".modal-backdrop, [role='dialog'], .ctx-menu, .config-popover",
        )
      ) {
        return;
      }
      if (keepHorizontalCaret(e.target)) return;
      const list = tabsRef.current;
      if (list.length < 2) return;
      const pending = tabNavTargetRef.current;
      const current =
        pending && list.some((t) => t.path === pending) ? pending : activePathRef.current;
      const idx = list.findIndex((t) => t.path === current);
      if (idx < 0) return;
      const nextIdx = idx + (e.key === "ArrowRight" ? 1 : -1);
      e.preventDefault();
      e.stopPropagation();
      if (nextIdx < 0 || nextIdx >= list.length) return;
      const nextPath = list[nextIdx].path;
      tabNavTargetRef.current = nextPath;
      void openFile(nextPath);
    }

    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [noteFullscreen, openFile]);

  useEffect(() => {
    if (panel !== "search") return;
    const q = searchQ.trim();
    if (!q) {
      setHits([]);
      return;
    }
    const t = setTimeout(() => {
      void api.search(q).then((r) => setHits(r.results));
    }, 200);
    return () => clearTimeout(t);
  }, [panel, searchQ]);

  const startNotesSync = useCallback(async (full: boolean) => {
    const label = full ? "Rebuild" : "동기화";
    setNotesSyncTitle(full ? "Notes Rebuild" : "Notes Sync");
    setNotesSyncPopupOpen(true);
    setNotesSyncBusy(true);
    setNotesSyncProgress(null);
    setNotesSyncMsg(
      full ? "Notes 전체 재빌드를 시작합니다…" : "Notes 동기화를 시작합니다…",
    );
    try {
      const result = await api.syncNotesGraph(full);
      if (result.status === "error") {
        setNotesSyncBusy(false);
        setNotesSyncMsg(result.error || `Notes ${label}에 실패했습니다.`);
        return;
      }
      if (result.status === "ready" || result.status === "unchanged") {
        setNotesSyncBusy(false);
        setNotesSyncMsg(
          result.message ||
            (result.status === "unchanged"
              ? "변경된 파일이 없습니다."
              : `Notes ${label}가 완료되었습니다.`),
        );
        return;
      }
      setNotesSyncBusy(true);
      setNotesSyncMsg(
        result.message ||
          (full
            ? "Notes 전체 재빌드를 백그라운드에서 실행 중입니다."
            : "Notes 동기화를 백그라운드에서 실행 중입니다."),
      );
    } catch (err) {
      setNotesSyncBusy(false);
      setNotesSyncMsg(
        err instanceof Error ? err.message : `Notes ${label}에 실패했습니다.`,
      );
    }
  }, []);

  const handleGraphAction = useCallback(
    (choice: string) => {
      setGraphMenuOpen(false);
      if (choice === "Graph") {
        setNotesGraphOpen(true);
        return;
      }
      if (choice === "Configure") {
        setNotesConfigureOpen(true);
        return;
      }
      if (choice === "Sync") {
        void startNotesSync(false);
        return;
      }
      if (choice === "Rebuild") {
        void startNotesSync(true);
      }
    },
    [startNotesSync],
  );

  const startDocumentsSync = useCallback(async () => {
    setDocumentsSyncPopupOpen(true);
    setDocumentsSyncBusy(true);
    setDocumentsSyncMsg("Documents 동기화를 시작합니다…");
    setDocumentsSyncProgress(null);
    try {
      const result = await api.syncDocuments(false, agentModel || undefined);
      if (result.status === "error") {
        setDocumentsSyncBusy(false);
        setDocumentsSyncMsg(result.error || "Documents 동기화에 실패했습니다.");
      } else if (result.status === "unchanged" || result.status === "ready") {
        setDocumentsSyncBusy(false);
        setDocumentsSyncMsg(
          result.message || "Documents가 이미 최신 상태입니다.",
        );
      } else {
        setDocumentsSyncBusy(true);
        setDocumentsSyncMsg(
          result.message || "Documents 동기화를 백그라운드에서 실행 중입니다.",
        );
        if (result.progress) setDocumentsSyncProgress(result.progress);
      }
    } catch (err) {
      setDocumentsSyncBusy(false);
      setDocumentsSyncMsg(
        err instanceof Error ? err.message : "Documents 동기화에 실패했습니다.",
      );
    }
  }, [agentModel]);

  const handleDocumentsAction = useCallback(
    (choice: string) => {
      setDocumentsMenuOpen(false);
      setSettingsOpen(false);
      if (choice === "Configure") {
        setDocumentsConfigureOpen(true);
        return;
      }
      if (choice === "Projects") {
        setDocumentsListKind("project");
        setDocumentsListOpen(true);
        return;
      }
      if (choice === "Drawings") {
        setDocumentsListKind("drawing");
        setDocumentsListOpen(true);
      }
    },
    [],
  );

  useEffect(() => {
    if (!documentsSyncBusy && !documentsSyncPopupOpen) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    async function pollDocumentsSync() {
      try {
        const next = await api.getDocumentsStatus();
        if (cancelled) return;
        const busy = next.status === "queued" || next.status === "running";
        setDocumentsSyncBusy(busy);
        if (next.progress) {
          setDocumentsSyncProgress(next.progress);
        }
        if (busy) {
          setDocumentsSyncMsg(
            next.message || "Documents 동기화를 백그라운드에서 실행 중입니다.",
          );
          timer = setTimeout(pollDocumentsSync, 1500);
          return;
        }
        if (next.status === "ready" || next.status === "unchanged") {
          setDocumentsSyncMsg(
            next.message || "Documents 동기화가 완료되었습니다.",
          );
        } else if (next.status === "idle") {
          setDocumentsSyncMsg(
            next.message || "Documents 동기화가 완료되었습니다.",
          );
        } else if (next.status === "error") {
          setDocumentsSyncMsg(next.error || "Documents 동기화에 실패했습니다.");
        }
      } catch {
        if (cancelled) return;
        if (documentsSyncBusy) {
          timer = setTimeout(pollDocumentsSync, 4000);
        }
      }
    }

    void pollDocumentsSync();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [documentsSyncBusy, documentsSyncPopupOpen]);

  useEffect(() => {
    if (!notesSyncBusy) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    async function pollNotesSync() {
      try {
        const next = await api.getNotesGraphStatus();
        if (cancelled) return;
        const busy = next.status === "queued" || next.status === "running";
        setNotesSyncBusy(busy);
        if (next.progress) {
          setNotesSyncProgress(next.progress);
        }
        if (busy) {
          setNotesSyncMsg(
            next.message || "Notes 동기화를 백그라운드에서 실행 중입니다.",
          );
          timer = setTimeout(pollNotesSync, 1500);
          return;
        }
        if (next.status === "ready" || next.status === "unchanged") {
          setNotesSyncMsg(
            next.message ||
              (next.status === "unchanged"
                ? "변경된 파일이 없습니다."
                : "Notes 동기화가 완료되었습니다."),
          );
        } else if (next.status === "error") {
          setNotesSyncMsg(next.error || "Notes 동기화에 실패했습니다.");
        }
      } catch {
        if (cancelled) return;
        if (notesSyncBusy) {
          timer = setTimeout(pollNotesSync, 4000);
        }
      }
    }

    void pollNotesSync();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [notesSyncBusy]);

  const onWikiClick = useCallback(
    async (target: string) => {
      const flatten = (nodes: TreeNode[]): TreeNode[] =>
        nodes.flatMap((n) => (n.type === "folder" ? flatten(n.children || []) : [n]));
      const all = flatten(treeRef.current);
      const local = resolveWikiTarget(target, all, activePathRef.current);
      const openInAppTab = async (path: string) => {
        // Always open inside the vault window as an editor tab (never window.open).
        await openFile(path);
      };
      if (local) {
        await openInAppTab(local);
        return;
      }
      try {
        const resolved = await api.resolveWikiLink(target, activePathRef.current);
        if (resolved.path) {
          await openInAppTab(resolved.path);
          return;
        }
      } catch {
        /* fall through to alert */
      }
      void showAlert(`노트를 찾을 수 없습니다: ${target}`, "Not found");
    },
    [openFile, showAlert],
  );

  const uploadNoteVideos = useCallback(
    async (videos: File[]): Promise<string> => {
      if (!activePath || !/\.md$/i.test(activePath) || !videos.length) return "";
      const parent = noteParentDir(activePath);
      const taken = new Set(flattenAllPaths(treeRef.current));
      const chunks: string[] = [];
      for (const file of videos) {
        const vaultPath = allocateUploadPath(parent, file.name, taken);
        const fileName = vaultPath.split("/").pop() || file.name;
        await api.uploadFile(vaultPath, file, fileName);
        chunks.push(`![[${fileName}]]`);
      }
      if (!showImages) {
        persistShowImages(true);
        setShowImages(true);
      }
      await refreshTree();
      return chunks.join("\n\n");
    },
    [activePath, refreshTree, showImages],
  );

  const uploadPastedImage = useCallback(
    async (file: File): Promise<string> => {
      if (!activePath || !/\.md$/i.test(activePath)) return "";
      const parent = noteParentDir(activePath);
      const ext = extFromImageMime(file.type || "image/png");
      const vaultPath = uniqueImagePath(parent, ext, treeRef.current);
      const fileName = vaultPath.split("/").pop() || `image.${ext}`;
      await api.uploadFile(vaultPath, file, fileName);
      if (!showImages) {
        persistShowImages(true);
        setShowImages(true);
      }
      await refreshTree();
      return `![image](${fileName})`;
    },
    [activePath, refreshTree, showImages],
  );

  const onEditorPaste = useCallback(
    async (e: ClipboardEvent<HTMLTextAreaElement>) => {
      if (!activePath || pastingImage) return;
      const items = Array.from(e.clipboardData?.items || []);
      const imageItem = items.find((it) => it.type.startsWith("image/"));
      if (!imageItem) return;

      const blob = imageItem.getAsFile();
      if (!blob) return;
      e.preventDefault();

      const ta = e.currentTarget;
      const start = ta.selectionStart;
      const end = ta.selectionEnd;

      setPastingImage(true);
      try {
        const md = await uploadPastedImage(blob);
        const el = editorRef.current;
        if (el && md) insertEditorText(el, start, end, md);
      } catch (err) {
        void showAlert(err instanceof Error ? err.message : String(err), "Image paste failed");
      } finally {
        setPastingImage(false);
      }
    },
    [activePath, pastingImage, showAlert, uploadPastedImage],
  );

  const selectTreeFolder = useCallback((path: string) => {
    setSelectedFolder(path);
    setTreeFocus("folder");
    // Leave the note editor so the next paste targets the file tree, not the note.
    sidebarBodyRef.current?.focus();
  }, []);

  const draftParentPath = useMemo(() => {
    if (selectedFolder) return selectedFolder;
    if (!activePath) return "";
    const parts = activePath.split("/");
    if (parts.length <= 1) return "";
    return parts.slice(0, -1).join("/");
  }, [activePath, selectedFolder]);

  const createNoteIn = useCallback(
    async (parent: string) => {
      await renameChainRef.current;
      const path = uniqueNotePath(parent, treeRef.current);
      // Drop a leftover alias (Untitled.md → previously saved note) so the new
      // file keeps this path instead of saving over that note.
      renamedFromRef.current.delete(path);
      await api.writeFile(path, noteTemplate("Untitled"));
      await refreshTree();
      await openFile(path);
      setViewMode("edit");
    },
    [openFile, refreshTree],
  );

  const createNote = useCallback(async () => {
    await createNoteIn(draftParentPath || "00-Inbox");
  }, [createNoteIn, draftParentPath]);

  const saveMeetingToVault = useCallback(async () => {
    const source = meeting.batchEntries.filter((entry) =>
      String(entry.text || "").trim(),
    );
    if (!source.length) {
      meeting.setStatus("보낼 배치 기록이 없습니다.");
      return;
    }
    meeting.setSavingVault(true);
    try {
      const typed = meeting.title.trim();
      const title =
        typed && typed !== DEFAULT_MEETING_TITLE
          ? typed
          : extractTitleFromEntries(source);
      const baseName = meetingFileBaseName(title, meeting.recordedAt);
      const path = uniqueNamedPath(MEETING_FOLDER, baseName, treeRef.current);
      const md = buildMeetingMarkdown(title, source, meeting.recordedAt);
      await api.writeFile(path, md);
      await refreshTree();
      meeting.setStatus(`노트로 저장했습니다: ${path}`);
      meeting.setCanSaveVault(false);
      setPanel("files");
      await openFile(path);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      meeting.setStatus(`노트 저장 실패: ${msg}`);
      void showAlert(msg, "노트 저장 실패");
    } finally {
      meeting.setSavingVault(false);
    }
  }, [meeting, openFile, refreshTree, showAlert]);

  const startCreateFolder = useCallback(
    (parentPath?: string) => {
      setPanel("files");
      setCtxMenu(null);
      setRenamingPath(null);
      setDraftFolder({ parentPath: parentPath ?? draftParentPath });
    },
    [draftParentPath],
  );

  const confirmCreateFolder = useCallback(
    async (name: string) => {
      const parent = draftFolder?.parentPath ?? "";
      const safe = name.replace(/[\\/]/g, "").trim();
      setDraftFolder(null);
      if (!safe) return;
      const path = parent ? `${parent}/${safe}` : safe;
      try {
        await api.mkdir(path);
        setSelectedFolder(path);
        setTreeFocus("folder");
        await refreshTree();
      } catch (err) {
        void showAlert(err instanceof Error ? err.message : String(err));
      }
    },
    [draftFolder, refreshTree, showAlert],
  );

  const cancelCreateFolder = useCallback(() => {
    setDraftFolder(null);
  }, []);

  const confirmRename = useCallback(
    async (path: string, name: string) => {
      setRenamingPath(null);
      let safe = name.replace(/[\\/]/g, "").trim();
      if (!safe) return;
      const isFile = /\.[a-z0-9]+$/i.test(path.split("/").pop() || "");
      if (isFile && path.toLowerCase().endsWith(".md") && !safe.toLowerCase().endsWith(".md")) {
        safe = `${safe}.md`;
      }
      const parts = path.split("/");
      const parent = parts.slice(0, -1).join("/");
      const to = parent ? `${parent}/${safe}` : safe;
      if (to === path) return;
      const rewritePath = (p: string) =>
        p === path ? to : p.startsWith(path + "/") ? to + p.slice(path.length) : p;
      const rewriteTreeNodes = (nodes: TreeNode[]): TreeNode[] =>
        nodes.map((n) => {
          if (n.path === path) {
            return {
              ...n,
              name: safe,
              path: to,
              children: n.children
                ? n.children.map(function mapChild(c): TreeNode {
                    return {
                      ...c,
                      path: rewritePath(c.path),
                      children: c.children ? c.children.map(mapChild) : c.children,
                    };
                  })
                : n.children,
            };
          }
          if (n.children) {
            return { ...n, children: rewriteTreeNodes(n.children) };
          }
          return n;
        });
      try {
        await api.rename(path, to);
        renamedFromRef.current.set(path, to);
        if (activePathRef.current === path || activePathRef.current?.startsWith(path + "/")) {
          activePathRef.current = rewritePath(activePathRef.current!);
        }
        updatePinnedPaths(rewritePinnedPaths(pinnedPaths, path, to));
        rewriteOpenFolders(path, to);
        if (selectedFolder === path || selectedFolder?.startsWith(path + "/")) {
          setSelectedFolder(rewritePath(selectedFolder!));
        }
        // Optimistic sidebar update so the new name is visible immediately.
        setTree((prev) => rewriteTreeNodes(prev));

        setTabs((prev) =>
          prev.map((t) => {
            const next = rewritePath(t.path);
            if (next === t.path) return t;
            return {
              ...t,
              path: next,
              title: next.split("/").pop()?.replace(/\.md$/i, "") || t.title,
            };
          }),
        );

        if (activePath === path || activePath?.startsWith(path + "/")) {
          const nextActive = rewritePath(activePath!);
          setActivePath(nextActive);
          writeLastNotePath(nextActive);
          if (activePath === path && /\.md$/i.test(nextActive)) {
            try {
              const payload = await api.readFile(nextActive);
              setFile(payload);
              setDraft(payload.content);
              setDirty(false);
            } catch {
              /* ignore */
            }
          } else if (file?.path === path || file?.path.startsWith(path + "/")) {
            setFile((prev) => (prev ? { ...prev, path: rewritePath(prev.path) } : prev));
          }
        }

        await refreshTree();
        // S3 flush may lag; a second refresh catches the settled remote tree.
        window.setTimeout(() => {
          void refreshTree();
        }, 800);
      } catch (err) {
        void showAlert(err instanceof Error ? err.message : String(err));
        await refreshTree();
      }
    },
    [
      activePath,
      file?.path,
      pinnedPaths,
      refreshTree,
      selectedFolder,
      showAlert,
      updatePinnedPaths,
    ],
  );

  const movePath = useCallback(
    async (fromPath: string, toParentPath: string) => {
      const base = fromPath.split("/").pop() || fromPath;
      const to = toParentPath ? `${toParentPath}/${base}` : base;
      if (to === fromPath) return;
      const fromParent = fromPath.includes("/")
        ? fromPath.slice(0, fromPath.lastIndexOf("/"))
        : "";
      if (fromParent === toParentPath) return;
      if (toParentPath === fromPath || toParentPath.startsWith(fromPath + "/")) {
        void showAlert("폴더를 자기 자신이나 하위로 옮길 수 없습니다.", "Move failed");
        return;
      }
      let companionQueued = false;
      try {
        const result = await api.rename(fromPath, to, {
          copyCompanions: isMarkdownNotePath(fromPath),
        });
        companionQueued = result.companion_images === "queued";
      } catch (err) {
        // Duplicate drop handlers can race; if source is already gone, treat as done.
        const status = (err as { status?: number } | null)?.status;
        if (status === 404) {
          await refreshTree();
          return;
        }
        void showAlert(err instanceof Error ? err.message : String(err), "Move failed");
        return;
      }
      try {
        renamedFromRef.current.set(fromPath, to);
        if (activePathRef.current === fromPath) activePathRef.current = to;
        else if (activePathRef.current?.startsWith(fromPath + "/")) {
          activePathRef.current =
            to + activePathRef.current.slice(fromPath.length);
        }
        updatePinnedPaths(rewritePinnedPaths(pinnedPaths, fromPath, to));
        rewriteOpenFolders(fromPath, to);
        const rewrite = (p: string) =>
          p === fromPath ? to : p.startsWith(fromPath + "/") ? to + p.slice(fromPath.length) : p;

        if (selectedFolder === fromPath || selectedFolder?.startsWith(fromPath + "/")) {
          setSelectedFolder(selectedFolder === fromPath ? to : rewrite(selectedFolder!));
        } else if (toParentPath) {
          setSelectedFolder(toParentPath);
        }

        setTabs((prev) =>
          prev.map((t) => {
            const next = rewrite(t.path);
            if (next === t.path) return t;
            return {
              ...t,
              path: next,
              title: next.split("/").pop()?.replace(/\.md$/i, "") || t.title,
            };
          }),
        );

        if (activePath === fromPath || activePath?.startsWith(fromPath + "/")) {
          const nextActive = rewrite(activePath!);
          setActivePath(nextActive);
          if (/\.md$/i.test(nextActive)) {
            writeLastNotePath(nextActive);
          }
          if (activePath === fromPath && /\.md$/i.test(nextActive)) {
            try {
              const payload = await api.readFile(nextActive);
              setFile(payload);
              setDraft(payload.content);
              setDirty(false);
            } catch {
              /* ignore */
            }
          } else if (file?.path === fromPath || file?.path.startsWith(fromPath + "/")) {
            setFile((prev) => (prev ? { ...prev, path: rewrite(prev.path) } : prev));
          }
        }

        // Moving an image or video into the tree — keep Images view on so it stays visible.
        if (isCompanionMediaFileName(fromPath) && !showImages) {
          persistShowImages(true);
          setShowImages(true);
        }

        await refreshTree();
        // S3 / companion-image moves settle asynchronously; refresh again.
        if (
          companionQueued ||
          isCompanionMediaFileName(fromPath) ||
          isCompanionMediaFileName(to)
        ) {
          window.setTimeout(() => {
            void refreshTree();
          }, 1200);
          window.setTimeout(() => {
            void refreshTree();
          }, 4000);
        }
      } catch (err) {
        void showAlert(err instanceof Error ? err.message : String(err), "Move failed");
      }
    },
    [activePath, file?.path, pinnedPaths, refreshTree, selectedFolder, showAlert, showImages, updatePinnedPaths],
  );

  const reorderInFolder = useCallback(
    async (folderPath: string, names: string[]) => {
      // Optimistic local reorder so the tree updates immediately.
      const applyLocal = (nodes: TreeNode[]): TreeNode[] => {
        const rewrite = (list: TreeNode[], parent: string): TreeNode[] => {
          if (parent === folderPath) {
            const byName = new Map(list.map((n) => [n.name, n]));
            const ordered: TreeNode[] = [];
            for (const name of names) {
              const hit = byName.get(name);
              if (hit) {
                ordered.push(hit);
                byName.delete(name);
              }
            }
            for (const n of list) {
              if (byName.has(n.name)) ordered.push(n);
            }
            return ordered.map((n) =>
              n.type === "folder" && n.children
                ? { ...n, children: rewrite(n.children, n.path) }
                : n,
            );
          }
          return list.map((n) =>
            n.type === "folder" && n.children
              ? { ...n, children: rewrite(n.children, n.path) }
              : n,
          );
        };
        return rewrite(nodes, "");
      };
      setTree((prev) => applyLocal(prev));
      try {
        await api.reorderFolder(folderPath, names);
      } catch (err) {
        void showAlert(err instanceof Error ? err.message : String(err), "Reorder failed");
        await refreshTree();
      }
    },
    [refreshTree, showAlert],
  );

  const uploadFilesToFolder = useCallback(
    async (parentPath: string, files: File[]) => {
      const images = files.filter(
        (f) => f.type.startsWith("image/") || isImageFileName(f.name),
      );
      const videos = files.filter((f) => isVideoUpload(f) && !isImageFileName(f.name));
      if (!images.length && !videos.length) {
        void showAlert("이미지 또는 mp4 동영상만 폴더로 끌어다 놓을 수 있습니다.", "Upload");
        return;
      }
      try {
        const taken = new Set(flattenAllPaths(treeRef.current));
        for (const file of images) {
          const ext =
            extFromImageMime(file.type) ||
            (file.name.includes(".") ? file.name.split(".").pop()!.toLowerCase() : "png");
          const vaultPath = uniqueImagePath(parentPath, ext, treeRef.current);
          taken.add(vaultPath);
          const fileName = vaultPath.split("/").pop() || file.name;
          await api.uploadFile(vaultPath, file, fileName);
        }
        for (const file of videos) {
          const vaultPath = allocateUploadPath(parentPath, file.name, taken);
          const fileName = vaultPath.split("/").pop() || file.name;
          await api.uploadFile(vaultPath, file, fileName);
        }
        if (!showImages) {
          persistShowImages(true);
          setShowImages(true);
        }
        await refreshTree();
      } catch (err) {
        void showAlert(err instanceof Error ? err.message : String(err), "Upload failed");
      }
    },
    [refreshTree, showAlert, showImages],
  );

  const pasteMarkdownIntoFolder = useCallback(
    async (parentPath: string, files: File[]) => {
      try {
        const taken = new Set(flattenAllPaths(treeRef.current));
        for (const file of files) {
          const text = await file.text();
          const vaultPath = allocateUploadPath(parentPath, file.name, taken);
          await api.writeFile(vaultPath, text);
          ensureAncestorsOpen(vaultPath);
        }
        if (parentPath) setFolderOpen(parentPath, true);
        await refreshTree();
      } catch (err) {
        void showAlert(err instanceof Error ? err.message : String(err), "Paste failed");
      }
    },
    [refreshTree, showAlert],
  );

  useEffect(() => {
    if (panel !== "files" && panel !== "hidden") return;
    const onPaste = (e: Event) => {
      if (!(e instanceof ClipboardEvent)) return;
      if (document.querySelector(".modal-backdrop")) return;
      const inNoteEditor = e.target === editorRef.current;
      if (isTextEntryTarget(e.target) && !inNoteEditor) return;
      if (!clipboardHasFiles(e.clipboardData)) return;
      const folder =
        treeFocus === "folder" && selectedFolder
          ? selectedFolder
          : activePath && isMarkdownFileName(activePath)
            ? noteParentDir(activePath)
            : null;
      if (folder == null) return;
      const files = markdownFilesFromClipboard(e.clipboardData);
      if (!files.length) {
        if (inNoteEditor) return;
        e.preventDefault();
        e.stopPropagation();
        void showAlert("Markdown 파일만 붙여넣을 수 있습니다.", "Paste");
        return;
      }
      e.preventDefault();
      e.stopPropagation();
      void pasteMarkdownIntoFolder(folder, files);
    };
    window.addEventListener("paste", onPaste);
    return () => window.removeEventListener("paste", onPaste);
  }, [activePath, panel, pasteMarkdownIntoFolder, selectedFolder, showAlert, treeFocus]);

  const insertVideosIntoNote = useCallback(
    async (videos: File[], insertAt: number | null) => {
      if (!videos.length) return;
      try {
        const md = await uploadNoteVideos(videos);
        if (!md) return;
        const el = editorRef.current;
        if (el && viewMode === "edit") {
          const at = insertAt == null ? el.value.length : insertAt;
          const base = el.value;
          const prefix =
            insertAt == null && base && !base.endsWith("\n")
              ? "\n\n"
              : insertAt == null && base
                ? "\n"
                : "";
          const suffix = insertAt == null ? "\n" : "";
          insertEditorText(el, at, at, `${prefix}${md}${suffix}`);
        } else {
          const base = draftRef.current;
          const next =
            insertAt == null
              ? `${base}${base && !base.endsWith("\n") ? "\n\n" : base ? "\n" : ""}${md}\n`
              : `${base.slice(0, insertAt)}${md}${base.slice(insertAt)}`;
          setDraft(next);
        }
        setDirty(true);
        if (insertAt != null && editorRef.current) {
          editorRef.current.style.height = "auto";
          editorRef.current.style.height = `${Math.max(editorRef.current.scrollHeight, 320)}px`;
        }
      } catch (err) {
        void showAlert(err instanceof Error ? err.message : String(err), "Video upload failed");
      }
    },
    [showAlert, uploadNoteVideos, viewMode],
  );

  const onNoteVideoDrop = useCallback(
    (e: DragEvent<HTMLDivElement>) => {
      if (!hasExternalFileDrag(e) || e.defaultPrevented) return;
      e.preventDefault();
      e.stopPropagation();
      const videos = Array.from(e.dataTransfer.files || []).filter(isVideoUpload);
      if (!videos.length) {
        void showAlert("mp4 동영상만 노트에 넣을 수 있습니다.", "Upload");
        return;
      }
      const ta = editorRef.current;
      const at = viewMode === "edit" && ta ? ta.selectionStart : null;
      void insertVideosIntoNote(videos, at);
    },
    [insertVideosIntoNote, showAlert, viewMode],
  );

  const vaultNoteDrag = useCallback((e: DragEvent<HTMLElement>, readPayload: boolean) => {
    const types = Array.from(e.dataTransfer.types || []);
    const active = getActiveVaultDrag();
    if (!types.includes("application/x-ob-note-path") && !active) return null;
    const data = (readPayload ? parseVaultDrag(e) : null) || active;
    if (!data || data.kind !== "file" || !isMarkdownNotePath(data.path)) return null;
    return data;
  }, []);

  const onEditorDragOver = useCallback(
    (e: DragEvent<HTMLElement>) => {
      const ta = editorRef.current;
      if (!ta || !vaultNoteDrag(e, false)) return;
      e.preventDefault();
      e.stopPropagation();
      e.dataTransfer.dropEffect = "copy";
      const index = textareaDropIndex(ta, e.clientX, e.clientY);
      if (document.activeElement !== ta) ta.focus({ preventScroll: true });
      if (ta.selectionStart !== index || ta.selectionEnd !== index) {
        ta.setSelectionRange(index, index);
      }
    },
    [vaultNoteDrag],
  );

  const onEditorDrop = useCallback(
    (e: DragEvent<HTMLElement>) => {
      const ta = editorRef.current;
      const data = vaultNoteDrag(e, true);
      if (!ta || !data) return;
      e.preventDefault();
      e.stopPropagation();
      const link = wikiLinkMarkdown(data.path);
      if (!link) return;
      const start = textareaDropIndex(ta, e.clientX, e.clientY);
      const before = ta.value.slice(0, start);
      const after = ta.value.slice(start);
      let chunk = link;
      const left = before.slice(-1);
      const right = after.slice(0, 1);
      if (left && !/\s/.test(left)) chunk = ` ${chunk}`;
      if (right && !/\s/.test(right)) chunk = `${chunk} `;
      insertEditorText(ta, start, start, chunk);
      ta.style.height = "auto";
      ta.style.height = `${Math.max(ta.scrollHeight, 320)}px`;
    },
    [vaultNoteDrag],
  );

  const onFileMenuAction = useCallback(
    async (action: FileMenuAction, path: string) => {
      if (action === "open-tab") {
        await openFile(path);
        return;
      }
      if (action === "open-agent") {
        setAgentNotePath(path);
        setAgentOpen(true);
        await openFile(path);
        return;
      }
      if (action === "pin") {
        updatePinnedPaths(togglePinnedPath(path, pinnedPaths));
        return;
      }
      if (action === "share") {
        if (!/\.md$/i.test(path)) {
          void showAlert("Markdown 노트만 공개 링크로 공유할 수 있습니다.", "Share");
          return;
        }
        try {
          const res = await api.createShare(path);
          const url =
            (res.url && res.url.startsWith("http")
              ? res.url
              : `${window.location.origin}${res.url_path}`);
          window.open(url, "_blank", "noopener,noreferrer");
        } catch (err) {
          void showAlert(err instanceof Error ? err.message : String(err), "Share failed");
        }
        return;
      }
      if (action === "duplicate") {
        try {
          const res = await api.duplicate(path);
          await refreshTree();
          await openFile(res.to);
        } catch (err) {
          void showAlert(err instanceof Error ? err.message : String(err));
        }
        return;
      }
      if (action === "rename") {
        setDraftFolder(null);
        setRenamingPath(path);
        return;
      }
      if (action === "delete") {
        const label = path.split("/").pop() || path;
        const ok = await askConfirm({
          title: "Delete file",
          message: `Are you sure you want to delete “${label}”?`,
          detail: "This cannot be undone.",
          confirmLabel: "Delete",
          cancelLabel: "Cancel",
          danger: true,
          dontAskAgainKey: "delete-file",
        });
        if (!ok) return;
        try {
          await api.deletePath(path);
          updatePinnedPaths(removePinnedPaths(pinnedPaths, path));
          setTabs((prev) => prev.filter((t) => t.path !== path));
          if (activePath === path) {
            setActivePath(null);
            setFile(null);
            setDraft("");
            setDirty(false);
          }
          removeTreePath(path, false);
          await refreshTreeAfterMutation();
        } catch (err) {
          void showAlert(err instanceof Error ? err.message : String(err));
          await refreshTreeAfterMutation();
        }
      }
    },
    [
      activePath,
      askConfirm,
      openFile,
      pinnedPaths,
      refreshTreeAfterMutation,
      removeTreePath,
      showAlert,
      updatePinnedPaths,
    ],
  );

  const finishCompressJob = useCallback(
    (next: CompressJobStatus) => {
      if (next.status !== "ready" && next.status !== "error") return;
      const key = next.job_id || `${next.status}:${next.path}:${next.url || next.error || ""}`;
      if (compressFinishRef.current === key) {
        compressBusyRef.current = false;
        compressJobRef.current = null;
        setCompressBusy(false);
        setCompressPopupOpen(false);
        return;
      }
      compressFinishRef.current = key;
      compressBusyRef.current = false;
      compressJobRef.current = null;
      setCompressBusy(false);
      setCompressPopupOpen(false);
      setCompressProgress(null);
      if (next.status === "ready") {
        if (next.url) {
          const link = document.createElement("a");
          link.href = next.url;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          document.body.appendChild(link);
          link.click();
          link.remove();
        }
        return;
      }
      void showAlert(next.error || next.message || "압축에 실패했습니다.", "Compress failed");
    },
    [showAlert],
  );

  const startCompress = useCallback(
    async (target: { path?: string; scope: "folder" | "vault" }) => {
      if (compressBusyRef.current) {
        void showAlert(
          "이미 압축이 진행 중입니다. 끝난 뒤에 다시 시도하세요.",
          "Compress",
        );
        return;
      }
      compressBusyRef.current = true;
      compressFinishRef.current = null;
      compressSawActiveRef.current = false;
      compressJobRef.current = null;
      setSettingsOpen(false);
      setAppearanceOpen(false);
      setViewOpen(false);
      setLayoutOpen(false);
      setSharePermissionOpen(false);
      setDocumentsMenuOpen(false);
      setCompressPopupOpen(true);
      setCompressBusy(true);
      setCompressProgress({ phase: "scan" });
      setCompressMsg(
        target.scope === "vault" ? "vault 전체 압축을 준비하는 중…" : "압축을 준비하는 중…",
      );
      try {
        const res =
          target.scope === "vault"
            ? await api.compressVault()
            : await api.compressFolder(target.path || "");
        if (res.already_running) {
          compressBusyRef.current = false;
          compressJobRef.current = null;
          setCompressBusy(false);
          setCompressPopupOpen(false);
          void showAlert(
            res.message || "이미 압축이 진행 중입니다. 끝난 뒤에 다시 시도하세요.",
            "Compress",
          );
          return;
        }
        if (res.job_id && compressFinishRef.current === res.job_id) return;
        compressJobRef.current = res.job_id || null;
        setCompressMsg(res.message || "압축을 진행하고 있습니다…");
        if (res.progress) setCompressProgress(res.progress);
        const busy = res.status === "queued" || res.status === "running";
        compressBusyRef.current = busy;
        setCompressBusy(busy);
        if (!busy) finishCompressJob(res);
      } catch (err) {
        compressBusyRef.current = false;
        compressJobRef.current = null;
        setCompressBusy(false);
        setCompressPopupOpen(false);
        void showAlert(err instanceof Error ? err.message : String(err), "Compress failed");
      }
    },
    [finishCompressJob, showAlert],
  );

  const startClearing = useCallback(async () => {
    setSettingsOpen(false);
    setAppearanceOpen(false);
    setViewOpen(false);
    setLayoutOpen(false);
    setSharePermissionOpen(false);
    setDocumentsMenuOpen(false);
    setClearingOpen(false);
    setClearingPopupOpen(true);
    setClearingBusy(true);
    setClearingMsg("미디어 참조 검사를 시작합니다…");
    setClearingProgress({ pct: 0, phase: "queued" });
    try {
      const result = await api.startClearing();
      if (result.status === "error" || result.ok === false) {
        setClearingBusy(false);
        setClearingMsg(result.message || "검사를 시작하지 못했습니다.");
        return;
      }
      setClearingMsg(result.message || "미디어 참조를 검사하고 있습니다…");
      if (result.progress) setClearingProgress(result.progress);
      setClearingBusy(true);
    } catch (err) {
      setClearingBusy(false);
      setClearingMsg(err instanceof Error ? err.message : "검사를 시작하지 못했습니다.");
    }
  }, []);

  useEffect(() => {
    if (!clearingBusy) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    async function poll() {
      try {
        const next = await api.getClearingStatus();
        if (cancelled) return;
        const busy =
          Boolean(next.busy) || next.status === "queued" || next.status === "running";
        setClearingBusy(busy);
        if (next.progress) setClearingProgress(next.progress);
        if (busy) {
          setClearingMsg(next.message || "미디어 참조를 검사하고 있습니다…");
          timer = setTimeout(poll, 800);
          return;
        }
        if (next.status === "ready" && next.scan) {
          const key = next.job_id || String(next.updated_at || "");
          setClearingMsg(next.message || "검사를 마쳤습니다.");
          if (clearingOpenedRef.current !== key) {
            clearingOpenedRef.current = key;
            setClearingScan(next.scan);
            setClearingPopupOpen(false);
            setClearingOpen(true);
          }
          return;
        }
        if (next.status === "error") {
          setClearingMsg(next.error || next.message || "검사에 실패했습니다.");
        }
      } catch {
        if (cancelled) return;
        timer = setTimeout(poll, 2000);
      }
    }

    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [clearingBusy]);

  const handleClearingDeleted = useCallback(
    (paths: string[]) => {
      if (!paths.length) return;
      const dropped = new Set(paths);
      let pins = pinnedPaths;
      for (const path of paths) {
        pins = removePinnedPaths(pins, path);
        removeTreePath(path, false);
      }
      updatePinnedPaths(pins);
      setTabs((prev) => prev.filter((tab) => !dropped.has(tab.path)));
      if (activePath && dropped.has(activePath)) {
        setActivePath(null);
        setFile(null);
        setDraft("");
        setDirty(false);
      }
      void refreshTreeAfterMutation();
    },
    [activePath, pinnedPaths, refreshTreeAfterMutation, removeTreePath, updatePinnedPaths],
  );

  const onFolderMenuAction = useCallback(
    async (action: FolderMenuAction, path: string) => {
      setSelectedFolder(path);
      setTreeFocus("folder");
      if (action === "new-note") {
        await createNoteIn(path);
        return;
      }
      if (action === "new-folder") {
        startCreateFolder(path);
        return;
      }
      if (action === "pin") {
        updatePinnedPaths(togglePinnedPath(path, pinnedPaths));
        return;
      }
      if (action === "duplicate") {
        try {
          const res = await api.duplicate(path);
          setSelectedFolder(res.to);
          await refreshTree();
        } catch (err) {
          void showAlert(err instanceof Error ? err.message : String(err));
        }
        return;
      }
      if (action === "compress") {
        await startCompress({ path, scope: "folder" });
        return;
      }
      if (action === "share") {
        try {
          const res = await api.createShare(path);
          const url =
            res.url && res.url.startsWith("http")
              ? res.url
              : `${window.location.origin}${res.url_path}`;
          window.open(url, "_blank", "noopener,noreferrer");
        } catch (err) {
          void showAlert(err instanceof Error ? err.message : String(err), "Share failed");
        }
        return;
      }
      if (action === "rename") {
        setDraftFolder(null);
        setRenamingPath(path);
        return;
      }
      if (action === "delete") {
        const label = path.split("/").pop() || path;
        const ok = await askConfirm({
          title: "Delete folder",
          message: `Are you sure you want to delete “${label}”?`,
          detail: "All notes inside this folder will be permanently deleted.",
          confirmLabel: "Delete",
          cancelLabel: "Cancel",
          danger: true,
          dontAskAgainKey: "delete-folder",
        });
        if (!ok) return;
        try {
          await api.deletePath(path);
          updatePinnedPaths(removePinnedPaths(pinnedPaths, path));
          removeOpenFolders(path);
          if (selectedFolder === path || selectedFolder?.startsWith(path + "/")) {
            setSelectedFolder(null);
            setTreeFocus("file");
          }
          if (activePath === path || activePath?.startsWith(path + "/")) {
            setActivePath(null);
            setFile(null);
            setDraft("");
            setDirty(false);
          }
          setTabs((prev) => prev.filter((t) => !t.path.startsWith(path + "/") && t.path !== path));
          removeTreePath(path, true);
          await refreshTreeAfterMutation();
        } catch (err) {
          void showAlert(err instanceof Error ? err.message : String(err));
          await refreshTreeAfterMutation();
        }
      }
    },
    [
      activePath,
      askConfirm,
      createNoteIn,
      pinnedPaths,
      refreshTreeAfterMutation,
      removeTreePath,
      selectedFolder,
      showAlert,
      startCompress,
      startCreateFolder,
      updatePinnedPaths,
    ],
  );

  useEffect(() => {
    if (!compressBusy) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let abort: AbortController | null = null;

    async function pollCompress() {
      abort = new AbortController();
      const timeout = setTimeout(() => abort?.abort(), 10000);
      try {
        const next = await api.getCompressStatus(abort.signal);
        if (cancelled) return;
        if (
          !compressJobRef.current ||
          (next.job_id && next.job_id !== compressJobRef.current)
        ) {
          timer = setTimeout(pollCompress, 400);
          return;
        }
        if (next.status === "idle") {
          if (compressSawActiveRef.current) {
            compressBusyRef.current = false;
            compressJobRef.current = null;
            setCompressBusy(false);
            setCompressPopupOpen(false);
            return;
          }
          timer = setTimeout(pollCompress, 400);
          return;
        }
        compressSawActiveRef.current = true;
        if (next.progress) setCompressProgress(next.progress);
        const busy = next.status === "queued" || next.status === "running";
        if (busy) {
          setCompressBusy(true);
          setCompressMsg(next.message || "압축을 진행하고 있습니다…");
          timer = setTimeout(pollCompress, 800);
          return;
        }
        if (next.message) setCompressMsg(next.message);
        finishCompressJob(next);
      } catch {
        if (cancelled) return;
        if (compressBusy) timer = setTimeout(pollCompress, 2000);
      } finally {
        clearTimeout(timeout);
      }
    }

    void pollCompress();
    return () => {
      cancelled = true;
      abort?.abort();
      if (timer) clearTimeout(timer);
    };
  }, [compressBusy, finishCompressJob]);

  const onPanelMenuAction = useCallback(
    async (action: PanelMenuAction, parentPath: string) => {
      // Empty-area menu always targets vault root (parentPath === "").
      if (action === "new-note") {
        await createNoteIn(parentPath || "00-Inbox");
        return;
      }
      if (action === "new-folder") {
        startCreateFolder(parentPath);
      }
    },
    [createNoteIn, startCreateFolder],
  );

  const openPanelContextMenu = useCallback((x: number, y: number) => {
    setCtxMenu({
      kind: "panel",
      path: "",
      x,
      y,
    });
  }, []);

  const onSidebarResizeStart = useCallback((e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    const startX = e.clientX;
    const startW = sidebarWidthRef.current;
    setSidebarResizing(true);
    document.body.classList.add("is-resizing-sidebar");

    const onMove = (ev: PointerEvent) => {
      const next = clampSidebarWidth(startW + (ev.clientX - startX));
      sidebarWidthRef.current = next;
      setSidebarWidth(next);
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      document.body.classList.remove("is-resizing-sidebar");
      setSidebarResizing(false);
      persistSidebarWidth(sidebarWidthRef.current);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  }, []);

  const onSidebarResizeReset = useCallback(() => {
    setSidebarWidth(SIDEBAR_W_DEFAULT);
    persistSidebarWidth(SIDEBAR_W_DEFAULT);
  }, []);

  const onAgentResizeStart = useCallback((e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    const startX = e.clientX;
    const startW = agentWidthRef.current;
    setAgentResizing(true);
    document.body.classList.add("is-resizing-agent");

    const onMove = (ev: PointerEvent) => {
      // Dragging left edge: moving left grows the panel.
      const next = clampAgentWidth(startW - (ev.clientX - startX));
      agentWidthRef.current = next;
      setAgentWidth(next);
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      document.body.classList.remove("is-resizing-agent");
      setAgentResizing(false);
      persistAgentWidth(agentWidthRef.current);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  }, []);

  const onAgentResizeReset = useCallback(() => {
    setAgentWidth(AGENT_W_DEFAULT);
    persistAgentWidth(AGENT_W_DEFAULT);
  }, []);

  const onAgentNoteUpdated = useCallback(
    (path: string) => {
      if (activePath === path || tabs.some((t) => t.path === path)) {
        void openFile(path);
      }
      void refreshTree();
    },
    [activePath, openFile, refreshTree, tabs],
  );

  const crumbs = useMemo(() => {
    if (!activePath) return [];
    const parts = activePath.split("/");
    if (isCompanionMediaFileName(activePath)) {
      return parts;
    }
    const h1 = extractH1(draft);
    const last = h1
      ? sanitizeFilename(h1)
      : parts[parts.length - 1].replace(/\.md$/i, "");
    return [...parts.slice(0, -1), last];
  }, [activePath, draft]);

  const visibleTree = useMemo(
    () => filterTreeForView(tree, showImages),
    [tree, showImages],
  );

  const pinnedPathSet = useMemo(() => new Set(pinnedPaths), [pinnedPaths]);

  const pinnedNodes = useMemo(() => {
    const existing = new Set(flattenAllPaths(tree));
    const nodes: TreeNode[] = [];
    for (const path of pinnedPaths) {
      if (!existing.has(path)) continue;
      const node = findTreeNode(tree, path);
      if (!node) continue;
      if (node.type === "folder") {
        const filtered = filterTreeForView([node], showImages)[0];
        if (filtered) nodes.push(filtered);
      } else if (showImages || !isCompanionMediaFileName(node.name)) {
        nodes.push(node);
      }
    }
    return nodes;
  }, [pinnedPaths, showImages, tree]);

  // Drop stale pins when files disappear from the vault
  useEffect(() => {
    if (!tree.length) return;
    const existing = new Set(flattenAllPaths(tree));
    const next = pinnedPaths.filter((p) => existing.has(p));
    if (next.length !== pinnedPaths.length) {
      updatePinnedPaths(next);
    }
  }, [tree, pinnedPaths, updatePinnedPaths]);

  if (!ready) {
    return <div className="empty-state">Loading vault…</div>;
  }

  if (authError && !userId) {
    return (
      <GoogleLoginModal
        authMode={publicConfig?.auth_mode === "cognito" ? "cognito" : "google"}
        clientId={publicConfig?.google_client_id || ""}
        localAuthBypass={Boolean(publicConfig?.local_auth_bypass)}
        projectName={publicConfig?.project_name || "OB Note"}
        cognitoAdminUsername={publicConfig?.cognito_admin_username || "admin"}
        error={loginError || (authBusy ? "로그인 중…" : null)}
        onAccessToken={(token) => void handleGoogleAccessToken(token)}
        onCognitoLogin={(u, p) => void handleCognitoLogin(u, p)}
        onLocalUserId={
          publicConfig?.local_auth_bypass
            ? (id) => void handleLocalUserId(id)
            : undefined
        }
      />
    );
  }

  return (
    <div
      className={`app${panel === "hidden" ? " sidebar-collapsed" : " panel-open"}${isNarrow ? " is-narrow" : ""}${layoutMode === "desktop" ? " layout-desktop" : ""}${layoutMode === "mobile" ? " layout-mobile" : ""}${agentOpen ? " agent-open" : ""}${sidebarResizing || agentResizing ? " is-resizing" : ""}${noteFullscreen ? " note-fullscreen" : ""}${noteFullscreen && fullscreenChrome ? " fullscreen-chrome" : ""}`}
      style={{
        ["--sidebar-w" as string]: `${sidebarWidth}px`,
        ["--agent-w" as string]: `${agentWidth}px`,
      }}
    >
      {ctxMenu && (
        <FolderContextMenu
          menu={ctxMenu}
          pinned={pinnedPathSet.has(ctxMenu.path)}
          onFolderAction={(action, path) => void onFolderMenuAction(action, path)}
          onFileAction={(action, path) => void onFileMenuAction(action, path)}
          onPanelAction={(action, path) => void onPanelMenuAction(action, path)}
          onClose={() => setCtxMenu(null)}
        />
      )}
      {tabMenu && (
        <TabContextMenu
          menu={tabMenu}
          disableOthers={tabs.length <= 1}
          disableAfter={
            tabs.findIndex((t) => t.path === tabMenu.path) >= tabs.length - 1
          }
          onAction={onTabMenuAction}
          onClose={() => setTabMenu(null)}
        />
      )}
      <aside className="rail">
        <button
          type="button"
          className={`rail-btn${panel === "files" ? " active" : ""}`}
          data-tooltip="Files"
          aria-label="Files"
          aria-pressed={panel === "files"}
          onClick={() => setPanel((p) => (p === "files" ? "hidden" : "files"))}
        >
          <FilesIcon />
        </button>
        <button
          type="button"
          className={`rail-btn${panel === "search" ? " active" : ""}`}
          data-tooltip="Search"
          aria-label="Search"
          aria-pressed={panel === "search"}
          onClick={() => setPanel((p) => (p === "search" ? "hidden" : "search"))}
        >
          <SearchIcon />
        </button>
        <button
          ref={graphBtnRef}
          type="button"
          className={`rail-btn${graphMenuOpen || notesSyncBusy || notesGraphOpen ? " active" : ""}`}
          data-tooltip={notesSyncMsg ?? "Graph"}
          aria-label="Graph"
          aria-expanded={graphMenuOpen}
          aria-haspopup="dialog"
          onClick={() => {
            setSettingsOpen(false);
            setAppearanceOpen(false);
            setViewOpen(false);
            setLayoutOpen(false);
            setModelMenuOpen(false);
            setGraphMenuOpen((v) => !v);
          }}
        >
          <GraphIcon />
        </button>
        <button
          type="button"
          className={`rail-btn${panel === "meeting" ? " active" : ""}`}
          data-tooltip="Meeting Log"
          aria-label="Meeting Log"
          aria-pressed={panel === "meeting"}
          onClick={() => setPanel((p) => (p === "meeting" ? "hidden" : "meeting"))}
        >
          <MicIcon />
        </button>
        <div className="rail-spacer" />
        <button
          ref={modelBtnRef}
          type="button"
          className={`rail-btn${modelMenuOpen ? " active" : ""}`}
          data-tooltip={agentModel || "Model"}
          aria-label="Model"
          aria-expanded={modelMenuOpen}
          aria-haspopup="dialog"
          onClick={() => {
            setSettingsOpen(false);
            setAppearanceOpen(false);
            setViewOpen(false);
            setLayoutOpen(false);
            setGraphMenuOpen(false);
            setModelMenuOpen((v) => !v);
          }}
        >
          <ModelIcon />
        </button>
        <button
          ref={settingsBtnRef}
          type="button"
          className={`rail-btn${settingsOpen ? " active" : ""}`}
          data-tooltip="Settings"
          aria-label="Settings"
          aria-expanded={settingsOpen}
          onClick={() => {
            setGraphMenuOpen(false);
            setModelMenuOpen(false);
            setSettingsOpen((v) => !v);
          }}
        >
          <SettingsIcon />
        </button>
      </aside>

      {settingsOpen && settingsFlyoutPos && (
        <div
          ref={settingsFlyoutRef}
          className="rail-settings-flyout"
          style={{ left: settingsFlyoutPos.left, bottom: settingsFlyoutPos.bottom }}
        >
          <button
            type="button"
            className={`rail-settings-btn${syncing ? " is-active" : ""}`}
            title={
              pendingSync
                ? `대기 업로드 ${pendingSync}건을 먼저 반영한 뒤 S3에서 가져옵니다`
                : "S3 vault와 동기화 (변경분만)"
            }
            onClick={() => {
              setAppearanceOpen(false);
              setViewOpen(false);
              setLayoutOpen(false);
              setSharePermissionOpen(false);
              setDocumentsMenuOpen(false);
              void runVaultSync();
            }}
          >
            <SyncIcon />
            <span>
              {syncing
                ? "Sync (Syncing…)"
                : pendingSync
                  ? `Sync (${pendingSync} pending)`
                  : "Sync"}
            </span>
          </button>
          <button
            type="button"
            className={`rail-settings-btn${compressListOpen || compressPopupOpen ? " is-active" : ""}`}
            title="압축 목록을 엽니다. 시작하기는 vault 전체를 압축합니다."
            onClick={() => {
              setAppearanceOpen(false);
              setViewOpen(false);
              setLayoutOpen(false);
              setSharePermissionOpen(false);
              setDocumentsMenuOpen(false);
              setSettingsOpen(false);
              setCompressListOpen(true);
            }}
          >
            <ArchiveIcon />
            <span>Compress</span>
          </button>
          <button
            type="button"
            className={`rail-settings-btn${clearingOpen || clearingPopupOpen || clearingBusy || clearingDeletePopupOpen || clearingDeleteBusy ? " is-active" : ""}`}
            title="같은 폴더 마크다운에서 참조되지 않은 미디어를 검사합니다."
            onClick={() => {
              setAppearanceOpen(false);
              setViewOpen(false);
              setLayoutOpen(false);
              setSharePermissionOpen(false);
              setDocumentsMenuOpen(false);
              setSettingsOpen(false);
              if (clearingDeleteBusy) {
                setClearingDeletePopupOpen(false);
                setClearingOpen(true);
                return;
              }
              if (clearingBusy) {
                setClearingPopupOpen(true);
                return;
              }
              void startClearing();
            }}
          >
            <ClearingIcon />
            <span>
              {clearingDeleteBusy
                ? "Clearing (삭제 중…)"
                : clearingBusy
                  ? "Clearing (검사 중…)"
                  : "Clearing"}
            </span>
          </button>
          <button
            ref={documentsBtnRef}
            type="button"
            className={`rail-settings-btn${documentsMenuOpen || documentsSyncBusy || documentsConfigureOpen || documentsListOpen ? " is-active" : ""}`}
            aria-expanded={documentsMenuOpen}
            aria-haspopup="dialog"
            title={documentsSyncMsg ?? "Documents"}
            onClick={() => {
              setAppearanceOpen(false);
              setViewOpen(false);
              setLayoutOpen(false);
              setSharePermissionOpen(false);
              setDocumentsMenuOpen((v) => !v);
            }}
          >
            <DocumentsIcon />
            <span>
              {documentsSyncBusy ? "Documents (Syncing…)" : "Documents"}
            </span>
          </button>
          <button
            type="button"
            className={`rail-settings-btn${sharedListOpen ? " is-active" : ""}`}
            onClick={() => {
              setAppearanceOpen(false);
              setViewOpen(false);
              setLayoutOpen(false);
              setSharePermissionOpen(false);
              setDocumentsMenuOpen(false);
              setSettingsOpen(false);
              setSharedListOpen(true);
            }}
          >
            <ShareListIcon />
            <span>Shared List</span>
          </button>
          <button
            ref={sharePermissionBtnRef}
            type="button"
            className={`rail-settings-btn${sharePermissionOpen ? " is-active" : ""}`}
            aria-expanded={sharePermissionOpen}
            aria-haspopup="dialog"
            title="Folder share wiki link scope"
            onClick={() => {
              setAppearanceOpen(false);
              setViewOpen(false);
              setLayoutOpen(false);
              setDocumentsMenuOpen(false);
              setSharePermissionOpen((v) => !v);
            }}
          >
            <ShareListIcon />
            <span>Share permission ({sharePermissionToLabel(sharePermission)})</span>
          </button>
          <button
            ref={layoutBtnRef}
            type="button"
            className={`rail-settings-btn${layoutOpen ? " is-active" : ""}`}
            aria-expanded={layoutOpen}
            aria-haspopup="dialog"
            title="Auto follows the window width. PC keeps the desktop layout on this browser."
            onClick={() => {
              setAppearanceOpen(false);
              setViewOpen(false);
              setSharePermissionOpen(false);
              setDocumentsMenuOpen(false);
              setLayoutOpen((v) => !v);
            }}
          >
            <ViewIcon />
            <span>Layout ({layoutModeToLabel(layoutMode)})</span>
          </button>
          <button
            ref={viewBtnRef}
            type="button"
            className={`rail-settings-btn${viewOpen ? " is-active" : ""}`}
            aria-expanded={viewOpen}
            aria-haspopup="dialog"
            onClick={() => {
              setAppearanceOpen(false);
              setLayoutOpen(false);
              setSharePermissionOpen(false);
              setDocumentsMenuOpen(false);
              setViewOpen((v) => !v);
            }}
          >
            <ViewIcon />
            <span>View{showImages ? " (Images)" : ""}</span>
          </button>
          <button
            ref={appearanceBtnRef}
            type="button"
            className={`rail-settings-btn${appearanceOpen ? " is-active" : ""}`}
            aria-expanded={appearanceOpen}
            aria-haspopup="dialog"
            onClick={() => {
              setViewOpen(false);
              setLayoutOpen(false);
              setSharePermissionOpen(false);
              setDocumentsMenuOpen(false);
              setAppearanceOpen((v) => !v);
            }}
          >
            <AppearanceIcon />
            <span>Appearance ({themeToLabel(theme)})</span>
          </button>
          <button
            type="button"
            className="rail-settings-btn"
            title="Log out"
            onClick={() => {
              setAppearanceOpen(false);
              setViewOpen(false);
              setLayoutOpen(false);
              setSharePermissionOpen(false);
              void handleLogout();
            }}
          >
            <LogoutIcon />
            <span>Log out</span>
          </button>
        </div>
      )}
      {syncPopupOpen && (
        <SyncProgressModal
          title="Vault Sync"
          busy={syncing}
          message={syncMsg}
          progress={syncProgress}
          onClose={() => setSyncPopupOpen(false)}
        />
      )}
      {notesSyncPopupOpen && (
        <SyncProgressModal
          title={notesSyncTitle}
          busy={notesSyncBusy}
          message={notesSyncMsg}
          progress={notesSyncProgress}
          onClose={() => setNotesSyncPopupOpen(false)}
        />
      )}
      {documentsSyncPopupOpen && (
        <SyncProgressModal
          title="Documents Sync"
          busy={documentsSyncBusy}
          message={documentsSyncMsg}
          progress={documentsSyncProgress}
          onClose={() => setDocumentsSyncPopupOpen(false)}
        />
      )}
      {compressPopupOpen && (
        <SyncProgressModal
          title="Compress"
          busy={compressBusy}
          message={compressMsg}
          progress={compressProgress}
          hint="완료되면 다운로드가 열리고 이 창은 닫힙니다. 닫아도 압축은 계속됩니다."
          onClose={() => setCompressPopupOpen(false)}
        />
      )}
      {clearingPopupOpen && (
        <SyncProgressModal
          title="Clearing"
          busy={clearingBusy}
          message={clearingMsg}
          progress={clearingProgress}
          hint="이 창을 닫아도 검사는 계속됩니다. 끝나면 목록이 열립니다."
          onClose={() => setClearingPopupOpen(false)}
        />
      )}
      {clearingDeletePopupOpen && !clearingOpen && (
        <SyncProgressModal
          title="Clearing 삭제"
          busy={clearingDeleteBusy}
          message={clearingDeleteMsg}
          progress={clearingDeleteProgress}
          hint="이 창을 닫아도 삭제는 계속됩니다. Settings의 Clearing에서 다시 볼 수 있습니다."
          onClose={() => setClearingDeletePopupOpen(false)}
        />
      )}
      <SharedListModal open={sharedListOpen} onClose={() => setSharedListOpen(false)} />
      <CompressListModal
        open={compressListOpen}
        onClose={() => setCompressListOpen(false)}
        onStart={() => {
          setCompressListOpen(false);
          void startCompress({ scope: "vault" });
        }}
      />
      <ClearingListModal
        open={clearingOpen}
        scan={clearingScan}
        onClose={() => setClearingOpen(false)}
        onDeleted={handleClearingDeleted}
        onRescan={() => void startClearing()}
        onDeleteActivity={(info) => {
          setClearingDeleteBusy(info.busy);
          setClearingDeleteMsg(info.message);
          setClearingDeleteProgress(info.progress ?? null);
          if (!info.busy) setClearingDeletePopupOpen(false);
        }}
        onDeleteBackground={() => setClearingDeletePopupOpen(true)}
      />
      {notesGraphOpen && (
        <NotesGraphModal
          onClose={() => setNotesGraphOpen(false)}
          onOpenNote={(path) => void openFile(path)}
        />
      )}
      {notesConfigureOpen && (
        <NotesConfigureModal onClose={() => setNotesConfigureOpen(false)} />
      )}
      {documentsConfigureOpen && (
        <DocumentsConfigureModal
          onClose={() => setDocumentsConfigureOpen(false)}
          onFileUploaded={() => {
            void startDocumentsSync();
          }}
        />
      )}
      {documentsListOpen && (
        <DocumentsListModal
          kind={documentsListKind}
          onClose={() => setDocumentsListOpen(false)}
          onCopied={async (path) => {
            await refreshTree();
            await openFile(path);
          }}
        />
      )}
      {graphMenuOpen && (
        <ConfigDrawer
          title="Graph"
          options={[...GRAPH_OPTIONS]}
          selected={[]}
          mode="single"
          placement="end"
          anchorEl={graphBtnRef.current}
          onChange={(next) => {
            if (next[0]) handleGraphAction(next[0]);
          }}
          onClose={() => setGraphMenuOpen(false)}
        />
      )}
      {documentsMenuOpen && (
        <ConfigDrawer
          title="Documents"
          options={[...DOCUMENTS_OPTIONS]}
          selected={[]}
          mode="single"
          anchorEl={documentsBtnRef.current}
          onChange={(next) => {
            if (next[0]) handleDocumentsAction(next[0]);
          }}
          onClose={() => setDocumentsMenuOpen(false)}
        />
      )}
      {modelMenuOpen && (
        <ConfigDrawer
          title="Model"
          options={agentModels}
          selected={agentModel ? [agentModel] : []}
          mode="single"
          placement="end"
          anchorEl={modelBtnRef.current}
          onChange={(next) => {
            const name = next[0];
            if (!name) return;
            setAgentModel(name);
            setAgentModelState(name);
          }}
          onClose={() => setModelMenuOpen(false)}
        />
      )}
      {appearanceOpen && (
        <ConfigDrawer
          title="Appearance"
          options={[...THEME_OPTIONS]}
          selected={[themeToLabel(theme)]}
          mode="single"
          anchorEl={appearanceBtnRef.current}
          onChange={(next) => {
            if (next[0]) setTheme(labelToTheme(next[0]));
          }}
          onClose={() => setAppearanceOpen(false)}
        />
      )}
      {sharePermissionOpen && (
        <ConfigDrawer
          title="Share permission"
          options={[...SHARE_PERMISSION_OPTIONS]}
          selected={[sharePermissionToLabel(sharePermission)]}
          mode="single"
          anchorEl={sharePermissionBtnRef.current}
          onChange={(next) => {
            const label = next[0];
            if (!label) return;
            const permission = labelToSharePermission(label);
            setSharePermission(permission);
            void api.setSharePermission(permission).catch(() => {
              /* keep optimistic value; next open reloads */
            });
          }}
          onClose={() => setSharePermissionOpen(false)}
        />
      )}
      {layoutOpen && (
        <ConfigDrawer
          title="Layout"
          options={LAYOUT_MENU}
          selected={[layoutModeToLabel(layoutMode)]}
          mode="single"
          anchorEl={layoutBtnRef.current}
          onChange={(next) => {
            const label = next[0];
            if (!label) return;
            const mode = labelToLayoutMode(label);
            persistLayoutMode(mode);
            setLayoutMode(mode);
          }}
          onClose={() => setLayoutOpen(false)}
        />
      )}
      {viewOpen && (
        <ConfigDrawer
          title="View"
          options={[...VIEW_OPTIONS]}
          selected={showImages ? ["Images"] : []}
          mode="multi"
          anchorEl={viewBtnRef.current}
          onChange={(next) => {
            const on = next.includes("Images");
            persistShowImages(on);
            setShowImages(on);
          }}
          onClose={() => setViewOpen(false)}
        />
      )}

      <aside className={`sidebar${panel === "hidden" ? " collapsed" : ""}`}>
        {panel === "files" && (
          <>
            <div className="sidebar-header">
              <div className="sidebar-actions">
                <button
                  type="button"
                  className={`icon-btn${treeRefreshing ? " is-refreshing" : ""}`}
                  data-tooltip="Refresh"
                  aria-label="Refresh"
                  disabled={treeRefreshing}
                  onClick={() => void handleRefreshTree()}
                >
                  <RefreshIcon />
                </button>
                <button
                  type="button"
                  className="icon-btn"
                  data-tooltip="New note"
                  aria-label="New note"
                  onClick={() => void createNote()}
                >
                  <PlusFileIcon />
                </button>
                <button
                  type="button"
                  className={`icon-btn${draftFolder ? " active" : ""}`}
                  data-tooltip="New folder"
                  aria-label="New folder"
                  onClick={() => startCreateFolder()}
                >
                  <PlusFolderIcon />
                </button>
              </div>
            </div>
            <div
              className="sidebar-body"
              ref={sidebarBodyRef}
              tabIndex={-1}
              onContextMenu={(e) => {
                const el = e.target as HTMLElement;
                if (el.closest?.(".tree-item") || el.closest?.(".ctx-menu")) return;
                e.preventDefault();
                openPanelContextMenu(e.clientX, e.clientY);
              }}
              onDragOver={(e) => {
                const el = e.target as HTMLElement;
                if (el.closest?.(".tree-item")) return;
                if (isVaultMoveDrag(e) || hasExternalFileDrag(e)) {
                  e.preventDefault();
                  e.dataTransfer.dropEffect = isVaultMoveDrag(e) ? vaultDropEffect() : "copy";
                }
              }}
              onDrop={(e) => {
                const el = e.target as HTMLElement;
                if (el.closest?.(".tree-item")) return;
                e.preventDefault();
                acceptDrop(
                  e,
                  "",
                  (from, toParent) => void movePath(from, toParent),
                  (parent, files) => void uploadFilesToFolder(parent, files),
                );
              }}
            >
              {pinnedNodes.length > 0 && (
                <div className="tree-section">
                  <div className="section-label">Pinned</div>
                  <FileTree
                    nodes={pinnedNodes}
                    activePath={activePath}
                    selectedFolder={selectedFolder}
                    treeFocus={treeFocus}
                    pinnedPaths={pinnedPathSet}
                    hidePinBadge
                    onOpen={(p) => void openFile(p)}
                    onSelectFolder={selectTreeFolder}
                    onMove={(from, toParent) => void movePath(from, toParent)}
                    onReorder={(folder, names) => void reorderInFolder(folder, names)}
                    onUploadFiles={(parent, files) => void uploadFilesToFolder(parent, files)}
                    onFolderContextMenu={(path, x, y) =>
                      setCtxMenu({ kind: "folder", path, x, y })
                    }
                    onFileContextMenu={(path, x, y) => {
                      setTreeFocus("file");
                      setCtxMenu({ kind: "file", path, x, y });
                    }}
                    onPanelContextMenu={openPanelContextMenu}
                    draftFolder={draftFolder}
                    onDraftConfirm={(name) => void confirmCreateFolder(name)}
                    onDraftCancel={cancelCreateFolder}
                    renamingPath={renamingPath}
                    onRenameConfirm={(path, name) => void confirmRename(path, name)}
                    onRenameCancel={() => setRenamingPath(null)}
                  />
                </div>
              )}
              {pinnedNodes.length > 0 && <div className="section-label">Vaults</div>}
              <FileTree
                nodes={visibleTree}
                activePath={activePath}
                selectedFolder={selectedFolder}
                treeFocus={treeFocus}
                pinnedPaths={pinnedPathSet}
                onOpen={(p) => void openFile(p)}
                onSelectFolder={selectTreeFolder}
                onMove={(from, toParent) => void movePath(from, toParent)}
                onReorder={(folder, names) => void reorderInFolder(folder, names)}
                onUploadFiles={(parent, files) => void uploadFilesToFolder(parent, files)}
                onFolderContextMenu={(path, x, y) =>
                  setCtxMenu({ kind: "folder", path, x, y })
                }
                onFileContextMenu={(path, x, y) => {
                  setTreeFocus("file");
                  setCtxMenu({ kind: "file", path, x, y });
                }}
                onPanelContextMenu={openPanelContextMenu}
                draftFolder={draftFolder}
                onDraftConfirm={(name) => void confirmCreateFolder(name)}
                onDraftCancel={cancelCreateFolder}
                renamingPath={renamingPath}
                onRenameConfirm={(path, name) => void confirmRename(path, name)}
                onRenameCancel={() => setRenamingPath(null)}
              />
            </div>
          </>
        )}
        {panel === "search" && (
          <>
            <div className="sidebar-header">
              <span>Search</span>
            </div>
            <div className="search-box">
              <input
                value={searchQ}
                onChange={(e) => setSearchQ(e.target.value)}
                placeholder="Search vault…"
                autoFocus
              />
            </div>
            <div className="sidebar-body">
              {hits.map((h) => (
                <div key={h.path} className="search-hit" onClick={() => void openFile(h.path)}>
                  <div className="search-hit-title">{h.title}</div>
                  <div className="search-hit-path">{h.path}</div>
                  <div className="search-hit-snippet">{h.snippet}</div>
                </div>
              ))}
            </div>
          </>
        )}
        {panel === "meeting" && (
          <MeetingLogSidebar
            meeting={meeting}
            onClose={() => setPanel("files")}
            onSaveVault={() => void saveMeetingToVault()}
            onClearConfirm={async () =>
              askConfirm({
                title: "기록 지우기",
                message: "실시간·배치 변환 기록을 모두 지울까요?",
                confirmLabel: "지우기",
                danger: true,
              })
            }
          />
        )}
        {panel !== "hidden" && (
          <div
            className={`sidebar-resizer${sidebarResizing ? " is-active" : ""}`}
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize file panel"
            title="Drag to resize · double-click to reset"
            onPointerDown={onSidebarResizeStart}
            onDoubleClick={onSidebarResizeReset}
          />
        )}
      </aside>

      <main className="main">
            <div className="tabs">
              {tabs.map((t) => (
                <div
                  key={t.path}
                  className={`tab${activePath === t.path ? " active" : ""}`}
                  onClick={() => void openFile(t.path)}
                  onContextMenu={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    setCtxMenu(null);
                    setTabMenu({ path: t.path, x: e.clientX, y: e.clientY });
                  }}
                >
                  <span className="tab-title">{t.title}</span>
                  <button
                    type="button"
                    className="tab-close"
                    onClick={(e) => {
                      e.stopPropagation();
                      closeTab(t.path);
                    }}
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>

            {activePath && isCompanionMediaFileName(activePath) ? (
              <>
                <div className="toolbar">
                  <div className="breadcrumb">
                    {crumbs.map((c, i) => (
                      <span key={`${c}-${i}`}>
                        {i > 0 ? " / " : ""}
                        {c}
                      </span>
                    ))}
                  </div>
                  <div className="toolbar-actions">
                    <button
                      type="button"
                      className="icon-btn"
                      data-tooltip="Open agent"
                      aria-label="Open agent"
                      onClick={() => {
                        if (!activePath) return;
                        void onFileMenuAction("open-agent", activePath);
                      }}
                      style={{
                        color:
                          agentOpen && agentNotePath === activePath
                            ? "var(--accent)"
                            : undefined,
                      }}
                    >
                      <AgentIcon />
                    </button>
                  </div>
                </div>
                <div className="content">
                  {isVideoFileName(activePath) ? (
                    <VideoPreview path={activePath} />
                  ) : (
                    <ImagePreview path={activePath} />
                  )}
                </div>
                <div className="status-bar">
                  <span>{isVideoFileName(activePath) ? "Video" : "Image"}</span>
                  <span>{activePath.split("/").pop()}</span>
                </div>
              </>
            ) : activePath && file ? (
              <>
                <div className="toolbar">
                  <div className="breadcrumb">
                    {crumbs.map((c, i) => (
                      <span key={`${c}-${i}`}>
                        {i > 0 ? " / " : ""}
                        {c.replace(/\.md$/i, "")}
                      </span>
                    ))}
                    {dirty ? " *" : ""}
                  </div>
                  <div className="toolbar-actions">
                    <button
                      type="button"
                      className="icon-btn"
                      data-tooltip="Open agent"
                      aria-label="Open agent"
                      onClick={() => {
                        if (!activePath) return;
                        void onFileMenuAction("open-agent", activePath);
                      }}
                      style={{
                        color:
                          agentOpen && agentNotePath === activePath
                            ? "var(--accent)"
                            : undefined,
                      }}
                    >
                      <AgentIcon />
                    </button>
                    <button
                      type="button"
                      className="icon-btn"
                      data-tooltip={noteFullscreen ? "Exit fullscreen" : "Full Screen"}
                      aria-label={noteFullscreen ? "Exit fullscreen" : "Full Screen"}
                      aria-pressed={noteFullscreen}
                      onClick={() => {
                        if (noteFullscreen) {
                          exitNoteFullscreen();
                          return;
                        }
                        setFullscreenChrome(false);
                        setNoteFullscreen(true);
                      }}
                      style={{ color: noteFullscreen ? "var(--accent)" : undefined }}
                    >
                      {noteFullscreen ? <CollapseIcon /> : <ExpandIcon />}
                    </button>
                    <button
                      type="button"
                      className="icon-btn"
                      data-tooltip="Preview"
                      aria-label="Preview"
                      onClick={() => setViewMode("preview")}
                      style={{ color: viewMode === "preview" ? "var(--accent)" : undefined }}
                    >
                      <BookIcon />
                    </button>
                    <button
                      type="button"
                      className="icon-btn"
                      data-tooltip="Edit"
                      aria-label="Edit"
                      onClick={openSourceEditor}
                      style={{ color: viewMode === "edit" ? "var(--accent)" : undefined }}
                    >
                      <EditIcon />
                    </button>
                    <button
                      type="button"
                      className="icon-btn"
                      data-tooltip={saving ? "Saving…" : "Save"}
                      aria-label={saving ? "Saving" : "Save"}
                      aria-disabled={saving || !dirty}
                      onClick={() => {
                        if (saving || !dirty) return;
                        void save();
                      }}
                      style={{
                        opacity: dirty && !saving ? 1 : 0.4,
                        cursor: dirty && !saving ? undefined : "default",
                      }}
                    >
                      <SaveIcon />
                    </button>
                  </div>
                </div>
                <div
                  className="content"
                  onClick={(e) => {
                    if (!noteFullscreen) return;
                    const raw = e.target;
                    const target =
                      raw instanceof HTMLElement
                        ? raw
                        : raw instanceof Node
                          ? raw.parentElement
                          : null;
                    if (!target) return;
                    if (target.closest("a, button, input, textarea, video, select, summary, .toolbar")) {
                      return;
                    }
                    const onBackground =
                      target.classList.contains("content") ||
                      target.classList.contains("editor-pane") ||
                      target.classList.contains("preview-pane") ||
                      target.classList.contains("preview-editor") ||
                      target.classList.contains("ProseMirror");
                    if (!onBackground) return;
                    setFullscreenChrome((visible) => !visible);
                  }}
                  onDragOver={(e) => {
                    if (!hasExternalFileDrag(e)) return;
                    e.preventDefault();
                  }}
                  onDrop={onNoteVideoDrop}
                >
                  {viewMode === "edit" ? (
                    <div
                      className="editor-pane"
                      onDragOver={onEditorDragOver}
                      onDrop={onEditorDrop}
                    >
                      <textarea
                        key={activePath}
                        defaultValue={draft}
                        onCompositionStart={() => {
                          editorComposingRef.current = true;
                        }}
                        onCompositionEnd={(e) => {
                          editorComposingRef.current = false;
                          resizeEditor(e.currentTarget);
                        }}
                        onChange={(e) => {
                          const el = e.target;
                          setDraft(el.value);
                          setDirty(el.value !== file.content);
                          requestAnimationFrame(() => resizeEditor(el));
                        }}
                        onPaste={(e) => void onEditorPaste(e)}
                        onFocus={(e) => resizeEditor(e.target)}
                        ref={bindEditor}
                        spellCheck={false}
                      />
                    </div>
                  ) : (
                    <MarkdownEditor
                      key={activePath}
                      handleRef={markdownEditorRef}
                      content={draft}
                      notePath={activePath}
                      onChange={(markdown) => {
                        setDraft(markdown);
                        setDirty(markdown !== file.content);
                      }}
                      onWikiClick={(t) => void onWikiClick(t)}
                      onUploadImage={async (image) => {
                        if (pastingImage) return "";
                        setPastingImage(true);
                        try {
                          return await uploadPastedImage(image);
                        } catch (err) {
                          void showAlert(
                            err instanceof Error ? err.message : String(err),
                            "Image paste failed",
                          );
                          return "";
                        } finally {
                          setPastingImage(false);
                        }
                      }}
                      onUploadVideos={async (videos) => {
                        try {
                          return await uploadNoteVideos(videos);
                        } catch (err) {
                          void showAlert(
                            err instanceof Error ? err.message : String(err),
                            "Video upload failed",
                          );
                          return "";
                        }
                      }}
                    />
                  )}
                </div>
                <div className="status-bar">
                  <span>{file.backlinks.length} backlinks</span>
                  <span>{file.word_count} words</span>
                  <span>{file.char_count.toLocaleString()} characters</span>
                </div>
              </>
            ) : (
              <div className="empty-state">
                왼쪽에서 노트를 선택하거나 새 노트를 만드세요.
                <br />
                <span style={{ fontSize: 12 }}>Local-first · .md SoT · .vault settings</span>
              </div>
            )}
      </main>

      {agentOpen && (
        <AgentPanel
          notePath={agentNotePath}
          modelName={agentModel}
          onClose={() => {
            setAgentOpen(false);
            setAgentNotePath(null);
          }}
          onNoteUpdated={onAgentNoteUpdated}
          onResizeStart={onAgentResizeStart}
          onResizeReset={onAgentResizeReset}
          resizing={agentResizing}
        />
      )}

      <ConfirmDialog
        open={!!confirmState}
        options={confirmState?.options ?? null}
        onCancel={() => {
          confirmState?.resolve(false);
          setConfirmState(null);
        }}
        onConfirm={(dontAskAgain) => {
          if (dontAskAgain && confirmState?.options.dontAskAgainKey) {
            rememberSkipConfirm(confirmState.options.dontAskAgainKey);
          }
          confirmState?.resolve(true);
          setConfirmState(null);
        }}
      />
      <AlertDialog
        open={!!alertState}
        title={alertState?.title}
        message={alertState?.message ?? ""}
        link={alertState?.link}
        onClose={() => {
          alertState?.resolve();
          setAlertState(null);
        }}
      />
    </div>
  );
}
