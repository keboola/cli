import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pause, Play, Trash2 } from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import { Empty, ErrorBox, Loading, PageTitle } from "../components/Empty";
import { JsonView } from "../components/JsonView";
import { DataTable } from "../components/Table";
import { useUIState } from "../state";
import type { DataApp, ProjectError } from "../types";

interface DataAppsResp {
  apps: DataApp[];
  errors: ProjectError[];
}

const STATE_STYLE: Record<string, string> = {
  running: "nerd-pill-green",
  starting: "nerd-pill-amber",
  stopping: "nerd-pill-amber",
  stopped: "nerd-pill",
  error: "nerd-pill-red",
};

export function DataAppsPage() {
  const { project, branchId } = useUIState();
  const qc = useQueryClient();
  const [selected, setSelected] = useState<DataApp | null>(null);
  const q = useQuery<DataAppsResp>({
    queryKey: ["data-apps", project, branchId],
    queryFn: () =>
      api.get("/data-apps", { query: { project: project ?? undefined, branch_id: branchId ?? undefined } }),
    enabled: !!project,
  });
  const startMu = useMutation({
    mutationFn: ({ alias, appId }: { alias: string; appId: string }) =>
      api.post(`/data-apps/${encodeURIComponent(alias)}/${encodeURIComponent(appId)}/start`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["data-apps"] }),
  });
  const stopMu = useMutation({
    mutationFn: ({ alias, appId }: { alias: string; appId: string }) =>
      api.post(`/data-apps/${encodeURIComponent(alias)}/${encodeURIComponent(appId)}/stop`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["data-apps"] }),
  });
  const delMu = useMutation({
    mutationFn: ({ alias, appId }: { alias: string; appId: string }) =>
      api.delete(`/data-apps/${encodeURIComponent(alias)}/${encodeURIComponent(appId)}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["data-apps"] }),
  });
  return (
    <div className="space-y-4">
      <PageTitle title="Data Apps" description="Custom Streamlit / Python data apps deployed on Keboola." />
      {!project ? (
        <Empty title="Select a project" />
      ) : q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox message={(q.error as Error).message} />
      ) : (
        <DataTable
          rows={q.data?.apps ?? []}
          rowKey={(a) => `${a.project_alias}/${a.app_id}`}
          onRowClick={(a) => setSelected(a)}
          columns={[
            { header: "App", cell: (a) => <span className="font-bold">{a.name}</span> },
            { header: "ID", cell: (a) => <span className="text-zinc-500 text-xs">{a.app_id}</span> },
            { header: "Type", cell: (a) => <span className="text-zinc-400">{a.type}</span> },
            {
              header: "State",
              cell: (a) => <span className={STATE_STYLE[a.state] ?? "nerd-pill"}>{a.state}</span>,
            },
            { header: "Size", cell: (a) => <span className="text-zinc-500">{a.size}</span> },
            { header: "URL", cell: (a) => a.url ? <a className="text-accent text-xs" href={a.url} target="_blank" rel="noreferrer">open</a> : null },
            {
              header: "",
              align: "right",
              cell: (a) => (
                <div className="flex justify-end gap-1">
                  {a.state === "running" ? (
                    <button
                      type="button"
                      className="nerd-btn text-xs"
                      onClick={(e) => {
                        e.stopPropagation();
                        stopMu.mutate({ alias: a.project_alias, appId: a.app_id });
                      }}
                    >
                      <Pause className="w-3 h-3" />
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="nerd-btn text-xs hover:text-keboola"
                      onClick={(e) => {
                        e.stopPropagation();
                        startMu.mutate({ alias: a.project_alias, appId: a.app_id });
                      }}
                    >
                      <Play className="w-3 h-3" />
                    </button>
                  )}
                  <button
                    type="button"
                    className="nerd-btn text-xs hover:text-red-400 hover:border-red-700"
                    onClick={(e) => {
                      e.stopPropagation();
                      if (confirm(`Delete app ${a.name}?`))
                        delMu.mutate({ alias: a.project_alias, appId: a.app_id });
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
            <h3 className="font-bold text-keboola">{selected.name}</h3>
            <button type="button" className="nerd-btn text-xs" onClick={() => setSelected(null)}>
              Close
            </button>
          </div>
          <JsonView data={selected} />
        </div>
      ) : null}
    </div>
  );
}
