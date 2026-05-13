import { useQuery } from "@tanstack/react-query";
import { Activity, Clock, Cpu, FileCode, Play, Server, Square, Timer, User } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api, sseSubscribe } from "../api/client";
import { Drawer } from "../components/Drawer";
import { Empty, ErrorBox, Loading, PageTitle } from "../components/Empty";
import { JsonView } from "../components/JsonView";
import { DataTable } from "../components/Table";
import { useUIState } from "../state";
import type { Job, ProjectError } from "../types";

interface JobsResp {
  jobs: Job[];
  errors: ProjectError[];
}

const STATUS_COLORS: Record<string, string> = {
  success: "nerd-pill-green",
  error: "nerd-pill-red",
  warning: "nerd-pill-amber",
  processing: "nerd-pill-amber",
  cancelled: "nerd-pill",
  terminated: "nerd-pill",
};

export function JobsPage() {
  const { project } = useUIState();
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

  return (
    <div className="space-y-4">
      <PageTitle
        title="Jobs"
        description={`Recent Queue API jobs in ${project ?? "(no project)"} -- auto refreshing 8s`}
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
          onRowClick={(j) => setSelected(j)}
          columns={[
            { header: "Job ID", cell: (j) => <span className="text-zinc-400">{j.id}</span> },
            {
              header: "Status",
              cell: (j) => (
                <span className={STATUS_COLORS[j.status] ?? "nerd-pill"}>{j.status}</span>
              ),
            },
            { header: "Component", cell: (j) => <span className="text-accent">{j.component}</span> },
            { header: "Config", cell: (j) => <span className="text-zinc-500">{j.configId}</span> },
            {
              header: "Created",
              cell: (j) => <span className="text-zinc-500 text-xs">{j.createdTime}</span>,
            },
            {
              header: "Duration",
              align: "right",
              cell: (j) => (
                <span className="text-xs text-zinc-400">
                  {j.durationSeconds != null ? formatDuration(j.durationSeconds) : "-"}
                </span>
              ),
            },
          ]}
        />
      )}

      {selected ? <JobDetailDrawer job={selected} onClose={() => setSelected(null)} /> : null}
    </div>
  );
}

function formatDuration(sec: number): string {
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  if (m < 60) return `${m}m ${s}s`;
  const h = Math.floor(m / 60);
  const mr = m % 60;
  return `${h}h ${mr}m`;
}

function JobDetailDrawer({ job, onClose }: { job: Job; onClose: () => void }) {
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
      subtitle={`${job.component} ・ config ${job.configId}`}
      width="max-w-5xl"
      actions={
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
  const config = (detail.config as string | undefined) ?? job.configId;
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
        <div className="text-xs text-zinc-300 break-words">{tokenDesc || "—"}</div>
      </Card>
      <Card icon={<Activity className="w-3.5 h-3.5" />} label="Configuration">
        <KV k="Component" v={String(detail.component ?? job.component)} />
        <KV k="Config ID" v={String(config)} />
        {branchId ? <KV k="Branch ID" v={String(branchId)} /> : null}
      </Card>
      <Card icon={<Server className="w-3.5 h-3.5" />} label="Backend">
        <div className="text-sm text-zinc-300">{backendSize}</div>
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
      <span className="font-mono text-zinc-300">{v}</span>
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
          <div className="text-xs text-zinc-600">No parameters.</div>
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
        <div className="text-xs text-zinc-300 mb-1">
          Input ({inputTables.length})
        </div>
        {inputTables.length === 0 ? (
          <div className="text-xs text-zinc-600">No tables.</div>
        ) : (
          <ul className="text-xs space-y-1">
            {inputTables.map((t, i) => (
              <li key={i} className="font-mono text-accent">
                {String(t.source ?? "")} → {String(t.destination ?? "")}
              </li>
            ))}
          </ul>
        )}
        <div className="text-xs text-zinc-300 mt-3 mb-1">
          Output ({outputTables.length})
        </div>
        {outputTables.length === 0 ? (
          <div className="text-xs text-zinc-600">No tables.</div>
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
