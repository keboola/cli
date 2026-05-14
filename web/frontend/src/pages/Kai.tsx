import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, MessageSquarePlus, RefreshCw, Send } from "lucide-react";
import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "../api/client";
import { ErrorBox, Loading, PageTitle } from "../components/Empty";
import { JsonView } from "../components/JsonView";
import { useUIState } from "../state";

interface Message {
  role: "user" | "assistant";
  content: string;
  meta?: unknown;
  pending?: boolean;
  error?: boolean;
}

interface ChatSummary {
  id: string;
  title: string;
  created_at: string | null;
}

interface PreflightResponse {
  project_alias: string;
  ok: boolean;
  is_master_token: boolean;
  has_agent_chat_feature: boolean;
  token_description: string | null;
  project_id: number | null;
  project_name: string | null;
  error: string | null;
}

interface ChatDetailResponse {
  project_alias: string;
  chat_id: string;
  title: string | null;
  created_at: string | null;
  messages: { id: string; role: string; content: string; created_at: string | null }[];
}

// Per-project active chat ID, persisted across page navigations + refresh.
// Keyed on project so switching projects doesn't load the wrong conversation.
function activeChatKey(project: string | null): string {
  return `kbagent:kai:active-chat:${project ?? "_"}`;
}

function loadActiveChat(project: string | null): string | null {
  try {
    return localStorage.getItem(activeChatKey(project));
  } catch {
    return null;
  }
}

function saveActiveChat(project: string | null, chatId: string | null) {
  try {
    if (chatId) localStorage.setItem(activeChatKey(project), chatId);
    else localStorage.removeItem(activeChatKey(project));
  } catch {
    // localStorage may be disabled (private mode); silent no-op is fine.
  }
}

/**
 * Strip Keboola Connection's `icon:NAME` inline syntax (Font Awesome style)
 * out of Kai responses. Keboola's own UI renders them as actual icons; our
 * markdown view would otherwise show the raw `icon:database` literal next to
 * each heading. We translate common ones to emoji and drop the rest.
 */
const ICON_EMOJI: Record<string, string> = {
  database: "🗄️",
  "circle-exclamation": "⚠️",
  "circle-info": "ℹ️",
  "circle-check": "✅",
  "triangle-exclamation": "⚠️",
  lightbulb: "💡",
  table: "📊",
  "chart-bar": "📊",
  flask: "🧪",
  flow: "🔀",
  "hand-pointer": "👉",
  bolt: "⚡",
  bug: "🐛",
  gear: "⚙️",
};

function normalizeKaiText(text: string): string {
  return text.replace(/icon:([a-z][a-z0-9-]*)\s*/gi, (_match, name: string) => {
    const emoji = ICON_EMOJI[name.toLowerCase()];
    return emoji ? `${emoji} ` : "";
  });
}

