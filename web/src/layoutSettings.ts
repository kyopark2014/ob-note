import { getPrefs, patchPrefs } from "./uiPrefs";

export type LayoutMode = "auto" | "desktop" | "mobile";

export const LAYOUT_OPTIONS = ["Auto", "PC", "Mobile"] as const;
export type LayoutOption = (typeof LAYOUT_OPTIONS)[number];

export function getLayoutMode(): LayoutMode {
  return getPrefs().layoutMode;
}

export function setLayoutMode(mode: LayoutMode): void {
  patchPrefs({ layoutMode: mode });
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
