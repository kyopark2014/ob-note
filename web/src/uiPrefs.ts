/**
 * Account-scoped UI preferences.
 *
 * localStorage holds a cache for instant paint; after login we hydrate from
 * GET /api/prefs (DynamoDB META#ui) and debounce PUT on changes.
 * Meeting-log drafts stay out of this blob.
 */

import type { LayoutMode } from "./layoutSettings";
import type { TreeSortMode } from "./treeSortSettings";
import type { Theme } from "./theme";

/** Defaults duplicated here to avoid circular imports with *Settings modules. */
const DEFAULT_AGENT_MODEL = "Claude 5.0 Sonnet";
const SIDEBAR_W_DEFAULT = 280;
const SIDEBAR_W_MIN = 180;
const SIDEBAR_W_MAX = 560;
const AGENT_W_DEFAULT = 360;
const AGENT_W_MIN = 280;
const AGENT_W_MAX = 640;

export type UiPrefs = {
  pinnedPaths: string[];
  treeSort: TreeSortMode;
  theme: Theme;
  layoutMode: LayoutMode;
  showImages: boolean;
  agentModel: string;
  lastNotePath: string | null;
  openFolders: string[];
  sidebarWidth: number;
  agentWidth: number;
  pinnedListOpen: boolean;
  recentListOpen: boolean;
  skipConfirm: Record<string, boolean>;
  updated_at?: string;
};

const PREFS_KEY = "ob-note:ui-prefs";
const SAVE_DEBOUNCE_MS = 400;

const LEGACY = {
  pinned: "ob-note:pinned-paths",
  treeSort: "ob-note:tree-sort",
  theme: "ob-note-theme",
  layout: "ob-note:layout-mode",
  showImages: "ob-note:view-show-images",
  agentModel: "ob-note:agent-model",
  lastNote: "ob-note:last-note-path",
  openFolders: "ob-note:open-folders",
  sidebarW: "ob-note:sidebar-w",
  agentW: "ob-note:agent-w",
  pinnedListOpen: "ob-note:pinned-list-open",
  recentListOpen: "ob-note:recent-list-open",
  skipPrefix: "ob-note:skip-confirm:",
} as const;

export const DEFAULT_PREFS: UiPrefs = {
  pinnedPaths: [],
  treeSort: "modified",
  theme: "dark",
  layoutMode: "auto",
  showImages: false,
  agentModel: DEFAULT_AGENT_MODEL,
  lastNotePath: null,
  openFolders: [],
  sidebarWidth: SIDEBAR_W_DEFAULT,
  agentWidth: AGENT_W_DEFAULT,
  pinnedListOpen: true,
  recentListOpen: true,
  skipConfirm: {},
};

type PrefsListener = (prefs: UiPrefs) => void;

let memory: UiPrefs | null = null;
let saveTimer: ReturnType<typeof setTimeout> | null = null;
let saveInFlight: Promise<void> | null = null;
let remoteSyncEnabled = false;
const listeners = new Set<PrefsListener>();

function lsGet(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function lsSet(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* ignore */
  }
}

function lsRemove(key: string): void {
  try {
    localStorage.removeItem(key);
  } catch {
    /* ignore */
  }
}

function parseJson<T>(raw: string | null, fallback: T): T {
  if (!raw) return fallback;
  try {
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

function clampInt(n: unknown, min: number, max: number, fallback: number): number {
  const v = typeof n === "number" ? n : Number(n);
  if (!Number.isFinite(v)) return fallback;
  return Math.min(max, Math.max(min, Math.round(v)));
}

function asStringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((p): p is string => typeof p === "string" && p.length > 0);
}

function asSkipMap(value: unknown): Record<string, boolean> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  const out: Record<string, boolean> = {};
  for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
    if (v === true || v === "1" || v === 1) out[k] = true;
  }
  return out;
}

