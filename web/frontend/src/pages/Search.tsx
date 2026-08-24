import { useMutation } from "@tanstack/react-query";
import {
  AppWindow,
  Code2,
  CornerDownLeft,
  Database,
  Search as SearchIcon,
  Settings2,
  Table2,
  TriangleAlert,
  Workflow,
} from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api/client";
import { ErrorBox, Loading, PageTitle } from "../components/Empty";
import { DataTable } from "../components/Table";
import { useUIState } from "../state";

interface SearchResult {
  project_alias: string;
  /** Raw Storage API type: bucket | table | flow | transformation | configuration | configuration-row. */
  type: string;
  name: string;
  id: string;
  component_id?: string | null;
  bucket_id?: string;
  description?: string;
  /** Table results matched via a column name carry the matching columns. */
  matched_columns?: string[];
}

interface SearchResp {
  results: SearchResult[];
  /** Per-project failures (e.g. FEATURE_NOT_ENABLED) -- the fan-out never aborts. */
  errors?: Array<{ project_alias: string; error_code?: string; message: string }>;
  stats: { results_found: number; projects_searched: number };
}

type SearchMode = "textual" | "config-based";

/** Filter values understood by the API's `type` param, in reading order. */
const TYPE_FILTERS: Array<{
  value: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
}> = [
  { value: "table", label: "tables", icon: Table2 },
  { value: "bucket", label: "buckets", icon: Database },
  { value: "config", label: "configs", icon: Settings2 },
  { value: "flow", label: "flows", icon: Workflow },
  { value: "transformation", label: "transformations", icon: Code2 },
  { value: "data-app", label: "data apps", icon: AppWindow },
];

/**
 * Presentation for the RAW result type the API returns. Filter names and
 * result types are different vocabularies: a `data-app` filter comes back as
 * `configuration` whose component is keboola.data-apps, so that special case
 * is resolved in `resultKind` below rather than in this table.
 */
const RESULT_KINDS: Record<
  string,
  { label: string; icon: React.ComponentType<{ className?: string }> }
> = {
  bucket: { label: "bucket", icon: Database },
  table: { label: "table", icon: Table2 },
  flow: { label: "flow", icon: Workflow },
  transformation: { label: "transformation", icon: Code2 },
  configuration: { label: "config", icon: Settings2 },
  "configuration-row": { label: "config row", icon: Settings2 },
};

const DATA_APP_COMPONENT_ID = "keboola.data-apps";

function resultKind(r: SearchResult): { label: string; icon: React.ComponentType<{ className?: string }> } {
  if (r.type === "configuration" && r.component_id === DATA_APP_COMPONENT_ID) {
    return { label: "data app", icon: AppWindow };
  }
  return RESULT_KINDS[r.type] ?? { label: r.type, icon: Settings2 };
}

/**
 * Deep-link target for one result, expressed in the owning page's `?sel=`
 * grammar (same contract the command palette uses: the page owns the meaning
 * of its selection, this page only picks a target). `null` = not navigable
 * (a configuration-row has no stable parent-config link in the result).
 */
function navTarget(r: SearchResult): { page: "storage" | "configs" | "flows"; sel: string | null } | null {
  switch (r.type) {
    case "bucket":
      return { page: "storage", sel: `bucket/${r.id}` };
    case "table":
      return { page: "storage", sel: `tables/${r.id}` };
    case "flow":
      return { page: "flows", sel: r.id };
    case "transformation":
    case "configuration":
      return r.component_id ? { page: "configs", sel: `${r.component_id}/${r.id}` } : null;
    default:
      return null;
  }
}

