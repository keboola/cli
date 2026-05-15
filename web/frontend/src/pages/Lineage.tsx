import { useMutation, useQuery } from "@tanstack/react-query";
import { ArrowLeftRight, FolderOpen, Hammer, Network } from "lucide-react";
import mermaid from "mermaid";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import { Empty, ErrorBox, Loading, PageTitle } from "../components/Empty";
import { JsonView } from "../components/JsonView";
import { DataTable } from "../components/Table";
import { useTheme } from "../theme";
import type { LineageEdge, ProjectError, SharedBucket } from "../types";

interface SharingResp {
  edges: LineageEdge[];
  shared_buckets: Array<SharedBucket & { project_alias: string; bucket_id: string; bucket_name: string }>;
  linked_buckets: Array<Record<string, unknown>>;
  summary: { total_edges: number; total_shared_buckets: number; total_linked_buckets: number };
  errors: ProjectError[];
}

/**
 * Re-initialize mermaid with the appropriate theme variables for the current
 * light/dark mode. Must be called both on first render and any time the theme
 * flips so previously rendered diagrams pick up the new palette on next render.
 */
/**
 * Mermaid's default ``maxTextSize`` is 50 KB. Real Keboola sharing graphs
 * (multi-org setups with 50+ projects and 250+ edges) routinely break that
 * limit, so we bump it to ~5 MB. The browser will still struggle with
 * truly massive graphs — when it does, the OversizeBanner kicks in and
 * points the user at Deep Lineage / the source-code download.
 */
const MERMAID_MAX_TEXT_SIZE = 5_000_000;

function initMermaid(theme: "light" | "dark"): void {
  if (theme === "dark") {
    mermaid.initialize({
      startOnLoad: false,
      maxTextSize: MERMAID_MAX_TEXT_SIZE,
      theme: "dark",
      themeVariables: {
        background: "#050508",
        primaryColor: "#1a1a2e",
        primaryTextColor: "#22c55e",
        primaryBorderColor: "#22c55e",
        lineColor: "#22d3ee",
      },
    });
  } else {
    mermaid.initialize({
      startOnLoad: false,
      maxTextSize: MERMAID_MAX_TEXT_SIZE,
      theme: "default",
      themeVariables: {
        background: "#ffffff",
        primaryColor: "#f4f4f5",
        primaryTextColor: "#18181b",
        primaryBorderColor: "#16a34a",
        lineColor: "#0891b2",
      },
    });
  }
}

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
      {tab === "sharing" ? <SharingTab onOpenDeepLineage={() => setTab("deep")} /> : <DeepLineageTab />}
    </div>
  );
}

