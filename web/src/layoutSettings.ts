const LAYOUT_MODE_KEY = "ob-note:layout-mode";

export type LayoutMode = "auto" | "desktop" | "mobile";

export const LAYOUT_OPTIONS = ["Auto", "PC", "Mobile"] as const;
export type LayoutOption = (typeof LAYOUT_OPTIONS)[number];

export function getLayoutMode(): LayoutMode {
  try {
    const stored = localStorage.getItem(LAYOUT_MODE_KEY);
    if (stored === "desktop" || stored === "mobile" || stored === "auto") return stored;
  } catch {
    /* ignore */
  }
  return "auto";
}

export function setLayoutMode(mode: LayoutMode): void {
  try {
    localStorage.setItem(LAYOUT_MODE_KEY, mode);
  } catch {
    /* ignore */
  }
}

export function layoutModeToLabel(mode: LayoutMode): LayoutOption {
  if (mode === "desktop") return "PC";
  if (mode === "mobile") return "Mobile";
  return "Auto";
}

export function labelToLayoutMode(label: string): LayoutMode {
  if (label === "PC") return "desktop";
  if (label === "Mobile") return "mobile";
  return "auto";
}
