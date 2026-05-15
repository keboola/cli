import { Bot, Eraser, Send, Sparkles, User, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ssePost, type SsePostHandle } from "../api/client";
import { ErrorBox, PageTitle } from "../components/Empty";
import { useUIState } from "../state";

/**
 * Local AI chat page (#300).
 *
 * Replaces the per-project Kai tile with a generic chat surface backed by
 * the user's local Claude / Codex / Gemini CLI. The backend
 * (POST /ai/chat/stream) spawns the chosen CLI with a meta-prompt
 * grounding it as a kbagent co-pilot; the same stream_ai_agent_events
 * machinery the workspace SQL helper and agent prompt helper already use.
 *
 * UX: append-only conversation. Each user message starts a new isolated
 * AI invocation (single-shot — history is shown in scrollback but is NOT
 * forwarded to the next request yet). "New conversation" clears scrollback.
 *
 * Why not multi-turn yet? Each subprocess is a fresh CLI session — we'd
 * need to render previous turns into the meta-prompt by hand. That's the
 * v2 follow-up; v1 nails the "ask a Keboola question, get a real answer"
 * flow first.
 */

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  activity?: string[];
  metaPrompt?: string;
  pending?: boolean;
  error?: string;
}

/**
 * AbortError shape detection across browsers (DOMException on standards,
 * named Error on some shims). Duplicated from Workspaces.tsx pending a
 * shared util module.
 */
function isAbortError(err: unknown): boolean {
  if (err instanceof DOMException && err.name === "AbortError") return true;
  if (err instanceof Error && err.name === "AbortError") return true;
  return Boolean(
    err &&
      typeof err === "object" &&
      "message" in err &&
      String((err as { message: unknown }).message).toLowerCase().includes("abort"),
  );
}

