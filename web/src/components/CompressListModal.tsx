import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { api, type CompressListItem } from "../api";
import { ConfirmDialog, type ConfirmOptions } from "./ConfirmDialog";

type Props = {
  open: boolean;
  onClose: () => void;
  onStart: () => void;
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

function formatWhen(value: number | null | undefined): string {
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

function itemTitle(item: CompressListItem): string {
  if (item.scope === "vault" || !item.path) return "vault 전체";
  return item.path.split("/").pop() || item.path;
}

export function CompressListModal({ open, onClose, onStart }: Props) {
  const [items, setItems] = useState<CompressListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [refreshingId, setRefreshingId] = useState<string | null>(null);
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

  const loadItems = useCallback(async () => {
    const data = await api.listCompressItems();
    setItems(data.items || []);
  }, []);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        await loadItems();
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, loadItems]);

  const processing = items.some((item) => item.status === "Processing");

  useEffect(() => {
    if (!open || !processing) return;
    let cancelled = false;
    const timer = setInterval(() => {
      void api
        .listCompressItems()
        .then((data) => {
          if (!cancelled) setItems(data.items || []);
        })
        .catch(() => {
          /* keep the last list while a poll fails */
        });
    }, 1500);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [open, processing]);

  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape" && !confirmState) onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose, confirmState]);

  async function deleteItem(item: CompressListItem) {
    const ok = await askConfirm({
      title: "Delete archive",
      message: `“${item.zip_name || itemTitle(item)}” 압축을 삭제할까요?`,
      detail: "목록과 압축 파일이 함께 삭제됩니다.",
      confirmLabel: "삭제",
      cancelLabel: "취소",
      danger: true,
      dontAskAgainKey: "delete-compress",
    });
    if (!ok) return;
    setDeletingId(item.id);
    setError(null);
    try {
      await api.deleteCompressItem(item.id);
      setItems((prev) => prev.filter((entry) => entry.id !== item.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setDeletingId(null);
    }
  }

  async function refreshLink(item: CompressListItem) {
    setRefreshingId(item.id);
    setError(null);
    try {
      const res = await api.refreshCompressItem(item.id);
      setItems((prev) => prev.map((entry) => (entry.id === item.id ? res.item : entry)));
      if (res.item.url) window.open(res.item.url, "_blank", "noopener,noreferrer");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRefreshingId(null);
    }
  }

  if (!open) return null;

  return createPortal(
    <>
      <div
        className="modal-backdrop"
        role="dialog"
        aria-modal="true"
        aria-labelledby="compress-list-title"
        onMouseDown={(e) => {
          if (confirmState) return;
          if (e.target === e.currentTarget) onClose();
        }}
      >
        <div className="modal-card compress-list-modal" onMouseDown={(e) => e.stopPropagation()}>
          <div className="modal-header">
            <h2 id="compress-list-title" className="modal-title">
              Compress
            </h2>
            <div className="compress-list-header-actions">
              <button
                type="button"
                className="modal-btn modal-btn-confirm"
                disabled={processing}
                title="vault 전체를 압축합니다. .vault 폴더는 제외됩니다."
                onClick={onStart}
              >
                시작하기
              </button>
              <button type="button" className="modal-close" aria-label="Close" onClick={onClose}>
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M6 6l12 12M18 6L6 18" />
                </svg>
              </button>
            </div>
          </div>

          <div className="modal-body share-list-body">
            {loading ? (
              <p className="share-list-muted">압축 목록을 불러오는 중…</p>
            ) : items.length === 0 ? (
              error ? (
                <p className="share-list-error" role="alert">
                  {error}
                </p>
              ) : (
                <p className="share-list-muted">
                  압축한 항목이 없습니다. 시작하기를 누르면 vault 전체가 압축됩니다.
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
                  {items.map((item) => {
                    const busy = deletingId === item.id || refreshingId === item.id;
                    const processingItem = item.status === "Processing";
                    const fileLabel =
                      item.progress?.file_n
                        ? `${item.progress.file || item.message || "압축 중"} (${item.progress.file_i || 0}/${item.progress.file_n})`
                        : item.message;
                    return (
                      <li key={item.id} className="share-doc-list-item">
                        <div className="share-doc-list-meta">
                          <span className="share-doc-list-name" title={item.zip_name || itemTitle(item)}>
                            {itemTitle(item)}
                            <span className={`compress-status compress-status-${item.status.toLowerCase()}`}>
                              {item.status}
                            </span>
                          </span>
                          <span className="share-doc-list-sub" title={item.backup_path || item.s3_key || ""}>
                            생성 {formatWhen(item.created_at)} · 만료 {formatWhen(item.expires_at)}
                          </span>
                          <span className="share-doc-list-sub" title={item.s3_key || ""}>
                            {item.s3_key || item.backup_path || item.zip_name}
                          </span>
                          {processingItem && fileLabel ? (
                            <span className="share-doc-list-sub">{fileLabel}</span>
                          ) : null}
                          {item.status === "Failed" && item.error ? (
                            <span className="share-list-error">{item.error}</span>
                          ) : null}
                        </div>
                        <div className="share-doc-list-actions">
                          {item.status === "Completed" && item.url ? (
                            <button
                              type="button"
                              className="share-doc-list-btn share-doc-list-btn-success"
                              disabled={busy}
                              onClick={() => window.open(item.url || "", "_blank", "noopener,noreferrer")}
                            >
                              다운로드
                            </button>
                          ) : null}
                          {item.status === "Expired" || item.status === "Failed" ? (
                            <button
                              type="button"
                              className="share-doc-list-btn"
                              disabled={busy}
                              title="1시간 동안 유효한 다운로드 링크를 다시 만듭니다."
                              onClick={() => void refreshLink(item)}
                            >
                              {refreshingId === item.id ? "만드는 중…" : "링크 다시 받기"}
                            </button>
                          ) : null}
                          <button
                            type="button"
                            className="share-doc-list-btn share-doc-list-btn-danger"
                            disabled={busy || processingItem}
                            title={processingItem ? "압축이 끝난 뒤 삭제할 수 있습니다." : "압축 파일 삭제"}
                            onClick={() => void deleteItem(item)}
                          >
                            {deletingId === item.id ? "삭제 중…" : "삭제"}
                          </button>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              </>
            )}
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