function normalizePrefs(raw: Partial<UiPrefs> | Record<string, unknown> | null | undefined): UiPrefs {
  const src = (raw || {}) as Partial<UiPrefs>;
  const treeSort =
    src.treeSort === "name" || src.treeSort === "created" || src.treeSort === "modified"
      ? src.treeSort
      : DEFAULT_PREFS.treeSort;
  const theme = src.theme === "light" || src.theme === "dark" ? src.theme : DEFAULT_PREFS.theme;
  const layoutMode =
    src.layoutMode === "desktop" || src.layoutMode === "mobile" || src.layoutMode === "auto"
      ? src.layoutMode
      : DEFAULT_PREFS.layoutMode;
  const agentModel =
    typeof src.agentModel === "string" && src.agentModel.trim()
      ? src.agentModel.trim()
      : DEFAULT_PREFS.agentModel;
  const lastNotePath =
    typeof src.lastNotePath === "string" && src.lastNotePath.length > 0
      ? src.lastNotePath
      : null;

  return {
    pinnedPaths: asStringList(src.pinnedPaths),
    treeSort,
    theme,
    layoutMode,
    showImages: src.showImages === true,
    agentModel,
    lastNotePath,
    openFolders: asStringList(src.openFolders),
    sidebarWidth: clampInt(
      src.sidebarWidth,
      SIDEBAR_W_MIN,
      SIDEBAR_W_MAX,
      SIDEBAR_W_DEFAULT,
    ),
    agentWidth: clampInt(src.agentWidth, AGENT_W_MIN, AGENT_W_MAX, AGENT_W_DEFAULT),
    pinnedListOpen: src.pinnedListOpen !== false,
    recentListOpen: src.recentListOpen !== false,
    skipConfirm: asSkipMap(src.skipConfirm),
    updated_at: typeof src.updated_at === "string" ? src.updated_at : undefined,
  };
}

function readLegacySkipConfirm(): Record<string, boolean> {
  const out: Record<string, boolean> = {};
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (!key || !key.startsWith(LEGACY.skipPrefix)) continue;
      if (localStorage.getItem(key) === "1") {
        out[key.slice(LEGACY.skipPrefix.length)] = true;
      }
    }
  } catch {
    /* ignore */
  }
  return out;
}

function migrateFromLegacy(): UiPrefs {
  const pinned = asStringList(parseJson(lsGet(LEGACY.pinned), []));
  const treeRaw = lsGet(LEGACY.treeSort);
  const themeRaw = lsGet(LEGACY.theme);
  const layoutRaw = lsGet(LEGACY.layout);
  const openFolders = asStringList(parseJson(lsGet(LEGACY.openFolders), []));
  return normalizePrefs({
    pinnedPaths: pinned,
    treeSort: treeRaw as TreeSortMode,
    theme: themeRaw as Theme,
    layoutMode: layoutRaw as LayoutMode,
    showImages: lsGet(LEGACY.showImages) === "1",
    agentModel: lsGet(LEGACY.agentModel) || DEFAULT_AGENT_MODEL,
    lastNotePath: lsGet(LEGACY.lastNote),
    openFolders,
    sidebarWidth: Number(lsGet(LEGACY.sidebarW)),
    agentWidth: Number(lsGet(LEGACY.agentW)),
    pinnedListOpen: lsGet(LEGACY.pinnedListOpen) !== "0",
    recentListOpen: lsGet(LEGACY.recentListOpen) !== "0",
    skipConfirm: readLegacySkipConfirm(),
  });
}

function writeLocal(prefs: UiPrefs): void {
  lsSet(PREFS_KEY, JSON.stringify(prefs));
  // Keep a few legacy keys in sync so older code paths / tabs stay coherent.
  lsSet(LEGACY.pinned, JSON.stringify(prefs.pinnedPaths));
  lsSet(LEGACY.treeSort, prefs.treeSort);
  lsSet(LEGACY.theme, prefs.theme);
  lsSet(LEGACY.layout, prefs.layoutMode);
  lsSet(LEGACY.showImages, prefs.showImages ? "1" : "0");
  lsSet(LEGACY.agentModel, prefs.agentModel);
  if (prefs.lastNotePath) lsSet(LEGACY.lastNote, prefs.lastNotePath);
  else lsRemove(LEGACY.lastNote);
  lsSet(LEGACY.openFolders, JSON.stringify(prefs.openFolders));
  lsSet(LEGACY.sidebarW, String(prefs.sidebarWidth));
  lsSet(LEGACY.agentW, String(prefs.agentWidth));
  lsSet(LEGACY.pinnedListOpen, prefs.pinnedListOpen ? "1" : "0");
  lsSet(LEGACY.recentListOpen, prefs.recentListOpen ? "1" : "0");
  for (const [key, on] of Object.entries(prefs.skipConfirm)) {
    if (on) lsSet(`${LEGACY.skipPrefix}${key}`, "1");
    else lsRemove(`${LEGACY.skipPrefix}${key}`);
  }
}

