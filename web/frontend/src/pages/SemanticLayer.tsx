import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  Download,
  GitCompare,
  KeyRound,
  PackagePlus,
  Pencil,
  Plus,
  RefreshCw,
  Send,
  Trash2,
  Upload,
  XCircle,
} from "lucide-react";
import { useMemo, useState } from "react";
import { api } from "../api/client";
import { Drawer } from "../components/Drawer";
import { Empty, ErrorBox, Loading, PageTitle } from "../components/Empty";
import { JsonView } from "../components/JsonView";
import { DataTable } from "../components/Table";
import { useUIState } from "../state";
import {
  BuildDialog,
  DiffDialog,
  ImportDialog,
  PromoteDialog,
  TokenEncryptDialog,
} from "./SemanticLayerDialogs";

/**
 * Semantic Layer (Keboola Metastore) page.
 *
 * Mirrors the `kbagent semantic-layer` CLI surface (v0.41.0+) for full
 * CRUD on models and the five entity kinds — metric, dataset,
 * relationship, constraint, glossary — plus every read-side workflow
 * operation: Validate, Export, Diff, Promote, Import, Build, and the
 * project-scoped Token-encrypt utility (Phase 3 dialogs live in
 * SemanticLayerDialogs.tsx).
 *
 * Design notes:
 * - Schema-driven forms: each entity kind has a declarative field list
 *   (see ENTITY_SCHEMAS) that drives both the Add and Edit drawer
 *   bodies. Avoids hand-coding 5 separate form components and keeps
 *   field validation rules in one place.
 * - The CLI distinguishes "model" by name OR uuid; the UI defaults to
 *   the first model returned by /models and lets the user swap via a
 *   dropdown. UUIDs surface in the JsonView; users mostly interact by
 *   model name.
 * - Constraint orphan validation, cascade-rename, and severity bands
 *   are CLI-only quirks documented in gotchas.md (since v0.41.0). We
 *   surface the underlying API errors but do not replicate the CLI's
 *   preflight prose in UI form labels.
 */

// ── Types from /semantic-layer/* responses ─────────────────────────

interface Model {
  uuid: string;
  name: string;
  description?: string;
  sql_dialect?: string;
  created?: string;
  updated?: string;
}

interface ModelsResponse {
  project_alias: string;
  models: Model[];
}

interface EntityRow {
  uuid?: string;
  name?: string;
  // metric
  sql?: string;
  dataset?: string;
  description?: string;
  // dataset
  table_id?: string;
  grain?: string;
  primary_key?: string[];
  // relationship
  from?: string;
  to?: string;
  on?: string;
  type?: string;
  // constraint
  constraint_type?: string;
  rule?: string;
  metrics?: string[];
  severity?: string;
  // glossary
  term?: string;
  definition?: string;
  // catch-all so JsonView can render unmodelled fields
  [k: string]: unknown;
}

interface ShowResponse {
  project_alias: string;
  model: { uuid: string; name: string; sql_dialect?: string };
  datasets: EntityRow[];
  metrics: EntityRow[];
  relationships: EntityRow[];
  constraints: EntityRow[];
  glossary: EntityRow[];
}

type EntityKind = "metric" | "dataset" | "relationship" | "constraint" | "glossary";

// ── Schema-driven form definitions ────────────────────────────────

interface FieldDef {
  key: string;
  label: string;
  type: "text" | "textarea" | "select" | "list" | "boolean";
  required?: boolean;
  options?: string[]; // for select
  placeholder?: string;
  help?: string;
  // The CLI accepts the field at different names for Add vs Edit
  // (e.g. metric.sql for Add, metric.new_sql for Edit). The form sends
  // the raw key; the backend payload mapper handles the new_* prefix.
  editKey?: string; // for edit mode, the new_* variant
}

interface EntitySchema {
  kind: EntityKind;
  pluralLabel: string; // "Metrics"
  pkField: string; // primary identifier displayed in the table + sent on edit/delete (usually "name", glossary uses "term")
  fields: FieldDef[];
  tableColumns: Array<{ header: string; cell: (row: EntityRow) => React.ReactNode }>;
}

