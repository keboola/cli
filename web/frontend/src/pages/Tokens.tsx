import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Copy, Globe, KeyRound, Plus, RefreshCw, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { ConfirmModal } from "../components/ConfirmModal";
import { Drawer } from "../components/Drawer";
import { Empty, ErrorBox, Loading, PageTitle } from "../components/Empty";
import { DataTable } from "../components/Table";
import type { Column } from "../components/Table";
import { useUIState } from "../state";
import {
  errMessage,
  LAST_USED_CAVEAT,
  ScopeCell,
  StatusCell,
  type TokenEntry,
} from "./tokensShared";

/**
 * Scoped Storage tokens -- the UI half of `kbagent token list|create|delete|refresh`.
 *
 * Two things about this page are not obvious from the API shape:
 *
 * 1. **`lastUsed` is DERIVED, not read.** The Storage API's token listing
 *    carries no `lastUsed` field at all (only the Manage API's PAT response
 *    does). The backend synthesizes it per token from that token's OWN event
 *    feed -- `GET /v2/storage/tokens/{id}/events?q=token.id:{id}`, narrowed
 *    SERVER-SIDE to events the token PERFORMED, not events performed ON it
 *    (the raw feed also carries `storage.tokenCreated`, which would make a
 *    freshly minted, never-used token read as "used today"). That is one extra
 *    API call PER TOKEN, which is why it is opt-in behind the toggle rather
 *    than part of the default listing.
 *
 * 2. **The secret is shown exactly once.** `create` and `refresh` are the only
 *    responses that ever carry a `token` value; the listing strips it. Nothing
 *    persists it, so the reveal panel is the user's single chance to copy it.
 *
 * The read-only vocabulary this page shares with the cross-project audit view
 * (`TokensAll`) lives in `tokensShared.tsx`; the mutations below stay here,
 * because each of them is scoped to one project's token.
 */

interface TokenListResp {
  alias: string;
  count: number;
  tokens: TokenEntry[];
  errors?: Array<{ token_id?: string; message: string }>;
}

/** `create` / `refresh` return the raw token dict INCLUDING the one-time secret. */
interface TokenSecretResp {
  alias?: string;
  id?: string | number;
  description?: string;
  token?: string;
  [key: string]: unknown;
}

/** What the reveal panel renders. `value` is held in React state only. */
interface RevealedSecret {
  value: string;
  title: string;
  subtitle: string;
}

/** "a, b , ,c" -> ["a","b","c"]; empty input -> undefined (key omitted from the body). */
function splitList(raw: string): string[] | undefined {
  const items = raw
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
  return items.length > 0 ? items : undefined;
}

