/**
 * Lightweight global state via React Context. Holds the current page, the
 * selected project alias, the active branch ID and the page-owned selection;
 * pages read these to fan out queries.
 *
 * This state IS the URL: it is seeded from `window.location.hash` on the first
 * render and mirrored back into the hash on every change, so any view can be
 * shared as a link. See `router.ts` for the schema and the parse/build pair.
 */
import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { buildHash, parseHash, type RouteState } from "./router";

/**
 * Every navigable page. Single source of truth: the `PageId` union is derived
 * from it, and the router validates the `<page>` URL segment against it, so a
 * page can never be routable-but-unknown or known-but-unroutable.
 */
export const PAGE_IDS = [
  "dashboard",
  "projects",
  "configs",
  "storage",
  "stream",
  "jobs",
  "jobs-all",
  "branches",
  "workspaces",
  "flows",
  "schedules",
  "lineage",
  "semantic-layer",
  "sharing",
  "data-apps",
  "components",
  "localai",
  "agents",
  "search",
  "encrypt",
  "org",
  "members",
  "tokens",
  "tokens-all",
  "doctor",
  "changelog",
] as const;

export type PageId = (typeof PAGE_IDS)[number];

interface UIState {
  page: PageId;
  setPage: (p: PageId) => void;
  project: string | null;
  setProject: (p: string | null) => void;
  branchId: number | null;
  setBranchId: (b: number | null) => void;
  /**
   * Opaque, page-owned selection mirrored into the URL as `?sel=`. The page
   * that writes it defines its shape (a job id, `<tab>/<tableId>`, ...);
   * nothing outside that page may interpret it. Cleared automatically on any
   * page / project / branch change -- a selected object from another context
   * is meaningless. Pages consume it via `useHashSelection()`.
   */
  sel: string | null;
  setSel: (s: string | null) => void;
  manageToken: string | null;
  setManageToken: (t: string | null) => void;
  // Hand-off slot: the Dashboard hero "Ask <cli>" box drops a message here
  // when the user hits Send, then navigates to the Local AI page. The
  // Local AI page reads this on mount, auto-sends, and clears the slot.
  // Avoids re-typing while keeping all chat plumbing on a single page.
  pendingLocalAiMessage: string | null;
  setPendingLocalAiMessage: (m: string | null) => void;
  // Force-open slot: the command palette's "What's new" action flips this to
  // true, and the WhatsNew modal reads it on render, shows itself regardless
  // of the seen-marker, and clears the slot when dismissed. Same hand-off
  // shape as pendingLocalAiMessage -- one writer, one reader, self-clearing.
  whatsNewForced: boolean;
  setWhatsNewForced: (v: boolean) => void;
  // Hand-off slot: dropped by the command palette's "Search '...' across
  // projects" escape row. The Search page reads it on mount, auto-runs the
  // search, then clears it.
  pendingSearchQuery: string | null;
  setPendingSearchQuery: (q: string | null) => void;
}

const UIStateContext = createContext<UIState | null>(null);

/** Current location hash, or `""` outside a browser (tests, SSR). */
function currentHash(): string {
  return typeof window === "undefined" ? "" : window.location.hash;
}

/** Replace the hash without touching the history stack. */
function replaceHash(hash: string): void {
  const { pathname, search } = window.location;
  window.history.replaceState(null, "", `${pathname}${search}${hash}`);
}

export function UIStateProvider({ children }: { children: ReactNode }) {
  // Seeded from the URL so a shared link restores page + project + branch +
  // selection on the very first render -- before any effect (notably the top
  // bar's default-project pick) gets a chance to run.
  const [initial] = useState<RouteState>(() => parseHash(currentHash()));

  const [page, setPageState] = useState<PageId>(initial.page);
  const [project, setProjectState] = useState<string | null>(initial.project);
  const [branchId, setBranchIdState] = useState<number | null>(initial.branchId);
  const [sel, setSel] = useState<string | null>(initial.sel);
  const [manageToken, setManageToken] = useState<string | null>(null);
  const [pendingLocalAiMessage, setPendingLocalAiMessage] = useState<string | null>(null);
  const [whatsNewForced, setWhatsNewForced] = useState(false);
  const [pendingSearchQuery, setPendingSearchQuery] = useState<string | null>(null);

  // Navigating to another page drops the previous page's selection: `sel` is
  // page-owned, so carrying it across would hand one page another's cookie.
  const setPage = useCallback((p: PageId) => {
    setPageState(p);
    setSel(null);
  }, []);

  // Same reasoning across projects and branches: an object id resolved in one
  // project (or branch) does not exist in the next one.
  const setProject = useCallback((p: string | null) => {
    setProjectState(p);
    setSel(null);
  }, []);

  const setBranchId = useCallback((b: number | null) => {
    setBranchIdState(b);
    setSel(null);
  }, []);

  // Last hash WE wrote. A hashchange carrying exactly this value is our own
  // write echoing back and must not be re-applied; anything else is a real
  // navigation (Back/Forward, a hand-edited URL) and is parsed into state.
  const lastWrittenRef = useRef<string | null>(null);
  const prevPageRef = useRef<PageId>(initial.page);

  // State -> URL.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const pageChanged = page !== prevPageRef.current;
    prevPageRef.current = page;

    const next = buildHash({ page, project, branchId, sel });
    if (next === window.location.hash) {
      lastWrittenRef.current = next;
      return;
    }
    lastWrittenRef.current = next;
    if (pageChanged) {
      // Assignment pushes a history entry, so Back walks PAGE history...
      window.location.hash = next;
    } else {
      // ...while a project / branch / selection change only rewrites the
      // current entry. Otherwise every row click would need its own Back.
      replaceHash(next);
    }
  }, [page, project, branchId, sel]);

  // URL -> state (Back / Forward, hand-edited hash).
  useEffect(() => {
    if (typeof window === "undefined") return;
    const onHashChange = () => {
      const hash = window.location.hash;
      if (hash === lastWrittenRef.current) return;
      const route = parseHash(hash);
      // Adopt the page silently: the write effect must treat this as "already
      // in sync" and not push a duplicate history entry for it.
      prevPageRef.current = route.page;
      lastWrittenRef.current = hash;
      setPageState(route.page);
      setProjectState(route.project);
      setBranchIdState(route.branchId);
      setSel(route.sel);
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  return (
    <UIStateContext.Provider
      value={{
        page,
        setPage,
        project,
        setProject,
        branchId,
        setBranchId,
        sel,
        setSel,
        manageToken,
        setManageToken,
        pendingLocalAiMessage,
        setPendingLocalAiMessage,
        whatsNewForced,
        setWhatsNewForced,
        pendingSearchQuery,
        setPendingSearchQuery,
      }}
    >
      {children}
    </UIStateContext.Provider>
  );
}

export function useUIState(): UIState {
  const ctx = useContext(UIStateContext);
  if (!ctx) throw new Error("useUIState must be used inside UIStateProvider");
  return ctx;
}
