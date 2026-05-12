import { useQuery } from "@tanstack/react-query";
import mermaid from "mermaid";
import { useEffect, useId, useRef, useState } from "react";
import { api } from "../api/client";
import { Empty, ErrorBox, Loading, PageTitle } from "../components/Empty";
import { DataTable } from "../components/Table";
import type { LineageEdge, ProjectError, SharedBucket } from "../types";

interface LineageResp {
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
  const q = useQuery<LineageResp>({
    queryKey: ["lineage-edges"],
    queryFn: () => api.get("/lineage/edges"),
  });

  return (
    <div className="space-y-4">
      <PageTitle
        title="Cross-project lineage"
        description="Bucket-sharing graph across all registered Keboola projects."
      />
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
                  { header: "Source", cell: (e) => <span className="text-keboola">{e.source_project_alias || `#${e.source_project_id}`}</span> },
                  { header: "Source bucket", cell: (e) => <span className="text-accent">{e.source_bucket_id}</span> },
                  { header: "Type", cell: (e) => <span className="nerd-pill">{e.sharing_type}</span> },
                  { header: "Target", cell: (e) => <span className="text-keboola">{e.target_project_alias || `#${e.target_project_id}`}</span> },
                  { header: "Target bucket", cell: (e) => <span className="text-accent">{e.target_bucket_id}</span> },
                ]}
              />
            </>
          ) : (
            <Empty title="No bucket sharing between registered projects." />
          )}
        </>
      ) : null}
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
    const projects = new Set<string>();
    const slug = (s: string) => s.replace(/[^a-zA-Z0-9]/g, "_");
    const esc = (s: string) =>
      s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    for (const e of edges) {
      const src = e.source_project_alias || `p${e.source_project_id}`;
      const dst = e.target_project_alias || `p${e.target_project_id}`;
      projects.add(src);
      projects.add(dst);
      const srcId = `n_${slug(src)}_${slug(e.source_bucket_id)}`;
      const dstId = `n_${slug(dst)}_${slug(e.target_bucket_id)}`;
      const srcLabel = `${esc(src)}<br/>${esc(e.source_bucket_id)}`;
      const dstLabel = `${esc(dst)}<br/>${esc(e.target_bucket_id)}`;
      const edgeLabel = esc(e.sharing_type || "shared");
      lines.push(`  ${srcId}["${srcLabel}"] -->|${edgeLabel}| ${dstId}["${dstLabel}"]`);
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
