import { useCallback, useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  Check,
  Loader2,
  Sparkles,
  Table as TableIcon,
  Undo2,
  X,
} from "lucide-react";
import { api } from "../../api/client";
import { askLocalAi, type LocalAiCli } from "../../api/ai";
import { Empty, ErrorBox, Loading, PageTitle } from "../../components/Empty";
import { Drawer } from "../../components/Drawer";
import { useUIState } from "../../state";
import type { Project, Table } from "../../types";
import {
  type ColumnProfile,
  defaultTypeFor,
  profileTable,
} from "./profile";
import { extractTypeFromAiResponse } from "./ai_parse";

interface TablesResp {
  tables: Table[];
  errors: unknown[];
}

interface TableDetail {
  table_id: string;
  columns?: string[] | null;
  column_details?: Array<{ name: string; type?: string; length?: string }> | null;
  rows_count?: number;
  primary_key?: string[];
}

interface TablePreview {
  header: string[];
  rows: unknown[][];
  row_count: number;
}

const PREVIEW_LIMIT = 500;
const SYNC_PREVIEW_COLUMN_LIMIT = 30;

type ColumnState =
  | { kind: "pending" }
  | { kind: "loading" }
  | { kind: "proposed"; type: string; raw: string }
  | { kind: "approved"; type: string }
  | { kind: "rejected" }
  | { kind: "error"; message: string };

