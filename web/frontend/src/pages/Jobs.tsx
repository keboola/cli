import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { sseSubscribe, api } from "../api/client";
import { Empty, ErrorBox, Loading, PageTitle } from "../components/Empty";
import { JsonView } from "../components/JsonView";
import { DataTable } from "../components/Table";
import { useUIState } from "../state";
import type { Job, ProjectError } from "../types";
import { useEffect, useRef } from "react";

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
        query: { project: project ?? undefined, status: statusFilter ?? undefined, limit: 100 },
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
            className={`nerd-btn ${statusFilter === s ? "border-keboola text-keboola" : ""}`}
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
              cell: (j) => <span className={STATUS_COLORS[j.status] ?? "nerd-pill"}>{j.status}</span>,
            },
            { header: "Component", cell: (j) => <span className="text-accent">{j.component}</span> },
            { header: "Config", cell: (j) => <span className="text-zinc-500">{j.configId}</span> },
            { header: "Created", cell: (j) => <span className="text-zinc-500 text-xs">{j.createdTime}</span> },
          ]}
        />
      )}

      {selected ? (
        <JobDetail job={selected} onClose={() => setSelected(null)} />
      ) : null}
    </div>
  );
}

function JobDetail({ job, onClose }: { job: Job; onClose: () => void }) {
  const detailQ = useQuery({
    queryKey: ["job-detail", job.project_alias, job.id],
    queryFn: () =>
      api.get(`/jobs/${encodeURIComponent(job.project_alias)}/${encodeURIComponent(String(job.id))}`),
  });
  const [logs, setLogs] = useState<Array<{ id: number | string; message: string; type?: string }>>([]);
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

  return (
    <div className="nerd-card">
      <div className="flex items-center justify-between mb-3">
        <h3 className="font-bold text-keboola">Job {job.id}</h3>
        <div className="flex gap-2">
          <button
            type="button"
            className="nerd-btn text-xs"
            onClick={() => (streaming ? esRef.current?.close() : startStream())}
          >
            {streaming ? "stop stream" : "stream logs (SSE)"}
          </button>
          <button type="button" className="nerd-btn text-xs" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
      {detailQ.isLoading ? <Loading /> : null}
      {detailQ.data ? <JsonView data={detailQ.data} maxHeight="40vh" /> : null}
      {logs.length > 0 ? (
        <div className="mt-4">
          <div className="text-xs text-zinc-500 mb-1">Live log tail (SSE):</div>
          <pre className="nerd-code" style={{ maxHeight: "30vh" }}>
            {logs.map((l) => `[${l.type ?? "log"}] ${l.message}`).join("\n")}
          </pre>
        </div>
      ) : null}
    </div>
  );
}