const ENTITY_SCHEMAS: Record<EntityKind, EntitySchema> = {
  metric: {
    kind: "metric",
    pluralLabel: "Metrics",
    pkField: "name",
    fields: [
      {
        key: "name",
        label: "Name",
        type: "text",
        required: true,
        placeholder: "monthly_active_users",
        help: "Lowercase + underscores. Must be unique within the model.",
      },
      {
        key: "sql",
        editKey: "new_sql",
        label: "SQL expression",
        type: "textarea",
        required: true,
        placeholder: "COUNT(DISTINCT user_id)",
        help: "Aggregation expression evaluated against the dataset.",
      },
      {
        key: "dataset",
        editKey: "new_dataset",
        label: "Dataset (FQN)",
        type: "text",
        required: true,
        placeholder: "in.c-users.events",
        help: "Storage table this metric aggregates over.",
      },
      {
        key: "description",
        editKey: "new_description",
        label: "Description",
        type: "textarea",
      },
    ],
    tableColumns: [
      { header: "Name", cell: (r) => <span className="font-bold">{r.name}</span> },
      {
        header: "Dataset",
        cell: (r) => <span className="font-mono text-xs text-accent">{r.dataset}</span>,
      },
      {
        header: "SQL",
        cell: (r) => (
          <code className="text-xs text-zinc-600 dark:text-zinc-400 truncate block max-w-md">
            {r.sql}
          </code>
        ),
      },
    ],
  },

  dataset: {
    kind: "dataset",
    pluralLabel: "Datasets",
    pkField: "name",
    fields: [
      {
        key: "name",
        label: "Name",
        type: "text",
        required: true,
        placeholder: "events",
        help: "Lowercase + underscores. Defaults to the table name if omitted.",
      },
      {
        key: "table_id",
        label: "Table ID",
        type: "text",
        required: true,
        placeholder: "in.c-users.events",
        help: "Storage table FQN. Used to derive the dataset's FQN.",
      },
      {
        key: "grain",
        editKey: "new_grain",
        label: "Grain",
        type: "text",
        placeholder: "user_id, event_timestamp",
        help: "Comma-separated columns that uniquely identify a row.",
      },
      {
        key: "primary_key",
        label: "Primary key",
        type: "list",
        placeholder: "user_id, event_id",
        help: "Columns making up the primary key (one per line or comma-separated).",
      },
      {
        key: "description",
        editKey: "new_description",
        label: "Description",
        type: "textarea",
      },
      {
        key: "deep_fields",
        label: "Probe column types",
        type: "boolean",
        help: "When true, kbagent queries Snowflake to populate column metadata + role classification (slow).",
      },
    ],
    tableColumns: [
      { header: "Name", cell: (r) => <span className="font-bold">{r.name}</span> },
      {
        header: "Table",
        cell: (r) => <span className="font-mono text-xs text-accent">{r.table_id}</span>,
      },
      {
        header: "Grain",
        cell: (r) => <span className="text-xs text-zinc-500">{r.grain || "—"}</span>,
      },
    ],
  },

  relationship: {
    kind: "relationship",
    pluralLabel: "Relationships",
    pkField: "name",
    fields: [
      {
        key: "name",
        label: "Name",
        type: "text",
        required: true,
        placeholder: "users_to_events",
      },
      {
        key: "from",
        editKey: "new_from",
        label: "From dataset",
        type: "text",
        required: true,
        placeholder: "users",
      },
      {
        key: "to",
        editKey: "new_to",
        label: "To dataset",
        type: "text",
        required: true,
        placeholder: "events",
      },
      {
        key: "on",
        editKey: "new_on",
        label: "Join expression",
        type: "text",
        required: true,
        placeholder: "users.id = events.user_id",
      },
      {
        key: "type",
        editKey: "new_type",
        label: "Join type",
        type: "select",
        options: ["left", "inner", "right", "outer"],
      },
    ],
    tableColumns: [
      { header: "Name", cell: (r) => <span className="font-bold">{r.name}</span> },
      {
        header: "From → To",
        cell: (r) => (
          <span className="text-xs">
            <span className="font-mono text-accent">{r.from}</span>
            <span className="text-zinc-500 mx-1">→</span>
            <span className="font-mono text-accent">{r.to}</span>
          </span>
        ),
      },
      {
        header: "Join",
        cell: (r) => (
          <span className="text-xs text-zinc-500">
            <span className="nerd-pill text-[10px] mr-1">{r.type || "left"}</span>
            <code>{r.on}</code>
          </span>
        ),
      },
    ],
  },

  constraint: {
    kind: "constraint",
    pluralLabel: "Constraints",
    pkField: "name",
    fields: [
      {
        key: "name",
        label: "Name",
        type: "text",
        required: true,
        placeholder: "revenue_above_zero_critical",
        help: "Lowercase + underscores. Suffix with _critical / _warning / _healthy / _review for health-band classification.",
      },
      {
        key: "constraint_type",
        editKey: "new_constraint_type",
        label: "Type",
        type: "select",
        required: true,
        options: [
          "inequality",
          "equality",
          "range",
          "composition",
          "exclusion",
          "temporal",
          "conditional",
        ],
      },
      {
        key: "rule",
        editKey: "new_rule",
        label: "Rule expression",
        type: "textarea",
        required: true,
        placeholder: "monthly_revenue > 0",
        help: "STRING expression (not an object). Plain SQL boolean predicate.",
      },
      {
        key: "metrics",
        editKey: "new_metrics",
        label: "Applies to metrics",
        type: "list",
        placeholder: "monthly_revenue, daily_active_users",
        help: "Comma-separated metric names.",
      },
      {
        key: "severity",
        editKey: "new_severity",
        label: "Severity",
        type: "select",
        options: ["warning", "critical", "info"],
        help: "API enum is 3-level; the 4-band health (critical / warning / healthy / review) lives in the name suffix.",
      },
    ],
    tableColumns: [
      { header: "Name", cell: (r) => <span className="font-bold">{r.name}</span> },
      {
        header: "Type",
        cell: (r) => <span className="nerd-pill text-[10px]">{r.constraint_type}</span>,
      },
      {
        header: "Severity",
        cell: (r) => {
          const s = r.severity || "warning";
          const cls =
            s === "critical"
              ? "nerd-pill-red"
              : s === "warning"
                ? "nerd-pill-amber"
                : "nerd-pill";
          return <span className={`${cls} text-[10px]`}>{s}</span>;
        },
      },
      {
        header: "Rule",
        cell: (r) => (
          <code className="text-xs text-zinc-600 dark:text-zinc-400 truncate block max-w-md">
            {r.rule}
          </code>
        ),
      },
    ],
  },

  glossary: {
    kind: "glossary",
    pluralLabel: "Glossary",
    pkField: "term",
    fields: [
      {
        key: "term",
        editKey: "new_term",
        label: "Term",
        type: "text",
        required: true,
        placeholder: "MAU",
        help: "The term being defined. Used as the unique identifier for edit/delete.",
      },
      {
        key: "definition",
        editKey: "new_definition",
        label: "Definition",
        type: "textarea",
        required: true,
        placeholder: "Monthly Active Users — distinct users observed in a calendar month.",
      },
    ],
    tableColumns: [
      { header: "Term", cell: (r) => <span className="font-bold">{r.term}</span> },
      {
        header: "Definition",
        cell: (r) => (
          <span className="text-xs text-zinc-600 dark:text-zinc-400 truncate block max-w-2xl">
            {r.definition}
          </span>
        ),
      },
    ],
  },
};

