import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Bot,
  Brain,
  Check,
  ChevronRight,
  Clipboard,
  Pause,
  Pencil,
  Play,
  Plus,
  Sparkles,
  Terminal,
  Trash2,
  Wrench,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, ssePost, type SsePostHandle } from "../api/client";
import { Drawer } from "../components/Drawer";
import { ErrorBox, Loading, PageTitle, TwoPathEmpty } from "../components/Empty";
import { JsonView } from "../components/JsonView";
import { DataTable } from "../components/Table";
import { useUIState } from "../state";

type ActionType = "mcp_tool" | "cli_command" | "ai_agent";

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
}

// One line item in the live event timeline shown during a test run.
// Mirror of the SSE `event:` field; data shape depends on event name
// (init / stdout / stderr / done). See routers/agents.py:test_action_stream.
interface TestEvent {
  type: string;
  data: Record<string, unknown>;
  at: number; // ms since the test started
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

// Render the live event stream from /agents/test/stream. We do type-aware
// formatting for claude's stream-json events (the ones the user wants to see:
// "session init", "tool use Bash", "tool result", "assistant text") and fall
// back to a raw JSON line for unknown shapes.
// Serialize the event stream as a copy-paste-friendly text block.
// One event per line for stdout/stderr (the JSONL body) and a single
// line for init/done. Mirrors what the user visually scans, but without
// the type-aware icons -- so they can paste into a bug report or grep.
function formatEventsForClipboard(events: TestEvent[]): string {
  const lines: string[] = [];
  for (const evt of events) {
    const ts = `+${(evt.at / 1000).toFixed(1)}s`;
    const d = evt.data;
    if (evt.type === "init") {
      lines.push(`${ts} [init] ${d.cli ?? d.action_type ?? ""} argv=${JSON.stringify(d.argv ?? [])}`);
    } else if (evt.type === "stdout" || evt.type === "stderr") {
      const body =
        "raw" in d ? String(d.raw) : JSON.stringify(d);
      lines.push(`${ts} [${evt.type}] ${body}`);
    } else if (evt.type === "done") {
      lines.push(
        `${ts} [done] status=${d.status ?? "?"} exit=${d.exit_code ?? "?"} elapsed=${d.elapsed_seconds ?? "?"}s`,
      );
      if (d.response) lines.push(`--- response ---\n${d.response}`);
      if (d.stderr) lines.push(`--- stderr ---\n${d.stderr}`);
      if (d.error) lines.push(`--- error ---\n${d.error}`);
    } else {
      lines.push(`${ts} [${evt.type}] ${JSON.stringify(d)}`);
    }
  }
  return lines.join("\n");
}

function TimelinePanel({
  events,
  running,
  elapsed,
}: {
  events: TestEvent[];
  running: boolean;
  elapsed: number;
}) {
  // Stick-to-bottom UX: auto-scroll on new events only while the user is
  // already near the bottom (within ~40px). If they scrolled up to read
  // an older event we leave their viewport alone -- a chat-log convention
  // (Slack, Discord, terminal `tail -f` UIs).
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickyRef = useRef(true);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !stickyRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, [events.length]);
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(formatEventsForClipboard(events));
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard API requires secure context + user gesture. We're inside
      // an onClick so the gesture is fine; secure context is the failure
      // mode (http:// without --allow-insecure-localhost). Silent fall
      // through: user will notice "copied!" never appears.
    }
  };
  return (
    <div className="nerd-card border-neon-pink/40">
      <div className="flex items-center justify-between mb-2">
        <div className="text-xs text-neon-pink flex items-center gap-2">
          {running ? (
            <>
              <span className="w-2 h-2 rounded-full bg-neon-pink animate-pulse" />
              Test running ・ {elapsed}s elapsed ・ AI agents can take 30-120s
            </>
          ) : (
            <>
              <span className="w-2 h-2 rounded-full bg-zinc-700" />
              Test finished ・ {events.length} events
            </>
          )}
        </div>
        <button
          type="button"
          onClick={onCopy}
          className="nerd-btn text-[10px] flex items-center gap-1"
          title="Copy the full event log as plain text (for bug reports, grep, jq)"
          disabled={events.length === 0}
        >
          {copied ? (
            <>
              <Check className="w-3 h-3 text-keboola" />
              copied!
            </>
          ) : (
            <>
              <Clipboard className="w-3 h-3" />
              copy log
            </>
          )}
        </button>
      </div>
      <div
        ref={scrollRef}
        onScroll={(e) => {
          const el = e.currentTarget;
          stickyRef.current =
            el.scrollHeight - el.scrollTop - el.clientHeight < 40;
        }}
        className="space-y-1 font-mono text-[11px] overflow-auto"
        style={{ maxHeight: "calc(100vh - 14rem)" }}
      >
        {events.map((evt, i) => (
          <TimelineRow key={i} event={evt} />
        ))}
      </div>
    </div>
  );
}