export function KaiPage() {
  const { project } = useUIState();
  const qc = useQueryClient();
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [chatId, setChatIdState] = useState<string | null>(null);

  // Setter that also mirrors the value to localStorage so it survives page
  // navigation in the SPA.
  function setChatId(id: string | null) {
    setChatIdState(id);
    saveActiveChat(project, id);
  }

  // On mount / project change, restore the active chat ID from localStorage.
  // The detail-fetch effect below then populates messages from the server.
  useEffect(() => {
    const restored = loadActiveChat(project);
    setChatIdState(restored);
    if (!restored) setMessages([]);
  }, [project]);

  const preflightQ = useQuery<PreflightResponse>({
    queryKey: ["kai-preflight", project],
    queryFn: () => api.get("/kai/preflight", { query: { project: project ?? undefined } }),
    retry: false,
  });

  const pingQ = useQuery({
    queryKey: ["kai-ping", project],
    queryFn: () => api.get("/kai/ping", { query: { project: project ?? undefined } }),
    retry: false,
    // Only attempt /ping once preflight confirms the token is usable —
    // otherwise we'd surface a confusing KAI_NOT_ENABLED error on top of the
    // (more actionable) preflight banner.
    enabled: preflightQ.data?.ok === true,
  });

  const historyQ = useQuery<{ chats: ChatSummary[]; has_more: boolean }>({
    queryKey: ["kai-history", project],
    queryFn: () =>
      api.get("/kai/history", { query: { project: project ?? undefined, limit: 30 } }),
    retry: false,
    enabled: preflightQ.data?.ok === true,
  });

  // When the user clicks a chat in the sidebar, load its full transcript.
  // We restore the conversation into `messages` instead of streaming —
  // `/kai/chat/{id}` already returns the parsed message list.
  const detailQ = useQuery<ChatDetailResponse>({
    queryKey: ["kai-chat-detail", project, chatId],
    queryFn: () =>
      api.get(`/kai/chat/${chatId}`, { query: { project: project ?? undefined } }),
    enabled: preflightQ.data?.ok === true && !!chatId,
    retry: false,
  });

  useEffect(() => {
    if (detailQ.data && detailQ.data.chat_id === chatId) {
      const restored: Message[] = detailQ.data.messages.map((m) => ({
        role: m.role === "user" ? "user" : "assistant",
        content: m.content,
      }));
      setMessages(restored);
    }
  }, [detailQ.data, chatId]);

  const sendMu = useMutation({
    mutationFn: (payload: { message: string; chatId: string | null }) =>
      api.post<{ message?: string; chat_id?: string; response?: string }>("/kai/chat", {
        message: payload.message,
        chat_id: payload.chatId,
        project,
      }),
    onSuccess: (data) => {
      setMessages((m) => {
        const next = [...m];
        for (let i = next.length - 1; i >= 0; i--) {
          if (next[i].role === "assistant" && next[i].pending) {
            next[i] = {
              role: "assistant",
              content: data.response ?? data.message ?? "",
              meta: data,
            };
            return next;
          }
        }
        next.push({
          role: "assistant",
          content: data.response ?? data.message ?? "",
          meta: data,
        });
        return next;
      });
      if (data.chat_id) setChatId(data.chat_id);
      // Refresh the sidebar so newly-created chats / updated titles appear.
      qc.invalidateQueries({ queryKey: ["kai-history", project] });
    },
    onError: (err: unknown) => {
      const msg = err instanceof Error ? err.message : String(err);
      setMessages((m) => {
        const next = [...m];
        for (let i = next.length - 1; i >= 0; i--) {
          if (next[i].role === "assistant" && next[i].pending) {
            next[i] = {
              role: "assistant",
              content: `**Kai error:** ${msg}`,
              error: true,
            };
            return next;
          }
        }
        next.push({ role: "assistant", content: `**Kai error:** ${msg}`, error: true });
        return next;
      });
    },
  });

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const text = input.trim();
    if (!text || sendMu.isPending) return;
    setMessages((m) => [
      ...m,
      { role: "user", content: text },
      { role: "assistant", content: "", pending: true },
    ]);
    setInput("");
    sendMu.mutate({ message: text, chatId });
  }

  function handleNewChat() {
    setChatId(null);
    setMessages([]);
  }

  function handleOpenChat(id: string) {
    if (id === chatId) return;
    setChatId(id);
    setMessages([]); // cleared by detail effect once it lands
  }

  const preflight = preflightQ.data;
  const preflightBlocking = preflight && !preflight.ok;

  return (
    <div className="space-y-4">
      <PageTitle
        title="Kai (Keboola AI)"
        description="Chat with the Keboola AI assistant scoped to your project."
      />
      <PreflightBanner preflight={preflight} isLoading={preflightQ.isLoading} error={preflightQ.error} />
      {!preflightBlocking ? (
        pingQ.error ? (
          <ErrorBox message={`Kai not available: ${(pingQ.error as Error).message}`} />
        ) : pingQ.isLoading ? (
          <Loading label="connecting to Kai..." />
        ) : pingQ.data ? (
          <div className="nerd-pill-green">connected as {project}</div>
        ) : null
      ) : null}

      <div className="grid grid-cols-1 md:grid-cols-[260px_1fr] gap-4">
        <ChatHistorySidebar
          chats={historyQ.data?.chats ?? []}
          isLoading={historyQ.isLoading}
          error={historyQ.error}
          activeChatId={chatId}
          onOpen={handleOpenChat}
          onNew={handleNewChat}
          onRefresh={() => qc.invalidateQueries({ queryKey: ["kai-history", project] })}
        />

        <div className="space-y-3">
          <div className="nerd-card space-y-3" style={{ minHeight: 360 }}>
            {detailQ.isLoading && chatId && messages.length === 0 ? (
              <Loading label="loading chat..." />
            ) : detailQ.error && chatId ? (
              <ErrorBox message={`Failed to load chat: ${(detailQ.error as Error).message}`} />
            ) : messages.length === 0 ? (
              <div className="text-xs text-zinc-500">
                {chatId
                  ? "(empty conversation)"
                  : "No messages yet. Ask Kai something about this project."}
              </div>
            ) : (
              messages.map((m, i) => (
                <div key={i} className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
                  <div
                    className={`max-w-[80%] rounded p-3 text-sm ${
                      m.role === "user"
                        ? "bg-keboola/10 border border-keboola/30"
                        : m.error
                          ? "bg-red-50 border border-red-200 dark:bg-red-950/30 dark:border-red-900"
                          : "bg-zinc-100 border border-zinc-200 dark:bg-zinc-900 dark:border-zinc-800"
                    }`}
                  >
                    <div className="text-xs text-zinc-500 mb-1">{m.role}</div>
                    {m.role === "user" ? (
                      <div className="whitespace-pre-wrap">{m.content}</div>
                    ) : m.pending ? (
                      <div className="text-zinc-500 italic flex items-center gap-2">
                        <span className="inline-block w-2 h-2 rounded-full bg-keboola animate-pulse" />
                        Kai is thinking...
                      </div>
                    ) : (
                      <KaiMarkdown text={m.content} />
                    )}
                    {m.meta ? (
                      <details className="mt-2">
                        <summary className="text-xs text-zinc-500 cursor-pointer">raw</summary>
                        <JsonView data={m.meta} maxHeight="200px" />
                      </details>
                    ) : null}
                  </div>
                </div>
              ))
            )}
          </div>
          <form className="flex gap-2" onSubmit={handleSubmit}>
            <input
              className="nerd-input flex-1"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder={preflightBlocking ? "fix the token first…" : "ask Kai..."}
              disabled={sendMu.isPending || !!preflightBlocking}
            />
            <button
              type="submit"
              className="nerd-btn flex items-center gap-1 hover:text-keboola"
              disabled={sendMu.isPending || !input.trim() || !!preflightBlocking}
            >
              <Send className="w-3 h-3" /> {sendMu.isPending ? "..." : "Send"}
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}

