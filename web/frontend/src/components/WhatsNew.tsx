/**
 * "What's new" release-highlights popup.
 *
 * Shows the curated reel from ``whatsnew.ts`` once per version, then never
 * again unless the user explicitly asks for it from the command palette.
 *
 * Three independent gates decide whether the UNSOLICITED popup appears:
 *   1. the operator has not disabled it (``GET /ui-config`` -> ``banner``),
 *   2. a curated entry exists for the running version, and
 *   3. the seen-marker in localStorage is not already that version.
 *
 * All three must pass. The banner gate FAILS CLOSED: while the query is in
 * flight, if it errors, or if the flag is anything other than ``true``, no
 * auto-popup. A release-notes modal is never important enough to appear
 * against an operator who ran ``kbagent serve --no-banner``.
 *
 * The forced path (command palette action) bypasses gates 1 and 3 -- the
 * operator's ``--no-banner`` suppresses the *unsolicited* popup, not one the
 * user just asked for by name.
 */
import { useQuery } from "@tanstack/react-query";
import { Sparkles, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../api/client";
import { useUIState } from "../state";
import { whatsNewFor } from "../whatsnew";

interface VersionResp {
  kbagent: { version: string; latest_version: string; up_to_date: boolean };
}

interface UiConfigResp {
  banner: boolean;
}

/** Last dismissed release version, e.g. "0.89.0". */
const SEEN_KEY = "kbagent.whatsnew.seen";

/** Safari private mode throws on read as well as write -- never let it crash the shell. */
function readSeen(): string | null {
  try {
    return window.localStorage.getItem(SEEN_KEY);
  } catch {
    return null;
  }
}

function writeSeen(version: string): void {
  try {
    window.localStorage.setItem(SEEN_KEY, version);
  } catch {
    // Storage unavailable (private mode, quota). The popup will reappear on
    // the next load -- annoying, but strictly better than a white screen.
  }
}

/** Inline keycap, matching the inline-code treatment used elsewhere in the UI. */
function Keycap({ children }: { children: string }) {
  return (
    <kbd className="px-1 py-0.5 rounded bg-zinc-100 border border-zinc-200 text-[10px] text-zinc-600 dark:bg-zinc-950 dark:border-zinc-800 dark:text-zinc-400">
      {children}
    </kbd>
  );
}

export function WhatsNew() {
  const { setPage, whatsNewForced, setWhatsNewForced } = useUIState();
  const [autoOpen, setAutoOpen] = useState(false);
  // Drives the single entrance transition; flipped one frame after mount.
  const [shown, setShown] = useState(false);

  // Same query key + staleTime as StatusBar, so this shares the cache and
  // costs no extra request.
  const versionQ = useQuery<VersionResp>({
    queryKey: ["version"],
    queryFn: () => api.get<VersionResp>("/version"),
    staleTime: 5 * 60_000,
  });
  const uiConfigQ = useQuery<UiConfigResp>({
    queryKey: ["ui-config"],
    queryFn: () => api.get<UiConfigResp>("/ui-config"),
    staleTime: 5 * 60_000,
  });

  const release = whatsNewFor(versionQ.data?.kbagent.version);
  // Fail closed: loading and error both read as "not enabled".
  const bannerEnabled = uiConfigQ.data?.banner === true;

  useEffect(() => {
    if (!bannerEnabled || !release) return;
    if (readSeen() === release.version) return;
    setAutoOpen(true);
  }, [bannerEnabled, release]);

  const open = autoOpen || whatsNewForced;

  useEffect(() => {
    if (!open) {
      setShown(false);
      return;
    }
    const t = window.setTimeout(() => setShown(true), 10);
    return () => window.clearTimeout(t);
  }, [open]);

  const dismiss = useCallback(() => {
    if (release) writeSeen(release.version);
    setAutoOpen(false);
    setWhatsNewForced(false);
  }, [release, setWhatsNewForced]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") dismiss();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, dismiss]);

  if (!open) return null;

  const version = versionQ.data?.kbagent.version;

  return createPortal(
    // z-[55]: above drawers (z-50), below the command palette (z-[60]).
    <div
      className="fixed inset-0 z-[55] bg-zinc-900/50 backdrop-blur-sm flex items-center justify-center p-4 dark:bg-black/70"
      onClick={dismiss}
      role="presentation"
    >
      <div
        className={`nerd-card w-full max-w-2xl p-0 overflow-hidden border-keboola/40 shadow-2xl transition duration-200 ${
          shown ? "opacity-100 translate-y-0" : "opacity-0 translate-y-2"
        }`}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="What's new"
      >
        <div className="flex items-center gap-2 px-4 py-2.5 border-b border-zinc-200 dark:border-zinc-800">
          <Sparkles className="w-4 h-4 text-keboola shrink-0" />
          <h3 className="text-sm font-bold text-keboola">What's new</h3>
          {release ? (
            <span className="nerd-pill-green text-[10px] shrink-0">{release.version}</span>
          ) : null}
          <button
            type="button"
            className="ml-auto text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-200"
            onClick={dismiss}
            aria-label="Dismiss"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="max-h-[60vh] overflow-y-auto px-4 py-3">
          {release ? (
            <ul className="space-y-3">
              {release.items.map((item) => (
                <li key={item.title}>
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-bold text-zinc-900 dark:text-zinc-100">
                      {item.title}
                    </span>
                    {item.hint ? <Keycap>{item.hint}</Keycap> : null}
                  </div>
                  <p className="text-xs leading-relaxed text-zinc-600 dark:text-zinc-400">
                    {item.body}
                  </p>
                </li>
              ))}
            </ul>
          ) : (
            // Forced open on a version nobody curated highlights for. Say so
            // plainly rather than rendering an empty shell or inventing items.
            <p className="py-4 text-xs text-zinc-500 text-center">
              Nothing curated for {version ?? "this version"} — see the full changelog for
              everything that shipped.
            </p>
          )}
        </div>

        <div className="flex items-center gap-2 px-4 py-2 border-t border-zinc-200 dark:border-zinc-800">
          <button
            type="button"
            className="text-xs text-zinc-500 hover:text-keboola transition-colors"
            onClick={() => {
              setPage("changelog");
              dismiss();
            }}
          >
            full changelog →
          </button>
          <button type="button" className="nerd-btn text-xs ml-auto" onClick={dismiss}>
            got it
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
