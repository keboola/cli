import { useQuery } from "@tanstack/react-query";
import mermaid from "mermaid";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { Drawer } from "../components/Drawer";
import { Empty, ErrorBox, Loading, PageTitle } from "../components/Empty";
import { JsonView } from "../components/JsonView";
import { DataTable } from "../components/Table";
import { useUIState } from "../state";
import type { Flow, ProjectError } from "../types";

interface Phase {
  id: string | number;
  name?: string;
  dependsOn?: Array<string | number>;
}
interface Task {
  id: string | number;
  phase: string | number;
  name?: string;
  task?: { componentId?: string; configId?: string };
}
interface FlowDetail {
  name: string;
  phases: Phase[];
  tasks: Task[];
  description?: string;
}

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
                  <span className="text-xs text-zinc-500 dark:text-zinc-600">-</span>
                ),
            },
          ]}
        />
      )}
      {selected ? (
        <FlowDrawer flow={selected} onClose={() => setSelected(null)} />
      ) : null}
    </div>
  );
}

function FlowDrawer({ flow, onClose }: { flow: Flow; onClose: () => void }) {
  const { branchId } = useUIState();
  const [tab, setTab] = useState<"builder" | "raw">("builder");
  const q = useQuery<FlowDetail>({
    queryKey: ["flow-detail", flow.project_alias, flow.component_id, flow.config_id, branchId],
    queryFn: () =>
      api.get(
        `/flows/${encodeURIComponent(flow.project_alias)}/${encodeURIComponent(flow.config_id)}`,
        { query: { component_id: flow.component_id, branch_id: branchId ?? undefined } },
      ),
  });
  return (
    <Drawer
      open={true}
      onClose={onClose}
      title={flow.name}
      subtitle={`${flow.component_id} ・ ${flow.config_id} ・ ${flow.is_disabled ? "disabled" : "enabled"}`}
      width="max-w-5xl"
    >
      <div className="flex gap-2 mb-4">
        <button
          type="button"
          className={`nerd-btn text-xs ${tab === "builder" ? "border-keboola text-keboola" : ""}`}
          onClick={() => setTab("builder")}
        >
          Builder
        </button>
        <button
          type="button"
          className={`nerd-btn text-xs ${tab === "raw" ? "border-keboola text-keboola" : ""}`}
          onClick={() => setTab("raw")}
        >
          Raw JSON
        </button>
      </div>
      {q.isLoading ? <Loading /> : null}
      {q.error ? <ErrorBox message={(q.error as Error).message} /> : null}
      {q.data && tab === "builder" ? <FlowBuilder detail={q.data} /> : null}
      {q.data && tab === "raw" ? <JsonView data={q.data} /> : null}
    </Drawer>
  );
}

/**
 * Read-only visual flow representation, mimicking Keboola UI's drag-and-drop
 * canvas. Renders phases as Mermaid boxes connected via dependsOn edges,
 * with task list per phase shown below the diagram.
 */
function FlowBuilder({ detail }: { detail: FlowDetail }) {
  const phases = detail.phases ?? [];
  const tasks = detail.tasks ?? [];
  const tasksByPhase = new Map<string, Task[]>();
  for (const t of tasks) {
    const k = String(t.phase);
    const arr = tasksByPhase.get(k) ?? [];
    arr.push(t);
    tasksByPhase.set(k, arr);
  }

  if (phases.length === 0) {
    return <Empty title="This flow has no phases yet." />;
  }

  return (
    <div className="space-y-4">
      <FlowMermaid phases={phases} tasksByPhase={tasksByPhase} />
      <div className="space-y-3">
        {phases.map((p, i) => {
          const pTasks = tasksByPhase.get(String(p.id)) ?? [];
          return (
            <div key={String(p.id)} className="nerd-card">
              <div className="flex items-center justify-between mb-2">
                <h4 className="text-sm font-bold text-keboola">
                  Step {i + 1} ・ {p.name ?? p.id}
                </h4>
                <span className="text-xs text-zinc-500">
                  {pTasks.length} task(s)
                  {p.dependsOn && p.dependsOn.length > 0 ? (
                    <span className="ml-2 text-zinc-500 dark:text-zinc-600">
                      depends on:{" "}
                      {p.dependsOn
                        .map((d) => phases.find((x) => x.id === d)?.name ?? d)
                        .join(", ")}
                    </span>
                  ) : null}
                </span>
              </div>
              {pTasks.length === 0 ? (
                <div className="text-xs text-zinc-500 dark:text-zinc-600">No tasks in this phase.</div>
              ) : (
                <ul className="text-xs space-y-1">
                  {pTasks.map((t) => (
                    <li
                      key={String(t.id)}
                      className="flex items-center gap-2 py-1 border-b border-zinc-200 dark:border-zinc-900/40"
                    >
                      <span className="font-mono text-accent">
                        {t.task?.componentId ?? "?"}
                      </span>
                      <span className="text-zinc-500">/</span>
                      <span className="font-mono text-zinc-600 dark:text-zinc-400">
                        {t.task?.configId ?? "?"}
                      </span>
                      <span className="ml-auto text-zinc-700 dark:text-zinc-300">{t.name ?? ""}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function FlowMermaid({
  phases,
  tasksByPhase,
}: {
  phases: Phase[];
  tasksByPhase: Map<string, Task[]>;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const seq = useRef(0);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    let cancelled = false;
    seq.current += 1;
    const runId = `flow_${Date.now()}_${seq.current}`;
    const slug = (s: string) => s.replace(/[^a-zA-Z0-9]/g, "_");
    const esc = (s: string) =>
      s
        .replace(/&/g, "&amp;")
        .replace(/"/g, "&quot;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");

    const lines = ["graph TD"];
    for (const p of phases) {
      const id = `p_${slug(String(p.id))}`;
      const tcount = tasksByPhase.get(String(p.id))?.length ?? 0;
      const label = `${esc(p.name ?? String(p.id))}<br/><small>${tcount} task${tcount === 1 ? "" : "s"}</small>`;
      lines.push(`  ${id}["${label}"]`);
    }
    // Phases without dependsOn flow in declaration order (implicit chain), so
    // we draw both explicit edges AND a fallback chain to match Keboola UI.
    for (let i = 0; i < phases.length; i++) {
      const p = phases[i];
      const id = `p_${slug(String(p.id))}`;
      if (p.dependsOn && p.dependsOn.length > 0) {
        for (const dep of p.dependsOn) {
          lines.push(`  p_${slug(String(dep))} --> ${id}`);
        }
      } else if (i > 0) {
        const prev = phases[i - 1];
        lines.push(`  p_${slug(String(prev.id))} --> ${id}`);
      }
    }

    mermaid
      .render(runId, lines.join("\n"))
      .then(({ svg }) => {
        if (cancelled || !ref.current) return;
        ref.current.innerHTML = svg;
        setError(null);
      })
      .catch((err) => {
        if (!cancelled) setError(String(err));
      });

    return () => {
      cancelled = true;
      document.getElementById(runId)?.remove();
    };
  }, [phases, tasksByPhase]);

  return (
    <div className="nerd-card">
      <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-2">
        Flow diagram
      </div>
      {error ? <div className="text-xs text-red-600 dark:text-red-400">{error}</div> : null}
      <div ref={ref} className="overflow-auto" style={{ minHeight: 200 }} />
    </div>
  );
}
