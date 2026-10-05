import { useCallback, useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, type AgentChatHandlers, type AgentToolEvent } from "../api";
import { isImageFileName } from "../viewSettings";
import {
  AgentChatInput,
  type AgentNoteChip,
  type AgentSendPayload,
} from "./AgentChatInput";
import { RefreshIcon } from "./Icons";
import { ToolCallCard } from "./ToolCallCard";

// 6F~12F, 2~2.5처럼 범위에 쓰는 단일 ~ 는 그대로 둔다.
// 취소선은 ~~텍스트~~ 만 적용한다.
const gfmRemarkPlugins = [[remarkGfm, { singleTilde: false }]];

export type AgentMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  /** Vault-relative paths attached with this user turn (images + loaded files). */
  attachments?: string[];
  toolEvents?: AgentToolEvent[];
};

type Props = {
  notePath: string | null;
  modelName?: string | null;
  onClose: () => void;
  onNoteUpdated?: (path: string) => void;
  onResizeStart?: (e: ReactPointerEvent<HTMLDivElement>) => void;
  onResizeReset?: () => void;
  resizing?: boolean;
};

function fileNameFromPath(path: string): string {
  const raw = path.split("?")[0].split("#")[0];
  const name = raw.split("/").pop();
  try {
    return decodeURIComponent(name || path);
  } catch {
    return name || path;
  }
}

function MessageAttachments({ paths }: { paths: string[] }) {
  if (!paths.length) return null;

  const images = paths.filter((p) => isImageFileName(p));
  const files = paths.filter((p) => !isImageFileName(p));

  return (
    <>
      {files.length > 0 && (
        <div className="agent-message-loaded-files" aria-label="첨부 파일">
          {files.map((path) => {
            const label = fileNameFromPath(path);
            return (
              <a
                key={path}
                className="agent-message-loaded-file agent-message-loaded-file-link"
                href={api.viewUrl(path)}
                target="_blank"
                rel="noopener noreferrer"
                title={`${path}\n클릭하여 새 탭에서 열기`}
              >
                <span className="agent-message-loaded-file-name">{label}</span>
              </a>
            );
          })}
        </div>
      )}
      {images.length > 0 && (
        <div className="agent-message-images" aria-label="첨부 이미지">
          {images.map((path) => (
            <a
              key={path}
              className="agent-message-image-link"
              href={api.viewUrl(path)}
              target="_blank"
              rel="noopener noreferrer"
              title={`${path}\n클릭하여 새 탭에서 열기`}
            >
              <img src={api.rawUrl(path)} alt={fileNameFromPath(path)} />
            </a>
          ))}
        </div>
      )}
    </>
  );
}

function newId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
}

/** Client-side safety net: never render VAULT_WRITE marker bodies in chat. */
function stripVaultWriteMarkers(text: string): string {
  const complete =
    /<<<VAULT_WRITE\s+.+?>>>\s*\n?[\s\S]*?<<<END_VAULT_WRITE>>>/g;
  const incomplete = /<<<VAULT_WRITE\b[\s\S]*$/;
  return text.replace(complete, "").replace(incomplete, "").trim();
}

function upsertStreamEvent(
  prev: AgentToolEvent[],
  event: AgentToolEvent,
): AgentToolEvent[] {
  if (event.type === "text") {
    const data = stripVaultWriteMarkers(event.data || "");
    if (!data) return prev;
    return [...prev, { type: "text", data }];
  }
  const tid = (event.toolUseId || "").trim();
  if (!tid) return [...prev, event];
  const next = [...prev];
  for (let i = next.length - 1; i >= 0; i -= 1) {
    if (next[i].type === event.type && next[i].toolUseId === tid) {
      next[i] = { ...next[i], ...event };
      return next;
    }
  }
  next.push(event);
  return next;
}

function normalizeText(value: string): string {
  return value.trim().replace(/\s+/g, " ");
}

function isStreamingPrefixOfFinal(partial: string, finalText: string): boolean {
  if (!partial || !finalText) return false;
  if (finalText.startsWith(partial) || partial.startsWith(finalText)) return true;
  const headLen = Math.min(partial.length, finalText.length, 80);
  return partial.slice(0, headLen) === finalText.slice(0, headLen);
}

