import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Plus, RefreshCw, Trash2, XCircle } from "lucide-react";
import { useEffect, useState } from "react";
import { ApiError, api } from "../api/client";
import { Empty, ErrorBox, Loading, PageTitle } from "../components/Empty";
import { JsonView } from "../components/JsonView";
import { DataTable } from "../components/Table";
import type { Project, ProjectStatus } from "../types";

export function ProjectsPage() {
  const qc = useQueryClient();
  const [showAdd, setShowAdd] = useState(false);
  const [selected, setSelected] = useState<Project | null>(null);

  const projectsQ = useQuery<{ projects: Project[] }>({
    queryKey: ["projects"],
    queryFn: () => api.get("/projects"),
  });
  const statusQ = useQuery<{ status: ProjectStatus[] }>({
    queryKey: ["projects-status"],
    queryFn: () => api.get("/projects/status"),
  });

  // /projects/status performs an opportunistic backfill of org_id/org_name
  // on the backend for projects registered before #290. Invalidate the
  // /projects cache once status finishes so the ORG column picks up the
  // freshly persisted values without the user having to reload the page.
  useEffect(() => {
    if (statusQ.data) qc.invalidateQueries({ queryKey: ["projects"] });
  }, [statusQ.data, qc]);

  const removeMu = useMutation({
    mutationFn: (alias: string) => api.delete(`/projects/${encodeURIComponent(alias)}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["projects"] }),
  });

  const useMu = useMutation({
    mutationFn: (alias: string) => api.post(`/projects/use/${encodeURIComponent(alias)}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["projects"] }),
  });

  const statusByAlias = new Map(statusQ.data?.status?.map((s) => [s.alias, s]) ?? []);

  return (
    <div className="space-y-4">
      <PageTitle
        title="Projects"
        description="Keboola projects registered in this kbagent config."
        actions={
          <>
            <button
              type="button"
              className="nerd-btn flex items-center gap-1"
              onClick={() => qc.invalidateQueries({ queryKey: ["projects-status"] })}
            >
              <RefreshCw className="w-3 h-3" /> Refresh status
            </button>
            <button
              type="button"
              className="nerd-btn flex items-center gap-1 hover:text-keboola"
              onClick={() => setShowAdd((v) => !v)}
            >
              <Plus className="w-3 h-3" /> Add project
            </button>
          </>
        }
      />

      {showAdd ? (
        <AddProject
          onDone={() => {
            setShowAdd(false);
            qc.invalidateQueries({ queryKey: ["projects"] });
          }}
        />
      ) : null}

      {projectsQ.isLoading ? <Loading /> : null}
      {projectsQ.error ? (
        <ErrorBox message={(projectsQ.error as Error).message} />
      ) : projectsQ.data?.projects.length === 0 ? (
        <Empty
          title="No projects configured"
          hint="Click 'Add project' to register your first Keboola project."
        />
      ) : (
        <DataTable
          rows={projectsQ.data?.projects ?? []}
          rowKey={(p) => p.alias}
          onRowClick={(p) => setSelected(p)}
          columns={[
            {
              header: "Alias",
              cell: (p) => (
                <span className="font-bold text-zinc-900 dark:text-zinc-100">
                  {p.alias} {p.is_default ? <span className="nerd-pill-green">default</span> : null}
                </span>
              ),
            },
            {
              header: "Org",
              cell: (p) => {
                // Org name comes from Manage API (via `org setup`); the
                // Storage token alone only returns the numeric id. We show
                // the name when we have it, fall back to "#73" so multi-org
                // setups are still distinguishable, and only render "—"
                // when neither is known (very old stacks without
                // organization in the verify response).
                if (p.org_name) {
                  return <span className="text-zinc-700 dark:text-zinc-300">{p.org_name}</span>;
                }
                if (p.org_id != null) {
                  return (
                    <span
                      className="text-zinc-500 font-mono"
                      title={`Organization #${p.org_id}. Name unknown — Storage API only exposes the id. Run \`kbagent org setup --org-id ${p.org_id} --url <stack>\` to populate the name.`}
                    >
                      #{p.org_id}
                    </span>
                  );
                }
                return (
                  <span
                    className="text-zinc-400"
                    title="Org info unavailable — older stacks omit the organization block from /v2/storage/tokens/verify. Use `kbagent org setup` to populate via the Manage API."
                  >
                    —
                  </span>
                );
              },
            },
            { header: "Project", cell: (p) => <span>{p.project_name}</span> },
            {
              header: "ID",
              cell: (p) => <span className="text-zinc-500">{p.project_id ?? "-"}</span>,
            },
            { header: "Stack", cell: (p) => <span className="text-zinc-500 text-xs">{p.stack_url}</span> },
            {
              header: "Status",
              cell: (p) => {
                const s = statusByAlias.get(p.alias);
                if (!s) return <span className="text-zinc-600">-</span>;
                return s.status === "ok" ? (
                  <span className="nerd-pill-green flex items-center gap-1 w-fit">
                    <CheckCircle2 className="w-3 h-3" /> {s.response_time_ms}ms
                  </span>
                ) : (
                  <span className="nerd-pill-red flex items-center gap-1 w-fit">
                    <XCircle className="w-3 h-3" /> {s.error_code}
                  </span>
                );
              },
            },
            {
              header: "Token",
              cell: (p) => <span className="text-zinc-500 text-xs">{p.token}</span>,
            },
            {
              header: "",
              align: "right",
              cell: (p) => (
                <div className="flex justify-end gap-1">
                  {!p.is_default ? (
                    <button
                      type="button"
                      className="nerd-btn text-xs"
                      onClick={(e) => {
                        e.stopPropagation();
                        useMu.mutate(p.alias);
                      }}
                    >
                      Make default
                    </button>
                  ) : null}
                  <button
                    type="button"
                    className="nerd-btn text-xs hover:text-red-400 hover:border-red-700"
                    onClick={(e) => {
                      e.stopPropagation();
                      if (confirm(`Remove project '${p.alias}' from kbagent?`)) {
                        removeMu.mutate(p.alias);
                      }
                    }}
                  >
                    <Trash2 className="w-3 h-3" />
                  </button>
                </div>
              ),
            },
          ]}
        />
      )}

      {selected ? (
        <div className="nerd-card">
          <div className="flex items-center justify-between mb-3">
            <h3 className="font-bold text-keboola">Project: {selected.alias}</h3>
            <button type="button" className="nerd-btn text-xs" onClick={() => setSelected(null)}>
              Close
            </button>
          </div>
          <ProjectInfo alias={selected.alias} />
        </div>
      ) : null}
    </div>
  );
}

