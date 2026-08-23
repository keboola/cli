/**
 * Pieces shared by the per-project Jobs page and the cross-project All Jobs
 * page: status colouring, the terminate guard, duration formatting, the
 * row/drawer action buttons and the detail drawer itself.
 *
 * Everything here takes the project alias from the JOB ROW (`project_alias`),
 * never from the page's active project. That is what makes the same drawer and
 * the same action buttons work on a merged, multi-project list -- and it costs
 * the per-project page nothing, because its rows carry the same field.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Clock,
  Cpu,
  FileCode,
  Play,
  RotateCw,
  Server,
  Square,
  Timer,
  User,
  XOctagon,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api, sseSubscribe } from "../api/client";
import { ConfirmModal } from "../components/ConfirmModal";
import { Drawer } from "../components/Drawer";
import { ErrorBox, Loading } from "../components/Empty";
import { JsonView } from "../components/JsonView";
import type { Job, ProjectError } from "../types";

/**
 * `GET /jobs` envelope. `errors` is per-project and always present: with no
 * `project` param the server fans out over every registered project, and one
 * project failing must not blank the other twenty rows.
 */
export interface JobsResp {
  jobs: Job[];
  errors: ProjectError[];
}

export const STATUS_COLORS: Record<string, string> = {
  success: "nerd-pill-green",
  error: "nerd-pill-red",
  warning: "nerd-pill-amber",
  processing: "nerd-pill-amber",
  cancelled: "nerd-pill",
  terminated: "nerd-pill",
};

/**
 * Statuses the Queue API will actually accept a terminate for. A terminal job
 * (success / error / ...) has nothing left to stop, so we hide the button
 * rather than let the user discover that by getting a 4xx back.
 */
export const TERMINABLE_STATUSES = new Set(["created", "waiting", "processing"]);

/**
 * Human label for a job's target: `component ・ config <id>`, dropping the
 * config half entirely when the job carries none. A job run from an inline
 * `configData` payload has no stored configuration, and rendering the raw
 * value there produced a literal "config undefined" in the drawer header.
 */
export function jobLabel(job: Job): string {
  return job.config ? `${job.component} ・ config ${job.config}` : job.component;
}

/**
 * The job's branch as the `JobRun` body wants it: an integer, or `undefined`
 * for the default branch. The Queue API is inconsistent about whether
 * `branchId` arrives numeric or as a string, and the router declares
 * `branch_id: int | None`, so anything non-numeric is dropped rather than
 * sent as a value FastAPI would reject.
 */
export function jobBranchId(job: Job): number | undefined {
  if (job.branchId === null || job.branchId === undefined) return undefined;
  const n = Number(job.branchId);
  return Number.isFinite(n) ? n : undefined;
}

