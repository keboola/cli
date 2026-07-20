import { AlertTriangle, X } from "lucide-react";
import { useEffect, useRef } from "react";
import type { ReactNode } from "react";

/**
 * Lightweight confirmation dialog matching the app's modal style (see
 * ManageTokenModal). Replaces the native ``window.confirm()`` for actions that
 * deserve a clearer, on-brand prompt. Esc or a backdrop click cancels. On open
 * we focus Cancel for ``danger`` modals (so a stray Enter does NOT fire the
 * destructive action) and the confirm button otherwise.
 */
export function ConfirmModal({
  title,
  body,
  items,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  danger = false,
  busy = false,
  onConfirm,
  onCancel,
}: {
  title: string;
  body?: ReactNode;
  /** Optional list rendered in a scrollable box (e.g. affected aliases). */
  items?: string[];
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const confirmRef = useRef<HTMLButtonElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    // Danger modals focus Cancel so a stray Enter cannot fire the destructive
    // action; non-danger modals focus Confirm for fast keyboard confirmation.
    (danger ? cancelRef : confirmRef).current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [danger, busy, onCancel]);

  return (
    <div
      className="fixed inset-0 z-50 bg-black/70 backdrop-blur-sm flex items-center justify-center p-4"
      onClick={() => {
        if (!busy) onCancel();
      }}
    >
      <div
        className={`nerd-card w-full max-w-md space-y-3 ${danger ? "border-red-700/50" : "border-keboola/40"}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <h3
            className={`font-bold flex items-center gap-2 ${danger ? "text-red-400" : "text-keboola"}`}
          >
            <AlertTriangle className="w-4 h-4" /> {title}
          </h3>
          <button
            type="button"
            className="text-zinc-500 hover:text-zinc-200 disabled:opacity-50"
            onClick={onCancel}
            disabled={busy}
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {body ? <div className="text-sm text-zinc-400">{body}</div> : null}

        {items && items.length > 0 ? (
          <ul className="max-h-40 overflow-auto rounded border border-zinc-200 bg-zinc-50 p-2 text-xs font-mono text-zinc-700 dark:border-zinc-800 dark:bg-zinc-900/40 dark:text-zinc-300">
            {items.map((it) => (
              <li key={it} className="py-0.5">
                {it}
              </li>
            ))}
          </ul>
        ) : null}

        <div className="flex gap-2 justify-end">
          <button
            ref={cancelRef}
            type="button"
            className="nerd-btn text-xs"
            onClick={onCancel}
            disabled={busy}
          >
            {cancelLabel}
          </button>
          <button
            ref={confirmRef}
            type="button"
            className={`nerd-btn text-xs disabled:opacity-50 ${
              danger
                ? "text-red-400 border-red-700 hover:bg-red-950/40"
                : "hover:text-keboola"
            }`}
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? "Working..." : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
