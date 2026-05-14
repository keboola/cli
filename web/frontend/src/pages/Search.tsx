import { useMutation } from "@tanstack/react-query";
import { Search as SearchIcon } from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import { ErrorBox, PageTitle } from "../components/Empty";
import { DataTable } from "../components/Table";

interface SearchResp {
  results: Array<{
    project_alias: string;
    type: string;
    name: string;
    id: string;
    component_id?: string;
    bucket_id?: string;
    description?: string;
  }>;
  stats: { results_found: number; projects_searched: number };
}

export function SearchPage() {
  const [query, setQuery] = useState("");
  const [searchType, setSearchType] = useState<"textual" | "config-based">("textual");
  const [types, setTypes] = useState<string[]>([]);
  const [result, setResult] = useState<SearchResp | null>(null);

  const mu = useMutation({
    mutationFn: () =>
      api.get<SearchResp>("/search", {
        query: {
          query,
          search_type: searchType,
          type: types.length ? types : undefined,
        },
      }),
    onSuccess: (data) => setResult(data),
  });

  return (
    <div className="space-y-4">
      <PageTitle title="Search" description="Global search across all registered projects." />
      <form
        className="nerd-card flex gap-2 flex-wrap"
        onSubmit={(e) => {
          e.preventDefault();
          if (query.trim()) mu.mutate();
        }}
      >
        <input
          className="nerd-input flex-1 min-w-[260px]"
          placeholder="search query..."
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <select
          className="nerd-input"
          value={searchType}
          onChange={(e) => setSearchType(e.target.value as "textual" | "config-based")}
        >
          <option value="textual">textual (name)</option>
          <option value="config-based">config-based (body)</option>
        </select>
        <select
          className="nerd-input"
          multiple
          size={3}
          value={types}
          onChange={(e) => setTypes(Array.from(e.target.selectedOptions, (o) => o.value))}
        >
          <option value="bucket">bucket</option>
          <option value="table">table</option>
          <option value="config">config</option>
          <option value="flow">flow</option>
          <option value="data-app">data-app</option>
          <option value="transformation">transformation</option>
        </select>
        <button type="submit" className="nerd-btn flex items-center gap-1 hover:text-keboola">
          <SearchIcon className="w-3 h-3" /> {mu.isPending ? "searching..." : "Search"}
        </button>
      </form>
      {mu.error ? <ErrorBox message={(mu.error as Error).message} /> : null}
      {result ? (
        <>
          <div className="text-xs text-zinc-500">
            {result.stats.results_found} hit(s) across {result.stats.projects_searched} project(s)
          </div>
          <DataTable
            rows={result.results}
            rowKey={(r) => `${r.project_alias}/${r.type}/${r.id}`}
            columns={[
              { header: "Project", cell: (r) => <span className="text-keboola">{r.project_alias}</span> },
              { header: "Type", cell: (r) => <span className="nerd-pill">{r.type}</span> },
              { header: "Name", cell: (r) => <span className="font-bold">{r.name}</span> },
              { header: "ID", cell: (r) => <span className="text-zinc-500">{r.id}</span> },
              { header: "Component", cell: (r) => r.component_id ?? "" },
            ]}
          />
        </>
      ) : null}
    </div>
  );
}