export function TypeInspectorPage() {
  const { branchId } = useUIState();
  const [project, setProject] = useState<string | null>(null);
  const [tableId, setTableId] = useState<string | null>(null);
  const [columnStates, setColumnStates] = useState<Record<string, ColumnState>>({});
  const [showApplyHelp, setShowApplyHelp] = useState(false);
  // Which local CLI runs the propose-type calls. Defaults to `claude`
  // because it ships in this repo's design system; user can switch
  // mid-session without losing approved decisions.
  const [cli, setCli] = useState<LocalAiCli>("claude");

  const projectsQ = useQuery<{ projects: Project[] }>({
    queryKey: ["ti-projects"],
    queryFn: () => api.get("/projects"),
  });

  const tablesQ = useQuery<TablesResp>({
    queryKey: ["ti-tables", project],
    queryFn: () =>
      api.get("/storage/tables", { query: { project: project ?? "" } }),
    enabled: !!project,
  });

  const detailQ = useQuery<TableDetail>({
    queryKey: ["ti-detail", project, tableId],
    queryFn: () =>
      api.get(`/storage/table-detail/${project}/${tableId}`),
    enabled: !!project && !!tableId,
  });

  // Preview is limited to 30 columns by the upstream sync export. For
  // wider tables we only profile the first 30 and show a banner; a future
  // iteration could paginate via the `columns` query param.
  const previewColumns = useMemo(
    () => (detailQ.data?.columns ?? []).slice(0, SYNC_PREVIEW_COLUMN_LIMIT),
    [detailQ.data?.columns],
  );

  const previewQ = useQuery<TablePreview>({
    queryKey: ["ti-preview", project, tableId, previewColumns.join(",")],
    queryFn: () =>
      api.get(`/storage/table-preview/${project}/${tableId}`, {
        query: { limit: PREVIEW_LIMIT, columns: previewColumns },
      }),
    enabled: !!project && !!tableId && previewColumns.length > 0,
  });

  const profiles = useMemo<ColumnProfile[]>(() => {
    if (!previewQ.data) return [];
    return profileTable(previewQ.data.header, previewQ.data.rows);
  }, [previewQ.data]);

  const proposeMutation = useMutation({
    mutationFn: async (col: ColumnProfile) => {
      const prompt = buildPropositionPrompt(detailQ.data, col);
      const response = await askLocalAi({
        cli,
        message: prompt,
        project,
        branchId,
      });
      return { col, response };
    },
    onMutate: ({ name }: ColumnProfile) => {
      setColumnStates((s) => ({ ...s, [name]: { kind: "loading" } }));
    },
    onSuccess: ({ col, response }) => {
      const type = extractTypeFromAiResponse(response);
      setColumnStates((s) => ({
        ...s,
        [col.name]: { kind: "proposed", type, raw: response },
      }));
    },
    onError: (err: Error, col) => {
      setColumnStates((s) => ({
        ...s,
        [col.name]: { kind: "error", message: err.message },
      }));
    },
  });

  const setState = useCallback((name: string, state: ColumnState) => {
    setColumnStates((s) => ({ ...s, [name]: state }));
  }, []);

  // Reset state when table changes.
  const resetForNewTable = useCallback(
    (newTableId: string | null) => {
      setTableId(newTableId);
      setColumnStates({});
    },
    [],
  );

  const aliases = projectsQ.data?.projects ?? [];
  const tables = (tablesQ.data?.tables ?? []).sort((a, b) =>
    a.id.localeCompare(b.id),
  );

  // KPI summary
  const approved = Object.values(columnStates).filter(
    (s) => s.kind === "approved",
  ).length;
  const proposed = Object.values(columnStates).filter(
    (s) => s.kind === "proposed",
  ).length;
  const totalColumns = previewColumns.length;
  const truncatedColumns =
    (detailQ.data?.columns?.length ?? 0) > SYNC_PREVIEW_COLUMN_LIMIT;

  return (
    <div className="space-y-6">
      <PageTitle
        title="Type Inspector"
        description="Profile a Storage table column by column. Ask your local AI (claude / codex / gemini) for native-type proposals. Approve per column. Apply step opens a Playbook stub."
        actions={
          <button
            type="button"
            className="nerd-btn"
            disabled={approved === 0}
            onClick={() => setShowApplyHelp(true)}
          >
            Apply {approved}/{totalColumns}
          </button>
        }
      />

      <Picker
        aliases={aliases.map((p) => p.alias)}
        project={project}
        onProjectChange={(p) => {
          setProject(p);
          resetForNewTable(null);
        }}
        tables={tables.map((t) => t.id)}
        tableId={tableId}
        onTableChange={resetForNewTable}
        tablesLoading={tablesQ.isLoading}
        cli={cli}
        onCliChange={setCli}
      />

      {!project ? (
        <Empty title="Pick a project" hint="Choose one from the dropdown." />
      ) : null}

      {project && !tableId ? (
        <Empty
          title="Pick a table"
          hint={
            tablesQ.isLoading
              ? "loading tables..."
              : `${tables.length} tables in ${project}. Stage tables (in.c-*) are usually untyped.`
          }
        />
      ) : null}

      {project && tableId ? (
        <>
          {detailQ.isLoading || previewQ.isLoading ? (
            <Loading label="profiling table..." />
          ) : null}
          {detailQ.error ? (
            <ErrorBox message={(detailQ.error as Error).message} />
          ) : null}
          {previewQ.error ? (
            <ErrorBox
              message={`Preview failed: ${(previewQ.error as Error).message}`}
            />
          ) : null}

          {previewQ.data ? (
            <SummaryBar
              detail={detailQ.data}
              previewRows={previewQ.data.row_count}
              totalColumns={totalColumns}
              truncated={truncatedColumns}
              fullColumnCount={detailQ.data?.columns?.length ?? 0}
              approved={approved}
              proposed={proposed}
            />
          ) : null}

          {profiles.length > 0 ? (
            <ColumnGrid
              profiles={profiles}
              detail={detailQ.data}
              states={columnStates}
              onPropose={(col) => proposeMutation.mutate(col)}
              onApprove={(col, type) => setState(col.name, { kind: "approved", type })}
              onReject={(col) => setState(col.name, { kind: "rejected" })}
              onReset={(col) => setState(col.name, { kind: "pending" })}
            />
          ) : null}
        </>
      ) : null}

      {showApplyHelp ? (
        <ApplyHelpDrawer
          project={project}
          tableId={tableId}
          decisions={columnStates}
          profiles={profiles}
          onClose={() => setShowApplyHelp(false)}
        />
      ) : null}
    </div>
  );
}

