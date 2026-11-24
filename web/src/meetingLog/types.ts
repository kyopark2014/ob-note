import type { SpeakerId } from "./config";

export type MeetingEntry = {
  id: string;
  speaker: SpeakerId | string;
  text: string;
  at: number;
  start?: number;
};

export type BatchEntry = {
  speaker: string;
  text: string;
  start?: number;
};

export type PendingBatchJob = {
  jobName: string;
  segmentOffset: number;
  startedAt: number;
  key?: string;
};

export type RecoverableBatchJob = {
  jobName: string;
  status: string;
  createdAt?: string | null;
  completedAt?: string | null;
  failureReason?: string | null;
};

export type MeetingView = "live" | "batch";

export type InterimState = {
  speaker: string;
  text: string;
} | null;
