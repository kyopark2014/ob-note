/** Obsidian-like wiki link resolution against the in-memory file tree. */

export type WikiFile = { path: string; name: string };

export function normWikiKey(name: string): string {
  let s = name.normalize("NFC").trim().replace(/\\/g, "/");
  if (s.toLowerCase().endsWith(".md")) s = s.slice(0, -3);
  return s.toLowerCase();
}

/** Obsidian wiki link for a vault markdown path. Folder paths keep a readable title. */
export function wikiLinkMarkdown(path: string): string {
  const normalized = path.replace(/\\/g, "/").replace(/^\/+/, "");
  const base = normalized.split("/").pop() || normalized;
  const stem = base.replace(/\.(md|markdown)$/i, "");
  const target = normalized.replace(/\.(md|markdown)$/i, "");
  const clean = (s: string) => s.replace(/[|\]]/g, "").trim();
  const label = clean(stem);
  const dest = clean(target);
  if (!label) return "";
  if (dest && dest !== label) return `[[${dest}|${label}]]`;
  return `[[${label}]]`;
}

export function noteParentDir(path: string): string {
  const i = path.replace(/\\/g, "/").lastIndexOf("/");
  return i >= 0 ? path.slice(0, i) : "";
}

function joinVaultPath(parent: string, rel: string): string {
  const parts = [
    ...(parent ? parent.replace(/\\/g, "/").split("/") : []),
    ...rel.normalize("NFC").replace(/\\/g, "/").split("/"),
  ];
  const out: string[] = [];
  for (const part of parts) {
    if (!part || part === ".") continue;
    if (part === "..") {
      out.pop();
      continue;
    }
    out.push(part);
  }
  return out.join("/");
}

function pathKey(path: string): string {
  return normWikiKey(path.replace(/\\/g, "/"));
}

function parentKey(path: string): string {
  return noteParentDir(path).normalize("NFC");
}

function preferSameFolder(hits: WikiFile[], fromPath?: string | null): WikiFile | null {
  if (!hits.length) return null;
  if (hits.length === 1 || !fromPath) return hits[0];
  const parent = parentKey(fromPath);
  const same = hits.filter((f) => parentKey(f.path) === parent);
  return same[0] || hits[0];
}

const PREFIX_BOUNDARY = new Set([
  " ",
  "\t",
  "(",
  "-",
  "—",
  "–",
  ":",
  "|",
  "/",
  "[",
  "]",
  "·",
  "•",
]);

function isPrefixTitleMatch(name: string, key: string): boolean {
  if (!key || !name || name === key) return false;
  if (!name.startsWith(key)) return false;
  return PREFIX_BOUNDARY.has(name[key.length] || "");
}

function isNumberedCopyStem(stem: string): boolean {
  return / \d+$/.test(stem || "");
}

function fileStem(file: WikiFile): string {
  const base = file.name || file.path.split("/").pop() || "";
  return base.toLowerCase().endsWith(".md") ? base.slice(0, -3) : base;
}

function resolvePrefixMatch(
  key: string,
  mdFiles: WikiFile[],
  fromPath?: string | null,
): string | null {
  if (!key) return null;
  let hits = mdFiles.filter((f) => {
    const stem = normWikiKey(fileStem(f));
    return isPrefixTitleMatch(stem, key);
  });
  if (!hits.length) return null;
  if (fromPath) {
    const parent = parentKey(fromPath);
    const same = hits.filter((f) => parentKey(f.path) === parent);
    if (same.length) hits = same;
  }
  if (hits.length === 1) return hits[0].path;
  const nonNumbered = hits.filter((f) => !isNumberedCopyStem(fileStem(f)));
  if (nonNumbered.length === 1) return nonNumbered[0].path;
  if (nonNumbered.length > 1) return null;
  hits = [...hits].sort((a, b) => {
    const sa = fileStem(a);
    const sb = fileStem(b);
    const na = /(\d+)$/.exec(sa)?.[1];
    const nb = /(\d+)$/.exec(sb)?.[1];
    const ia = na ? Number(na) : 1e9;
    const ib = nb ? Number(nb) : 1e9;
    if (ia !== ib) return ia - ib;
    if (sa.length !== sb.length) return sa.length - sb.length;
    return a.path.localeCompare(b.path);
  });
  return hits[0]?.path ?? null;
}

/**
 * Resolve [[target]] to a vault-relative markdown path.
 * Mirrors application.vault_index.resolve_link for the client tree.
 */
export function resolveWikiTarget(
  target: string,
  files: WikiFile[],
  fromPath?: string | null,
): string | null {
  const raw = target.normalize("NFC").trim().replace(/\\/g, "/");
  if (!raw) return null;
  const needle = normWikiKey(raw);
  const basename = normWikiKey(raw.split("/").pop() || raw);
  const mdFiles = files.filter(
    (f) => f.path.toLowerCase().endsWith(".md") || f.name.toLowerCase().endsWith(".md"),
  );

  const exactPath = (want: string): WikiFile | undefined => {
    const key = normWikiKey(want);
    return mdFiles.find((f) => pathKey(f.path) === key);
  };

  // 1. Vault-absolute path
  let hit = exactPath(raw);
  if (hit) return hit.path;

  // 2. Relative to current note folder
  if (fromPath) {
    const joined = joinVaultPath(noteParentDir(fromPath), raw);
    hit = exactPath(joined);
    if (hit) return hit.path;
  }

  // 3. Path suffix match
  if (needle.includes("/")) {
    const suffixHits = mdFiles.filter((f) => {
      const pk = pathKey(f.path);
      return pk === needle || pk.endsWith("/" + needle);
    });
    const preferred = preferSameFolder(suffixHits, fromPath);
    if (preferred) return preferred.path;
  }

  // 4. Basename (same folder first), including last segment of path-style links
  const stemHits = mdFiles.filter((f) => normWikiKey(f.name) === basename);
  if (stemHits.length) {
    if (fromPath) {
      const parent = parentKey(fromPath);
      const sameFolder = stemHits.filter((f) => parentKey(f.path) === parent);
      if (sameFolder.length) return sameFolder[0].path;
    }
    if (stemHits.length === 1 || !needle.includes("/")) {
      return preferSameFolder(stemHits, fromPath)!.path;
    }
  }

  // 5. Unique title prefix (short wiki → longer note title)
  for (const key of needle === basename ? [needle] : [needle, basename]) {
    const prefixHit = resolvePrefixMatch(key, mdFiles, fromPath);
    if (prefixHit) return prefixHit;
  }

  return null;
}
