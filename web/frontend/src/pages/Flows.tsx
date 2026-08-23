import { useQuery } from "@tanstack/react-query";
import mermaid from "mermaid";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { Drawer } from "../components/Drawer";
import { Empty, ErrorBox, Loading, PageTitle } from "../components/Empty";
import { JsonView } from "../components/JsonView";
import { DataTable } from "../components/Table";
import { useUIState } from "../state";
import { useHashSelection } from "../useHashSelection";
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

interface NotificationSubscription {
  project_alias: string;
  subscription_id: string;
  /** kebab-case event name, e.g. "job-failed". */
  event: string;
  /** "" when the subscription carries no component filter. */
  component_id: string;
  /** "" when the subscription is project-wide (no config filter). */
  config_id: string;
  branch_id: string;
  phase_id: string;
  /** "email" | "webhook" | ... */
  channel: string;
  /** Email address OR webhook URL, depending on `channel`. */
  address: string;
  expires_at: string;
  config_name: string;
  filters: Array<Record<string, unknown>>;
  /** "config" | "project-wide" */
  scope: string;
}

interface NotificationsResp {
  subscriptions: NotificationSubscription[];
  errors: ProjectError[];
  project_wide_excluded: number;
}

export function FlowsPage() {
  const { project, branchId } = useUIState();
  // Deep link: `?sel=<flowConfigId>` opens that flow's drawer.
  const [sel, setSel] = useHashSelection();
  const [selected, setSelected] = useState<Flow | null>(null);
  const q = useQuery<FlowsResp>({
    queryKey: ["flows", project, branchId],
    queryFn: () =>
      api.get("/flows", {
        query: { project: project ?? undefined, branch_id: branchId ?? undefined, with_schedules: true },
      }),
    enabled: !!project,
  });

  // Restore a deep-linked flow ONCE, after the first list load. A link to a
  // flow that no longer exists (or lives on another branch) leaves the list
  // open rather than erroring.
  const restoredRef = useRef(false);
  useEffect(() => {
    if (restoredRef.current) return;
    if (!sel) {
      restoredRef.current = true;
      return;
    }
    if (!q.data) return;
    restoredRef.current = true;
    const hit = q.data.flows.find((f) => f.config_id === sel);
    if (hit) setSelected(hit);
  }, [sel, q.data]);

  const openFlow = (f: Flow) => {
    setSelected(f);
    setSel(f.config_id);
  };
  const closeFlow = () => {
    setSelected(null);
    setSel(null);
  };

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
          onRowClick={openFlow}
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
        <FlowDrawer flow={selected} onClose={closeFlow} />
      ) : null}
    </div>
  );
}

