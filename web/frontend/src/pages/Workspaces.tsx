import { Editor } from "@monaco-editor/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Brain,
  Code,
  Database,
  Eye,
  Key,
  Play,
  Plus,
  Share2,
  Sparkles,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api, ssePost, type SsePostHandle } from "../api/client";
import { Drawer } from "../components/Drawer";
import { Empty, ErrorBox, Loading, PageTitle, TwoPathEmpty } from "../components/Empty";
import { DataTable } from "../components/Table";
import { useUIState } from "../state";
import type { ProjectError, Workspace } from "../types";

/**
 * AbortError shape detection across browsers (DOMException on standards,
 * named Error on some shims). Kept inline so a future move of this util
 * to a shared module doesn't fan out to every page that needs it.
 */
function isAbortError(err: unknown): boolean {
  if (err instanceof DOMException && err.name === "AbortError") return true;
  if (err instanceof Error && err.name === "AbortError") return true;
  return Boolean(
    err &&
      typeof err === "object" &&
      "message" in err &&
      String((err as { message: unknown }).message).toLowerCase().includes("abort"),
  );
}

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
              cell: (w) => <span className="text-zinc-600 dark:text-zinc-400">{w.schema}</span>,
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
                  className="nerd-btn text-xs hover:text-red-600 hover:border-red-300 dark:hover:text-red-400 dark:hover:border-red-700"
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
        <WorkspaceInfoDrawer
          workspace={selected}
          onClose={() => setSelected(null)}
          onDelete={(ws) => {
            if (confirm(`Delete workspace ${ws.name}?`))
              deleteMu.mutate({ alias: ws.project_alias, id: ws.id });
          }}
        />
      ) : null}
    </div>
  );
}

/**
 * The "info page" for a workspace -- matches Keboola UI's layout where
 * the SQL editor is one click away (Open SQL Editor button), and the
 * workspace itself surfaces description / parameters / actions.
 */
