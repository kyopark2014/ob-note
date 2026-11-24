/** Meeting Log Amazon Transcribe API (from meeting-log installer). */
export type MeetingLogConfig = {
  projectName: string;
  apiGatewayUrl: string;
  apiStreamUrl: string;
  apiUploadUrl: string;
  apiBatchUrl: string;
  apiBatchJobUrl: string;
  apiBatchJobsUrl: string;
};

// Same-origin, session-authenticated proxy (no public API Gateway).
const API = "/api/meeting-log";

export const MEETING_LOG_CONFIG: MeetingLogConfig = {
  projectName: "meeting-log",
  apiGatewayUrl: API,
  apiStreamUrl: `${API}/stream-url`,
  apiUploadUrl: `${API}/upload-url`,
  apiBatchUrl: `${API}/batch-transcribe`,
  apiBatchJobUrl: `${API}/batch-job`,
  apiBatchJobsUrl: `${API}/batch-jobs`,
};

/** Batch Transcribe poll window (long meetings often take 5–10+ minutes). */
export const BATCH_POLL_INTERVAL_MS = 3000;
export const BATCH_POLL_MAX_MS = 15 * 60 * 1000;
export const BATCH_POLL_MAX_ATTEMPTS = Math.ceil(
  BATCH_POLL_MAX_MS / BATCH_POLL_INTERVAL_MS,
);

export const TARGET_SAMPLE_RATE = 16000;
export const SPEAKERS = ["A", "B", "C", "D", "E", "F"] as const;
export type SpeakerId = (typeof SPEAKERS)[number];

export const DEFAULT_MEETING_TITLE = "제목없음";
export const MEETING_FOLDER = "Meeting";

/** Legacy global keys (pre user-scoping / pre rename). Cleared on load so they cannot leak across users. */
export const STORAGE_KEY_LEGACY = "ob-docs:meeting-log-entries-v1";
export const BATCH_STORAGE_KEY_LEGACY = "ob-docs:meeting-log-batch-v1";

export function meetingEntriesStorageKey(userId: string): string {
  return `ob-note:meeting-log-entries-v1:${userId}`;
}

export function meetingBatchStorageKey(userId: string): string {
  return `ob-note:meeting-log-batch-v1:${userId}`;
}

export function meetingPendingJobStorageKey(userId: string): string {
  return `ob-note:meeting-log-pending-job-v1:${userId}`;
}