function Picker({
  aliases,
  project,
  onProjectChange,
  tables,
  tableId,
  onTableChange,
  tablesLoading,
  cli,
  onCliChange,
}: {
  aliases: string[];
  project: string | null;
  onProjectChange: (p: string | null) => void;
  tables: string[];
  tableId: string | null;
  onTableChange: (t: string | null) => void;
  tablesLoading: boolean;
  cli: LocalAiCli;
  onCliChange: (c: LocalAiCli) => void;
}) {
  return (
    <div className="nerd-card flex flex-wrap items-center gap-3">
      <div className="flex items-center gap-2">
        <span className="text-xs uppercase tracking-widest text-zinc-500">project</span>
        <select
          className="nerd-btn text-sm py-1"
          value={project ?? ""}
          onChange={(e) => onProjectChange(e.target.value || null)}
        >
          <option value="">--</option>
          {aliases.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>
      </div>
      <div className="flex items-center gap-2 flex-1 min-w-[300px]">
        <span className="text-xs uppercase tracking-widest text-zinc-500">table</span>
        <select
          className="nerd-btn text-sm py-1 flex-1"
          value={tableId ?? ""}
          onChange={(e) => onTableChange(e.target.value || null)}
          disabled={!project || tablesLoading}
        >
          <option value="">
            {tablesLoading ? "loading tables..." : "-- pick a table --"}
          </option>
          {tables.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      </div>
      <div className="flex items-center gap-2">
        <span className="text-xs uppercase tracking-widest text-zinc-500">ai</span>
        <select
          className="nerd-btn text-sm py-1"
          value={cli}
          onChange={(e) => onCliChange(e.target.value as LocalAiCli)}
          title="Local CLI used for `propose` -- no master token needed"
        >
          <option value="claude">claude</option>
          <option value="codex">codex</option>
          <option value="gemini">gemini</option>
        </select>
      </div>
    </div>
  );
}

function SummaryBar({
  detail,
  previewRows,
  totalColumns,
  truncated,
  fullColumnCount,
  approved,
  proposed,
}: {
  detail: TableDetail | undefined;
  previewRows: number;
  totalColumns: number;
  truncated: boolean;
  fullColumnCount: number;
  approved: number;
  proposed: number;
}) {
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
      <Kpi label="Rows in table" value={(detail?.rows_count ?? 0).toLocaleString()} icon={TableIcon} />
      <Kpi
        label="Columns profiled"
        value={`${totalColumns}${truncated ? ` of ${fullColumnCount}` : ""}`}
        hint={truncated ? "first 30 only (sync export limit)" : undefined}
        tone={truncated ? "warn" : "neutral"}
      />
      <Kpi
        label="Preview sample"
        value={`${previewRows} rows`}
        hint={`limit ${PREVIEW_LIMIT}`}
      />
      <Kpi
        label="Decisions"
        value={`${approved} / ${totalColumns}`}
        hint={`${proposed} awaiting approval`}
        tone={approved === totalColumns && totalColumns > 0 ? "good" : "neutral"}
      />
    </div>
  );
}

function Kpi({
  label,
  value,
  hint,
  icon: Icon,
  tone = "neutral",
}: {
  label: string;
  value: string;
  hint?: string;
  icon?: React.ComponentType<{ className?: string }>;
  tone?: "neutral" | "good" | "warn";
}) {
  return (
    <div className="nerd-card">
      <div className="flex items-start justify-between">
        <div>
          <div className="text-xs uppercase tracking-widest text-zinc-500">{label}</div>
          <div
            className={`mt-1 text-2xl font-bold ${
              tone === "good"
                ? "text-keboola"
                : tone === "warn"
                  ? "text-amber-600 dark:text-amber-400"
                  : "text-zinc-900 dark:text-zinc-100"
            }`}
          >
            {value}
          </div>
          {hint ? <div className="text-xs text-zinc-500 mt-1">{hint}</div> : null}
        </div>
        {Icon ? <Icon className="w-4 h-4 text-zinc-400" /> : null}
      </div>
    </div>
  );
}

function ColumnGrid({
  profiles,
  detail,
  states,
  onPropose,
  onApprove,
  onReject,
  onReset,
}: {
  profiles: ColumnProfile[];
  detail: TableDetail | undefined;
  states: Record<string, ColumnState>;
  onPropose: (col: ColumnProfile) => void;
  onApprove: (col: ColumnProfile, type: string) => void;
  onReject: (col: ColumnProfile) => void;
  onReset: (col: ColumnProfile) => void;
}) {
  const existing = useMemo(() => {
    const map = new Map<string, string>();
    for (const cd of detail?.column_details ?? []) {
      if (cd.type) map.set(cd.name, cd.length ? `${cd.type}(${cd.length})` : cd.type);
    }
    return map;
  }, [detail]);

  return (
    <div className="overflow-x-auto">
      <table className="w-full nerd-card !p-0 text-sm">
        <thead>
          <tr className="border-b border-zinc-200 dark:border-zinc-800 text-xs uppercase tracking-widest text-zinc-500">
            <th className="text-left p-3">Column</th>
            <th className="text-left p-3">Current</th>
            <th className="text-left p-3">Inferred</th>
            <th className="text-right p-3">Null %</th>
            <th className="text-right p-3">Distinct</th>
            <th className="text-left p-3">Samples</th>
            <th className="text-left p-3">Proposal</th>
            <th className="text-right p-3">Decision</th>
          </tr>
        </thead>
        <tbody>
          {profiles.map((p) => {
            const state = states[p.name] ?? ({ kind: "pending" } satisfies ColumnState);
            const fallback = defaultTypeFor(p);
            return (
              <ColumnRow
                key={p.name}
                profile={p}
                state={state}
                existingType={existing.get(p.name)}
                fallbackType={fallback}
                onPropose={() => onPropose(p)}
                onApprove={(type) => onApprove(p, type)}
                onReject={() => onReject(p)}
                onReset={() => onReset(p)}
              />
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function ColumnRow({
  profile,
  state,
  existingType,
  fallbackType,
  onPropose,
  onApprove,
  onReject,
  onReset,
}: {
  profile: ColumnProfile;
  state: ColumnState;
  existingType: string | undefined;
  fallbackType: string;
  onPropose: () => void;
  onApprove: (type: string) => void;
  onReject: () => void;
  onReset: () => void;
}) {
  // The currently displayed proposed type is editable inline once Kai
  // returns. The user might want to tweak `VARCHAR(64)` -> `VARCHAR(255)`
  // before approving. We keep the value in component state so the input is
  // controlled but cheap to host here.
  const [proposalDraft, setProposalDraft] = useState<string>("");
  const proposedType =
    state.kind === "proposed" ? state.type : state.kind === "approved" ? state.type : null;

  const draftValue = proposalDraft || proposedType || fallbackType;

  return (
    <tr className="border-b border-zinc-100 dark:border-zinc-900 hover:bg-zinc-50 dark:hover:bg-zinc-900/40">
      <td className="p-3 font-mono text-accent">{profile.name}</td>
      <td className="p-3 text-zinc-500">
        {existingType ?? <span className="text-zinc-400">untyped</span>}
      </td>
      <td className="p-3">
        <span className="nerd-pill">{profile.inferredType}</span>
        <span className="ml-2 text-zinc-500 text-xs">{fallbackType}</span>
      </td>
      <td className="p-3 text-right text-zinc-600 dark:text-zinc-400">
        {(profile.nullRatio * 100).toFixed(0)}%
      </td>
      <td className="p-3 text-right text-zinc-600 dark:text-zinc-400">
        {profile.distinctCount}/{profile.sampleSize}
      </td>
      <td className="p-3 text-zinc-500 text-xs max-w-md">
        {profile.samples.length === 0 ? (
          <span className="text-zinc-400">--</span>
        ) : (
          <span className="font-mono block truncate" title={profile.samples.join(", ")}>
            {profile.samples
              .slice(0, 3)
              .map((s) => (s.length > 40 ? `${s.slice(0, 40)}…` : s))
              .join(", ")}
          </span>
        )}
      </td>
      <td className="p-3">
        {state.kind === "loading" ? (
          <span className="inline-flex items-center gap-1 text-zinc-500 text-xs">
            <Loader2 className="w-3 h-3 animate-spin" />
            asking kai...
          </span>
        ) : state.kind === "error" ? (
          <span className="nerd-pill-red text-xs" title={state.message}>
            error
          </span>
        ) : state.kind === "proposed" || state.kind === "approved" ? (
          <input
            type="text"
            className="nerd-btn text-xs py-1 px-2 font-mono w-40"
            value={draftValue}
            onChange={(e) => setProposalDraft(e.target.value)}
            disabled={state.kind === "approved"}
          />
        ) : (
          <button type="button" className="nerd-btn text-xs py-1 px-2" onClick={onPropose}>
            <Sparkles className="w-3 h-3 mr-1 inline" />
            propose
          </button>
        )}
      </td>
      <td className="p-3 text-right">
        {state.kind === "approved" ? (
          <button
            type="button"
            className="nerd-btn text-xs py-1 px-2"
            onClick={onReset}
            title="undo approval"
          >
            <Undo2 className="w-3 h-3" />
          </button>
        ) : state.kind === "proposed" ? (
          <span className="inline-flex gap-1">
            <button
              type="button"
              className="nerd-btn text-xs py-1 px-2 text-keboola border-keboola/40"
              onClick={() => onApprove(draftValue)}
            >
              <Check className="w-3 h-3 mr-1 inline" />
              approve
            </button>
            <button
              type="button"
              className="nerd-btn text-xs py-1 px-2"
              onClick={onReject}
            >
              <X className="w-3 h-3" />
            </button>
          </span>
        ) : state.kind === "rejected" ? (
          <button
            type="button"
            className="nerd-btn text-xs py-1 px-2"
            onClick={onReset}
          >
            re-try
          </button>
        ) : (
          <button
            type="button"
            className="nerd-btn text-xs py-1 px-2 text-keboola border-keboola/40"
            onClick={() => onApprove(fallbackType)}
            title="approve the inferred default without asking AI"
          >
            <Check className="w-3 h-3 mr-1 inline" />
            use default
          </button>
        )}
      </td>
    </tr>
  );
}

function ApplyHelpDrawer({
  project,
  tableId,
  decisions,
  profiles,
  onClose,
}: {
  project: string | null;
  tableId: string | null;
  decisions: Record<string, ColumnState>;
  profiles: ColumnProfile[];
  onClose: () => void;
}) {
  const plan = useMemo(() => {
    return profiles
      .map((p) => {
        const state = decisions[p.name];
        if (!state || state.kind !== "approved") return null;
        return { name: p.name, type: state.type };
      })
      .filter((x): x is { name: string; type: string } => x !== null);
  }, [decisions, profiles]);

  return (
    <Drawer
      open={true}
      onClose={onClose}
      title="Apply native types -- a Playbook is the right home"
      subtitle={`${plan.length} columns approved on ${tableId ?? "(no table)"}`}
    >
      <div className="space-y-4">
        <div className="nerd-card">
          <div className="text-xs uppercase tracking-widest text-zinc-500 mb-2">
            What this app does
          </div>
          <p className="text-sm text-zinc-700 dark:text-zinc-300">
            Inspector profiles a Storage table and lets you approve a native
            type per column. The output is a typed column list — not the
            actual table swap.
          </p>
        </div>

        <div className="nerd-card">
          <div className="text-xs uppercase tracking-widest text-zinc-500 mb-2">
            What "Apply" would do (Playbook scope)
          </div>
          <ol className="text-sm text-zinc-700 dark:text-zinc-300 list-decimal pl-5 space-y-1">
            <li>
              Create a dev branch (
              <span className="font-mono text-accent">{project ?? "<project>"}</span>) so prod is
              untouched.
            </li>
            <li>
              Create a new typed table next to{" "}
              <span className="font-mono text-accent">{tableId ?? "<table>"}</span> with the
              approved column types.
            </li>
            <li>
              Reload the source data into the new table; report any rows that
              fail typing.
            </li>
            <li>
              Re-run downstream configurations against the typed table in the
              branch.
            </li>
            <li>
              Compare results to the production output via SQL in a workspace.
            </li>
            <li>
              If clean: swap (
              <span className="font-mono">POST /storage/tables/{`{project}`}/{`{table_id}`}/swap</span>
              ); else surface the diff and abort.
            </li>
          </ol>
          <p className="text-xs text-zinc-500 mt-3">
            That sequence is linear, has HITL checkpoints, needs budget enforcement, and writes
            to production -- exactly what the Playbook runtime is for. This Inspector app
            produces the input; the Playbook executes it.
          </p>
        </div>

        <div className="nerd-card">
          <div className="text-xs uppercase tracking-widest text-zinc-500 mb-2">
            Approved column types ({plan.length})
          </div>
          {plan.length === 0 ? (
            <p className="text-sm text-zinc-500">
              No columns approved yet. Pick "use default" or hit "propose" + "approve" first.
            </p>
          ) : (
            <pre className="text-xs font-mono bg-zinc-50 dark:bg-zinc-900 p-2 rounded overflow-x-auto">
              {plan.map((c) => `${c.name.padEnd(32)} ${c.type}`).join("\n")}
            </pre>
          )}
        </div>

        <div className="flex gap-2">
          <button
            type="button"
            className="nerd-btn"
            disabled={plan.length === 0}
            onClick={() => {
              const yaml = `# Playbook stub -- paste into a future Playbook builder\nproject: ${project}\ntable_id: ${tableId}\ntypes:\n${plan
                .map((c) => `  ${c.name}: ${c.type}`)
                .join("\n")}\n`;
              navigator.clipboard.writeText(yaml);
              window.alert("Copied stub YAML to clipboard. Wire to /playbooks once that runtime ships.");
            }}
          >
            Copy as Playbook stub
          </button>
          <button type="button" className="nerd-btn" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </Drawer>
  );
}

/**
 * Compose a tight prompt that gives Kai enough signal to pick a type
 * without losing tokens on chitchat. The "respond with only the type"
 * instruction is repeated because Kai can be chatty otherwise.
 */
function buildPropositionPrompt(detail: TableDetail | undefined, col: ColumnProfile): string {
  const tableId = detail?.table_id ?? "(unknown)";
  const ctx = [
    `Table: ${tableId}`,
    `Column: ${col.name}`,
    `Inferred basic type from data: ${col.inferredType}`,
    `Null ratio: ${(col.nullRatio * 100).toFixed(0)}%`,
    `Distinct values: ${col.distinctCount} of ${col.sampleSize}`,
    col.minLength !== null
      ? `String length: min ${col.minLength}, max ${col.maxLength}`
      : "",
    col.samples.length > 0
      ? `Sample values: ${col.samples.map((s) => JSON.stringify(s.slice(0, 80))).join(", ")}`
      : "",
  ]
    .filter(Boolean)
    .join("\n");
  return (
    `Propose a single Snowflake column type for this column in a Keboola Storage table.\n\n` +
    `${ctx}\n\n` +
    `Reply with ONLY the type expression (e.g. "VARCHAR(128)", "INTEGER", "TIMESTAMP_NTZ"). ` +
    `No explanation, no markdown, no surrounding quotes. Just the type.`
  );
}

