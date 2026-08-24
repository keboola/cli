/**
 * Global "session expired" banner.
 *
 * Rendered when an API call answered 401 even after the client re-fetched
 * the shell to refresh the `kbagent_session` cookie (see
 * `SESSION_EXPIRED_EVENT` in api/client.ts). At that point the tab cannot
 * self-heal: either `kbagent serve` restarted and a full reload is needed
 * to re-bootstrap, or a session-registered project expired on the host
 * (the server message then names the `kbagent auth login` remedy). Without
 * this banner the failure mode is silent -- every list renders empty and
 * nothing says why.
 */
import { TriangleAlert, X } from "lucide-react";
import { useEffect, useState } from "react";
import { SESSION_EXPIRED_EVENT, type SessionExpiredDetail } from "../api/client";

export function SessionExpiredBanner() {
  const [detail, setDetail] = useState<SessionExpiredDetail | null>(null);
  useEffect(() => {
    const onExpired = (evt: Event) => {
      const incoming = (evt as CustomEvent<SessionExpiredDetail>).detail;
      setDetail(incoming ?? { code: "UNAUTHORIZED", message: "" });
    };
    window.addEventListener(SESSION_EXPIRED_EVENT, onExpired);
    return () => window.removeEventListener(SESSION_EXPIRED_EVENT, onExpired);
  }, []);
  if (!detail) return null;
  // The middleware's "Invalid Bearer token." is phrased for API callers; for
  // a browser tab the actionable truth is "the server restarted under you".
  // Session-login expiries (SESSION_EXPIRED / SESSION_NOT_FOUND) keep the
  // server message verbatim -- it names the on-host remedy.
  const text =
    detail.code === "UNAUTHORIZED"
      ? "The server was restarted and this tab's session is stale. Reload to reconnect."
      : detail.message || "The server rejected this session.";
  return (
    <div
      role="alert"
      className="flex items-center gap-3 border-b border-amber-300 bg-amber-50 px-4 py-1.5 text-xs text-amber-900 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-neon-amber"
    >
      <TriangleAlert className="h-3.5 w-3.5 shrink-0" />
      <span className="min-w-0 truncate">
        <span className="font-semibold">Session expired.</span> {text}
      </span>
      <button
        type="button"
        onClick={() => window.location.reload()}
        className="ml-auto shrink-0 rounded border border-amber-400 px-2 py-0.5 font-medium hover:bg-amber-100 dark:border-amber-500/50 dark:hover:bg-amber-500/20"
      >
        Reload
      </button>
      <button
        type="button"
        aria-label="Dismiss"
        onClick={() => setDetail(null)}
        className="shrink-0 rounded p-0.5 hover:bg-amber-100 dark:hover:bg-amber-500/20"
      >
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}