function PreflightBanner({
  preflight,
  isLoading,
  error,
}: {
  preflight: PreflightResponse | undefined;
  isLoading: boolean;
  error: unknown;
}) {
  if (isLoading) {
    return <div className="text-xs text-zinc-500">checking token…</div>;
  }
  if (error) {
    return <ErrorBox message={`Preflight failed: ${(error as Error).message}`} />;
  }
  if (!preflight) return null;
  if (preflight.ok) {
    return (
      <div className="text-xs text-zinc-500">
        Token: <span className="font-mono">{preflight.token_description ?? "—"}</span>{" "}
        (master token, AI Agent Chat enabled)
      </div>
    );
  }

  // One of the two preconditions failed. Spell out exactly which, with the
  // important nouns highlighted in red so the user can scan it in 2 seconds.
  return (
    <div className="rounded border border-red-300 bg-red-50 p-3 text-sm dark:bg-red-950/30 dark:border-red-900">
      <div className="flex items-start gap-2">
        <AlertTriangle className="w-4 h-4 text-red-600 dark:text-red-400 mt-0.5 shrink-0" />
        <div className="space-y-1">
          <div className="font-semibold text-red-700 dark:text-red-300">
            Kai cannot run with the current token
          </div>
          <ul className="space-y-1 text-zinc-700 dark:text-zinc-300">
            {!preflight.is_master_token ? (
              <li>
                The configured token{" "}
                <span className="font-mono">{preflight.token_description ?? "—"}</span> is{" "}
                <span className="font-bold text-red-600 dark:text-red-400">not the master</span>{" "}
                token. Kai requires the project's{" "}
                <span className="font-bold text-red-600 dark:text-red-400">
                  master ("owner") Storage API token
                </span>{" "}
                — custom tokens cannot access Kai. Re-add the project with the master token.
              </li>
            ) : null}
            {!preflight.has_agent_chat_feature ? (
              <li>
                The project is missing the{" "}
                <span className="font-bold text-red-600 dark:text-red-400">AI Agent Chat</span>{" "}
                feature flag. Enable it in project settings and try again.
              </li>
            ) : null}
          </ul>
        </div>
      </div>
    </div>
  );
}

