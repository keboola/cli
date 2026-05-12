import { useMutation, useQuery } from "@tanstack/react-query";
import { Send } from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import { ErrorBox, Loading, PageTitle } from "../components/Empty";
import { JsonView } from "../components/JsonView";
import { useUIState } from "../state";

interface Message {
  role: "user" | "assistant";
  content: string;
  meta?: unknown;
}

export function KaiPage() {
  const { project } = useUIState();
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [chatId, setChatId] = useState<string | null>(null);

  const pingQ = useQuery({
    queryKey: ["kai-ping", project],
    queryFn: () => api.get("/kai/ping", { query: { project: project ?? undefined } }),
    retry: false,
  });

  const sendMu = useMutation({
    mutationFn: () =>
      api.post<{ message?: string; chat_id?: string; response?: string }>("/kai/chat", {
        message: input,
        chat_id: chatId,
        project,
      }),
    onSuccess: (data) => {
      setMessages((m) => [
        ...m,
        { role: "user", content: input },
        { role: "assistant", content: data.response ?? data.message ?? "", meta: data },
      ]);
      if (data.chat_id) setChatId(data.chat_id);
      setInput("");
    },
  });

  return (
    <div className="space-y-4">
      <PageTitle title="Kai (Keboola AI)" description="Chat with the Keboola AI assistant scoped to your project." />
      {pingQ.error ? (
        <ErrorBox message={`Kai not available: ${(pingQ.error as Error).message}`} />
      ) : pingQ.isLoading ? (
        <Loading label="connecting to Kai..." />
      ) : (
        <div className="nerd-pill-green">connected as {project}</div>
      )}
      <div className="nerd-card space-y-3" style={{ minHeight: 360 }}>
        {messages.length === 0 ? (
          <div className="text-xs text-zinc-500">No messages yet. Ask Kai something about this project.</div>
        ) : (
          messages.map((m, i) => (
            <div key={i} className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
              <div
                className={`max-w-[80%] rounded p-3 text-sm ${
                  m.role === "user" ? "bg-keboola/10 border border-keboola/30" : "bg-zinc-900 border border-zinc-800"
                }`}
              >
                <div className="text-xs text-zinc-500 mb-1">{m.role}</div>
                <div className="whitespace-pre-wrap">{m.content}</div>
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
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (input.trim() && !sendMu.isPending) sendMu.mutate();
        }}
      >
        <input
          className="nerd-input flex-1"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="ask Kai..."
          disabled={sendMu.isPending}
        />
        <button type="submit" className="nerd-btn flex items-center gap-1 hover:text-keboola" disabled={sendMu.isPending || !input.trim()}>
          <Send className="w-3 h-3" /> {sendMu.isPending ? "..." : "Send"}
        </button>
      </form>
      {sendMu.error ? <ErrorBox message={(sendMu.error as Error).message} /> : null}
    </div>
  );
}
