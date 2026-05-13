import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Bot,
  Brain,
  Pause,
  Pencil,
  Play,
  Plus,
  Sparkles,
  Terminal,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, ssePost, type SsePostHandle } from "../api/client";
import {
  AgentRunRaw,
  AgentRunView,
  type AgentEvent,
  type RunSummary,
} from "../components/AgentRunView";
import { Drawer } from "../components/Drawer";
import { ErrorBox, Loading, PageTitle, TwoPathEmpty } from "../components/Empty";
import { JsonView } from "../components/JsonView";
import { DataTable } from "../components/Table";
import { useUIState } from "../state";

type ActionType = "mcp_tool" | "cli_command" | "ai_agent";

/**
 * True if `err` is the AbortError thrown when an in-flight fetch is
 * cancelled via AbortController.abort(). React StrictMode in dev
 * triggers cleanup between mount and re-mount, which fires
 * controller.abort() before the user has done anything -- the SSE
 * promise then rejects with this error. It is *not* a user-facing
 * failure; the user-initiated cancel paths (cancelTest, cancelLive,
 * drawer-close cleanup) all produce the same exception, so the
 * symmetric handling is to swallow it.
 */
function isAbortError(err: unknown): boolean {
  if (err instanceof DOMException && err.name === "AbortError") return true;
  if (err instanceof Error && err.name === "AbortError") return true;
  // Some Chrome versions stringify as "signal is aborted without reason"
  // even though name is set. Belt-and-suspenders.
  return Boolean(
    err && typeof err === "object" && "message" in err &&
      String((err as { message: unknown }).message).toLowerCase().includes("abort"),
  );
}

interface AgentTask {
  id: string;
  name: string;
  description: string;
  cron: string;
  enabled: boolean;
  action: { type: ActionType; params: Record<string, unknown> };
  created_at: string;
  last_run_at: string | null;
  next_run_at: string | null;
}

interface AgentRun {
  run_id: string;
  task_id: string;
  started_at: string;
  ended_at: string | null;
  status: "running" | "ok" | "error";
  output: Record<string, unknown> | null;
  error: string | null;
  // Optional fields populated by ``server/pricing.build_run_summary`` after
  // v0.10.x for ai_agent runs. Older persisted runs carry them as null/absent.
  summary?: RunSummary | null;
  events_path?: string | null;
}

const CRON_PRESETS: Array<{ label: string; cron: string }> = [
  { label: "every minute", cron: "* * * * *" },
  { label: "every 5 minutes", cron: "*/5 * * * *" },
  { label: "every 15 minutes", cron: "*/15 * * * *" },
  { label: "every hour", cron: "0 * * * *" },
  { label: "every 6 hours", cron: "0 */6 * * *" },
  { label: "daily @ midnight UTC", cron: "0 0 * * *" },
  { label: "daily @ 06:00 UTC", cron: "0 6 * * *" },
  { label: "weekdays @ 09:00 UTC", cron: "0 9 * * 1-5" },
];

function ActionLabel({ action }: { action: AgentTask["action"] }) {
  if (action.type === "mcp_tool") {
    return (
      <span className="text-xs flex items-center gap-1">
        <Sparkles className="w-3 h-3 text-accent" />
        {String(action.params.tool ?? "")}
      </span>
    );
  }
  if (action.type === "cli_command") {
    return (
      <span className="text-xs flex items-center gap-1">
        <Terminal className="w-3 h-3 text-accent" />
        {(action.params.argv as string[] | undefined)?.join(" ") ?? ""}
      </span>
    );
  }
  // ai_agent
  return (
    <span className="text-xs flex items-center gap-1">
      <Brain className="w-3 h-3 text-neon-pink" />
      <span className="text-neon-pink">{String(action.params.cli ?? "ai")}</span>
      <span className="text-zinc-500 truncate max-w-[40ch]">
        {String(action.params.prompt ?? "")}
      </span>
    </span>
  );
}

// Helper: convert raw SSE event from /agents/.../run/stream into the
// AgentEvent shape consumed by AgentRunView. ``type`` becomes ``event``,
// ``data`` is preserved, and ``at`` is filled in by the caller (it's the
// ms-since-start that lives in the SSE consumer closure).