function ProjectInfo({ alias }: { alias: string }) {
  const infoQ = useQuery<Record<string, unknown>>({
    queryKey: ["project-info", alias],
    queryFn: () => api.get(`/projects/${encodeURIComponent(alias)}/info`),
  });
  if (infoQ.isLoading) return <Loading />;
  if (infoQ.error) return <ErrorBox message={(infoQ.error as Error).message} />;
  return <JsonView data={infoQ.data} />;
}

function AddProject({ onDone }: { onDone: () => void }) {
  const [alias, setAlias] = useState("");
  const [stackUrl, setStackUrl] = useState("https://connection.keboola.com");
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);

  const mu = useMutation({
    mutationFn: () => api.post("/projects", { alias, stack_url: stackUrl, token }),
    onSuccess: () => {
      onDone();
      setAlias("");
      setToken("");
    },
    onError: (err) => {
      setError(err instanceof ApiError ? err.message : (err as Error).message);
    },
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
      <h3 className="font-bold text-keboola">Add new project</h3>
      <div className="grid grid-cols-3 gap-3">
        <label className="text-xs text-zinc-400">
          Alias
          <input
            className="nerd-input w-full mt-1"
            value={alias}
            onChange={(e) => setAlias(e.target.value)}
            required
            placeholder="my-project"
          />
        </label>
        <label className="text-xs text-zinc-400">
          Stack URL
          <input
            className="nerd-input w-full mt-1"
            value={stackUrl}
            onChange={(e) => setStackUrl(e.target.value)}
            required
          />
        </label>
        <label className="text-xs text-zinc-400">
          Storage Token
          <input
            type="password"
            className="nerd-input w-full mt-1"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            required
            placeholder="123-12345-..."
          />
        </label>
      </div>
      {error ? <div className="text-red-400 text-xs">{error}</div> : null}
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={mu.isPending}
          className="nerd-btn hover:text-keboola"
        >
          {mu.isPending ? "Adding..." : "Add project"}
        </button>
        <button type="button" className="nerd-btn" onClick={onDone}>
          Cancel
        </button>
      </div>
    </form>
  );
}
