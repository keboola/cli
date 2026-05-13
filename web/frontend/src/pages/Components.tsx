import { useQuery } from "@tanstack/react-query";
import { Sparkles } from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import { Drawer } from "../components/Drawer";
import { ErrorBox, Loading, PageTitle } from "../components/Empty";
import { JsonView } from "../components/JsonView";
import { useUIState } from "../state";
import type { Component } from "../types";

interface ComponentsResp {
  components: Component[];
  errors: Array<Record<string, unknown>>;
}

const PROMPT_HINTS = [
  "I need to download data from marketing campaigns",
  "I want to write data to BigQuery",
  "I need a Snowflake transformation",
  "Connect to Salesforce CRM",
  "Schedule daily exports to S3",
];

const TYPE_FILTERS: Array<{ id: string; label: string }> = [
  { id: "", label: "All" },
  { id: "extractor", label: "Data Source" },
  { id: "writer", label: "Data Destination" },
  { id: "application", label: "Application" },
  { id: "transformation", label: "Transformation" },
];

export function ComponentsPage() {
  const { project } = useUIState();
  const [query, setQuery] = useState("");
  const [type, setType] = useState("");
  const [selected, setSelected] = useState<Component | null>(null);
  const [hintIdx, setHintIdx] = useState(0);

  const q = useQuery<ComponentsResp>({
    queryKey: ["components", project, query, type],
    queryFn: () =>
      api.get("/components", {
        query: {
          project: project ?? undefined,
          query: query || undefined,
          type: type || undefined,
        },
      }),
    enabled: !!project,
  });

  const components = q.data?.components ?? [];

  return (
    <div className="space-y-4">
      <PageTitle
        title="Components"
        description={`${components.length} component(s) available in ${project ?? "(no project)"}. Type a use-case below — the search runs against the AI Service when filled, falls back to Storage catalog otherwise.`}
      />

      {/* Big AI-style search input -- the Components page is a discovery page,
          this is the primary action so it deserves the screen real estate. */}
      <div className="nerd-card border-keboola/30 bg-zinc-900/40">
        <div className="flex items-center gap-3">
          <Sparkles className="w-5 h-5 text-keboola flex-shrink-0" />
          <input
            className="flex-1 bg-transparent border-0 focus:outline-none text-sm font-mono placeholder-zinc-600"
            placeholder={PROMPT_HINTS[hintIdx]}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onFocus={() => setHintIdx((i) => (i + 1) % PROMPT_HINTS.length)}
          />
          {query ? (
            <button
              type="button"
              className="text-xs text-zinc-500 hover:text-zinc-200"
              onClick={() => setQuery("")}
            >
              clear
            </button>
          ) : null}
        </div>
        <div className="mt-3 flex flex-wrap gap-1.5">
          {PROMPT_HINTS.map((h) => (
            <button
              key={h}
              type="button"
              className="nerd-pill hover:border-keboola hover:text-keboola text-[10px]"
              onClick={() => setQuery(h)}
            >
              {h}
            </button>
          ))}
        </div>
      </div>

      <div className="flex gap-2 flex-wrap">
        {TYPE_FILTERS.map((t) => (
          <button
            key={t.id || "all"}
            type="button"
            className={`nerd-btn text-xs ${
              type === t.id ? "border-keboola text-keboola" : ""
            }`}
            onClick={() => setType(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {q.isLoading ? <Loading /> : null}
      {q.error ? <ErrorBox message={(q.error as Error).message} /> : null}

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
        {components.map((c) => (
          <button
            key={c.component_id}
            type="button"
            onClick={() => setSelected(c)}
            className="nerd-card text-left hover:border-keboola/50 transition-colors"
          >
            <div className="flex items-start justify-between mb-1">
              <div className="font-bold text-accent text-sm">{c.component_name}</div>
              <span className="nerd-pill text-[10px]">{c.component_type}</span>
            </div>
            <div className="text-[11px] text-zinc-600 font-mono mb-2">
              {c.component_id}
            </div>
            <div className="text-xs text-zinc-500 line-clamp-3">
              {c.description ?? ""}
            </div>
          </button>
        ))}
      </div>

      {selected ? (
        <Drawer
          open={true}
          onClose={() => setSelected(null)}
          title={selected.component_name}
          subtitle={selected.component_id}
          width="max-w-3xl"
        >
          <ComponentDetail componentId={selected.component_id} />
        </Drawer>
      ) : null}
    </div>
  );
}

function ComponentDetail({ componentId }: { componentId: string }) {
  const { project } = useUIState();
  const q = useQuery({
    queryKey: ["component-detail", componentId, project],
    queryFn: () =>
      api.get(`/components/${encodeURIComponent(componentId)}`, {
        query: { project: project ?? undefined },
      }),
  });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox message={(q.error as Error).message} />;
  return <JsonView data={q.data} />;
}
