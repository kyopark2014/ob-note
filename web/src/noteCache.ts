import type { FilePayload } from "./types";

/** In-memory LRU for recently opened notes — makes tab/tree re-opens feel instant. */
const MAX_ENTRIES = 48;

const cache = new Map<string, FilePayload>();

function touch(path: string, payload: FilePayload): void {
  cache.delete(path);
  cache.set(path, payload);
  while (cache.size > MAX_ENTRIES) {
    const oldest = cache.keys().next().value;
    if (oldest === undefined) break;
    cache.delete(oldest);
  }
}

export function getCachedNote(path: string): FilePayload | null {
  const key = path.normalize("NFC");
  const hit = cache.get(key);
  if (!hit) return null;
  // LRU: re-insert as newest
  cache.delete(key);
  cache.set(key, hit);
  return hit;
}

export function setCachedNote(payload: FilePayload): void {
  if (!payload?.path) return;
  touch(payload.path.normalize("NFC"), payload);
}

/** Update cached body after a local save without waiting for another /read. */
export function patchCachedNote(
  path: string,
  content: string,
  extras?: Partial<FilePayload>,
): void {
  const key = path.normalize("NFC");
  const prev = cache.get(key);
  const words = content.split(/\s+/).filter(Boolean).length;
  touch(key, {
    path: key,
    content,
    word_count: extras?.word_count ?? prev?.word_count ?? words,
    char_count: extras?.char_count ?? prev?.char_count ?? content.length,
    backlinks: extras?.backlinks ?? prev?.backlinks ?? [],
    title:
      extras?.title ??
      prev?.title ??
      (key.split("/").pop()?.replace(/\.md$/i, "") || key),
    tags: extras?.tags ?? prev?.tags ?? [],
    note_id: extras?.note_id ?? prev?.note_id,
    size_bytes: extras?.size_bytes ?? prev?.size_bytes ?? content.length,
    created_at: extras?.created_at ?? prev?.created_at ?? null,
    updated_at: extras?.updated_at ?? prev?.updated_at ?? null,
  });
}

export function invalidateCachedNote(path: string): void {
  const key = path.normalize("NFC");
  cache.delete(key);
  // Also drop anything nested under a deleted folder prefix.
  for (const p of [...cache.keys()]) {
    if (p === key || p.startsWith(key + "/")) cache.delete(p);
  }
}

export function rewriteCachedNotePath(from: string, to: string): void {
  const src = from.normalize("NFC");
  const dst = to.normalize("NFC");
  if (src === dst) return;
  const hit = cache.get(src);
  if (hit) {
    cache.delete(src);
    touch(dst, { ...hit, path: dst });
  }
  for (const [p, payload] of [...cache.entries()]) {
    if (p.startsWith(src + "/")) {
      cache.delete(p);
      const next = dst + p.slice(src.length);
      touch(next, { ...payload, path: next });
    }
  }
}

export function clearNoteCache(): void {
  cache.clear();
}
