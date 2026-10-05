import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, type ClearingDeleteStatus, type ClearingItem, type ClearingScan } from "../api";
import { ConfirmDialog, type ConfirmOptions } from "./ConfirmDialog";

type DeleteActivity = {
  busy: boolean;
  message: string | null;
  progress: ClearingDeleteStatus["progress"];
};

type Props = {
  open: boolean;
  scan: ClearingScan | null;
  onClose: () => void;
  onDeleted: (paths: string[]) => void;
  onRescan: () => void;
  onDeleteActivity?: (info: DeleteActivity) => void;
  onDeleteBackground?: () => void;
};

const SKIP_CONFIRM_PREFIX = "ob-note:skip-confirm:";

function shouldSkipConfirm(key: string): boolean {
  try {
    return localStorage.getItem(`${SKIP_CONFIRM_PREFIX}${key}`) === "1";
  } catch {
    return false;
  }
}

function setSkipConfirm(key: string): void {
  try {
    localStorage.setItem(`${SKIP_CONFIRM_PREFIX}${key}`, "1");
  } catch {
    /* ignore */
  }
}

function formatSize(size: number): string {
  if (!Number.isFinite(size) || size < 0) return "—";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function folderLabel(folder: string): string {
  return folder || "vault 루트";
}

function openInNewTab(path: string) {
  window.open(api.viewUrl(path), "_blank", "noopener,noreferrer");
}

function pathKey(path: string): string {
  return path.normalize("NFC").replaceAll("\\", "/").replace(/^\/+/, "").toLocaleLowerCase();
}

export function ClearingListModal({
  open,
  scan: scanProp,
  onClose,
  onDeleted,
  onRescan,
  onDeleteActivity,
  onDeleteBackground,
}: Props) {
  const [scan, setScan] = useState<ClearingScan | null>(scanProp);
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteActivity, setDeleteActivity] = useState<DeleteActivity | null>(null);
  const reportedDeletes = useRef(new Set<string>());
  const deleteJobId = useRef<string | null>(null);
  const onDeletedRef = useRef(onDeleted);
  const onDeleteActivityRef = useRef(onDeleteActivity);
  onDeletedRef.current = onDeleted;
  onDeleteActivityRef.current = onDeleteActivity;
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busyPaths, setBusyPaths] = useState<Set<string>>(new Set());
  const [confirmState, setConfirmState] = useState<{
    options: ConfirmOptions;
    resolve: (ok: boolean) => void;
  } | null>(null);

  const askConfirm = useCallback((options: ConfirmOptions) => {
    if (options.dontAskAgainKey && shouldSkipConfirm(options.dontAskAgainKey)) {
      return Promise.resolve(true);
    }
    return new Promise<boolean>((resolve) => {
      setConfirmState({ options, resolve });
    });
  }, []);

  useEffect(() => {
    setScan(scanProp);
    setSelected(new Set());
    setError(null);
  }, [scanProp]);

  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape" && !confirmState) onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose, confirmState]);

  const items = scan?.items ?? [];
  const allSelected = items.length > 0 && items.every((item) => selected.has(item.path));
  const selectedItems = useMemo(
    () => items.filter((item) => selected.has(item.path)),
    [items, selected],
  );
  const busy = busyPaths.size > 0 || deleting;

  useEffect(() => {
    if (!deleting) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    async function poll() {
      try {
        const next = await api.getClearingDeleteStatus();
        if (cancelled) return;
        if (
          !deleteJobId.current ||
          (next.job_id && next.job_id !== deleteJobId.current)
        ) {
          timer = setTimeout(poll, 200);
          return;
        }
        const active =
          Boolean(next.busy) || next.status === "queued" || next.status === "running";
        const activity: DeleteActivity = {
          busy: active,
          message: next.message ?? null,
          progress: next.progress ?? null,
        };
        setDeleteActivity(activity);
        onDeleteActivityRef.current?.(activity);
        const fresh = (next.deleted || []).filter((path) => !reportedDeletes.current.has(path));
        if (fresh.length) {
          for (const path of fresh) reportedDeletes.current.add(path);
          dropItems(fresh);
          onDeletedRef.current(fresh);
        }
        if (active) {
          timer = setTimeout(poll, 400);
          return;
        }
        setDeleting(false);
        if (next.errors?.length) {
          setError(next.errors.map((entry) => `${entry.path}: ${entry.error}`).join("\n"));
        }
      } catch {
        if (cancelled) return;
        timer = setTimeout(poll, 1500);
      }
    }

    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [deleting]);

  function toggleAll() {
    setSelected(allSelected ? new Set() : new Set(items.map((item) => item.path)));
  }

  function toggleOne(path: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }

  function dropItems(paths: string[]) {
    const drop = new Set(paths.map(pathKey));
    setScan((prev) =>
      prev
        ? {
            ...prev,
            items: prev.items.filter((item) => !drop.has(pathKey(item.path))),
          }
        : prev,
    );
    setSelected((prev) => {
      const next = new Set<string>();
      for (const path of prev) {
        if (!drop.has(pathKey(path))) next.add(path);
      }
      return next;
    });
  }

  async function keepPaths(paths: string[]) {
    if (!paths.length || busy) return;
    setBusyPaths(new Set(paths));
    setError(null);
    try {
      const res = await api.keepClearing(paths);
      dropItems(paths);
      setScan((prev) =>
        prev
          ? {
              ...prev,
              kept_count: res.kept.length,
              kept_paths: res.kept,
            }
          : prev,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyPaths(new Set());
    }
  }

  async function deletePaths(paths: string[]) {
    if (!paths.length || busy) return;
    const label = paths.length === 1 ? paths[0].split("/").pop() || paths[0] : null;
    const ok = await askConfirm({
      title: "미디어 삭제",
      message: label ? `“${label}” 파일을 삭제할까요?` : `${paths.length}개 파일을 삭제할까요?`,
      detail: "vault에서 바로 제거되며 되돌릴 수 없습니다.",
      confirmLabel: "삭제",
      cancelLabel: "취소",
      danger: true,
      dontAskAgainKey: "clearing-delete",
    });
    if (!ok) return;
    setError(null);
    reportedDeletes.current = new Set();
    deleteJobId.current = null;
    setDeleting(true);
    const queued: DeleteActivity = {
      busy: true,
      message: `삭제를 시작합니다… (0/${paths.length})`,
      progress: { pct: 0, phase: "delete", file_i: 0, file_n: paths.length },
    };
    setDeleteActivity(queued);
    onDeleteActivityRef.current?.(queued);
    try {
      const res = await api.startClearingDelete(paths);
      deleteJobId.current = res.job_id ?? null;
      if (res.status === "error" && !res.busy) {
        setDeleting(false);
        setDeleteActivity(null);
        setError(res.message || res.error || "삭제를 시작하지 못했습니다.");
        onDeleteActivityRef.current?.({ busy: false, message: res.message ?? null, progress: null });
      }
    } catch (err) {
      setDeleting(false);
      deleteJobId.current = null;
      setDeleteActivity(null);
      setError(err instanceof Error ? err.message : String(err));
      onDeleteActivityRef.current?.({ busy: false, message: null, progress: null });
    }
  }

  function hideToBackground() {
    onDeleteBackground?.();
    onClose();
  }

  async function restoreKept() {
    if (busy || !scan?.kept_count) return;
    setBusyPaths(new Set(["*"]));
    setError(null);
    try {
      await api.unkeepClearing([], true);
      onRescan();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusyPaths(new Set());
    }
  }

  if (!open) return null;

  return createPortal(
    <>
      <div
        className="modal-backdrop"
        role="dialog"
        aria-modal="true"
        aria-labelledby="clearing-list-title"
        onMouseDown={(e) => {
          if (confirmState) return;
          if (e.target === e.currentTarget) {
            if (deleting) hideToBackground();
            else onClose();
          }
        }}
      >
        <div className="modal-card clearing-list-modal" onMouseDown={(e) => e.stopPropagation()}>
          <div className="modal-header">
            <h2 id="clearing-list-title" className="modal-title">
              Clearing
            </h2>
            <div className="compress-list-header-actions">
              <button
                type="button"
                className="modal-btn modal-btn-cancel"
                disabled={busy}
                onClick={onRescan}
              >
                다시 검사
              </button>
              <button
                type="button"
                className="modal-close"
                aria-label="Close"
                onClick={() => (deleting ? hideToBackground() : onClose())}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M6 6l12 12M18 6L6 18" />
                </svg>
              </button>
            </div>
          </div>

          <p className="clearing-list-lead">
            png, jpg, jpeg, wav, mp3, mp4가 같은 폴더의 마크다운에서 파일 이름(확장자 포함)으로
            참조되지 않으면 여기에 표시됩니다. 유지는 다음 검사에서 건너뛰고, 삭제는 vault에서
            제거합니다.
          </p>

          {deleting && deleteActivity ? <ClearingDeleteProgress activity={deleteActivity} onBackground={hideToBackground} /> : null}

          <div className="modal-body share-list-body">
            {items.length === 0 ? (
              error ? (
                <p className="share-list-error" role="alert">
                  {error}
                </p>
              ) : (
                <p className="share-list-muted">
                  참조되지 않은 미디어 파일이 없습니다.
                  {scan
                    ? ` 미디어 ${scan.media_count}개 중 ${scan.referenced_count}개가 참조되고 있습니다.`
                    : ""}
                </p>
              )
            ) : (
              <>
                {error ? (
                  <p className="share-list-error" role="alert">
                    {error}
                  </p>
                ) : null}
                <div className="clearing-list-toolbar">
                  <label className="clearing-list-check">
                    <input
                      type="checkbox"
                      checked={allSelected}
                      onChange={toggleAll}
                      disabled={busy}
                    />
                    <span>
                      {selectedItems.length
                        ? `${selectedItems.length}개 선택`
                        : `${items.length}개 미참조`}
                    </span>
                  </label>
                  <div className="share-doc-list-actions">
                    <button
                      type="button"
                      className="share-doc-list-btn"
                      disabled={busy || selectedItems.length === 0}
                      onClick={() => void keepPaths(selectedItems.map((item) => item.path))}
                    >
                      선택 유지
                    </button>
                    <button
                      type="button"
                      className="share-doc-list-btn share-doc-list-btn-danger"
                      disabled={busy || selectedItems.length === 0}
                      onClick={() => void deletePaths(selectedItems.map((item) => item.path))}
                    >
                      선택 삭제
                    </button>
                  </div>
                </div>
                <ul className="share-doc-list">
                  {items.map((item) => (
                    <ClearingRow
                      key={item.path}
                      item={item}
                      checked={selected.has(item.path)}
                      busy={busy}
                      onToggle={() => toggleOne(item.path)}
                      onView={() => openInNewTab(item.path)}
                      onKeep={() => void keepPaths([item.path])}
                      onDelete={() => void deletePaths([item.path])}
                    />
                  ))}
                </ul>
              </>
            )}
            {scan && scan.kept_count > 0 ? (
              <p className="clearing-kept-note">
                유지한 파일 {scan.kept_count}개는 이번 목록에서 빠졌습니다.{" "}
                <button
                  type="button"
                  className="clearing-kept-link"
                  disabled={busy}
                  onClick={() => void restoreKept()}
                >
                  다시 검사 대상에 넣기
                </button>
              </p>
            ) : null}
          </div>
        </div>
      </div>
      <ConfirmDialog
        open={!!confirmState}
        options={confirmState?.options ?? null}
        onConfirm={(dontAskAgain) => {
          const key = confirmState?.options.dontAskAgainKey;
          if (dontAskAgain && key) setSkipConfirm(key);
          confirmState?.resolve(true);
          setConfirmState(null);
        }}
        onCancel={() => {
          confirmState?.resolve(false);
          setConfirmState(null);
        }}
      />
    </>,
    document.body,
  );
}

function ClearingDeleteProgress({
  activity,
  onBackground,
}: {
  activity: DeleteActivity;
  onBackground: () => void;
}) {
  const pct =
    typeof activity.progress?.pct === "number" && Number.isFinite(activity.progress.pct)
      ? Math.max(0, Math.min(100, Math.round(activity.progress.pct)))
      : 0;
  const count =
    typeof activity.progress?.file_i === "number" &&
    typeof activity.progress?.file_n === "number" &&
    activity.progress.file_n > 0
      ? `${activity.progress.file_i}/${activity.progress.file_n}`
      : null;
  const fileName = activity.progress?.file?.trim() || null;

  return (
    <div className="clearing-delete-progress" role="status" aria-live="polite">
      <div className="sync-progress-spinner" aria-hidden="true" />
      <div className="clearing-delete-progress-body">
        <p className="clearing-delete-progress-message">
          {activity.message || "삭제 중…"}
          {count ? ` · ${count}` : ""}
        </p>
        {fileName ? (
          <p className="clearing-delete-progress-file" title={fileName}>
            {fileName}
          </p>
        ) : null}
        <div
          className="sync-progress-bar"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={pct}
        >
          <div className="sync-progress-bar-fill" style={{ width: `${pct}%` }} />
        </div>
      </div>
      <button type="button" className="share-doc-list-btn" onClick={onBackground}>
        백그라운드
      </button>
    </div>
  );
}

function ClearingRow({
  item,
  checked,
  busy,
  onToggle,
  onView,
  onKeep,
  onDelete,
}: {
  item: ClearingItem;
  checked: boolean;
  busy: boolean;
  onToggle: () => void;
  onView: () => void;
  onKeep: () => void;
  onDelete: () => void;
}) {
  return (
    <li className="share-doc-list-item">
      <label className="clearing-list-check">
        <input type="checkbox" checked={checked} disabled={busy} onChange={onToggle} />
      </label>
      <div className="share-doc-list-meta">
        <span className="share-doc-list-name" title={item.path}>
          {item.name}
        </span>
        <span className="share-doc-list-sub" title={item.path}>
          {folderLabel(item.folder)} · {formatSize(item.size)}
        </span>
      </div>
      <div className="share-doc-list-actions">
        <button type="button" className="share-doc-list-btn" disabled={busy} onClick={onView}>
          보기
        </button>
        <button type="button" className="share-doc-list-btn" disabled={busy} onClick={onKeep}>
          유지
        </button>
        <button
          type="button"
          className="share-doc-list-btn share-doc-list-btn-danger"
          disabled={busy}
          onClick={onDelete}
        >
          삭제
        </button>
      </div>
    </li>
  );
}