function WorkspaceInfoDrawer({
  workspace,
  onClose,
  onDelete,
}: {
  workspace: Workspace;
  onClose: () => void;
  onDelete: (ws: Workspace) => void;
}) {
  const [editorOpen, setEditorOpen] = useState(false);
  const [showPassword, setShowPassword] = useState(false);

  const passwordMu = useMutation({
    mutationFn: () =>
      api.post<{ password: string }>(
        `/workspaces/${encodeURIComponent(workspace.project_alias)}/${workspace.id}/password`,
      ),
  });

  return (
    <>
      <Drawer
        open={true}
        onClose={onClose}
        title={workspace.name}
        subtitle={`workspace #${workspace.id} ・ ${workspace.backend} ・ schema ${workspace.schema}`}
        width="max-w-4xl"
        actions={
          <button
            type="button"
            className="nerd-btn flex items-center gap-1 hover:text-keboola border-keboola/40"
            onClick={() => setEditorOpen(true)}
          >
            <Code className="w-3.5 h-3.5" /> Open SQL Editor
          </button>
        }
      >
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          {/* Main column: description + parameters card */}
          <div className="lg:col-span-2 space-y-4">
            <div className="nerd-card">
              <div className="flex items-center justify-between mb-2">
                <h3 className="text-sm font-bold text-keboola">Description</h3>
                <button
                  type="button"
                  className="nerd-pill text-[10px] hover:border-neon-pink hover:text-neon-pink"
                  title="AI-generated description (not yet wired)"
                  disabled
                >
                  <Sparkles className="w-3 h-3 inline mr-1" /> Generate
                </button>
              </div>
              <p className="text-xs text-zinc-500">
                No description yet. Workspaces are throwaway SQL sandboxes —
                document non-trivial ones so teammates know what they were for.
              </p>
            </div>
            <div className="nerd-card">
              <h3 className="text-sm font-bold text-keboola mb-3">Parameters</h3>
              <div className="grid grid-cols-2 gap-3 text-xs">
                <KV label="Workspace ID" value={String(workspace.id)} mono />
                <KV label="Type" value={workspace.backend} />
                <KV label="Schema" value={workspace.schema} mono />
                <KV label="Host" value={workspace.host} mono />
                <KV label="User" value={workspace.user} mono />
                <KV label="Created" value={workspace.created} />
                <KV
                  label="Sandbox component"
                  value={workspace.component_id}
                  mono
                />
                <KV label="Config ID" value={workspace.config_id} mono />
              </div>
            </div>
            <div className="nerd-card">
              <h3 className="text-sm font-bold text-keboola mb-3">Credentials</h3>
              <div className="space-y-2">
                <KV label="Host" value={workspace.host} mono />
                <KV label="User" value={workspace.user} mono />
                <KV label="Schema" value={workspace.schema} mono />
                <div className="flex items-center gap-2">
                  <span className="text-xs text-zinc-500">Password:</span>
                  <button
                    type="button"
                    className="nerd-btn text-xs hover:text-keboola"
                    onClick={() => {
                      setShowPassword(true);
                      passwordMu.mutate();
                    }}
                  >
                    <Key className="w-3 h-3 inline mr-1" /> Reset & show
                  </button>
                  {showPassword && passwordMu.data ? (
                    <code className="text-xs text-amber-700 dark:text-neon-amber font-mono">
                      {passwordMu.data.password}
                    </code>
                  ) : null}
                </div>
                <div className="text-[11px] text-zinc-500 dark:text-zinc-600">
                  Reset is the only way to retrieve a password — Keboola never
                  stores the plaintext.
                </div>
              </div>
            </div>
          </div>

          {/* Right column: actions sidebar mimicking Keboola UI */}
          <div className="space-y-2">
            <button
              type="button"
              className="w-full nerd-btn flex items-center gap-2 hover:text-keboola justify-start py-2"
              onClick={() => setEditorOpen(true)}
            >
              <Code className="w-4 h-4 text-keboola" />
              <span>Open SQL Editor</span>
            </button>
            <ActionItem
              icon={<Upload className="w-3.5 h-3.5" />}
              label="Load tables"
              hint="Copy tables from Storage into this workspace"
            />
            <ActionItem
              icon={<Eye className="w-3.5 h-3.5" />}
              label="Browse storage"
              hint="(use SQL Editor → Storage Explorer sidebar)"
            />
            <ActionItem
              icon={<Share2 className="w-3.5 h-3.5" />}
              label="Enable sharing"
              hint="Share this workspace with the project"
            />
            <ActionItem
              icon={<Brain className="w-3.5 h-3.5" />}
              label="Run AI agent here"
              hint="Spawn an agent task scoped to this workspace"
            />
            <hr className="border-zinc-100 dark:border-zinc-900 my-2" />
            <button
              type="button"
              className="w-full nerd-btn flex items-center gap-2 hover:text-red-600 hover:border-red-300 dark:hover:text-red-400 dark:hover:border-red-700 justify-start py-2 text-red-600 dark:text-red-400"
              onClick={() => onDelete(workspace)}
            >
              <Trash2 className="w-4 h-4" />
              <span>Delete workspace</span>
            </button>
          </div>
        </div>
      </Drawer>

      {editorOpen ? (
        <SqlEditorDrawer
          workspace={workspace}
          onClose={() => setEditorOpen(false)}
        />
      ) : null}
    </>
  );
}

function KV({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-zinc-500">{label}</div>
      <div className={`text-xs mt-0.5 ${mono ? "font-mono text-accent" : "text-zinc-800 dark:text-zinc-200"} truncate`}>
        {value || "—"}
      </div>
    </div>
  );
}