const ENTITY_KINDS: EntityKind[] = ["metric", "dataset", "relationship", "constraint", "glossary"];

// ── Page ──────────────────────────────────────────────────────────

export function SemanticLayerPage() {
  const { project } = useUIState();
  const [activeModel, setActiveModel] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<EntityKind>("metric");
  const [showNewModel, setShowNewModel] = useState(false);
  // Phase 3 workflow dialogs. State lives on the page so the buttons can
  // sit in the page header (Build/Import/Token are project-scoped, not
  // model-scoped) while Diff/Promote get triggered from ModelHeader.
  const [showDiff, setShowDiff] = useState(false);
  const [showPromote, setShowPromote] = useState(false);
  const [showImport, setShowImport] = useState(false);
  const [showBuild, setShowBuild] = useState(false);
  const [showToken, setShowToken] = useState(false);
  const qc = useQueryClient();

  const modelsQ = useQuery<ModelsResponse>({
    queryKey: ["sl-models", project],
    queryFn: () => api.get("/semantic-layer/models", { query: { project: project! } }),
    enabled: !!project,
  });

  // Auto-select the first model on load if none is active.
  const models = modelsQ.data?.models ?? [];
  const resolvedModel =
    activeModel ?? (models.length > 0 ? models[0].name : null);

  if (!project) {
    return (
      <div className="space-y-4">
        <PageTitle
          title="Semantic Layer"
          description="Keboola Metastore — models, metrics, datasets, relationships, constraints, glossary."
        />
        <Empty
          title="Pick a project"
          hint="Use the top-bar project picker to scope the semantic-layer view."
        />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <PageTitle
        title="Semantic Layer"
        description={`Keboola Metastore for ${project}. Models, metrics, datasets, relationships, constraints, glossary.`}
        actions={
          <div className="flex items-center gap-1 flex-wrap">
            <button
              type="button"
              className="nerd-btn flex items-center gap-1 hover:text-keboola"
              onClick={() => setShowNewModel(true)}
            >
              <Plus className="w-3 h-3" /> New model
            </button>
            <button
              type="button"
              className="nerd-btn flex items-center gap-1 hover:text-keboola"
              onClick={() => setShowBuild(true)}
              title="Heuristic greenfield builder — pick tables, get a starter model"
            >
              <PackagePlus className="w-3 h-3" /> Build
            </button>
            <button
              type="button"
              className="nerd-btn flex items-center gap-1 hover:text-keboola"
              onClick={() => setShowImport(true)}
              title="Replay a JSON snapshot into a model"
            >
              <Upload className="w-3 h-3" /> Import
            </button>
            <button
              type="button"
              className="nerd-btn flex items-center gap-1 hover:text-keboola"
              onClick={() => setShowToken(true)}
              title="Encrypt project storage token for transformation user_properties"
            >
              <KeyRound className="w-3 h-3" /> Encrypt token
            </button>
          </div>
        }
      />

      {modelsQ.isLoading ? <Loading /> : null}
      {modelsQ.error ? <ErrorBox message={(modelsQ.error as Error).message} /> : null}

      {modelsQ.data && models.length === 0 ? (
        <Empty
          title="No semantic-layer models yet"
          hint="Click 'New model' above to create your first one, or run `kbagent sl build` from the CLI for an AI-assisted greenfield."
        />
      ) : null}

      {models.length > 0 ? (
        <ModelHeader
          models={models}
          activeModel={resolvedModel}
          onChange={setActiveModel}
          project={project}
          onOpenDiff={() => setShowDiff(true)}
          onOpenPromote={() => setShowPromote(true)}
        />
      ) : null}

      {resolvedModel ? (
        <ModelDetail
          project={project}
          model={resolvedModel}
          activeTab={activeTab}
          setActiveTab={setActiveTab}
        />
      ) : null}

      {showNewModel ? (
        <NewModelDrawer
          project={project}
          onClose={() => setShowNewModel(false)}
          onCreated={(name) => {
            setShowNewModel(false);
            qc.invalidateQueries({ queryKey: ["sl-models", project] });
            setActiveModel(name);
          }}
        />
      ) : null}

      {showDiff ? (
        <DiffDialog initialProject={project} onClose={() => setShowDiff(false)} />
      ) : null}

      {showPromote ? (
        <PromoteDialog
          initialFromProject={project}
          onClose={() => setShowPromote(false)}
        />
      ) : null}

      {showImport ? (
        <ImportDialog
          initialProject={project}
          initialModel={resolvedModel ?? ""}
          onClose={() => setShowImport(false)}
          onImported={() => {
            // Refresh the show query so the new entities surface immediately.
            qc.invalidateQueries({ queryKey: ["sl-show"] });
            qc.invalidateQueries({ queryKey: ["sl-models", project] });
          }}
        />
      ) : null}

      {showBuild ? (
        <BuildDialog
          initialProject={project}
          initialModel={resolvedModel ?? ""}
          onClose={() => setShowBuild(false)}
          onBuilt={() => {
            // Built model may be brand new — refresh the model list so it
            // appears in the dropdown and gets auto-selected.
            qc.invalidateQueries({ queryKey: ["sl-models", project] });
            qc.invalidateQueries({ queryKey: ["sl-show"] });
          }}
        />
      ) : null}

      {showToken ? (
        <TokenEncryptDialog initialProject={project} onClose={() => setShowToken(false)} />
      ) : null}
    </div>
  );
}

// ── Header: model picker + workflow actions (Validate, Export) ────

function ModelHeader({
  models,
  activeModel,
  onChange,
  project,
  onOpenDiff,
  onOpenPromote,
}: {
  models: Model[];
  activeModel: string | null;
  onChange: (m: string) => void;
  project: string;
  onOpenDiff: () => void;
  onOpenPromote: () => void;
}) {
  const qc = useQueryClient();
  const [showValidate, setShowValidate] = useState(false);
  const [deepValidate, setDeepValidate] = useState(false);

  const deleteMu = useMutation({
    mutationFn: (model: string) =>
      api.delete(`/semantic-layer/models/${encodeURIComponent(model)}`, {
        query: { project },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["sl-models", project] });
    },
  });

  const exportMu = useMutation<{ snapshot: unknown; model?: { name?: string } }>({
    mutationFn: () =>
      api.get("/semantic-layer/export", {
        query: { project, model: activeModel ?? undefined },
      }),
    onSuccess: (data) => {
      // Drop the snapshot to disk so users can pipe it through `kbagent sl
      // import` or version-control it. Filename includes the model name +
      // a UTC timestamp so successive exports don't overwrite each other.
      const blob = new Blob([JSON.stringify(data, null, 2)], {
        type: "application/json;charset=utf-8",
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "");
      a.download = `sl-${data.model?.name ?? activeModel ?? "model"}-${stamp}.json`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    },
  });

  return (
    <div className="nerd-card flex items-center justify-between flex-wrap gap-2">
      <div className="flex items-center gap-3 flex-wrap">
        <span className="text-[10px] uppercase tracking-wider text-zinc-500">Model:</span>
        <select
          className="nerd-input text-sm py-1"
          value={activeModel ?? ""}
          onChange={(e) => onChange(e.target.value)}
        >
          {models.map((m) => (
            <option key={m.uuid} value={m.name}>
              {m.name}
            </option>
          ))}
        </select>
        {activeModel ? (
          <button
            type="button"
            className="nerd-btn text-xs hover:text-red-400 hover:border-red-700"
            onClick={() => {
              if (confirm(`Delete model '${activeModel}'? This deletes every entity it contains.`)) {
                deleteMu.mutate(activeModel);
              }
            }}
            disabled={deleteMu.isPending}
            title="Delete this model + everything in it"
          >
            <Trash2 className="w-3 h-3" />
          </button>
        ) : null}
      </div>
      <div className="flex items-center gap-2 flex-wrap">
        <label className="flex items-center gap-1 text-xs text-zinc-500">
          <input
            type="checkbox"
            checked={deepValidate}
            onChange={(e) => setDeepValidate(e.target.checked)}
          />
          deep
        </label>
        <button
          type="button"
          className="nerd-btn text-xs flex items-center gap-1 hover:text-keboola"
          onClick={() => setShowValidate(true)}
        >
          <CheckCircle2 className="w-3 h-3" /> Validate
        </button>
        <button
          type="button"
          className="nerd-btn text-xs flex items-center gap-1 hover:text-keboola"
          onClick={() => exportMu.mutate()}
          disabled={exportMu.isPending}
        >
          <Download className="w-3 h-3" /> Export
        </button>
        <button
          type="button"
          className="nerd-btn text-xs flex items-center gap-1 hover:text-keboola"
          onClick={onOpenDiff}
          title="Diff two snapshots (project↔project, project↔file, file↔file)"
        >
          <GitCompare className="w-3 h-3" /> Diff
        </button>
        <button
          type="button"
          className="nerd-btn text-xs flex items-center gap-1 hover:text-keboola"
          onClick={onOpenPromote}
          title="Promote this model to another project (with dry-run preview)"
        >
          <Send className="w-3 h-3" /> Promote
        </button>
      </div>

      {showValidate && activeModel ? (
        <ValidateDrawer
          project={project}
          model={activeModel}
          deep={deepValidate}
          onClose={() => setShowValidate(false)}
        />
      ) : null}
    </div>
  );
}

function ValidateDrawer({
  project,
  model,
  deep,
  onClose,
}: {
  project: string;
  model: string;
  deep: boolean;
  onClose: () => void;
}) {
  const q = useQuery({
    queryKey: ["sl-validate", project, model, deep],
    queryFn: () =>
      api.get("/semantic-layer/validate", {
        query: { project, model, deep },
      }),
  });
  return (
    <Drawer
      open={true}
      onClose={onClose}
      title={`Validate · ${model}`}
      subtitle={deep ? "deep (Snowflake column probes)" : "structural checks only"}
    >
      {q.isLoading ? <Loading /> : null}
      {q.error ? <ErrorBox message={(q.error as Error).message} /> : null}
      {q.data ? <JsonView data={q.data} /> : null}
    </Drawer>
  );
}

function NewModelDrawer({
  project,
  onClose,
  onCreated,
}: {
  project: string;
  onClose: () => void;
  onCreated: (name: string) => void;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  // Metastore is case-sensitive on the dialect name. Service default is
  // "Snowflake" (capital S) -- lower-case "snowflake" trips a 422.
  const [sqlDialect, setSqlDialect] = useState("Snowflake");
  const [error, setError] = useState<string | null>(null);

  const mu = useMutation<{ model?: { name?: string } }>({
    mutationFn: () =>
      api.post("/semantic-layer/models", {
        project,
        name,
        description,
        sql_dialect: sqlDialect,
      }),
    onSuccess: (data) => onCreated(data.model?.name ?? name),
    onError: (err) => setError((err as Error).message),
  });

  return (
    <Drawer open={true} onClose={onClose} title="New semantic-layer model" width="40rem">
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          setError(null);
          mu.mutate();
        }}
      >
        <label className="block text-xs text-zinc-500">
          Name
          <input
            className="nerd-input w-full mt-1"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            placeholder="core_metrics"
          />
        </label>
        <label className="block text-xs text-zinc-500">
          Description
          <textarea
            className="nerd-input w-full mt-1 h-20"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </label>
        <label className="block text-xs text-zinc-500">
          SQL dialect
          <select
            className="nerd-input w-full mt-1"
            value={sqlDialect}
            onChange={(e) => setSqlDialect(e.target.value)}
          >
            <option value="Snowflake">Snowflake</option>
            <option value="BigQuery">BigQuery</option>
          </select>
        </label>
        {error ? <ErrorBox message={error} /> : null}
        <div className="flex gap-2">
          <button
            type="submit"
            className="nerd-btn hover:text-keboola"
            disabled={mu.isPending}
          >
            {mu.isPending ? "Creating…" : "Create model"}
          </button>
          <button type="button" className="nerd-btn" onClick={onClose}>
            Cancel
          </button>
        </div>
      </form>
    </Drawer>
  );
}

