const TREE_SORT_KEY = "ob-note:tree-sort";

/** File-manager style sort modes for the vault sidebar. */
export type TreeSortMode = "name" | "created" | "modified";

export const TREE_SORT_OPTIONS = [
  "Name",
  "Date created",
  "Date modified",
] as const;

export type TreeSortOption = (typeof TREE_SORT_OPTIONS)[number];

const DEFAULT_MODE: TreeSortMode = "modified";

export function getTreeSortMode(): TreeSortMode {
  try {
    const stored = localStorage.getItem(TREE_SORT_KEY);
    if (stored === "name" || stored === "created" || stored === "modified") {
      return stored;
    }
  } catch {
    /* ignore */
  }
  return DEFAULT_MODE;
}

export function setTreeSortMode(mode: TreeSortMode): void {
  try {
    localStorage.setItem(TREE_SORT_KEY, mode);
  } catch {
    /* ignore */
  }
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
