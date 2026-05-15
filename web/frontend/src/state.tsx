/**
 * Lightweight global state via React Context. Holds the currently-selected
 * project alias and active branch ID; pages read these to fan out queries.
 */
import { createContext, useCallback, useContext, useState } from "react";
import type { ReactNode } from "react";

export type PageId =
  | "dashboard"
  | "projects"
  | "configs"
  | "storage"
  | "jobs"
  | "branches"
  | "workspaces"
  | "flows"
  | "schedules"
  | "lineage"
  | "semantic-layer"
  | "sharing"
  | "data-apps"
  | "components"
  | "mcp"
  | "localai"
  | "agents"
  | "search"
  | "encrypt"
  | "org"
  | "members"
  | "doctor"
  | "changelog";

interface UIState {
  page: PageId;
  setPage: (p: PageId) => void;
  project: string | null;
  setProject: (p: string | null) => void;
  branchId: number | null;
  setBranchId: (b: number | null) => void;
  manageToken: string | null;
  setManageToken: (t: string | null) => void;
  // Hand-off slot: the Dashboard hero "Ask <cli>" box drops a message here
  // when the user hits Send, then navigates to the Local AI page. The
  // Local AI page reads this on mount, auto-sends, and clears the slot.
  // Avoids re-typing while keeping all chat plumbing on a single page.
  pendingLocalAiMessage: string | null;
  setPendingLocalAiMessage: (m: string | null) => void;
}

const UIStateContext = createContext<UIState | null>(null);

export function UIStateProvider({ children }: { children: ReactNode }) {
  const [page, setPage] = useState<PageId>("dashboard");
  const [project, setProject] = useState<string | null>(null);
  const [branchId, setBranchId] = useState<number | null>(null);
  const [manageToken, setManageToken] = useState<string | null>(null);
  const [pendingLocalAiMessage, setPendingLocalAiMessage] = useState<string | null>(null);

  return (
    <UIStateContext.Provider
      value={{
        page,
        setPage,
        project,
        setProject,
        branchId,
        setBranchId,
        manageToken,
        setManageToken,
        pendingLocalAiMessage,
        setPendingLocalAiMessage,
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

/**
 * Helper: prompt for the manage token (via modal in the future). For now,
 * stash it in component state and pass it down to the API call.
 */
export function useManageTokenPrompt(): (reason: string) => Promise<string | null> {
  return useCallback(async (reason: string) => {
    const value = window.prompt(
      `${reason}\n\nThis manage token is used for THIS request only and never stored.`,
    );
    return value && value.trim() ? value.trim() : null;
  }, []);
}