export function TokensPage() {
  const { project, setPage } = useUIState();
  const qc = useQueryClient();
  const [withLastUsed, setWithLastUsed] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [secret, setSecret] = useState<RevealedSecret | null>(null);
  const [refreshTarget, setRefreshTarget] = useState<TokenEntry | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<TokenEntry | null>(null);

  const q = useQuery<TokenListResp>({
    queryKey: ["tokens", project, withLastUsed],
    queryFn: () =>
      api.get<TokenListResp>(`/token/${encodeURIComponent(project ?? "")}/list`, {
        query: { with_last_used: withLastUsed },
      }),
    enabled: !!project,
  });

  const refreshMu = useMutation<TokenSecretResp, Error, TokenEntry>({
    mutationFn: (t) =>
      api.post<TokenSecretResp>(`/token/${encodeURIComponent(project ?? "")}/refresh`, {
        token_id: String(t.id),
      }),
    onSuccess: (data, t) => {
      qc.invalidateQueries({ queryKey: ["tokens"] });
      setRefreshTarget(null);
      if (typeof data.token === "string" && data.token) {
        setSecret({
          value: data.token,
          title: "Token rotated",
          subtitle: `#${String(t.id)} ・ ${t.description ?? "(no description)"}`,
        });
      }
    },
  });

  const deleteMu = useMutation<unknown, Error, TokenEntry>({
    mutationFn: (t) =>
      api.post(`/token/${encodeURIComponent(project ?? "")}/delete`, {
        token_id: String(t.id),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tokens"] });
      setDeleteTarget(null);
    },
  });

  const closeDrawer = () => {
    // Clearing `secret` drops the one-time value out of React state.
    setSecret(null);
    setShowCreate(false);
  };

  const columns: Column<TokenEntry>[] = [
    {
      header: "ID",
      cell: (t) => <span className="text-zinc-500">{String(t.id)}</span>,
    },
    {
      header: "Description",
      cell: (t) => (
        <span className="font-medium text-zinc-900 dark:text-zinc-100">
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
      cell: (t) => <span className="text-zinc-500 text-xs">{t.expires || "—"}</span>,
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

  columns.push({
    header: "",
    align: "right",
    cell: (t) => (
      <div className="flex gap-1 justify-end">
        <button
          type="button"
          className="nerd-btn text-xs hover:text-amber-700 hover:border-amber-400 dark:hover:text-neon-amber"
          title="Rotate this token (invalidates the current value)"
          onClick={(e) => {
            e.stopPropagation();
            setRefreshTarget(t);
          }}
        >
          <RefreshCw className="w-3 h-3" />
        </button>
        <button
          type="button"
          className="nerd-btn text-xs hover:text-red-600 hover:border-red-300 dark:hover:text-red-400 dark:hover:border-red-700"
          title="Revoke this token"
          onClick={(e) => {
            e.stopPropagation();
            setDeleteTarget(t);
          }}
        >
          <Trash2 className="w-3 h-3" />
        </button>
      </div>
    ),
  });

  const perTokenErrors = q.data?.errors ?? [];

  return (
    <div className="space-y-4">
      <PageTitle
        title="Tokens"
        description={`Scoped Storage API tokens in ${project ?? "(no project)"}. Secrets are revealed once, at mint -- kbagent never stores them.`}
        actions={
          <>
            <button
              type="button"
              className="nerd-btn flex items-center gap-1"
              title="Audit tokens across every registered project (read-only)"
              onClick={() => setPage("tokens-all")}
            >
              <Globe className="w-3 h-3" /> All projects
            </button>
            <button
              type="button"
              className="nerd-btn flex items-center gap-1 hover:text-keboola"
              disabled={!project}
              onClick={() => {
                setSecret(null);
                setShowCreate(true);
              }}
            >
              <Plus className="w-3 h-3" /> Create token
            </button>
          </>
        }
      />

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          className={`nerd-btn flex items-center gap-1 ${
            withLastUsed ? "border-keboola text-keboola" : ""
          }`}
          onClick={() => setWithLastUsed((v) => !v)}
        >
          <RefreshCw className="w-3 h-3" /> derive last-used
        </button>
        {withLastUsed ? (
          <span className="text-xs text-zinc-500 dark:text-zinc-500">{LAST_USED_CAVEAT}</span>
        ) : null}
      </div>

      {refreshMu.error ? (
        <ErrorBox message={`Rotate failed: ${errMessage(refreshMu.error)}`} />
      ) : null}
      {deleteMu.error ? (
        <ErrorBox message={`Revoke failed: ${errMessage(deleteMu.error)}`} />
      ) : null}

      {!project ? (
        <Empty title="Select a project from the top bar" />
      ) : q.isLoading ? (
        <Loading label={withLastUsed ? "deriving last-used per token..." : "loading"} />
      ) : q.error ? (
        <ErrorBox message={errMessage(q.error)} />
      ) : (
        <>
          {perTokenErrors.length > 0 ? (
            <div className="text-xs text-amber-700 dark:text-neon-amber">
              {perTokenErrors.length} per-token lookup(s) failed:{" "}
              {perTokenErrors
                .map((e) => `${e.token_id ? `#${e.token_id}: ` : ""}${e.message}`)
                .join(" ・ ")}
            </div>
          ) : null}
          <DataTable
            rows={q.data?.tokens ?? []}
            rowKey={(t) => String(t.id)}
            emptyMessage="No tokens. The acting token needs canManageTokens to list or mint them."
            // No client-side sort: with `with_last_used` the server already
            // returns dormant-first, so reading order IS cleanup order.
            columns={columns}
          />
        </>
      )}

      {(showCreate || secret) && project ? (
        <Drawer
          open={true}
          onClose={closeDrawer}
          title={secret ? secret.title : "Create token"}
          subtitle={secret ? secret.subtitle : `Project: ${project}`}
          width="max-w-xl"
        >
          {secret ? (
            <SecretPanel secret={secret.value} onClose={closeDrawer} />
          ) : (
            <CreateTokenForm
              project={project}
              onCreated={(resp) => {
                qc.invalidateQueries({ queryKey: ["tokens"] });
                if (typeof resp.token === "string" && resp.token) {
                  setShowCreate(false);
                  setSecret({
                    value: resp.token,
                    title: "Token created",
                    subtitle: `#${String(resp.id ?? "?")} ・ ${resp.description ?? ""}`,
                  });
                } else {
                  closeDrawer();
                }
              }}
            />
          )}
        </Drawer>
      ) : null}

      {refreshTarget ? (
        <ConfirmModal
          title="Rotate token?"
          danger
          busy={refreshMu.isPending}
          confirmLabel="Rotate"
          cancelLabel="Cancel"
          body={
            <>
              Rotating <span className="text-accent">#{String(refreshTarget.id)}</span> (
              {refreshTarget.description || "no description"}) mints a NEW secret and{" "}
              <strong>invalidates the old value immediately</strong>. Every consumer still using it
              — CI jobs, components, scripts — breaks until you update it. The new secret is shown
              once, right after this.
            </>
          }
          onConfirm={() => refreshMu.mutate(refreshTarget)}
          onCancel={() => setRefreshTarget(null)}
        />
      ) : null}

      {deleteTarget ? (
        <ConfirmModal
          title="Revoke token?"
          danger
          busy={deleteMu.isPending}
          confirmLabel="Revoke"
          cancelLabel="Cancel"
          body={
            <>
              Revoke <span className="text-accent">#{String(deleteTarget.id)}</span> (
              {deleteTarget.description || "no description"})? Revocation is{" "}
              <strong>immediate and irreversible</strong> — the token cannot be restored, only
              replaced by a new one.
            </>
          }
          onConfirm={() => deleteMu.mutate(deleteTarget)}
          onCancel={() => setDeleteTarget(null)}
        />
      ) : null}
    </div>
  );
}

function CreateTokenForm({
  project,
  onCreated,
}: {
  project: string;
  onCreated: (resp: TokenSecretResp) => void;
}) {
  const [description, setDescription] = useState("");
  const [expiresIn, setExpiresIn] = useState("");
  const [bucketWrite, setBucketWrite] = useState("");
  const [bucketRead, setBucketRead] = useState("");
  const [componentAccess, setComponentAccess] = useState("");
  const [canReadAllFileUploads, setCanReadAllFileUploads] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const createMu = useMutation<TokenSecretResp>({
    mutationFn: () => {
      const parsedExpires = Number.parseInt(expiresIn.trim(), 10);
      return api.post<TokenSecretResp>(`/token/${encodeURIComponent(project)}/create`, {
        description: description.trim(),
        bucket_write: splitList(bucketWrite),
        bucket_read: splitList(bucketRead),
        component_access: splitList(componentAccess),
        can_read_all_file_uploads: canReadAllFileUploads,
        expires_in: Number.isFinite(parsedExpires) ? parsedExpires : undefined,
      });
    },
    onSuccess: (resp) => onCreated(resp),
    onError: (err) => setError(errMessage(err)),
  });

  const submitDisabled = !description.trim() || createMu.isPending;

  return (
    <div className="space-y-3">
      <label className="text-xs text-zinc-600 dark:text-zinc-400 block">
        Description <span className="text-red-600 dark:text-red-400">*</span>
        <input
          className="nerd-input w-full mt-1 font-mono"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="ci-writer for in.c-sales"
        />
        <span className="text-zinc-500 dark:text-zinc-600">
          The only way to tell tokens apart later — name the consumer, not the person.
        </span>
      </label>

      <label className="text-xs text-zinc-600 dark:text-zinc-400 block">
        Expires in (seconds)
        <input
          className="nerd-input w-full mt-1 font-mono"
          type="number"
          min={1}
          value={expiresIn}
          onChange={(e) => setExpiresIn(e.target.value)}
          placeholder="leave blank for no expiry"
        />
      </label>

      <label className="text-xs text-zinc-600 dark:text-zinc-400 block">
        Bucket write
        <input
          className="nerd-input w-full mt-1 font-mono"
          value={bucketWrite}
          onChange={(e) => setBucketWrite(e.target.value)}
          placeholder="in.c-sales, out.c-reports"
        />
        <span className="text-zinc-500 dark:text-zinc-600">Comma-separated bucket IDs.</span>
      </label>

      <label className="text-xs text-zinc-600 dark:text-zinc-400 block">
        Bucket read
        <input
          className="nerd-input w-full mt-1 font-mono"
          value={bucketRead}
          onChange={(e) => setBucketRead(e.target.value)}
          placeholder="in.c-reference"
        />
        <span className="text-zinc-500 dark:text-zinc-600">Comma-separated bucket IDs.</span>
      </label>

      <label className="text-xs text-zinc-600 dark:text-zinc-400 block">
        Component access
        <input
          className="nerd-input w-full mt-1 font-mono"
          value={componentAccess}
          onChange={(e) => setComponentAccess(e.target.value)}
          placeholder="keboola.ex-db-snowflake"
        />
        <span className="text-zinc-500 dark:text-zinc-600">Comma-separated component IDs.</span>
      </label>

      <label className="flex items-center gap-2 text-xs text-zinc-600 dark:text-zinc-400">
        <input
          type="checkbox"
          checked={canReadAllFileUploads}
          onChange={(e) => setCanReadAllFileUploads(e.target.checked)}
        />
        Can read all file uploads
      </label>

      {error ? <ErrorBox message={error} /> : null}

      <div className="flex justify-end">
        <button
          type="button"
          className="nerd-btn flex items-center gap-1 hover:text-keboola disabled:opacity-50"
          disabled={submitDisabled}
          onClick={() => {
            setError(null);
            createMu.mutate();
          }}
        >
          <KeyRound className="w-3 h-3" />
          {createMu.isPending ? "creating..." : "Create token"}
        </button>
      </div>
    </div>
  );
}

/**
 * One-time secret reveal. The value lives in React state only -- it is never
 * logged, never written to a query string, and is dropped the moment the panel
 * closes. `navigator.clipboard` is undefined on a non-secure origin, so the
 * copy button degrades to a "select it manually" hint instead of throwing.
 */
function SecretPanel({ secret, onClose }: { secret: string; onClose: () => void }) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "manual">("idle");
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  const onCopy = () => {
    const clip = navigator.clipboard;
    if (!clip || typeof clip.writeText !== "function") {
      setCopyState("manual");
      return;
    }
    clip.writeText(secret).then(
      () => {
        setCopyState("copied");
        if (timerRef.current) clearTimeout(timerRef.current);
        timerRef.current = setTimeout(() => setCopyState("idle"), 2000);
      },
      () => setCopyState("manual"),
    );
  };

  return (
    <div className="space-y-3">
      <div className="nerd-pill-amber">
        This value is shown once. kbagent never stores it — copy it now.
      </div>

      <pre className="nerd-code break-all whitespace-pre-wrap select-all">{secret}</pre>

      <div className="flex items-center gap-2">
        <button
          type="button"
          className="nerd-btn flex items-center gap-1 hover:text-keboola"
          onClick={onCopy}
        >
          {copyState === "copied" ? (
            <>
              <Check className="w-3 h-3" /> copied
            </>
          ) : (
            <>
              <Copy className="w-3 h-3" /> copy
            </>
          )}
        </button>
        <button type="button" className="nerd-btn text-xs" onClick={onClose}>
          Done
        </button>
        {copyState === "manual" ? (
          <span className="text-xs text-amber-700 dark:text-neon-amber">
            Clipboard unavailable (non-secure origin) — select the value above and copy manually.
          </span>
        ) : null}
      </div>
    </div>
  );
}