export function LocalAiPage() {
  const { project, branchId, pendingLocalAiMessage, setPendingLocalAiMessage } = useUIState();
  const [cli, setCli] = useState<"claude" | "codex" | "gemini">("claude");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [running, setRunning] = useState(false);
  const handleRef = useRef<SsePostHandle | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Hand-off slot from the Dashboard hero (#300): if the user typed a
  // question on /dashboard and clicked Send, the message was dropped
  // into UIState.pendingLocalAiMessage. Read it once, fire send() with
  // the message passed directly (not via input state — setInput is
  // async and the immediate send() would capture stale empty string).
  // Then clear the slot so a remount can't fire it twice.
  useEffect(() => {
    if (!pendingLocalAiMessage) return;
    const msg = pendingLocalAiMessage;
    setPendingLocalAiMessage(null);
    sendRef.current?.(msg);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingLocalAiMessage]);
  const sendRef = useRef<((override?: string) => void) | null>(null);

  // Auto-scroll to bottom on new message / streamed content. We attach
  // to the wrapper's scrollHeight after every render where messages
  // changed. Cheap and avoids the "user scrolled up to read history"
  // edge case (since the user has to deliberately scroll up; new
  // messages otherwise pin to bottom naturally).
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  // Abort any in-flight stream on unmount so the backend doesn't keep
  // spawning subprocesses for a closed connection.
  useEffect(() => {
    return () => {
      if (handleRef.current) {
        handleRef.current.abort();
        handleRef.current = null;
      }
    };
  }, []);

  const send = (messageOverride?: string) => {
    // messageOverride lets the Dashboard hand-off effect bypass the
    // input state (which is async and would be stale on the same tick
    // setInput was called). Manual Send button goes through input.
    const message = (messageOverride ?? input).trim();
    if (!message || running) return;
    setInput("");
    setMessages((prev) => [...prev, { role: "user", content: message }]);
    // Insert a pending assistant placeholder we will mutate via index as
    // events stream in.
    const assistantIdx = messages.length + 1; // user just pushed, this is the next slot
    setMessages((prev) => [
      ...prev,
      { role: "assistant", content: "", pending: true, activity: [] },
    ]);
    setRunning(true);

    let assistantText = "";
    const activity: string[] = [];

    const updateAssistant = (patch: Partial<ChatMessage>) => {
      setMessages((prev) => {
        const next = [...prev];
        if (next[assistantIdx]) {
          next[assistantIdx] = { ...next[assistantIdx], ...patch };
        }
        return next;
      });
    };

    const handle = ssePost(
      "/ai/chat/stream",
      {
        cli,
        message,
        project: project ?? null,
        branch_id: branchId,
      },
      {
        init: (d) => {
          const data = (d ?? {}) as Record<string, unknown>;
          if (typeof data.meta_prompt === "string") {
            updateAssistant({ metaPrompt: data.meta_prompt });
          }
        },
        stdout: (d) => {
          const data = (d ?? {}) as Record<string, unknown>;
          // Claude stream-json: assistant turns + tool_use + tool_result.
          if (data.type === "assistant" && typeof data.message === "object") {
            const msg = data.message as Record<string, unknown>;
            const content = msg.content;
            if (Array.isArray(content)) {
              for (const block of content) {
                if (!block || typeof block !== "object") continue;
                const b = block as Record<string, unknown>;
                if (b.type === "text" && typeof b.text === "string") {
                  assistantText += b.text;
                  updateAssistant({ content: assistantText });
                } else if (b.type === "tool_use") {
                  const name = typeof b.name === "string" ? b.name : "tool";
                  const input = b.input;
                  const args =
                    typeof input === "object" && input !== null
                      ? (() => {
                          const obj = input as Record<string, unknown>;
                          if (typeof obj.command === "string") return obj.command;
                          if (typeof obj.description === "string") return obj.description;
                          return JSON.stringify(obj).slice(0, 200);
                        })()
                      : "";
                  activity.push(`→ ${name}: ${args}`);
                  updateAssistant({ activity: [...activity] });
                }
              }
            }
          } else if (data.type === "user" && typeof data.message === "object") {
            // Tool results — one-line status only.
            const msg = data.message as Record<string, unknown>;
            const content = msg.content;
            if (Array.isArray(content)) {
              for (const block of content) {
                if (!block || typeof block !== "object") continue;
                const b = block as Record<string, unknown>;
                if (b.type === "tool_result") {
                  const isErr = b.is_error === true;
                  activity.push(`  ${isErr ? "✗" : "✓"} tool result${isErr ? " (error)" : ""}`);
                  updateAssistant({ activity: [...activity] });
                }
              }
            }
          } else if (typeof data.raw === "string") {
            // codex / gemini stream raw text lines (no jsonl).
            assistantText += (assistantText ? "\n" : "") + data.raw;
            updateAssistant({ content: assistantText });
          }
        },
        stderr: () => {
          /* progress notes — already covered by Activity panel */
        },
        done: (d) => {
          const data = (d ?? {}) as Record<string, unknown>;
          if (data.status === "error") {
            updateAssistant({
              pending: false,
              error: String(data.error ?? "AI chat failed"),
            });
            return;
          }
          // If the assistant produced no text but the run completed OK
          // (e.g. the AI only ran tools and never wrote a summary), pass
          // the raw final response through. Otherwise leave the
          // streamed text as-is.
          if (!assistantText && typeof data.response === "string") {
            assistantText = data.response;
          }
          updateAssistant({
            pending: false,
            content: assistantText || "(empty response)",
          });
        },
        message: () => {
          /* unknown event — ignore */
        },
      },
    );
    handleRef.current = handle;
    handle.done
      .catch((err) => {
        if (isAbortError(err)) return;
        updateAssistant({ pending: false, error: (err as Error).message });
      })
      .finally(() => {
        setRunning(false);
        handleRef.current = null;
      });
  };
  // Expose send via a ref so the pendingLocalAiMessage effect can call
  // it without re-binding on every render. send itself closes over
  // input + running state which is fine: the override path bypasses
  // both, and the manual path always runs after a user gesture (so
  // state is fresh at click time).
  sendRef.current = send;

  const cancel = () => {
    if (handleRef.current) {
      handleRef.current.abort();
      handleRef.current = null;
    }
    setRunning(false);
    setMessages((prev) => {
      const next = [...prev];
      const last = next[next.length - 1];
      if (last && last.role === "assistant" && last.pending) {
        next[next.length - 1] = {
          ...last,
          pending: false,
          error: "Cancelled by user.",
        };
      }
      return next;
    });
  };

  const clearChat = () => {
    cancel();
    setMessages([]);
  };

  const placeholder = project
    ? `Ask ${cli} about ${project} — e.g. "list jobs that failed in the last 24h"`
    : `Ask ${cli} anything about your Keboola projects — pick one in the top bar, or ask cross-project (use --project NAME)`;

  return (
    <div className="space-y-4 flex flex-col" style={{ height: "calc(100vh - 7rem)" }}>
      <PageTitle
        title="Local AI"
        description={`Chat with your local ${cli} install. It has the kbagent CLI on PATH and can run real Keboola commands to answer questions.`}
        actions={
          <div className="flex items-center gap-2">
            <span className="text-[10px] uppercase tracking-wider text-zinc-500">CLI:</span>
            {(["claude", "codex", "gemini"] as const).map((c) => (
              <button
                key={c}
                type="button"
                className={`nerd-btn text-xs ${cli === c ? "border-keboola text-keboola" : ""}`}
                onClick={() => setCli(c)}
                disabled={running}
              >
                {c}
              </button>
            ))}
            <button
              type="button"
              className="nerd-btn text-xs flex items-center gap-1 hover:text-red-400 hover:border-red-700"
              onClick={clearChat}
              disabled={messages.length === 0}
              title="Clear conversation and start fresh"
            >
              <Eraser className="w-3 h-3" /> new
            </button>
          </div>
        }
      />

      <div
        ref={scrollRef}
        className="flex-1 overflow-y-auto nerd-card space-y-4 min-h-0"
      >
        {messages.length === 0 ? (
          <EmptyState project={project} cli={cli} />
        ) : (
          messages.map((m, i) => <MessageBubble key={i} msg={m} cli={cli} />)
        )}
      </div>

      <form
        className="nerd-card border-keboola/30 bg-white dark:bg-zinc-900/40 flex items-end gap-2 shrink-0"
        onSubmit={(e) => {
          e.preventDefault();
          send();
        }}
      >
        <Sparkles className="w-5 h-5 text-keboola flex-shrink-0 mb-1" />
        <textarea
          className="flex-1 bg-transparent border-0 focus:outline-none text-sm placeholder-zinc-500 dark:placeholder-zinc-600 resize-none"
          rows={2}
          placeholder={placeholder}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            // Cmd/Ctrl+Enter sends; plain Enter inserts newline. Matches
            // the Workspace SQL editor's Run shortcut for muscle memory.
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
              e.preventDefault();
              send();
            }
          }}
          disabled={running}
        />
        {running ? (
          <button
            type="button"
            className="nerd-btn flex items-center gap-1 hover:text-red-400 hover:border-red-700"
            onClick={cancel}
            title="Abort the current response"
          >
            <X className="w-3 h-3" /> cancel
          </button>
        ) : (
          <button
            type="submit"
            className="nerd-btn flex items-center gap-1 hover:text-keboola"
            disabled={!input.trim()}
            title="Send (Cmd/Ctrl + Enter)"
          >
            <Send className="w-3 h-3" /> send
          </button>
        )}
      </form>
    </div>
  );
}

