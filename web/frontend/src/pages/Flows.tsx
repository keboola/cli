import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { Empty, ErrorBox, Loading, PageTitle } from "../components/Empty";
import { JsonView } from "../components/JsonView";
import { DataTable } from "../components/Table";
import { useUIState } from "../state";
import type { Flow, ProjectError } from "../types";

interface FlowsResp {
  flows: Flow[];
  errors: ProjectError[];
}

export function FlowsPage() {
  const { project, branchId } = useUIState();
  const [selected, setSelected] = useState<Flow | null>(null);
  const q = useQuery<FlowsResp>({
    queryKey: ["flows", project, branchId],
    queryFn: () =>
      api.get("/flows", {
        query: { project: project ?? undefined, branch_id: branchId ?? undefined, with_schedules: true },
      }),
    enabled: !!project,
  });
  return (
    <div className="space-y-4">
      <PageTitle title="Flows" description="Orchestrator and flow component configurations." />
      {!project ? (
        <Empty title="Select a project" />
      ) : q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox message={(q.error as Error).message} />
      ) : (
        <DataTable
          rows={q.data?.flows ?? []}
          rowKey={(f) => `${f.project_alias}/${f.component_id}/${f.config_id}`}
          onRowClick={(f) => setSelected(f)}
          columns={[
            { header: "Name", cell: (f) => <span className="font-bold">{f.name}</span> },
            { header: "Component", cell: (f) => <span className="text-accent text-xs">{f.component_id}</span> },
            { header: "ID", cell: (f) => <span className="text-zinc-500">{f.config_id}</span> },
            {
              header: "State",
              cell: (f) =>
                f.is_disabled ? <span className="nerd-pill">disabled</span> : <span className="nerd-pill-green">enabled</span>,
            },
            {
              header: "Schedules",
              cell: (f) =>
                f.schedules?.length ? (
                  <span className="text-xs">{f.schedules.length} schedule(s)</span>
                ) : (
                  <span className="text-xs text-zinc-600">-</span>
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
          <FlowDetail flow={selected} />
        </div>
      ) : null}
    </div>
  );
}

function FlowDetail({ flow }: { flow: Flow }) {
  const { branchId } = useUIState();
  const q = useQuery({
    queryKey: ["flow-detail", flow.project_alias, flow.component_id, flow.config_id, branchId],
    queryFn: () =>
      api.get(`/flows/${encodeURIComponent(flow.project_alias)}/${encodeURIComponent(flow.config_id)}`, {
        query: { component_id: flow.component_id, branch_id: branchId ?? undefined },
      }),
  });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox message={(q.error as Error).message} />;
  return <JsonView data={q.data} />;
}