// ── Model detail: tabs per entity kind ─────────────────────────────

function ModelDetail({
  project,
  model,
  activeTab,
  setActiveTab,
}: {
  project: string;
  model: string;
  activeTab: EntityKind;
  setActiveTab: (k: EntityKind) => void;
}) {
  const showQ = useQuery<ShowResponse>({
    queryKey: ["sl-show", project, model],
    queryFn: () =>
      api.get("/semantic-layer/show", {
        query: { project, model },
      }),
  });

  // Map UI tab -> show response field. (plurals)
  const kindToField: Record<EntityKind, keyof ShowResponse> = {
    metric: "metrics",
    dataset: "datasets",
    relationship: "relationships",
    constraint: "constraints",
    glossary: "glossary",
  };

  const rows = useMemo<EntityRow[]>(() => {
    if (!showQ.data) return [];
    return (showQ.data[kindToField[activeTab]] ?? []) as EntityRow[];
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showQ.data, activeTab]);

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2 flex-wrap">
        {ENTITY_KINDS.map((k) => {
          const count = showQ.data
            ? (showQ.data[kindToField[k]] as EntityRow[] | undefined)?.length ?? 0
            : null;
          const schema = ENTITY_SCHEMAS[k];
          return (
            <button
              key={k}
              type="button"
              className={`nerd-btn flex items-center gap-1 ${
                activeTab === k ? "border-keboola text-keboola" : ""
              }`}
              onClick={() => setActiveTab(k)}
            >
              <span>{schema.pluralLabel}</span>
              {count !== null ? (
                <span className="text-[10px] text-zinc-500">({count})</span>
              ) : null}
            </button>
          );
        })}
        <button
          type="button"
          className="nerd-btn text-xs ml-auto hover:text-keboola"
          onClick={() => showQ.refetch()}
          disabled={showQ.isFetching}
          title="Reload entities"
        >
          <RefreshCw className="w-3 h-3" />
        </button>
      </div>

      {showQ.isLoading ? <Loading /> : null}
      {showQ.error ? <ErrorBox message={(showQ.error as Error).message} /> : null}

      {showQ.data ? (
        <EntityPanel
          project={project}
          model={model}
          kind={activeTab}
          rows={rows}
          onChange={() => showQ.refetch()}
        />
      ) : null}
    </div>
  );
}