function SharingTab({ onOpenDeepLineage }: { onOpenDeepLineage: () => void }) {
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
              <MermaidGraph edges={q.data.edges} onOpenDeepLineage={onOpenDeepLineage} />
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
  // Pre-fill defaults so the Build button is enabled out of the box.
  // Empty inputs were the #1 reason "Build does nothing" reports landed --
  // the placeholder text looked identical to a real value and the disabled
  // state on the button was too subtle to notice.
  const [path, setPath] = useState(
    () => localStorage.getItem("kbagent-lineage-path") ?? "",
  );
  const [buildDir, setBuildDir] = useState(
    () => localStorage.getItem("kbagent-lineage-build-dir") ?? "/tmp/kbagent-lineage",
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

  // loadMu takes the path as a mutate() argument (instead of reading state)
  // because React batches setPath() so the post-build auto-load was reading
  // the *previous* (empty) path through closure -> 500 from /info?load=
  const loadMu = useMutation({
    mutationFn: (overridePath?: string) => {
      const target = (overridePath ?? path).trim();
      if (!target) throw new Error("No lineage cache path to load.");
      return api.get(`/lineage/info?load=${encodeURIComponent(target)}`);
    },
    onSuccess: (data, variables) => {
      setInfo(data);
      setError(null);
      const used = (variables ?? path).trim();
      if (used) localStorage.setItem("kbagent-lineage-path", used);
    },
    onError: (err) => {
      setInfo(null);
      setError((err as Error).message);
    },
  });

  const buildMu = useMutation({
    mutationFn: () => {
      const out = path.trim() || `${buildDir.replace(/\/$/, "")}/lineage.json`;
      return api.post<Record<string, unknown> & { output_path?: string }>(
        "/lineage/build",
        {
          directory: buildDir,
          output: out,
          use_ai: useAi,
          refresh,
        },
      );
    },
    onSuccess: (data) => {
      setBuildResult(data);
      setError(null);
      localStorage.setItem("kbagent-lineage-build-dir", buildDir);
      // Server returns the resolved output_path; trust that over what we sent.
      const resolved =
        (data.output_path as string | undefined) ??
        (path.trim() || `${buildDir}/lineage.json`);
      setPath(resolved);
      // Pass the resolved path explicitly -- can't rely on `path` here because
      // React hasn't applied setPath yet when this fires synchronously.
      loadMu.mutate(resolved);
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
          <label className="text-xs text-zinc-600 dark:text-zinc-400">
            Working directory (where to sync project configs)
            <input
              className="nerd-input w-full mt-1 font-mono"
              placeholder="/tmp/kbagent-lineage"
              value={buildDir}
              onChange={(e) => setBuildDir(e.target.value)}
            />
          </label>
          <label className="text-xs text-zinc-600 dark:text-zinc-400">
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
          <label className="flex items-center gap-2 text-xs text-zinc-600 dark:text-zinc-400">
            <input
              type="checkbox"
              checked={refresh}
              onChange={(e) => setRefresh(e.target.checked)}
            />
            sync pull all projects first (recommended)
          </label>
          <label className="flex items-center gap-2 text-xs text-zinc-600 dark:text-zinc-400">
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
              className="nerd-btn hover:text-keboola disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:text-zinc-600 dark:disabled:hover:text-zinc-400"
              disabled={!buildDir.trim() || buildMu.isPending}
              title={
                !buildDir.trim() ? "Working directory required" : undefined
              }
              onClick={() => {
                setError(null);
                setBuildResult(null);
                buildMu.mutate();
              }}
            >
              <Hammer className="w-3 h-3 inline mr-1" />
              {buildMu.isPending ? "building..." : "Build"}
            </button>
          </div>
        </div>
        {!buildDir.trim() ? (
          <div className="text-xs text-amber-700 dark:text-neon-amber">
            ⚠ Working directory is empty -- type a path (e.g.{" "}
            <code className="text-accent">/tmp/kbagent-lineage</code>) to enable
            Build.
          </div>
        ) : null}
        {buildMu.isPending ? (
          <div className="text-xs text-keboola flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-keboola animate-pulse" />
            sync pull + parse SQL + scan configs ・ this can take minutes for big
            organizations ・ stay on this page (the request is in-flight)
          </div>
        ) : null}
        {buildResult ? (
          <details open>
            <summary className="text-xs text-keboola cursor-pointer">
              ✓ Build complete -- summary (click to expand)
            </summary>
            <JsonView data={buildResult} maxHeight="240px" />
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
            onClick={() => loadMu.mutate(undefined)}
          >
            {loadMu.isPending ? "loading..." : "Load"}
          </button>
        </div>
      </div>

      {error ? <ErrorBox message={error} /> : null}

      {info ? (
        <>
          <details className="nerd-card">
            <summary className="text-sm font-bold text-keboola cursor-pointer">
              Graph summary (click to expand)
            </summary>
            <div className="mt-2">
              <JsonView data={info} maxHeight="200px" />
            </div>
          </details>

          {/* Embed the existing `kbagent lineage server` HTML browser as an
              iframe -- it already handles sidebar / node search / direction /
              depth / mermaid diagram / columns toggle / ER view. No need to
              re-implement any of it in React. */}
          <div className="nerd-card p-0 overflow-hidden">
            <div className="px-3 py-2 border-b border-zinc-200 flex items-center justify-between text-xs dark:border-zinc-800">
              <span className="text-keboola font-bold">
                Interactive browser
                <span className="text-zinc-500 font-normal ml-2">
                  (sidebar tree ・ search ・ upstream / downstream walk ・ Mermaid ・ ER ・ columns)
                </span>
              </span>
              <a
                href={`/api/lineage/browser?load=${encodeURIComponent(path)}`}
                target="_blank"
                rel="noreferrer"
                className="text-zinc-500 hover:text-keboola"
              >
                open in new tab ↗
              </a>
            </div>
            <iframe
              key={path}
              src={`/api/lineage/browser?load=${encodeURIComponent(path)}`}
              title="Lineage browser"
              className="w-full bg-white"
              style={{ height: "70vh", border: 0 }}
            />
          </div>

          {/* Headless walk -- handy if you just want a JSON dump for an LLM
              to consume without scraping the iframe. */}
          <details className="nerd-card space-y-3">
            <summary className="text-sm font-bold text-keboola cursor-pointer">
              Headless walk (JSON, for scripting)
            </summary>
            <div className="flex gap-2 flex-wrap items-end mt-3">
              <label className="text-xs text-zinc-600 flex-1 min-w-[260px] dark:text-zinc-400">
                Node FQN (project:table or table.id)
                <input
                  className="nerd-input w-full mt-1"
                  placeholder="padak:in.c-bucket.table"
                  value={queryNode}
                  onChange={(e) => setQueryNode(e.target.value)}
                />
              </label>
              <label className="text-xs text-zinc-600 dark:text-zinc-400">
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
              <label className="text-xs text-zinc-600 dark:text-zinc-400">
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
            {queryResult ? <JsonView data={queryResult} /> : null}
          </details>
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

function MermaidGraph({
  edges,
  onOpenDeepLineage,
}: {
  edges: LineageEdge[];
  onOpenDeepLineage: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const renderSeq = useRef(0);
  const [error, setError] = useState<string | null>(null);
  // The Mermaid source code we generated for THIS render. Stashed so the
  // oversize banner can offer it as a download — even when the embedded
  // renderer can't draw the graph, the user can paste this into
  // mermaid.live or a local renderer.
  const [mermaidCode, setMermaidCode] = useState<string>("");
  // Filter state — when empty (""), no filter is applied. The picker
  // dropdowns let users narrow the edge set on the source or target
  // project alias, which is the main escape hatch when the full graph
  // hits Mermaid's size guard (#289).
  const [sourceFilter, setSourceFilter] = useState<string>("");
  const [targetFilter, setTargetFilter] = useState<string>("");
  const { theme } = useTheme();

  // Build the unique alias lists for the dropdowns. We sort them so the
  // user can scan alphabetically; "(all)" is rendered separately as the
  // empty-string option in the JSX, so this list excludes it.
  const sourceAliases = useMemo(() => {
    const set = new Set<string>();
    for (const e of edges) {
      const alias = e.source_project_alias || `p${e.source_project_id}`;
      set.add(alias);
    }
    return Array.from(set).sort((a, b) => a.localeCompare(b));
  }, [edges]);
  const targetAliases = useMemo(() => {
    const set = new Set<string>();
    for (const e of edges) {
      const alias = e.target_project_alias || `p${e.target_project_id}`;
      set.add(alias);
    }
    return Array.from(set).sort((a, b) => a.localeCompare(b));
  }, [edges]);

  // Apply both filters. Empty filter strings pass through unconditionally;
  // when both are set the edges must satisfy BOTH (AND, not OR) — that's
  // how users isolate a specific project-to-project pair.
  const filteredEdges = useMemo(() => {
    if (!sourceFilter && !targetFilter) return edges;
    return edges.filter((e) => {
      const src = e.source_project_alias || `p${e.source_project_id}`;
      const dst = e.target_project_alias || `p${e.target_project_id}`;
      if (sourceFilter && src !== sourceFilter) return false;
      if (targetFilter && dst !== targetFilter) return false;
      return true;
    });
  }, [edges, sourceFilter, targetFilter]);

  useEffect(() => {
    if (!ref.current) return;
    let cancelled = false;
    // Re-initialize mermaid with the current theme palette before each
    // render so dark <-> light flips swap node fills/text without a reload.
    initMermaid(theme);
    // Unique id per render so mermaid doesn't choke on duplicate IDs in
    // dev StrictMode (first effect inserts SVG, second pass would collide).
    renderSeq.current += 1;
    const runId = `mmd_${Date.now()}_${renderSeq.current}`;

    const slug = (s: string) => s.replace(/[^a-zA-Z0-9]/g, "_");
    const esc = (s: string) =>
      s
        .replace(/&/g, "&amp;")
        .replace(/"/g, "&quot;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");

    const lines = ["graph LR"];
    for (const e of filteredEdges) {
      const src = e.source_project_alias || `p${e.source_project_id}`;
      const dst = e.target_project_alias || `p${e.target_project_id}`;
      const srcId = `n_${slug(src)}_${slug(e.source_bucket_id)}`;
      const dstId = `n_${slug(dst)}_${slug(e.target_bucket_id)}`;
      lines.push(
        `  ${srcId}["${esc(src)}<br/>${esc(e.source_bucket_id)}"] -->|${esc(e.sharing_type || "shared")}| ${dstId}["${esc(dst)}<br/>${esc(e.target_bucket_id)}"]`,
      );
    }
    const code = lines.join("\n");
    setMermaidCode(code);

    if (filteredEdges.length === 0) {
      // Avoid handing Mermaid an empty graph — it would render a single
      // placeholder node which is confusing in this "I filtered too hard"
      // context. Show our own empty-state instead.
      ref.current.innerHTML = "";
      setError(null);
      return;
    }

    mermaid
      .render(runId, code)
      .then(({ svg }) => {
        if (cancelled || !ref.current) return;
        // Mermaid's text-size guard does NOT throw — it returns a "soft
        // error" SVG with the failure rendered as a <text> element. Detect
        // the marker substring before mounting so the banner kicks in
        // instead of silently embedding a useless red box. (Confirmed
        // against mermaid 10.x output; future versions may shift wording
        // slightly, so we match a case-insensitive substring not the exact
        // phrase.)
        if (/maximum text size/i.test(svg)) {
          setError("Maximum text size in diagram exceeded");
          ref.current.innerHTML = "";
          return;
        }
        ref.current.innerHTML = svg;
        setError(null);
      })
      .catch((err) => {
        if (!cancelled) setError(String(err));
      });

    // Cleanup leftover off-DOM mermaid temp elements -- mermaid.render
    // creates a hidden <div id={runId}> sibling for measuring; without
    // this they accumulate.
    return () => {
      cancelled = true;
      const orphan = document.getElementById(runId);
      orphan?.remove();
    };
  }, [filteredEdges, theme]);

  if (edges.length === 0) {
    return (
      <div className="nerd-card text-center py-6 text-xs text-zinc-500">
        No edges to render.
      </div>
    );
  }

  // Mermaid trips its hardcoded text-size guard around ~50 KB of source.
  // The error message is "Maximum text size in diagram exceeded"; we also
  // accept a generic substring match in case the message wording shifts.
  const isOversize = !!error && /maximum text size/i.test(error);

  const filtered = !!sourceFilter || !!targetFilter;

  return (
    <div className="nerd-card">
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <h3 className="text-keboola font-bold text-sm">Diagram</h3>
        {/* Filter toolbar: two pickers + edge counter. Lets the user narrow
            the graph down to a specific source / target project pair so the
            embedded renderer never needs to fight a 250+-edge graph (#289). */}
        <div className="flex items-center gap-2 text-xs flex-wrap">
          <label className="flex items-center gap-1 text-zinc-500">
            <span className="text-[10px] uppercase tracking-wider">Source:</span>
            <select
              className="nerd-input text-xs py-0.5"
              value={sourceFilter}
              onChange={(e) => setSourceFilter(e.target.value)}
              title="Filter edges by source project"
            >
              <option value="">(all {sourceAliases.length})</option>
              {sourceAliases.map((a) => (
                <option key={a} value={a}>
                  {a}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-1 text-zinc-500">
            <span className="text-[10px] uppercase tracking-wider">Target:</span>
            <select
              className="nerd-input text-xs py-0.5"
              value={targetFilter}
              onChange={(e) => setTargetFilter(e.target.value)}
              title="Filter edges by target project"
            >
              <option value="">(all {targetAliases.length})</option>
              {targetAliases.map((a) => (
                <option key={a} value={a}>
                  {a}
                </option>
              ))}
            </select>
          </label>
          {filtered ? (
            <button
              type="button"
              className="nerd-btn text-xs hover:text-keboola"
              onClick={() => {
                setSourceFilter("");
                setTargetFilter("");
              }}
              title="Clear both filters"
            >
              clear
            </button>
          ) : null}
          <span className="text-zinc-500 text-[10px]">
            {filteredEdges.length}/{edges.length} edges
          </span>
        </div>
      </div>
      {isOversize ? (
        <OversizeBanner
          edgeCount={filteredEdges.length}
          mermaidCode={mermaidCode}
          onOpenDeepLineage={onOpenDeepLineage}
        />
      ) : error ? (
        <div className="text-red-600 text-xs mb-2 dark:text-red-400">{error}</div>
      ) : null}
      {!isOversize ? (
        filteredEdges.length === 0 ? (
          <div className="text-center py-6 text-xs text-zinc-500">
            No edges match the current filter.
          </div>
        ) : (
          // Fixed-height scrollable viewport so the diagram never pushes
          // the page layout — scrolling stays INSIDE the box (#289).
          // Mermaid renders the SVG with its natural size; we let it
          // overflow horizontally + vertically and the user pans inside.
          <div
            ref={ref}
            className="overflow-auto border border-zinc-200 dark:border-zinc-800 rounded bg-white dark:bg-zinc-950/40"
            style={{ height: "600px" }}
          />
        )
      ) : null}
    </div>
  );
}

function OversizeBanner({
  edgeCount,
  mermaidCode,
  onOpenDeepLineage,
}: {
  edgeCount: number;
  mermaidCode: string;
  onOpenDeepLineage: () => void;
}) {
  const downloadMermaid = () => {
    // Hand the user the raw Mermaid source so they can paste it into
    // mermaid.live or a local renderer. The embedded preview can't draw
    // it, but the source is small enough to ship and any external
    // renderer (which usually has much higher caps) handles it fine.
    const blob = new Blob([mermaidCode], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `lineage-sharing-${edgeCount}-edges.mmd`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  return (
    <div className="border border-neon-amber/40 bg-neon-amber/10 rounded p-4 text-sm space-y-3">
      <div className="font-bold text-amber-700 dark:text-neon-amber">
        Diagram too large to render here ({edgeCount} edges)
      </div>
      <p className="text-zinc-700 dark:text-zinc-300 text-xs leading-relaxed">
        Mermaid's embedded renderer caps source-text size and rejected this graph.
        Two ways forward — both stay in the UI, no shell required:
      </p>
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          onClick={onOpenDeepLineage}
          className="nerd-btn text-xs hover:text-keboola hover:border-keboola/60"
          title="Switch to the Deep Lineage tab — supports zoom, pan, search, and column-level drill-down."
        >
          → Open Deep Lineage tab
        </button>
        <button
          type="button"
          onClick={downloadMermaid}
          disabled={!mermaidCode}
          className="nerd-btn text-xs hover:text-keboola hover:border-keboola/60"
          title="Download the raw Mermaid source — paste into mermaid.live or any local renderer with higher size limits."
        >
          ⬇ Download Mermaid source ({(mermaidCode.length / 1024).toFixed(0)} KB)
        </button>
      </div>
      <p className="text-[11px] text-zinc-500">
        Tip: filter the edge table below first — narrowing the set often
        brings the diagram back under the embedded renderer's limit.
      </p>
    </div>
  );
}