function FlowDrawer({ flow, onClose }: { flow: Flow; onClose: () => void }) {
  const { branchId } = useUIState();
  const [tab, setTab] = useState<"builder" | "raw" | "notifications">("builder");
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
        <button
          type="button"
          className={`nerd-btn text-xs ${tab === "notifications" ? "border-keboola text-keboola" : ""}`}
          onClick={() => setTab("notifications")}
        >
          Notifications
        </button>
      </div>
      {/* The Notifications tab owns its own query (a different platform
          service), so it must not be gated on the flow-detail request. */}
      {tab === "notifications" ? (
        <FlowNotifications flow={flow} />
      ) : (
        <>
          {q.isLoading ? <Loading /> : null}
          {q.error ? <ErrorBox message={(q.error as Error).message} /> : null}
          {q.data && tab === "builder" ? <FlowBuilder detail={q.data} /> : null}
          {q.data && tab === "raw" ? <JsonView data={q.data} /> : null}
        </>
      )}
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

/** Pill styling per event name. Exact matches win before the substring rules
 *  so "job-succeeded-with-warning" reads amber, not green. */
function eventPillClass(event: string): string {
  if (event === "job-failed") return "nerd-pill-red";
  if (event === "job-succeeded") return "nerd-pill-green";
  if (event.includes("warning") || event.includes("long")) return "nerd-pill-amber";
  return "nerd-pill";
}

const BRANCH_NOTE =
  "Branch: the UI always writes a branch.id filter, and for production that value is the " +
  "DEFAULT branch's numeric id — so a value here does NOT by itself mean the subscription is " +
  "dev-branch-only. Compare it against the project's branch list.";

function NotificationTable({ rows }: { rows: NotificationSubscription[] }) {
  return (
    <DataTable
      rows={rows}
      rowKey={(s) => s.subscription_id}
      columns={[
        {
          header: "Event",
          cell: (s) => <span className={eventPillClass(s.event)}>{s.event}</span>,
        },
        { header: "Channel", cell: (s) => <span className="text-xs">{s.channel || "—"}</span> },
        {
          header: "Address",
          cell: (s) => (
            <span className="font-mono text-xs text-accent break-all">{s.address || "—"}</span>
          ),
        },
        {
          header: "Branch",
          cell: (s) => (
            <span className="font-mono text-xs text-zinc-600 dark:text-zinc-400">
              {s.branch_id || "—"}
            </span>
          ),
        },
      ]}
    />
  );
}

/**
 * Read-only "Notifications" tab — who actually gets paged about this flow.
 *
 * These recipients are the ones behind the Flow Builder's Notifications tab
 * (bell icon). They live in a SEPARATE platform service
 * (notification.{stack}) and are NOT part of the flow's configuration JSON,
 * which is why `flow detail` — and therefore the Builder and Raw JSON tabs —
 * never showed them. (The in-flow `type: "notification"` TASK is a different
 * mechanism and stays visible in the Builder.)
 *
 * We deliberately fetch the project's subscriptions UNFILTERED (only
 * `project`) and split them client-side. Passing `config_id` to the API drops
 * the filter-less, project-wide catch-alls SERVER-SIDE — and those fire for
 * every job in the project, this flow included — so a filtered fetch would
 * silently under-report the recipient list.
 */
function FlowNotifications({ flow }: { flow: Flow }) {
  // Keyed by project only: the response is the project's full, unfiltered
  // subscription list, so every flow in the project shares one cache entry.
  const q = useQuery<NotificationsResp>({
    queryKey: ["flow-notifications", flow.project_alias],
    queryFn: () => api.get("/notifications", { query: { project: [flow.project_alias] } }),
  });

  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox message={(q.error as Error).message} />;

  const subs = q.data?.subscriptions ?? [];
  const errors = q.data?.errors ?? [];
  // Mutually exclusive by construction: a subscription either carries a
  // config filter (scoped) or carries none at all (project-wide catch-all).
  const forThisFlow = subs.filter((s) => s.config_id && s.config_id === flow.config_id);
  const projectWide = subs.filter((s) => !s.config_id);

  return (
    <div className="space-y-6">
      {errors.map((e) => (
        <ErrorBox
          key={`${e.project_alias}/${e.error_code}`}
          message={`${e.project_alias}: ${e.error_code} — ${e.message}`}
        />
      ))}

      <section className="space-y-2">
        <div className="flex items-center gap-2">
          <h4 className="text-sm font-bold text-keboola">This flow</h4>
          <span className="ml-auto text-xs text-zinc-500">
            {forThisFlow.length} recipient(s)
          </span>
        </div>
        {forThisFlow.length === 0 ? (
          <Empty
            title="No notification recipients for this flow."
            hint="Nothing is subscribed to this flow's own jobs. Check the project-wide group below — those fire for it too."
          />
        ) : (
          <NotificationTable rows={forThisFlow} />
        )}
      </section>

      <section className="space-y-2">
        <div className="flex items-center gap-2">
          <h4 className="text-sm font-bold text-zinc-700 dark:text-zinc-300">Project-wide</h4>
          <span className="nerd-pill-amber">project-wide</span>
          <span className="ml-auto text-xs text-zinc-500">{projectWide.length} recipient(s)</span>
        </div>
        <p className="text-xs text-amber-700 dark:text-amber-400">
          No config filter — these fire for every job in the project, this flow included.
        </p>
        {projectWide.length === 0 ? (
          <Empty title="No project-wide notification recipients." />
        ) : (
          <NotificationTable rows={projectWide} />
        )}
      </section>

      <p className="text-[11px] text-zinc-500 dark:text-zinc-600">{BRANCH_NOTE}</p>
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