/** Hide early text segments that were superseded by the final reply (harness-work). */
function filterSupersededTextEvents(
  events: AgentToolEvent[],
  content: string,
): AgentToolEvent[] {
  const normalizedContent = normalizeText(content);
  const textIndexes = events
    .map((event, index) => (event.type === "text" ? index : -1))
    .filter((index) => index >= 0);
  const hidden = new Set<number>();

  for (let i = 0; i < textIndexes.length; i += 1) {
    const index = textIndexes[i];
    const text = normalizeText(events[index].data ?? "");
    for (let j = i + 1; j < textIndexes.length; j += 1) {
      const laterIndex = textIndexes[j];
      const later = normalizeText(events[laterIndex].data ?? "");
      if (isStreamingPrefixOfFinal(text, later) && text.length < later.length) {
        hidden.add(index);
        break;
      }
    }
    if (
      !hidden.has(index) &&
      normalizedContent &&
      isStreamingPrefixOfFinal(text, normalizedContent) &&
      text.length < normalizedContent.length
    ) {
      hidden.add(index);
    }
  }

  return events.filter((_, index) => !hidden.has(index));
}

function MessageTimeline({
  content,
  toolEvents,
  liveText,
  streaming,
}: {
  content?: string;
  toolEvents?: AgentToolEvent[];
  liveText?: string;
  streaming?: boolean;
}) {
  const rawEvents = toolEvents || [];
  const events = filterSupersededTextEvents(rawEvents, content || "");
  const hasToolCards = events.some(
    (e) => e.type === "tool" || e.type === "tool_result" || e.type === "info",
  );
  const hasTextEvent = events.some(
    (e) => e.type === "text" && (e.data || "").trim(),
  );
  const normalizedContent = normalizeText(content || "");
  const contentCoveredByTimeline = events.some(
    (event) =>
      event.type === "text" &&
      normalizeText(event.data ?? "") === normalizedContent,
  );
  // Show content even when earlier status text exists in the timeline
  // (e.g. "읽어오겠습니다" then tools then final summary in `content`).
  const showTrailingContent =
    !liveText &&
    normalizedContent.length > 0 &&
    !contentCoveredByTimeline;
  const showThinking =
    !!streaming && !liveText?.trim() && !showTrailingContent && !hasToolCards;

  if (!hasToolCards && !hasTextEvent && !liveText && !showTrailingContent) {
    if (streaming) {
      return (
        <div className="agent-streaming-indicator" aria-label="Thinking">
          Thinking
          <span className="agent-thinking-ellipsis" aria-hidden="true" />
        </div>
      );
    }
    return null;
  }

  return (
    <div className="agent-message-timeline">
      {events.map((event, index) => {
        if (event.type === "text") {
          const text = event.data || "";
          if (!text.trim()) return null;
          return (
            <div key={`text-${index}`} className="agent-message-bubble">
              <ReactMarkdown remarkPlugins={gfmRemarkPlugins}>{text}</ReactMarkdown>
            </div>
          );
        }
        return (
          <ToolCallCard
            key={`${event.type}-${event.toolUseId || index}`}
            event={event}
          />
        );
      })}
      {liveText ? (
        <div className="agent-message-bubble">
          <ReactMarkdown remarkPlugins={gfmRemarkPlugins}>{liveText}</ReactMarkdown>
        </div>
      ) : null}
      {showTrailingContent ? (
        <div className="agent-message-bubble">
          <ReactMarkdown remarkPlugins={gfmRemarkPlugins}>{content!}</ReactMarkdown>
        </div>
      ) : null}
      {showThinking ? (
        <div className="agent-streaming-indicator" aria-label="Thinking">
          Thinking
          <span className="agent-thinking-ellipsis" aria-hidden="true" />
        </div>
      ) : null}
    </div>
  );
}

