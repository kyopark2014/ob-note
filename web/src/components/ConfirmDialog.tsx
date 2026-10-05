import { useEffect, useId, useState } from "react";

export type ConfirmOptions = {
  title: string;
  message: string;
  detail?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  /** localStorage key suffix; when set, shows "Don't ask again" */
  dontAskAgainKey?: string;
};

type Props = {
  open: boolean;
  options: ConfirmOptions | null;
  onConfirm: (dontAskAgain: boolean) => void;
  onCancel: () => void;
};

export function ConfirmDialog({ open, options, onConfirm, onCancel }: Props) {
  const titleId = useId();
  const [dontAsk, setDontAsk] = useState(false);

  useEffect(() => {
    if (open) setDontAsk(false);
  }, [open, options?.title, options?.message]);

  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        onCancel();
      } else if (e.key === "Enter") {
        e.preventDefault();
        onConfirm(dontAsk);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, dontAsk, onCancel, onConfirm]);

  if (!open || !options) return null;

  const confirmLabel = options.confirmLabel ?? "Confirm";
  const cancelLabel = options.cancelLabel ?? "Cancel";

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onCancel}>
      <div
        className="modal-card"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h2 id={titleId} className="modal-title">
            {options.title}
          </h2>
          <button type="button" className="modal-close" aria-label="Close" onClick={onCancel}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M6 6l12 12M18 6L6 18" />
            </svg>
          </button>
        </div>

        <div className="modal-body">
          <p className="modal-message">{options.message}</p>
          {options.detail ? <p className="modal-detail">{options.detail}</p> : null}
        </div>

        <div className="modal-footer">
          {options.dontAskAgainKey ? (
            <label className="modal-dont-ask">
              <input
                type="checkbox"
                checked={dontAsk}
                onChange={(e) => setDontAsk(e.target.checked)}
              />
              <span>Don&apos;t ask again</span>
            </label>
          ) : (
            <span />
          )}
          <div className="modal-actions">
            <button type="button" className="modal-btn modal-btn-cancel" onClick={onCancel}>
              {cancelLabel}
            </button>
            <button
              type="button"
              className={`modal-btn modal-btn-confirm${options.danger ? " danger" : ""}`}
              onClick={() => onConfirm(dontAsk)}
              autoFocus
            >
              {confirmLabel}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

type AlertLink = {
  href: string;
  label: string;
};

type AlertProps = {
  open: boolean;
  title?: string;
  message: string;
  link?: AlertLink | null;
  onClose: () => void;
};

export function AlertDialog({
  open,
  title = "Notice",
  message,
  link = null,
  onClose,
}: AlertProps) {
  const titleId = useId();

  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape" || e.key === "Enter") {
        e.preventDefault();
        onClose();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <div
        className="modal-card"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <h2 id={titleId} className="modal-title">
            {title}
          </h2>
          <button type="button" className="modal-close" aria-label="Close" onClick={onClose}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M6 6l12 12M18 6L6 18" />
            </svg>
          </button>
        </div>
        <div className="modal-body">
          <p className="modal-message" style={{ whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
            {message}
          </p>
        </div>
        <div className="modal-footer">
          <span />
          <div className="modal-actions">
            {link ? (
              <a
                className="modal-btn modal-btn-confirm"
                href={link.href}
                target="_blank"
                rel="noopener noreferrer"
              >
                {link.label}
              </a>
            ) : null}
            <button
              type="button"
              className={`modal-btn ${link ? "modal-btn-cancel" : "modal-btn-confirm"}`}
              onClick={onClose}
              autoFocus
            >
              OK
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