function ChatHistorySidebar({
  chats,
  isLoading,
  error,
  activeChatId,
  onOpen,
  onNew,
  onRefresh,
}: {
  chats: ChatSummary[];
  isLoading: boolean;
  error: unknown;
  activeChatId: string | null;
  onOpen: (id: string) => void;
  onNew: () => void;
  onRefresh: () => void;
}) {
  return (
    <div className="nerd-card space-y-2 h-fit">
      <div className="flex items-center justify-between">
        <div className="text-xs uppercase tracking-wide text-zinc-500">conversations</div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={onRefresh}
            className="nerd-btn !px-1.5 !py-1"
            title="Refresh history"
          >
            <RefreshCw className="w-3 h-3" />
          </button>
          <button
            type="button"
            onClick={onNew}
            className="nerd-btn !px-2 !py-1 flex items-center gap-1"
            title="Start a new chat"
          >
            <MessageSquarePlus className="w-3 h-3" /> new
          </button>
        </div>
      </div>
      {isLoading ? (
        <div className="text-xs text-zinc-500">loading…</div>
      ) : error ? (
        <div className="text-xs text-red-500">{(error as Error).message}</div>
      ) : chats.length === 0 ? (
        <div className="text-xs text-zinc-500">No chats yet.</div>
      ) : (
        <ul className="space-y-1 max-h-[70vh] overflow-y-auto">
          {chats.map((c) => {
            const active = c.id === activeChatId;
            return (
              <li key={c.id}>
                <button
                  type="button"
                  onClick={() => onOpen(c.id)}
                  className={`w-full text-left text-xs rounded px-2 py-1.5 transition-colors ${
                    active
                      ? "bg-keboola/10 border border-keboola/40 text-keboola"
                      : "border border-transparent hover:border-zinc-300 dark:hover:border-zinc-700"
                  }`}
                  title={c.id}
                >
                  <div className="truncate">{c.title}</div>
                  {c.created_at ? (
                    <div className="text-[10px] text-zinc-500">
                      {new Date(c.created_at).toLocaleString()}
                    </div>
                  ) : null}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

/**
 * Render Kai's markdown answer.
 *
 * - GFM enabled → tables, strikethrough, task lists work out of the box.
 * - Links open in a new tab (Kai often returns deep-links to Keboola UI).
 * - No raw HTML allowed (react-markdown default) → safe against injection.
 * - `icon:NAME` tokens are pre-processed into emoji because Kai uses Keboola
 *   Connection's Font Awesome shorthand that our UI doesn't ship.
 */
function KaiMarkdown({ text }: { text: string }) {
  const normalized = normalizeKaiText(text);
  return (
    <div className="kai-md text-sm leading-relaxed">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: (props) => (
            <a
              {...props}
              target="_blank"
              rel="noopener noreferrer"
              className="text-keboola underline decoration-keboola/40 hover:decoration-keboola"
            />
          ),
          table: (props) => (
            <div className="overflow-x-auto my-2">
              <table
                {...props}
                className="text-xs border-collapse border border-zinc-300 dark:border-zinc-700"
              />
            </div>
          ),
          thead: (props) => <thead {...props} className="bg-zinc-200 dark:bg-zinc-800" />,
          th: (props) => (
            <th
              {...props}
              className="text-left px-2 py-1 border border-zinc-300 dark:border-zinc-700 font-semibold"
            />
          ),
          td: (props) => (
            <td {...props} className="px-2 py-1 border border-zinc-300 dark:border-zinc-700 align-top" />
          ),
          code: (props) => {
            const { className, children, ...rest } = props as {
              className?: string;
              children?: React.ReactNode;
            };
            const isBlock = (className ?? "").startsWith("language-");
            if (isBlock) {
              return (
                <pre className="nerd-code my-2 whitespace-pre-wrap">
                  <code {...rest} className={className}>
                    {children}
                  </code>
                </pre>
              );
            }
            return (
              <code
                {...rest}
                className="px-1 py-0.5 rounded bg-zinc-200 dark:bg-zinc-800 text-[0.85em] font-mono"
              >
                {children}
              </code>
            );
          },
          ul: (props) => <ul {...props} className="list-disc pl-5 my-1 space-y-0.5" />,
          ol: (props) => <ol {...props} className="list-decimal pl-5 my-1 space-y-0.5" />,
          h1: (props) => <h1 {...props} className="text-base font-semibold mt-2 mb-1" />,
          h2: (props) => <h2 {...props} className="text-sm font-semibold mt-2 mb-1" />,
          h3: (props) => <h3 {...props} className="text-sm font-semibold mt-2 mb-1" />,
          hr: () => <hr className="my-3 border-zinc-300 dark:border-zinc-700" />,
          p: (props) => <p {...props} className="my-1" />,
        }}
      >
        {normalized}
      </ReactMarkdown>
    </div>
  );
}
