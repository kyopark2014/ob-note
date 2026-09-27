import { useCallback, useEffect, useMemo, useRef, useState, type ClipboardEvent, type PointerEvent as ReactPointerEvent } from "react";
import { api } from "./api";
import { FileTree, acceptDrop, hasExternalFileDrag, isVaultMoveDrag } from "./components/FileTree";
import {
  AlertDialog,
  ConfirmDialog,
  type ConfirmOptions,
} from "./components/ConfirmDialog";
import { ConfigDrawer } from "./components/ConfigDrawer";
import { SyncProgressModal, type SyncProgressInfo } from "./components/SyncProgressModal";
import { SharedListModal } from "./components/SharedListModal";
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
import { MarkdownPreview } from "./components/MarkdownPreview";
import {
  AppearanceIcon,
  AgentIcon,
  BookIcon,
  DocumentsIcon,
  EditIcon,
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
} from "./treeSettings";
import {
  getShowImages,
  isImageFileName,
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
import { resolveWikiTarget } from "./wikiLink";
import type {
  FilePayload,
  OpenTab,
  PanelMode,
  SearchHit,
  TreeNode,
  ViewMode,
} from "./types";

const THEME_OPTIONS = ["Light", "Dark"] as const;
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
      return !isImageFileName(n.name);
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
/** Match CSS mobile overlay layout (Files/Search/Meeting full-bleed). */
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
  const [isNarrow, setIsNarrow] = useState(() =>
    typeof window !== "undefined" ? window.matchMedia(NARROW_LAYOUT_MQ).matches : false,
  );
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
    resolve: () => void;
  } | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [appearanceOpen, setAppearanceOpen] = useState(false);
  const [viewOpen, setViewOpen] = useState(false);
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
    const sync = () => setIsNarrow(mq.matches);
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
  const graphBtnRef = useRef<HTMLButtonElement>(null);
  const modelBtnRef = useRef<HTMLButtonElement>(null);
  const documentsBtnRef = useRef<HTMLButtonElement>(null);
  const settingsFlyoutRef = useRef<HTMLDivElement>(null);
  const editorRef = useRef<HTMLTextAreaElement | null>(null);
  /** Serializes H1↔filename renames so save never races a half-finished rename. */
  const renameChainRef = useRef(Promise.resolve());
  /** Old path → latest path after H1 auto-rename (follows chains). */
  const renamedFromRef = useRef(new Map<string, string>());
  const draftRef = useRef(draft);
  const activePathRef = useRef(activePath);
  const treeRef = useRef(tree);
  const didRestoreNote = useRef(false);
  draftRef.current = draft;
  activePathRef.current = activePath;
  treeRef.current = tree;

  const updatePinnedPaths = useCallback((next: string[]) => {
    setPinnedPathsState(next);
    persistPinnedPaths(next);
  }, []);

  const resolveLatestPath = useCallback((path: string): string => {
    let cur = path;
    const seen = new Set<string>();
    while (renamedFromRef.current.has(cur) && !seen.has(cur)) {
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
        const parts = startPath.split("/");
        const parent = parts.slice(0, -1).join("/");
        const currentStem = parts[parts.length - 1]?.replace(/\.md$/i, "") || "";
        if (safe === currentStem) {
          await api.writeFile(startPath, content);
          return startPath;
        }
        await api.writeFile(startPath, content);
        const dest = uniqueNamedPath(parent, safe, currentTree, startPath);
        if (dest === startPath) return startPath;
        await api.rename(startPath, dest);
        renamedFromRef.current.set(startPath, dest);
        // Sync ref immediately so concurrent save/read sees the new path before React re-renders.
        if (
          activePathRef.current === startPath ||
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

  const showAlert = useCallback((message: string, title = "Notice") => {
    return new Promise<void>((resolve) => {
      setAlertState({ title, message, resolve });
    });
  }, []);

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
      const isMd = /\.md$/i.test(path);
      const isImage = isImageFileName(path);
      // Notes and images open in the main pane; other binaries stay drag/move-only.
      if (!isMd && !isImage) return false;
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
          if (finalPath !== currentPath) {
            setActivePath(finalPath);
            writeLastNotePath(finalPath);
            syncDeepLinkNotePath(finalPath);
          }
        } catch (err) {
          void showAlert(err instanceof Error ? err.message : String(err), "Save failed");
          return false;
        }
      }

      if (isImage) {
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
        if (window.matchMedia(NARROW_LAYOUT_MQ).matches) {
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
      } catch (err) {
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
          return false;
        }
        void showAlert(err instanceof Error ? err.message : String(err), "Open failed");
        return false;
      }
      const resolvedPath = payload.path || path;
      setFile(payload);
      setDraft(payload.content);
      setDirty(false);
      setActivePath(resolvedPath);
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
      if (window.matchMedia(NARROW_LAYOUT_MQ).matches) {
        setPanel("hidden");
      } else {
        setPanel("files");
      }
      setViewMode("preview");
      return true;
    },
    [dirty, file?.content, persistNote, pinnedPaths, refreshTree, showAlert, updatePinnedPaths],
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
      const parent = noteParentDir(activePath);
      const ext = extFromImageMime(blob.type || "image/png");
      const vaultPath = uniqueImagePath(parent, ext, treeRef.current);
      const fileName = vaultPath.split("/").pop() || `image.${ext}`;
      const md = `![image](${fileName})`;

      setPastingImage(true);
      try {
        await api.uploadFile(vaultPath, blob, fileName);
        const next = `${draftRef.current.slice(0, start)}${md}${draftRef.current.slice(end)}`;
        setDraft(next);
        setDirty(true);
        await refreshTree();
        requestAnimationFrame(() => {
          const el = editorRef.current;
          if (!el) return;
          const caret = start + md.length;
          el.focus();
          el.setSelectionRange(caret, caret);
        });
      } catch (err) {
        void showAlert(err instanceof Error ? err.message : String(err), "Image paste failed");
      } finally {
        setPastingImage(false);
      }
    },
    [activePath, pastingImage, refreshTree, showAlert],
  );

  const selectTreeFolder = useCallback((path: string) => {
    setSelectedFolder(path);
    setTreeFocus("folder");
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
      const path = uniqueNotePath(parent, tree);
      await api.writeFile(path, noteTemplate("Untitled"));
      await refreshTree();
      await openFile(path);
      setViewMode("edit");
    },
    [openFile, refreshTree, tree],
  );

  const createNote = useCallback(async () => {
    await createNoteIn(draftParentPath || "00-Inbox");
  }, [createNoteIn, draftParentPath]);

  const saveMeetingToVault = useCallback(async () => {
    const source =
      meeting.batchEntries.length > 0 ? meeting.batchEntries : meeting.entries;
    if (!source.length) {
      meeting.setStatus("저장할 회의 기록이 없습니다.");
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
      meeting.setStatus(`Vault에 저장했습니다: ${path}`);
      meeting.setCanSaveVault(false);
      setPanel("files");
      await openFile(path);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      meeting.setStatus(`Vault 저장 실패: ${msg}`);
      void showAlert(msg, "Vault 저장 실패");
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
        const result = await api.rename(fromPath, to);
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

        // Moving an image into the tree — keep Images view on so it stays visible.
        if (isImageFileName(fromPath) && !showImages) {
          persistShowImages(true);
          setShowImages(true);
        }

        await refreshTree();
        // S3 / companion-image moves settle asynchronously; refresh again.
        if (companionQueued || isImageFileName(fromPath) || isImageFileName(to)) {
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
      if (!images.length) {
        void showAlert("이미지 파일만 폴더로 끌어다 놓을 수 있습니다.", "Upload");
        return;
      }
      try {
        for (const file of images) {
          const ext =
            extFromImageMime(file.type) ||
            (file.name.includes(".") ? file.name.split(".").pop()!.toLowerCase() : "png");
          const vaultPath = uniqueImagePath(parentPath, ext, treeRef.current);
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
      startCreateFolder,
      updatePinnedPaths,
    ],
  );

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
    if (isImageFileName(activePath)) {
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
      } else if (showImages || !isImageFileName(node.name)) {
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
      className={`app${panel === "hidden" ? " sidebar-collapsed" : " panel-open"}${isNarrow ? " is-narrow" : ""}${agentOpen ? " agent-open" : ""}${sidebarResizing || agentResizing ? " is-resizing" : ""}`}
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
            ref={documentsBtnRef}
            type="button"
            className={`rail-settings-btn${documentsMenuOpen || documentsSyncBusy || documentsConfigureOpen || documentsListOpen ? " is-active" : ""}`}
            aria-expanded={documentsMenuOpen}
            aria-haspopup="dialog"
            title={documentsSyncMsg ?? "Documents"}
            onClick={() => {
              setAppearanceOpen(false);
              setViewOpen(false);
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
              setDocumentsMenuOpen(false);
              setSharePermissionOpen((v) => !v);
            }}
          >
            <ShareListIcon />
            <span>Share permission ({sharePermissionToLabel(sharePermission)})</span>
          </button>
          <button
            ref={viewBtnRef}
            type="button"
            className={`rail-settings-btn${viewOpen ? " is-active" : ""}`}
            aria-expanded={viewOpen}
            aria-haspopup="dialog"
            onClick={() => {
              setAppearanceOpen(false);
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
      <SharedListModal open={sharedListOpen} onClose={() => setSharedListOpen(false)} />
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
                  e.dataTransfer.dropEffect = isVaultMoveDrag(e) ? "move" : "copy";
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

            {activePath && isImageFileName(activePath) ? (
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
                      title="Open agent"
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
                  <ImagePreview path={activePath} />
                </div>
                <div className="status-bar">
                  <span>Image</span>
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
                      title="Open agent"
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
                      title="Preview"
                      onClick={() => setViewMode("preview")}
                      style={{ color: viewMode === "preview" ? "var(--accent)" : undefined }}
                    >
                      <BookIcon />
                    </button>
                    <button
                      type="button"
                      className="icon-btn"
                      title="Edit"
                      onClick={() => setViewMode("edit")}
                      style={{ color: viewMode === "edit" ? "var(--accent)" : undefined }}
                    >
                      <EditIcon />
                    </button>
                    <button
                      type="button"
                      className="icon-btn"
                      title="Save"
                      onClick={() => void save()}
                      disabled={saving || !dirty}
                      style={{ opacity: dirty ? 1 : 0.4, fontSize: 11, width: "auto", padding: "0 8px" }}
                    >
                      {saving ? "Saving…" : "Save"}
                    </button>
                  </div>
                </div>
                <div className="content">
                  {viewMode === "edit" ? (
                    <div className="editor-pane">
                      <textarea
                        value={draft}
                        onChange={(e) => {
                          const el = e.target;
                          setDraft(el.value);
                          setDirty(el.value !== file.content);
                          el.style.height = "auto";
                          el.style.height = `${Math.max(el.scrollHeight, 320)}px`;
                        }}
                        onPaste={(e) => void onEditorPaste(e)}
                        onFocus={(e) => {
                          const el = e.target;
                          el.style.height = "auto";
                          el.style.height = `${Math.max(el.scrollHeight, 320)}px`;
                        }}
                        ref={(el) => {
                          editorRef.current = el;
                          if (!el) return;
                          el.style.height = "auto";
                          el.style.height = `${Math.max(el.scrollHeight, 320)}px`;
                        }}
                        spellCheck={false}
                      />
                    </div>
                  ) : (
                    <MarkdownPreview
                      content={draft}
                      notePath={activePath}
                      onWikiClick={(t) => void onWikiClick(t)}
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
        onClose={() => {
          alertState?.resolve();
          setAlertState(null);
        }}
      />
    </div>
  );
}
