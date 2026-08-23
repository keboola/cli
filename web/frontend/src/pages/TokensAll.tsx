import { useQuery } from "@tanstack/react-query";
import { KeyRound, RefreshCw } from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import { Empty, ErrorBox, Loading, PageTitle } from "../components/Empty";
import { DataTable } from "../components/Table";
import type { Column } from "../components/Table";
import { useUIState } from "../state";
import {
  errMessage,
  expiresLabel,
  LAST_USED_CAVEAT_ALL_PROJECTS,
  ScopeCell,
  StatusCell,
  type TokenEntry,
} from "./tokensShared";

/**
 * Cross-project token audit: one `GET /token/list` call with no `project`
 * param, the server fans out over every registered project in parallel and
 * returns the merged `{tokens, errors}` envelope with `project_alias` stamped
 * on each row.
 *
 * **Deliberately READ-ONLY.** Minting, rotating and revoking stay on the
 * per-project page. Two reasons: every one of those calls needs a single
 * project's credentials anyway (there is no cross-project mutation to batch),
 * and a destructive click on a merged list is one mis-read `project_alias`
 * away from revoking the right-looking token in the wrong project. A row click
 * therefore navigates INTO that project's Tokens page, where the actions live
 * next to the context that makes them safe.
 */

interface TokenProjectError {
  project_alias: string;
  /** Optional: a transport-level failure has no kbagent error code. */
  error_code?: string;
  message: string;
}

interface TokensAllResp {
  tokens: TokenEntry[];
  count: number;
  errors?: TokenProjectError[];
}

/** Tone -> NERD pill/text classes for the Expires cell. */
const EXPIRY_CLASS: Record<string, string> = {
  none: "text-zinc-500 text-xs",
  later: "text-zinc-500 text-xs",
  unknown: "text-zinc-500 text-xs",
  soon: "text-amber-700 dark:text-neon-amber text-xs",
  expired: "text-red-600 dark:text-red-400 text-xs",
};

/** Hover text that makes the row's only interaction -- navigation -- discoverable. */
function rowTitle(t: TokenEntry): string {
  return t.project_alias
    ? `Open in project view (${t.project_alias})`
    : "Open in project view";
}

export function TokensAllPage() {
  const { setPage, setProject } = useUIState();
  const [withLastUsed, setWithLastUsed] = useState(false);

  // No polling: tokens are minted and revoked by hand, not by a running job.
  const q = useQuery<TokensAllResp>({
    queryKey: ["tokens-all", withLastUsed],
    queryFn: () =>
      api.get<TokensAllResp>("/token/list", {
        // Omitting `project` entirely is what asks for every registered one.
        query: { with_last_used: withLastUsed || undefined },
      }),
  });

  const openInProject = (t: TokenEntry) => {
    if (!t.project_alias) return;
    setProject(t.project_alias);
    setPage("tokens");
  };

  const columns: Column<TokenEntry>[] = [
    {
      header: "Project",
      cell: (t) => (
        <span className="text-xs text-zinc-500 dark:text-zinc-500" title={rowTitle(t)}>
          {t.project_alias ?? "—"}
        </span>
      ),
    },
    {
      header: "ID",
      cell: (t) => (
        <span className="text-zinc-500" title={rowTitle(t)}>
          {String(t.id)}
        </span>
      ),
    },
    {
      header: "Description",
      cell: (t) => (
        <span className="font-medium text-zinc-900 dark:text-zinc-100" title={rowTitle(t)}>
          {t.description || "(no description)"}
        </span>
      ),
    },
    { header: "Scope", cell: (t) => <ScopeCell t={t} /> },
    {
      header: "Created",
      cell: (t) => <span className="text-zinc-500 text-xs">{t.created || "—"}</span>,
    },
    {
      header: "Refreshed",
      cell: (t) => <span className="text-zinc-500 text-xs">{t.refreshed || "—"}</span>,
    },
    {
      header: "Expires",
      cell: (t) => {
        const label = expiresLabel(t.expires);
        return (
          <span
            className={EXPIRY_CLASS[label.tone] ?? "text-zinc-500 text-xs"}
            title={t.expires ?? "no expiry"}
          >
            {label.text}
          </span>
        );
      },
    },
  ];

  if (withLastUsed) {
    columns.push(
      {
        header: "Last used",
        cell: (t) => <span className="text-zinc-500 text-xs">{t.lastUsed || "—"}</span>,
      },
      {
        header: "Last event",
        cell: (t) => (
          <span className="text-accent text-xs break-all">{t.lastUsedEvent || "—"}</span>
        ),
      },
      { header: "Status", cell: (t) => <StatusCell t={t} /> },
    );
  }

  const projectErrors = q.data?.errors ?? [];
  const rows = q.data?.tokens ?? [];

  return (
    <div className="space-y-4">
      <PageTitle
        title="All Tokens"
        description="Scoped Storage API tokens across every registered project ・ secrets are never included. Read-only — mint, rotate and revoke live on the per-project page."
        actions={
          <>
            <button
              type="button"
              className={`nerd-btn flex items-center gap-1 ${
                withLastUsed ? "border-keboola text-keboola" : ""
              }`}
              onClick={() => setWithLastUsed((v) => !v)}
            >
              <RefreshCw className="w-3 h-3" /> derive last-used
            </button>
            <button
              type="button"
              className="nerd-btn flex items-center gap-1"
              title="Back to the active project's tokens, where create / rotate / revoke live"
              onClick={() => setPage("tokens")}
            >
              <KeyRound className="w-3 h-3" /> Current project only
            </button>
          </>
        }
      />

      {withLastUsed ? (
        <div className="text-xs text-zinc-500 dark:text-zinc-500">
          {LAST_USED_CAVEAT_ALL_PROJECTS}
        </div>
      ) : null}

      {projectErrors.length > 0 ? (
        <div className="nerd-card border-amber-300 text-amber-700 dark:border-amber-700/40 dark:text-neon-amber">
          <div className="font-bold text-sm mb-1">
            {projectErrors.length} project(s) could not be listed
          </div>
          <ul className="text-xs space-y-0.5">
            {projectErrors.map((e) => (
              <li key={`${e.project_alias}-${e.error_code ?? ""}-${e.message}`}>
                <span className="font-mono">{e.project_alias}</span> — {e.message}
                {e.error_code ? <span className="text-zinc-500"> ({e.error_code})</span> : null}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {q.isLoading ? (
        <Loading
          label={
            withLastUsed ? "deriving last-used per token, across all projects..." : "loading"
          }
        />
      ) : q.error ? (
        <ErrorBox message={errMessage(q.error)} />
      ) : rows.length === 0 && projectErrors.length === 0 ? (
        <Empty
          title="No tokens found in any registered project"
          hint="Listing tokens requires a token with canManageTokens in each project."
        />
      ) : (
        <DataTable
          rows={rows}
          rowKey={(t) => `${t.project_alias ?? "?"}-${String(t.id)}`}
          emptyMessage="No tokens. The acting token needs canManageTokens to list them."
          // No client-side sort: with `with_last_used` the server returns
          // dormant-first globally, otherwise grouped by project -- either way
          // reading order is the order the audit wants.
          onRowClick={openInProject}
          columns={columns}
        />
      )}
    </div>
  );
}
