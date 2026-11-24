import { getPrefs, patchPrefs } from "./uiPrefs";

export type Theme = "light" | "dark";

/** @deprecated Prefer getPrefs().theme — kept for callers that read the raw key. */
export const THEME_STORAGE_KEY = "ob-note-theme";

export function getStoredTheme(): Theme {
  return getPrefs().theme;
}

export function applyTheme(theme: Theme) {
  document.documentElement.setAttribute("data-theme", theme);
  if (getPrefs().theme === theme) return;
  patchPrefs({ theme });
}

export function initTheme() {
  const theme = getStoredTheme();
  document.documentElement.setAttribute("data-theme", theme);
}
