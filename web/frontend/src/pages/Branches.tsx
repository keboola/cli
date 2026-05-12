import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { GitBranch, GitMerge, Plus, RotateCcw, Trash2 } from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import { Empty, ErrorBox, Loading, PageTitle } from "../components/Empty";
import { DataTable } from "../components/Table";
import { useUIState } from "../state";
import type { Branch, ProjectError } from "../types";

interface BranchesResp {
  branches: Branch[];
  errors: ProjectError[];
  active_branches: Record<string, number | null>;
}

export function BranchesPage() {
  const { project, setBranchId } = useUIState();
  const qc = useQueryClient();
  const [creating, setCreating] = useState(false);

  const q = useQuery<BranchesResp>({
    queryKey: ["branches", project],
    queryFn: () => api.get("/branches", { query: { project: project ?? undefined } }),
    enabled: !!project,
  });

  const useMu = useMutation({
    mutationFn: ({ alias, branchId }: { alias: string; branchId: number }) =>
      api.post(`/branches/${encodeURIComponent(alias)}/use`, { branch_id: branchId }),
    onSuccess: (_, vars) => {
      setBranchId(vars.branchId);
      qc.invalidateQueries({ queryKey: ["branches"] });
    },
  });
  const resetMu = useMutation({
    mutationFn: (alias: string) => api.post(`/branches/${encodeURIComponent(alias)}/reset`),
    onSuccess: () => {
      setBranchId(null);
      qc.invalidateQueries({ queryKey: ["branches"] });
    },
  });
  const deleteMu = useMutation({
    mutationFn: ({ alias, branchId }: { alias: string; branchId: number }) =>
      api.delete(`/branches/${encodeURIComponent(alias)}/${branchId}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["branches"] }),
  });

  const branches = q.data?.branches ?? [];
  const activeId = project ? q.data?.active_branches?.[project] ?? null : null;

  return (
    <div className="space-y-4">
      <PageTitle
        title="Branches"
        description={`Development branches in ${project ?? "(no project)"}`}
        actions={
          <>
            <button
              type="button"
              className="nerd-btn flex items-center gap-1"
              onClick={() => project && resetMu.mutate(project)}
              disabled={!project || activeId === null}
            >
              <RotateCcw className="w-3 h-3" /> Reset to main
            </button>
            <button
              type="button"
              className="nerd-btn flex items-center gap-1 hover:text-keboola"
              onClick={() => setCreating(true)}
              disabled={!project}
            >
              <Plus className="w-3 h-3" /> Create branch
            </button>
          </>
        }
      />
      {creating && project ? (
        <CreateBranch
          alias={project}
          onDone={() => {
            setCreating(false);
            qc.invalidateQueries({ queryKey: ["branches"] });
          }}
        />
      ) : null}
      {!project ? (
        <Empty title="Select a project" />
      ) : q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox message={(q.error as Error).message} />
      ) : (
        <DataTable
          rows={branches}
          rowKey={(b) => `${b.project_alias}/${b.id}`}
          columns={[
            {
              header: "Branch",
              cell: (b) => (
                <span className="flex items-center gap-2">
                  <GitBranch className="w-3 h-3 text-zinc-500" />
                  <span className={b.id === activeId ? "text-keboola font-bold" : ""}>{b.name}</span>
                  {b.isDefault ? <span className="nerd-pill-green">main</span> : null}
                  {b.id === activeId ? <span className="nerd-pill-amber">active</span> : null}
                </span>
              ),
            },
            { header: "ID", cell: (b) => <span className="text-zinc-500">{b.id}</span> },
            { header: "Description", cell: (b) => <span className="text-zinc-500 text-xs">{b.description}</span> },
            { header: "Created", cell: (b) => <span className="text-zinc-500 text-xs">{b.created}</span> },
            {
              header: "",
              align: "right",
              cell: (b) => (
                <div className="flex justify-end gap-1">
                  {!b.isDefault && b.id !== activeId ? (
                    <button
                      type="button"
                      className="nerd-btn text-xs hover:text-keboola"
                      onClick={() => useMu.mutate({ alias: b.project_alias, branchId: b.id })}
                    >
                      Switch
                    </button>
                  ) : null}
                  {!b.isDefault ? (
                    <button
                      type="button"
                      className="nerd-btn text-xs hover:text-red-400 hover:border-red-700"
                      onClick={() => {
                        if (confirm(`Delete branch '${b.name}'?`))
                          deleteMu.mutate({ alias: b.project_alias, branchId: b.id });
                      }}
                    >
                      <Trash2 className="w-3 h-3" />
                    </button>
                  ) : (
                    <span className="nerd-pill flex items-center gap-1">
                      <GitMerge className="w-3 h-3" /> default
                    </span>
                  )}
                </div>
              ),
            },
          ]}
        />
      )}
    </div>
  );
}

function CreateBranch({ alias, onDone }: { alias: string; onDone: () => void }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);
  const mu = useMutation({
    mutationFn: () => api.post(`/branches/${encodeURIComponent(alias)}`, { name, description }),
    onSuccess: () => onDone(),
    onError: (err) => setError((err as Error).message),
  });
  return (
    <form
      className="nerd-card space-y-3"
      onSubmit={(e) => {
        e.preventDefault();
        setError(null);
        mu.mutate();
      }}
    >
      <h3 className="font-bold text-keboola">New branch in {alias}</h3>
      <input
        className="nerd-input w-full"
        placeholder="branch name"
        value={name}
        onChange={(e) => setName(e.target.value)}
        required
      />
      <input
        className="nerd-input w-full"
        placeholder="description (optional)"
        value={description}
        onChange={(e) => setDescription(e.target.value)}
      />
      {error ? <div className="text-red-400 text-xs">{error}</div> : null}
      <div className="flex gap-2">
        <button type="submit" className="nerd-btn hover:text-keboola" disabled={mu.isPending}>
          {mu.isPending ? "Creating..." : "Create"}
        </button>
        <button type="button" className="nerd-btn" onClick={onDone}>
          Cancel
        </button>
      </div>
    </form>
  );
}
