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

import {
  Activity,
  Brain,
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  Download,
  FileText,
  Wrench,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// GFM = GitHub Flavored Markdown. Without this plugin react-markdown only
// implements CommonMark, which omits pipe-table syntax, strikethrough,
// task lists, and autolinks. Agent reports rely heavily on tables (one
// row per finding), so this is non-optional. Declared once at module
// scope so React doesn't see a fresh array identity on every render and
// re-instantiate the markdown processor.
const MARKDOWN_PLUGINS = [remarkGfm];

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
  // Plain-text payload of the step body, used by Copy / Download .md actions
  // and by the Artifacts tab heuristic. Distinct from ``detail`` (a React
  // node) because clipboard/file exports want raw text without the JSX
  // wrappers. Set for thinking, text, and tool_result steps; absent on
  // structural rows (session init, run finished) which have no body.
  rawText?: string;
}

/**
 * A step body that looks like a long-form report worth surfacing as a
 * standalone artifact (Artifacts tab).
 *
 * Originally this required a heading in the first 8 lines, which gave too
 * many false negatives: agents often wrap their report in an Insight or
 * preamble block before the actual ``# Title``. Switched to a multi-signal
 * scoring model: each markdown feature contributes one point, and the
 * threshold is 2.
 *
 * Signals (each worth 1 point):
 *   • Heading anywhere (``#``..``####`` at line start)
 *   • Markdown pipe-table (≥ 2 lines with ``| col | col |`` shape)
 *   • Bullet / numbered list (≥ 3 items)
 *   • Bold spans (``**…**``, ≥ 2 occurrences)
 *   • Substantial length (≥ 1000 chars) — long-form bodies are usually
 *     reports even when the markdown markup is sparse
 *
 * Two-point threshold rejects both:
 *   • Short chat replies that happen to contain one ``#`` heading
 *   • Long but markup-free conversational answers
 *
 * The hard 500-char floor is kept as an early-exit so we don't bother
 * scanning the text for sub-paragraph bodies that obviously aren't worth
 * a standalone artifact card.
 */
