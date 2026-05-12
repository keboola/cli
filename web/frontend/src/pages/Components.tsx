import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { ErrorBox, Loading, PageTitle } from "../components/Empty";
import { JsonView } from "../components/JsonView";
import { DataTable } from "../components/Table";
import { useUIState } from "../state";
import type { Component } from "../types";

interface ComponentsResp {
  components: Component[];
  errors: Array<Record<string, unknown>>;
}

export function ComponentsPage() {
  const { project } = useUIState();
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Component | null>(null);
  const q = useQuery<ComponentsResp>({
    queryKey: ["components", project, query],
    queryFn: () =>
      api.get("/components", { query: { project: project ?? undefined, query: query || undefined } }),
    enabled: !!project,
  });
  return (
    <div className="space-y-4">
      <PageTitle title="Components" description="Catalog of available Keboola components." />
      <input
        className="nerd-input w-full max-w-md"
        placeholder="search components (AI-assisted)..."
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      {q.isLoading ? <Loading /> : null}
      {q.error ? <ErrorBox message={(q.error as Error).message} /> : null}
      <DataTable
        rows={q.data?.components ?? []}
        rowKey={(c) => c.component_id}
        onRowClick={(c) => setSelected(c)}
        columns={[
          { header: "ID", cell: (c) => <span className="text-accent">{c.component_id}</span> },
          { header: "Name", cell: (c) => c.component_name },
          { header: "Type", cell: (c) => <span className="text-zinc-500">{c.component_type}</span> },
        ]}
      />
      {selected ? (
        <div className="nerd-card">
          <div className="flex items-center justify-between mb-3">
            <h3 className="font-bold text-keboola">{selected.component_id}</h3>
            <button type="button" className="nerd-btn text-xs" onClick={() => setSelected(null)}>
              Close
            </button>
          </div>
          <ComponentDetail componentId={selected.component_id} />
        </div>
      ) : null}
    </div>
  );
}

function ComponentDetail({ componentId }: { componentId: string }) {
  const { project } = useUIState();
  const q = useQuery({
    queryKey: ["component-detail", componentId, project],
    queryFn: () =>
      api.get(`/components/${encodeURIComponent(componentId)}`, { query: { project: project ?? undefined } }),
  });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox message={(q.error as Error).message} />;
  return <JsonView data={q.data} />;
}
