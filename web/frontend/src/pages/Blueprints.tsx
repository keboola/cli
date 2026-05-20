/**
 * Blueprints Catalogue — Phase 1 read-only surface.
 *
 * Curated, forkable Playbook templates. The catalogue is a static
 * in-code seed on the server (`agent_studio.blueprints_catalog`);
 * "Use this blueprint" forks it into a new draft Playbook and
 * navigates to the Playbooks library.
 *
 * Sources of truth:
 * - layout: `docs/mockups/02-blueprints-catalog.png`
 * - data shape: `docs/agents-v2.md` § 12 (Solution / Blueprint)
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { ErrorBox, Loading, PageTitle } from "../components/Empty";
import { useUIState } from "../state";

interface Blueprint {
  id: string;
  name: string;
  category: string;
  description: string;
  systems: string[];
  connections: string[];
  skills: string[];
  plugins: string[];
}

interface BlueprintsResponse {
  blueprints: Blueprint[];
}

// Mirrors BLUEPRINT_CATEGORIES on the server (agent_studio/models/
// blueprint.py). "All" is a UI-only pseudo-category that clears the
// filter.
const CATEGORIES = [
  "All",
  "Data Cleanup",
  "Process Mining",
  "Decision Analysis",
  "Decision Triggers",
  "Custom Agent Builder",
] as const;

export function BlueprintsPage() {
  const qc = useQueryClient();
  const { setPage } = useUIState();
  const [category, setCategory] = useState<(typeof CATEGORIES)[number]>("All");
  const [query, setQuery] = useState("");

  const q = useQuery<BlueprintsResponse>({
    queryKey: ["blueprints", category],
    queryFn: () =>
      api.get("/v1/agent-studio/blueprints", {
        query: category === "All" ? undefined : { category },
      }),
  });

  const forkMu = useMutation({
    mutationFn: (blueprintId: string) =>
      api.post(`/v1/agent-studio/blueprints/${blueprintId}/fork`, {}),
    onSuccess: () => {
      // The fork lands a new draft Playbook on disk; refresh the
      // library query and hop to the Playbooks page so the user sees
      // their new Playbook immediately.
      qc.invalidateQueries({ queryKey: ["playbooks"] });
      setPage("playbooks");
    },
  });

  const blueprints = (q.data?.blueprints ?? []).filter((b) => {
    if (!query.trim()) return true;
    const needle = query.toLowerCase();
    return (
      b.name.toLowerCase().includes(needle) ||
      b.description.toLowerCase().includes(needle) ||
      b.systems.some((s) => s.toLowerCase().includes(needle))
    );
  });

  return (
    <div className="space-y-4">
      <PageTitle
        title="Blueprints"
        description="Curated playbook templates grounded in Keboola data. Fork one to get a working Playbook in seconds."
      />

      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex gap-1.5 flex-wrap">
          {CATEGORIES.map((c) => (
            <button
              key={c}
              type="button"
              onClick={() => setCategory(c)}
              className={
                c === category
                  ? "nerd-btn border-keboola text-keboola"
                  : "nerd-btn"
              }
            >
              {c}
            </button>
          ))}
        </div>
        <input
          type="text"
          className="nerd-input w-56"
          placeholder="filter blueprints..."
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>

      {q.isLoading ? <Loading /> : null}
      {q.error ? <ErrorBox message={(q.error as Error).message} /> : null}
      {forkMu.error ? (
        <ErrorBox message={(forkMu.error as Error).message} />
      ) : null}

      {!q.isLoading && blueprints.length === 0 ? (
        <p className="text-sm text-zinc-500 italic">
          No blueprints match {query ? `"${query}"` : "this category"}.
        </p>
      ) : null}

      {blueprints.length > 0 ? (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          {blueprints.map((b) => (
            <BlueprintCard
              key={b.id}
              blueprint={b}
              onFork={() => forkMu.mutate(b.id)}
              forking={forkMu.isPending}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function BlueprintCard({
  blueprint,
  onFork,
  forking,
}: {
  blueprint: Blueprint;
  onFork: () => void;
  forking: boolean;
}) {
  return (
    <div className="nerd-card flex flex-col hover:border-keboola/30 transition-colors">
      <span className="nerd-pill font-mono w-fit mb-2 uppercase tracking-wider text-[10px]">
        {blueprint.category}
      </span>
      <h3 className="font-bold text-base mb-1">{blueprint.name}</h3>
      <p className="text-xs text-zinc-500 dark:text-zinc-500 line-clamp-2 min-h-[2.2em]">
        {blueprint.description}
      </p>
      <p className="text-[11px] font-mono text-zinc-500 mt-3 line-clamp-2 break-words">
        Systems: {blueprint.systems.join(" + ")}
      </p>
      <div className="mt-3 flex justify-end">
        <button
          type="button"
          className="nerd-btn hover:text-keboola hover:border-keboola/40"
          onClick={onFork}
          disabled={forking}
        >
          {forking ? "Forking..." : "Use this blueprint"}
        </button>
      </div>
    </div>
  );
}
