/**
 * Three-panel visualization of an agent run.
 *
 * Layout (wide screens):
 *
 *   ┌───────────────┬─────────────────────────┬─────────────┐
 *   │  Steps        │  Step detail            │  Cost &     │
 *   │  (timeline)   │  (selected step body)   │  tokens     │
 *   │               │                         │             │
 *   │  thinking →   │  Bash(...)              │  Opus 4.7   │
 *   │  Bash         │  └ command: ls          │  $0.0234    │
 *   │  └ ok         │  └ output: ...          │  1.2k tok   │
 *   │  Read         │                         │             │
 *   │  └ ok         │                         │  Tools used │
 *   │  Result       │                         │  Bash × 4   │
 *   └───────────────┴─────────────────────────┴─────────────┘
 *
 * Plus a collapsed-by-default "raw events" pane at the bottom for the
 * power-user case (jq, bug reports). Both live SSE streams and persisted
 * /events endpoints feed the same shape, so this component does not care
 * which source the events came from -- it just renders.
 */

import { Activity, Brain, ChevronDown, ChevronRight, Wrench, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

// Mirrors the SSE event envelope (see routers/agents.py:_sse) and the
// persisted JSONL shape (server/agents_store.py:append_events).
export interface AgentEvent {
  event: string; // "init" | "stdout" | "stderr" | "done"
  data: Record<string, unknown>;
  // ``seq`` is set by RunBroadcaster on the server; ``at`` (ms-since-start)
  // is set by the live consumer for relative timestamps. Persisted events
  // have seq but not at -- we fall back to index-based ordering.
  seq?: number;
  at?: number;
}

// One UI-level step distilled from the raw event stream. Closer to what
// users want to see than the underlying claude protocol noise: every
// thinking block, every tool call, every assistant message is one row.
interface UiStep {
  id: string;
  kind: "session" | "thinking" | "text" | "tool" | "result";
  title: string;
  subtitle?: string;
  status?: "ok" | "error" | "running";
  // Index into the raw events list -- used when the user clicks a row to
  // jump to the underlying event in the "raw" pane.
  eventIndex: number;
  // Optional preformatted detail body shown in the middle pane on click.
  detail?: ReactNode;
  // For tool steps: the tool name (used in the right-side per-tool counts).
  toolName?: string;
  // Relative seconds since start (for the timestamp gutter).
  ts?: number;
  // For tool calls: a paired tool_result so clicking the step shows both
  // input and output without forcing the user to find the matching row.
  toolResult?: { isError: boolean; text: string };
}

// Per-run summary as built by ``server/pricing.py:build_run_summary`` and
// exposed via /agents/{id}/runs/{run_id}.summary. Optional fields handle
// the live-streaming case where the summary isn't computed yet.
export interface RunSummary {
  model?: string | null;
  model_recognized?: boolean;
  tokens?: {
    input: number;
    output: number;
    cache_create: number;
    cache_read: number;
    total: number;
  };
  rate_per_mtok?: { input: number; output: number; cache_create: number; cache_read: number };
  cost_usd?: {
    input: number;
    output: number;
    cache_create: number;
    cache_read: number;
    total: number;
    source?: string;
  };
  tools?: { count: number; by_tool: Record<string, number>; errors: number };
  events_count?: number;
}

/** Build a UI-level step list from the raw event stream. */
function distillSteps(events: AgentEvent[]): UiStep[] {
  const steps: UiStep[] = [];
  // First pass: find tool_use → tool_result pairs by tool_use_id so the
  // step list can show input + output together. Walks events once; if a
  // tool_result is missing (run still in progress) the tool step renders
  // with status "running" and no result body.
  const toolUseById: Record<string, { stepIdx: number; input: unknown }> = {};

  events.forEach((evt, idx) => {
    const data = evt.data ?? {};
    const ts = typeof evt.at === "number" ? Math.round(evt.at / 100) / 10 : undefined;

    if (evt.event === "init") {
      // Server-emitted init carries task id / action_type. Worth one row
      // so the user sees the run actually started, but kept compact.
      steps.push({
        id: `${idx}-init`,
        kind: "session",
        title: "Run started",
        subtitle: typeof data.action_type === "string" ? `(${data.action_type})` : undefined,
        eventIndex: idx,
        ts,
      });
      return;
    }

    if (evt.event === "stderr") {
      // stderr is noisy and usually structural ("Loading config…", "Read 12
      // tools"). Keep it out of the step list; the raw pane has it.
      return;
    }

    if (evt.event === "done") {
      const status = (data.status as string | undefined) ?? "ok";
      steps.push({
        id: `${idx}-done`,
        kind: "result",
        title: status === "ok" ? "Run finished" : `Run failed: ${data.error ?? "?"}`,
        subtitle:
          typeof data.elapsed_seconds === "number"
            ? `${data.elapsed_seconds}s`
            : typeof data.exit_code === "number"
              ? `exit ${data.exit_code}`
              : undefined,
        status: status === "ok" ? "ok" : "error",
        eventIndex: idx,
        ts,
      });
      return;
    }

    // Claude stream-json frames carried inside stdout events.
    const claudeType = typeof data.type === "string" ? data.type : null;
    if (claudeType === "system" && data.subtype === "init") {
      const tools = (data.tools as unknown[] | undefined)?.length ?? 0;
      steps.push({
        id: `${idx}-system-init`,
        kind: "session",
        title: "Session initialized",
        subtitle: `model ${String(data.model ?? "?")} · ${tools} tools available`,
        eventIndex: idx,
        ts,
      });
      return;
    }
    if (claudeType === "assistant") {
      const msg = (data.message as Record<string, unknown> | undefined) ?? {};
      const content = (msg.content as Array<Record<string, unknown>> | undefined) ?? [];
      content.forEach((block, blockIdx) => {
        const blockId = `${idx}-${blockIdx}`;
        if (block.type === "thinking") {
          const text = String(block.thinking ?? "");
          steps.push({
            id: `${blockId}-think`,
            kind: "thinking",
            title: text.split("\n")[0]?.slice(0, 80) || "(thinking)",
            subtitle: text.length > 80 ? `${text.length} chars` : undefined,
            eventIndex: idx,
            ts,
            detail: <pre className="whitespace-pre-wrap text-xs">{text}</pre>,
          });
        } else if (block.type === "text") {
          const text = String(block.text ?? "");
          steps.push({
            id: `${blockId}-text`,
            kind: "text",
            title: text.split("\n")[0]?.slice(0, 80) || "(empty)",
            subtitle: text.length > 80 ? `${text.length} chars` : undefined,
            eventIndex: idx,
            ts,
            detail: <pre className="whitespace-pre-wrap text-xs">{text}</pre>,
          });
        } else if (block.type === "tool_use") {
          const name = String(block.name ?? "?");
          const input = block.input ?? {};
          const stepIdx = steps.length;
          steps.push({
            id: `${blockId}-tool`,
            kind: "tool",
            title: name,
            subtitle: summarizeToolInput(name, input),
            status: "running",
            eventIndex: idx,
            toolName: name,
            ts,
            detail: (
              <div className="space-y-2">
                <div>
                  <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
                    input
                  </div>
                  <pre className="nerd-code whitespace-pre-wrap text-xs">
                    {JSON.stringify(input, null, 2)}
                  </pre>
                </div>
              </div>
            ),
          });
          if (typeof block.id === "string") {
            toolUseById[block.id] = { stepIdx, input };
          }
        }
      });
      return;
    }
    if (claudeType === "user") {
      // Tool results land here; pair them with the tool_use step we
      // already added so clicking the step shows both sides.
      const msg = (data.message as Record<string, unknown> | undefined) ?? {};
      const content = (msg.content as Array<Record<string, unknown>> | undefined) ?? [];
      content.forEach((block) => {
        if (block.type !== "tool_result") return;
        const toolUseId = typeof block.tool_use_id === "string" ? block.tool_use_id : null;
        const isError = block.is_error === true;
        const text = Array.isArray(block.content)
          ? (block.content as Array<{ text?: string }>).map((c) => c.text ?? "").join("")
          : String(block.content ?? "");
        if (toolUseId && toolUseById[toolUseId]) {
          const { stepIdx, input } = toolUseById[toolUseId];
          const prev = steps[stepIdx];
          prev.status = isError ? "error" : "ok";
          prev.toolResult = { isError, text };
          prev.detail = (
            <div className="space-y-3">
              <div>
                <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">
                  input
                </div>
                <pre className="nerd-code whitespace-pre-wrap text-xs">
                  {JSON.stringify(input, null, 2)}
                </pre>
              </div>
              <div>
                <div
                  className={`text-[10px] uppercase tracking-wider mb-1 ${
                    isError ? "text-red-500" : "text-zinc-500"
                  }`}
                >
                  output {isError ? "(error)" : ""}
                </div>
                <pre
                  className={`nerd-code whitespace-pre-wrap text-xs ${
                    isError ? "border-red-300 dark:border-red-700/40" : ""
                  }`}
                  style={{ maxHeight: "320px" }}
                >
                  {text}
                </pre>
              </div>
            </div>
          );
        }
      });
    }
  });
  return steps;
}

function summarizeToolInput(name: string, input: unknown): string | undefined {
  if (!input || typeof input !== "object") return undefined;
  const obj = input as Record<string, unknown>;
  // Common tools have a single dominant field worth surfacing inline.
  // Falls through to "{...} N keys" for unknown tools so the row still
  // hints at the payload shape.
  const candidate = (() => {
    if (typeof obj.command === "string") return obj.command;
    if (typeof obj.file_path === "string") return obj.file_path;
    if (typeof obj.url === "string") return obj.url;
    if (typeof obj.query === "string") return obj.query;
    if (typeof obj.pattern === "string") return obj.pattern;
    if (typeof obj.path === "string") return obj.path;
    return null;
  })();
  if (candidate) return candidate.length > 70 ? `${candidate.slice(0, 70)}…` : candidate;
  const keys = Object.keys(obj);
  if (keys.length === 0) return undefined;
  return `${keys.length} ${keys.length === 1 ? "key" : "keys"}`;
}

function fmtUsd(n?: number): string {
  if (n == null || !Number.isFinite(n)) return "$0.00";
  if (n < 0.01) return `$${n.toFixed(4)}`;
  if (n < 1) return `$${n.toFixed(3)}`;
  return `$${n.toFixed(2)}`;
}

function fmtTokens(n?: number): string {
  if (!n) return "0";
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1_000_000).toFixed(2)}M`;
}

interface AgentRunViewProps {
  events: AgentEvent[];
  running?: boolean;
  elapsed?: number;
  // Optional precomputed summary (for replay of persisted runs). When
  // omitted, we display lightweight live counts derived directly from
  // events; this keeps the right pane useful during streaming runs too.
  summary?: RunSummary;
  // For live runs: shows a "cancel" button at the top.
  onCancel?: () => void;
}

export function AgentRunView({
  events,
  running = false,
  elapsed = 0,
  summary,
  onCancel,
}: AgentRunViewProps) {
  const steps = useMemo(() => distillSteps(events), [events]);
  // When live (no summary yet) we synthesize counts from steps so the
  // right pane shows progress instead of zeros until the run finishes.
  const liveSummary = useMemo<RunSummary>(() => {
    const byTool: Record<string, number> = {};
    let count = 0;
    let errors = 0;
    for (const s of steps) {
      if (s.kind === "tool" && s.toolName) {
        byTool[s.toolName] = (byTool[s.toolName] ?? 0) + 1;
        count++;
        if (s.status === "error") errors++;
      }
    }
    return { tools: { count, by_tool: byTool, errors } };
  }, [steps]);
  const effectiveSummary = summary ?? liveSummary;
  // Selection model. ``null`` means "follow the latest step" (live tail);
  // a step id means "user has pinned this row, don't auto-jump". Mirrors
  // the Slack/terminal `tail -f` UX: clicking history pauses the autoscroll
  // but the user can re-attach to live via the "Live tail" affordance.
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const lastStepId = steps[steps.length - 1]?.id ?? null;
  // Snapshot the step count at the moment the user pinned a step, so the
  // "+N new" badge in the live-tail button can show how many steps the
  // user has missed since pinning. Reset to current count on unpin.
  const pinnedAtCountRef = useRef<number | null>(null);
  // If the user is "pinned" (selectedId set), we still treat clicking the
  // CURRENT latest step as an auto-follow signal: it's the same row the
  // tail would have shown anyway, so we drop the pin so the next event
  // jumps the view forward as expected.
  const effectiveSelectedId = selectedId === lastStepId ? null : selectedId;
  const isLiveTail = effectiveSelectedId === null;
  // Reset the pin baseline whenever it changes from "pinned" to "tail" or
  // vice versa, so ``newSinceCount`` measures only the gap since the LAST
  // pin event, not the entire run.
  useEffect(() => {
    if (isLiveTail) {
      pinnedAtCountRef.current = null;
    } else if (pinnedAtCountRef.current === null) {
      pinnedAtCountRef.current = steps.length;
    }
  }, [isLiveTail, steps.length]);
  const newSinceCount =
    !isLiveTail && pinnedAtCountRef.current !== null
      ? Math.max(0, steps.length - pinnedAtCountRef.current)
      : 0;
  const showStep = isLiveTail
    ? steps[steps.length - 1] ?? null
    : steps.find((s) => s.id === selectedId) ?? null;
  const resumeLiveTail = () => setSelectedId(null);

  return (
    <div className="grid grid-cols-12 gap-3 min-h-[24rem]">
      {/* Steps panel */}
      <div className="col-span-4 border border-zinc-200 rounded bg-white dark:border-zinc-800 dark:bg-zinc-900/30 flex flex-col">
        <div className="px-3 py-2 border-b border-zinc-200 dark:border-zinc-800 flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <span
              className={`w-2 h-2 rounded-full shrink-0 ${
                running ? "bg-neon-pink animate-pulse" : "bg-zinc-400 dark:bg-zinc-600"
              }`}
            />
            <span className="text-xs uppercase tracking-wider truncate">
              {running ? `running · ${elapsed}s` : `${steps.length} steps`}
            </span>
          </div>
          <div className="flex items-center gap-1 shrink-0">
            {/* Live-tail re-attach: only shown when user pinned a non-latest
                step DURING a still-running run. Click drops the pin so the
                next event auto-scrolls the view forward (Slack's "↓ Jump
                to present" pattern). */}
            {running && !isLiveTail ? (
              <button
                type="button"
                className="nerd-btn !px-2 !py-0.5 text-[10px] !border-neon-pink/60 !text-neon-pink hover:!bg-neon-pink/10"
                onClick={resumeLiveTail}
                title="Jump back to the most recent step and resume auto-following"
              >
                <ChevronDown className="w-3 h-3 inline mr-1" />
                Live tail
                {newSinceCount > 0 ? (
                  <span className="ml-1 tabular-nums">+{newSinceCount}</span>
                ) : null}
              </button>
            ) : null}
            {running && onCancel ? (
              <button
                type="button"
                className="nerd-btn !px-2 !py-0.5 text-[10px] hover:text-red-500"
                onClick={onCancel}
                title="Stop the run"
              >
                <X className="w-3 h-3 inline mr-1" />
                cancel
              </button>
            ) : null}
          </div>
        </div>
        <StepsList
          steps={steps}
          showStepId={showStep?.id ?? null}
          isLiveTail={isLiveTail}
          running={running}
          onSelect={(id) => setSelectedId(id)}
        />
      </div>

      {/* Detail panel */}
      <div className="col-span-5 border border-zinc-200 rounded bg-white dark:border-zinc-800 dark:bg-zinc-900/30 flex flex-col">
        <div className="px-3 py-2 border-b border-zinc-200 dark:border-zinc-800">
          <div className="text-xs uppercase tracking-wider text-zinc-500">step detail</div>
          {showStep ? (
            <div className="text-sm font-mono mt-0.5 truncate">
              {showStep.title}
              {showStep.subtitle ? (
                <span className="text-zinc-500 ml-2">{showStep.subtitle}</span>
              ) : null}
            </div>
          ) : (
            <div className="text-sm text-zinc-500 mt-0.5">select a step</div>
          )}
        </div>
        <div className="overflow-auto flex-1 p-3" style={{ maxHeight: "calc(100vh - 18rem)" }}>
          {showStep?.detail ?? (
            <div className="text-xs text-zinc-500">No body for this step.</div>
          )}
        </div>
      </div>

      {/* Cost / tokens / tools panel */}
      <div className="col-span-3 space-y-3">
        <CostCard summary={effectiveSummary} running={running} />
        <ToolsCard summary={effectiveSummary} />
      </div>
    </div>
  );
}

/**
 * Scrollable list of timeline steps. Split out so we can hold a ref on the
 * scroll container and run a sticky-bottom auto-scroll effect: when the
 * caller is in "live tail" mode (``isLiveTail`` true), every new step
 * scrolls the latest row into view. When the user has pinned a step
 * (``isLiveTail`` false), we leave the scroll position alone so they can
 * read history without the viewport jumping under them.
 */
function StepsList({
  steps,
  showStepId,
  isLiveTail,
  running,
  onSelect,
}: {
  steps: UiStep[];
  showStepId: string | null;
  isLiveTail: boolean;
  running: boolean;
  onSelect: (id: string) => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  // Track the latest step id we auto-scrolled to so the effect doesn't
  // refire on unrelated re-renders. Without this guard, mouse-hover state
  // changes on individual rows would re-trigger scroll math (cheap, but
  // visually wrong: it would also cancel any in-progress user scroll).
  const lastScrolledId = useRef<string | null>(null);
  useEffect(() => {
    if (!isLiveTail) return;
    const last = steps[steps.length - 1];
    if (!last || last.id === lastScrolledId.current) return;
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    lastScrolledId.current = last.id;
  }, [steps, isLiveTail]);
  return (
    <div
      ref={scrollRef}
      className="overflow-auto flex-1"
      style={{ maxHeight: "calc(100vh - 18rem)" }}
    >
      {steps.length === 0 ? (
        <div className="px-3 py-6 text-xs text-zinc-500 text-center">
          {running ? "waiting for first event…" : "no steps recorded"}
        </div>
      ) : (
        <ul className="divide-y divide-zinc-100 dark:divide-zinc-900">
          {steps.map((step) => (
            <li key={step.id}>
              <button
                type="button"
                onClick={() => onSelect(step.id)}
                className={`w-full text-left px-3 py-2 flex items-start gap-2 hover:bg-zinc-50 dark:hover:bg-zinc-900/50 ${
                  showStepId === step.id
                    ? "bg-zinc-50 dark:bg-zinc-900/60 border-l-2 border-l-neon-pink"
                    : "border-l-2 border-l-transparent"
                }`}
              >
                <StepIcon kind={step.kind} status={step.status} />
                <div className="flex-1 min-w-0">
                  <div className="text-xs flex items-center gap-1">
                    {step.kind === "tool" ? (
                      <span className="font-mono text-keboola">{step.title}</span>
                    ) : step.kind === "thinking" ? (
                      <span className="text-zinc-700 dark:text-zinc-300 italic">
                        {step.title}
                      </span>
                    ) : step.kind === "result" ? (
                      <span
                        className={`font-bold ${
                          step.status === "ok"
                            ? "text-keboola"
                            : "text-red-600 dark:text-red-400"
                        }`}
                      >
                        {step.title}
                      </span>
                    ) : (
                      <span className="text-zinc-700 dark:text-zinc-300">{step.title}</span>
                    )}
                  </div>
                  {step.subtitle ? (
                    <div className="text-[10px] text-zinc-500 truncate">{step.subtitle}</div>
                  ) : null}
                </div>
                {step.ts != null ? (
                  <span className="text-[10px] text-zinc-400 dark:text-zinc-600 font-mono shrink-0">
                    +{step.ts}s
                  </span>
                ) : null}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function StepIcon({ kind, status }: { kind: UiStep["kind"]; status?: UiStep["status"] }) {
  if (kind === "thinking") return <Brain className="w-3.5 h-3.5 text-neon-pink shrink-0 mt-0.5" />;
  if (kind === "tool") {
    const color =
      status === "error"
        ? "text-red-500"
        : status === "running"
          ? "text-neon-pink animate-pulse"
          : "text-keboola";
    return <Wrench className={`w-3.5 h-3.5 shrink-0 mt-0.5 ${color}`} />;
  }
  if (kind === "session") return <ChevronRight className="w-3.5 h-3.5 text-zinc-500 shrink-0 mt-0.5" />;
  if (kind === "result")
    return <Activity className={`w-3.5 h-3.5 shrink-0 mt-0.5 ${status === "ok" ? "text-keboola" : "text-red-500"}`} />;
  return <ChevronRight className="w-3.5 h-3.5 text-zinc-400 shrink-0 mt-0.5" />;
}

function CostCard({ summary, running }: { summary: RunSummary; running: boolean }) {
  const tokens = summary.tokens;
  const cost = summary.cost_usd;
  const model = summary.model ?? "—";
  const approx = summary.model_recognized === false;
  return (
    <div className="border border-zinc-200 rounded bg-white dark:border-zinc-800 dark:bg-zinc-900/30">
      <div className="px-3 py-2 border-b border-zinc-200 dark:border-zinc-800 flex items-center justify-between">
        <div className="text-xs uppercase tracking-wider text-zinc-500">cost</div>
        {cost?.source === "claude_result" ? (
          <span className="text-[9px] uppercase text-keboola border border-keboola/40 px-1 rounded">
            authoritative
          </span>
        ) : approx ? (
          <span className="text-[9px] uppercase text-amber-600 border border-amber-400/60 px-1 rounded dark:text-neon-amber dark:border-neon-amber/40">
            approx
          </span>
        ) : null}
      </div>
      <div className="px-3 py-3">
        <div className="text-2xl font-bold text-keboola tabular-nums">
          {cost ? fmtUsd(cost.total) : running ? "…" : "—"}
        </div>
        <div className="text-[10px] text-zinc-500 mt-0.5 truncate" title={model}>
          {model}
        </div>
        {tokens ? (
          <div className="mt-3 space-y-1 text-[11px]">
            <Row label="input" value={fmtTokens(tokens.input)} cost={cost?.input} />
            <Row label="output" value={fmtTokens(tokens.output)} cost={cost?.output} />
            {tokens.cache_create > 0 ? (
              <Row label="cache write" value={fmtTokens(tokens.cache_create)} cost={cost?.cache_create} />
            ) : null}
            {tokens.cache_read > 0 ? (
              <Row label="cache read" value={fmtTokens(tokens.cache_read)} cost={cost?.cache_read} hint="saved $$" />
            ) : null}
            <div className="pt-1 mt-1 border-t border-zinc-100 dark:border-zinc-800 flex justify-between font-bold">
              <span className="text-zinc-500">total</span>
              <span className="tabular-nums">{fmtTokens(tokens.total)} tok</span>
            </div>
          </div>
        ) : (
          <div className="text-[10px] text-zinc-500 mt-3">
            {running ? "tokens reported on each turn…" : "no token data"}
          </div>
        )}
      </div>
    </div>
  );
}

function Row({
  label,
  value,
  cost,
  hint,
}: {
  label: string;
  value: string;
  cost?: number;
  hint?: string;
}) {
  return (
    <div className="flex justify-between items-baseline">
      <span className="text-zinc-500">{label}</span>
      <span className="tabular-nums text-zinc-700 dark:text-zinc-300">
        {value}{" "}
        {cost != null ? (
          <span className="text-zinc-400 dark:text-zinc-600 ml-1">{fmtUsd(cost)}</span>
        ) : null}
        {hint ? (
          <span className="text-keboola ml-1 text-[9px] uppercase">{hint}</span>
        ) : null}
      </span>
    </div>
  );
}

function ToolsCard({ summary }: { summary: RunSummary }) {
  const tools = summary.tools;
  if (!tools) return null;
  const entries = Object.entries(tools.by_tool).sort(([, a], [, b]) => b - a);
  return (
    <div className="border border-zinc-200 rounded bg-white dark:border-zinc-800 dark:bg-zinc-900/30">
      <div className="px-3 py-2 border-b border-zinc-200 dark:border-zinc-800 flex items-center justify-between">
        <div className="text-xs uppercase tracking-wider text-zinc-500">tools used</div>
        <div className="text-[10px] text-zinc-500">
          {tools.count} call{tools.count === 1 ? "" : "s"}
          {tools.errors > 0 ? (
            <span className="text-red-500 ml-2">{tools.errors} err</span>
          ) : null}
        </div>
      </div>
      <div className="px-3 py-2 space-y-1 text-xs">
        {entries.length === 0 ? (
          <div className="text-zinc-500">No tool calls yet.</div>
        ) : (
          entries.map(([name, count]) => (
            <div key={name} className="flex justify-between">
              <span className="font-mono text-zinc-700 dark:text-zinc-300">{name}</span>
              <span className="tabular-nums text-zinc-500">×{count}</span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

/**
 * Collapsed-by-default raw events view. Power-user fallback: full JSONL,
 * one event per line, copy-paste-friendly.
 */
export function AgentRunRaw({ events }: { events: AgentEvent[] }) {
  const [open, setOpen] = useState(false);
  return (
    <details
      open={open}
      onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}
      className="border border-zinc-200 rounded bg-white dark:border-zinc-800 dark:bg-zinc-900/30"
    >
      <summary className="px-3 py-2 cursor-pointer text-xs uppercase tracking-wider text-zinc-500 select-none">
        raw events ({events.length})
      </summary>
      {open ? (
        <pre
          className="nerd-code whitespace-pre overflow-auto text-[10px]"
          style={{ maxHeight: "320px" }}
        >
          {events
            .map((e) => {
              const ts = typeof e.at === "number" ? `+${(e.at / 1000).toFixed(1)}s ` : "";
              return `${ts}[${e.event}] ${JSON.stringify(e.data)}`;
            })
            .join("\n")}
        </pre>
      ) : null}
    </details>
  );
}
