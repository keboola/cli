import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, Brain, Pause, Play, Plus, Sparkles, Terminal, Trash2 } from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import { Drawer } from "../components/Drawer";
import { Empty, ErrorBox, Loading, PageTitle } from "../components/Empty";
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

export function AgentsPage() {
  const qc = useQueryClient();
  const [showNew, setShowNew] = useState(false);
  const [selected, setSelected] = useState<AgentTask | null>(null);

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
  const runMu = useMutation({
    mutationFn: (id: string) => api.post(`/agents/${id}/run`),
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
        <Empty
          title="No agent tasks scheduled yet"
          hint="Click 'New task' to create your first one. E.g. every midnight, ask claude to triage error jobs."
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
                    title="Run now"
                    onClick={(e) => {
                      e.stopPropagation();
                      runMu.mutate(t.id);
                    }}
                  >
                    <Play className="w-3 h-3" />
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
      {selected ? (
        <TaskDetailDrawer task={selected} onClose={() => setSelected(null)} />
      ) : null}
    </div>
  );
}

function NewTaskDrawer({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const { project } = useUIState();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [cron, setCron] = useState("0 0 * * *");
  const [enabled, setEnabled] = useState(true);
  const [actionType, setActionType] = useState<ActionType>("ai_agent");
  const [tool, setTool] = useState("get_jobs");
  const [toolInput, setToolInput] = useState('{"status": "error"}');
  const [argv, setArgv] = useState(
    project ? `job list --project ${project} --status error --limit 10` : "doctor",
  );
  const [aiCli, setAiCli] = useState<"claude" | "codex" | "gemini">("claude");
  const [aiPrompt, setAiPrompt] = useState(
    project
      ? `Use the kbagent CLI to list errored jobs in project '${project}' from the last 24 hours, then summarize the top 3 root causes.`
      : "Use the kbagent CLI to summarize the doctor report and flag anything alarming.",
  );
  const [aiExtraArgs, setAiExtraArgs] = useState("");
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

  const testMu = useMutation({
    mutationFn: async () => {
      const start = Date.now();
      // Tick an "elapsed" counter every 500ms so the user sees something
      // happen instead of staring at a frozen button (AI agent runs can
      // take 30-120 seconds).
      const tick = setInterval(
        () => setTestElapsed(Math.round((Date.now() - start) / 1000)),
        500,
      );
      try {
        return await api.post<AgentRun>("/agents/test", {
          name: name || "[preview]",
          description,
          cron,
          enabled: false,
          action: buildAction(),
        });
      } finally {
        clearInterval(tick);
      }
    },
    onSuccess: (data) => {
      setTestRun(data);
      setError(null);
    },
    onError: (err) => {
      setTestRun(null);
      setError((err as Error).message);
    },
  });

  const createMu = useMutation({
    mutationFn: () =>
      api.post("/agents", {
        name,
        description,
        cron,
        enabled,
        action: buildAction(),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["agents"] });
      onClose();
    },
    onError: (err) => setError((err as Error).message),
  });

  return (
    <Drawer
      open={true}
      onClose={onClose}
      title="New scheduled agent task"
      subtitle="Pick a cron schedule and one of three actions: AI agent (claude/codex/gemini), MCP tool call, or kbagent CLI command."
      width="max-w-3xl"
      actions={
        <>
          <button
            type="button"
            className="nerd-btn hover:text-neon-pink"
            disabled={testMu.isPending || createMu.isPending}
            onClick={() => {
              setError(null);
              setTestRun(null);
              setTestElapsed(0);
              testMu.mutate();
            }}
            title="Run this action right now without saving the schedule"
          >
            <Play className="w-3 h-3 inline mr-1" />
            {testMu.isPending ? `running... ${testElapsed}s` : "Test now"}
          </button>
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
            {createMu.isPending ? "creating..." : "Create"}
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

        {testMu.isPending ? (
          <div className="nerd-card border-neon-pink/40">
            <div className="text-xs text-neon-pink flex items-center gap-2">
              <span className="w-2 h-2 rounded-full bg-neon-pink animate-pulse" />
              Test running ・ {testElapsed}s elapsed ・ AI agents can take 30-120s
            </div>
            <div className="text-zinc-500 text-xs mt-1">
              Sending the action to /agents/test (no persistence). Output appears
              below when finished.
            </div>
          </div>
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
              <details open>
                <summary className="text-xs text-keboola cursor-pointer">
                  AI response
                </summary>
                <pre
                  className="nerd-code whitespace-pre-wrap"
                  style={{ maxHeight: "320px" }}
                >
                  {String(testRun.output.response ?? "")}
                </pre>
              </details>
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
      </div>
    </Drawer>
  );
}

function TaskDetailDrawer({
  task,
  onClose,
}: {
  task: AgentTask;
  onClose: () => void;
}) {
  const runsQ = useQuery<{ runs: AgentRun[] }>({
    queryKey: ["agent-runs", task.id],
    queryFn: () => api.get(`/agents/${task.id}/runs?limit=50`),
    refetchInterval: 5_000,
  });
  return (
    <Drawer
      open={true}
      onClose={onClose}
      title={task.name}
      subtitle={`${task.action.type} ・ cron: ${task.cron} ・ ${
        task.enabled ? "enabled" : "disabled"
      }`}
      width="max-w-4xl"
    >
      <div className="space-y-4">
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
