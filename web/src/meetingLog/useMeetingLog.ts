import { useCallback, useEffect, useRef, useState } from "react";
import { encodeWav, floatTo16BitPcm, recordedSeconds } from "./audio";
import {
  BATCH_STORAGE_KEY_LEGACY,
  DEFAULT_MEETING_TITLE,
  MEETING_LOG_CONFIG,
  SPEAKERS,
  STORAGE_KEY_LEGACY,
  meetingBatchStorageKey,
  meetingEntriesStorageKey,
  type SpeakerId,
} from "./config";
import { decodeEventStream, encodeAudioEvent } from "./eventstream";
import { entriesToCopyText } from "./format";
import type { BatchEntry, InterimState, MeetingEntry, MeetingView } from "./types";

type SpeakerGroup = { speaker: string; text: string; start: number };

function loadJson<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    const parsed = raw ? JSON.parse(raw) : fallback;
    return Array.isArray(parsed) || typeof parsed === "object" ? (parsed as T) : fallback;
  } catch {
    return fallback;
  }
}

function joinTokens(tokens: Array<{ type?: string; content?: string }>): string {
  let text = "";
  for (const token of tokens) {
    const content = String(token.content || "");
    if (!content) continue;
    if (token.type === "punctuation") text += content;
    else text += (text && !/\s$/.test(text) ? " " : "") + content;
  }
  return text.replace(/\s+/g, " ").trim();
}

