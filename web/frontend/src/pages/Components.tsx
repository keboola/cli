import { useQuery } from "@tanstack/react-query";
import { BookOpen, Boxes, FileJson, Info, Sparkles } from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api } from "../api/client";
import { Drawer } from "../components/Drawer";
import { ErrorBox, Loading, PageTitle } from "../components/Empty";
import { KeyValueGrid } from "../components/KeyValueGrid";
import { PillList } from "../components/PillList";
import { RawDetail } from "../components/RawDetail";
import { useUIState } from "../state";
import type { Component } from "../types";
import { useHashSelection } from "../useHashSelection";

// Module-level so the array identity is stable across renders (a fresh
// literal would re-run react-markdown's plugin pipeline on every render).
const MARKDOWN_PLUGINS = [remarkGfm];

interface ComponentsResp {
  components: Component[];
  errors: Array<Record<string, unknown>>;
}

/**
 * `GET /components/{id}` — mirrors `ComponentService.get_component_detail`.
 *
 * Every field is optional on purpose: the payload is assembled from EITHER
 * the AI Service documentation index or (on a 404 there) the project's own
 * Storage component catalog, and the catalog path fills the AI-only fields
 * with empty values. `documentation_source` is the discriminator that tells
 * the two apart — nothing else has to branch on it.
 */
interface ComponentDetailPayload {
  component_id?: string;
  component_name?: string;
  component_type?: string;
  categories?: string[];
  flags?: string[];
  description?: string;
  long_description?: string;
  documentation_url?: string;
  schema_summary?: {
    property_count?: number;
    required_count?: number;
    has_row_schema?: boolean;
  };
  examples_count?: number;
  row_examples_count?: number;
  project_alias?: string;
  documentation_source?: string;
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
  // Deep link: `?sel=<component_id>` opens that component's detail drawer.
  const [sel, setSel] = useHashSelection();
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

  // Restore a deep-linked selection ONCE, after the first list load. Guarded
  // by a ref rather than by `selected` so closing the drawer does not re-open
  // it when the list refetches (typing in the search box refetches constantly).
  const restoredRef = useRef(false);
  useEffect(() => {
    if (restoredRef.current) return;
    if (!sel || !project) {
      restoredRef.current = true;
      return;
    }
    if (q.isLoading) return;
    restoredRef.current = true;
    const hit = components.find((c) => c.component_id === sel);
    // A shared link can point at a component the current filter hides (or one
    // this project cannot see at all). Nothing to open, so drop the stale id
    // rather than leaving `?sel=` pointing at a drawer that never appears.
    if (hit) setSelected(hit);
    else setSel(null);
  }, [sel, project, q.isLoading, components, setSel]);

  const openComponent = (c: Component) => {
    setSelected(c);
    setSel(c.component_id);
  };
  const closeComponent = () => {
    setSelected(null);
    setSel(null);
  };

  return (
    <div className="space-y-4">
      <PageTitle
        title="Components"
        description={`${components.length} component(s) available in ${project ?? "(no project)"}. Type a use-case below — the search runs against the AI Service when filled, falls back to Storage catalog otherwise.`}
      />

      {/* Big AI-style search input -- the Components page is a discovery page,
          this is the primary action so it deserves the screen real estate. */}
      <div className="nerd-card border-keboola/30 bg-keboola/5 dark:bg-zinc-900/40">
        <div className="flex items-center gap-3">
          <Sparkles className="w-5 h-5 text-keboola flex-shrink-0" />
          <input
            className="flex-1 bg-transparent border-0 focus:outline-none text-sm font-mono placeholder-zinc-400 dark:placeholder-zinc-600"
            placeholder={PROMPT_HINTS[hintIdx]}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onFocus={() => setHintIdx((i) => (i + 1) % PROMPT_HINTS.length)}
          />
          {query ? (
            <button
              type="button"
              className="text-xs text-zinc-500 hover:text-zinc-900 dark:hover:text-zinc-200"
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
            onClick={() => openComponent(c)}
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
          onClose={closeComponent}
          title={selected.component_name}
          subtitle={selected.component_id}
          wide
        >
          <ComponentDetail componentId={selected.component_id} />
        </Drawer>
      ) : null}
    </div>
  );
}

