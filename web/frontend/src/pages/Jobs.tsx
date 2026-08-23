import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { Empty, ErrorBox, Loading, PageTitle } from "../components/Empty";
import { DataTable } from "../components/Table";
import { useUIState } from "../state";
import { useHashSelection } from "../useHashSelection";
import type { Job } from "../types";
import {
  formatDuration,
  JobActions,
  JobDetailDrawer,
  ProjectErrorsBanner,
  STATUS_COLORS,
  type JobsResp,
} from "./jobsShared";

export function JobsPage() {
  const { project, setPage } = useUIState();
  // Deep link: `?sel=<jobId>` opens that job's detail drawer.
  const [sel, setSel] = useHashSelection();
  const [statusFilter, setStatusFilter] = useState<string | null>(null);
  const [selected, setSelected] = useState<Job | null>(null);

  const q = useQuery<JobsResp>({
    queryKey: ["jobs", project, statusFilter],
    queryFn: () =>
      api.get("/jobs", {
        query: {
          project: project ?? undefined,
          status: statusFilter ?? undefined,
          limit: 100,
        },
      }),
    enabled: !!project,
    refetchInterval: 8000,
  });

  // Restore a deep-linked selection ONCE, after the first list load. Guarded
  // by a ref rather than by `selected`, so closing the drawer does not
  // immediately re-open it on the next poll.
  const restoredRef = useRef(false);
  useEffect(() => {
    if (restoredRef.current) return;
    if (!sel || !project) {
      restoredRef.current = true;
      return;
    }
    if (q.isLoading) return;
    restoredRef.current = true;
    const hit = q.data?.jobs.find((j) => String(j.id) === sel);
    if (hit) {
      setSelected(hit);
      return;
    }
    if (q.data) {
      // The list is capped at 100 rows, so a shared link to an older job will
      // miss. The drawer fetches its own detail by id anyway, so fall back to a
      // minimal row: the header stays sparse until that detail lands, and the
      // row-level actions (which need the component/config) stay hidden.
      setSelected({
        project_alias: project,
        id: sel,
        status: "",
        component: "",
        config: null,
        createdTime: "",
      });
    } else {
      // The list itself errored -- typically a foreign link whose project
      // alias this install does not know (TopBar is about to fall back to
      // the default project). Opening the synthetic drawer here would pin an
      // errored detail fetch to a project that is being swapped away, so
      // drop the deep link instead.
      setSel(null);
    }
  }, [sel, setSel, project, q.isLoading, q.data]);

  const openJob = (j: Job) => {
    setSelected(j);
    setSel(String(j.id));
  };
  const closeJob = () => {
    setSelected(null);
    setSel(null);
  };

  return (
    <div className="space-y-4">
      <PageTitle
        title="Jobs"
        description={`Recent Queue API jobs in ${project ?? "(no project)"} -- auto refreshing 8s`}
        actions={
          <button type="button" className="nerd-btn" onClick={() => setPage("jobs-all")}>
            All projects
          </button>
        }
      />
      <div className="flex gap-2">
        {[null, "success", "error", "processing", "warning"].map((s) => (
          <button
            key={s ?? "all"}
            type="button"
            className={`nerd-btn ${
              statusFilter === s ? "border-keboola text-keboola" : ""
            }`}
            onClick={() => setStatusFilter(s)}
          >
            {s ?? "all"}
          </button>
        ))}
      </div>
      {/* Single-project requests fan out over exactly one project, so this is
          normally empty -- but the envelope carries the same `errors` list and
          dropping it would make a failing project look like an idle one. */}
      <ProjectErrorsBanner errors={q.data?.errors ?? []} />
      {!project ? (
        <Empty title="Select a project from the top bar" />
      ) : q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox message={(q.error as Error).message} />
      ) : (
        <DataTable
          rows={q.data?.jobs ?? []}
          rowKey={(j) => String(j.id)}
          onRowClick={openJob}
          columns={[
            { header: "Job ID", cell: (j) => <span className="text-zinc-600 dark:text-zinc-400">{j.id}</span> },
            {
              header: "Status",
              cell: (j) => (
                <span className={STATUS_COLORS[j.status] ?? "nerd-pill"}>{j.status}</span>
              ),
            },
            { header: "Component", cell: (j) => <span className="text-accent">{j.component}</span> },
            {
              header: "Config",
              cell: (j) => <span className="text-zinc-500">{j.config ?? "—"}</span>,
            },
            {
              header: "Created",
              cell: (j) => <span className="text-zinc-500 text-xs">{j.createdTime}</span>,
            },
            {
              header: "Duration",
              align: "right",
              cell: (j) => (
                <span className="text-xs text-zinc-600 dark:text-zinc-400">
                  {j.durationSeconds != null ? formatDuration(j.durationSeconds) : "-"}
                </span>
              ),
            },
            {
              header: "Actions",
              align: "right",
              cell: (j) => <JobActions job={j} />,
            },
          ]}
        />
      )}

      {selected ? <JobDetailDrawer job={selected} onClose={closeJob} /> : null}
    </div>
  );
}