function notify(prefs: UiPrefs): void {
  for (const fn of listeners) {
    try {
      fn(prefs);
    } catch {
      /* ignore listener errors */
    }
  }
}

function prefsHaveUserData(prefs: UiPrefs): boolean {
  return (
    prefs.pinnedPaths.length > 0 ||
    prefs.openFolders.length > 0 ||
    prefs.lastNotePath != null ||
    Object.keys(prefs.skipConfirm).length > 0 ||
    prefs.treeSort !== DEFAULT_PREFS.treeSort ||
    prefs.theme !== DEFAULT_PREFS.theme ||
    prefs.layoutMode !== DEFAULT_PREFS.layoutMode ||
    prefs.showImages !== DEFAULT_PREFS.showImages ||
    prefs.agentModel !== DEFAULT_PREFS.agentModel ||
    prefs.sidebarWidth !== DEFAULT_PREFS.sidebarWidth ||
    prefs.agentWidth !== DEFAULT_PREFS.agentWidth ||
    prefs.pinnedListOpen !== DEFAULT_PREFS.pinnedListOpen ||
    prefs.recentListOpen !== DEFAULT_PREFS.recentListOpen ||
    !!prefs.updated_at
  );
}

export function getPrefs(): UiPrefs {
  if (memory) return memory;
  const raw = lsGet(PREFS_KEY);
  if (raw) {
    memory = normalizePrefs(parseJson(raw, {}));
    return memory;
  }
  memory = migrateFromLegacy();
  writeLocal(memory);
  return memory;
}

export function subscribePrefs(listener: PrefsListener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function patchPrefs(partial: Partial<UiPrefs>): UiPrefs {
  const next = normalizePrefs({ ...getPrefs(), ...partial });
  memory = next;
  writeLocal(next);
  notify(next);
  scheduleRemoteSave();
  return next;
}

export function replacePrefs(raw: Partial<UiPrefs> | Record<string, unknown>): UiPrefs {
  const next = normalizePrefs(raw);
  memory = next;
  writeLocal(next);
  notify(next);
  return next;
}

export function setRemoteSyncEnabled(enabled: boolean): void {
  remoteSyncEnabled = enabled;
  if (!enabled && saveTimer) {
    clearTimeout(saveTimer);
    saveTimer = null;
  }
}

function scheduleRemoteSave(): void {
  if (!remoteSyncEnabled) return;
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    saveTimer = null;
    void flushRemoteSave();
  }, SAVE_DEBOUNCE_MS);
}

export async function flushRemoteSave(): Promise<void> {
  if (!remoteSyncEnabled) return;
  if (saveInFlight) {
    await saveInFlight;
  }
  const snapshot = getPrefs();
  saveInFlight = (async () => {
    try {
      const { api } = await import("./api");
      await api.putPrefs(snapshot);
    } catch {
      /* keep local cache; next change retries */
    } finally {
      saveInFlight = null;
    }
  })();
  await saveInFlight;
}

/** After login: server wins when it has data; otherwise upload local cache. */
export async function syncPrefsWithServer(): Promise<UiPrefs> {
  const local = getPrefs();
  remoteSyncEnabled = true;
  try {
    const { api } = await import("./api");
    const remote = normalizePrefs(await api.getPrefs());
    if (prefsHaveUserData(remote) && remote.updated_at) {
      return replacePrefs(remote);
    }
    if (prefsHaveUserData(remote) && !prefsHaveUserData(local)) {
      return replacePrefs(remote);
    }
    if (prefsHaveUserData(local)) {
      const saved = await api.putPrefs(local);
      if (saved?.prefs) return replacePrefs(saved.prefs);
      return local;
    }
    if (prefsHaveUserData(remote)) {
      return replacePrefs(remote);
    }
    return local;
  } catch {
    return local;
  }
}

export function shouldSkipConfirm(key: string): boolean {
  return getPrefs().skipConfirm[key] === true;
}

export function rememberSkipConfirm(key: string): void {
  patchPrefs({ skipConfirm: { ...getPrefs().skipConfirm, [key]: true } });
}
