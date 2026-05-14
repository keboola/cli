import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { ErrorBox, Loading, PageTitle } from "../components/Empty";
import { DataTable } from "../components/Table";
import { useUIState } from "../state";
import type { ProjectError, Schedule } from "../types";

interface SchedulesResp {
  schedules: Schedule[];
  errors: ProjectError[];
}

export function SchedulesPage() {
  const { project, branchId } = useUIState();
  const q = useQuery<SchedulesResp>({
    queryKey: ["schedules", project, branchId],
    queryFn: () =>
      api.get("/schedules", {
        query: {
          project: project ? [project] : undefined,
          branch_id: branchId ?? undefined,
        },
      }),
    enabled: !!project,
  });
  return (
    <div className="space-y-4">
      <PageTitle title="Schedules" description="Cron-based job triggers across the platform." />
      {q.isLoading ? <Loading /> : null}
      {q.error ? <ErrorBox message={(q.error as Error).message} /> : null}
      <DataTable
        rows={q.data?.schedules ?? []}
        rowKey={(s) => `${s.project_alias}/${s.schedule_id}`}
        columns={[
          { header: "Project", cell: (s) => <span className="text-keboola">{s.project_alias}</span> },
          { header: "Schedule ID", cell: (s) => <span className="text-zinc-500">{s.schedule_id}</span> },
          { header: "Cron", cell: (s) => <span className="font-bold text-accent">{s.cron_tab}</span> },
          { header: "TZ", cell: (s) => <span className="text-xs text-zinc-500">{s.timezone}</span> },
          {
            header: "State",
            cell: (s) =>
              s.state === "enabled" ? (
                <span className="nerd-pill-green">enabled</span>
              ) : (
                <span className="nerd-pill">disabled</span>
              ),
          },
          {
            header: "Target",
            cell: (s) =>
              s.target ? (
                <span className="text-xs">
                  <span className="text-accent">{s.target.component_id}</span> / {s.target.config_id}
                </span>
              ) : (
                <span className="text-xs text-zinc-600">-</span>
              ),
          },
        ]}
      />
    </div>
  );
}