function TimelineRow({ event }: { event: TestEvent }) {
  const { type, data, at } = event;
  const ts = `+${(at / 1000).toFixed(1)}s`;
  const claudeType =
    type === "stdout" && typeof data?.type === "string"
      ? (data.type as string)
      : null;

  // --- claude stream-json events: type-aware pretty rendering ---
  if (claudeType === "system" && data.subtype === "init") {
    return (
      <div className="text-zinc-500">
        <span className="text-zinc-600 mr-2">{ts}</span>
        <ChevronRight className="w-3 h-3 inline" /> session init ・ model{" "}
        <span className="text-accent">{String(data.model ?? "?")}</span>
        {data.tools ? ` ・ ${(data.tools as unknown[]).length} tools` : ""}
      </div>
    );
  }
  if (claudeType === "assistant") {
    const content = ((data.message as Record<string, unknown> | undefined)?.content ??
      []) as Array<Record<string, unknown>>;
    return (
      <>
        {content.map((block, i) => {
          if (block.type === "text") {
            return (
              <div key={i} className="text-zinc-200">
                <span className="text-zinc-600 mr-2">{ts}</span>
                <Brain className="w-3 h-3 inline mr-1 text-neon-pink" />
                {String(block.text ?? "")}
              </div>
            );
          }
          if (block.type === "tool_use") {
            const input = block.input ? JSON.stringify(block.input) : "";
            return (
              <div key={i} className="text-keboola">
                <span className="text-zinc-600 mr-2">{ts}</span>
                <Wrench className="w-3 h-3 inline mr-1" /> {String(block.name)}(
                <span className="text-zinc-500">
                  {input.length > 120 ? input.slice(0, 120) + "…" : input}
                </span>
                )
              </div>
            );
          }
          return (
            <div key={i} className="text-zinc-500">
              <span className="text-zinc-600 mr-2">{ts}</span>
              {JSON.stringify(block)}
            </div>
          );
        })}
      </>
    );
  }
  if (claudeType === "user") {
    const content = ((data.message as Record<string, unknown> | undefined)?.content ??
      []) as Array<Record<string, unknown>>;
    return (
      <>
        {content.map((block, i) => {
          if (block.type === "tool_result") {
            const text = Array.isArray(block.content)
              ? (block.content as Array<{ text?: string }>)
                  .map((c) => c.text ?? "")
                  .join("")
              : String(block.content ?? "");
            const isError = block.is_error === true;
            return (
              <div
                key={i}
                className={isError ? "text-red-400" : "text-zinc-400"}
              >
                <span className="text-zinc-600 mr-2">{ts}</span>
                {isError ? "✗" : "✓"} tool result:{" "}
                <span className="text-zinc-500">
                  {text.length > 200 ? text.slice(0, 200) + "…" : text}
                </span>
              </div>
            );
          }
          return null;
        })}
      </>
    );
  }
  if (claudeType === "result") {
    return (
      <div className="text-keboola font-bold">
        <span className="text-zinc-600 mr-2">{ts}</span>
        ✓ result ・ {String(data.subtype ?? "")} ・{" "}
        {data.duration_ms ? `${Math.round((data.duration_ms as number) / 100) / 10}s` : ""}
      </div>
    );
  }

  // --- non-claude events ---
  if (type === "init") {
    return (
      <div className="text-zinc-500">
        <span className="text-zinc-600 mr-2">{ts}</span>
        <ChevronRight className="w-3 h-3 inline" /> spawning{" "}
        <span className="text-accent">{String(data.cli ?? data.action_type ?? "?")}</span>
      </div>
    );
  }
  if (type === "stderr") {
    return (
      <div className="text-yellow-500/80">
        <span className="text-zinc-600 mr-2">{ts}</span>
        stderr: {String(data.raw ?? "")}
      </div>
    );
  }
  if (type === "done") {
    const status = String(data.status ?? "?");
    return (
      <div
        className={`font-bold ${
          status === "ok" ? "text-keboola" : "text-red-400"
        }`}
      >
        <span className="text-zinc-600 mr-2">{ts}</span>
        done ・ status={status} ・ exit={String(data.exit_code ?? "?")}
      </div>
    );
  }
  // Fallback: raw JSON
  return (
    <div className="text-zinc-500">
      <span className="text-zinc-600 mr-2">{ts}</span>
      [{type}] {JSON.stringify(data)}
    </div>
  );
}

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
  const [testEvents, setTestEvents] = useState<TestEvent[]>([]);
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
    const push = (type: string, data: unknown) => {
      setTestEvents((prev) => [
        ...prev,
        {
          type,
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
      <div className="flex gap-4">
        <div className="flex-1 min-w-0 space-y-4">
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
        {testRunning || testEvents.length > 0 || testRun ? (
          <aside
            className="w-[440px] flex-shrink-0 sticky top-0 self-start space-y-3"
            style={{ maxHeight: "calc(100vh - 8rem)" }}
          >
            {testRunning || testEvents.length > 0 ? (
              <TimelinePanel
                events={testEvents}
                running={testRunning}
                elapsed={testElapsed}
              />
            ) : null}
            {testRun ? (
          <div
            className={`nerd-card ${
              testRun.status === "ok"
                ? "border-keboola/40"
                : "border-red-700/40"
            }`}
          >
            <div className="flex items-center justify-between mb-2">
              <h3 className="text-sm font-bold text-keboola">
                Test result ・ {testRun.status} ・{" "}
                {testRun.ended_at && testRun.started_at
                  ? `${Math.round(
                      (new Date(testRun.ended_at).getTime() -
                        new Date(testRun.started_at).getTime()) /
                        1000,
                    )}s`
                  : "?s"}
              </h3>
              <button
                type="button"
                className="nerd-btn text-xs"
                onClick={() => setTestRun(null)}
              >
                clear
              </button>
            </div>
            {testRun.error ? <ErrorBox message={testRun.error} /> : null}
            {testRun.output && "response" in testRun.output ? (
              <>
                <details open>
                  <summary className="text-xs text-keboola cursor-pointer">
                    AI response (exit code:{" "}
                    {String(testRun.output.exit_code ?? "?")})
                  </summary>
                  <pre
                    className="nerd-code whitespace-pre-wrap"
                    style={{ maxHeight: "320px" }}
                  >
                    {String(testRun.output.response ?? "")}
                  </pre>
                </details>
                {testRun.output.stderr ? (
                  <details
                    open={
                      Number(testRun.output.exit_code) !== 0 ||
                      !testRun.output.response
                    }
                  >
                    <summary className="text-xs text-zinc-500 cursor-pointer mt-2">
                      stderr (where claude logs its plan + errors)
                    </summary>
                    <pre
                      className="nerd-code whitespace-pre-wrap"
                      style={{ maxHeight: "240px" }}
                    >
                      {String(testRun.output.stderr ?? "")}
                    </pre>
                  </details>
                ) : null}
                {testRun.output.argv ? (
                  <details>
                    <summary className="text-xs text-zinc-500 cursor-pointer mt-2">
                      argv (exact subprocess invocation)
                    </summary>
                    <pre
                      className="nerd-code whitespace-pre-wrap"
                      style={{ maxHeight: "120px" }}
                    >
                      {Array.isArray(testRun.output.argv)
                        ? (testRun.output.argv as string[]).join(" ")
                        : String(testRun.output.argv)}
                    </pre>
                  </details>
                ) : null}
              </>
            ) : null}
            {testRun.output && "stdout" in testRun.output ? (
              <details open>
                <summary className="text-xs text-keboola cursor-pointer">
                  stdout (exit code:{" "}
                  {String(testRun.output.exit_code ?? "?")})
                </summary>
                <pre
                  className="nerd-code whitespace-pre-wrap"
                  style={{ maxHeight: "240px" }}
                >
                  {String(testRun.output.stdout ?? "")}
                </pre>
                {testRun.output.stderr ? (
                  <details>
                    <summary className="text-xs text-zinc-500 cursor-pointer mt-2">
                      stderr
                    </summary>
                    <pre
                      className="nerd-code whitespace-pre-wrap"
                      style={{ maxHeight: "160px" }}
                    >
                      {String(testRun.output.stderr ?? "")}
                    </pre>
                  </details>
                ) : null}
              </details>
            ) : null}
            {testRun.output && "results" in testRun.output ? (
              <details open>
                <summary className="text-xs text-keboola cursor-pointer">
                  MCP results
                </summary>
                <JsonView data={testRun.output} maxHeight="320px" />
              </details>
            ) : null}
          </div>
        ) : null}
          </aside>
        ) : null}
      </div>
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
  const [liveEvents, setLiveEvents] = useState<TestEvent[]>([]);
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
    const push = (type: string, data: unknown) => {
      setLiveEvents((prev) => [
        ...prev,
        { type, data: (data ?? {}) as Record<string, unknown>, at: Date.now() - start },
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
      .catch((err) => setLiveError((err as Error).message))
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

  const showSidebar = liveRunning || liveEvents.length > 0;

  return (
    <Drawer
      open={true}
      onClose={onClose}
      title={task.name}
      subtitle={`${task.action.type} ・ cron: ${task.cron} ・ ${
        task.enabled ? "enabled" : "disabled"
      }`}
      width={showSidebar ? "max-w-6xl" : "max-w-4xl"}
      actions={
        <>
          {liveRunning ? (
            <button
              type="button"
              className="nerd-btn hover:text-red-400"
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
      <div className="flex gap-4">
        <div className="flex-1 min-w-0 space-y-4">
          <div className="nerd-card">
            <h3 className="text-xs uppercase tracking-wider text-zinc-500 mb-2">
              Action
            </h3>
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
                <RunItem key={r.run_id} run={r} />
              ))}
            </div>
          </div>
          {liveError ? <ErrorBox message={liveError} /> : null}
        </div>
        {showSidebar ? (
          <aside
            className="w-[440px] flex-shrink-0 sticky top-0 self-start"
            style={{ maxHeight: "calc(100vh - 8rem)" }}
          >
            <TimelinePanel
              events={liveEvents}
              running={liveRunning}
              elapsed={liveElapsed}
            />
          </aside>
        ) : null}
      </div>
    </Drawer>
  );
}

function RunItem({ run }: { run: AgentRun }) {
  const [open, setOpen] = useState(false);
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
  return (
    <div className="border border-zinc-800 rounded">
      <button
        type="button"
        className="w-full text-left px-3 py-2 flex items-center justify-between hover:bg-zinc-900/40"
        onClick={() => setOpen((o) => !o)}
      >
        <div className="flex items-center gap-3 text-xs">
          <span className={styleClass}>{run.status}</span>
          <span className="text-zinc-400">{new Date(run.started_at).toLocaleString()}</span>
          {run.ended_at ? (
            <span className="text-zinc-600">
              (
              {Math.round(
                (new Date(run.ended_at).getTime() - new Date(run.started_at).getTime()) /
                  1000,
              )}
              s)
            </span>
          ) : null}
        </div>
        <span className="text-zinc-600 text-xs">{open ? "− hide" : "+ details"}</span>
      </button>
      {open ? (
        <div className="p-3 border-t border-zinc-900 space-y-3">
          {run.error ? <ErrorBox message={run.error} /> : null}
          {aiResponse ? (
            <details open>
              <summary className="text-xs text-zinc-500 cursor-pointer">
                AI response
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
