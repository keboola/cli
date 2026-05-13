import { X } from "lucide-react";
import { type ReactNode, useEffect } from "react";
import { createPortal } from "react-dom";

/**
 * Right-side slide-over drawer. Fixed-position, full viewport height,
 * blocks scroll behind it. Use for "open detail / runner without
 * losing place in the table" UX (MCP tool runner, table detail, ...).
 *
 * The drawer is rendered through a React portal into ``document.body`` so it
 * is NOT constrained by any wrapper styles further up the page tree. Without
 * the portal a parent's ``space-y-4`` was leaking ``margin-top: 16px`` onto
 * the fixed container — the drawer's modality starts ~16px below the top of
 * the viewport, letting the page sneak through the gap.
 */
export function Drawer({
  open,
  title,
  subtitle,
  width = "max-w-3xl",
  onClose,
  actions,
  children,
}: {
  open: boolean;
  title: string;
  subtitle?: string;
  width?: string;
  onClose: () => void;
  actions?: ReactNode;
  children: ReactNode;
}) {
  useEffect(() => {
    if (!open) return;
    const onEsc = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onEsc);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onEsc);
      document.body.style.overflow = "";
    };
  }, [open, onClose]);

  if (!open) return null;
  // Near-opaque overlay (90% black-ish) — 70% let the page content under the
  // drawer bleed through enough to break modality, especially on the agent-task
  // table where action buttons are right under the click-catch area.
  return createPortal(
    <div className="fixed inset-0 z-50 flex bg-zinc-950/90 backdrop-blur-sm">
      <div className="flex-1" onClick={onClose} role="presentation" />
      <aside
        className={`relative w-full ${width} h-full bg-white border-l border-zinc-200 shadow-2xl flex flex-col dark:bg-zinc-950 dark:border-zinc-800`}
      >
        <header className="flex items-start justify-between gap-3 p-4 border-b border-zinc-200 shrink-0 dark:border-zinc-900">
          <div>
            <h2 className="font-bold text-keboola text-base">{title}</h2>
            {subtitle ? (
              <p className="text-xs text-zinc-500 mt-1">{subtitle}</p>
            ) : null}
          </div>
          <div className="flex items-center gap-2">
            {actions}
            <button
              type="button"
              onClick={onClose}
              className="text-zinc-500 hover:text-zinc-900 p-1 dark:hover:text-zinc-100"
              aria-label="Close"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </header>
        <div className="flex-1 overflow-auto p-4">{children}</div>
      </aside>
    </div>,
    document.body,
  );
}
