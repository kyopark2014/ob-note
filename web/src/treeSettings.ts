import { getPrefs, patchPrefs } from "./uiPrefs";

export function getOpenFolders(): Set<string> {
  return new Set(getPrefs().openFolders);
}

export function setOpenFolders(paths: Set<string> | string[]): void {
  const list = Array.isArray(paths) ? paths : Array.from(paths);
  patchPrefs({ openFolders: list });
}

export function isFolderOpen(path: string): boolean {
  return getOpenFolders().has(path);
}

export function setFolderOpen(path: string, open: boolean): void {
  const next = getOpenFolders();
  if (open) next.add(path);
  else next.delete(path);
  setOpenFolders(next);
}

/** Ensure ancestor folders of a note path are marked open. */
export function ensureAncestorsOpen(notePath: string): void {
  const next = getOpenFolders();
  let changed = false;
  const parts = notePath.split("/");
  for (let i = 1; i < parts.length; i++) {
    const folder = parts.slice(0, i).join("/");
    if (!next.has(folder)) {
      next.add(folder);
      changed = true;
    }
  }
  if (changed) setOpenFolders(next);
}

export function rewriteOpenFolders(from: string, to: string): void {
  const next = new Set<string>();
  let changed = false;
  for (const p of getOpenFolders()) {
    if (p === from) {
      next.add(to);
      changed = true;
    } else if (p.startsWith(from + "/")) {
      next.add(to + p.slice(from.length));
      changed = true;
    } else {
      next.add(p);
    }
  }
  if (changed) setOpenFolders(next);
}

export function removeOpenFolders(deleted: string): void {
  const prev = getOpenFolders();
  const next = new Set(
    [...prev].filter((p) => p !== deleted && !p.startsWith(deleted + "/")),
  );
  if (next.size !== prev.size) setOpenFolders(next);
}
