import { useMutation, useQuery } from "@tanstack/react-query";
import { Play } from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import { ErrorBox, Loading, PageTitle } from "../components/Empty";
import { JsonView } from "../components/JsonView";
import { useUIState } from "../state";
import type { McpTool, ProjectError } from "../types";

interface ToolsResp {
  tools: McpTool[];
  errors: ProjectError[];
}

export function McpPage() {
  const { project, branchId } = useUIState();
  const [selected, setSelected] = useState<McpTool | null>(null);
  const q = useQuery<ToolsResp>({
    queryKey: ["mcp-tools", project, branchId],
    queryFn: () =>
      api.get("/mcp/tools", {
        query: { project: project ?? undefined, branch_id: branchId ? String(branchId) : undefined },
      }),
  });
  return (
    <div className="space-y-4">
      <PageTitle
        title="MCP Tools"
        description="Tools exposed by keboola-mcp-server. Multi-project tools fan out across all registered projects."
      />
      {q.isLoading ? <Loading /> : null}
      {q.error ? <ErrorBox message={(q.error as Error).message} /> : null}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
        {(q.data?.tools ?? []).map((t) => (
          <button
            key={t.name}
            type="button"
            onClick={() => setSelected(t)}
            className="nerd-card text-left hover:border-keboola/50 transition-colors"
          >
            <div className="flex items-center justify-between mb-1">
              <div className="font-bold text-accent">{t.name}</div>
              {t.multi_project ? (
                <span className="nerd-pill-green">multi-project</span>
              ) : (
                <span className="nerd-pill-amber">write</span>
              )}
            </div>
            <div className="text-xs text-zinc-500 line-clamp-3">{t.description}</div>
          </button>
        ))}
      </div>
      {selected ? <ToolRunner tool={selected} onClose={() => setSelected(null)} /> : null}
    </div>
  );
}

function ToolRunner({ tool, onClose }: { tool: McpTool; onClose: () => void }) {
  const { project } = useUIState();
  const [json, setJson] = useState("{}");
  const [result, setResult] = useState<unknown | null>(null);
  const [error, setError] = useState<string | null>(null);

  const mu = useMutation({
    mutationFn: () => {
      let parsed: Record<string, unknown> = {};
      try {
        parsed = JSON.parse(json || "{}");
      } catch (e) {
        throw new Error(`Invalid JSON: ${(e as Error).message}`);
      }
      return api.post(`/mcp/tools/${encodeURIComponent(tool.name)}/call`, {
        input: parsed,
        project: project,
      });
    },
    onSuccess: (data) => {
      setResult(data);
      setError(null);
    },
    onError: (err) => {
      setError((err as Error).message);
      setResult(null);
    },
  });

  return (
    <div className="nerd-card">
      <div className="flex items-center justify-between mb-3">
        <h3 className="font-bold text-keboola">{tool.name}</h3>
        <div className="flex gap-2">
          <button
            type="button"
            className="nerd-btn flex items-center gap-1 hover:text-keboola"
            onClick={() => mu.mutate()}
          >
            <Play className="w-3 h-3" /> {mu.isPending ? "running..." : "Call"}
          </button>
          <button type="button" className="nerd-btn text-xs" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
      <p className="text-xs text-zinc-500 mb-3">{tool.description}</p>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <div className="text-xs text-zinc-500 mb-1">Input (JSON)</div>
          <textarea
            className="nerd-input w-full h-64 font-mono"
            value={json}
            onChange={(e) => setJson(e.target.value)}
          />
          <div className="text-xs text-zinc-500 mt-2">Schema:</div>
          <JsonView data={tool.inputSchema} maxHeight="240px" />
        </div>
        <div>
          <div className="text-xs text-zinc-500 mb-1">Result</div>
          {error ? <ErrorBox message={error} /> : null}
          {result ? <JsonView data={result} /> : null}
          {!error && !result ? (
            <div className="text-zinc-600 text-xs">Result will appear here.</div>
          ) : null}
        </div>
      </div>
    </div>
  );
}