function ActionItem({
  icon,
  label,
  hint,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  hint?: string;
  onClick?: () => void;
}) {
  const disabled = !onClick;
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={`w-full text-left p-2 rounded border border-zinc-100 dark:border-zinc-900 ${
        disabled
          ? "text-zinc-500 dark:text-zinc-600 cursor-not-allowed"
          : "text-zinc-700 dark:text-zinc-300 hover:bg-zinc-100 dark:hover:bg-zinc-900 hover:border-keboola/30"
      }`}
      title={disabled ? "Not yet wired" : undefined}
    >
      <div className="flex items-center gap-2 text-xs">
        <span>{icon}</span>
        <span className="font-medium">{label}</span>
      </div>
      {hint ? <div className="text-[10px] text-zinc-500 dark:text-zinc-600 mt-0.5 ml-5.5">{hint}</div> : null}
    </button>
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
        <label className="text-xs text-zinc-600 dark:text-zinc-400 block">
          Name
          <input
            className="nerd-input w-full mt-1 font-mono"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="my-workspace"
            required
          />
          <span className="text-zinc-500 dark:text-zinc-600">
            Visible on the warehouse and in the Keboola UI (if ui_mode).
          </span>
        </label>
        <label className="flex items-center gap-2 text-xs text-zinc-600 dark:text-zinc-400">
          <input
            type="checkbox"
            checked={readOnly}
            onChange={(e) => setReadOnly(e.target.checked)}
          />
          Read-only storage access (recommended for analysis -- prevents
          accidental writes to project tables)
        </label>
        <label className="flex items-center gap-2 text-xs text-zinc-600 dark:text-zinc-400">
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

interface BucketsResp {
  buckets: Array<{ project_alias: string; id: string }>;
}
interface TablesResp {
  tables: Array<{ project_alias: string; id: string; bucket_id: string }>;
}

function SqlEditorDrawer({
  workspace,
  onClose,
}: {
  workspace: Workspace;
  onClose: () => void;
}) {
  const [sql, setSql] = useState(`-- ${workspace.backend} workspace ・ schema: ${workspace.schema}
-- Click a table in the Storage Explorer (left) to insert it.
-- Query Service runs SELECT only; SHOW / DDL → INFORMATION_SCHEMA.
SELECT current_timestamp() AS now;`);
  const [result, setResult] = useState<unknown | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hint, setHint] = useState<string | null>(null);
  // The AI CLI choice is persisted on the SqlEditorDrawer (not the panel)
  // so the user's pick survives toggling the helper open/closed.
  const [aiCli, setAiCli] = useState<"claude" | "codex" | "gemini">("claude");

  // Fetch buckets + tables from the workspace's project so users can click
  // them into the editor (Storage Explorer pattern from Keboola UI).
  const bucketsQ = useQuery<BucketsResp>({
    queryKey: ["sql-buckets", workspace.project_alias],
    queryFn: () =>
      api.get("/storage/buckets", { query: { project: workspace.project_alias } }),
  });
  const tablesQ = useQuery<TablesResp>({
    queryKey: ["sql-tables", workspace.project_alias],
    queryFn: () =>
      api.get("/storage/tables", { query: { project: workspace.project_alias } }),
  });

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

  /** Insert table reference at cursor (or append). For Snowflake we use
   *  "<DATABASE>"."<SCHEMA>"."<TABLE>" pattern; the workspace schema is
   *  the source-of-truth bucket name on the warehouse. */
  const insertTable = (tableId: string) => {
    const tableName = tableId.split(".").pop() ?? tableId;
    const fqn =
      workspace.backend === "bigquery"
        ? `\`${workspace.schema}\`.\`${tableName}\``
        : `"${tableName}"`;
    setSql((prev) => `${prev}\n${fqn}`);
  };

  // Group tables by bucket for the tree.
  const tablesByBucket = new Map<string, string[]>();
  for (const t of tablesQ.data?.tables ?? []) {
    const arr = tablesByBucket.get(t.bucket_id) ?? [];
    arr.push(t.id);
    tablesByBucket.set(t.bucket_id, arr);
  }

  return (
    <Drawer
      open={true}
      onClose={onClose}
      title={`SQL ・ ${workspace.name}`}
      subtitle={`${workspace.backend} ・ ${workspace.schema} ・ workspace #${workspace.id}`}
      width="max-w-7xl"
      actions={
        <button
          type="button"
          className="nerd-btn flex items-center gap-1 hover:text-keboola"
          onClick={() => runMu.mutate()}
          disabled={runMu.isPending}
        >
          <Play className="w-3 h-3" /> {runMu.isPending ? "running..." : "Run (⌘↵)"}
        </button>
      }
    >
      <div className="flex gap-3 h-full">
        {/* Storage Explorer sidebar */}
        <aside className="w-64 shrink-0 nerd-card overflow-y-auto" style={{ maxHeight: "75vh" }}>
          <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-2">
            Storage Explorer
          </div>
          {bucketsQ.isLoading || tablesQ.isLoading ? (
            <Loading label="loading buckets..." />
          ) : (
            <div className="space-y-2 text-xs">
              {(bucketsQ.data?.buckets ?? []).map((b) => (
                <BucketNode
                  key={b.id}
                  bucketId={b.id}
                  tables={tablesByBucket.get(b.id) ?? []}
                  onPick={insertTable}
                />
              ))}
            </div>
          )}
        </aside>
        {/* Editor + results */}
        <div className="flex-1 space-y-3 min-w-0">
          <SqlHelperPanel
            cli={aiCli}
            onCliChange={setAiCli}
            project={workspace.project_alias}
            workspaceId={workspace.id}
            backend={workspace.backend}
            schemaName={workspace.schema}
            draftSql={sql}
            bucketIds={(bucketsQ.data?.buckets ?? []).map((b) => b.id)}
            onApply={(generatedSql) => setSql(generatedSql)}
          />
          <div className="border border-zinc-200 dark:border-zinc-800 rounded">
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
            <div className="nerd-card border-red-300 dark:border-red-700/40 text-red-600 dark:text-red-400 text-sm space-y-2">
              <div>{error}</div>
              {hint ? (
                <div className="text-amber-700 dark:text-amber-400 text-xs whitespace-pre-wrap">
                  Hint: {hint}
                </div>
              ) : null}
            </div>
          ) : null}
          {result ? <SqlResults result={result} /> : null}
        </div>
      </div>
    </Drawer>
  );
}

