/**
 * Playbook Library — Phase 1 read-mostly surface.
 *
 * The page shows every Playbook stored under
 * `<config_dir>/playbooks/*.yaml` as a card. Phase 1 has no run
 * logic so cards link only to a placeholder detail drawer (TODO in
 * the next slice). Empty state uses the same TwoPathEmpty pattern as
 * Agent Tasks to keep the new-user flow consistent.
 *
 * Source of truth for the layout is the design system mockup
 * `docs/mockups/01-playbooks-library.png` plus § 20.1 of
 * `docs/agents-v2.md`.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookOpen, Plus, Sparkles } from "lucide-react";
import { clsx } from "clsx";
import { useState } from "react";
import { api } from "../api/client";
import { ErrorBox, Loading, PageTitle, TwoPathEmpty } from "../components/Empty";

interface PlaybookSummary {
  id: string;
  name: string;
  description: string | null;
  revision: number;
  enabled: boolean;
  status:
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
  created_at: string;
  updated_at: string;
}

interface PlaybooksResponse {
  playbooks: PlaybookSummary[];
}

// `.nerd-pill-*` family stays the source of truth for outlined status
// colors. Map each status to one of the three buckets per design
// system § 2.3.
const STATUS_PILL_CLASS: Record<PlaybookSummary["status"], string> = {
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

const STATUS_LABEL: Record<PlaybookSummary["status"], string> = {
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
      api.post<PlaybookSummary>("/v1/agent-studio/playbooks", {
        name,
        description: null,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["playbooks"] });
      setCreating(false);
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
            <PlaybookCard key={p.id} playbook={p} />
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
    </div>
  );
}

function PlaybookCard({ playbook }: { playbook: PlaybookSummary }) {
  const pillClass = STATUS_PILL_CLASS[playbook.status];
  return (
    <div className="nerd-card hover:border-keboola/30 transition-colors">
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
    </div>
  );
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
