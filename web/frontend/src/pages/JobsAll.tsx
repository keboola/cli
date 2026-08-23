import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { Empty, ErrorBox, Loading, PageTitle } from "../components/Empty";
import { DataTable } from "../components/Table";
import { calculateJobCredits, formatCredits, sumJobCredits } from "../config/credits";
import { formatRelativeTime } from "../lib/time";
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

/**
 * Cross-project jobs feed: one `GET /jobs` call with no `project` param, the
 * server fans out over every registered project in parallel and returns the
 * merged `{jobs, errors}` envelope with `project_alias` stamped on each row.
 *
 * The page deliberately ignores the active project in the top bar -- switching
 * projects must not change what "all jobs" means.
 */

/**
 * `limit` is PER PROJECT, not for the merged list: the server asks each
 * project for this many rows and then merges. 50 keeps a twenty-project
 * install answering in reasonable time while still covering more than a day
 * of activity for most projects.
 */
const PER_PROJECT_LIMIT = 50;

/**
 * Statuses the Queue API accepts. Passed straight through per project; the
 * leading `null` is the unfiltered view.
 */
const STATUS_FILTERS: Array<string | null> = [
  null,
  "processing",
  "waiting",
  "success",
  "error",
  "warning",
  "terminated",
  "cancelled",
];

export function JobsAllPage() {
  const { setPage } = useUIState();
  // Deep link: `?sel=<projectAlias>/<jobId>`. Job ids are only unique WITHIN a
  // project, so the alias is part of the key -- a bare id would open the wrong
  // project's job on a merged list.
  const [sel, setSel] = useHashSelection();
  const [statusFilter, setStatusFilter] = useState<string | null>(null);
  const [selected, setSelected] = useState<Job | null>(null);

  const q = useQuery<JobsResp>({
    queryKey: ["jobs-all", statusFilter],
    queryFn: () =>
      api.get("/jobs", {
        query: {
          // No `project`: that omission IS the fan-out switch server-side.
          status: statusFilter ?? undefined,
          limit: PER_PROJECT_LIMIT,
          sort_by: "createdTime",
          sort_order: "desc",
        },
      }),
    // Deliberately slower than the per-project page's 8s: every tick here is
    // one Queue API call PER REGISTERED PROJECT, so the same cadence would
    // multiply the load on the stack by the size of the install.
    refetchInterval: 15_000,
  });

  const jobs = q.data?.jobs ?? [];
  const errors = q.data?.errors ?? [];
  // Sum over the rows actually on screen, so the headline figure moves with
  // the status filter instead of claiming to describe the whole project.
  const totalCredits = sumJobCredits(jobs);

  // Restore a deep-linked selection ONCE, after the first list load. Guarded
  // by a ref rather than by `selected`, so closing the drawer does not
  // immediately re-open it on the next poll.
  const restoredRef = useRef(false);
  useEffect(() => {
    if (restoredRef.current) return;
    if (!sel) {
      restoredRef.current = true;
      return;
    }
    if (q.isLoading) return;
    restoredRef.current = true;
    const slash = sel.indexOf("/");
    if (slash <= 0) {
      // Not a `<alias>/<id>` pair -- nothing addressable.
      setSel(null);
      return;
    }
    const alias = sel.slice(0, slash);
    const jobId = sel.slice(slash + 1);
    const hit = q.data?.jobs.find(
      (j) => j.project_alias === alias && String(j.id) === jobId,
    );
    if (hit) {
      setSelected(hit);
      return;
    }
    if (q.data) {
      // The list is capped per project, so a shared link to an older job will
      // miss. The drawer fetches its own detail by alias+id anyway, so fall
      // back to a minimal row: the header stays sparse until that detail
      // lands, and the row-level actions (which need the component/config)
      // stay hidden.
      setSelected({
        project_alias: alias,
        id: jobId,
        status: "",
        component: "",
        config: null,
        createdTime: "",
      });
    } else {
      // The list itself errored, so we cannot tell whether that alias is even
      // registered here. Pinning an errored detail fetch to it would show a
      // second failure with no more information; drop the deep link instead.
      setSel(null);
    }
  }, [sel, setSel, q.isLoading, q.data]);

  const openJob = (j: Job) => {
    setSelected(j);
    setSel(`${j.project_alias}/${j.id}`);
  };
  const closeJob = () => {
    setSelected(null);
    setSel(null);
  };

  return (
    <div className="space-y-4">
      <PageTitle
        title="All Jobs"
        description={
          totalCredits > 0
            ? `Jobs across all projects · ~${formatCredits(totalCredits)} credits (shown jobs, estimated)`
            : "Jobs across all projects"
        }
        actions={
          <button type="button" className="nerd-btn" onClick={() => setPage("jobs")}>
            Current project only
          </button>
        }
      />
      <div className="flex flex-wrap gap-2">
        {STATUS_FILTERS.map((s) => (
          <button
            key={s ?? "all"}
            type="button"
            className={`nerd-btn ${statusFilter === s ? "border-keboola text-keboola" : ""}`}
            onClick={() => setStatusFilter(s)}
          >
            {s ?? "all"}
          </button>
        ))}
      </div>

      <ProjectErrorsBanner errors={errors} />

      {q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox message={(q.error as Error).message} />
      ) : jobs.length === 0 ? (
        <Empty
          title="No jobs"
          hint={
            statusFilter
              ? `No ${statusFilter} jobs in any registered project.`
              : "No jobs in any registered project."
          }
        />
      ) : (
        <DataTable
          rows={jobs}
          rowKey={(j) => `${j.project_alias}-${j.id}`}
          onRowClick={openJob}
          columns={[
            {
              header: "Project",
              cell: (j) => <span className="text-xs text-zinc-500">{j.project_alias}</span>,
            },
            {
              header: "Job ID",
              cell: (j) => <span className="text-zinc-600 dark:text-zinc-400">{j.id}</span>,
            },
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
              header: "Duration",
              align: "right",
              cell: (j) => (
                <span className="text-xs text-zinc-600 dark:text-zinc-400">
                  {j.durationSeconds != null ? formatDuration(j.durationSeconds) : "-"}
                </span>
              ),
            },
            {
              header: "Credits",
              align: "right",
              cell: (j) => (
                // Estimate, not a billing figure -- see config/credits.ts. A
                // job with no duration has nothing to estimate from.
                <span
                  className="font-mono text-xs text-zinc-600 dark:text-zinc-400"
                  title="Estimated from duration and container size — not a billing figure"
                >
                  {j.durationSeconds != null ? formatCredits(calculateJobCredits(j)) : "—"}
                </span>
              ),
            },
            {
              header: "Created",
              cell: (j) => (
                <span className="text-zinc-500 text-xs" title={j.createdTime}>
                  {formatRelativeTime(j.createdTime)}
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