export function formatDuration(sec: number): string {
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  if (m < 60) return `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  const mr = m % 60;
  return `${h}h ${mr}m`;
}

/**
 * Per-job Re-run / Terminate actions, shared by the table row and the detail
 * drawer header.
 *
 * Re-run posts the job's OWN component + config + branch to
 * `POST /jobs/{p}/run`, i.e. it starts a fresh job from the configuration as
 * it stands NOW -- it does not replay the historical `configData` the old job
 * ran with. That is the same semantics as `kbagent job run`, and the only
 * thing the Queue API offers. The branch IS preserved, though: see
 * `jobBranchId` and the comment on the mutation body.
 *
 * Terminate goes through `POST /jobs/{p}/terminate` with an explicit
 * `job_ids` list; the filter form of that endpoint (status / component) is
 * deliberately not exposed here -- one row, one job.
 */
export function JobActions({ job, compact = true }: { job: Job; compact?: boolean }) {
  const qc = useQueryClient();
  const [confirm, setConfirm] = useState<"terminate" | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Both job lists are invalidated unconditionally, not just the one this
  // button happens to be rendered on: re-running or terminating a job changes
  // what the per-project list AND the cross-project list should show, and the
  // component cannot tell which of them mounted it.
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["jobs"] });
    qc.invalidateQueries({ queryKey: ["jobs-all"] });
    qc.invalidateQueries({ queryKey: ["dashboard-jobs"] });
  };

  const rerun = useMutation({
    mutationFn: () =>
      api.post(`/jobs/${encodeURIComponent(job.project_alias)}/run`, {
        component_id: job.component,
        config_id: job.config,
        // Branch fidelity: omitting this resolves to the DEFAULT branch
        // server-side, so a job that originally ran against a dev-branch
        // config would silently re-run against the production one -- a
        // different configuration, writing to different tables. The row
        // already carries the branch, so pass it straight back.
        branch_id: jobBranchId(job),
      }),
    onError: (e) => setError((e as Error).message),
    onSuccess: () => {
      setError(null);
      invalidate();
    },
  });

  const terminate = useMutation({
    mutationFn: () =>
      api.post(`/jobs/${encodeURIComponent(job.project_alias)}/terminate`, {
        job_ids: [String(job.id)],
        dry_run: false,
      }),
    onError: (e) => setError((e as Error).message),
    onSuccess: () => {
      setError(null);
      setConfirm(null);
      invalidate();
    },
  });

  // A job started from an inline `configData` payload has no stored
  // configuration to re-run, so `config` is null and the button is hidden.
  const canRerun = !!job.component && !!job.config;
  const canTerminate = TERMINABLE_STATUSES.has(job.status);
  const btn = `nerd-btn ${compact ? "text-[10px] py-0.5 px-1.5" : "text-xs"} flex items-center gap-1 disabled:opacity-50`;

  return (
    <span
      className="inline-flex items-center gap-1.5"
      // Row clicks open the detail drawer; an action click must not.
      onClick={(e) => e.stopPropagation()}
      role="presentation"
    >
      {error ? (
        <span className="text-[10px] text-red-600 dark:text-red-400 max-w-[16rem] truncate" title={error}>
          {error}
        </span>
      ) : null}
      {canRerun ? (
        <button
          type="button"
          className={`${btn} hover:text-keboola`}
          disabled={rerun.isPending}
          onClick={() => rerun.mutate()}
          // Name the branch in the tooltip: the whole point of threading
          // branch_id through is that the user can trust where this lands.
          title={`Start a new job for ${job.component} / ${job.config} on ${
            jobBranchId(job) === undefined ? "the default branch" : `branch #${jobBranchId(job)}`
          }`}
        >
          <RotateCw className={`w-3 h-3 ${rerun.isPending ? "animate-spin" : ""}`} />
          {rerun.isPending ? "starting…" : "re-run"}
        </button>
      ) : null}
      {canTerminate ? (
        <button
          type="button"
          className={`${btn} hover:text-red-600 dark:hover:text-red-400`}
          disabled={terminate.isPending}
          onClick={() => setConfirm("terminate")}
          title="Terminate this job"
        >
          <XOctagon className="w-3 h-3" /> terminate
        </button>
      ) : null}
      {confirm === "terminate" ? (
        <ConfirmModal
          danger
          busy={terminate.isPending}
          title="Terminate job?"
          body={
            <>
              Job <span className="font-mono text-accent">{String(job.id)}</span> (
              {jobLabel(job)}) is <span className="font-mono">{job.status}</span>.
              Terminating stops it where it is — partially written output stays written.
            </>
          }
          confirmLabel="Terminate"
          onConfirm={() => terminate.mutate()}
          onCancel={() => setConfirm(null)}
        />
      ) : null}
    </span>
  );
}

export function JobDetailDrawer({ job, onClose }: { job: Job; onClose: () => void }) {
  const detailQ = useQuery<Record<string, unknown>>({
    queryKey: ["job-detail", job.project_alias, job.id],
    queryFn: () =>
      api.get(
        `/jobs/${encodeURIComponent(job.project_alias)}/${encodeURIComponent(String(job.id))}`,
      ),
  });
  const [logs, setLogs] = useState<
    Array<{ id: number | string; message: string; type?: string }>
  >([]);
  const [streaming, setStreaming] = useState(false);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    return () => {
      esRef.current?.close();
    };
  }, []);

  const startStream = () => {
    setLogs([]);
    setStreaming(true);
    const es = sseSubscribe(
      `/jobs/${encodeURIComponent(job.project_alias)}/${encodeURIComponent(String(job.id))}/stream`,
      undefined,
      {
        log: (data) => {
          const ev = data as { id: number | string; message: string; type?: string };
          setLogs((l) => [...l, ev]);
        },
        status: (data) => {
          const ev = data as { status: string };
          setLogs((l) => [...l, { id: `s-${Date.now()}`, message: `→ status: ${ev.status}` }]);
        },
        done: (data) => {
          const ev = data as { final: string };
          setLogs((l) => [...l, { id: `d-${Date.now()}`, message: `✓ done: ${ev.final}` }]);
          setStreaming(false);
          es.close();
        },
      },
    );
    esRef.current = es;
  };

  const detail = detailQ.data ?? {};

  return (
    <Drawer
      open={true}
      onClose={onClose}
      title={`Job ${job.id}`}
      subtitle={jobLabel(job)}
      width="max-w-5xl"
      actions={
        <>
          {/* Status comes from the freshly fetched detail when available, so a
              job that finished while the drawer was open loses its Terminate
              button on the next poll instead of offering a doomed call. */}
          <JobActions
            job={{ ...job, status: String(detailQ.data?.status ?? job.status) }}
            compact={false}
          />
          <button
            type="button"
            className="nerd-btn flex items-center gap-1 hover:text-keboola"
            onClick={() => (streaming ? esRef.current?.close() : startStream())}
          >
            {streaming ? (
              <>
                <Square className="w-3 h-3" /> stop stream
              </>
            ) : (
              <>
                <Play className="w-3 h-3" /> stream logs
              </>
            )}
          </button>
        </>
      }
    >
      {detailQ.isLoading ? <Loading /> : null}
      {detailQ.error ? <ErrorBox message={(detailQ.error as Error).message} /> : null}
      {detailQ.data ? (
        <div className="space-y-4">
          <JobCards detail={detail} job={job} />
          <ParametersAndMapping detail={detail} />
          {logs.length > 0 ? (
            <div className="nerd-card">
              <div className="text-xs text-zinc-500 mb-2">Live log tail (SSE)</div>
              <pre className="nerd-code" style={{ maxHeight: "30vh" }}>
                {logs.map((l) => `[${l.type ?? "log"}] ${l.message}`).join("\n")}
              </pre>
            </div>
          ) : null}
          <details>
            <summary className="text-xs text-zinc-500 cursor-pointer">raw JSON</summary>
            <JsonView data={detail} maxHeight="40vh" />
          </details>
        </div>
      ) : null}
    </Drawer>
  );
}

function JobCards({
  detail,
  job,
}: {
  detail: Record<string, unknown>;
  job: Job;
}) {
  const status = String(detail.status ?? job.status);
  const start = String(detail.startTime ?? "");
  const end = String(detail.endTime ?? "");
  const duration = (detail.durationSeconds as number | undefined) ?? job.durationSeconds;
  const created = String(detail.createdTime ?? job.createdTime ?? "");
  const tokenDesc =
    (detail.tokenDescription as string | undefined) ??
    (detail.token as { description?: string } | undefined)?.description ??
    "";
  const url = (detail.url as string | undefined) ?? "";
  // Empty string, not null: KV drops a falsy value, so a configData-only job
  // renders no "Config ID" row at all instead of a literal "null".
  const config = (detail.config as string | undefined) ?? job.config ?? "";
  const branchId = (detail.branchId as number | undefined) ?? null;
  const params = (detail.params as Record<string, unknown> | undefined) ?? {};
  const backendSize = String(
    (params?.backend as Record<string, unknown> | undefined)?.context ??
      params?.size ??
      "—",
  );

  const statusBadge = STATUS_COLORS[status] ?? "nerd-pill";

  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
      <Card icon={<FileCode className="w-3.5 h-3.5" />} label="Status">
        <span className={statusBadge}>{status}</span>
        {url ? (
          <a
            href={url}
            target="_blank"
            rel="noreferrer"
            className="block mt-2 text-xs text-accent hover:underline"
          >
            open in Keboola UI →
          </a>
        ) : null}
      </Card>
      <Card icon={<Timer className="w-3.5 h-3.5" />} label="Duration">
        <div className="text-2xl font-bold text-keboola">
          {duration != null ? formatDuration(duration) : "-"}
        </div>
      </Card>
      <Card icon={<Clock className="w-3.5 h-3.5" />} label="Times">
        <KV k="Created" v={created} />
        <KV k="Started" v={start} />
        <KV k="Ended" v={end} />
      </Card>
      <Card icon={<User className="w-3.5 h-3.5" />} label="Created by">
        <div className="text-xs text-zinc-700 dark:text-zinc-300 break-words">{tokenDesc || "—"}</div>
      </Card>
      <Card icon={<Activity className="w-3.5 h-3.5" />} label="Configuration">
        <KV k="Component" v={String(detail.component ?? job.component)} />
        <KV k="Config ID" v={String(config)} />
        {branchId ? <KV k="Branch ID" v={String(branchId)} /> : null}
      </Card>
      <Card icon={<Server className="w-3.5 h-3.5" />} label="Backend">
        <div className="text-sm text-zinc-700 dark:text-zinc-300">{backendSize}</div>
      </Card>
      <Card icon={<Cpu className="w-3.5 h-3.5" />} label="Run IDs">
        <KV k="Run ID" v={String(detail.runId ?? "")} />
        <KV k="Job ID" v={String(detail.id ?? job.id)} />
      </Card>
      <Card icon={<Activity className="w-3.5 h-3.5" />} label="Project">
        <KV k="Alias" v={job.project_alias} />
      </Card>
    </div>
  );
}

function Card({
  icon,
  label,
  children,
}: {
  icon?: React.ReactNode;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="nerd-card">
      <div className="text-[10px] uppercase tracking-wider text-zinc-500 flex items-center gap-1 mb-2">
        {icon}
        {label}
      </div>
      {children}
    </div>
  );
}

function KV({ k, v }: { k: string; v: string }) {
  if (!v) return null;
  return (
    <div className="text-xs">
      <span className="text-zinc-500">{k}:</span>{" "}
      <span className="font-mono text-zinc-700 dark:text-zinc-300">{v}</span>
    </div>
  );
}

function ParametersAndMapping({ detail }: { detail: Record<string, unknown> }) {
  const params = (detail.params as Record<string, unknown> | undefined) ?? {};
  const result = (detail.result as Record<string, unknown> | undefined) ?? {};
  const config = (detail.configData as Record<string, unknown> | undefined) ?? {};
  const storage = (config.storage as Record<string, unknown> | undefined) ?? {};
  const inputTables = (
    (storage.input as { tables?: Array<Record<string, unknown>> } | undefined)?.tables ?? []
  ) as Array<Record<string, unknown>>;
  const outputTables = (
    (storage.output as { tables?: Array<Record<string, unknown>> } | undefined)?.tables ?? []
  ) as Array<Record<string, unknown>>;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
      <div className="nerd-card">
        <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-2">
          Parameters
        </div>
        {Object.keys(params).length === 0 ? (
          <div className="text-xs text-zinc-500 dark:text-zinc-600">No parameters.</div>
        ) : (
          <pre className="nerd-code text-[10px]" style={{ maxHeight: "200px" }}>
            {JSON.stringify(params, null, 2)}
          </pre>
        )}
        {Object.keys(result).length > 0 ? (
          <details className="mt-2">
            <summary className="text-xs text-zinc-500 cursor-pointer">result</summary>
            <pre className="nerd-code text-[10px]" style={{ maxHeight: "200px" }}>
              {JSON.stringify(result, null, 2)}
            </pre>
          </details>
        ) : null}
      </div>
      <div className="nerd-card">
        <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-2">
          Mapping
        </div>
        <div className="text-xs text-zinc-700 dark:text-zinc-300 mb-1">
          Input ({inputTables.length})
        </div>
        {inputTables.length === 0 ? (
          <div className="text-xs text-zinc-500 dark:text-zinc-600">No tables.</div>
        ) : (
          <ul className="text-xs space-y-1">
            {inputTables.map((t, i) => (
              <li key={i} className="font-mono text-accent">
                {String(t.source ?? "")} → {String(t.destination ?? "")}
              </li>
            ))}
          </ul>
        )}
        <div className="text-xs text-zinc-700 dark:text-zinc-300 mt-3 mb-1">
          Output ({outputTables.length})
        </div>
        {outputTables.length === 0 ? (
          <div className="text-xs text-zinc-500 dark:text-zinc-600">No tables.</div>
        ) : (
          <ul className="text-xs space-y-1">
            {outputTables.map((t, i) => (
              <li key={i} className="font-mono text-accent">
                {String(t.source ?? "")} → {String(t.destination ?? "")}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

/**
 * One-line amber strip listing the projects whose fan-out leg failed.
 *
 * Both job lists render this: the merged envelope always carries `errors`, and
 * silently dropping it means a project that is down looks identical to a
 * project with no jobs.
 */
export function ProjectErrorsBanner({ errors }: { errors: ProjectError[] }) {
  if (errors.length === 0) return null;
  return (
    <div className="nerd-card border-neon-amber/40 bg-neon-amber/10 py-2">
      <div className="text-xs font-bold text-amber-700 dark:text-neon-amber mb-1">
        {errors.length} project(s) failed
      </div>
      <ul className="text-[11px] text-amber-700 dark:text-neon-amber space-y-0.5">
        {errors.map((e) => (
          <li key={e.project_alias} className="truncate" title={`${e.error_code}: ${e.message}`}>
            <span className="font-mono">{e.project_alias}</span> — {e.message}
          </li>
        ))}
      </ul>
    </div>
  );
}
