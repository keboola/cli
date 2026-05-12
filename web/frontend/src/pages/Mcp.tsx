import { useMutation, useQuery } from "@tanstack/react-query";
import { Play } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api/client";
import { Drawer } from "../components/Drawer";
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
  const [filter, setFilter] = useState("");

  const q = useQuery<ToolsResp>({
    queryKey: ["mcp-tools", project, branchId],
    queryFn: () =>
      api.get("/mcp/tools", {
        query: {
          project: project ?? undefined,
          branch_id: branchId ? String(branchId) : undefined,
        },
      }),
  });

  const tools = (q.data?.tools ?? []).filter((t) =>
    filter
      ? `${t.name} ${t.description}`.toLowerCase().includes(filter.toLowerCase())
      : true,
  );

  return (
    <div className="space-y-4">
      <PageTitle
        title="MCP Tools"
        description={`${q.data?.tools.length ?? 0} tools exposed by keboola-mcp-server. Click a tile to call it.`}
      />
      <input
        className="nerd-input w-full max-w-md"
        placeholder="filter tools..."
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
      />
      {q.isLoading ? <Loading /> : null}
      {q.error ? <ErrorBox message={(q.error as Error).message} /> : null}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
        {tools.map((t) => (
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
      <ToolRunnerDrawer tool={selected} onClose={() => setSelected(null)} />
    </div>
  );
}

function ToolRunnerDrawer({ tool, onClose }: { tool: McpTool | null; onClose: () => void }) {
  const { project } = useUIState();
  const [json, setJson] = useState("{}");
  const [result, setResult] = useState<unknown | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Reset state whenever a different tool is selected.
  useEffect(() => {
    if (tool) {
      const required = (tool.inputSchema as { required?: string[] })?.required ?? [];
      const props = (tool.inputSchema as { properties?: Record<string, unknown> })?.properties ?? {};
      const skeleton: Record<string, unknown> = {};
      for (const key of required) {
        const prop = props[key] as { type?: string; default?: unknown } | undefined;
        skeleton[key] = prop?.default ?? (prop?.type === "array" ? [] : prop?.type === "object" ? {} : "");
      }
      setJson(JSON.stringify(skeleton, null, 2));
      setResult(null);
      setError(null);
    }
  }, [tool]);

  const mu = useMutation({
    mutationFn: () => {
      if (!tool) throw new Error("No tool selected");
      let parsed: Record<string, unknown> = {};
      try {
        parsed = JSON.parse(json || "{}");
      } catch (e) {
        throw new Error(`Invalid JSON: ${(e as Error).message}`);
      }
      return api.post(`/mcp/tools/${encodeURIComponent(tool.name)}/call`, {
        input: parsed,
        project,
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
    <Drawer
      open={!!tool}
      onClose={onClose}
      title={tool?.name ?? ""}
      subtitle={tool?.description}
      width="max-w-4xl"
      actions={
        <button
          type="button"
          className="nerd-btn flex items-center gap-1 hover:text-keboola"
          onClick={() => mu.mutate()}
          disabled={!tool || mu.isPending}
        >
          <Play className="w-3 h-3" /> {mu.isPending ? "running..." : "Call"}
        </button>
      }
    >
      {tool ? (
        <div className="space-y-4">
          <div>
            <div className="text-xs text-zinc-500 mb-1">Input (JSON)</div>
            <textarea
              className="nerd-input w-full h-48 font-mono text-xs"
              value={json}
              onChange={(e) => setJson(e.target.value)}
            />
          </div>
          <details>
            <summary className="text-xs text-zinc-500 cursor-pointer">
              Schema (
              {(tool.inputSchema as { required?: string[] })?.required?.length ?? 0} required)
            </summary>
            <JsonView data={tool.inputSchema} maxHeight="200px" />
          </details>
          <div>
            <div className="text-xs text-zinc-500 mb-1">Result</div>
            {error ? <ErrorBox message={error} /> : null}
            {result ? <JsonView data={result} /> : null}
            {!error && !result ? (
              <div className="text-zinc-600 text-xs">Result will appear here after Call.</div>
            ) : null}
          </div>
        </div>
      ) : null}
    </Drawer>
  );
}