function ComponentDetail({ componentId }: { componentId: string }) {
  const { project } = useUIState();
  const q = useQuery<ComponentDetailPayload>({
    queryKey: ["component-detail", componentId, project],
    queryFn: () =>
      api.get(`/components/${encodeURIComponent(componentId)}`, {
        query: { project: project ?? undefined },
      }),
  });
  if (q.isLoading) return <Loading />;
  if (q.error) {
    const message = (q.error as Error).message;
    return (
      <div className="space-y-2">
        <ErrorBox message={message} />
        {/not found/i.test(message) ? (
          <div className="text-xs text-zinc-500">
            Neither the AI Service documentation index nor this project's Storage component
            catalog knows <span className="font-mono text-accent">{componentId}</span>. Check the
            id, or switch to a project that has the component enabled.
          </div>
        ) : null}
      </div>
    );
  }
  if (!q.data) return null;
  return <RawDetail data={q.data} overview={<ComponentOverview detail={q.data} />} />;
}

function ComponentOverview({ detail }: { detail: ComponentDetailPayload }) {
  const schema = detail.schema_summary ?? {};
  const fromCatalog = detail.documentation_source === "storage_catalog";

  return (
    <div className="space-y-4">
      {/* The fallback is invisible in the payload's other fields (they are
          simply empty), so say it out loud — otherwise a component with no AI
          Service entry looks like one that ships no documentation at all. */}
      {fromCatalog ? (
        <div className="flex items-start gap-1.5 text-xs text-zinc-500">
          <Info className="w-3 h-3 shrink-0 mt-0.5" />
          <span>
            AI Service has no documentation for this component — showing the project's Storage
            catalog entry.
          </span>
        </div>
      ) : null}

      <Section icon={<Boxes className="w-3.5 h-3.5" />} label="Component">
        <KeyValueGrid
          columns={3}
          items={[
            { label: "Component ID", value: detail.component_id, mono: true },
            { label: "Name", value: detail.component_name },
            { label: "Type", value: detail.component_type },
            { label: "Project", value: detail.project_alias, mono: true },
            {
              label: "Documentation",
              value: detail.documentation_url ? (
                <a
                  href={detail.documentation_url}
                  target="_blank"
                  rel="noreferrer"
                  className="hover:underline"
                >
                  {detail.documentation_url}
                </a>
              ) : (
                ""
              ),
              mono: true,
            },
            { label: "Documentation source", value: detail.documentation_source, mono: true },
          ]}
        />
        {detail.description ? (
          <p className="text-xs text-zinc-600 mt-3 dark:text-zinc-400">{detail.description}</p>
        ) : null}
      </Section>

      <Section
        icon={<FileJson className="w-3.5 h-3.5" />}
        label="Configuration schema & examples"
      >
        <KeyValueGrid
          columns={4}
          items={[
            {
              label: "Schema properties",
              value: schema.property_count != null ? String(schema.property_count) : "",
              mono: true,
            },
            {
              label: "Required",
              value: schema.required_count != null ? String(schema.required_count) : "",
              mono: true,
            },
            {
              label: "Row schema",
              value: schema.has_row_schema ? (
                <span className="nerd-pill-green text-[10px]">yes</span>
              ) : (
                <span className="nerd-pill text-[10px]">no</span>
              ),
            },
            {
              label: "Root examples",
              value: detail.examples_count != null ? String(detail.examples_count) : "",
              mono: true,
            },
            {
              label: "Row examples",
              value: detail.row_examples_count != null ? String(detail.row_examples_count) : "",
              mono: true,
            },
          ]}
        />
      </Section>

      <Section label={`Categories (${detail.categories?.length ?? 0})`}>
        <PillList items={detail.categories} empty="No categories." />
      </Section>

      <Section label={`Flags (${detail.flags?.length ?? 0})`}>
        <PillList items={detail.flags} empty="No flags." />
      </Section>

      {detail.long_description ? (
        <Section icon={<BookOpen className="w-3.5 h-3.5" />} label="Documentation">
          <div className="markdown-body text-xs">
            <ReactMarkdown remarkPlugins={MARKDOWN_PLUGINS}>
              {detail.long_description}
            </ReactMarkdown>
          </div>
        </Section>
      ) : null}
    </div>
  );
}

/** Card with the icon + micro-label header used by the other detail drawers. */
function Section({
  icon,
  label,
  children,
}: {
  icon?: ReactNode;
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="nerd-card">
      <div className="text-[10px] uppercase tracking-wider text-zinc-500 flex items-center gap-1 mb-2">
        {icon}
        {label}
      </div>
      {children}
    </div>
  );
}