// ── Entity panel: table + add button + edit/delete row actions ────

function EntityPanel({
  project,
  model,
  kind,
  rows,
  onChange,
}: {
  project: string;
  model: string;
  kind: EntityKind;
  rows: EntityRow[];
  onChange: () => void;
}) {
  const schema = ENTITY_SCHEMAS[kind];
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<EntityRow | null>(null);
  const [inspecting, setInspecting] = useState<EntityRow | null>(null);

  const deleteMu = useMutation({
    mutationFn: (row: EntityRow) => {
      const pkValue = row[schema.pkField as keyof EntityRow] as string;
      return api.delete(
        `/semantic-layer/items/${kind}/${encodeURIComponent(pkValue)}`,
        { query: { project, model } },
      );
    },
    onSuccess: onChange,
  });

  return (
    <>
      <div className="nerd-card">
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-keboola font-bold text-sm">{schema.pluralLabel}</h3>
          <button
            type="button"
            className="nerd-btn text-xs flex items-center gap-1 hover:text-keboola"
            onClick={() => setAdding(true)}
          >
            <Plus className="w-3 h-3" /> Add {kind}
          </button>
        </div>
        {rows.length === 0 ? (
          <Empty title={`No ${schema.pluralLabel.toLowerCase()} yet`} />
        ) : (
          <DataTable
            rows={rows}
            rowKey={(r) => (r[schema.pkField] as string) ?? JSON.stringify(r).slice(0, 32)}
            onRowClick={(r) => setInspecting(r)}
            columns={[
              ...schema.tableColumns,
              {
                header: "",
                align: "right",
                cell: (r) => (
                  <div className="flex justify-end gap-1">
                    <button
                      type="button"
                      className="nerd-btn text-xs"
                      onClick={(e) => {
                        e.stopPropagation();
                        setEditing(r);
                      }}
                      title="Edit"
                    >
                      <Pencil className="w-3 h-3" />
                    </button>
                    <button
                      type="button"
                      className="nerd-btn text-xs hover:text-red-400 hover:border-red-700"
                      onClick={(e) => {
                        e.stopPropagation();
                        const pk = r[schema.pkField] as string;
                        if (confirm(`Delete ${kind} '${pk}'?`)) deleteMu.mutate(r);
                      }}
                      title="Delete"
                    >
                      <Trash2 className="w-3 h-3" />
                    </button>
                  </div>
                ),
              },
            ]}
          />
        )}
      </div>

      {adding ? (
        <EntityFormDrawer
          mode="add"
          project={project}
          model={model}
          kind={kind}
          initial={null}
          onClose={() => setAdding(false)}
          onSaved={() => {
            setAdding(false);
            onChange();
          }}
        />
      ) : null}
      {editing ? (
        <EntityFormDrawer
          mode="edit"
          project={project}
          model={model}
          kind={kind}
          initial={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            onChange();
          }}
        />
      ) : null}
      {inspecting ? (
        <Drawer
          open={true}
          onClose={() => setInspecting(null)}
          title={`${kind} · ${inspecting[schema.pkField] ?? "(no name)"}`}
        >
          <JsonView data={inspecting} />
        </Drawer>
      ) : null}
    </>
  );
}