function BucketNode({
  bucketId,
  tables,
  onPick,
}: {
  bucketId: string;
  tables: string[];
  onPick: (tableId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button
        type="button"
        className="w-full text-left flex items-center gap-1 px-1 py-0.5 hover:text-keboola"
        onClick={() => setOpen((o) => !o)}
      >
        <span className="text-zinc-500 dark:text-zinc-600">{open ? "▾" : "▸"}</span>
        <span className="font-mono text-accent truncate">{bucketId}</span>
        <span className="text-[10px] text-zinc-500 dark:text-zinc-600 ml-auto">{tables.length}</span>
      </button>
      {open && tables.length > 0 ? (
        <div className="ml-4 mt-1 space-y-0.5 border-l border-zinc-100 dark:border-zinc-900 pl-2">
          {tables.map((t) => (
            <button
              key={t}
              type="button"
              className="w-full text-left text-xs font-mono text-zinc-600 dark:text-zinc-400 hover:text-keboola truncate"
              onClick={() => onPick(t)}
              title={`Click to insert ${t}`}
            >
              {t.split(".").pop()}
            </button>
          ))}
        </div>
      ) : null}
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
    <div className="space-y-3">
      {statements.map((stmt, i) => (
        <div key={stmt.statement_id ?? i} className="border border-zinc-200 dark:border-zinc-800 rounded">
          <div className="px-3 py-2 border-b border-zinc-200 dark:border-zinc-800 text-xs flex justify-between">
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
        <thead className="bg-zinc-100 dark:bg-zinc-900/60">
          <tr>
            {header.map((h, i) => (
              <th
                key={i}
                className="px-3 py-1.5 text-left text-keboola border-b border-zinc-200 dark:border-zinc-800 whitespace-nowrap"
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {body.slice(0, 200).map((r, i) => (
            <tr key={i} className="border-b border-zinc-200 dark:border-zinc-900/40">
              {r.map((c, j) => (
                <td key={j} className="px-3 py-1 text-zinc-700 dark:text-zinc-300 whitespace-nowrap">
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

/**
 * Inline AI helper for the workspace SQL editor (#287).
 *
 * Modeled on Agents.tsx > PromptHelperPanel — same SSE protocol (init /
 * stdout / stderr / done), same claude / codex / gemini CLI selector, same
 * "live preview while streaming → final suggestion → Apply / Discard"
 * workflow. The differences:
 *
 * - Endpoint is /workspaces/sql/improve/stream (workspace-grounded
 *   meta-prompt: backend, schema, visible buckets are passed in).
 * - The `done` payload carries `sql` (not `prompt`); `onApply` replaces
 *   the editor body with it instead of a prompt textarea.
 * - The cancel-on-unmount cleanup is mandatory — without it, the backend
 *   keeps the claude/codex/gemini subprocess alive while waiting for an
 *   SSE consumer that will never return.
 */
function SqlHelperPanel({
  cli,
  onCliChange,
  project,
  workspaceId,
  backend,
  schemaName,
  draftSql,
  bucketIds,
  onApply,
}: {
  cli: "claude" | "codex" | "gemini";
  onCliChange: (c: "claude" | "codex" | "gemini") => void;
  project: string;
  workspaceId: number;
  backend: string;
  schemaName: string;
  draftSql: string;
  bucketIds: string[];
  onApply: (sql: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [goal, setGoal] = useState("");
  const [running, setRunning] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [livePreview, setLivePreview] = useState("");
  const [finalSql, setFinalSql] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const handleRef = useRef<SsePostHandle | null>(null);

  const reset = () => {
    setLivePreview("");
    setFinalSql(null);
    setError(null);
    setElapsed(0);
  };

  const start = () => {
    if (!goal.trim()) {
      setError("Describe the query you want first (e.g. 'top 10 customers by revenue last 30 days').");
      return;
    }
    if (handleRef.current) {
      handleRef.current.abort();
      handleRef.current = null;
    }
    reset();
    setRunning(true);
    const startMs = Date.now();
    const tick = setInterval(
      () => setElapsed(Math.round((Date.now() - startMs) / 1000)),
      500,
    );
    let assistantText = "";
    const handle = ssePost(
      "/workspaces/sql/improve/stream",
      {
        cli,
        goal,
        project,
        backend,
        schema_name: schemaName,
        workspace_id: workspaceId,
        draft_sql: draftSql,
        bucket_ids: bucketIds,
      },
      {
        init: () => {
          /* the running indicator already covers this */
        },
        stdout: (d) => {
          const data = (d ?? {}) as Record<string, unknown>;
          // Claude stream-json: assistant turns carry message.content[].text.
          if (data.type === "assistant" && typeof data.message === "object") {
            const msg = data.message as Record<string, unknown>;
            const content = msg.content;
            if (Array.isArray(content)) {
              for (const block of content) {
                if (
                  block &&
                  typeof block === "object" &&
                  (block as Record<string, unknown>).type === "text" &&
                  typeof (block as Record<string, unknown>).text === "string"
                ) {
                  assistantText += (block as Record<string, unknown>).text as string;
                  setLivePreview(assistantText);
                }
              }
            }
          } else if (typeof data.raw === "string") {
            // codex / gemini stream raw text lines (no jsonl).
            assistantText += (assistantText ? "\n" : "") + data.raw;
            setLivePreview(assistantText);
          }
        },
        stderr: () => {
          /* progress notes — ignored for the preview pane */
        },
        done: (d) => {
          const data = (d ?? {}) as Record<string, unknown>;
          if (data.status === "error") {
            setError(String(data.error ?? "AI helper failed"));
            return;
          }
          const cleaned = typeof data.sql === "string" ? data.sql.trim() : "";
          if (!cleaned) {
            setError("AI returned an empty query. Refine the goal and regenerate.");
            return;
          }
          setFinalSql(cleaned);
        },
        message: () => {
          /* unknown event — ignore */
        },
      },
    );
    handleRef.current = handle;
    handle.done
      .catch((err) => {
        if (isAbortError(err)) return;
        setError((err as Error).message);
      })
      .finally(() => {
        clearInterval(tick);
        setRunning(false);
        handleRef.current = null;
      });
  };

  const cancel = () => {
    if (handleRef.current) {
      handleRef.current.abort();
      handleRef.current = null;
    }
    setRunning(false);
  };

  useEffect(() => {
    return () => {
      if (handleRef.current) {
        handleRef.current.abort();
        handleRef.current = null;
      }
    };
  }, []);

  if (!open) {
    return (
      <div className="flex items-center gap-2">
        <button
          type="button"
          className="nerd-btn text-xs flex items-center gap-1 hover:text-neon-pink hover:border-neon-pink/60"
          onClick={() => setOpen(true)}
          title={`Let ${cli} write the SQL for you`}
        >
          <Sparkles className="w-3 h-3" />
          Help me write this SQL
        </button>
        <span className="text-xs text-zinc-500">
          uses {cli} with your workspace context (project, backend, visible buckets) baked in
        </span>
      </div>
    );
  }

  return (
    <div className="nerd-card border-neon-pink/40 dark:border-neon-pink/40 space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-xs font-bold text-neon-pink flex items-center gap-1">
          <Sparkles className="w-3 h-3" />
          AI SQL helper · {cli}
        </div>
        <button
          type="button"
          className="text-xs text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-300 flex items-center gap-1"
          onClick={() => {
            cancel();
            setOpen(false);
            reset();
            setGoal("");
          }}
          title="Close the helper without applying"
        >
          <X className="w-3 h-3" /> close
        </button>
      </div>

      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[10px] uppercase tracking-wider text-zinc-500">CLI:</span>
        {(["claude", "codex", "gemini"] as const).map((c) => (
          <button
            key={c}
            type="button"
            className={`nerd-btn text-xs ${cli === c ? "border-neon-pink text-neon-pink" : ""}`}
            onClick={() => onCliChange(c)}
            disabled={running}
          >
            {c}
          </button>
        ))}
      </div>

      <label className="text-xs text-zinc-400 block">
        What query do you need?
        <textarea
          className="nerd-input w-full mt-1 h-20"
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          placeholder="e.g. Top 10 customers by total revenue in the last 30 days, joined with their country from in.c-customers."
          disabled={running}
        />
      </label>

      {draftSql.trim() ? (
        <div className="text-xs text-zinc-500">
          Existing SQL in the editor will be passed to the AI as a starting point ({draftSql.trim().length} chars).
        </div>
      ) : null}

      <div className="flex gap-2 items-center flex-wrap">
        {running ? (
          <button
            type="button"
            className="nerd-btn hover:text-red-400 hover:border-red-700"
            onClick={cancel}
            title="Abort the AI helper"
          >
            <X className="w-3 h-3 inline mr-1" />
            cancel ({elapsed}s)
          </button>
        ) : (
          <button
            type="button"
            className="nerd-btn hover:text-neon-pink hover:border-neon-pink/60"
            onClick={start}
            disabled={!goal.trim()}
            title="Ask the AI to write the SQL"
          >
            <Sparkles className="w-3 h-3 inline mr-1" />
            {finalSql ? "Regenerate" : "Generate SQL"}
          </button>
        )}
        {!running && finalSql ? (
          <span className="text-xs text-zinc-500">generated in {elapsed}s</span>
        ) : null}
      </div>

      {error ? <ErrorBox message={error} /> : null}

      {running && livePreview ? (
        <div>
          <div className="text-xs text-zinc-500 mb-1">live output:</div>
          <pre
            className="nerd-code whitespace-pre-wrap text-xs text-zinc-600 dark:text-zinc-400"
            style={{ maxHeight: "200px", overflow: "auto" }}
          >
            {livePreview}
          </pre>
        </div>
      ) : null}

      {finalSql ? (
        <div className="space-y-2">
          <div className="text-xs text-zinc-500">AI suggestion:</div>
          <pre
            className="nerd-code whitespace-pre-wrap text-zinc-800 dark:text-zinc-200 border-neon-pink/30"
            style={{ maxHeight: "320px", overflow: "auto" }}
          >
            {finalSql}
          </pre>
          <div className="flex gap-2 flex-wrap">
            <button
              type="button"
              className="nerd-btn hover:text-keboola hover:border-keboola/60"
              onClick={() => {
                onApply(finalSql);
                setOpen(false);
                reset();
                setGoal("");
              }}
              title="Replace the editor's SQL with this suggestion"
            >
              ✓ Use this SQL
            </button>
            <button
              type="button"
              className="nerd-btn hover:text-red-400 hover:border-red-700"
              onClick={() => {
                setFinalSql(null);
                setLivePreview("");
              }}
              title="Throw away this suggestion (keeps the helper open)"
            >
              ✗ Discard
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
