import { useEffect, useRef, useState } from "react";
import type { MeetingLogController } from "../meetingLog/useMeetingLog";
import { DEFAULT_MEETING_TITLE, SPEAKERS } from "../meetingLog/config";
import { formatTime, speakerClass } from "../meetingLog/format";

const EMPTY_HINT =
  "회의 내용이 여기에 쌓입니다. 스크롤로 이전 내용을 볼 수 있습니다.";

type Props = {
  meeting: MeetingLogController;
  onClose: () => void;
  onSaveVault: () => void;
  onClearConfirm: () => Promise<boolean>;
};

export function MeetingLogSidebar({
  meeting,
  onClose,
  onSaveVault,
  onClearConfirm,
}: Props) {
  const {
    status,
    listening,
    batchBusy,
    autoSpeaker,
    pinnedSpeaker,
    view,
    setView,
    title,
    setTitle,
    savingVault,
    toggleListening,
    selectSpeakerMode,
    entries,
    batchEntries,
    interim,
    cycleSpeaker,
    recordedAt,
    hasDownloadableAudio,
    downloadAudio,
    pendingJob,
    recoverJobs,
    recoverOpen,
    setRecoverOpen,
    recoverBusy,
    resumePendingJob,
    loadRecoverableJobs,
    importRecoverableJob,
  } = meeting;

  const [editingTitle, setEditingTitle] = useState(false);
  const [draftTitle, setDraftTitle] = useState(title);
  const titleInputRef = useRef<HTMLInputElement>(null);
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!editingTitle) setDraftTitle(title);
  }, [title, editingTitle]);

  useEffect(() => {
    if (!editingTitle) return;
    const el = titleInputRef.current;
    if (!el) return;
    el.focus();
    el.select();
  }, [editingTitle]);

  useEffect(() => {
    const el = logRef.current;
    if (!el) return;
    const pin = () => {
      el.scrollTop = el.scrollHeight;
    };
    pin();
    requestAnimationFrame(() => requestAnimationFrame(pin));
  }, [entries, batchEntries, interim, view]);

  const canSendNote = batchEntries.some((entry) => String(entry.text || "").trim());

  const commitTitle = () => {
    const next = draftTitle.trim() || DEFAULT_MEETING_TITLE;
    setTitle(next);
    setDraftTitle(next);
    setEditingTitle(false);
  };

  const cancelTitleEdit = () => {
    setDraftTitle(title);
    setEditingTitle(false);
  };

  return (
    <div className="meeting-sidebar">
      <div className="sidebar-header meeting-sidebar-header">
        <span>회의 로그</span>
        <div className="meeting-head-actions">
          <button
            type="button"
            className="link-btn"
            disabled={!canSendNote || savingVault || listening || batchBusy}
            title={canSendNote ? "배치 기록을 노트로 저장" : "배치에 텍스트가 있을 때만 보낼 수 있습니다"}
            onClick={onSaveVault}
          >
            {savingVault ? "저장 중…" : "보내기"}
          </button>
          <button
            type="button"
            className="link-btn"
            disabled={!hasDownloadableAudio || listening || batchBusy}
            title={
              hasDownloadableAudio
                ? "녹음 파일을 WAV로 다운로드"
                : "녹음 파일이 없을 때는 다운로드할 수 없습니다"
            }
            onClick={downloadAudio}
          >
            다운로드
          </button>
          <button
            type="button"
            className="link-btn"
            onClick={() => {
              void onClearConfirm().then((ok) => {
                if (ok) meeting.clearLog();
              });
            }}
          >
            지우기
          </button>
          <button
            type="button"
            className="link-btn"
            onClick={() => {
              meeting.stopListening({ autoBatch: false });
              onClose();
            }}
          >
            닫기
          </button>
        </div>
      </div>

      <div className="meeting-sidebar-body">
        <div className="meeting-sidebar-controls">
          <div className="meeting-title-field">
            {editingTitle ? (
              <input
                ref={titleInputRef}
                type="text"
                className="meeting-title-input"
                value={draftTitle}
                placeholder={DEFAULT_MEETING_TITLE}
                maxLength={60}
                aria-label="회의 제목 수정"
                onChange={(e) => setDraftTitle(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    commitTitle();
                  } else if (e.key === "Escape") {
                    e.preventDefault();
                    cancelTitleEdit();
                  }
                }}
                onBlur={cancelTitleEdit}
              />
            ) : (
              <button
                type="button"
                className="meeting-title-display"
                title="클릭하여 제목 수정"
                aria-label="회의 제목 수정"
                onClick={() => {
                  setDraftTitle(title);
                  setEditingTitle(true);
                }}
              >
                {title.trim() || DEFAULT_MEETING_TITLE}
              </button>
            )}
          </div>

          <p className="meeting-hint">
            마이크를 누르면 실시간으로 기록합니다. 녹음이 끝나면 전체 음성을 AWS
            Transcribe로 다시 보내 회의 내용을 정리합니다.
          </p>

          <div className="meeting-mic-wrap">
            <button
              type="button"
              className={`meeting-mic-btn${listening ? " recording" : ""}`}
              aria-label={listening ? "회의 기록 중지" : "회의 기록 시작"}
              title={listening ? "회의 기록 중지" : "회의 기록 시작"}
              onClick={toggleListening}
              disabled={batchBusy}
            >
              <svg className="meeting-mic-icon" viewBox="0 0 24 24" aria-hidden="true">
                <path
                  fill="currentColor"
                  d="M12 14a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v5a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.9V21h2v-3.1A7 7 0 0 0 19 11h-2z"
                />
              </svg>
            </button>
          </div>

          <p className="meeting-status">{status}</p>

          <div className="meeting-recover-row">
            {pendingJob?.jobName ? (
              <button
                type="button"
                className="meeting-batch-btn"
                disabled={listening || batchBusy || recoverBusy}
                onClick={() => void resumePendingJob()}
                title="대기 중이던 Transcribe 작업을 이어서 확인합니다"
              >
                {batchBusy ? "확인 중…" : "대기 중 변환 이어받기"}
              </button>
            ) : null}
            <button
              type="button"
              className="meeting-batch-btn"
              disabled={listening || batchBusy || recoverBusy}
              onClick={() => {
                if (recoverOpen) {
                  setRecoverOpen(false);
                  return;
                }
                void loadRecoverableJobs();
              }}
              title="이전에 완료된 배치 변환을 불러옵니다"
            >
              {recoverBusy && !batchBusy
                ? "목록 불러오는 중…"
                : recoverOpen
                  ? "변환 목록 닫기"
                  : "변환 결과 불러오기"}
            </button>
          </div>

          {recoverOpen ? (
            <div className="meeting-recover-list" role="list" aria-label="이전 변환 목록">
              {recoverJobs.length === 0 ? (
                <p className="meeting-recover-empty">표시할 이전 변환이 없습니다.</p>
              ) : (
                recoverJobs.map((job) => {
                  const created = job.createdAt
                    ? new Date(job.createdAt).toLocaleString("ko-KR")
                    : "시간 미상";
                  const label =
                    job.status === "COMPLETED"
                      ? "완료"
                      : job.status === "FAILED"
                        ? "실패"
                        : job.status === "IN_PROGRESS" || job.status === "QUEUED"
                          ? "진행 중"
                          : job.status;
                  return (
                    <div key={job.jobName} className="meeting-recover-item" role="listitem">
                      <div className="meeting-recover-meta">
                        <span className="meeting-recover-when">{created}</span>
                        <span className={`meeting-recover-status status-${String(job.status || "").toLowerCase()}`}>
                          {label}
                        </span>
                      </div>
                      <button
                        type="button"
                        className="link-btn"
                        disabled={
                          listening ||
                          batchBusy ||
                          recoverBusy ||
                          job.status === "FAILED"
                        }
                        onClick={() => void importRecoverableJob(job.jobName)}
                      >
                        {job.status === "FAILED" ? "불러올 수 없음" : "가져오기"}
                      </button>
                    </div>
                  );
                })
              )}
            </div>
          ) : null}

          <div className="meeting-speaker-row" role="group" aria-label="화자 선택">
            <span className="meeting-speaker-label">화자</span>
            {SPEAKERS.map((s) => (
              <button
                key={s}
                type="button"
                className={`meeting-speaker-chip${
                  !autoSpeaker && pinnedSpeaker === s ? " active" : ""
                }`}
                data-speaker={s}
                aria-pressed={!autoSpeaker && pinnedSpeaker === s}
                onClick={() => selectSpeakerMode(s)}
              >
                {s}
              </button>
            ))}
            <button
              type="button"
              className={`meeting-speaker-chip auto${autoSpeaker ? " active" : ""}`}
              aria-pressed={autoSpeaker}
              onClick={() => selectSpeakerMode("auto")}
            >
              자동
            </button>
          </div>

          <div className="meeting-controls">
            <div className="meeting-view-seg" role="group" aria-label="기록 보기">
              {(
                [
                  ["live", "실시간"],
                  ["batch", "배치"],
                ] as const
              ).map(([id, label]) => (
                <button
                  key={id}
                  type="button"
                  className={`meeting-view-btn${view === id ? " active" : ""}`}
                  aria-pressed={view === id}
                  onClick={() => setView(id)}
                >
                  {label}
                </button>
              ))}
            </div>
          </div>
        </div>

        <div ref={logRef} className="meeting-log" aria-live="polite">
          {view === "batch" ? (
            batchEntries.length === 0 ? (
              <p className="meeting-empty">
                아직 배치 변환 결과가 없습니다. 녹음을 멈추면 자동으로 정리됩니다.
              </p>
            ) : (
              batchEntries.map((entry, index) => (
                <LogCard
                  key={`batch-${index}`}
                  speaker={entry.speaker}
                  text={entry.text}
                  time={
                    entry.start != null
                      ? formatTime(
                          recordedAt.getTime() + Math.max(0, entry.start) * 1000,
                        )
                      : undefined
                  }
                />
              ))
            )
          ) : entries.length === 0 && !interim ? (
            <p className="meeting-empty">{EMPTY_HINT}</p>
          ) : (
            <>
              {entries.map((entry) => (
                <LogCard
                  key={entry.id}
                  speaker={entry.speaker}
                  text={entry.text}
                  time={formatTime(entry.at)}
                  onCycleSpeaker={() => cycleSpeaker(entry.id)}
                />
              ))}
              {interim && (
                <LogCard
                  speaker={interim.speaker}
                  text={interim.text}
                  time="인식 중"
                  interim
                />
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function LogCard({
  speaker,
  text,
  time,
  interim,
  onCycleSpeaker,
}: {
  speaker: string;
  text: string;
  time?: string;
  interim?: boolean;
  onCycleSpeaker?: () => void;
}) {
  return (
    <article className={`meeting-log-item${interim ? " interim" : ""}`}>
      <button
        type="button"
        className={`meeting-speaker-badge ${speakerClass(speaker)}`}
        title={onCycleSpeaker ? "화자 바꾸기" : undefined}
        disabled={!onCycleSpeaker}
        onClick={onCycleSpeaker}
      >
        {speaker}
      </button>
      <div className="meeting-log-body">
        <p className="meeting-log-text">{text}</p>
        {time != null && <time className="meeting-log-time">{time}</time>}
      </div>
    </article>
  );
}
