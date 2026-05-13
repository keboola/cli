import { Editor } from "@monaco-editor/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Database, Play, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import { Drawer } from "../components/Drawer";
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
  const [showCreate, setShowCreate] = useState(false);

  const q = useQuery<WorkspacesResp>({
    queryKey: ["workspaces", project],
    queryFn: () => api.get("/workspaces", { query: { project: project ?? undefined } }),
    enabled: !!project,
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
        description={`Run SQL against ${project ?? "(no project)"} via the Query Service. Workspaces are owned by you on the warehouse and stay alive until deleted.`}
        actions={
          <button
            type="button"
            className="nerd-btn flex items-center gap-1 hover:text-keboola"
            disabled={!project}
            onClick={() => setShowCreate(true)}
          >
            <Plus className="w-3 h-3" /> New workspace
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
            {
              header: "Name",
              cell: (w) => <span className="font-bold text-accent">{w.name}</span>,
            },
            { header: "ID", cell: (w) => <span className="text-zinc-500">{w.id}</span> },
            { header: "Backend", cell: (w) => w.backend },
            {
              header: "Schema",
              cell: (w) => <span className="text-zinc-400">{w.schema}</span>,
            },
            {
              header: "Created",
              cell: (w) => <span className="text-xs text-zinc-500">{w.created}</span>,
            },
            {
              header: "",
              align: "right",
              cell: (w) => (
                <button
                  type="button"
                  className="nerd-btn text-xs hover:text-red-400 hover:border-red-700"
                  onClick={(e) => {
                    e.stopPropagation();
                    if (confirm(`Delete workspace ${w.name} (#${w.id})?`)) {
                      deleteMu.mutate({ alias: w.project_alias, id: w.id });
                    }
                  }}
                >
                  <Trash2 className="w-3 h-3" />
                </button>
              ),
            },
          ]}
        />
      )}
      {showCreate && project ? (
        <CreateWorkspaceDrawer
          project={project}
          onClose={() => setShowCreate(false)}
          onCreated={(ws) => {
            setShowCreate(false);
            qc.invalidateQueries({ queryKey: ["workspaces"] });
            setSelected(ws);
          }}
        />
      ) : null}
      {selected ? (
        <SqlEditorDrawer workspace={selected} onClose={() => setSelected(null)} />
      ) : null}
    </div>
  );
}

