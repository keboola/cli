import { Editor } from "@monaco-editor/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Play, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import { Empty, ErrorBox, Loading, PageTitle } from "../components/Empty";
import { DataTable } from "../components/Table";
import { useUIState } from "../state";
import type { ProjectError, Workspace } from "../types";

interface WorkspacesResp {
  workspaces: Workspace[];
  errors: ProjectError[];
}

export function WorkspacesPage() {
  const { project } = useUIState();
  const qc = useQueryClient();
  const [selected, setSelected] = useState<Workspace | null>(null);

  const q = useQuery<WorkspacesResp>({
    queryKey: ["workspaces", project],
    queryFn: () => api.get("/workspaces", { query: { project: project ?? undefined } }),
    enabled: !!project,
  });

  const createMu = useMutation({
    mutationFn: () =>
      api.post<Workspace>(`/workspaces/${encodeURIComponent(project!)}`, {
        name: "",
        read_only: false,
      }),
    onSuccess: (ws) => {
      qc.invalidateQueries({ queryKey: ["workspaces"] });
      setSelected(ws);
    },
  });

  const deleteMu = useMutation({
    mutationFn: ({ alias, id }: { alias: string; id: number }) =>
      api.delete(`/workspaces/${encodeURIComponent(alias)}/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["workspaces"] });
      setSelected(null);
    },
  });

  return (
    <div className="space-y-4">
      <PageTitle
        title="SQL Workspaces"
        description={`Run SQL against ${project ?? "(no project)"} via Query Service.`}
        actions={
          <button
            type="button"
            className="nerd-btn flex items-center gap-1 hover:text-keboola"
            disabled={!project || createMu.isPending}
            onClick={() => createMu.mutate()}
          >
            <Plus className="w-3 h-3" /> {createMu.isPending ? "creating..." : "New workspace"}
          </button>
        }
      />
      {!project ? (
        <Empty title="Select a project" />
      ) : q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox message={(q.error as Error).message} />
      ) : (
        <DataTable
          rows={q.data?.workspaces ?? []}
          rowKey={(w) => `${w.project_alias}/${w.id}`}
          onRowClick={(w) => setSelected(w)}
          columns={[
            { header: "Name", cell: (w) => <span className="font-bold text-accent">{w.name}</span> },
            { header: "ID", cell: (w) => <span className="text-zinc-500">{w.id}</span> },
            { header: "Backend", cell: (w) => w.backend },
            { header: "Schema", cell: (w) => <span className="text-zinc-400">{w.schema}</span> },
            { header: "Created", cell: (w) => <span className="text-xs text-zinc-500">{w.created}</span> },
            {
              header: "",
              align: "right",
              cell: (w) => (
                <button
                  type="button"
                  className="nerd-btn text-xs hover:text-red-400 hover:border-red-700"
                  onClick={(e) => {
                    e.stopPropagation();
                    if (confirm(`Delete workspace ${w.name}?`))
                      deleteMu.mutate({ alias: w.project_alias, id: w.id });
                  }}
                >
                  <Trash2 className="w-3 h-3" />
                </button>
              ),
            },
          ]}
        />
      )}
      {selected ? <SqlEditor workspace={selected} onClose={() => setSelected(null)} /> : null}
    </div>
  );
}

function SqlEditor({ workspace, onClose }: { workspace: Workspace; onClose: () => void }) {
  const [sql, setSql] = useState("SELECT current_timestamp() AS now;");
  const [result, setResult] = useState<unknown | null>(null);
  const [error, setError] = useState<string | null>(null);

  const runMu = useMutation({
    mutationFn: () =>
      api.post(`/workspaces/${encodeURIComponent(workspace.project_alias)}/${workspace.id}/query`, {
        sql,
      }),
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
        <h3 className="font-bold text-keboola">
          SQL ・ {workspace.name} ・ {workspace.backend} ・ {workspace.schema}
        </h3>
        <div className="flex gap-2">
          <button
            type="button"
            className="nerd-btn flex items-center gap-1 hover:text-keboola"
            onClick={() => runMu.mutate()}
            disabled={runMu.isPending}
          >
            <Play className="w-3 h-3" /> {runMu.isPending ? "running..." : "Run (⌘↵)"}
          </button>
          <button type="button" className="nerd-btn text-xs" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
      <div className="border border-zinc-800 rounded">
        <Editor
          height="280px"
          language="sql"
          theme="vs-dark"
          value={sql}
          onChange={(v) => setSql(v ?? "")}
          options={{
            fontSize: 13,
            minimap: { enabled: false },
            scrollBeyondLastLine: false,
            wordWrap: "on",
          }}
        />
      </div>
      {error ? (
        <div className="mt-3 nerd-card border-red-700/40 text-red-400 text-sm">{error}</div>
      ) : null}
      {result ? <SqlResults result={result} /> : null}
    </div>
  );
}

function SqlResults({ result }: { result: unknown }) {
  const data = result as {
    statements?: Array<{
      statement_id: string;
      status: string;
      rows_affected: number;
      csv_data?: string;
    }>;
  };
  const statements = data.statements ?? [];
  return (
    <div className="mt-4 space-y-3">
      {statements.map((stmt, i) => (
        <div key={stmt.statement_id ?? i} className="border border-zinc-800 rounded">
          <div className="px-3 py-2 border-b border-zinc-800 text-xs flex justify-between">
            <span>Statement {i + 1} ・ {stmt.status} ・ {stmt.rows_affected} rows</span>
          </div>
          {stmt.csv_data ? (
            <CsvTable csv={stmt.csv_data} />
          ) : (
            <div className="px-3 py-2 text-xs text-zinc-500">No result data.</div>
          )}
        </div>
      ))}
    </div>
  );
}

function CsvTable({ csv }: { csv: string }) {
  const rows = csv
    .trim()
    .split("\n")
    .map((line) => line.split(",").map((c) => c.replace(/^"|"$/g, "")));
  if (rows.length === 0) return null;
  const [header, ...body] = rows;
  return (
    <div className="overflow-auto" style={{ maxHeight: 300 }}>
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b border-zinc-800 text-zinc-500">
            {header.map((h, i) => (
              <th key={i} className="px-2 py-1 text-left font-normal">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {body.slice(0, 100).map((r, i) => (
            <tr key={i} className="border-b border-zinc-900/50">
              {r.map((c, j) => (
                <td key={j} className="px-2 py-1">{c}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {body.length > 100 ? (
        <div className="text-xs text-zinc-500 px-3 py-1">... {body.length - 100} more rows</div>
      ) : null}
    </div>
  );
}
