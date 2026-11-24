import { getPrefs, patchPrefs } from "./uiPrefs";

/** File-manager style sort modes for the vault sidebar. */
export type TreeSortMode = "name" | "created" | "modified";

export const TREE_SORT_OPTIONS = [
  "Name",
  "Date created",
  "Date modified",
] as const;

export type TreeSortOption = (typeof TREE_SORT_OPTIONS)[number];

export function getTreeSortMode(): TreeSortMode {
  return getPrefs().treeSort;
}

export function setTreeSortMode(mode: TreeSortMode): void {
  patchPrefs({ treeSort: mode });
}

export function treeSortModeToLabel(mode: TreeSortMode): TreeSortOption {
  if (mode === "name") return "Name";
  if (mode === "created") return "Date created";
  return "Date modified";
}

export function labelToTreeSortMode(label: string): TreeSortMode {
  if (label === "Name") return "name";
  if (label === "Date created") return "created";
  return "modified";
}