export function SearchPage() {
  const {
    pendingSearchQuery,
    setPendingSearchQuery,
    project,
    setProject,
    setBranchId,
    setPage,
    setSel,
  } = useUIState();
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<SearchMode>("textual");
  const [types, setTypes] = useState<string[]>([]);
  const [result, setResult] = useState<SearchResp | null>(null);
  /** The query the visible result set was produced by (for the header/empty copy). */
  const [ranQuery, setRanQuery] = useState("");

  // The mutation takes the query as an argument (rather than closing over
  // `query` state) so the hand-off effect below can fire it with a value
  // that bypasses the async setState -- an immediate mu.mutate() right
  // after setQuery(q) would otherwise still see the stale (pre-update)
  // query state, same stale-closure trap the LocalAi consumer documents.
  const mu = useMutation({
    mutationFn: (q: string) =>
      api.get<SearchResp>("/search", {
        query: {
          query: q,
          search_type: mode,
          type: types.length ? types : undefined,
        },
      }),
    onSuccess: (data, q) => {
      setResult(data);
      setRanQuery(q);
    },
  });

  // Hand-off slot from the command palette's "Search '...' across projects"
  // escape row: adopt the query into local state, clear the slot so a
  // remount can't re-fire, and run the search immediately with the value
  // passed directly.
  useEffect(() => {
    if (!pendingSearchQuery) return;
    const q = pendingSearchQuery;
    setQuery(q);
    setPendingSearchQuery(null);
    mu.mutate(q);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingSearchQuery]);

  const toggleType = (value: string) =>
    setTypes((prev) => (prev.includes(value) ? prev.filter((t) => t !== value) : [...prev, value]));

  /**
   * Navigate to the result's home page. ORDER MATTERS (same rule as the
   * command palette's openStorage): setProject / setBranchId / setPage each
   * clear `sel`, so the selection has to be written LAST.
   */
  const openResult = (r: SearchResult) => {
    const target = navTarget(r);
    if (!target) return;
    if (r.project_alias !== project) {
      setProject(r.project_alias);
      // A branch id is only meaningful inside its own project.
      setBranchId(null);
    }
    setPage(target.page);
    setSel(target.sel);
  };

  const errors = result?.errors ?? [];
  // Group per-project failures by message: with dozens of registered projects
  // the same expired-session text repeats for most of them, and one row per
  // project would drown the results. One row per DISTINCT message, carrying
  // the affected project aliases, stays readable at any fleet size.
  const errorGroups = new Map<string, string[]>();
  for (const e of errors) {
    const group = errorGroups.get(e.message);
    if (group) group.push(e.project_alias);
    else errorGroups.set(e.message, [e.project_alias]);
  }

  return (
    <div className="space-y-4">
      <PageTitle title="Search" description="Global search across all registered projects." />

      <form
        className="nerd-card space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          if (query.trim()) mu.mutate(query.trim());
        }}
      >
        <div className="flex gap-2 flex-wrap items-center">
          <div className="relative flex-1 min-w-[260px]">
            <SearchIcon className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-zinc-400 pointer-events-none" />
            <input
              className="nerd-input w-full pl-9 pr-9 py-2.5"
              placeholder={mode === "textual" ? "Search by name…" : "Search configuration bodies…"}
              value={query}
              autoFocus
              onChange={(e) => setQuery(e.target.value)}
            />
            {query.trim() && !mu.isPending ? (
              <CornerDownLeft className="w-3.5 h-3.5 absolute right-3 top-1/2 -translate-y-1/2 text-zinc-400 pointer-events-none" />
            ) : null}
          </div>

          <div
            className="flex rounded border border-zinc-300 dark:border-zinc-700 overflow-hidden text-xs shrink-0"
            role="radiogroup"
            aria-label="Search mode"
          >
            <button
              type="button"
              role="radio"
              aria-checked={mode === "textual"}
              title="Match entity names (fast, uses the global-search index)"
              onClick={() => setMode("textual")}
              className={`px-3 py-2 transition-colors ${
                mode === "textual"
                  ? "bg-keboola/10 text-keboola"
                  : "text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300"
              }`}
            >
              names
            </button>
            <button
              type="button"
              role="radio"
              aria-checked={mode === "config-based"}
              title="Scan configuration JSON bodies (slower, works without the global-search feature)"
              onClick={() => setMode("config-based")}
              className={`px-3 py-2 border-l border-zinc-300 dark:border-zinc-700 transition-colors ${
                mode === "config-based"
                  ? "bg-keboola/10 text-keboola"
                  : "text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300"
              }`}
            >
              config bodies
            </button>
          </div>

          <button
            type="submit"
            disabled={!query.trim() || mu.isPending}
            className="nerd-btn flex items-center gap-1.5 py-2 shrink-0 hover:text-keboola disabled:opacity-50 disabled:hover:border-zinc-300 disabled:hover:text-inherit dark:disabled:hover:border-zinc-700"
          >
            <SearchIcon className="w-3 h-3" /> {mu.isPending ? "Searching…" : "Search"}
          </button>
        </div>

        <div className="flex gap-1.5 flex-wrap items-center">
          <span className="text-[10px] uppercase tracking-wider text-zinc-400 mr-1">Types</span>
          <button
            type="button"
            onClick={() => setTypes([])}
            className={types.length === 0 ? "nerd-pill-green" : "nerd-pill hover:border-zinc-400 dark:hover:border-zinc-500 transition-colors"}
          >
            all
          </button>
          {TYPE_FILTERS.map(({ value, label, icon: Icon }) => (
            <button
              key={value}
              type="button"
              onClick={() => toggleType(value)}
              className={
                types.includes(value)
                  ? "nerd-pill-green"
                  : "nerd-pill hover:border-zinc-400 dark:hover:border-zinc-500 transition-colors"
              }
            >
              <Icon className="w-3 h-3" /> {label}
            </button>
          ))}
        </div>
      </form>

      {mu.error ? <ErrorBox message={(mu.error as Error).message} /> : null}
      {mu.isPending ? <Loading label="searching all registered projects…" /> : null}

      {errors.length > 0 ? (
        <details className="nerd-card border-neon-amber/50 dark:border-neon-amber/40 text-xs">
          <summary className="flex items-center gap-2 cursor-pointer select-none list-none [&::-webkit-details-marker]:hidden">
            <TriangleAlert className="w-3.5 h-3.5 text-amber-700 dark:text-neon-amber shrink-0" />
            <span className="text-zinc-700 dark:text-zinc-300">
              {errors.length} {errors.length === 1 ? "project" : "projects"} skipped
            </span>
            <span className="text-zinc-500">— expand for details</span>
          </summary>
          <div className="mt-3 space-y-3">
            {[...errorGroups.entries()].map(([message, aliases]) => (
              <div key={message}>
                <div className="text-zinc-600 dark:text-zinc-400">{message}</div>
                <div className="flex gap-1 flex-wrap mt-1.5">
                  {aliases.map((a) => (
                    <span key={a} className="nerd-pill">
                      {a}
                    </span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </details>
      ) : null}

      {result && !mu.isPending ? (
        result.results.length > 0 ? (
          <>
            <div className="text-xs text-zinc-500">
              <span className="text-keboola font-bold">{result.stats.results_found}</span>{" "}
              {result.stats.results_found === 1 ? "hit" : "hits"} for{" "}
              <span className="text-zinc-700 dark:text-zinc-300">"{ranQuery}"</span> across{" "}
              {result.stats.projects_searched}{" "}
              {result.stats.projects_searched === 1 ? "project" : "projects"}
            </div>
            <DataTable
              rows={result.results}
              rowKey={(r) => `${r.project_alias}/${r.type}/${r.id}`}
              onRowClick={openResult}
              columns={[
                {
                  header: "Type",
                  width: "10rem",
                  cell: (r) => {
                    const kind = resultKind(r);
                    const Icon = kind.icon;
                    return (
                      <span className="nerd-pill whitespace-nowrap">
                        <Icon className="w-3 h-3" /> {kind.label}
                      </span>
                    );
                  },
                },
                {
                  header: "Name",
                  cell: (r) => (
                    <div>
                      <div className="font-bold">{r.name}</div>
                      {r.matched_columns?.length ? (
                        <div className="text-[10px] text-zinc-500 mt-0.5">
                          matched columns: {r.matched_columns.join(", ")}
                        </div>
                      ) : r.description ? (
                        <div className="text-[10px] text-zinc-500 mt-0.5 line-clamp-1">
                          {r.description}
                        </div>
                      ) : null}
                    </div>
                  ),
                },
                {
                  header: "ID",
                  cell: (r) => <span className="text-xs text-zinc-500">{r.id}</span>,
                },
                {
                  header: "Component",
                  cell: (r) => <span className="text-xs text-zinc-500">{r.component_id ?? ""}</span>,
                },
                {
                  header: "Project",
                  width: "10rem",
                  cell: (r) => <span className="text-keboola">{r.project_alias}</span>,
                },
              ]}
            />
          </>
        ) : (
          <div className="nerd-card text-center py-10">
            <div className="text-zinc-700 text-sm dark:text-zinc-300">
              {result.stats.projects_searched === 0
                ? "No project could be searched"
                : `No matches for "${ranQuery}"`}
            </div>
            <div className="text-zinc-500 text-xs mt-2">
              {result.stats.projects_searched === 0
                ? "Every registered project was skipped — expand the notice above to see why."
                : mode === "textual"
                  ? "Names are matched as substrings. Try a shorter term, clear the type filter, or switch to config bodies to scan configuration JSON."
                  : "Config-based search scans configuration bodies. Try a shorter term or clear the type filter."}
            </div>
          </div>
        )
      ) : null}

      {!result && !mu.isPending && !mu.error ? (
        <div className="nerd-card text-center py-14">
          <SearchIcon className="w-8 h-8 mx-auto text-zinc-300 dark:text-zinc-700" />
          <div className="text-zinc-700 text-sm mt-4 dark:text-zinc-300">
            Search every registered project at once
          </div>
          <div className="text-zinc-500 text-xs mt-2 max-w-md mx-auto">
            Tables, buckets, configs, flows, transformations and data apps — matched by name.
            Results open straight in their home page.
          </div>
        </div>
      ) : null}
    </div>
  );
}
