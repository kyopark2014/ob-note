const SIDEBAR_W_KEY = "ob-note:sidebar-w";
const RECENT_LIST_OPEN_KEY = "ob-note:recent-list-open";
const PINNED_LIST_OPEN_KEY = "ob-note:pinned-list-open";

export const SIDEBAR_W_DEFAULT = 280;
export const SIDEBAR_W_MIN = 180;
export const SIDEBAR_W_MAX = 560;

function getSectionOpen(key: string, fallback = true): boolean {
  try {
    const raw = localStorage.getItem(key);
    if (raw === "0") return false;
    if (raw === "1") return true;
  } catch {
    /* ignore */
  }
  return fallback;
}

function setSectionOpen(key: string, open: boolean): void {
  try {
    localStorage.setItem(key, open ? "1" : "0");
  } catch {
    /* ignore */
  }
}

export function getRecentListOpen(): boolean {
  return getSectionOpen(RECENT_LIST_OPEN_KEY);
}

export function setRecentListOpen(open: boolean): void {
  setSectionOpen(RECENT_LIST_OPEN_KEY, open);
}

export function getPinnedListOpen(): boolean {
  return getSectionOpen(PINNED_LIST_OPEN_KEY);
}

export function setPinnedListOpen(open: boolean): void {
  setSectionOpen(PINNED_LIST_OPEN_KEY, open);
}

export function getSidebarWidth(): number {
  try {
    const n = Number(localStorage.getItem(SIDEBAR_W_KEY));
    if (Number.isFinite(n) && n >= SIDEBAR_W_MIN && n <= SIDEBAR_W_MAX) {
      return Math.round(n);
    }
  } catch {
    /* ignore */
  }
  return SIDEBAR_W_DEFAULT;
}

export function setSidebarWidth(width: number): void {
  const clamped = Math.min(
    SIDEBAR_W_MAX,
    Math.max(SIDEBAR_W_MIN, Math.round(width)),
  );
  try {
    localStorage.setItem(SIDEBAR_W_KEY, String(clamped));
  } catch {
    /* ignore */
  }
}

export function clampSidebarWidth(width: number): number {
  return Math.min(SIDEBAR_W_MAX, Math.max(SIDEBAR_W_MIN, Math.round(width)));
}
