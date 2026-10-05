import type { FilePayload, GraphPayload, SearchHit, TreeNode } from "./types";

export type ShareEntry = {
  token: string;
  path: string;
  title: string;
  type?: "note" | "folder";
  created_at: number;
  url_path: string;
  url?: string;
};

export type GraphPattern = "pattern1" | "pattern2" | "pattern3";

export type NotesFolderOption = {
  name: string;
  path: string;
};

export type NotesGraphStatus = {
  notes_dir: string;
  folders?: string[];
  include_missing?: boolean;
  available_folders?: NotesFolderOption[];
  exists: boolean;
  path?: string | null;
  storage?: string;
  status: string;
  pattern?: GraphPattern | string;
  error?: string | null;
  message?: string | null;
  last_success_at?: string | null;
  progress?: {
    file?: string | null;
    file_i?: number | null;
    file_n?: number | null;
    pct?: number | null;
    phase?: string | null;
  } | null;
};

export type NotesSourcesConfig = {
  notes_dir: string;
  folders: string[];
  include_missing: boolean;
  available_folders: NotesFolderOption[];
  max_sources: number;
};

export type CompressJobStatus = {
  ok: boolean;
  job_id?: string | null;
  status: "idle" | "queued" | "running" | "ready" | "error" | string;
  message?: string | null;
  error?: string | null;
  path?: string | null;
  zip_name?: string | null;
  backup_path?: string | null;
  s3_key?: string | null;
  url?: string | null;
  expires_in?: number | null;
  progress?: {
    file?: string | null;
    file_i?: number | null;
    file_n?: number | null;
    pct?: number | null;
    phase?: string | null;
  } | null;
  already_running?: boolean;
};

export type ClearingItem = {
  path: string;
  name: string;
  folder: string;
  size: number;
  mtime: number;
  suffix: string;
};

export type ClearingScan = {
  ok: boolean;
  media_count: number;
  referenced_count: number;
  kept_count: number;
  kept_paths: string[];
  items: ClearingItem[];
};

export type ClearingDeleteStatus = {
  ok: boolean;
  job_id?: string | null;
  status: "idle" | "queued" | "running" | "ready" | "error" | string;
  busy?: boolean;
  message?: string | null;
  error?: string | null;
  updated_at?: number;
  deleted?: string[];
  errors?: { path: string; error: string }[];
  progress?: {
    file?: string | null;
    file_i?: number | null;
    file_n?: number | null;
    pct?: number | null;
    phase?: string | null;
  } | null;
};

export type ClearingJobStatus = {
  ok: boolean;
  job_id?: string | null;
  status: "idle" | "queued" | "running" | "ready" | "error" | string;
  busy?: boolean;
  message?: string | null;
  error?: string | null;
  updated_at?: number;
  progress?: {
    file?: string | null;
    file_i?: number | null;
    file_n?: number | null;
    pct?: number | null;
    phase?: string | null;
  } | null;
  scan?: ClearingScan | null;
};

export type CompressListItem = {
  id: string;
  scope: "vault" | "folder" | string;
  path?: string | null;
  zip_name?: string | null;
  backup_path?: string | null;
  s3_key?: string | null;
  status: "Processing" | "Completed" | "Expired" | "Failed" | string;
  created_at?: number | null;
  expires_at?: number | null;
  url?: string | null;
  message?: string | null;
  error?: string | null;
  size?: number | null;
  progress?: CompressJobStatus["progress"];
};

const BASE = "/api";

async function readResponseBody(res: Response): Promise<unknown> {
  const raw = await res.text();
  if (!raw) return null;
  try {
    return JSON.parse(raw) as unknown;
  } catch {
    return raw;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
    ...init,
  });
  const body = await readResponseBody(res);
  if (!res.ok) {
    const message = formatApiError(body, res.status);
    const err = new Error(message) as Error & { status?: number; detail?: unknown };
    err.status = res.status;
    err.detail = body;
    throw err;
  }
  return body as T;
}