const ARTIFACT_MIN_CHARS = 500;
const ARTIFACT_SCORE_THRESHOLD = 2;
function looksLikeMarkdownArtifact(text: string): boolean {
  if (!text || text.length < ARTIFACT_MIN_CHARS) return false;
  let score = 0;
  // Heading (h1..h4) anywhere in the body. ``\s{0,3}`` allows the small
  // amount of leading whitespace that ATX-style headings tolerate.
  if (/^\s{0,3}#{1,4}\s+\S/m.test(text)) score++;
  // Pipe-table: at least 2 lines that look like ``| col | col |``. One
  // line could be coincidental ASCII art; two strongly imply a table.
  const tableLines = text.match(/^[^\n|]*\|[^\n|]*\|[^\n]*$/gm);
  if (tableLines && tableLines.length >= 2) score++;
  // Bullet or ordered list. 3+ items because a single dash could be a
  // hyphenated word in a sentence; 3 in a row is unmistakably a list.
  const listItems = text.match(/^(?:[ \t]*[-*+]\s|\s*\d+\.\s)\S/gm);
  if (listItems && listItems.length >= 3) score++;
  // Bold spans. 2+ to avoid catching a single emphatic word in chat.
  const boldMatches = text.match(/\*\*[^*\n]{2,}?\*\*/g);
  if (boldMatches && boldMatches.length >= 2) score++;
  // Length signal: bodies that long are reports even if they happen to be
  // markup-light (e.g. a Q&A answer with a single inline table).
  if (text.length >= 1000) score++;
  return score >= ARTIFACT_SCORE_THRESHOLD;
}

export interface RunArtifact {
  /** UiStep id; lets the user jump from Artifacts back to the timeline row. */
  stepId: string;
  /** Best-effort title: first ``# `` heading, falling back to step title. */
  title: string;
  /** Raw markdown content. */
  content: string;
  /** Step kind so the badge can colour it (thinking vs text vs tool_result). */
  sourceKind: UiStep["kind"];
  /** Relative seconds since run start, if available. */
  ts?: number;
  sizeChars: number;
}

/** Walk the distilled steps once and pull every markdown-report-like body. */
export function extractArtifacts(steps: UiStep[]): RunArtifact[] {
  const out: RunArtifact[] = [];
  for (const step of steps) {
    const text = step.rawText;
    if (!text || !looksLikeMarkdownArtifact(text)) continue;
    const firstHeading =
      text.match(/^\s{0,3}#{1,4}\s+(.+)$/m)?.[1]?.trim() ?? step.title;
    out.push({
      stepId: step.id,
      title: firstHeading.slice(0, 120),
      content: text,
      sourceKind: step.kind,
      ts: step.ts,
      sizeChars: text.length,
    });
  }
  return out;
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

/**
 * Render the body of a step. Auto-detects long-form markdown reports
 * (those that ``looksLikeMarkdownArtifact`` recognises) and renders them
 * with ``react-markdown`` so headings, lists, and code blocks are styled
 * the way the agent intended. Short bodies, chat snippets, and tool output
 * fall back to a monospace ``<pre>`` so we don't surprise users by
 * reformatting their plain text.
 */
function StepBody({ text }: { text: string }) {
  const isReport = looksLikeMarkdownArtifact(text);
  if (!isReport) {
    return <pre className="whitespace-pre-wrap text-xs">{text}</pre>;
  }
  return (
    <div className="markdown-body text-xs">
      <ReactMarkdown remarkPlugins={MARKDOWN_PLUGINS}>{text}</ReactMarkdown>
    </div>
  );
}

/**
 * Trigger a browser download of ``content`` as a file named ``filename``.
 *
 * We use the Blob + URL.createObjectURL + anchor-click pattern rather than
 * a server-side download endpoint: the markdown body is already in the
 * user's browser (we received it via SSE / JSONL replay), so a server
 * round-trip would only add latency and re-authentication risk. The blob
 * URL is revoked after the click so the GC can reclaim it.
 */
function downloadAsFile(content: string, filename: string, mime = "text/markdown"): void {
  const blob = new Blob([content], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  // setTimeout(0) so the browser has a tick to start the download before
  // the URL becomes invalid. Revoking synchronously cancels the download
  // on Firefox <= 128.
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

/**
 * Build a filesystem-safe filename for a downloaded step body. The default
 * filename has to convey three things at once: which run it came from,
 * which step inside the run, and that it's markdown. ``stepId`` already
 * encodes both run-step position; we just sanitize it.
 */
function safeFilename(prefix: string, raw: string, ext: string): string {
  const cleaned = raw.replace(/[^a-zA-Z0-9._-]+/g, "-").slice(0, 60).replace(/^-+|-+$/g, "");
  return `${prefix}-${cleaned || "step"}.${ext}`;
}

/**
 * Two-button affordance shown above any step body that has a ``rawText``
 * payload: copy the body to clipboard, or download it as a ``.md`` file.
 *
 * Why both: in our usage the same artifact frequently has two downstream
 * destinations -- Slack/email (copy), or a local notes folder / Notion
 * (download). Forcing the user to pick one in a single button hides the
 * second route, so we expose both. The "Copied!" affordance is local
 * state with a short timeout because the codebase has no shared toast
 * system and one would be over-engineering for a single feature.
 */
function CopyDownloadButtons({
  text,
  filenameStem,
  className,
}: {
  text: string;
  filenameStem: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);
  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch (err) {
      // Browsers may reject clipboard in insecure contexts (no https,
      // no user gesture). Fall back to the download path so the user
      // still gets the content out of the UI.
      console.warn("clipboard write rejected, falling back to download", err);
      downloadAsFile(text, safeFilename("kbagent", filenameStem, "md"));
    }
  };
  return (
    <div className={`flex items-center gap-1 ${className ?? ""}`}>
      <button
        type="button"
        onClick={onCopy}
        className="nerd-btn !px-2 !py-0.5 text-[10px] inline-flex items-center gap-1"
        title="Copy markdown to clipboard"
      >
        {copied ? (
          <>
            <Check className="w-3 h-3 text-keboola" />
            <span className="text-keboola">Copied</span>
          </>
        ) : (
          <>
            <Copy className="w-3 h-3" />
            <span>Copy</span>
          </>
        )}
      </button>
      <button
        type="button"
        onClick={() => downloadAsFile(text, safeFilename("kbagent", filenameStem, "md"))}
        className="nerd-btn !px-2 !py-0.5 text-[10px] inline-flex items-center gap-1"
        title="Download as Markdown file"
      >
        <Download className="w-3 h-3" />
        <span>.md</span>
      </button>
    </div>
  );
}

/**
 * Full-screen overlay for reading one artifact at its natural width. The
 * timeline detail pane is fixed at ~5 of 12 columns which works for chat
 * snippets but truncates long-form reports (this is exactly why the
 * Artifacts tab exists). The viewer is opened from an Artifacts card and
 * closes on Escape / backdrop click. Clipboard + download actions ride
 * along so the user doesn't have to close the modal first.
 */
function MarkdownViewerModal({
  artifact,
  onClose,
  onJumpToStep,
}: {
  artifact: RunArtifact;
  onClose: () => void;
  onJumpToStep?: (stepId: string) => void;
}) {
  useEffect(() => {
    // Esc handler runs in the CAPTURE phase and calls
    // ``stopImmediatePropagation`` so the parent ``Drawer`` (which also
    // listens for Esc on ``window``) does NOT also close itself. Without
    // this, pressing Esc inside the artifact modal would close both
    // layers and dump the user back to the agent-task overview instead
    // of the run detail they opened the artifact from.
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopImmediatePropagation();
        onClose();
      }
    };
    window.addEventListener("keydown", handler, { capture: true });
    return () => window.removeEventListener("keydown", handler, { capture: true });
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-[60] bg-zinc-900/70 backdrop-blur-sm flex items-center justify-center p-4"
      onClick={onClose}
    >
      <div
        // ``max-w-[90vw]`` so tables and code blocks have room to breathe
        // (VSCode preview style). ``max-h-[92vh]`` leaves a sliver of the
        // backdrop visible so the user always sees there is an outside-the
        // -modal area to click for dismissal.
        style={{ maxWidth: "90vw", maxHeight: "92vh" }}
        className="bg-white dark:bg-zinc-950 border border-zinc-200 dark:border-zinc-800 rounded shadow-2xl w-full flex flex-col"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-4 py-3 border-b border-zinc-200 dark:border-zinc-800 flex items-center justify-between gap-3">
          <div className="min-w-0 flex-1">
            <div className="text-[10px] uppercase tracking-wider text-zinc-500">artifact</div>
            <div className="text-sm font-mono truncate">{artifact.title}</div>
          </div>
          <CopyDownloadButtons
            text={artifact.content}
            filenameStem={artifact.title}
            className="shrink-0"
          />
          {onJumpToStep ? (
            <button
              type="button"
              onClick={() => {
                onJumpToStep(artifact.stepId);
                onClose();
              }}
              className="nerd-btn !px-2 !py-0.5 text-[10px] shrink-0"
              title="Show the underlying step in the timeline"
            >
              <ChevronRight className="w-3 h-3 inline mr-0.5" />
              View step
            </button>
          ) : null}
          <button
            type="button"
            onClick={onClose}
            className="nerd-btn !px-2 !py-0.5 text-[10px] shrink-0"
            title="Close (Esc)"
          >
            <X className="w-3 h-3" />
          </button>
        </div>
        <div className="overflow-auto flex-1 px-8 py-6">
          {/* ``markdown-body-lg`` upgrades typography to VSCode-preview
              register (16px body, larger headings, more padding in tables).
              ``max-w-3xl mx-auto`` constrains line length even though the
              modal itself is wide: long lines of body text become harder
              to read past ~80ch / ~720px, so we centre the column the way
              GitHub renders README files inside a wide repo page. */}
          <div className="markdown-body markdown-body-lg max-w-3xl mx-auto">
            <ReactMarkdown remarkPlugins={MARKDOWN_PLUGINS}>{artifact.content}</ReactMarkdown>
          </div>
        </div>
      </div>
    </div>
  );
}

/**
 * Grid of artifact cards shown when the user picks the Artifacts tab. Each
 * card is a quick summary: title, source kind badge, size, timestamp,
 * preview of the first non-heading paragraph. Clicking the card opens the
 * full-screen viewer; the per-card Copy/Download buttons let the user
 * export without opening.
 */
function ArtifactsView({
  artifacts,
  onJumpToStep,
}: {
  artifacts: RunArtifact[];
  onJumpToStep?: (stepId: string) => void;
}) {
  const [openId, setOpenId] = useState<string | null>(null);
  const openArtifact = artifacts.find((a) => a.stepId === openId) ?? null;

  if (artifacts.length === 0) {
    return (
      <div className="border border-zinc-200 rounded bg-white dark:border-zinc-800 dark:bg-zinc-900/30 px-6 py-10 text-center">
        <FileText className="w-8 h-8 mx-auto text-zinc-400 dark:text-zinc-600 mb-2" />
        <div className="text-sm text-zinc-600 dark:text-zinc-400">
          No markdown reports detected in this run.
        </div>
        <div className="text-[11px] text-zinc-500 mt-1">
          Artifacts are step bodies ≥ {ARTIFACT_MIN_CHARS} chars that start with a heading
          (<code className="font-mono">#</code> or <code className="font-mono">##</code>).
        </div>
      </div>
    );
  }

  return (
    <>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {artifacts.map((a) => (
          <ArtifactCard
            key={a.stepId}
            artifact={a}
            onOpen={() => setOpenId(a.stepId)}
          />
        ))}
      </div>
      {openArtifact ? (
        <MarkdownViewerModal
          artifact={openArtifact}
          onClose={() => setOpenId(null)}
          onJumpToStep={onJumpToStep}
        />
      ) : null}
    </>
  );
}

function ArtifactCard({
  artifact,
  onOpen,
}: {
  artifact: RunArtifact;
  onOpen: () => void;
}) {
  // First non-heading paragraph (first ~200 chars). Skip leading blanks
  // and any line starting with ``#`` so the preview is the actual body
  // text, not a duplicate of the title.
  const preview =
    artifact.content
      .split("\n")
      .filter((line) => line.trim().length > 0 && !line.trim().startsWith("#"))
      .join(" ")
      .slice(0, 200) || "(no preview)";

  return (
    <div className="border border-zinc-200 rounded bg-white dark:border-zinc-800 dark:bg-zinc-900/30 flex flex-col">
      <div className="px-3 py-2 border-b border-zinc-200 dark:border-zinc-800 flex items-start gap-2">
        <FileText className="w-4 h-4 text-keboola shrink-0 mt-0.5" />
        <div className="flex-1 min-w-0">
          <button
            type="button"
            onClick={onOpen}
            className="text-sm font-medium text-left hover:text-keboola truncate block w-full"
            title={artifact.title}
          >
            {artifact.title}
          </button>
          <div className="text-[10px] text-zinc-500 mt-0.5 flex items-center gap-2">
            <span className="uppercase tracking-wider">{artifact.sourceKind}</span>
            <span>·</span>
            <span className="tabular-nums">{artifact.sizeChars.toLocaleString()} ch</span>
            {artifact.ts != null ? (
              <>
                <span>·</span>
                <span className="font-mono tabular-nums">+{artifact.ts}s</span>
              </>
            ) : null}
          </div>
        </div>
      </div>
      <div className="px-3 py-2 text-[11px] text-zinc-600 dark:text-zinc-400 line-clamp-3 flex-1">
        {preview}
      </div>
      <div className="px-3 py-2 border-t border-zinc-100 dark:border-zinc-900 flex items-center justify-between gap-2">
        <button
          type="button"
          onClick={onOpen}
          className="nerd-btn !px-2 !py-0.5 text-[10px]"
        >
          Open
        </button>
        <CopyDownloadButtons text={artifact.content} filenameStem={artifact.title} />
      </div>
    </div>
  );
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
            rawText: text,
            detail: <StepBody text={text} />,
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
            rawText: text,
            detail: <StepBody text={text} />,
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
          // Expose the tool output as ``rawText`` so Copy / Download / the
          // Artifacts heuristic can reach it. Bash steps that produce long
          // markdown reports (e.g. ``kbagent`` summaries) are the main case.
          prev.rawText = text;
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
  const artifacts = useMemo(() => extractArtifacts(steps), [steps]);
  const [view, setView] = useState<"timeline" | "artifacts">("timeline");
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

  // Click handler: when the user jumps from Artifacts back to the timeline
  // we want them to land directly on the step that produced the artifact,
  // not at "live tail" (which would scroll to the latest event and hide
  // the artifact body they just clicked).
  const handleJumpToStep = (stepId: string) => {
    setSelectedId(stepId);
    setView("timeline");
  };

  return (
    <div className="space-y-3">
      {/* Tab bar: Timeline (3-panel) vs Artifacts (markdown reports). The
          Artifacts tab is hidden until the run produces at least one
          report — most short interactive runs have none, so the chrome
          would just be visual noise. */}
      {artifacts.length > 0 ? (
        <div className="flex items-center gap-2 border-b border-zinc-200 dark:border-zinc-800">
          <button
            type="button"
            onClick={() => setView("timeline")}
            className={`px-3 py-1.5 text-xs uppercase tracking-wider border-b-2 -mb-px ${
              view === "timeline"
                ? "border-keboola text-keboola"
                : "border-transparent text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300"
            }`}
          >
            Timeline
          </button>
          <button
            type="button"
            onClick={() => setView("artifacts")}
            className={`px-3 py-1.5 text-xs uppercase tracking-wider border-b-2 -mb-px inline-flex items-center gap-1.5 ${
              view === "artifacts"
                ? "border-keboola text-keboola"
                : "border-transparent text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300"
            }`}
          >
            <FileText className="w-3 h-3" />
            Artifacts
            <span className="tabular-nums bg-keboola/10 text-keboola px-1.5 rounded">
              {artifacts.length}
            </span>
          </button>
        </div>
      ) : null}

      {view === "artifacts" ? (
        <ArtifactsView artifacts={artifacts} onJumpToStep={handleJumpToStep} />
      ) : (
        <TimelineGrid
          steps={steps}
          showStep={showStep}
          selectedId={selectedId}
          setSelectedId={setSelectedId}
          isLiveTail={isLiveTail}
          newSinceCount={newSinceCount}
          running={running}
          elapsed={elapsed}
          effectiveSummary={effectiveSummary}
          onCancel={onCancel}
          resumeLiveTail={resumeLiveTail}
        />
      )}
    </div>
  );
}

/**
 * The original three-panel layout (Steps · Detail · Cost/Tools), extracted
 * so the top-level ``AgentRunView`` can switch between this and the
 * Artifacts view without doubling the JSX tree.
 */
function TimelineGrid({
  steps,
  showStep,
  selectedId,
  setSelectedId,
  isLiveTail,
  newSinceCount,
  running,
  elapsed,
  effectiveSummary,
  onCancel,
  resumeLiveTail,
}: {
  steps: UiStep[];
  showStep: UiStep | null;
  selectedId: string | null;
  setSelectedId: (id: string) => void;
  isLiveTail: boolean;
  newSinceCount: number;
  running: boolean;
  elapsed: number;
  effectiveSummary: RunSummary;
  onCancel?: () => void;
  resumeLiveTail: () => void;
}) {
  // selectedId is unused here directly (StepsList uses showStepId for
  // highlight) but kept in the prop list so the parent owns the state.
  void selectedId;
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
        <div className="px-3 py-2 border-b border-zinc-200 dark:border-zinc-800 flex items-start justify-between gap-2">
          <div className="min-w-0 flex-1">
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
          {/* Per-step actions: Copy + Download .md. Hidden when the step has
              no extractable body (e.g. "Run started" structural rows) so
              the chrome only appears where it does something useful. */}
          {showStep?.rawText ? (
            <CopyDownloadButtons
              text={showStep.rawText}
              filenameStem={showStep.title}
              className="shrink-0"
            />
          ) : null}
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
