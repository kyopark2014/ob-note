import { getPrefs, patchPrefs } from "./uiPrefs";

export function getPinnedPaths(): string[] {
  return getPrefs().pinnedPaths;
}

export function setPinnedPaths(paths: string[]): void {
  patchPrefs({ pinnedPaths: paths });
}

export function togglePinnedPath(path: string, current: string[]): string[] {
  if (current.includes(path)) {
    return current.filter((p) => p !== path);
  }
  return [...current, path];
}

export function rewritePinnedPaths(paths: string[], from: string, to: string): string[] {
  return paths.map((p) => {
    if (p === from) return to;
    if (p.startsWith(from + "/")) return to + p.slice(from.length);
    return p;
  });
}

export function removePinnedPaths(paths: string[], deleted: string): string[] {
  return paths.filter((p) => p !== deleted && !p.startsWith(deleted + "/"));
}