function formatApiError(detail: unknown, status: number): string {
  if (typeof detail === "string" && detail.trim()) return detail.trim();
  if (detail && typeof detail === "object") {
    const root = detail as { detail?: unknown; message?: unknown; error?: unknown };
    const inner = root.detail !== undefined ? root.detail : root;
    if (typeof inner === "string" && inner.trim()) return inner.trim();
    if (inner && typeof inner === "object") {
      const obj = inner as { message?: unknown; error?: unknown; mode?: unknown };
      if (typeof obj.message === "string" && obj.message.trim()) {
        if (obj.error === "s3_unavailable" || obj.mode === "local") {
          return "로컬 모드에서는 S3 동기화를 사용할 수 없습니다. VAULT_S3_ENABLE=1 로 실행하세요.";
        }
        return obj.message.trim();
      }
      if (typeof obj.error === "string" && obj.error.trim()) return obj.error.trim();
    }
    if (typeof root.message === "string" && root.message.trim()) return root.message.trim();
  }
  return `HTTP ${status}`;
}

export const api = {
  getSession: () =>
    request<{ user_id: string; sharing_url: string; authenticated: boolean }>(
      "/session",
    ),
  createLocalSession: (user_id = "local-dev") =>
    request<{ user_id: string; sharing_url: string }>("/session", {
      method: "POST",
      body: JSON.stringify({ user_id }),
    }),
  setSessionWithAccessToken: (access_token: string) =>
    request<{ user_id: string; sharing_url: string; authenticated: boolean }>(
      "/session",
      {
        method: "POST",
        body: JSON.stringify({ access_token }),
      },
    ),
  loginWithCognito: (username: string, password: string) =>
    request<{ user_id: string; sharing_url: string; authenticated: boolean }>(
      "/session",
      {
        method: "POST",
        body: JSON.stringify({ username, password }),
      },
    ),
  getPublicConfig: () =>
    request<{
      auth_mode: "google" | "cognito";
      google_client_id: string;
      local_auth_bypass: boolean;
      sharing_url: string;
      project_name: string;
      cognito_admin_username: string;
    }>("/config"),
  clearSession: () =>
    request<{ ok: boolean }>("/session", { method: "DELETE" }),
  getTree: () =>
    request<{ root: string; mode: string; children: TreeNode[] }>("/files/tree"),
  readFile: (path: string) =>
    request<FilePayload>(`/files/read?path=${encodeURIComponent(path)}`),
  writeFile: (path: string, content: string, opts?: { syncFilename?: boolean }) =>
    request<{
      ok: boolean;
      path: string;
      renamed_from?: string | null;
      word_count?: number;
      char_count?: number;
    }>("/files/write", {
      method: "PUT",
      body: JSON.stringify({
        path,
        content,
        sync_filename: Boolean(opts?.syncFilename),
      }),
    }),
  mkdir: (path: string) =>
    request<{ ok: boolean }>("/files/mkdir", {
      method: "POST",
      body: JSON.stringify({ path }),
    }),
  rename: (from_path: string, to_path: string, opts?: { copyCompanions?: boolean }) =>
    request<{
      ok: boolean;
      from: string;
      to: string;
      companion_images?: "queued" | null;
    }>("/files/rename", {
      method: "POST",
      body: JSON.stringify({
        from_path,
        to_path,
        copy_companions: Boolean(opts?.copyCompanions),
      }),
    }),
  copyFile: (from_path: string, to_path: string) =>
    request<{
      ok: boolean;
      from: string;
      to: string;
      overwritten?: boolean;
      companion_images?: "queued" | null;
    }>("/files/copy", {
      method: "POST",
      body: JSON.stringify({ from_path, to_path }),
    }),
  reorderFolder: (folder: string, names: string[]) =>
    request<{ ok: boolean; folder: string; names: string[] }>("/files/order", {
      method: "PUT",
      body: JSON.stringify({ folder, names }),
    }),
  duplicate: (path: string) =>
    request<{ ok: boolean; from: string; to: string }>("/files/duplicate", {
      method: "POST",
      body: JSON.stringify({ path }),
    }),
  compressFolder: (path: string) =>
    request<CompressJobStatus>("/files/compress", {
      method: "POST",
      body: JSON.stringify({ path, scope: "folder" }),
    }),
  compressVault: () =>
    request<CompressJobStatus>("/files/compress", {
      method: "POST",
      body: JSON.stringify({ scope: "vault" }),
    }),
  getCompressStatus: (signal?: AbortSignal) =>
    request<CompressJobStatus>("/files/compress", { signal }),
  listCompressItems: () =>
    request<{ ok: boolean; items: CompressListItem[] }>("/files/compress/items"),
  deleteCompressItem: (id: string) =>
    request<{ ok: boolean; id: string }>("/files/compress/delete", {
      method: "POST",
      body: JSON.stringify({ id }),
    }),
  refreshCompressItem: (id: string) =>
    request<{ ok: boolean; item: CompressListItem }>("/files/compress/refresh", {
      method: "POST",
      body: JSON.stringify({ id }),
    }),
  startClearing: () =>
    request<ClearingJobStatus>("/files/clearing", { method: "POST" }),
  getClearingStatus: () => request<ClearingJobStatus>("/files/clearing"),
  keepClearing: (paths: string[]) =>
    request<{ ok: boolean; kept: string[] }>("/files/clearing/keep", {
      method: "POST",
      body: JSON.stringify({ paths }),
    }),
  unkeepClearing: (paths: string[] = [], all = false) =>
    request<{ ok: boolean; kept: string[] }>("/files/clearing/unkeep", {
      method: "POST",
      body: JSON.stringify({ paths, all }),
    }),
  startClearingDelete: (paths: string[]) =>
    request<ClearingDeleteStatus>("/files/clearing/delete", {
      method: "POST",
      body: JSON.stringify({ paths }),
    }),
  getClearingDeleteStatus: () =>
    request<ClearingDeleteStatus>("/files/clearing/delete"),
  createShare: (path: string) =>
    request<{
      ok: boolean;
      token: string;
      path: string;
      title: string;
      type?: "note" | "folder";
      created_at?: number;
      url_path: string;
      url?: string;
    }>("/files/share", {
      method: "POST",
      body: JSON.stringify({ path }),
    }),
  listShares: () =>
    request<{ ok: boolean; count: number; shares: ShareEntry[] }>("/files/shares"),
  deleteShare: (token: string) =>
    request<{ ok: boolean; token: string }>("/files/share/delete", {
      method: "POST",
      body: JSON.stringify({ token }),
    }),
  getSharePermission: () =>
    request<{
      ok: boolean;
      permission: string;
      options: string[];
      default: string;
    }>("/files/share/permission"),
  setSharePermission: (permission: string) =>
    request<{ ok: boolean; permission: string }>("/files/share/permission", {
      method: "PUT",
      body: JSON.stringify({ permission }),
    }),
  deletePath: (path: string) =>
    request<{ ok: boolean }>("/files/delete", {
      method: "POST",
      body: JSON.stringify({ path }),
    }),
  uploadFile: async (path: string, blob: Blob, filename?: string) => {
    const fd = new FormData();
    fd.append("path", path);
    fd.append("file", blob, filename || path.split("/").pop() || "upload.bin");
    const res = await fetch(`${BASE}/files/upload`, {
      method: "POST",
      credentials: "include",
      body: fd,
    });
    const body = await readResponseBody(res);
    if (!res.ok) {
      const err = new Error(
        typeof body === "object" && body && "detail" in body
          ? JSON.stringify((body as { detail: unknown }).detail)
          : formatApiError(body, res.status),
      ) as Error & { status?: number; detail?: unknown };
      err.status = res.status;
      err.detail = body;
      throw err;
    }
    return body as { ok: boolean; path: string; size: number };
  },
  rawUrl: (path: string) =>
    `${BASE}/files/raw?path=${encodeURIComponent(path)}`,
  /** Authenticated HTML viewer (new tab) for vault attachments — agentic-work style. */
  viewUrl: (path: string) =>
    `${BASE}/files/view?path=${encodeURIComponent(path)}`,
  search: (q: string) =>
    request<{ query: string; results: SearchHit[] }>(
      `/search?q=${encodeURIComponent(q)}`,
    ),
  resolveWikiLink: (target: string, fromPath?: string | null) => {
    const qs = new URLSearchParams({ target });
    if (fromPath) qs.set("from", fromPath);
    return request<{ target: string; from: string | null; path: string | null }>(
      `/search/resolve?${qs.toString()}`,
    );
  },
  getGraph: () => request<GraphPayload>("/graph"),
  getNotesGraphStatus: () => request<NotesGraphStatus>("/graph/status"),
  syncNotesGraph: (full = false) =>
    request<NotesGraphStatus>(`/graph/sync${full ? "?full=1" : ""}`, {
      method: "POST",
    }),
  rebuildGraph: () =>
    request<NotesGraphStatus & { ok?: boolean; notes?: number; links?: number }>(
      "/graph/rebuild",
      { method: "POST" },
    ),
  setNotesGraphPattern: (pattern: GraphPattern | string) =>
    request<NotesGraphStatus>("/graph/pattern", {
      method: "PATCH",
      body: JSON.stringify({ pattern }),
    }),
  getNotesGraphSources: () => request<NotesSourcesConfig>("/graph/sources"),
  putNotesGraphSources: (body: {
    folders: string[];
    include_missing?: boolean;
  }) =>
    request<NotesSourcesConfig>("/graph/sources", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  getSyncStatus: () =>
    request<{
      mode: string;
      pending: number;
      status?: string;
      busy?: boolean;
      message?: string | null;
      error?: string | null;
      progress?: {
        file?: string | null;
        file_i?: number | null;
        file_n?: number | null;
        pct?: number | null;
        phase?: string | null;
      } | null;
      ops: Array<{ op: string; path?: string }>;
    }>("/files/sync"),
  syncVault: () =>
    request<{
      ok?: boolean;
      status: string;
      busy?: boolean;
      message?: string;
      pending?: number;
    }>("/files/sync", { method: "POST" }),
  agentHealth: () =>
    request<{
      status: string;
      backend?: string;
      agentConfigured?: boolean;
      harnessConfigured?: boolean;
      harnessArn?: string | null;
      tools?: string[];
    }>("/agent/health"),
  agentNoteMeta: (path: string) =>
    request<{
      path: string;
      name: string;
      size: number;
      char_count: number;
      note_id?: string | null;
      title?: string | null;
      size_bytes?: number;
      created_at?: string | null;
      updated_at?: string | null;
    }>(`/agent/note-meta?path=${encodeURIComponent(path)}`),
  listNotes: () =>
    request<{
      count: number;
      notes: {
        note_id: string;
        title: string;
        path: string;
        size_bytes: number;
        created_at: string;
        updated_at: string;
      }[];
    }>("/files/notes"),
  agentMessages: (opts: { noteId?: string | null; notePath?: string | null }) => {
    const q = new URLSearchParams();
    if (opts.noteId) q.set("note_id", opts.noteId);
    if (opts.notePath) q.set("note_path", opts.notePath);
    return request<{
      note_id: string;
      count: number;
      messages: Array<{
        id: string;
        note_id: string;
        role: "user" | "assistant";
        content: string;
        attachments: string[];
        tool_events: AgentToolEvent[];
        created_at: string;
      }>;
    }>(`/agent/messages?${q.toString()}`);
  },
  clearAgentMessages: (opts: { noteId?: string | null; notePath?: string | null }) => {
    const q = new URLSearchParams();
    if (opts.noteId) q.set("note_id", opts.noteId);
    if (opts.notePath) q.set("note_path", opts.notePath);
    return request<{ ok: boolean; note_id: string; removed: number }>(
      `/agent/messages?${q.toString()}`,
      { method: "DELETE" },
    );
  },
  agentModels: () =>
    request<{ models: string[]; default_model: string }>("/agent/models"),
  getDocumentsStatus: () => request<DocumentsStatus>("/documents/status"),
  getDocumentsConfig: () => request<DocumentsConfig>("/documents/config"),
  putDocumentsConfig: (body: {
    foundation_model_parser_enabled?: boolean;
    parallel_processing_enabled?: boolean;
  }) =>
    request<DocumentsConfig>("/documents/config", {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  getDocumentsProjectList: (publishMd = true) =>
    request<DocumentsListResult>(
      `/documents/project-list${publishMd ? "" : "?publish_md=0"}`,
    ),
  getDocumentsDrawingList: (publishMd = true) =>
    request<DocumentsListResult>(
      `/documents/drawing-list${publishMd ? "" : "?publish_md=0"}`,
    ),
  deleteDocumentsDocument: (
    filename: string,
    kind: "project" | "drawing" = "project",
  ) =>
    request<{ ok: boolean }>(
      `/documents/documents/${encodeURIComponent(filename)}?kind=${encodeURIComponent(kind)}`,
      { method: "DELETE" },
    ),
  copyDocumentsToVault: (
    filename: string,
    kind: "project" | "drawing" = "project",
  ) =>
    request<DocumentsCopyToVaultResult>(
      `/documents/documents/${encodeURIComponent(filename)}/copy-to-vault?kind=${encodeURIComponent(kind)}`,
      { method: "POST" },
    ),
  uploadDocumentsProjectFile: async (
    file: File,
  ): Promise<DocumentsUploadResult> => {
    const presign = await request<DocumentsPresignResult>(
      "/documents/projects/presign",
      {
        method: "POST",
        body: JSON.stringify({
          file_name: file.name,
          size: file.size,
          content_type: file.type || undefined,
        }),
      },
    );
    if (!presign.upload_url || !presign.s3_key) {
      throw new Error("Presign succeeded but no upload URL was returned");
    }
    let putRes: Response;
    try {
      putRes = await fetch(presign.upload_url, {
        method: "PUT",
        headers:
          presign.headers || {
            "Content-Type": file.type || "application/octet-stream",
          },
        body: file,
      });
    } catch (err) {
      const detail = err instanceof Error ? err.message : String(err);
      throw new Error(`S3 upload failed: ${detail}`);
    }
    if (!putRes.ok) {
      const text = await putRes.text().catch(() => "");
      const msgMatch = text.match(/<Message>([^<]+)<\/Message>/i);
      throw new Error(
        msgMatch?.[1] || `S3 upload failed (HTTP ${putRes.status})`,
      );
    }
    return request<DocumentsUploadResult>("/documents/projects/complete", {
      method: "POST",
      body: JSON.stringify({
        file_name: presign.file_name,
        s3_key: presign.s3_key,
        size: file.size,
        original_filename: file.name,
      }),
    });
  },
  uploadDocumentsDrawingFile: async (
    file: File,
  ): Promise<DocumentsUploadResult> => {
    const presign = await request<DocumentsPresignResult>(
      "/documents/drawings/presign",
      {
        method: "POST",
        body: JSON.stringify({
          file_name: file.name,
          size: file.size,
          content_type: file.type || undefined,
        }),
      },
    );
    if (!presign.upload_url || !presign.s3_key) {
      throw new Error("Presign succeeded but no upload URL was returned");
    }
    let putRes: Response;
    try {
      putRes = await fetch(presign.upload_url, {
        method: "PUT",
        headers:
          presign.headers || {
            "Content-Type": file.type || "application/octet-stream",
          },
        body: file,
      });
    } catch (err) {
      const detail = err instanceof Error ? err.message : String(err);
      throw new Error(`S3 upload failed: ${detail}`);
    }
    if (!putRes.ok) {
      const text = await putRes.text().catch(() => "");
      const msgMatch = text.match(/<Message>([^<]+)<\/Message>/i);
      throw new Error(
        msgMatch?.[1] || `S3 upload failed (HTTP ${putRes.status})`,
      );
    }
    return request<DocumentsUploadResult>("/documents/drawings/complete", {
      method: "POST",
      body: JSON.stringify({
        file_name: presign.file_name,
        s3_key: presign.s3_key,
        size: file.size,
        original_filename: file.name,
      }),
    });
  },
  syncDocuments: (full = false, model?: string) => {
    const params = new URLSearchParams();
    if (full) params.set("full", "1");
    if (model) params.set("model", model);
    const qs = params.toString();
    return request<DocumentsStatus>(`/documents/sync${qs ? `?${qs}` : ""}`, {
      method: "POST",
    });
  },
  agentChat: (
    body: {
      prompt: string;
      note_path?: string | null;
      session_id?: string | null;
      model_name?: string | null;
      image_paths?: string[];
      file_paths?: string[];
    },
    handlers: AgentChatHandlers,
    signal?: AbortSignal,
  ) => streamAgentChat(body, handlers, signal),
};

export type AgentToolEvent = {
  type: "text" | "tool" | "tool_result" | "info";
  tool?: string;
  input?: unknown;
  toolUseId?: string;
  data?: string;
};

export type AgentChatHandlers = {
  onSession?: (sessionId: string) => void;
  onToken?: (text: string) => void;
  onText?: (data: string) => void;
  onTool?: (event: AgentToolEvent) => void;
  onToolResult?: (event: AgentToolEvent) => void;
  onNoteUpdated?: (path: string) => void;
  onDone?: (
    result: string,
    sessionId?: string,
    toolEvents?: AgentToolEvent[],
  ) => void;
  onError?: (message: string) => void;
};

export type DocumentsStatus = {
  documents_dir: string;
  projects_dir?: string;
  drawings_dir?: string;
  files?: Array<{ name: string; path: string; bytes: number; mtime?: number }>;
  exists?: boolean;
  status: "idle" | "queued" | "running" | "ready" | "error" | "unchanged" | string;
  foundation_model_parser_enabled?: boolean;
  parallel_processing_enabled?: boolean;
  error?: string | null;
  message?: string | null;
  last_success_at?: string | null;
  progress?: {
    file?: string | null;
    file_i?: number | null;
    file_n?: number | null;
    page?: number | null;
    page_n?: number | null;
    pct?: number | null;
    aggregated?: boolean | null;
  } | null;
};

export type DocumentsConfig = {
  documents_dir: string;
  projects_dir?: string;
  drawings_dir?: string;
  files?: Array<{ name: string; path: string; bytes: number; mtime?: number }>;
  foundation_model_parser_enabled?: boolean;
  parallel_processing_enabled?: boolean;
};

export type DocumentsDocument = {
  filename?: string;
  original_filename?: string;
  display_name?: string;
  md_file?: string;
  md_path?: string;
  source_path?: string;
  status?: string;
  bytes?: number;
  title?: string;
  pdf_available?: boolean;
  md_available?: boolean;
  md_bytes?: number | null;
  pdf_url?: string | null;
  pdf_api_url?: string | null;
  md_url?: string | null;
  md_workspace_path?: string | null;
  md_local_artifacts?: string | null;
  md_viewer_url?: string | null;
  md_published?: boolean;
  kind?: string;
  created_at?: string;
  updated_at?: string;
  extracted_at?: string;
};

export type DocumentsListResult = {
  documents_dir?: string;
  docs_dir?: string;
  projects_dir?: string;
  drawings_dir?: string;
  documents: DocumentsDocument[];
  doc_count?: number;
  doc_list?: string;
  doc_list_updated_at?: string | null;
  sharing_url?: string | null;
};

export type DocumentsPresignResult = {
  ok?: boolean;
  file_name: string;
  original_filename?: string;
  sanitized?: boolean;
  s3_key: string;
  content_type?: string;
  upload_url: string;
  headers?: Record<string, string>;
  expires_in?: number;
  docs_dir?: string;
};

export type DocumentsUploadResult = {
  documents_dir: string;
  docs_dir?: string;
  projects_dir?: string;
  drawings_dir?: string;
  raw_dir?: string;
  saved: {
    name: string;
    original_filename?: string;
    sanitized?: boolean;
    path: string;
    bytes: number;
    overwritten?: boolean;
  };
  count: number;
  files?: Array<{ name: string; path: string; bytes: number; mtime?: number }>;
  documents?: Array<Record<string, unknown>>;
  doc_count?: number;
  s3_key?: string;
};

export type DocumentsCopyToVaultResult = {
  ok?: boolean;
  path: string;
  kind: "project" | "drawing" | string;
  bytes: number;
  source_md?: string;
  note_id?: string | null;
};

async function streamAgentChat(
  body: {
    prompt: string;
    note_path?: string | null;
    session_id?: string | null;
    model_name?: string | null;
    image_paths?: string[];
    file_paths?: string[];
  },
  handlers: AgentChatHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${BASE}/agent/chat`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({
      prompt: body.prompt,
      note_path: body.note_path || null,
      session_id: body.session_id || null,
      model_name: body.model_name || null,
      image_paths: body.image_paths || [],
      file_paths: body.file_paths || [],
    }),
    signal,
  });
  if (!res.ok) {
    const errBody = await readResponseBody(res);
    throw new Error(formatApiError(errBody, res.status));
  }
  if (!res.body) {
    throw new Error("Empty SSE response");
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finished = false;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() || "";
    for (const part of parts) {
      const lines = part.split("\n");
      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const raw = line.slice(6).trim();
        if (!raw) continue;
        let payload: Record<string, unknown>;
        try {
          payload = JSON.parse(raw) as Record<string, unknown>;
        } catch {
          continue;
        }
        const type = String(payload.type || "");
        if (type === "session" && typeof payload.session_id === "string") {
          handlers.onSession?.(payload.session_id);
        } else if (type === "token" && typeof payload.text === "string") {
          handlers.onToken?.(payload.text);
        } else if (type === "text" && typeof payload.data === "string") {
          handlers.onText?.(payload.data);
        } else if (type === "tool") {
          handlers.onTool?.({
            type: "tool",
            tool: typeof payload.tool === "string" ? payload.tool : undefined,
            input: payload.input,
            toolUseId:
              typeof payload.toolUseId === "string" ? payload.toolUseId : undefined,
          });
        } else if (type === "tool_result") {
          handlers.onToolResult?.({
            type: "tool_result",
            tool: typeof payload.tool === "string" ? payload.tool : undefined,
            toolUseId:
              typeof payload.toolUseId === "string" ? payload.toolUseId : undefined,
            data: typeof payload.data === "string" ? payload.data : undefined,
          });
        } else if (type === "note_updated" && typeof payload.path === "string") {
          handlers.onNoteUpdated?.(payload.path);
        } else if (type === "done") {
          finished = true;
          const toolEvents = Array.isArray(payload.tool_events)
            ? (payload.tool_events as AgentToolEvent[])
            : undefined;
          handlers.onDone?.(
            typeof payload.result === "string" ? payload.result : "",
            typeof payload.session_id === "string" ? payload.session_id : undefined,
            toolEvents,
          );
        } else if (type === "error") {
          finished = true;
          handlers.onError?.(
            typeof payload.error === "string" ? payload.error : "Agent error",
          );
        }
      }
    }
  }
  if (!finished && !signal?.aborted) {
    handlers.onError?.("연결이 종료되었습니다. 다시 시도해 주세요.");
  }
}