function EmptyState({
  project,
  cli,
}: {
  project: string | null;
  cli: "claude" | "codex" | "gemini";
}) {
  const suggestions = project
    ? [
        `list jobs that failed in ${project} during the last 24 hours`,
        `which buckets in ${project} have the most rows?`,
        `summarise scheduled flows in ${project} that haven't run in 7 days`,
      ]
    : [
        "which of my projects has the most failed jobs in the last week?",
        "list all linked buckets across my projects and where they originate",
        "find any project where doctor reports warnings",
      ];
  return (
    <div className="text-center py-12 space-y-4">
      <div className="text-zinc-400 dark:text-zinc-600">
        <Bot className="w-12 h-12 mx-auto mb-2" />
      </div>
      <p className="text-sm text-zinc-600 dark:text-zinc-400">
        Chat with your local <span className="font-bold text-keboola">{cli}</span>.
        It has <code className="text-xs">kbagent</code> on PATH and runs real commands to answer.
      </p>
      <p className="text-xs text-zinc-500">Try one of these:</p>
      <div className="max-w-xl mx-auto space-y-1 text-left">
        {suggestions.map((s, i) => (
          <div
            key={i}
            className="text-xs text-zinc-500 dark:text-zinc-500 px-3 py-2 rounded border border-zinc-200 dark:border-zinc-900/40 bg-zinc-50 dark:bg-zinc-950/40"
          >
            {s}
          </div>
        ))}
      </div>
    </div>
  );
}