function CreateWorkspaceDrawer({
  project,
  onClose,
  onCreated,
}: {
  project: string;
  onClose: () => void;
  onCreated: (ws: Workspace) => void;
}) {
  const [name, setName] = useState(`kbagent-${project}`);
  const [readOnly, setReadOnly] = useState(false);
  const [uiMode, setUiMode] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const createMu = useMutation({
    mutationFn: () =>
      api.post<Workspace>(`/workspaces/${encodeURIComponent(project)}`, {
        name,
        read_only: readOnly,
        ui_mode: uiMode,
      }),
    onSuccess: (ws) => onCreated(ws),
    onError: (err) => setError((err as Error).message),
  });

  return (
    <Drawer
      open={true}
      onClose={onClose}
      title="New SQL workspace"
      subtitle={`Project: ${project}. Workspaces persist on the warehouse and cost compute -- delete when done.`}
      width="max-w-xl"
      actions={
        <button
          type="button"
          className="nerd-btn hover:text-keboola"
          disabled={!name.trim() || createMu.isPending}
          onClick={() => {
            setError(null);
            createMu.mutate();
          }}
        >
          <Database className="w-3 h-3 inline mr-1" />
          {createMu.isPending ? "creating..." : "Create"}
        </button>
      }
    >
      <div className="space-y-3">
        <label className="text-xs text-zinc-400 block">
          Name
          <input
            className="nerd-input w-full mt-1 font-mono"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="my-workspace"
            required
          />
          <span className="text-zinc-600">
            Visible on the warehouse and in the Keboola UI (if ui_mode).
          </span>
        </label>
        <label className="flex items-center gap-2 text-xs text-zinc-400">
          <input
            type="checkbox"
            checked={readOnly}
            onChange={(e) => setReadOnly(e.target.checked)}
          />
          Read-only storage access (recommended for analysis -- prevents
          accidental writes to project tables)
        </label>
        <label className="flex items-center gap-2 text-xs text-zinc-400">
          <input
            type="checkbox"
            checked={uiMode}
            onChange={(e) => setUiMode(e.target.checked)}
          />
          UI mode (~15s, visible in Keboola UI's Workspaces tab) -- otherwise
          headless (~1s, only seen by kbagent)
        </label>
        {error ? <ErrorBox message={error} /> : null}
      </div>
    </Drawer>
  );
}

function SqlEditorDrawer({
  workspace,
  onClose,
}: {
  workspace: Workspace;
  onClose: () => void;
}) {
  const [sql, setSql] = useState(`-- ${workspace.backend} workspace ・ schema: ${workspace.schema}
-- Hint: Query Service runs SELECT statements. SHOW / DESCRIBE / DDL
-- (CREATE, DROP, INSERT, ...) are typically rejected with 422.
SELECT current_timestamp() AS now;`);
  const [result, setResult] = useState<unknown | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hint, setHint] = useState<string | null>(null);

  const runMu = useMutation({
    mutationFn: () =>
      api.post(
        `/workspaces/${encodeURIComponent(workspace.project_alias)}/${workspace.id}/query`,
        { sql },
      ),
    onSuccess: (data) => {
      setResult(data);
      setError(null);
      setHint(null);
    },
    onError: (err) => {
      const msg = (err as Error).message;
      setError(msg);
      setResult(null);
      // Common cause: user typed SHOW / DESCRIBE / DDL. Surface a friendly
      // hint pointing to the warehouse-native equivalents.
      const stripped = sql.trim().toUpperCase();
      if (
        msg.includes("422") ||
        msg.toLowerCase().includes("unprocessable") ||
        msg.toLowerCase().includes("not allowed")
      ) {
        if (
          stripped.startsWith("SHOW") ||
          stripped.startsWith("DESCRIBE") ||
          stripped.startsWith("DESC ") ||
          stripped.startsWith("CREATE") ||
          stripped.startsWith("DROP") ||
          stripped.startsWith("INSERT") ||
          stripped.startsWith("UPDATE") ||
          stripped.startsWith("DELETE")
        ) {
          setHint(
            workspace.backend === "snowflake"
              ? "Query Service only runs SELECT. For SHOW TABLES / column listing on Snowflake, use:\n  SELECT TABLE_NAME, ROW_COUNT FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = CURRENT_SCHEMA();"
              : workspace.backend === "bigquery"
                ? "Query Service only runs SELECT. For BigQuery, list tables with:\n  SELECT table_name FROM `<dataset>.INFORMATION_SCHEMA.TABLES`;"
                : "Query Service only runs SELECT. Use INFORMATION_SCHEMA for catalog browsing.",
          );
        }
      }
    },
  });

  return (
    <Drawer
      open={true}
      onClose={onClose}
      title={`SQL ・ ${workspace.name}`}
      subtitle={`${workspace.backend} ・ ${workspace.schema} ・ workspace #${workspace.id}`}
      width="max-w-5xl"
      actions={
        <button
          type="button"
          className="nerd-btn flex items-center gap-1 hover:text-keboola"
          onClick={() => runMu.mutate()}
          disabled={runMu.isPending}
        >
          <Play className="w-3 h-3" /> {runMu.isPending ? "running..." : "Run"}
        </button>
      }
    >
      <div className="space-y-3">
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
          <div className="nerd-card border-red-700/40 text-red-400 text-sm space-y-2">
            <div>{error}</div>
            {hint ? (
              <div className="text-amber-400 text-xs whitespace-pre-wrap">
                Hint: {hint}
              </div>
            ) : null}
          </div>
        ) : null}
        {result ? <SqlResults result={result} /> : null}
      </div>
    </Drawer>
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
    <div className="space-y-3">
      {statements.map((stmt, i) => (
        <div key={stmt.statement_id ?? i} className="border border-zinc-800 rounded">
          <div className="px-3 py-2 border-b border-zinc-800 text-xs flex justify-between">
            <span>
              Statement {i + 1} ・ {stmt.status} ・ {stmt.rows_affected} rows
            </span>
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
    <div className="overflow-auto" style={{ maxHeight: 360 }}>
      <table className="w-full text-xs font-mono">
        <thead className="bg-zinc-900/60">
          <tr>
            {header.map((h, i) => (
              <th
                key={i}
                className="px-3 py-1.5 text-left text-keboola border-b border-zinc-800 whitespace-nowrap"
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {body.slice(0, 200).map((r, i) => (
            <tr key={i} className="border-b border-zinc-900/40">
              {r.map((c, j) => (
                <td key={j} className="px-3 py-1 text-zinc-300 whitespace-nowrap">
                  {c}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {body.length > 200 ? (
        <div className="text-xs text-zinc-500 px-3 py-1">
          ... {body.length - 200} more rows
        </div>
      ) : null}
    </div>
  );
}
