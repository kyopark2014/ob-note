import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { api, type ShareEntry } from "../api";
import {
  rememberSkipConfirm,
  shouldSkipConfirm,
} from "../uiPrefs";
import {
  ConfirmDialog,
  type ConfirmOptions,
} from "./ConfirmDialog";

type Props = {
  open: boolean;
  onClose: () => void;
};

function formatSharedAt(value: number | undefined): string {
  if (value == null || !Number.isFinite(value) || value <= 0) return "—";
  const date = new Date(value * 1000);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("ko-KR", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function openInNewTab(url: string) {
  window.open(url, "_blank", "noopener,noreferrer");
}

function shareAbsoluteUrl(entry: ShareEntry): string {
  if (entry.url && entry.url.startsWith("http")) return entry.url;
  return `${window.location.origin}${entry.url_path}`;
}

export function SharedListModal({ open, onClose }: Props) {
  const [shares, setShares] = useState<ShareEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deletingToken, setDeletingToken] = useState<string | null>(null);
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
    if (!open) return;
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const data = await api.listShares();
        if (!cancelled) setShares(data.shares || []);
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape" && !confirmState) onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose, confirmState]);

  async function deleteShare(entry: ShareEntry) {
    const ok = await askConfirm({
      title: "Delete public link",
      message: `Are you sure you want to delete the public link for “${entry.title}”?`,
      detail: "This link will no longer open. This cannot be undone.",
      confirmLabel: "Delete",
      cancelLabel: "Cancel",
      danger: true,
      dontAskAgainKey: "delete-share",
    });
    if (!ok) return;
    setDeletingToken(entry.token);
    setError(null);
    try {
      await api.deleteShare(entry.token);
      setShares((prev) => prev.filter((s) => s.token !== entry.token));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setDeletingToken(null);
    }
  }

  if (!open) return null;

  return createPortal(
    <>
      <div
        className="modal-backdrop"
        role="dialog"
        aria-modal="true"
        aria-labelledby="shared-list-title"
        onMouseDown={(e) => {
          if (confirmState) return;
          if (e.target === e.currentTarget) onClose();
        }}
      >
        <div className="modal-card share-list-modal" onMouseDown={(e) => e.stopPropagation()}>
          <div className="modal-header">
            <h2 id="shared-list-title" className="modal-title">
              Shared List
            </h2>
            <button type="button" className="modal-close" aria-label="Close" onClick={onClose}>
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M6 6l12 12M18 6L6 18" />
              </svg>
            </button>
          </div>

          <div className="modal-body share-list-body">
            {loading ? (
              <p className="share-list-muted">공유 목록을 불러오는 중…</p>
            ) : shares.length === 0 ? (
              error ? (
                <p className="share-list-error" role="alert">
                  {error}
                </p>
              ) : (
                <p className="share-list-muted">
                  공개 공유 중인 항목이 없습니다. 노트·파일·폴더 우클릭 → Share public
                  link로 추가하세요.
                </p>
              )
            ) : (
              <>
                {error ? (
                  <p className="share-list-error" role="alert">
                    {error}
                  </p>
                ) : null}
                <ul className="share-doc-list">
                  {shares.map((entry) => {
                    const isDeleting = deletingToken === entry.token;
                    const url = shareAbsoluteUrl(entry);
                    const kind =
                      entry.type === "folder"
                        ? "Folder"
                        : entry.type === "file"
                          ? "File"
                          : "Note";
                    return (
                      <li key={entry.token} className="share-doc-list-item">
                        <div className="share-doc-list-meta">
                          <span className="share-doc-list-name" title={entry.title}>
                            {entry.title}
                            <span className="share-doc-list-kind"> · {kind}</span>
                          </span>
                          <span className="share-doc-list-sub" title={url}>
                            {formatSharedAt(entry.created_at)} · {url}
                          </span>
                        </div>
                        <div className="share-doc-list-actions">
                          <button
                            type="button"
                            className="share-doc-list-btn share-doc-list-btn-success"
                            disabled={isDeleting}
                            title="공개 링크 열기"
                            onClick={() => openInNewTab(url)}
                          >
                            Link
                          </button>
                          <button
                            type="button"
                            className="share-doc-list-btn share-doc-list-btn-danger"
                            disabled={isDeleting}
                            title="공개 링크 삭제"
                            onClick={() => void deleteShare(entry)}
                          >
                            {isDeleting ? "삭제 중…" : "삭제"}
                          </button>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              </>
            )}
          </div>

          <div className="modal-footer">
            <span />
            <div className="modal-actions">
              <button type="button" className="modal-btn modal-btn-confirm" onClick={onClose}>
                닫기
              </button>
            </div>
          </div>
        </div>
      </div>

      <ConfirmDialog
        open={!!confirmState}
        options={confirmState?.options ?? null}
        onConfirm={(dontAskAgain) => {
          const key = confirmState?.options.dontAskAgainKey;
          if (dontAskAgain && key) rememberSkipConfirm(key);
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