export function useMeetingLog(userId: string | null) {
  const [entries, setEntries] = useState<MeetingEntry[]>([]);
  const [batchEntries, setBatchEntries] = useState<BatchEntry[]>([]);
  const [view, setView] = useState<MeetingView>("live");
  const [status, setStatus] = useState("마이크를 눌러 기록을 시작하세요.");
  const [listening, setListening] = useState(false);
  const [batchBusy, setBatchBusy] = useState(false);
  const [hasRecordedAudio, setHasRecordedAudio] = useState(false);
  const [autoSpeaker, setAutoSpeaker] = useState(true);
  const [pinnedSpeaker, setPinnedSpeaker] = useState<SpeakerId>("A");
  const [interim, setInterim] = useState<InterimState>(null);
  const [canSaveVault, setCanSaveVault] = useState(false);
  const [savingVault, setSavingVault] = useState(false);
  const [recordedAt, setRecordedAt] = useState<Date>(() => new Date());
  const [title, setTitle] = useState(DEFAULT_MEETING_TITLE);
  /** Only persist after this user's storage has been loaded (avoids cross-user overwrite). */
  const [hydratedUserId, setHydratedUserId] = useState<string | null>(null);

  const wantListenRef = useRef(false);
  const listeningRef = useRef(false);
  const autoSpeakerRef = useRef(autoSpeaker);
  const pinnedSpeakerRef = useRef(pinnedSpeaker);
  const viewRef = useRef(view);
  const entriesRef = useRef(entries);
  const batchEntriesRef = useRef(batchEntries);
  const recordedAtRef = useRef(recordedAt);
  const pcmChunksRef = useRef<Uint8Array[]>([]);
  /** Seconds from meeting start to the current recording segment (wall-clock). */
  const sessionOffsetRef = useRef(0);
  /** Offset captured when the current segment's batch job is queued. */
  const pendingBatchOffsetRef = useRef(0);
  const seenFinalIdsRef = useRef(new Set<string>());
  const socketRef = useRef<WebSocket | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const processorRef = useRef<ScriptProcessorNode | null>(null);
  const batchBusyRef = useRef(false);
  const wakeLockRef = useRef<WakeLockSentinel | null>(null);

  autoSpeakerRef.current = autoSpeaker;
  pinnedSpeakerRef.current = pinnedSpeaker;
  viewRef.current = view;
  entriesRef.current = entries;
  batchEntriesRef.current = batchEntries;
  recordedAtRef.current = recordedAt;
  listeningRef.current = listening;
  batchBusyRef.current = batchBusy;

  // Load / clear meeting state when the signed-in user changes.
  useEffect(() => {
    // Drop pre-scoping global keys so a shared browser profile cannot resurface another user's log.
    try {
      localStorage.removeItem(STORAGE_KEY_LEGACY);
      localStorage.removeItem(BATCH_STORAGE_KEY_LEGACY);
    } catch {
      /* ignore */
    }

    wantListenRef.current = false;
    setListening(false);
    setInterim(null);
    // Tear down any in-flight capture without auto-batch for the previous user.
    try {
      wakeLockRef.current?.release();
    } catch {
      /* ignore */
    }
    wakeLockRef.current = null;
    try {
      processorRef.current?.disconnect();
    } catch {
      /* ignore */
    }
    processorRef.current = null;
    try {
      mediaStreamRef.current?.getTracks().forEach((t) => t.stop());
    } catch {
      /* ignore */
    }
    mediaStreamRef.current = null;
    try {
      void audioCtxRef.current?.close();
    } catch {
      /* ignore */
    }
    audioCtxRef.current = null;
    try {
      socketRef.current?.close();
    } catch {
      /* ignore */
    }
    socketRef.current = null;

    if (!userId) {
      setEntries([]);
      setBatchEntries([]);
      setView("live");
      setStatus("마이크를 눌러 기록을 시작하세요.");
      setBatchBusy(false);
      setHasRecordedAudio(false);
      setAutoSpeaker(true);
      setPinnedSpeaker("A");
      setCanSaveVault(false);
      setSavingVault(false);
      const now = new Date();
      setRecordedAt(now);
      recordedAtRef.current = now;
      setTitle(DEFAULT_MEETING_TITLE);
      seenFinalIdsRef.current.clear();
      pcmChunksRef.current = [];
      sessionOffsetRef.current = 0;
      pendingBatchOffsetRef.current = 0;
      setHydratedUserId(null);
      return;
    }

    const nextEntries = loadJson<MeetingEntry[]>(meetingEntriesStorageKey(userId), []);
    const nextBatch = loadJson<BatchEntry[]>(meetingBatchStorageKey(userId), []);
    setEntries(Array.isArray(nextEntries) ? nextEntries : []);
    setBatchEntries(Array.isArray(nextBatch) ? nextBatch : []);
    setView("live");
    setStatus("마이크를 눌러 기록을 시작하세요.");
    setBatchBusy(false);
    setHasRecordedAudio(false);
    setAutoSpeaker(true);
    setPinnedSpeaker("A");
    setCanSaveVault(
      (Array.isArray(nextBatch) && nextBatch.length > 0) ||
        (Array.isArray(nextEntries) && nextEntries.length > 0),
    );
    setSavingVault(false);
    const now = new Date();
    setRecordedAt(now);
    recordedAtRef.current = now;
    setTitle(DEFAULT_MEETING_TITLE);
    seenFinalIdsRef.current.clear();
    pcmChunksRef.current = [];
    sessionOffsetRef.current = 0;
    pendingBatchOffsetRef.current = 0;
    setHydratedUserId(userId);
  }, [userId]);

  useEffect(() => {
    if (!userId || hydratedUserId !== userId) return;
    localStorage.setItem(meetingEntriesStorageKey(userId), JSON.stringify(entries));
  }, [entries, userId, hydratedUserId]);

  useEffect(() => {
    if (!userId || hydratedUserId !== userId) return;
    localStorage.setItem(meetingBatchStorageKey(userId), JSON.stringify(batchEntries));
  }, [batchEntries, userId, hydratedUserId]);

  const mapSpeakerLabel = useCallback((raw: unknown): string => {
    if (!autoSpeakerRef.current) return pinnedSpeakerRef.current;
    if (raw == null || raw === "") return pinnedSpeakerRef.current;
    const digits = String(raw).replace(/^spk_/i, "");
    const n = Number.parseInt(digits, 10);
    if (Number.isNaN(n) || n < 0) return pinnedSpeakerRef.current;
    const letter = SPEAKERS[Math.min(n, SPEAKERS.length - 1)]!;
    pinnedSpeakerRef.current = letter;
    setPinnedSpeaker(letter);
    return letter;
  }, []);

  const splitBySpeaker = useCallback(
    (alternative: {
      Items?: Array<{
        Speaker?: string | number;
        StartTime?: number | string;
        Type?: string;
        Content?: string;
      }>;
      Transcript?: string;
    }): SpeakerGroup[] => {
      const items = alternative?.Items || [];
      const fallback = String(alternative?.Transcript || "").trim();
      if (!items.length) {
        return fallback
          ? [{ speaker: pinnedSpeakerRef.current, text: fallback, start: 0 }]
          : [];
      }

      const groups: Array<{
        speaker: string;
        tokens: Array<{ type?: string; content?: string }>;
        start: number;
      }> = [];
      let current: (typeof groups)[number] | null = null;
      for (const item of items) {
        const letter: string =
          item.Speaker != null && item.Speaker !== ""
            ? mapSpeakerLabel(item.Speaker)
            : current?.speaker || pinnedSpeakerRef.current;
        const start = Number(item.StartTime);
        if (!current || letter !== current.speaker) {
          current = {
            speaker: letter,
            tokens: [],
            start: Number.isFinite(start) ? start : 0,
          };
          groups.push(current);
        }
        current.tokens.push({ type: item.Type, content: item.Content });
      }

      return groups
        .map((group) => ({
          speaker: group.speaker,
          text: joinTokens(group.tokens) || fallback,
          start: group.start,
        }))
        .filter((group) => group.text);
    },
    [mapSpeakerLabel],
  );

  const addEntry = useCallback((text: string, speaker: string, start?: number) => {
    const cleaned = String(text || "").trim();
    if (!cleaned) return;
    const now = Date.now();
    // Prefer Transcribe offset within this segment, shifted to meeting timeline.
    const meetingStart =
      start != null && Number.isFinite(start)
        ? sessionOffsetRef.current + Math.max(0, start)
        : (now - recordedAtRef.current.getTime()) / 1000;
    setEntries((prev) => {
      const last = prev[prev.length - 1];
      if (
        last &&
        last.text === cleaned &&
        last.speaker === speaker &&
        now - last.at < 1500
      ) {
        return prev;
      }
      return [
        ...prev,
        {
          id: `${now}-${Math.random().toString(16).slice(2)}`,
          speaker,
          text: cleaned,
          at: now,
          start: meetingStart,
        },
      ];
    });
    setInterim(null);
    setStatus(`화자 ${speaker} 발화를 기록했습니다.`);
  }, []);

  const handleTranscriptPayload = useCallback(
    (payload: Record<string, unknown> | null) => {
      const transcript = payload?.Transcript as
        | {
            Results?: Array<{
              Alternatives?: Array<{
                Items?: Array<{
                  Speaker?: string | number;
                  StartTime?: number | string;
                  Type?: string;
                  Content?: string;
                }>;
                Transcript?: string;
              }>;
              IsPartial?: boolean;
              ResultId?: string;
              StartTime?: number;
              EndTime?: number;
            }>;
          }
        | undefined;
      const results = transcript?.Results || [];
      for (const result of results) {
        const alternative = result.Alternatives?.[0];
        if (!alternative) continue;
        const groups = splitBySpeaker(alternative);
        if (!groups.length) continue;
        if (result.IsPartial) {
          if (viewRef.current !== "live") continue;
          const preview = groups.map((g) => `[${g.speaker}] ${g.text}`).join(" ");
          setInterim({ speaker: groups[0]!.speaker, text: preview });
          continue;
        }
        const resultId = String(
          result.ResultId || `${result.StartTime}-${result.EndTime}`,
        );
        if (seenFinalIdsRef.current.has(resultId)) continue;
        seenFinalIdsRef.current.add(resultId);
        groups.forEach((group) => addEntry(group.text, group.speaker, group.start));
      }
    },
    [addEntry, splitBySpeaker],
  );

  const stopAudioCapture = useCallback(() => {
    if (processorRef.current) {
      processorRef.current.onaudioprocess = null;
      try {
        processorRef.current.disconnect();
      } catch {
        /* ignore */
      }
      processorRef.current = null;
    }
    if (audioCtxRef.current) {
      void audioCtxRef.current.close().catch(() => {});
      audioCtxRef.current = null;
    }
    if (mediaStreamRef.current) {
      mediaStreamRef.current.getTracks().forEach((track) => track.stop());
      mediaStreamRef.current = null;
    }
  }, []);

  const closeSocket = useCallback(() => {
    const socket = socketRef.current;
    socketRef.current = null;
    if (!socket) return;
    try {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(encodeAudioEvent(new Uint8Array(0)));
      }
    } catch {
      /* ignore */
    }
    try {
      socket.close();
    } catch {
      /* ignore */
    }
  }, []);

  const sendPcm = useCallback((pcm: Uint8Array) => {
    const socket = socketRef.current;
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    if (!pcm.length) return;
    socket.send(encodeAudioEvent(pcm));
  }, []);

  const startAudioCapture = useCallback(async () => {
    // Each mic-on starts a fresh audio segment (do not concatenate pause gaps).
    pcmChunksRef.current = [];
    setHasRecordedAudio(false);
    const mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        channelCount: 1,
      },
    });
    mediaStreamRef.current = mediaStream;
    const audioCtx = new AudioContext();
    audioCtxRef.current = audioCtx;
    if (audioCtx.state === "suspended") await audioCtx.resume();
    const source = audioCtx.createMediaStreamSource(mediaStream);
    const processor = audioCtx.createScriptProcessor(4096, 1, 1);
    processorRef.current = processor;
    const mute = audioCtx.createGain();
    mute.gain.value = 0;
    processor.onaudioprocess = (event) => {
      if (!wantListenRef.current) return;
      const input = event.inputBuffer.getChannelData(0);
      const pcm = floatTo16BitPcm(input, audioCtx.sampleRate);
      pcmChunksRef.current.push(pcm);
      sendPcm(pcm);
    };
    source.connect(processor);
    processor.connect(mute);
    mute.connect(audioCtx.destination);
  }, [sendPcm]);

  const runBatchTranscribe = useCallback(async () => {
    if (batchBusyRef.current || wantListenRef.current) return;
    if (recordedSeconds(pcmChunksRef.current) < 1) {
      setStatus("전체 변환할 녹음이 충분하지 않습니다.");
      return;
    }

    const { apiUploadUrl, apiBatchUrl, apiBatchJobUrl } = MEETING_LOG_CONFIG;
    if (!userId) {
      setStatus("로그인 후 전체 변환할 수 있습니다.");
      return;
    }
    setBatchBusy(true);
    setCanSaveVault(false);
    const seconds = recordedSeconds(pcmChunksRef.current);
    const segmentOffset = pendingBatchOffsetRef.current;
    const chunks = pcmChunksRef.current.slice();
    setStatus(`이번 녹음(${seconds.toFixed(0)}초)을 올리는 중…`);

    try {
      const wav = encodeWav(chunks);
      const uploadUrl = `${apiUploadUrl}${apiUploadUrl.includes("?") ? "&" : "?"}userId=${encodeURIComponent(userId)}`;
      const signedRes = await fetch(uploadUrl, { cache: "no-store" });
      const signed = (await signedRes.json().catch(() => ({}))) as {
        uploadUrl?: string;
        key?: string;
        detail?: string;
        error?: string;
      };
      if (!signedRes.ok || !signed.uploadUrl || !signed.key) {
        throw new Error(signed.detail || signed.error || "upload-url 응답이 비어 있습니다.");
      }
      const put = await fetch(signed.uploadUrl, {
        method: "PUT",
        headers: { "Content-Type": "audio/wav" },
        body: wav,
      });
      if (!put.ok) {
        throw new Error(`S3 업로드 실패 (HTTP ${put.status})`);
      }

      setStatus("배치 Transcribe 작업을 시작하는 중…");
      const startedRes = await fetch(apiBatchUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: signed.key, userId }),
      });
      const started = (await startedRes.json().catch(() => ({}))) as {
        jobName?: string;
        detail?: string;
        error?: string;
        status?: string;
        entries?: BatchEntry[];
      };
      if (!startedRes.ok || !started.jobName) {
        throw new Error(started.detail || started.error || "작업을 시작하지 못했습니다.");
      }

      const jobUrl = `${apiBatchJobUrl.replace(/\/$/, "")}/${encodeURIComponent(started.jobName)}`;
      let result = started;
      for (let i = 0; i < 60; i += 1) {
        setStatus(`회의 내용 정리 중… (${i + 1}회 확인)`);
        const poll = await fetch(jobUrl, { cache: "no-store" });
        result = (await poll.json().catch(() => ({}))) as typeof started;
        if (!poll.ok && !result.status) {
          throw new Error(
            result.detail || result.error || `작업 조회 실패 (HTTP ${poll.status})`,
          );
        }
        if (result.status === "COMPLETED") break;
        if (result.status === "FAILED") {
          throw new Error(result.error || "배치 변환이 실패했습니다.");
        }
        await new Promise((resolve) => window.setTimeout(resolve, 3000));
      }
      if (result.status !== "COMPLETED") {
        throw new Error("배치 변환이 시간 안에 끝나지 않았습니다. 잠시 후 다시 시도하세요.");
      }

      const segmentEntries = (Array.isArray(result.entries) ? result.entries : []).map(
        (entry) => ({
          ...entry,
          start:
            entry.start != null && Number.isFinite(entry.start)
              ? segmentOffset + Number(entry.start)
              : segmentOffset,
        }),
      );
      // Append this segment's text; do not replace earlier segments.
      setBatchEntries((prev) => [...prev, ...segmentEntries]);
      pcmChunksRef.current = [];
      setHasRecordedAudio(false);
      setView("batch");
      setCanSaveVault(true);
      const added = segmentEntries.filter((entry) => String(entry.text || "").trim()).length;
      setStatus(
        added
          ? `배치 변환이 끝났습니다. ${added}개 구간을 추가했습니다. 보내기로 노트에 저장하세요.`
          : "배치 변환이 끝났습니다. 추가된 텍스트가 없습니다.",
      );
    } catch (err) {
      console.error(err);
      const raw = String(err instanceof Error ? err.message : err || "");
      setStatus(
        raw === "Failed to fetch"
          ? "배치 변환 요청이 네트워크/CORS에서 막혔습니다. 새로고침 후 다시 시도하세요."
          : raw || "배치 변환 중 오류가 났습니다.",
      );
    } finally {
      setBatchBusy(false);
    }
  }, [userId]);

  const releaseWakeLock = useCallback(async () => {
    const lock = wakeLockRef.current;
    wakeLockRef.current = null;
    if (!lock) return;
    try {
      await lock.release();
    } catch {
      /* ignore */
    }
  }, []);

  const requestWakeLock = useCallback(async () => {
    if (!wantListenRef.current) return;
    if (!("wakeLock" in navigator) || typeof navigator.wakeLock?.request !== "function") {
      return;
    }
    try {
      await releaseWakeLock();
      const lock = await navigator.wakeLock.request("screen");
      wakeLockRef.current = lock;
      lock.addEventListener("release", () => {
        if (wakeLockRef.current === lock) wakeLockRef.current = null;
      });
    } catch {
      // Unsupported, denied, or battery saver — recording still works.
    }
  }, [releaseWakeLock]);

  const stopListening = useCallback(
    (opts?: { silent?: boolean; autoBatch?: boolean }) => {
      const silent = opts?.silent ?? false;
      const autoBatch = opts?.autoBatch ?? true;
      wantListenRef.current = false;
      setListening(false);
      void releaseWakeLock();
      stopAudioCapture();
      closeSocket();
      setInterim(null);
      const ready = recordedSeconds(pcmChunksRef.current) >= 1;
      setHasRecordedAudio(ready);
      pendingBatchOffsetRef.current = sessionOffsetRef.current;
      if (!silent) {
        const extra = ready
          ? " 이번 녹음을 배치 변환합니다."
          : "";
        setStatus(
          entriesRef.current.length
            ? `기록을 중지했습니다.${extra}`
            : "마이크를 눌러 기록을 시작하세요.",
        );
      }
      if (autoBatch && ready && !batchBusyRef.current) {
        void runBatchTranscribe();
      }
    },
    [closeSocket, releaseWakeLock, runBatchTranscribe, stopAudioCapture],
  );

  const startListening = useCallback(async () => {
    if (!window.isSecureContext) {
      setStatus("마이크는 localhost 또는 HTTPS에서만 사용할 수 있습니다.");
      return;
    }
    if (!MEETING_LOG_CONFIG.apiStreamUrl) {
      setStatus("Amazon Transcribe API 설정이 없습니다.");
      return;
    }

    wantListenRef.current = true;
    setListening(true);
    setView("live");
    setCanSaveVault(false);
    seenFinalIdsRef.current.clear();
    void requestWakeLock();

    // Fresh audio each time; keep prior transcript text and meeting clock.
    const continuing =
      entriesRef.current.length > 0 || batchEntriesRef.current.length > 0;

    if (!continuing) {
      const now = new Date();
      setRecordedAt(now);
      recordedAtRef.current = now;
      sessionOffsetRef.current = 0;
    } else {
      sessionOffsetRef.current = Math.max(
        0,
        (Date.now() - recordedAtRef.current.getTime()) / 1000,
      );
    }

    setStatus(
      continuing
        ? "새 녹음을 시작합니다. 이전 대화 텍스트는 유지됩니다…"
        : "Transcribe 스트림을 준비하는 중…",
    );

    try {
      const res = await fetch(MEETING_LOG_CONFIG.apiStreamUrl, { cache: "no-store" });
      const data = (await res.json().catch(() => ({}))) as {
        url?: string;
        detail?: string;
        error?: string;
      };
      if (!res.ok) {
        throw new Error(data.detail || data.error || `stream-url 실패 (HTTP ${res.status})`);
      }
      if (!data.url) {
        throw new Error("stream-url 응답에 WebSocket 주소가 없습니다.");
      }
      if (!wantListenRef.current) return;

      await startAudioCapture();
      if (!wantListenRef.current) {
        stopAudioCapture();
        return;
      }

      const socket = new WebSocket(data.url);
      socket.binaryType = "arraybuffer";
      socketRef.current = socket;
      socket.onopen = () => {
        if (!wantListenRef.current) {
          closeSocket();
          return;
        }
        setListening(true);
        setStatus("듣고 있습니다. 실시간 기록 중…");
      };
      socket.onmessage = (event) => {
        try {
          const messages = decodeEventStream(event.data as ArrayBuffer);
          for (const message of messages) {
            const type = message.headers[":message-type"];
            const eventType =
              message.headers[":event-type"] || message.headers[":exception-type"];
            if (type === "exception") {
              const detail =
                (message.payload?.Message as string) ||
                (message.payload?.message as string) ||
                eventType ||
                "Transcribe 오류";
              setStatus(`음성 인식 오류: ${detail}`);
              stopListening({ silent: true, autoBatch: false });
              return;
            }
            if (eventType === "TranscriptEvent") {
              handleTranscriptPayload(message.payload);
            }
          }
        } catch (err) {
          console.error(err);
        }
      };
      socket.onerror = () => {
        if (!wantListenRef.current) return;
        setStatus("Transcribe WebSocket 연결 오류가 발생했습니다.");
      };
      socket.onclose = () => {
        socketRef.current = null;
        if (!wantListenRef.current) return;
        setStatus("Transcribe 세션이 끝났습니다.");
        stopListening({ silent: true, autoBatch: true });
      };
    } catch (err) {
      console.error(err);
      stopListening({ silent: true, autoBatch: false });
      const message = err instanceof Error ? err.message : "음성 인식을 시작하지 못했습니다.";
      const name = err instanceof Error ? err.name : "";
      if (/NotAllowedError|PermissionDenied/i.test(String(name || message))) {
        setStatus("마이크 권한이 필요합니다. 브라우저에서 허용해 주세요.");
        return;
      }
      setStatus(message);
    }
  }, [
    closeSocket,
    handleTranscriptPayload,
    requestWakeLock,
    startAudioCapture,
    stopAudioCapture,
    stopListening,
  ]);

  const toggleListening = useCallback(() => {
    if (wantListenRef.current || listeningRef.current) {
      stopListening({ autoBatch: true });
    } else {
      void startListening();
    }
  }, [startListening, stopListening]);

  const cycleSpeaker = useCallback((id: string) => {
    setEntries((prev) =>
      prev.map((entry) => {
        if (entry.id !== id) return entry;
        const idx = SPEAKERS.indexOf(entry.speaker as SpeakerId);
        const next = SPEAKERS[(idx + 1) % SPEAKERS.length]!;
        return { ...entry, speaker: next };
      }),
    );
  }, []);

  const selectSpeakerMode = useCallback((value: string) => {
    if (value === "auto") {
      setAutoSpeaker(true);
      setStatus("Amazon Transcribe 화자 분리(spk_0…)를 A–F로 표시합니다.");
    } else {
      setAutoSpeaker(false);
      setPinnedSpeaker(value as SpeakerId);
      setStatus(`화자 ${value}로 기록합니다.`);
    }
  }, []);

  const clearLog = useCallback(() => {
    if (!entriesRef.current.length && !batchEntries.length) {
      setStatus("지울 기록이 없습니다.");
      return false;
    }
    setEntries([]);
    setBatchEntries([]);
    seenFinalIdsRef.current.clear();
    pcmChunksRef.current = [];
    sessionOffsetRef.current = 0;
    pendingBatchOffsetRef.current = 0;
    setHasRecordedAudio(false);
    setCanSaveVault(false);
    setInterim(null);
    setTitle(DEFAULT_MEETING_TITLE);
    setStatus("기록을 지웠습니다. 마이크를 눌러 다시 시작하세요.");
    return true;
  }, [batchEntries.length]);

  const copyLog = useCallback(async () => {
    const text = entriesToCopyText(view, entries, batchEntries);
    if (!text) {
      setStatus("복사할 기록이 없습니다.");
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      setStatus("회의 기록을 클립보드에 복사했습니다.");
    } catch {
      setStatus("복사에 실패했습니다. 브라우저 클립보드 권한을 확인해 주세요.");
    }
  }, [batchEntries, entries, view]);

  useEffect(() => {
    const onVisibility = () => {
      if (document.visibilityState === "visible" && wantListenRef.current) {
        void requestWakeLock();
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      wantListenRef.current = false;
      void releaseWakeLock();
      stopAudioCapture();
      closeSocket();
    };
  }, [closeSocket, releaseWakeLock, requestWakeLock, stopAudioCapture]);

  const batchReady =
    hasRecordedAudio &&
    recordedSeconds(pcmChunksRef.current) >= 1 &&
    !listening &&
    !batchBusy;

  return {
    entries,
    batchEntries,
    view,
    setView,
    status,
    listening,
    batchBusy,
    hasRecordedAudio,
    autoSpeaker,
    pinnedSpeaker,
    interim,
    canSaveVault,
    setCanSaveVault,
    savingVault,
    setSavingVault,
    recordedAt,
    title,
    setTitle,
    batchReady,
    toggleListening,
    stopListening,
    runBatchTranscribe,
    cycleSpeaker,
    selectSpeakerMode,
    clearLog,
    copyLog,
    setStatus,
  };
}

export type MeetingLogController = ReturnType<typeof useMeetingLog>;