export function AgentsPage() {
  const qc = useQueryClient();
  const [showNew, setShowNew] = useState(false);
  const [editing, setEditing] = useState<AgentTask | null>(null);
  // ``selected`` opens the read-only detail drawer with run history; when
  // ``selectedAutoRun`` is true, the detail drawer also auto-starts a live
  // streamed run (the "Play" button shortcut from the row).
  const [selected, setSelected] = useState<AgentTask | null>(null);
  const [selectedAutoRun, setSelectedAutoRun] = useState(false);

  const q = useQuery<{ tasks: AgentTask[] }>({
    queryKey: ["agents"],
    queryFn: () => api.get("/agents"),
    refetchInterval: 10_000,
  });

  const toggleMu = useMutation({
    mutationFn: (t: AgentTask) => api.patch(`/agents/${t.id}`, { enabled: !t.enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["agents"] }),
  });
  const deleteMu = useMutation({
    mutationFn: (id: string) => api.delete(`/agents/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["agents"] }),
  });

  return (
    <div className="space-y-4">
      <PageTitle
        title="Agent Tasks"
        description="Cron-scheduled tasks running inside kbagent serve. Three flavours: MCP tool calls, raw kbagent CLI, or full AI agents (claude / codex / gemini) with prompts."
        actions={
          <button
            type="button"
            className="nerd-btn flex items-center gap-1 hover:text-keboola"
            onClick={() => setShowNew(true)}
          >
            <Plus className="w-3 h-3" /> New task
          </button>
        }
      />
      {q.isLoading ? <Loading /> : null}
      {q.error ? <ErrorBox message={(q.error as Error).message} /> : null}
      {q.data?.tasks.length === 0 ? (
        <TwoPathEmpty
          headline="Schedule your first agent"
          subline="Two paths -- pick the one that fits your task."
          paths={[
            {
              title: "Schedule a kbagent CLI command",
              description:
                "For deterministic background jobs: nightly sync pulls, periodic doctor checks, polling for failed jobs. Output captured to history.",
              icon: <Terminal className="w-8 h-8 text-keboola" />,
              action: (
                <button
                  type="button"
                  className="nerd-btn hover:text-keboola"
                  onClick={() => setShowNew(true)}
                >
                  + New CLI task
                </button>
              ),
            },
            {
              title: "Schedule an AI agent",
              description:
                "For tasks that need judgement: 'triage overnight error jobs', 'summarize storage growth'. Runs claude / codex / gemini with a prompt.",
              icon: <Brain className="w-8 h-8 text-neon-pink" />,
              badge: "more agentic",
              action: (
                <button
                  type="button"
                  className="nerd-btn hover:text-neon-pink"
                  onClick={() => setShowNew(true)}
                >
                  + New AI task
                </button>
              ),
            },
          ]}
        />
      ) : (
        <DataTable
          rows={q.data?.tasks ?? []}
          rowKey={(t) => t.id}
          onRowClick={(t) => setSelected(t)}
          columns={[
            {
              header: "",
              cell: (t) =>
                t.enabled ? (
                  <span className="w-2 h-2 rounded-full bg-keboola inline-block animate-pulse" />
                ) : (
                  <span className="w-2 h-2 rounded-full bg-zinc-700 inline-block" />
                ),
            },
            { header: "Name", cell: (t) => <span className="font-bold">{t.name}</span> },
            { header: "Action", cell: (t) => <ActionLabel action={t.action} /> },
            { header: "Cron", cell: (t) => <span className="font-mono text-xs">{t.cron}</span> },
            {
              header: "Next",
              cell: (t) => (
                <span className="text-xs text-zinc-500">
                  {t.next_run_at ? new Date(t.next_run_at).toLocaleString() : "-"}
                </span>
              ),
            },
            {
              header: "Last",
              cell: (t) => (
                <span className="text-xs text-zinc-500">
                  {t.last_run_at ? new Date(t.last_run_at).toLocaleString() : "never"}
                </span>
              ),
            },
            {
              header: "",
              align: "right",
              cell: (t) => (
                <div className="flex justify-end gap-1">
                  <button
                    type="button"
                    className="nerd-btn text-xs hover:text-keboola"
                    title="Run now (opens detail with live event stream)"
                    onClick={(e) => {
                      e.stopPropagation();
                      setSelectedAutoRun(true);
                      setSelected(t);
                    }}
                  >
                    <Play className="w-3 h-3" />
                  </button>
                  <button
                    type="button"
                    className="nerd-btn text-xs hover:text-neon-pink"
                    title="Edit task"
                    onClick={(e) => {
                      e.stopPropagation();
                      setEditing(t);
                    }}
                  >
                    <Pencil className="w-3 h-3" />
                  </button>
                  <button
                    type="button"
                    className="nerd-btn text-xs"
                    title={t.enabled ? "Disable" : "Enable"}
                    onClick={(e) => {
                      e.stopPropagation();
                      toggleMu.mutate(t);
                    }}
                  >
                    {t.enabled ? <Pause className="w-3 h-3" /> : <Play className="w-3 h-3" />}
                  </button>
                  <button
                    type="button"
                    className="nerd-btn text-xs hover:text-red-400 hover:border-red-700"
                    onClick={(e) => {
                      e.stopPropagation();
                      if (confirm(`Delete task '${t.name}'?`)) deleteMu.mutate(t.id);
                    }}
                  >
                    <Trash2 className="w-3 h-3" />
                  </button>
                </div>
              ),
            },
          ]}
        />
      )}
      {showNew ? <NewTaskDrawer onClose={() => setShowNew(false)} /> : null}
      {editing ? (
        <NewTaskDrawer existing={editing} onClose={() => setEditing(null)} />
      ) : null}
      {selected ? (
        <TaskDetailDrawer
          task={selected}
          autoRun={selectedAutoRun}
          onClose={() => {
            setSelected(null);
            setSelectedAutoRun(false);
          }}
          onEdit={() => {
            setEditing(selected);
            setSelected(null);
            setSelectedAutoRun(false);
          }}
        />
      ) : null}
    </div>
  );
}

// Pull the user-editable fields off an action params blob. Used to hydrate
// the edit form when opening NewTaskDrawer in edit mode. Each action_type
// has a distinct shape; this normalizer is the single place that maps
// backend params -> form state.
function extractActionFormFields(task: AgentTask | undefined): {
  tool: string;
  toolInput: string;
  argv: string;
  aiCli: "claude" | "codex" | "gemini";
  aiPrompt: string;
  aiExtraArgs: string;
} | null {
  if (!task) return null;
  const p = task.action.params as Record<string, unknown>;
  if (task.action.type === "mcp_tool") {
    return {
      tool: String(p.tool ?? "get_jobs"),
      toolInput: JSON.stringify(p.input ?? {}, null, 2),
      argv: "",
      aiCli: "claude",
      aiPrompt: "",
      aiExtraArgs: "",
    };
  }
  if (task.action.type === "cli_command") {
    return {
      tool: "",
      toolInput: "",
      argv: Array.isArray(p.argv) ? (p.argv as string[]).join(" ") : "",
      aiCli: "claude",
      aiPrompt: "",
      aiExtraArgs: "",
    };
  }
  return {
    tool: "",
    toolInput: "",
    argv: "",
    aiCli: (String(p.cli ?? "claude") as "claude" | "codex" | "gemini"),
    aiPrompt: String(p.prompt ?? ""),
    aiExtraArgs: Array.isArray(p.extra_args) ? (p.extra_args as string[]).join(" ") : "",
  };
}

function NewTaskDrawer({
  onClose,
  existing,
}: {
  onClose: () => void;
  existing?: AgentTask;
}) {
  const qc = useQueryClient();
  const { project } = useUIState();
  // Hydrate from `existing` when in edit mode. The defaults below are the
  // "new task" defaults; the existing values take precedence.
  const seed = extractActionFormFields(existing);
  const [name, setName] = useState(existing?.name ?? "");
  const [description, setDescription] = useState(existing?.description ?? "");
  const [cron, setCron] = useState(existing?.cron ?? "0 0 * * *");
  const [enabled, setEnabled] = useState(existing?.enabled ?? true);
  const [actionType, setActionType] = useState<ActionType>(
    existing?.action.type ?? "ai_agent",
  );
  const [tool, setTool] = useState(seed?.tool ?? "get_jobs");
  const [toolInput, setToolInput] = useState(seed?.toolInput ?? '{"status": "error"}');
  const [argv, setArgv] = useState(
    seed?.argv ??
      (project
        ? `job list --project ${project} --status error --limit 10`
        : "doctor"),
  );
  const [aiCli, setAiCli] = useState<"claude" | "codex" | "gemini">(
    seed?.aiCli ?? "claude",
  );
  const [aiPrompt, setAiPrompt] = useState(
    seed?.aiPrompt ??
      (project
        ? `Use the kbagent CLI to list errored jobs in project '${project}' from the last 24 hours, then summarize the top 3 root causes.`
        : "Use the kbagent CLI to summarize the doctor report and flag anything alarming."),
  );
  const [aiExtraArgs, setAiExtraArgs] = useState(seed?.aiExtraArgs ?? "");
  const [error, setError] = useState<string | null>(null);

  const previewQ = useQuery({
    queryKey: ["agent-cron-preview", cron],
    queryFn: () =>
      api.get<{ firings: string[] }>(
        `/agents/cron/preview?cron=${encodeURIComponent(cron)}`,
      ),
    retry: false,
  });

  const [testRun, setTestRun] = useState<AgentRun | null>(null);
  const [testElapsed, setTestElapsed] = useState(0);
  // Live event timeline -- populated by the SSE consumer, rendered while
  // the AI agent is still running so the user sees what claude is doing
  // instead of staring at a "running... 60s" spinner.
  const [testEvents, setTestEvents] = useState<AgentEvent[]>([]);
  const [testRunning, setTestRunning] = useState(false);
  const testHandleRef = useRef<SsePostHandle | null>(null);

  // Dirty-form guard: snapshot the initial state on first render, then compare
  // on every render. If the user touched anything, Esc / backdrop / X clicks
  // ask for confirmation before discarding the form (drawer unmount = state loss).
  // useMemo with [] runs once -- exhaustive-deps would force us to list every
  // field, which would defeat the snapshot.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const initialSnapshot = useMemo(
    () =>
      JSON.stringify({
        name,
        description,
        cron,
        enabled,
        actionType,
        tool,
        toolInput,
        argv,
        aiCli,
        aiPrompt,
        aiExtraArgs,
      }),
    [],
  );
  const currentSnapshot = JSON.stringify({
    name,
    description,
    cron,
    enabled,
    actionType,
    tool,
    toolInput,
    argv,
    aiCli,
    aiPrompt,
    aiExtraArgs,
  });
  const dirty = currentSnapshot !== initialSnapshot;
  const handleClose = () => {
    if (dirty && !window.confirm("Discard unsaved changes?")) return;
    onClose();
  };

  // Build the action body that both Test and Create use -- single source of
  // truth so Test really executes the same action that Create would persist.
  const buildAction = (): { type: ActionType; params: Record<string, unknown> } => {
    let actionParams: Record<string, unknown> = {};
    if (actionType === "mcp_tool") {
      actionParams = {
        tool,
        project,
        input: JSON.parse(toolInput || "{}"),
      };
    } else if (actionType === "cli_command") {
      actionParams = { argv: argv.trim().split(/\s+/) };
    } else {
      const extra = aiExtraArgs.trim() ? aiExtraArgs.trim().split(/\s+/) : [];
      actionParams = { cli: aiCli, prompt: aiPrompt, extra_args: extra };
    }
    return { type: actionType, params: actionParams };
  };

  // Live test runner via SSE. The /agents/test/stream endpoint emits
  // one SSE event per line of claude stdout (parsed JSONL: init / stdout /
  // stderr / done). We collect them in testEvents for live timeline rendering
  // and synthesize a final AgentRun shape from the `done` event so the
  // existing result panel (response / stderr / argv) still works.
  const startTest = () => {
    if (testHandleRef.current) {
      testHandleRef.current.abort();
      testHandleRef.current = null;
    }
    setError(null);
    setTestRun(null);
    setTestEvents([]);
    setTestElapsed(0);
    setTestRunning(true);
    const start = Date.now();
    const tick = setInterval(
      () => setTestElapsed(Math.round((Date.now() - start) / 1000)),
      500,
    );
    const push = (event: string, data: unknown) => {
      setTestEvents((prev) => [
        ...prev,
        {
          event,
          data: (data ?? {}) as Record<string, unknown>,
          at: Date.now() - start,
        },
      ]);
    };
    const handle = ssePost(
      "/agents/test/stream",
      {
        name: name || "[preview]",
        description,
        cron,
        enabled: false,
        action: buildAction(),
      },
      {
        init: (d) => push("init", d),
        stdout: (d) => push("stdout", d),
        stderr: (d) => push("stderr", d),
        done: (d) => {
          push("done", d);
          // Synthesize an AgentRun envelope for the existing result panel.
          const final = (d ?? {}) as Record<string, unknown>;
          setTestRun({
            run_id: "preview",
            task_id: "preview",
            started_at: new Date(start).toISOString(),
            ended_at: new Date().toISOString(),
            status: (final.status as AgentRun["status"]) ?? "ok",
            output: final,
            error: (final.error as string | null) ?? null,
          });
        },
        message: (d) => push("message", d),
      },
    );
    testHandleRef.current = handle;
    handle.done
      .catch((err) => {
        // AbortError is the *expected* outcome of cancelTest() and of the
        // dev-only StrictMode cleanup that fires between mount and re-mount.
        // Don't surface it as a user-facing error.
        if (isAbortError(err)) return;
        setError((err as Error).message);
      })
      .finally(() => {
        clearInterval(tick);
        setTestRunning(false);
        testHandleRef.current = null;
      });
  };

  const cancelTest = () => {
    if (testHandleRef.current) {
      testHandleRef.current.abort();
      testHandleRef.current = null;
    }
    setTestRunning(false);
  };

  const isEditing = existing != null;
  const createMu = useMutation({
    mutationFn: () => {
      const body = {
        name,
        description,
        cron,
        enabled,
        action: buildAction(),
      };
      // PATCH in edit mode (only mutates the named fields server-side via
      // routers/agents.py:update_task). POST for new tasks.
      return isEditing
        ? api.patch(`/agents/${existing!.id}`, body)
        : api.post("/agents", body);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["agents"] });
      onClose();
    },
    onError: (err) => setError((err as Error).message),
  });

  return (
    <Drawer
      open={true}
      onClose={handleClose}
      title={isEditing ? `Edit task: ${existing!.name}` : "New scheduled agent task"}
      subtitle={
        isEditing
          ? "Modify the schedule or action. Save updates the task in-place; existing run history is preserved."
          : "Pick a cron schedule and one of three actions: AI agent (claude/codex/gemini), MCP tool call, or kbagent CLI command."
      }
      width={
        testRunning || testEvents.length > 0 || testRun
          ? "max-w-6xl"
          : "max-w-3xl"
      }
      actions={
        <>
          {testRunning ? (
            <button
              type="button"
              className="nerd-btn hover:text-red-400"
              onClick={cancelTest}
              title="Abort the running test"
            >
              <X className="w-3 h-3 inline mr-1" />
              cancel ({testElapsed}s)
            </button>
          ) : (
            <button
              type="button"
              className="nerd-btn hover:text-neon-pink"
              disabled={createMu.isPending}
              onClick={startTest}
              title="Run this action right now without saving the schedule"
            >
              <Play className="w-3 h-3 inline mr-1" />
              Test now
            </button>
          )}
          <button
            type="button"
            className="nerd-btn hover:text-keboola"
            disabled={!name || createMu.isPending}
            onClick={() => {
              setError(null);
              createMu.mutate();
            }}
          >
            <Bot className="w-3 h-3 inline mr-1" />
            {createMu.isPending
              ? isEditing
                ? "saving..."
                : "creating..."
              : isEditing
                ? "Save"
                : "Create"}
          </button>
        </>
      }
    >
      <div className="space-y-4">
        <label className="text-xs text-zinc-400 block">
          Name
          <input
            className="nerd-input w-full mt-1"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Nightly error-job triage"
            required
          />
        </label>
        <label className="text-xs text-zinc-400 block">
          Description (optional)
          <input
            className="nerd-input w-full mt-1"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </label>

        <div>
          <div className="text-xs text-zinc-400 mb-1">Cron schedule</div>
          <input
            className="nerd-input w-full font-mono"
            value={cron}
            onChange={(e) => setCron(e.target.value)}
          />
          <div className="flex flex-wrap gap-1 mt-2">
            {CRON_PRESETS.map((p) => (
              <button
                key={p.cron}
                type="button"
                className="nerd-pill hover:border-keboola hover:text-keboola"
                onClick={() => setCron(p.cron)}
              >
                {p.label}
              </button>
            ))}
          </div>
          {previewQ.data ? (
            <div className="text-xs text-zinc-500 mt-2">
              Next firings:{" "}
              <span className="font-mono text-accent">
                {previewQ.data.firings
                  .slice(0, 3)
                  .map((f) => new Date(f).toLocaleString())
                  .join(" → ")}
              </span>
            </div>
          ) : previewQ.error ? (
            <div className="text-xs text-red-400 mt-2">Invalid cron expression</div>
          ) : null}
        </div>

        <div>
          <div className="text-xs text-zinc-400 mb-1">Action type</div>
          <div className="flex gap-2 flex-wrap">
            <button
              type="button"
              className={`nerd-btn flex items-center gap-1 ${
                actionType === "ai_agent" ? "border-neon-pink text-neon-pink" : ""
              }`}
              onClick={() => setActionType("ai_agent")}
            >
              <Brain className="w-3 h-3" /> AI agent
            </button>
            <button
              type="button"
              className={`nerd-btn flex items-center gap-1 ${
                actionType === "cli_command" ? "border-keboola text-keboola" : ""
              }`}
              onClick={() => setActionType("cli_command")}
            >
              <Terminal className="w-3 h-3" /> CLI command
            </button>
            <button
              type="button"
              className={`nerd-btn flex items-center gap-1 ${
                actionType === "mcp_tool" ? "border-keboola text-keboola" : ""
              }`}
              onClick={() => setActionType("mcp_tool")}
            >
              <Sparkles className="w-3 h-3" /> MCP tool
            </button>
          </div>
        </div>

        {actionType === "ai_agent" ? (
          <>
            <div>
              <div className="text-xs text-zinc-400 mb-1">AI CLI</div>
              <div className="flex gap-2">
                {(["claude", "codex", "gemini"] as const).map((c) => (
                  <button
                    key={c}
                    type="button"
                    className={`nerd-btn ${
                      aiCli === c ? "border-neon-pink text-neon-pink" : ""
                    }`}
                    onClick={() => setAiCli(c)}
                  >
                    {c}
                  </button>
                ))}
              </div>
              <div className="text-xs text-zinc-600 mt-2">
                The CLI must be installed and authenticated on this machine.
                The agent runs once with the prompt below and exits; its full
                response is captured into the run history.
              </div>
            </div>
            <label className="text-xs text-zinc-400 block">
              Prompt
              <textarea
                className="nerd-input w-full mt-1 font-mono h-40"
                value={aiPrompt}
                onChange={(e) => setAiPrompt(e.target.value)}
                placeholder="Check overnight error jobs and summarize the top 3 root causes"
              />
            </label>
            <label className="text-xs text-zinc-400 block">
              Extra CLI args (optional, space-separated)
              <input
                className="nerd-input w-full mt-1 font-mono"
                value={aiExtraArgs}
                onChange={(e) => setAiExtraArgs(e.target.value)}
                placeholder="--allowed-tools Read,Bash"
              />
            </label>
          </>
        ) : actionType === "cli_command" ? (
          <label className="text-xs text-zinc-400 block">
            Argv (will be prefixed with 'kbagent' if missing)
            <input
              className="nerd-input w-full mt-1 font-mono"
              value={argv}
              onChange={(e) => setArgv(e.target.value)}
            />
            <span className="text-zinc-600">
              stdout/stderr is captured into the run history.
            </span>
          </label>
        ) : (
          <>
            <label className="text-xs text-zinc-400 block">
              MCP tool name
              <input
                className="nerd-input w-full mt-1 font-mono"
                value={tool}
                onChange={(e) => setTool(e.target.value)}
              />
            </label>
            <label className="text-xs text-zinc-400 block">
              Tool input (JSON, current project = {project ?? "(none)"})
              <textarea
                className="nerd-input w-full mt-1 font-mono h-32"
                value={toolInput}
                onChange={(e) => setToolInput(e.target.value)}
              />
            </label>
          </>
        )}

        <label className="flex items-center gap-2 text-xs text-zinc-400">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
          />
          Enable immediately (will start running per cron)
        </label>
        {error ? <ErrorBox message={error} /> : null}
      </div>
      {testRunning || testEvents.length > 0 ? (
        <div className="mt-4 space-y-3">
          <AgentRunView
            events={testEvents}
            running={testRunning}
            elapsed={testElapsed}
            onCancel={cancelTest}
          />
          <AgentRunRaw events={testEvents} />
          {testRun?.output && ("stdout" in testRun.output || "results" in testRun.output) ? (
            <div className="nerd-card">
              <h3 className="text-xs uppercase tracking-wider text-zinc-500 mb-2">
                Raw output (cli/mcp action)
              </h3>
              {testRun.output && "stdout" in testRun.output ? (
                <pre
                  className="nerd-code whitespace-pre-wrap"
                  style={{ maxHeight: "240px" }}
                >
                  {String(testRun.output.stdout ?? "")}
                </pre>
              ) : null}
              {testRun.output && "results" in testRun.output ? (
                <JsonView data={testRun.output} maxHeight="320px" />
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </Drawer>
  );
}

function TaskDetailDrawer({
  task,
  autoRun,
  onClose,
  onEdit,
}: {
  task: AgentTask;
  autoRun: boolean;
  onClose: () => void;
  onEdit: () => void;
}) {
  const qc = useQueryClient();
  const runsQ = useQuery<{ runs: AgentRun[] }>({
    queryKey: ["agent-runs", task.id],
    queryFn: () => api.get(`/agents/${task.id}/runs?limit=50`),
    refetchInterval: 5_000,
  });

  // Live-run state mirrors what NewTaskDrawer does for /agents/test/stream,
  // but here we point at /agents/{id}/run/stream (persistent + attach-aware).
  const [liveEvents, setLiveEvents] = useState<AgentEvent[]>([]);
  const [liveRunning, setLiveRunning] = useState(false);
  const [liveElapsed, setLiveElapsed] = useState(0);
  const [liveError, setLiveError] = useState<string | null>(null);
  const liveHandleRef = useRef<SsePostHandle | null>(null);

  const startLive = () => {
    if (liveHandleRef.current) {
      liveHandleRef.current.abort();
      liveHandleRef.current = null;
    }
    setLiveError(null);
    setLiveEvents([]);
    setLiveElapsed(0);
    setLiveRunning(true);
    const start = Date.now();
    const tick = setInterval(
      () => setLiveElapsed(Math.round((Date.now() - start) / 1000)),
      500,
    );
    const push = (event: string, data: unknown) => {
      setLiveEvents((prev) => [
        ...prev,
        { event, data: (data ?? {}) as Record<string, unknown>, at: Date.now() - start },
      ]);
    };
    const handle = ssePost(
      `/agents/${task.id}/run/stream`,
      {},
      {
        init: (d) => push("init", d),
        stdout: (d) => push("stdout", d),
        stderr: (d) => push("stderr", d),
        done: (d) => {
          push("done", d);
          // Final persistent run record will appear in /runs via auto-refresh.
          qc.invalidateQueries({ queryKey: ["agent-runs", task.id] });
          qc.invalidateQueries({ queryKey: ["agents"] });
        },
        message: (d) => push("message", d),
      },
    );
    liveHandleRef.current = handle;
    handle.done
      .catch((err) => {
        if (isAbortError(err)) return;
        setLiveError((err as Error).message);
      })
      .finally(() => {
        clearInterval(tick);
        setLiveRunning(false);
        liveHandleRef.current = null;
      });
  };

  const cancelLive = () => {
    // The kill-on-empty broadcaster on the server will cancel the underlying
    // process once we disconnect (we are the only subscriber, by design).
    if (liveHandleRef.current) {
      liveHandleRef.current.abort();
      liveHandleRef.current = null;
    }
    setLiveRunning(false);
  };

  // Auto-start the stream when opened via the row's Play button. We guard
  // with a ref so a re-render doesn't re-fire the start.
  const autoStartedRef = useRef(false);
  useEffect(() => {
    if (autoRun && !autoStartedRef.current) {
      autoStartedRef.current = true;
      startLive();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoRun]);

  // Cancel on unmount so closing the drawer kills the spawned process.
  useEffect(() => {
    return () => {
      if (liveHandleRef.current) {
        liveHandleRef.current.abort();
        liveHandleRef.current = null;
      }
    };
  }, []);

  const showLive = liveRunning || liveEvents.length > 0;

  return (
    <Drawer
      open={true}
      onClose={onClose}
      title={task.name}
      subtitle={`${task.action.type} ・ cron: ${task.cron} ・ ${
        task.enabled ? "enabled" : "disabled"
      }`}
      width={showLive ? "max-w-7xl" : "max-w-4xl"}
      actions={
        <>
          {liveRunning ? (
            <button
              type="button"
              className="nerd-btn hover:text-red-500"
              onClick={cancelLive}
              title="Stop the live run (kill-on-disconnect)"
            >
              <X className="w-3 h-3 inline mr-1" />
              cancel ({liveElapsed}s)
            </button>
          ) : (
            <button
              type="button"
              className="nerd-btn hover:text-neon-pink"
              onClick={startLive}
              title="Run live with SSE event stream"
            >
              <Play className="w-3 h-3 inline mr-1" />
              Run live
            </button>
          )}
          <button
            type="button"
            className="nerd-btn hover:text-keboola"
            onClick={onEdit}
            title="Edit this task"
          >
            <Pencil className="w-3 h-3 inline mr-1" />
            Edit
          </button>
        </>
      }
    >
      <div className="space-y-4">
        {showLive ? (
          <div className="space-y-3">
            <AgentRunView
              events={liveEvents}
              running={liveRunning}
              elapsed={liveElapsed}
              onCancel={cancelLive}
            />
            <AgentRunRaw events={liveEvents} />
          </div>
        ) : null}
        {liveError ? <ErrorBox message={liveError} /> : null}
        <div className="nerd-card">
          <h3 className="text-xs uppercase tracking-wider text-zinc-500 mb-2">Action</h3>
          <JsonView data={task.action} maxHeight="200px" />
        </div>
        <div>
          <h3 className="text-xs uppercase tracking-wider text-zinc-500 mb-2">
            Recent runs (auto-refreshing)
          </h3>
          {runsQ.isLoading ? <Loading /> : null}
          {runsQ.data?.runs.length === 0 ? (
            <div className="text-xs text-zinc-500">No runs yet.</div>
          ) : null}
          <div className="space-y-2">
            {(runsQ.data?.runs ?? []).map((r) => (
              <RunItem key={r.run_id} run={r} taskId={task.id} />
            ))}
          </div>
        </div>
      </div>
    </Drawer>
  );
}

function RunItem({ run, taskId }: { run: AgentRun; taskId: string }) {
  const [open, setOpen] = useState(false);
  // Lazy-load the persisted event timeline only when the user expands the
  // row -- keeps the runs list cheap (we'd otherwise refetch N timelines
  // every 5s as the list auto-refreshes).
  const eventsQ = useQuery<{ events: AgentEvent[]; count: number }>({
    queryKey: ["agent-run-events", taskId, run.run_id],
    queryFn: () => api.get(`/agents/${taskId}/runs/${run.run_id}/events`),
    enabled: open && !!run.events_path,
    retry: false,
  });
  const styleClass =
    run.status === "ok"
      ? "nerd-pill-green"
      : run.status === "error"
        ? "nerd-pill-red"
        : "nerd-pill-amber";
  // For ai_agent runs, show the response prominently in addition to the JSON dump.
  const aiResponse =
    run.output && typeof run.output === "object" && "response" in run.output
      ? String((run.output as Record<string, unknown>).response ?? "")
      : null;
  const cliStdout =
    run.output && typeof run.output === "object" && "stdout" in run.output
      ? String((run.output as Record<string, unknown>).stdout ?? "")
      : null;
  // Surface a few key metrics inline on the collapsed row so users can scan
  // the runs list at a glance ($ + tokens + tool calls per run).
  const summary = run.summary ?? null;
  const dur = run.ended_at
    ? Math.round((new Date(run.ended_at).getTime() - new Date(run.started_at).getTime()) / 1000)
    : null;
  return (
    <div className="border border-zinc-200 rounded bg-white dark:border-zinc-800 dark:bg-transparent">
      <button
        type="button"
        className="w-full text-left px-3 py-2 flex items-center justify-between hover:bg-zinc-50 dark:hover:bg-zinc-900/40"
        onClick={() => setOpen((o) => !o)}
      >
        <div className="flex items-center gap-3 text-xs flex-wrap">
          <span className={styleClass}>{run.status}</span>
          <span className="text-zinc-700 dark:text-zinc-400">
            {new Date(run.started_at).toLocaleString()}
          </span>
          {dur != null ? (
            <span className="text-zinc-500 dark:text-zinc-600">({dur}s)</span>
          ) : null}
          {summary?.cost_usd?.total != null ? (
            <span className="nerd-pill border-keboola/40 text-keboola">
              ${summary.cost_usd.total.toFixed(summary.cost_usd.total < 0.01 ? 4 : 3)}
            </span>
          ) : null}
          {summary?.tokens?.total ? (
            <span className="nerd-pill">
              {summary.tokens.total < 1000
                ? `${summary.tokens.total} tok`
                : `${(summary.tokens.total / 1000).toFixed(1)}k tok`}
            </span>
          ) : null}
          {summary?.tools?.count ? (
            <span className="nerd-pill">
              {summary.tools.count} tool{summary.tools.count === 1 ? "" : "s"}
              {summary.tools.errors > 0 ? (
                <span className="text-red-500 ml-1">({summary.tools.errors} err)</span>
              ) : null}
            </span>
          ) : null}
        </div>
        <span className="text-zinc-500 dark:text-zinc-600 text-xs">
          {open ? "− hide" : "+ details"}
        </span>
      </button>
      {open ? (
        <div className="p-3 border-t border-zinc-100 dark:border-zinc-900 space-y-3">
          {run.error ? <ErrorBox message={run.error} /> : null}
          {/* Replay the persisted timeline if available -- gives historical
              runs the same 3-panel view the live runs show. */}
          {run.events_path ? (
            eventsQ.isLoading ? (
              <Loading />
            ) : eventsQ.error ? (
              <ErrorBox message={(eventsQ.error as Error).message} />
            ) : eventsQ.data ? (
              <>
                <AgentRunView events={eventsQ.data.events} summary={summary ?? undefined} />
                <AgentRunRaw events={eventsQ.data.events} />
              </>
            ) : null
          ) : aiResponse ? (
            <details open>
              <summary className="text-xs text-zinc-500 cursor-pointer">
                AI response (legacy run, no event timeline persisted)
              </summary>
              <pre className="nerd-code whitespace-pre-wrap" style={{ maxHeight: "320px" }}>
                {aiResponse}
              </pre>
            </details>
          ) : null}
          {cliStdout ? (
            <details>
              <summary className="text-xs text-zinc-500 cursor-pointer">stdout</summary>
              <pre className="nerd-code whitespace-pre-wrap" style={{ maxHeight: "320px" }}>
                {cliStdout}
              </pre>
            </details>
          ) : null}
          {run.output ? (
            <details>
              <summary className="text-xs text-zinc-500 cursor-pointer">raw json</summary>
              <JsonView data={run.output} maxHeight="240px" />
            </details>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