function MessageBubble({
  msg,
  cli,
}: {
  msg: ChatMessage;
  cli: "claude" | "codex" | "gemini";
}) {
  const [showActivity, setShowActivity] = useState(true);
  const [showPrompt, setShowPrompt] = useState(false);

  if (msg.role === "user") {
    return (
      <div className="flex items-start gap-3">
        <div className="w-7 h-7 rounded-full bg-zinc-200 dark:bg-zinc-800 flex items-center justify-center flex-shrink-0">
          <User className="w-3.5 h-3.5 text-zinc-600 dark:text-zinc-400" />
        </div>
        <div className="flex-1 min-w-0 text-sm text-zinc-900 dark:text-zinc-100 whitespace-pre-wrap py-1">
          {msg.content}
        </div>
      </div>
    );
  }

  return (
    <div className="flex items-start gap-3">
      <div className="w-7 h-7 rounded-full bg-keboola/10 border border-keboola/40 flex items-center justify-center flex-shrink-0">
        <Sparkles className="w-3.5 h-3.5 text-keboola" />
      </div>
      <div className="flex-1 min-w-0 space-y-2">
        {/* Transparency: show what context the AI received. Collapsed by
            default; debug aid for "why did the AI answer that way?" */}
        {msg.metaPrompt ? (
          <div className="border border-zinc-200 dark:border-zinc-800 rounded">
            <button
              type="button"
              className="w-full text-left px-2 py-1 text-[10px] uppercase tracking-wider text-zinc-500 hover:text-keboola flex items-center gap-1"
              onClick={() => setShowPrompt((v) => !v)}
            >
              <span>{showPrompt ? "▾" : "▸"}</span>
              <span>Prompt sent to {cli}</span>
              <span className="ml-auto text-zinc-400">{msg.metaPrompt.length} chars</span>
            </button>
            {showPrompt ? (
              <pre
                className="nerd-code whitespace-pre-wrap text-[11px] text-zinc-600 dark:text-zinc-400 border-t border-zinc-200 dark:border-zinc-800"
                style={{ maxHeight: "240px", overflow: "auto" }}
              >
                {msg.metaPrompt}
              </pre>
            ) : null}
          </div>
        ) : null}

        {/* Activity: live tool-use stream while the AI works through
            kbagent commands. Visible by default during the run because
            seeing the AI think is half the value of this surface. */}
        {msg.activity && msg.activity.length > 0 ? (
          <div className="border border-zinc-200 dark:border-zinc-800 rounded">
            <button
              type="button"
              className="w-full text-left px-2 py-1 text-[10px] uppercase tracking-wider text-zinc-500 hover:text-keboola flex items-center gap-1"
              onClick={() => setShowActivity((v) => !v)}
            >
              <span>{showActivity ? "▾" : "▸"}</span>
              <span>Activity</span>
              <span className="ml-auto text-zinc-400">{msg.activity.length} events</span>
            </button>
            {showActivity ? (
              <pre
                className="nerd-code whitespace-pre-wrap text-[11px] text-zinc-600 dark:text-zinc-400 border-t border-zinc-200 dark:border-zinc-800"
                style={{ maxHeight: "200px", overflow: "auto" }}
              >
                {msg.activity.join("\n")}
              </pre>
            ) : null}
          </div>
        ) : null}

        {/* The actual answer, rendered as markdown. While streaming
            (msg.pending) we show a subtle cursor blink to signal
            "still typing". */}
        {msg.error ? (
          <ErrorBox message={msg.error} />
        ) : msg.content ? (
          <div className="prose prose-sm dark:prose-invert max-w-none text-sm">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
            {msg.pending ? <span className="text-keboola animate-pulse">▌</span> : null}
          </div>
        ) : msg.pending ? (
          <div className="text-xs text-zinc-500">thinking…</div>
        ) : null}
      </div>
    </div>
  );
}
