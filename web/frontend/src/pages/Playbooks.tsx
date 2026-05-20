/**
 * Playbook Library — Phase 1 surface.
 *
 * Library cards render summaries; clicking opens a right-side Drawer
 * with the full Playbook (description, connections, skills, plugins,
 * triggers, timestamps). The Drawer is read-only for now — editing
 * the SOP / Budget / Approval policy lands in a later slice.
 *
 * Sources of truth:
 * - layout: `docs/mockups/01-playbooks-library.png`
 * - data shape: `docs/agents-v2.md` § 7 (PlaybookSummary + Playbook)
 * - components: `docs/agent-studio-design-system.md` § 5 (.nerd-card,
 *   .nerd-btn, .nerd-pill-*, Drawer)
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookOpen, Play, Plus, Sparkles, Trash2 } from "lucide-react";
import { clsx } from "clsx";
import { useState } from "react";
import { api } from "../api/client";
import { Drawer } from "../components/Drawer";
import { ErrorBox, Loading, PageTitle, TwoPathEmpty } from "../components/Empty";

type PlaybookStatus =
  | "draft"
  | "scheduled"
  | "queued"
  | "running"
  | "blocked"
  | "waiting_for_approval"
  | "reviewing"
  | "done"
  | "failed"
  | "cancelled";

interface PlaybookSummary {
  id: string;
  name: string;
  description: string | null;
  revision: number;
  enabled: boolean;
  status: PlaybookStatus;
  created_at: string;
  updated_at: string;
}

interface Playbook extends PlaybookSummary {
  connections: string[];
  skills: string[];
  plugins: string[];
  triggers: Array<Record<string, unknown>>;
}

interface PlaybookRun {
  id: string;
  playbook_id: string;
  playbook_revision: number;
  status: PlaybookStatus;
  started_at: string;
  ended_at: string | null;
  summary: string | null;
  objective_override: string | null;
}

interface PlaybooksResponse {
  playbooks: PlaybookSummary[];
}

interface RunsResponse {
  runs: PlaybookRun[];
}

// `.nerd-pill-*` family stays the source of truth for outlined status
// colors. Map each status to one of the three buckets per design
// system § 2.3.
const STATUS_PILL_CLASS: Record<PlaybookStatus, string> = {
  draft: "nerd-pill",
  scheduled: "nerd-pill-green",
  queued: "nerd-pill-green",
  running: "nerd-pill-green",
  done: "nerd-pill-green",
  blocked: "nerd-pill-amber",
  waiting_for_approval: "nerd-pill-amber",
  reviewing: "nerd-pill-amber",
  failed: "nerd-pill-red",
  cancelled: "nerd-pill-red",
};

const STATUS_LABEL: Record<PlaybookStatus, string> = {
  draft: "Draft",
  scheduled: "Scheduled",
  queued: "Queued",
  running: "Running",
  blocked: "Blocked",
  waiting_for_approval: "Waiting",
  reviewing: "Reviewing",
  done: "Done",
  failed: "Failed",
  cancelled: "Cancelled",
};

export function PlaybooksPage() {
  const qc = useQueryClient();
  const [creating, setCreating] = useState(false);
  // Selected playbook ID drives the detail Drawer. Keeping ID-only
  // (vs. the full summary) means the Drawer re-fetches the full body
  // every time it opens, picking up edits that happened in between.
  const [selectedId, setSelectedId] = useState<string | null>(null);
  // Two-step delete: clicking Delete in the Drawer surfaces the
  // confirmation modal; only then does the mutation run.
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);

  const q = useQuery<PlaybooksResponse>({
    queryKey: ["playbooks"],
    queryFn: () => api.get("/v1/agent-studio/playbooks"),
    // Match Agent Tasks' 10s polling cadence -- the spec calls this
    // out in `docs/agents-v2.md` § 7 to keep the two surfaces feeling
    // identical from a freshness standpoint.
    refetchInterval: 10_000,
  });

  const createMu = useMutation({
    mutationFn: (name: string) =>
      api.post<Playbook>("/v1/agent-studio/playbooks", {
        name,
        description: null,
      }),
    onSuccess: (created) => {
      qc.invalidateQueries({ queryKey: ["playbooks"] });
      setCreating(false);
      // Open the new Playbook's drawer so the user immediately sees
      // what their click produced — much better than dropping them
      // back onto the library and making them hunt for the row.
      setSelectedId(created.id);
    },
  });

  const deleteMu = useMutation({
    mutationFn: (id: string) =>
      api.delete<void>(`/v1/agent-studio/playbooks/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["playbooks"] });
      setConfirmDeleteId(null);
      setSelectedId(null);
    },
  });

  const playbooks = q.data?.playbooks ?? [];

  return (
    <div className="space-y-4">
      <PageTitle
        title="Playbooks"
        description="Reusable agentic procedures running inside kbagent serve. Each playbook is a SOP, a set of connections, skills, logins, and triggers."
        actions={
          <button
            type="button"
            className="nerd-btn flex items-center gap-1 hover:text-keboola"
            onClick={() => setCreating(true)}
            disabled={createMu.isPending}
          >
            <Plus className="w-3 h-3" /> New playbook
          </button>
        }
      />

      {q.isLoading ? <Loading /> : null}
      {q.error ? <ErrorBox message={(q.error as Error).message} /> : null}

      {!q.isLoading && playbooks.length === 0 ? (
        <TwoPathEmpty
          headline="Ship your first Playbook"
          subline="Two paths -- pick the one that fits your goal."
          paths={[
            {
              title: "Start from a Blueprint",
              description:
                "Curated playbook templates grounded in Keboola data. Fork one to get a working Playbook in seconds.",
              icon: <BookOpen className="w-8 h-8 text-keboola" />,
              action: (
                <button
                  type="button"
                  className="nerd-btn hover:text-keboola"
                  disabled
                  title="Blueprints catalogue ships in Phase 2"
                >
                  Browse Blueprints (Phase 2)
                </button>
              ),
            },
            {
              title: "Describe in plain English",
              description:
                "Tell kbagent what you want to automate. It compiles a SOP, picks connections, and stages skills for your review.",
              icon: <Sparkles className="w-8 h-8 text-neon-pink" />,
              badge: "more agentic",
              action: (
                <button
                  type="button"
                  className="nerd-btn hover:text-keboola"
                  disabled={createMu.isPending}
                  onClick={() => setCreating(true)}
                >
                  + New playbook
                </button>
              ),
            },
          ]}
        />
      ) : null}

      {playbooks.length > 0 ? (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          {playbooks.map((p) => (
            <PlaybookCard
              key={p.id}
              playbook={p}
              onOpen={() => setSelectedId(p.id)}
            />
          ))}
        </div>
      ) : null}

      {creating ? (
        <NewPlaybookModal
          onCancel={() => setCreating(false)}
          onConfirm={(name) => createMu.mutate(name)}
          isSubmitting={createMu.isPending}
          error={createMu.error ? (createMu.error as Error).message : null}
        />
      ) : null}

      <PlaybookDetailDrawer
        playbookId={selectedId}
        onClose={() => setSelectedId(null)}
        onDelete={(id) => setConfirmDeleteId(id)}
      />

      {confirmDeleteId ? (
        <DeleteConfirmModal
          playbookId={confirmDeleteId}
          onCancel={() => setConfirmDeleteId(null)}
          onConfirm={() => deleteMu.mutate(confirmDeleteId)}
          isSubmitting={deleteMu.isPending}
          error={deleteMu.error ? (deleteMu.error as Error).message : null}
        />
      ) : null}
    </div>
  );
}

function PlaybookCard({
  playbook,
  onOpen,
}: {
  playbook: PlaybookSummary;
  onOpen: () => void;
}) {
  const pillClass = STATUS_PILL_CLASS[playbook.status];
  return (
    <button
      type="button"
      onClick={onOpen}
      className="nerd-card hover:border-keboola/30 transition-colors text-left w-full"
    >
      <div className="flex items-center justify-between mb-2">
        <span className="nerd-pill font-mono">
          {playbook.id.slice(0, 8)} · v{playbook.revision}
        </span>
        <span className={clsx(pillClass, "font-mono")}>
          <span className="w-1.5 h-1.5 rounded-full bg-current opacity-80" />
          {STATUS_LABEL[playbook.status]}
        </span>
      </div>
      <h3 className="font-bold text-base mb-1">{playbook.name}</h3>
      <p className="text-xs text-zinc-500 dark:text-zinc-500 line-clamp-2 min-h-[2.2em]">
        {playbook.description ?? "No description yet."}
      </p>
      <div className="text-[10px] uppercase tracking-widest text-zinc-500 mt-3">
        rev {playbook.revision} · {playbook.enabled ? "enabled" : "disabled"}
      </div>
    </button>
  );
}

function PlaybookDetailDrawer({
  playbookId,
  onClose,
  onDelete,
}: {
  playbookId: string | null;
  onClose: () => void;
  onDelete: (id: string) => void;
}) {
  const qc = useQueryClient();
  // The Drawer mounts only when ``playbookId`` is set, so we are safe
  // to fan out the query unconditionally inside the body branch.
  const isOpen = playbookId !== null;
  const detailQ = useQuery<Playbook>({
    queryKey: ["playbook", playbookId],
    queryFn: () => api.get(`/v1/agent-studio/playbooks/${playbookId}`),
    enabled: isOpen,
  });

  const runsQ = useQuery<RunsResponse>({
    queryKey: ["playbook-runs", playbookId],
    queryFn: () =>
      api.get(`/v1/agent-studio/runs`, {
        query: { playbook_id: playbookId ?? undefined },
      }),
    enabled: isOpen,
    // Match the library's polling cadence so a run kicked off elsewhere
    // shows up here without a manual refresh.
    refetchInterval: 10_000,
  });

  const runMu = useMutation({
    mutationFn: () =>
      api.post<PlaybookRun>(
        `/v1/agent-studio/playbooks/${playbookId}/run`,
        {},
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["playbook-runs", playbookId] });
      // The library card status pill follows the latest run state, so
      // pop the library query too -- cheap and keeps everything in sync.
      qc.invalidateQueries({ queryKey: ["playbooks"] });
    },
  });

  const pb = detailQ.data;
  const title = pb?.name ?? (detailQ.isLoading ? "loading…" : "Playbook");
  const subtitle = pb
    ? `${pb.id} · rev ${pb.revision}`
    : playbookId
      ? playbookId
      : undefined;

  return (
    <Drawer
      open={isOpen}
      title={title}
      subtitle={subtitle}
      width="640px"
      onClose={onClose}
      actions={
        pb ? (
          <div className="flex gap-2">
            <button
              type="button"
              className="nerd-btn flex items-center gap-1 hover:text-keboola hover:border-keboola/40"
              onClick={() => runMu.mutate()}
              disabled={runMu.isPending}
            >
              <Play className="w-3 h-3" />
              {runMu.isPending ? "Running..." : "Run"}
            </button>
            <button
              type="button"
              className="nerd-btn flex items-center gap-1 hover:border-red-300 hover:text-red-500"
              onClick={() => onDelete(pb.id)}
            >
              <Trash2 className="w-3 h-3" /> Delete
            </button>
          </div>
        ) : null
      }
    >
      {detailQ.isLoading ? <Loading /> : null}
      {detailQ.error ? (
        <ErrorBox message={(detailQ.error as Error).message} />
      ) : null}
      {runMu.error ? (
        <ErrorBox message={(runMu.error as Error).message} />
      ) : null}
      {pb ? (
        <PlaybookBody
          playbook={pb}
          runs={runsQ.data?.runs ?? []}
          runsLoading={runsQ.isLoading}
        />
      ) : null}
    </Drawer>
  );
}

function PlaybookBody({
  playbook,
  runs,
  runsLoading,
}: {
  playbook: Playbook;
  runs: PlaybookRun[];
  runsLoading: boolean;
}) {
  const pillClass = STATUS_PILL_CLASS[playbook.status];
  return (
    <div className="space-y-5">
      <section>
        <div className="flex items-center gap-2 mb-2">
          <span className={clsx(pillClass, "font-mono")}>
            <span className="w-1.5 h-1.5 rounded-full bg-current opacity-80" />
            {STATUS_LABEL[playbook.status]}
          </span>
          <span className="nerd-pill font-mono">
            {playbook.enabled ? "enabled" : "disabled"}
          </span>
        </div>
        <p className="text-sm text-zinc-700 dark:text-zinc-300">
          {playbook.description ?? (
            <span className="text-zinc-500 italic">No description yet.</span>
          )}
        </p>
      </section>

      <DetailGroup label="Connections" items={playbook.connections} />
      <DetailGroup label="Skills" items={playbook.skills} />
      <DetailGroup label="Plugins" items={playbook.plugins} />

      <section>
        <h4 className="text-[10px] uppercase tracking-widest text-zinc-500 mb-2">
          Triggers
        </h4>
        {playbook.triggers.length === 0 ? (
          <p className="text-xs text-zinc-500 italic">
            No triggers — runs are manual-only.
          </p>
        ) : (
          <ul className="space-y-2">
            {playbook.triggers.map((t, i) => (
              <li
                key={i}
                className="nerd-code font-mono whitespace-pre-wrap break-words"
              >
                {JSON.stringify(t, null, 2)}
              </li>
            ))}
          </ul>
        )}
      </section>

      <RunsSection runs={runs} loading={runsLoading} />

      <section className="text-[10px] uppercase tracking-widest text-zinc-500 grid grid-cols-2 gap-2">
        <div>
          <div>Created</div>
          <div className="font-mono normal-case text-xs text-zinc-700 dark:text-zinc-300 tracking-normal">
            {formatTs(playbook.created_at)}
          </div>
        </div>
        <div>
          <div>Updated</div>
          <div className="font-mono normal-case text-xs text-zinc-700 dark:text-zinc-300 tracking-normal">
            {formatTs(playbook.updated_at)}
          </div>
        </div>
      </section>
    </div>
  );
}

function RunsSection({
  runs,
  loading,
}: {
  runs: PlaybookRun[];
  loading: boolean;
}) {
  // Truncate to last 5 — full run history is a Past Jobs tab in a
  // later slice. The "+N more" pill keeps the user informed without
  // making the drawer scroll.
  const displayed = runs.slice(0, 5);
  const hidden = runs.length - displayed.length;
  return (
    <section>
      <h4 className="text-[10px] uppercase tracking-widest text-zinc-500 mb-2">
        Recent Runs
        {runs.length > 0 ? (
          <span className="ml-2 normal-case tracking-normal text-zinc-400">
            ({runs.length})
          </span>
        ) : null}
      </h4>
      {loading ? <Loading /> : null}
      {!loading && runs.length === 0 ? (
        <p className="text-xs text-zinc-500 italic">
          No runs yet. Hit Run to kick the first one off.
        </p>
      ) : null}
      {displayed.length > 0 ? (
        <ul className="space-y-1.5">
          {displayed.map((r) => (
            <RunRow key={r.id} run={r} />
          ))}
          {hidden > 0 ? (
            <li className="text-[10px] uppercase tracking-widest text-zinc-500 italic">
              + {hidden} earlier run{hidden === 1 ? "" : "s"} (Past Jobs tab
              ships in a later slice)
            </li>
          ) : null}
        </ul>
      ) : null}
    </section>
  );
}

function RunRow({ run }: { run: PlaybookRun }) {
  const pillClass = STATUS_PILL_CLASS[run.status];
  const duration = computeDuration(run.started_at, run.ended_at);
  return (
    <li className="flex items-center gap-2 text-xs">
      <span className={clsx(pillClass, "font-mono shrink-0")}>
        <span className="w-1.5 h-1.5 rounded-full bg-current opacity-80" />
        {STATUS_LABEL[run.status]}
      </span>
      <span className="font-mono text-zinc-700 dark:text-zinc-300 truncate">
        #{run.id.slice(0, 8)}
      </span>
      <span className="font-mono text-zinc-500">·</span>
      <span className="font-mono text-zinc-500">{formatTs(run.started_at)}</span>
      {duration ? (
        <>
          <span className="font-mono text-zinc-500">·</span>
          <span className="font-mono text-zinc-500">{duration}</span>
        </>
      ) : null}
    </li>
  );
}

function computeDuration(startedIso: string, endedIso: string | null): string | null {
  if (!endedIso) return null;
  try {
    const ms = new Date(endedIso).getTime() - new Date(startedIso).getTime();
    if (Number.isNaN(ms) || ms < 0) return null;
    if (ms < 1000) return `${ms} ms`;
    const s = Math.round(ms / 1000);
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    const rem = s % 60;
    return `${m}m ${rem}s`;
  } catch {
    return null;
  }
}

function DetailGroup({ label, items }: { label: string; items: string[] }) {
  return (
    <section>
      <h4 className="text-[10px] uppercase tracking-widest text-zinc-500 mb-2">
        {label}
      </h4>
      {items.length === 0 ? (
        <p className="text-xs text-zinc-500 italic">None — set in a later slice.</p>
      ) : (
        <ul className="flex flex-wrap gap-1.5">
          {items.map((s) => (
            <li key={s} className="nerd-pill font-mono">
              {s}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function formatTs(iso: string): string {
  // Render server timestamps as the user's local clock so the Drawer
  // matches what they'd see in any system tray; UTC is preserved in
  // the YAML on disk so audit consumers can still parse deterministic
  // ISO-8601.
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function NewPlaybookModal({
  onCancel,
  onConfirm,
  isSubmitting,
  error,
}: {
  onCancel: () => void;
  onConfirm: (name: string) => void;
  isSubmitting: boolean;
  error: string | null;
}) {
  const [name, setName] = useState("");
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="nerd-card w-[420px] border-keboola/30">
        <h2 className="font-bold text-base mb-2">New Playbook</h2>
        <p className="text-xs text-zinc-500 mb-3">
          Give the Playbook a name. You can fill in the SOP, connections,
          skills, and triggers after it shows up in the library.
        </p>
        <label
          htmlFor="new-playbook-name"
          className="block text-[10px] uppercase tracking-widest text-zinc-500 mb-1"
        >
          Name
        </label>
        <input
          id="new-playbook-name"
          type="text"
          autoFocus
          className="nerd-input w-full mb-3"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. Cross-source CRM Cleanup"
        />
        {error ? <div className="text-xs text-red-500 mb-2">{error}</div> : null}
        <div className="flex justify-end gap-2">
          <button
            type="button"
            className="nerd-btn"
            onClick={onCancel}
            disabled={isSubmitting}
          >
            Cancel
          </button>
          <button
            type="button"
            className="nerd-btn hover:text-keboola hover:border-keboola/40"
            disabled={!name.trim() || isSubmitting}
            onClick={() => onConfirm(name.trim())}
          >
            {isSubmitting ? "Creating..." : "Create"}
          </button>
        </div>
      </div>
    </div>
  );
}

function DeleteConfirmModal({
  playbookId,
  onCancel,
  onConfirm,
  isSubmitting,
  error,
}: {
  playbookId: string;
  onCancel: () => void;
  onConfirm: () => void;
  isSubmitting: boolean;
  error: string | null;
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="nerd-card w-[460px] border-red-300/60 dark:border-red-700/40">
        <h2 className="font-bold text-base mb-2 text-red-700 dark:text-red-400">
          Delete this Playbook?
        </h2>
        <p className="text-xs text-zinc-500 mb-3">
          The on-disk YAML at{" "}
          <code className="font-mono text-zinc-700 dark:text-zinc-300">
            playbooks/{playbookId}.yaml
          </code>{" "}
          will be removed. Run history is unaffected. This action cannot
          be undone.
        </p>
        {error ? <div className="text-xs text-red-500 mb-2">{error}</div> : null}
        <div className="flex justify-end gap-2">
          <button
            type="button"
            className="nerd-btn"
            onClick={onCancel}
            disabled={isSubmitting}
          >
            Cancel
          </button>
          <button
            type="button"
            className="nerd-btn hover:border-red-300 hover:text-red-500"
            disabled={isSubmitting}
            onClick={onConfirm}
          >
            {isSubmitting ? "Deleting..." : "Delete"}
          </button>
        </div>
      </div>
    </div>
  );
}
