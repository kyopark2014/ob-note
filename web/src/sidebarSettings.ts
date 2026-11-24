import { getPrefs, patchPrefs } from "./uiPrefs";

export const SIDEBAR_W_DEFAULT = 280;
export const SIDEBAR_W_MIN = 180;
export const SIDEBAR_W_MAX = 560;

export function getRecentListOpen(): boolean {
  return getPrefs().recentListOpen;
}

export function setRecentListOpen(open: boolean): void {
  patchPrefs({ recentListOpen: open });
}

export function getPinnedListOpen(): boolean {
  return getPrefs().pinnedListOpen;
}

export function setPinnedListOpen(open: boolean): void {
  patchPrefs({ pinnedListOpen: open });
}

export function getSidebarWidth(): number {
  return getPrefs().sidebarWidth;
}

export function setSidebarWidth(width: number): void {
  patchPrefs({ sidebarWidth: clampSidebarWidth(width) });
}

export function clampSidebarWidth(width: number): number {
  return Math.min(SIDEBAR_W_MAX, Math.max(SIDEBAR_W_MIN, Math.round(width)));
}
