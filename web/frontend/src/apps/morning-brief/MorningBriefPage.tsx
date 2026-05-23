import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, Clock, PlayCircle, TrendingUp } from "lucide-react";
import { api } from "../../api/client";
import { Empty, ErrorBox, Loading, PageTitle } from "../../components/Empty";
import { DataTable } from "../../components/Table";
import { Drawer } from "../../components/Drawer";
import { JsonView } from "../../components/JsonView";
import type { Job, Project, ProjectError } from "../../types";
import {
  computeBrief,
  type BriefRow,
  type BriefSummary,
} from "./compute";

interface JobsResp {
  jobs: Job[];
  errors: ProjectError[];
}

const STATUS_PILL: Record<string, string> = {
  success: "nerd-pill-green",
  error: "nerd-pill-red",
  warning: "nerd-pill-amber",
  processing: "nerd-pill-amber",
  cancelled: "nerd-pill",
  terminated: "nerd-pill",
};

export function MorningBriefPage() {
  const [selected, setSelected] = useState<BriefRow | null>(null);

  const projectsQ = useQuery<{ projects: Project[] }>({
    queryKey: ["mb-projects"],
    queryFn: () => api.get("/projects"),
  });

  // Fetch a generous window of recent jobs across all projects. The /jobs
  // route accepts a `project` array; omitting it returns nothing on the
  // current backend, so we explicitly enumerate aliases once projects load.
  const aliases = useMemo(
    () => (projectsQ.data?.projects ?? []).map((p) => p.alias),
    [projectsQ.data],
  );

  const jobsQ = useQuery<JobsResp>({
    queryKey: ["mb-jobs", aliases.join(",")],
    queryFn: () =>
      api.get("/jobs", { query: { project: aliases, limit: 200 } }),
    enabled: aliases.length > 0,
    refetchInterval: 60_000,
  });

  const brief: BriefSummary = useMemo(
    () => computeBrief(jobsQ.data?.jobs ?? []),
    [jobsQ.data?.jobs],
  );

  if (projectsQ.isLoading) return <Loading label="loading projects" />;
  if (projectsQ.error) {
    return <ErrorBox message={(projectsQ.error as Error).message} />;
  }
  if (aliases.length === 0) {
    return (
      <Empty
        title="No projects registered"
        hint="Run `kbagent project add` first, then refresh."
      />
    );
  }

  return (
    <div className="space-y-6">
      <PageTitle
        title="Morning Brief"
        description={`Recent jobs across ${aliases.length} project${aliases.length === 1 ? "" : "s"} -- refresh every 60s`}
      />

      <KpiRow brief={brief} loading={jobsQ.isLoading} />

      {jobsQ.error ? (
        <ErrorBox message={(jobsQ.error as Error).message} />
      ) : null}

      {(jobsQ.data?.errors ?? []).length > 0 ? (
        <div className="nerd-card border-amber-300 text-amber-700 text-xs dark:border-amber-700/40 dark:text-amber-400">
          <div className="font-bold mb-1">Partial results</div>
          <ul className="space-y-0.5">
            {jobsQ.data!.errors.map((e) => (
              <li key={e.project_alias}>
                <span className="font-mono">{e.project_alias}</span>: {e.message}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <section className="space-y-2">
        <div className="flex items-baseline justify-between">
          <h2 className="text-sm font-bold text-zinc-900 dark:text-zinc-100">
            Outliers
            <span className="ml-2 text-zinc-500 text-xs font-normal">
              jobs running &gt;= 2x their config's recent median duration
            </span>
          </h2>
          <span className="text-xs text-zinc-500">
            {brief.outliers.length} flagged
          </span>
        </div>
        {brief.outliers.length === 0 ? (
          <Empty
            title="No outliers"
            hint="All recent jobs are within 2x median duration of their config."
          />
        ) : (
          <DataTable
            rows={brief.outliers}
            rowKey={(r) => `${r.job.project_alias}:${r.job.id}`}
            onRowClick={setSelected}
            columns={[
              {
                header: "Project",
                cell: (r) => (
                  <span className="text-zinc-600 dark:text-zinc-400">
                    {r.job.project_alias}
                  </span>
                ),
              },
              {
                header: "Status",
                cell: (r) => (
                  <span className={STATUS_PILL[r.job.status] ?? "nerd-pill"}>
                    {r.job.status}
                  </span>
                ),
              },
              {
                header: "Component",
                cell: (r) => <span className="text-accent">{r.job.component}</span>,
              },
              {
                header: "Config",
                cell: (r) => (
                  <span className="text-zinc-500">{r.job.configId}</span>
                ),
              },
              {
                header: "Duration",
                align: "right",
                cell: (r) => (
                  <span className="text-zinc-700 dark:text-zinc-300">
                    {formatDuration(r.job.durationSeconds ?? 0)}
                  </span>
                ),
              },
              {
                header: "Median",
                align: "right",
                cell: (r) => (
                  <span className="text-zinc-500 text-xs">
                    {formatDuration(r.medianSeconds)}
                  </span>
                ),
              },
              {
                header: "Factor",
                align: "right",
                cell: (r) => (
                  <span className="nerd-pill-amber">
                    {r.factor.toFixed(1)}x
                  </span>
                ),
              },
            ]}
          />
        )}
      </section>

      {selected ? (
        <OutlierDrawer row={selected} onClose={() => setSelected(null)} />
      ) : null}
    </div>
  );
}

function KpiRow({ brief, loading }: { brief: BriefSummary; loading: boolean }) {
  const cards: Array<{
    label: string;
    value: string;
    hint?: string;
    icon: React.ComponentType<{ className?: string }>;
    tone?: "neutral" | "good" | "warn";
  }> = [
    {
      label: "Jobs (last 24h)",
      value: loading ? "..." : String(brief.last24hCount),
      hint: brief.totalCount ? `${brief.totalCount} in window` : undefined,
      icon: PlayCircle,
    },
    {
      label: "Success rate",
      value: loading
        ? "..."
        : brief.totalCount === 0
          ? "-"
          : `${Math.round((brief.successCount / brief.totalCount) * 100)}%`,
      hint:
        brief.totalCount === 0
          ? undefined
          : `${brief.errorCount} errors, ${brief.warningCount} warnings`,
      icon: TrendingUp,
      tone: brief.errorCount === 0 ? "good" : "warn",
    },
    {
      label: "Total runtime",
      value: loading ? "..." : formatDuration(brief.totalDurationSeconds),
      hint:
        brief.longestJob === null
          ? undefined
          : `longest ${formatDuration(brief.longestJob.durationSeconds ?? 0)}`,
      icon: Clock,
    },
    {
      label: "Outliers",
      value: loading ? "..." : String(brief.outliers.length),
      hint: ">= 2x median duration",
      icon: AlertTriangle,
      tone: brief.outliers.length === 0 ? "good" : "warn",
    },
  ];
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3">
      {cards.map((c) => {
        const Icon = c.icon;
        return (
          <div key={c.label} className="nerd-card">
            <div className="flex items-start justify-between">
              <div>
                <div className="text-xs uppercase tracking-widest text-zinc-500">
                  {c.label}
                </div>
                <div
                  className={`mt-1 text-2xl font-bold ${
                    c.tone === "good"
                      ? "text-keboola"
                      : c.tone === "warn"
                        ? "text-amber-600 dark:text-amber-400"
                        : "text-zinc-900 dark:text-zinc-100"
                  }`}
                >
                  {c.value}
                </div>
                {c.hint ? (
                  <div className="text-xs text-zinc-500 mt-1">{c.hint}</div>
                ) : null}
              </div>
              <Icon className="w-4 h-4 text-zinc-400" />
            </div>
          </div>
        );
      })}
    </div>
  );
}

function OutlierDrawer({
  row,
  onClose,
}: {
  row: BriefRow;
  onClose: () => void;
}) {
  return (
    <Drawer
      open={true}
      onClose={onClose}
      title={`Job ${row.job.id}`}
      subtitle={`${row.job.project_alias} -- ${row.job.component}`}
    >
      <div className="space-y-4">
        <div className="nerd-card">
          <div className="text-xs uppercase tracking-widest text-zinc-500 mb-2">
            Why flagged
          </div>
          <div className="text-sm">
            Duration{" "}
            <span className="text-zinc-900 dark:text-zinc-100 font-mono">
              {formatDuration(row.job.durationSeconds ?? 0)}
            </span>{" "}
            is{" "}
            <span className="nerd-pill-amber">{row.factor.toFixed(1)}x</span>{" "}
            the median{" "}
            <span className="text-zinc-900 dark:text-zinc-100 font-mono">
              {formatDuration(row.medianSeconds)}
            </span>{" "}
            for{" "}
            <span className="text-accent font-mono">{row.job.component}</span>{" "}
            on config{" "}
            <span className="text-zinc-500 font-mono">{row.job.configId}</span>{" "}
            in this project ({row.sampleSize} recent runs).
          </div>
        </div>

        <div className="nerd-card">
          <div className="text-xs uppercase tracking-widest text-zinc-500 mb-2">
            Job payload
          </div>
          <JsonView data={row.job as unknown as Record<string, unknown>} />
        </div>

        <div className="flex gap-2">
          <button
            type="button"
            className="nerd-btn"
            onClick={() => {
              // Future: POST /agents/<analyse-job-cost>/run with row.job context.
              // For now the button is a stub so the design system + flow
              // is exercised without requiring an agent skill to exist.
              window.alert(
                "Analyse action stub. Wire to POST /agents/{id}/run later.",
              );
            }}
          >
            Analyse with AI (stub)
          </button>
          {row.job.url ? (
            <a
              href={row.job.url}
              target="_blank"
              rel="noopener noreferrer"
              className="nerd-btn"
            >
              Open in Keboola
            </a>
          ) : null}
        </div>
      </div>
    </Drawer>
  );
}

function formatDuration(sec: number): string {
  if (!sec || sec < 0) return "-";
  if (sec < 60) return `${Math.round(sec)}s`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  if (m < 60) return `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  const mr = m % 60;
  return `${h}h ${mr}m`;
}
