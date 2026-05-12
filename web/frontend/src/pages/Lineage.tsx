import { useMutation, useQuery } from "@tanstack/react-query";
import { ArrowLeftRight, FolderOpen, Hammer, Network } from "lucide-react";
import mermaid from "mermaid";
import { useEffect, useId, useRef, useState } from "react";
import { api } from "../api/client";
import { Empty, ErrorBox, Loading, PageTitle } from "../components/Empty";
import { JsonView } from "../components/JsonView";
import { DataTable } from "../components/Table";
import type { LineageEdge, ProjectError, SharedBucket } from "../types";

interface SharingResp {
  edges: LineageEdge[];
  shared_buckets: Array<SharedBucket & { project_alias: string; bucket_id: string; bucket_name: string }>;
  linked_buckets: Array<Record<string, unknown>>;
  summary: { total_edges: number; total_shared_buckets: number; total_linked_buckets: number };
  errors: ProjectError[];
}

mermaid.initialize({
  startOnLoad: false,
  theme: "dark",
  themeVariables: {
    background: "#050508",
    primaryColor: "#1a1a2e",
    primaryTextColor: "#22c55e",
    primaryBorderColor: "#22c55e",
    lineColor: "#22d3ee",
  },
});

export function LineagePage() {
  const [tab, setTab] = useState<"sharing" | "deep">("sharing");
  return (
    <div className="space-y-4">
      <PageTitle
        title="Lineage"
        description="Two views: bucket-sharing graph (live, cross-project) + deep column-level lineage (from a pre-computed JSON cache built via 'kbagent lineage build')."
      />
      <div className="flex gap-2">
        <button
          type="button"
          className={`nerd-btn flex items-center gap-1 ${
            tab === "sharing" ? "border-keboola text-keboola" : ""
          }`}
          onClick={() => setTab("sharing")}
        >
          <ArrowLeftRight className="w-3 h-3" /> Sharing graph
        </button>
        <button
          type="button"
          className={`nerd-btn flex items-center gap-1 ${
            tab === "deep" ? "border-keboola text-keboola" : ""
          }`}
          onClick={() => setTab("deep")}
        >
          <Network className="w-3 h-3" /> Deep lineage (from JSON)
        </button>
      </div>
      {tab === "sharing" ? <SharingTab /> : <DeepLineageTab />}
    </div>
  );
}

function SharingTab() {
  const q = useQuery<SharingResp>({
    queryKey: ["lineage-sharing"],
    queryFn: () => api.get("/lineage/edges"),
  });
  return (
    <>
      {q.isLoading ? <Loading /> : null}
      {q.error ? <ErrorBox message={(q.error as Error).message} /> : null}
      {q.data ? (
        <>
          <div className="grid grid-cols-3 gap-3">
            <Stat label="Edges" value={q.data.summary.total_edges} />
            <Stat label="Shared buckets" value={q.data.summary.total_shared_buckets} />
            <Stat label="Linked buckets" value={q.data.summary.total_linked_buckets} />
          </div>
          {q.data.edges.length > 0 ? (
            <>
              <MermaidGraph edges={q.data.edges} />
              <DataTable
                rows={q.data.edges}
                rowKey={(e) =>
                  `${e.source_project_id}/${e.source_bucket_id}-(${e.sharing_type})->${e.target_project_id}/${e.target_bucket_id}`
                }
                columns={[
                  {
                    header: "Source",
                    cell: (e) => (
                      <span className="text-keboola">
                        {e.source_project_alias || `#${e.source_project_id}`}
                      </span>
                    ),
                  },
                  {
                    header: "Source bucket",
                    cell: (e) => <span className="text-accent">{e.source_bucket_id}</span>,
                  },
                  {
                    header: "Type",
                    cell: (e) => <span className="nerd-pill">{e.sharing_type}</span>,
                  },
                  {
                    header: "Target",
                    cell: (e) => (
                      <span className="text-keboola">
                        {e.target_project_alias || `#${e.target_project_id}`}
                      </span>
                    ),
                  },
                  {
                    header: "Target bucket",
                    cell: (e) => <span className="text-accent">{e.target_bucket_id}</span>,
                  },
                ]}
              />
            </>
          ) : (
            <Empty title="No bucket sharing between registered projects." />
          )}
        </>
      ) : null}
    </>
  );
}