// ── Generic schema-driven add/edit form drawer ─────────────────────

function EntityFormDrawer({
  mode,
  project,
  model,
  kind,
  initial,
  onClose,
  onSaved,
}: {
  mode: "add" | "edit";
  project: string;
  model: string;
  kind: EntityKind;
  initial: EntityRow | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const schema = ENTITY_SCHEMAS[kind];
  const qc = useQueryClient();
  const [values, setValues] = useState<Record<string, unknown>>(() => {
    // Seed edit form with current values; add form starts empty.
    if (mode === "edit" && initial) {
      const seed: Record<string, unknown> = {};
      for (const f of schema.fields) {
        const v = initial[f.key];
        if (v !== undefined) seed[f.key] = v;
      }
      return seed;
    }
    return {};
  });
  const [error, setError] = useState<string | null>(null);

  const saveMu = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = { project, model };
      // Map UI keys → API keys (edit uses new_* variants).
      for (const f of schema.fields) {
        if (mode === "edit" && f.editKey) {
          if (values[f.key] !== undefined) body[f.editKey] = values[f.key];
        } else {
          body[f.key] = values[f.key];
        }
      }
      // List-typed fields arrive as comma/newline-separated strings; coerce.
      for (const f of schema.fields) {
        if (f.type !== "list") continue;
        const targetKey = mode === "edit" && f.editKey ? f.editKey : f.key;
        const raw = body[targetKey];
        if (typeof raw === "string") {
          body[targetKey] = raw
            .split(/[,\n]/)
            .map((s) => s.trim())
            .filter(Boolean);
        }
      }
      if (mode === "add") {
        return api.post(`/semantic-layer/items/${kind}`, body);
      }
      const pk = initial?.[schema.pkField] as string;
      return api.put(
        `/semantic-layer/items/${kind}/${encodeURIComponent(pk)}`,
        body,
      );
    },
    onSuccess: () => {
      // Force the parent's show-query to refetch BEFORE the drawer unmounts —
      // the parent's refetch() callback alone occasionally races with React
      // strict-mode double-effects, leaving the entity table stale. Invalidating
      // here makes the refresh deterministic.
      qc.invalidateQueries({ queryKey: ["sl-show", project, model] });
      onSaved();
    },
    onError: (err) => setError((err as Error).message),
  });

  const pkValue = initial?.[schema.pkField] as string | undefined;
  const title =
    mode === "add"
      ? `Add ${kind}`
      : `Edit ${kind} · ${pkValue ?? "(no id)"}`;

  return (
    <Drawer open={true} onClose={onClose} title={title} width="44rem">
      <form
        className="space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          setError(null);
          saveMu.mutate();
        }}
      >
        {schema.fields.map((f) => (
          <FieldInput
            key={f.key}
            field={f}
            mode={mode}
            value={values[f.key]}
            onChange={(v) => setValues((prev) => ({ ...prev, [f.key]: v }))}
          />
        ))}
        {error ? <ErrorBox message={error} /> : null}
        <div className="flex gap-2 pt-2">
          <button
            type="submit"
            className="nerd-btn hover:text-keboola"
            disabled={saveMu.isPending}
          >
            {saveMu.isPending ? "Saving…" : mode === "add" ? "Add" : "Save changes"}
          </button>
          <button type="button" className="nerd-btn" onClick={onClose}>
            Cancel
          </button>
        </div>
      </form>
    </Drawer>
  );
}

