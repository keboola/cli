import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Activity,
  Bot,
  Heart,
  Network,
  Play,
  PlayCircle,
  Send,
  Sparkles,
  Terminal,
} from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import { Empty, Loading, PageTitle } from "../components/Empty";
import { useUIState } from "../state";
import type { PageId } from "../state";
import type { Job, Project, ProjectStatus } from "../types";

interface AgentTask {
  id: string;
  name: string;
  cron: string;
  enabled: boolean;
  last_run_at: string | null;
  next_run_at: string | null;
  action: { type: string; params: Record<string, unknown> };
}

interface DoctorResp {
  checks: Array<{ name: string; status: string; message: string }>;
  summary: { total: number; passed: number; failed: number; warnings?: number };
}

function greeting(): string {
  const h = new Date().getHours();
  if (h < 12) return "Good Morning";
  if (h < 18) return "Good Afternoon";
  return "Good Evening";
}

export function DashboardPage() {
  const { project, setPage, setPendingLocalAiMessage } = useUIState();
  const [aiInput, setAiInput] = useState("");

  const projectsQ = useQuery<{ projects: Project[] }>({
    queryKey: ["projects"],
    queryFn: () => api.get("/projects"),
  });
  const statusQ = useQuery<{ status: ProjectStatus[] }>({
    queryKey: ["projects-status"],
    queryFn: () => api.get("/projects/status"),
  });
  const agentsQ = useQuery<{ tasks: AgentTask[] }>({
    queryKey: ["agents"],
    queryFn: () => api.get("/agents"),
  });
  const doctorQ = useQuery<DoctorResp>({
    queryKey: ["doctor"],
    queryFn: () => api.get("/doctor"),
  });
  const jobsQ = useQuery<{ jobs: Job[] }>({
    queryKey: ["dashboard-jobs", project],
    queryFn: () =>
      api.get("/jobs", { query: { project: project ?? undefined, limit: 5 } }),
    enabled: !!project,
  });

  /**
   * Hand the typed message off to the Local AI page (#300). Dashboard
   * hero stays minimal — full chat plumbing lives on /localai. User
   * types → presses Send → navigates with the message pre-loaded; the
   * Local AI page auto-fires the request on mount.
   */
  const sendToLocalAi = () => {
    const msg = aiInput.trim();
    if (!msg) return;
    setPendingLocalAiMessage(msg);
    setAiInput("");
    setPage("localai");
  };

  const projects = projectsQ.data?.projects ?? [];
  const tasks = agentsQ.data?.tasks ?? [];
  const enabledTasks = tasks.filter((t) => t.enabled);
  const onlineProjects =
    statusQ.data?.status?.filter((s) => s.status === "ok").length ?? 0;
  const doctorWarnings = doctorQ.data?.summary?.warnings ?? 0;
  const doctorFailed = doctorQ.data?.summary?.failed ?? 0;

  const nextAgent = enabledTasks
    .filter((t) => t.next_run_at)
    .sort((a, b) =>
      (a.next_run_at ?? "") > (b.next_run_at ?? "") ? 1 : -1,
    )[0];

  return (
    <div className="space-y-6">
      <PageTitle
        title={greeting()}
        description={
          project
            ? `Working in project ${project}. Ask the local AI anything below, or jump to a tile.`
            : "Pick a project in the top bar to scope per-project tiles, or ask cross-project below."
        }
      />

      {/* Hero "ask the local AI" prompt. Hands off to the Local AI page
          via UIState.pendingLocalAiMessage; full chat plumbing lives
          there, this stays a tiny launchpad. */}
      <form
        className="nerd-card border-keboola/30 bg-white dark:bg-zinc-900/40"
        onSubmit={(e) => {
          e.preventDefault();
          sendToLocalAi();
        }}
      >
        <div className="flex items-center gap-3">
          <Sparkles className="w-5 h-5 text-keboola flex-shrink-0" />
          <input
            className="flex-1 bg-transparent border-0 focus:outline-none text-sm placeholder-zinc-500 dark:placeholder-zinc-600"
            placeholder={
              project
                ? `Ask the local AI about ${project}…`
                : "Ask the local AI anything across your Keboola projects…"
            }
            value={aiInput}
            onChange={(e) => setAiInput(e.target.value)}
          />
          <button
            type="submit"
            className="nerd-btn flex items-center gap-1 hover:text-keboola"
            disabled={!aiInput.trim()}
            title="Open Local AI with this question (uses claude / codex / gemini on your machine)"
          >
            <Send className="w-3 h-3" /> Ask
          </button>
        </div>
      </form>

      {/* Stats row */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatTile
          label="Projects connected"
          value={`${onlineProjects} / ${projects.length}`}
          icon={<Network className="w-4 h-4" />}
          onClick={() => setPage("projects")}
        />
        <StatTile
          label="Agent tasks"
          value={`${enabledTasks.length} active`}
          subtle={`of ${tasks.length} total`}
          icon={<Bot className="w-4 h-4" />}
          onClick={() => setPage("agents")}
        />
        <StatTile
          label="Doctor"
          value={doctorFailed === 0 && doctorWarnings === 0 ? "all checks pass" : `${doctorFailed + doctorWarnings} issues`}
          tone={doctorFailed > 0 ? "red" : doctorWarnings > 0 ? "amber" : "green"}
          icon={<Heart className="w-4 h-4" />}
          onClick={() => setPage("doctor")}
        />
        <StatTile
          label="Recent jobs"
          value={`${jobsQ.data?.jobs?.length ?? 0} loaded`}
          subtle={project ?? "(no project)"}
          icon={<PlayCircle className="w-4 h-4" />}
          onClick={() => setPage("jobs")}
        />
      </div>

      {/* Two-column: agent activity + suggested actions */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <section className="nerd-card">
          <header className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-bold text-keboola flex items-center gap-2">
              <Activity className="w-4 h-4" /> Scheduled agents
            </h3>
            <button
              type="button"
              className="text-xs text-zinc-500 hover:text-keboola"
              onClick={() => setPage("agents")}
            >
              manage →
            </button>
          </header>
          {agentsQ.isLoading ? (
            <Loading />
          ) : tasks.length === 0 ? (
            <Empty
              title="No scheduled agents yet"
              hint="Hand off recurring chores to claude / codex / kbagent CLI."
            />
          ) : (
            <div className="space-y-2">
              {nextAgent ? (
                <div className="border border-keboola/40 bg-keboola/5 rounded p-2 text-xs">
                  <div className="text-[10px] uppercase tracking-wider text-keboola mb-1">
                    Next firing
                  </div>
                  <div className="font-bold text-zinc-900 dark:text-zinc-100">{nextAgent.name}</div>
                  <div className="text-zinc-500">
                    {nextAgent.next_run_at
                      ? new Date(nextAgent.next_run_at).toLocaleString()
                      : "(unscheduled)"}
                  </div>
                </div>
              ) : null}
              <ul className="space-y-1">
                {tasks.slice(0, 5).map((t) => (
                  <ScheduledAgentRow key={t.id} task={t} />
                ))}
              </ul>
            </div>
          )}
        </section>

        <section className="nerd-card">
          <header className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-bold text-keboola">Suggested next steps</h3>
          </header>
          <div className="space-y-2">
            {projects.length === 0 ? (
              <SuggestedAction
                icon={<Network className="w-3.5 h-3.5 text-keboola" />}
                text="Add your first Keboola project"
                target="projects"
                setPage={setPage}
              />
            ) : null}
            {tasks.length === 0 ? (
              <SuggestedAction
                icon={<Bot className="w-3.5 h-3.5 text-neon-pink" />}
                text="Schedule an AI agent — e.g. triage overnight error jobs"
                target="agents"
                setPage={setPage}
              />
            ) : null}
            {doctorWarnings > 0 || doctorFailed > 0 ? (
              <SuggestedAction
                icon={<Heart className="w-3.5 h-3.5 text-amber-700 dark:text-neon-amber" />}
                text={`Doctor reports ${doctorFailed} fail + ${doctorWarnings} warn -- triage`}
                target="doctor"
                setPage={setPage}
              />
            ) : null}
            <SuggestedAction
              icon={<Terminal className="w-3.5 h-3.5 text-accent" />}
              text="Open a SQL workspace and explore Storage interactively"
              target="workspaces"
              setPage={setPage}
            />
            <SuggestedAction
              icon={<Network className="w-3.5 h-3.5 text-keboola" />}
              text="Build deep column-level lineage across registered projects"
              target="lineage"
              setPage={setPage}
            />
          </div>
        </section>
      </div>

      {/* Recent jobs */}
      {project ? (
        <section className="nerd-card">
          <header className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-bold text-keboola flex items-center gap-2">
              <PlayCircle className="w-4 h-4" /> Recent jobs in {project}
            </h3>
            <button
              type="button"
              className="text-xs text-zinc-500 hover:text-keboola"
              onClick={() => setPage("jobs")}
            >
              all jobs →
            </button>
          </header>
          {jobsQ.isLoading ? (
            <Loading />
          ) : (jobsQ.data?.jobs.length ?? 0) === 0 ? (
            <div className="text-xs text-zinc-500">No recent jobs.</div>
          ) : (
            <ul className="text-xs space-y-1">
              {(jobsQ.data?.jobs ?? []).slice(0, 6).map((j) => (
                <li
                  key={String(j.id)}
                  className="flex items-center gap-3 py-1 border-b border-zinc-200 dark:border-zinc-900/40"
                >
                  <span className="font-mono text-zinc-500">{j.id}</span>
                  <span className="nerd-pill">{j.status}</span>
                  <span className="text-accent">{j.component}</span>
                  <span className="text-zinc-500 ml-auto">{j.createdTime}</span>
                </li>
              ))}
            </ul>
          )}
        </section>
      ) : null}
    </div>
  );
}

function StatTile({
  label,
  value,
  subtle,
  icon,
  tone = "default",
  onClick,
}: {
  label: string;
  value: string;
  subtle?: string;
  icon?: React.ReactNode;
  tone?: "default" | "red" | "amber" | "green";
  onClick?: () => void;
}) {
  const toneClass =
    tone === "red"
      ? "border-red-300 dark:border-red-700/40 text-red-600 dark:text-red-400"
      : tone === "amber"
        ? "border-neon-amber/40 text-amber-700 dark:text-neon-amber"
        : tone === "green"
          ? "border-keboola/40 text-keboola"
          : "border-zinc-200 dark:border-zinc-800";
  return (
    <button
      type="button"
      onClick={onClick}
      className={`nerd-card text-left hover:border-keboola/40 transition-colors ${toneClass}`}
    >
      <div className="flex items-center justify-between mb-1 text-[10px] uppercase tracking-wider text-zinc-500">
        <span>{label}</span>
        {icon}
      </div>
      <div className="text-2xl font-bold">{value}</div>
      {subtle ? <div className="text-[10px] text-zinc-500 mt-0.5">{subtle}</div> : null}
    </button>
  );
}

function SuggestedAction({
  icon,
  text,
  target,
  setPage,
}: {
  icon: React.ReactNode;
  text: string;
  target: PageId;
  setPage: (p: PageId) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => setPage(target)}
      className="w-full text-left p-2 rounded border border-zinc-200 dark:border-zinc-900 hover:border-keboola/40 hover:bg-zinc-100 dark:hover:bg-zinc-900/30 transition-colors flex items-start gap-2 text-xs"
    >
      <span className="mt-0.5">{icon}</span>
      <span className="text-zinc-700 dark:text-zinc-300">{text}</span>
      <span className="ml-auto text-zinc-500 dark:text-zinc-600">→</span>
    </button>
  );
}

/**
 * One row in the dashboard's Scheduled agents tile (#292). The Run button
 * fires the persisted task via POST /agents/{id}/run (blocking, same
 * machinery as the cron trigger) and invalidates the ["agents"] query on
 * completion so the row's last-run status refreshes inline. We deliberately
 * use the blocking endpoint instead of the SSE /run/stream one: the tile
 * is a glance-and-move-on surface, not a live progress viewer — users who
 * want to watch tool_use events use the full Run drawer on the Agents page.
 */
function ScheduledAgentRow({ task }: { task: AgentTask }) {
  const qc = useQueryClient();
  const runMu = useMutation({
    mutationFn: () => api.post(`/agents/${task.id}/run`, {}),
    onSettled: () => {
      // Refresh the tile (and any agent-runs lists on other pages) so
      // last_run_at and the status pill flip from stale to current.
      qc.invalidateQueries({ queryKey: ["agents"] });
      qc.invalidateQueries({ queryKey: ["agent-runs", task.id] });
    },
  });
  return (
    <li className="flex items-center gap-2 text-xs py-1 border-b border-zinc-200 dark:border-zinc-900/40">
      <span
        className={`w-2 h-2 rounded-full ${
          task.enabled ? "bg-keboola animate-pulse" : "bg-zinc-700"
        }`}
      />
      <span className="flex-1 truncate text-zinc-700 dark:text-zinc-300">{task.name}</span>
      <span className="font-mono text-[10px] text-zinc-500">{task.cron}</span>
      <button
        type="button"
        className="nerd-btn text-[10px] py-0.5 px-1.5 flex items-center gap-1 hover:text-keboola hover:border-keboola/60"
        onClick={() => runMu.mutate()}
        disabled={runMu.isPending}
        title={`Trigger '${task.name}' now (outside the cron schedule)`}
      >
        {runMu.isPending ? (
          <span className="text-keboola">running…</span>
        ) : (
          <>
            <Play className="w-3 h-3" /> run
          </>
        )}
      </button>
    </li>
  );
}