export function AgentPanel({
  notePath,
  modelName,
  onClose,
  onNoteUpdated,
  onResizeStart,
  onResizeReset,
  resizing = false,
}: Props) {
  const [messages, setMessages] = useState<AgentMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [streamText, setStreamText] = useState("");
  const [streamEvents, setStreamEvents] = useState<AgentToolEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [note, setNote] = useState<AgentNoteChip | null>(null);
  /** Additional notes from Open agent while panel is already open (do not replace). */
  const [extraNotes, setExtraNotes] = useState<AgentNoteChip[]>([]);
  const bottomRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const streamTextRef = useRef("");
  const noteRef = useRef<AgentNoteChip | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  noteRef.current = note;
  sessionIdRef.current = sessionId;

  const flushLiveTextIntoEvents = useCallback(() => {
    const cleaned = stripVaultWriteMarkers(streamTextRef.current);
    streamTextRef.current = "";
    setStreamText("");
    if (!cleaned) return;
    setStreamEvents((prev) =>
      upsertStreamEvent(prev, { type: "text", data: cleaned }),
    );
  }, []);

  // Bind Open Agent session to the open note's durable note_id (conversation room).
  useEffect(() => {
    let cancelled = false;
    const path = (notePath || "").trim();
    if (!path) {
      setSessionId(null);
      setMessages([]);
      return;
    }
    void (async () => {
      let chip: AgentNoteChip;
      let noteId: string | null = null;
      try {
        const meta = await api.agentNoteMeta(path);
        if (cancelled) return;
        chip = { path: meta.path, name: meta.name, size: meta.size };
        noteId = meta.note_id || null;
      } catch {
        if (cancelled) return;
        const name = path.split("/").pop() || path;
        chip = { path, name, size: 0 };
      }
      setSessionId(noteId);
      setStreamText("");
      setStreamEvents([]);
      setError(null);
      const primary = noteRef.current;
      if (!primary || primary.path !== chip.path) {
        setNote(chip);
        setExtraNotes((prev) => prev.filter((n) => n.path !== chip.path));
      }

      // Restore transcript from SQLite (agentic-work style).
      if (noteId) {
        try {
          const hist = await api.agentMessages({ noteId });
          if (cancelled) return;
          setMessages(
            (hist.messages || []).map((m) => ({
              id: m.id,
              role: m.role,
              content: m.content,
              attachments:
                m.attachments && m.attachments.length > 0
                  ? m.attachments
                  : undefined,
              toolEvents:
                m.tool_events && m.tool_events.length > 0
                  ? m.tool_events
                  : undefined,
            })),
          );
        } catch {
          if (!cancelled) setMessages([]);
        }
      } else if (!cancelled) {
        setMessages([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [notePath]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: streaming ? "auto" : "smooth" });
  }, [messages, streamText, streamEvents, streaming]);

  const stop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setStreaming(false);
  }, []);

  const clearHistory = useCallback(async () => {
    if (clearing) return;
    const noteId = sessionIdRef.current;
    const path = (notePath || noteRef.current?.path || "").trim();
    if (!noteId && !path) {
      setMessages([]);
      setStreamText("");
      setStreamEvents([]);
      setError(null);
      return;
    }
    setClearing(true);
    stop();
    try {
      await api.clearAgentMessages({
        noteId: noteId || null,
        notePath: path || null,
      });
      setMessages([]);
      setStreamText("");
      setStreamEvents([]);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setClearing(false);
    }
  }, [clearing, notePath, stop]);

  const send = useCallback(
    async (payload: AgentSendPayload) => {
      const text = (payload.text || "").trim();
      const imagePaths = payload.imagePaths || [];
      const filePaths = [
        ...extraNotes.map((n) => n.path),
        ...(payload.filePaths || []),
      ];
      const roomPath = (notePath || note?.path || "").trim() || null;

      // Ensure session_id = note_id for this conversation room before invoke.
      let roomSessionId = sessionIdRef.current;
      if (roomPath && /\.md$/i.test(roomPath) && !roomSessionId) {
        try {
          const meta = await api.agentNoteMeta(roomPath);
          roomSessionId = meta.note_id || null;
          if (roomSessionId) setSessionId(roomSessionId);
        } catch {
          /* server will still bind note_id from note_path */
        }
      }

      // Selected note chip (Open agent) + extras + Load-files / images.
      const attachments: string[] = [];
      const seen = new Set<string>();
      for (const path of [roomPath, ...imagePaths, ...filePaths]) {
        const p = (path || "").trim();
        if (!p || seen.has(p)) continue;
        seen.add(p);
        attachments.push(p);
      }
      const display =
        text ||
        (attachments.length
          ? [
              imagePaths.length ? `이미지 ${imagePaths.length}개` : "",
              filePaths.length ? `파일 ${filePaths.length}개` : "",
              !text && note?.path && !imagePaths.length && !filePaths.length
                ? note.name
                : "",
            ]
              .filter(Boolean)
              .join(", ")
          : "");
      setError(null);
      const userMsg: AgentMessage = {
        id: newId(),
        role: "user",
        content: display || "(첨부)",
        attachments: attachments.length > 0 ? attachments : undefined,
      };
      setMessages((prev) => [...prev, userMsg]);
      setStreaming(true);
      setStreamText("");
      streamTextRef.current = "";
      setStreamEvents([]);

      const ac = new AbortController();
      abortRef.current = ac;

      const handlers: AgentChatHandlers = {
        onSession: (id) => setSessionId(id),
        onToken: (t) => {
          const cleaned = stripVaultWriteMarkers(t);
          streamTextRef.current = cleaned;
          setStreamText(cleaned);
        },
        onText: (data) => {
          setStreamEvents((prev) =>
            upsertStreamEvent(prev, { type: "text", data }),
          );
          streamTextRef.current = "";
          setStreamText("");
        },
        onTool: (event) => {
          flushLiveTextIntoEvents();
          setStreamEvents((prev) => upsertStreamEvent(prev, event));
        },
        onToolResult: (event) => {
          setStreamEvents((prev) => upsertStreamEvent(prev, event));
        },
        onNoteUpdated: (path) => onNoteUpdated?.(path),
        onDone: (result, id, toolEvents) => {
          if (id) setSessionId(id);
          const content =
            stripVaultWriteMarkers(result || "").trim() || "(응답 없음)";
          const cleanedEvents = (toolEvents || [])
            .map((ev) =>
              ev.type === "text"
                ? { ...ev, data: stripVaultWriteMarkers(ev.data || "") }
                : ev,
            )
            .filter((ev) => ev.type !== "text" || (ev.data || "").trim());
          setMessages((prev) => [
            ...prev,
            {
              id: newId(),
              role: "assistant",
              content,
              toolEvents: cleanedEvents.length > 0 ? cleanedEvents : undefined,
            },
          ]);
          streamTextRef.current = "";
          setStreamText("");
          setStreamEvents([]);
          setStreaming(false);
          abortRef.current = null;
        },
        onError: (msg) => {
          setError(msg);
          streamTextRef.current = "";
          setStreamText("");
          setStreamEvents([]);
          setStreaming(false);
          abortRef.current = null;
        },
      };

      try {
        await api.agentChat(
          {
            prompt: text,
            note_path: roomPath,
            session_id: roomSessionId,
            model_name: modelName || null,
            image_paths: imagePaths,
            file_paths: filePaths,
          },
          handlers,
          ac.signal,
        );
      } catch (err) {
        if (ac.signal.aborted) {
          setStreaming(false);
          setStreamText("");
          setStreamEvents([]);
          return;
        }
        setError(err instanceof Error ? err.message : String(err));
        setStreaming(false);
        setStreamText("");
        setStreamEvents([]);
        abortRef.current = null;
      }
    },
    [
      notePath,
      note?.path,
      note?.name,
      extraNotes,
      modelName,
      onNoteUpdated,
      flushLiveTextIntoEvents,
    ],
  );

  return (
    <aside className="agent-panel" aria-label="Open agent">
      {onResizeStart && (
        <div
          className={`agent-panel-resizer${resizing ? " is-active" : ""}`}
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize agent panel"
          title="Drag to resize · double-click to reset"
          onPointerDown={onResizeStart}
          onDoubleClick={onResizeReset}
        />
      )}
      <header className="agent-panel-header">
        <span className="agent-panel-title">Agent</span>
        <div className="agent-panel-header-actions">
          <button
            type="button"
            className={`icon-btn agent-panel-refresh${clearing ? " is-refreshing" : ""}`}
            data-tooltip="Clear chat"
            aria-label="대화 기록 지우기"
            title="대화 기록 지우기"
            disabled={clearing || streaming}
            onClick={() => void clearHistory()}
          >
            <RefreshIcon />
          </button>
          <button
            type="button"
            className="agent-panel-close"
            aria-label="에이전트 닫기"
            onClick={() => {
              stop();
              onClose();
            }}
          >
            ×
          </button>
        </div>
      </header>
      <div className="agent-chat-scroll">
        <div className="agent-chat-thread">
          {messages.length === 0 && !streaming && (
            <div className="agent-empty-state">
              <p>선택한 노트를 수정·요약하도록 요청하세요.</p>
              <p>인터넷 검색 · 목차 추가</p>
            </div>
          )}
          {messages.map((m) => (
            <div key={m.id} className={`agent-message-row ${m.role}`}>
              {m.role === "user" && m.attachments && m.attachments.length > 0 && (
                <MessageAttachments paths={m.attachments} />
              )}
              {m.role === "assistant" && m.toolEvents && m.toolEvents.length > 0 ? (
                <MessageTimeline content={m.content} toolEvents={m.toolEvents} />
              ) : (
                <div className="agent-message-bubble">
                  {m.role === "assistant" ? (
                    <ReactMarkdown remarkPlugins={gfmRemarkPlugins}>{m.content}</ReactMarkdown>
                  ) : (
                    m.content
                  )}
                </div>
              )}
            </div>
          ))}
          {streaming && (
            <div className="agent-message-row assistant">
              <MessageTimeline
                toolEvents={streamEvents}
                liveText={streamText}
                streaming
              />
            </div>
          )}
          {error && (
            <div className="agent-chat-error" role="alert">
              {error}
            </div>
          )}
          <div ref={bottomRef} />
        </div>
      </div>
      <AgentChatInput
        note={note}
        extraNotes={extraNotes}
        notePath={notePath}
        waiting={streaming}
        onRemoveNote={() => setNote(null)}
        onRemoveExtraNote={(path) =>
          setExtraNotes((prev) => prev.filter((n) => n.path !== path))
        }
        onSend={(payload) => void send(payload)}
        onStop={stop}
      />
    </aside>
  );
}