function FieldInput({
  field,
  mode,
  value,
  onChange,
}: {
  field: FieldDef;
  mode: "add" | "edit";
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  // In edit mode, optional fields with no value remain unset so the
  // backend treats them as "no change". Required flag is only honored
  // for Add mode -- edit accepts a subset.
  const required = mode === "add" && field.required;
  const labelEl = (
    <span className="block text-xs text-zinc-500">
      {field.label}
      {required ? <span className="text-red-500 ml-1">*</span> : null}
    </span>
  );
  const helpEl = field.help ? (
    <span className="block text-[10px] text-zinc-500 dark:text-zinc-600 mt-0.5">
      {field.help}
    </span>
  ) : null;

  if (field.type === "boolean") {
    return (
      <label className="flex items-center gap-2 text-xs text-zinc-500">
        <input
          type="checkbox"
          checked={Boolean(value)}
          onChange={(e) => onChange(e.target.checked)}
        />
        {field.label}
        {field.help ? <span className="text-zinc-400">— {field.help}</span> : null}
      </label>
    );
  }

  if (field.type === "select") {
    return (
      <label className="block">
        {labelEl}
        <select
          className="nerd-input w-full mt-1"
          value={(value as string) ?? ""}
          onChange={(e) => onChange(e.target.value)}
          required={required}
        >
          <option value="">(unset)</option>
          {field.options?.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
        {helpEl}
      </label>
    );
  }

  if (field.type === "textarea") {
    return (
      <label className="block">
        {labelEl}
        <textarea
          className="nerd-input w-full mt-1 h-24"
          value={(value as string) ?? ""}
          onChange={(e) => onChange(e.target.value)}
          placeholder={field.placeholder}
          required={required}
        />
        {helpEl}
      </label>
    );
  }

  if (field.type === "list") {
    // List-typed values are entered as comma/newline-separated and
    // serialised back on submit. Keep the textarea form so users can
    // either paste comma-separated or one-per-line.
    const display = Array.isArray(value) ? value.join(", ") : ((value as string) ?? "");
    return (
      <label className="block">
        {labelEl}
        <textarea
          className="nerd-input w-full mt-1 h-16"
          value={display}
          onChange={(e) => onChange(e.target.value)}
          placeholder={field.placeholder}
        />
        {helpEl}
      </label>
    );
  }

  return (
    <label className="block">
      {labelEl}
      <input
        type="text"
        className="nerd-input w-full mt-1"
        value={(value as string) ?? ""}
        onChange={(e) => onChange(e.target.value)}
        placeholder={field.placeholder}
        required={required}
      />
      {helpEl}
    </label>
  );
}