function DeepLineageTab() {
  const [path, setPath] = useState(() =>
    localStorage.getItem("kbagent-lineage-path") ?? "",
  );
  const [buildDir, setBuildDir] = useState(() =>
    localStorage.getItem("kbagent-lineage-build-dir") ?? "",
  );
  const [refresh, setRefresh] = useState(true);
  const [useAi, setUseAi] = useState(false);
  const [info, setInfo] = useState<unknown | null>(null);
  const [buildResult, setBuildResult] = useState<unknown | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [queryNode, setQueryNode] = useState("");
  const [direction, setDirection] = useState<"upstream" | "downstream">("upstream");
  const [depth, setDepth] = useState(5);
  const [queryResult, setQueryResult] = useState<unknown | null>(null);

  const loadMu = useMutation({
    mutationFn: () => api.get(`/lineage/info?load=${encodeURIComponent(path)}`),
    onSuccess: (data) => {
      setInfo(data);
      setError(null);
      localStorage.setItem("kbagent-lineage-path", path);
    },
    onError: (err) => {
      setInfo(null);
      setError((err as Error).message);
    },
  });

  const buildMu = useMutation({
    mutationFn: () => {
      const out = path || `${buildDir}/lineage.json`;
      return api.post("/lineage/build", {
        directory: buildDir,
        output: out,
        use_ai: useAi,
        refresh,
      });
    },
    onSuccess: (data, _vars) => {
      setBuildResult(data);
      setError(null);
      localStorage.setItem("kbagent-lineage-build-dir", buildDir);
      // Auto-load info after a successful build.
      const out = path || `${buildDir}/lineage.json`;
      setPath(out);
      loadMu.mutate();
    },
    onError: (err) => {
      setBuildResult(null);
      setError((err as Error).message);
    },
  });

  const queryMu = useMutation({
    mutationFn: () =>
      api.post("/lineage/show", {
        load: path,
        upstream: direction === "upstream" ? queryNode : null,
        downstream: direction === "downstream" ? queryNode : null,
        depth,
        format: "text",
      }),
    onSuccess: (data) => setQueryResult(data),
    onError: (err) => setError((err as Error).message),
  });

  return (
    <div className="space-y-4">
      <div className="nerd-card space-y-3">
        <h3 className="text-sm font-bold text-keboola flex items-center gap-2">
          <Hammer className="w-4 h-4" /> Build deep lineage
        </h3>
        <p className="text-xs text-zinc-500">
          Pulls every project's configs (<code className="text-accent">kbagent sync pull</code>),
          parses SQL transformations, and writes a JSON cache. Big projects:
          minutes. The output JSON is auto-loaded on success.
        </p>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-2">
          <label className="text-xs text-zinc-400">
            Working directory (where to sync project configs)
            <input
              className="nerd-input w-full mt-1 font-mono"
              placeholder="/tmp/kbagent-lineage"
              value={buildDir}
              onChange={(e) => setBuildDir(e.target.value)}
            />
          </label>
          <label className="text-xs text-zinc-400">
            Output file (defaults to ./lineage.json inside the working dir)
            <input
              className="nerd-input w-full mt-1 font-mono"
              placeholder="(default)"
              value={path}
              onChange={(e) => setPath(e.target.value)}
            />
          </label>
        </div>
        <div className="flex flex-wrap gap-3 items-center">
          <label className="flex items-center gap-2 text-xs text-zinc-400">
            <input
              type="checkbox"
              checked={refresh}
              onChange={(e) => setRefresh(e.target.checked)}
            />
            sync pull all projects first (recommended)
          </label>
          <label className="flex items-center gap-2 text-xs text-zinc-400">
            <input
              type="checkbox"
              checked={useAi}
              onChange={(e) => setUseAi(e.target.checked)}
            />
            generate AI tasks for ambiguous edges (--ai)
          </label>
          <div className="ml-auto flex gap-2">
            <button
              type="button"
              className="nerd-btn hover:text-keboola"
              disabled={!buildDir || buildMu.isPending}
              onClick={() => buildMu.mutate()}
            >
              <Hammer className="w-3 h-3 inline mr-1" />
              {buildMu.isPending ? "building (this can take minutes)..." : "Build"}
            </button>
          </div>
        </div>
        {buildResult ? (
          <details>
            <summary className="text-xs text-zinc-500 cursor-pointer">
              Build summary
            </summary>
            <JsonView data={buildResult} maxHeight="220px" />
          </details>
        ) : null}
      </div>

      <div className="nerd-card space-y-3">
        <h3 className="text-sm font-bold text-keboola flex items-center gap-2">
          <FolderOpen className="w-4 h-4" /> Load existing lineage cache
        </h3>
        <div className="flex gap-2">
          <input
            className="nerd-input flex-1 font-mono"
            placeholder="/path/to/lineage.json"
            value={path}
            onChange={(e) => setPath(e.target.value)}
          />
          <button
            type="button"
            className="nerd-btn hover:text-keboola"
            disabled={!path || loadMu.isPending}
            onClick={() => loadMu.mutate()}
          >
            {loadMu.isPending ? "loading..." : "Load"}
          </button>
        </div>
      </div>

      {error ? <ErrorBox message={error} /> : null}

      {info ? (
        <>
          <div className="nerd-card">
            <h3 className="text-sm font-bold text-keboola mb-2">Graph summary</h3>
            <JsonView data={info} maxHeight="200px" />
          </div>

          <div className="nerd-card space-y-3">
            <h3 className="text-sm font-bold text-keboola">Walk the graph</h3>
            <div className="flex gap-2 flex-wrap items-end">
              <label className="text-xs text-zinc-400 flex-1 min-w-[260px]">
                Node FQN (project:table or table.id)
                <input
                  className="nerd-input w-full mt-1"
                  placeholder="padak:in.c-bucket.table"
                  value={queryNode}
                  onChange={(e) => setQueryNode(e.target.value)}
                />
              </label>
              <label className="text-xs text-zinc-400">
                Direction
                <select
                  className="nerd-input w-full mt-1"
                  value={direction}
                  onChange={(e) =>
                    setDirection(e.target.value as "upstream" | "downstream")
                  }
                >
                  <option value="upstream">upstream (sources)</option>
                  <option value="downstream">downstream (consumers)</option>
                </select>
              </label>
              <label className="text-xs text-zinc-400">
                Depth
                <input
                  type="number"
                  className="nerd-input w-20 mt-1"
                  value={depth}
                  onChange={(e) => setDepth(Number(e.target.value) || 1)}
                  min={1}
                  max={20}
                />
              </label>
              <button
                type="button"
                className="nerd-btn hover:text-keboola"
                disabled={!queryNode || queryMu.isPending}
                onClick={() => queryMu.mutate()}
              >
                {queryMu.isPending ? "walking..." : "Walk"}
              </button>
            </div>
          </div>

          {queryResult ? (
            <div className="nerd-card">
              <h3 className="text-sm font-bold text-keboola mb-2">Walk result</h3>
              <JsonView data={queryResult} />
            </div>
          ) : null}
        </>
      ) : (
        <Empty
          title="No lineage cache loaded yet"
          hint="Use the Build form above (UI does sync pull + lineage build for you), or point Load at an existing JSON cache."
        />
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="nerd-card">
      <div className="text-xs text-zinc-500 uppercase tracking-wider">{label}</div>
      <div className="text-3xl font-bold text-keboola mt-1">{value}</div>
    </div>
  );
}

function MermaidGraph({ edges }: { edges: LineageEdge[] }) {
  const id = useId().replace(/:/g, "");
  const ref = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    const lines = ["graph LR"];
    const slug = (s: string) => s.replace(/[^a-zA-Z0-9]/g, "_");
    const esc = (s: string) =>
      s
        .replace(/&/g, "&amp;")
        .replace(/"/g, "&quot;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
    for (const e of edges) {
      const src = e.source_project_alias || `p${e.source_project_id}`;
      const dst = e.target_project_alias || `p${e.target_project_id}`;
      const srcId = `n_${slug(src)}_${slug(e.source_bucket_id)}`;
      const dstId = `n_${slug(dst)}_${slug(e.target_bucket_id)}`;
      lines.push(
        `  ${srcId}["${esc(src)}<br/>${esc(e.source_bucket_id)}"] -->|${esc(e.sharing_type || "shared")}| ${dstId}["${esc(dst)}<br/>${esc(e.target_bucket_id)}"]`,
      );
    }
    const code = lines.join("\n");
    mermaid
      .render(`graph_${id}`, code)
      .then(({ svg }) => {
        if (ref.current) ref.current.innerHTML = svg;
        setError(null);
      })
      .catch((err) => setError(String(err)));
  }, [edges, id]);

  return (
    <div className="nerd-card">
      <h3 className="text-keboola font-bold text-sm mb-3">Diagram</h3>
      {error ? <div className="text-red-400 text-xs">{error}</div> : null}
      <div ref={ref} className="overflow-auto" style={{ minHeight: 280 }} />
    </div>
  );
}
