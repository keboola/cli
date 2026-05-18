/**
 * Phase 3 dialogs for the Semantic Layer page:
 *
 *   - DiffDialog     — POST /semantic-layer/diff   (project↔project, project↔file, file↔file)
 *   - PromoteDialog  — POST /semantic-layer/promote (cross-project copy with classify-and-apply)
 *   - ImportDialog   — POST /semantic-layer/import (snapshot replay with dry-run preview)
 *   - BuildDialog    — POST /semantic-layer/build  (heuristic greenfield builder)
 *   - TokenEncryptDialog — POST /semantic-layer/token/encrypt (transformation token KBC::secure)
 *
 * Split out of SemanticLayer.tsx to keep the page file under ~1500 lines.
 * Each dialog is a self-contained Drawer that the parent page mounts on
 * demand via boolean state -- no shared mutable state between dialogs.
 */

import { useMutation, useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowRight,
  Copy,
  FileUp,
  GitCompare,
  KeyRound,
  PackagePlus,
  Send,
  Sparkles,
  Upload,
} from "lucide-react";
import { useMemo, useState } from "react";
import { api } from "../api/client";
import { Drawer } from "../components/Drawer";
import { ErrorBox, Loading } from "../components/Empty";
import { JsonView } from "../components/JsonView";

// ── Shared types ─────────────────────────────────────────────────────

interface ProjectRow {
  alias: string;
  stack_url?: string;
  branch_id_default?: number;
  organization_id?: number;
  organization_name?: string;
}

interface ProjectListResponse {
  projects: ProjectRow[];
}

interface BucketTable {
  id: string;
  name?: string;
}

interface BucketRow {
  id: string;
  stage?: string;
  tables?: BucketTable[];
}

interface BucketsResponse {
  buckets: BucketRow[];
}

interface TablesResponse {
  tables: BucketTable[];
}

interface ModelRow {
  uuid: string;
  name: string;
  description?: string;
  sql_dialect?: string;
}

interface ModelsResponse {
  project_alias: string;
  models: ModelRow[];
}

const ENTITY_KEYS = ["datasets", "metrics", "relationships", "constraints", "glossary"] as const;
type EntityKey = (typeof ENTITY_KEYS)[number];

const ALL_TYPE_FILTERS: EntityKey[] = [...ENTITY_KEYS];

// ── Helpers ──────────────────────────────────────────────────────────

/** Read a File as JSON; returns parsed object or throws with a readable error. */
async function readFileAsJson(f: File): Promise<unknown> {
  const text = await f.text();
  try {
    return JSON.parse(text);
  } catch (err) {
    throw new Error(
      `${f.name}: not valid JSON (${err instanceof Error ? err.message : String(err)})`,
    );
  }
}

/**
 * Primary (filled) action button used in dialog footers. The plain `.nerd-btn`
 * is outline-only — putting two of them side-by-side looks identical to
 * "Close", so we mark the actionable one as filled-keboola for visual
 * hierarchy. Use `PrimaryButton` for the action that does the work; keep
 * "Close" / "Cancel" as plain `.nerd-btn`.
 */
function PrimaryButton({
  onClick,
  disabled,
  children,
}: {
  onClick: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className="px-3 py-1.5 rounded text-xs font-bold bg-keboola text-white border border-keboola hover:bg-keboola/80 hover:border-keboola/80 transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-1"
    >
      {children}
    </button>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[10px] uppercase tracking-widest text-zinc-500 dark:text-zinc-600 mb-1">
      {children}
    </div>
  );
}

// Re-used project picker. Lists all known aliases.
function ProjectPicker({
  value,
  onChange,
  placeholder = "(pick project)",
}: {
  value: string;
  onChange: (alias: string) => void;
  placeholder?: string;
}) {
  const projectsQ = useQuery<ProjectListResponse>({
    queryKey: ["sl-projects"],
    queryFn: () => api.get("/projects"),
  });
  const aliases = projectsQ.data?.projects?.map((p) => p.alias) ?? [];
  return (
    <select
      className="nerd-input text-sm py-1"
      value={value}
      onChange={(e) => onChange(e.target.value)}
    >
      <option value="">{placeholder}</option>
      {aliases.map((a) => (
        <option key={a} value={a}>
          {a}
        </option>
      ))}
    </select>
  );
}

// Re-used model picker scoped to a project. Returns optional model name --
// the backend treats "" as "pick the only model OR ambiguous".
function ModelPicker({
  project,
  value,
  onChange,
  placeholder = "(default model)",
  disabled,
}: {
  project: string;
  value: string;
  onChange: (model: string) => void;
  placeholder?: string;
  disabled?: boolean;
}) {
  const q = useQuery<ModelsResponse>({
    queryKey: ["sl-models", project],
    queryFn: () => api.get("/semantic-layer/models", { query: { project } }),
    enabled: !!project && !disabled,
  });
  const names = q.data?.models?.map((m) => m.name) ?? [];
  return (
    <select
      className="nerd-input text-sm py-1"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      disabled={disabled || !project}
    >
      <option value="">{placeholder}</option>
      {names.map((n) => (
        <option key={n} value={n}>
          {n}
        </option>
      ))}
    </select>
  );
}

// ── Diff dialog ──────────────────────────────────────────────────────

type SideMode = "project" | "file";

interface DiffSide {
  mode: SideMode;
  project: string;
  model: string;
  file: { name: string; content: unknown } | null;
}

interface DiffPerType {
  added: string[];
  removed: string[];
  changed: { name?: string; term?: string; diff_keys?: string[] }[];
}

interface DiffResponse {
  left: { source: string; ref?: unknown; model?: unknown };
  right: { source: string; ref?: unknown; model?: unknown };
  datasets: DiffPerType;
  metrics: DiffPerType;
  relationships: DiffPerType;
  constraints: DiffPerType;
  glossary: DiffPerType;
}

function emptySide(seedProject: string): DiffSide {
  return { mode: "project", project: seedProject, model: "", file: null };
}

function SideEditor({
  label,
  side,
  setSide,
}: {
  label: string;
  side: DiffSide;
  setSide: (s: DiffSide) => void;
}) {
  return (
    <div className="border border-zinc-200 dark:border-zinc-900 rounded p-3 space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-xs font-bold text-keboola">{label}</span>
        <div className="flex items-center gap-1 text-[10px]">
          <button
            type="button"
            className={`nerd-btn px-1.5 py-0.5 ${
              side.mode === "project" ? "border-keboola text-keboola" : ""
            }`}
            onClick={() => setSide({ ...side, mode: "project", file: null })}
          >
            Project
          </button>
          <button
            type="button"
            className={`nerd-btn px-1.5 py-0.5 ${
              side.mode === "file" ? "border-keboola text-keboola" : ""
            }`}
            onClick={() => setSide({ ...side, mode: "file" })}
          >
            File
          </button>
        </div>
      </div>

      {side.mode === "project" ? (
        <div className="space-y-1.5">
          <SectionLabel>Project</SectionLabel>
          <ProjectPicker
            value={side.project}
            onChange={(p) => setSide({ ...side, project: p, model: "" })}
          />
          <SectionLabel>Model (optional)</SectionLabel>
          <ModelPicker
            project={side.project}
            value={side.model}
            onChange={(m) => setSide({ ...side, model: m })}
          />
        </div>
      ) : (
        <div className="space-y-1.5">
          <SectionLabel>Snapshot file</SectionLabel>
          <label className="nerd-btn text-xs flex items-center gap-1 cursor-pointer w-fit">
            <FileUp className="w-3 h-3" /> {side.file ? side.file.name : "Pick JSON…"}
            <input
              type="file"
              accept=".json,application/json"
              className="hidden"
              onChange={async (e) => {
                const f = e.target.files?.[0];
                if (!f) return;
                try {
                  const json = await readFileAsJson(f);
                  setSide({ ...side, file: { name: f.name, content: json } });
                } catch (err) {
                  // surface in alert -- file pickers don't have a great
                  // inline-error story in this codebase yet.
                  alert(err instanceof Error ? err.message : String(err));
                }
                // reset input so re-picking the same file fires onChange
                e.target.value = "";
              }}
            />
          </label>
          {side.file ? (
            <div className="text-[10px] text-zinc-500">
              Loaded {side.file.name}. Diff uses inline body, the file is not uploaded.
            </div>
          ) : (
            <div className="text-[10px] text-zinc-500">
              Pick a JSON snapshot produced by Export (or `kbagent sl export`).
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function DiffPerTypePanel({ kind, data }: { kind: EntityKey; data: DiffPerType }) {
  const total = data.added.length + data.removed.length + data.changed.length;
  if (total === 0) {
    return (
      <div className="text-xs text-zinc-500 italic">
        {kind}: identical ({data.added.length === 0 ? "no items on either side" : "matched"}).
      </div>
    );
  }
  return (
    <div className="space-y-1">
      <div className="text-xs font-bold capitalize">
        {kind}{" "}
        <span className="text-zinc-500 font-normal text-[10px]">
          +{data.added.length} / −{data.removed.length} / ~{data.changed.length}
        </span>
      </div>
      {data.added.length > 0 ? (
        <div className="text-[11px] flex flex-wrap gap-1">
          <span className="text-emerald-500">+</span>
          {data.added.map((n) => (
            <span
              key={`a-${n}`}
              className="px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-700 dark:text-emerald-400"
            >
              {n}
            </span>
          ))}
        </div>
      ) : null}
      {data.removed.length > 0 ? (
        <div className="text-[11px] flex flex-wrap gap-1">
          <span className="text-red-500">−</span>
          {data.removed.map((n) => (
            <span
              key={`r-${n}`}
              className="px-1.5 py-0.5 rounded bg-red-500/10 text-red-700 dark:text-red-400"
            >
              {n}
            </span>
          ))}
        </div>
      ) : null}
      {data.changed.length > 0 ? (
        <div className="text-[11px] space-y-0.5">
          {data.changed.map((c) => {
            const id = c.name ?? c.term ?? "?";
            return (
              <div key={`c-${id}`} className="flex flex-wrap items-center gap-1">
                <span className="text-amber-500">~</span>
                <span className="px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-700 dark:text-amber-400">
                  {id}
                </span>
                {c.diff_keys && c.diff_keys.length > 0 ? (
                  <span className="text-zinc-500">
                    [{c.diff_keys.join(", ")}]
                  </span>
                ) : null}
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}

export function DiffDialog({
  initialProject,
  onClose,
}: {
  initialProject: string;
  onClose: () => void;
}) {
  const [left, setLeft] = useState<DiffSide>(emptySide(initialProject));
  const [right, setRight] = useState<DiffSide>(emptySide(""));

  const ready =
    (left.mode === "project" ? !!left.project : !!left.file) &&
    (right.mode === "project" ? !!right.project : !!right.file);

  const mu = useMutation<DiffResponse, Error>({
    mutationFn: () => {
      const body: Record<string, unknown> = {};
      if (left.mode === "project") {
        body.project_a = left.project;
        if (left.model) body.model_a = left.model;
      } else if (left.file) {
        body.file_a = left.file.content;
      }
      if (right.mode === "project") {
        body.project_b = right.project;
        if (right.model) body.model_b = right.model;
      } else if (right.file) {
        body.file_b = right.file.content;
      }
      return api.post("/semantic-layer/diff", body);
    },
  });

  return (
    <Drawer open onClose={onClose} title="Diff snapshots" width="80vw">
      <div className="space-y-4">
        <div className="grid md:grid-cols-2 gap-3">
          <SideEditor label="Left (A)" side={left} setSide={setLeft} />
          <SideEditor label="Right (B)" side={right} setSide={setRight} />
        </div>

        <div className="flex items-center gap-2 pt-2 border-t border-zinc-200 dark:border-zinc-900">
          <PrimaryButton onClick={() => mu.mutate()} disabled={!ready || mu.isPending}>
            <GitCompare className="w-3 h-3" />
            {mu.isPending ? "Diffing…" : "Run diff"}
          </PrimaryButton>
          <button type="button" className="nerd-btn text-xs" onClick={onClose}>
            Close
          </button>
        </div>

        {mu.error ? <ErrorBox message={(mu.error as Error).message} /> : null}

        {mu.data ? (
          <div className="space-y-3">
            <div className="text-[11px] text-zinc-500">
              <span className="font-mono">A:</span> {String(mu.data.left.source)} &nbsp;
              <ArrowRight className="inline w-3 h-3" /> &nbsp;
              <span className="font-mono">B:</span> {String(mu.data.right.source)}
            </div>
            <div className="grid md:grid-cols-2 gap-x-6 gap-y-3">
              {ENTITY_KEYS.map((k) => (
                <DiffPerTypePanel key={k} kind={k} data={mu.data[k]} />
              ))}
            </div>
            <details className="text-[11px]">
              <summary className="cursor-pointer text-zinc-500">Raw response</summary>
              <div className="mt-2">
                <JsonView data={mu.data} maxHeight="40vh" />
              </div>
            </details>
          </div>
        ) : null}
      </div>
    </Drawer>
  );
}

// ── Promote dialog ───────────────────────────────────────────────────

interface PromoteStats {
  new: string[];
  overwritten: string[];
  identical: string[];
  failed: { name: string; error: string }[];
}

interface PromoteResponse {
  from_project: string;
  to_project: string;
  from_model?: string;
  to_model?: string;
  dry_run: boolean;
  datasets?: PromoteStats;
  metrics?: PromoteStats;
  relationships?: PromoteStats;
  constraints?: PromoteStats;
  glossary?: PromoteStats;
}

function TypeFilterCheckboxes({
  selected,
  onChange,
}: {
  selected: EntityKey[];
  onChange: (next: EntityKey[]) => void;
}) {
  const toggle = (k: EntityKey) => {
    if (selected.includes(k)) {
      onChange(selected.filter((x) => x !== k));
    } else {
      onChange([...selected, k]);
    }
  };
  return (
    <div className="flex flex-wrap gap-2">
      {ALL_TYPE_FILTERS.map((k) => (
        <label key={k} className="flex items-center gap-1 text-xs text-zinc-600 dark:text-zinc-400">
          <input
            type="checkbox"
            checked={selected.includes(k)}
            onChange={() => toggle(k)}
          />
          <span className="capitalize">{k}</span>
        </label>
      ))}
    </div>
  );
}

function PromoteStatsPanel({
  kind,
  stats,
  dryRun,
}: {
  kind: EntityKey;
  stats: PromoteStats;
  dryRun: boolean;
}) {
  const verb = dryRun ? "would" : "did";
  return (
    <div className="space-y-1">
      <div className="text-xs font-bold capitalize">
        {kind}{" "}
        <span className="text-zinc-500 font-normal text-[10px]">
          +{stats.new.length} ~{stats.overwritten.length} ={stats.identical.length}{" "}
          ✗{stats.failed.length}
        </span>
      </div>
      {stats.new.length > 0 ? (
        <div className="text-[11px]">
          <span className="text-emerald-500">{verb} create:</span>{" "}
          {stats.new.join(", ")}
        </div>
      ) : null}
      {stats.overwritten.length > 0 ? (
        <div className="text-[11px]">
          <span className="text-amber-500">{verb} overwrite:</span>{" "}
          {stats.overwritten.join(", ")}
        </div>
      ) : null}
      {stats.identical.length > 0 ? (
        <div className="text-[11px]">
          <span className="text-zinc-500">unchanged ({stats.identical.length}):</span>{" "}
          {stats.identical.join(", ")}
        </div>
      ) : null}
      {stats.failed.length > 0 ? (
        <div className="text-[11px] space-y-0.5">
          {stats.failed.map((f) => (
            <div key={f.name} className="text-red-500">
              ✗ {f.name}: {f.error}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function PromoteDialog({
  initialFromProject,
  onClose,
}: {
  initialFromProject: string;
  onClose: () => void;
}) {
  const [fromProject, setFromProject] = useState(initialFromProject);
  const [fromModel, setFromModel] = useState("");
  const [toProject, setToProject] = useState("");
  const [toModel, setToModel] = useState("");
  const [types, setTypes] = useState<EntityKey[]>([...ENTITY_KEYS]);

  const ready =
    !!fromProject && !!toProject && fromProject !== toProject && types.length > 0;

  // Two-step: first preview (dry_run), then apply (dry_run off) -- once
  // preview returned, the user reviews stats and clicks Apply. The Apply
  // mutation reuses the form state, doesn't take "previewed payload" as
  // input, so any field edit invalidates the preview semantically.
  const previewMu = useMutation<PromoteResponse, Error>({
    mutationFn: () =>
      api.post("/semantic-layer/promote", {
        from_project: fromProject,
        to_project: toProject,
        from_model: fromModel || undefined,
        to_model: toModel || undefined,
        types: types.length === ENTITY_KEYS.length ? undefined : types,
        dry_run: true,
      }),
  });
  const applyMu = useMutation<PromoteResponse, Error>({
    mutationFn: () =>
      api.post("/semantic-layer/promote", {
        from_project: fromProject,
        to_project: toProject,
        from_model: fromModel || undefined,
        to_model: toModel || undefined,
        types: types.length === ENTITY_KEYS.length ? undefined : types,
        dry_run: false,
      }),
  });

  const lastResult = applyMu.data ?? previewMu.data;
  const lastDryRun = applyMu.data ? false : previewMu.data ? true : null;

  return (
    <Drawer open onClose={onClose} title="Promote model across projects" width="70vw">
      <div className="space-y-4">
        <div className="grid md:grid-cols-2 gap-3">
          <div className="border border-zinc-200 dark:border-zinc-900 rounded p-3 space-y-2">
            <span className="text-xs font-bold text-keboola">From</span>
            <SectionLabel>Project</SectionLabel>
            <ProjectPicker
              value={fromProject}
              onChange={(p) => {
                setFromProject(p);
                setFromModel("");
              }}
            />
            <SectionLabel>Model (optional)</SectionLabel>
            <ModelPicker project={fromProject} value={fromModel} onChange={setFromModel} />
          </div>
          <div className="border border-zinc-200 dark:border-zinc-900 rounded p-3 space-y-2">
            <span className="text-xs font-bold text-keboola">To</span>
            <SectionLabel>Project</SectionLabel>
            <ProjectPicker
              value={toProject}
              onChange={(p) => {
                setToProject(p);
                setToModel("");
              }}
            />
            <SectionLabel>Model (optional)</SectionLabel>
            <ModelPicker project={toProject} value={toModel} onChange={setToModel} />
          </div>
        </div>

        <div className="space-y-1">
          <SectionLabel>Entity types to promote</SectionLabel>
          <TypeFilterCheckboxes selected={types} onChange={setTypes} />
          <div className="text-[10px] text-zinc-500">
            All five are selected by default. Unchecking sends a `types` filter so the API
            only touches those kinds.
          </div>
        </div>

        {fromProject && toProject && fromProject === toProject ? (
          <div className="flex items-center gap-2 text-xs text-amber-500">
            <AlertTriangle className="w-3.5 h-3.5" /> From and To must be different projects.
          </div>
        ) : null}

        <div className="flex items-center gap-2 pt-2 border-t border-zinc-200 dark:border-zinc-900">
          <PrimaryButton
            onClick={() => previewMu.mutate()}
            disabled={!ready || previewMu.isPending || applyMu.isPending}
          >
            <Sparkles className="w-3 h-3" />
            {previewMu.isPending ? "Previewing…" : "Preview (dry-run)"}
          </PrimaryButton>
          <PrimaryButton
            onClick={() => {
              if (
                confirm(
                  `Apply promote from ${fromProject} → ${toProject}? CHANGED items will be overwritten on target.`,
                )
              ) {
                applyMu.mutate();
              }
            }}
            disabled={!ready || previewMu.isPending || applyMu.isPending}
          >
            <Send className="w-3 h-3" />
            {applyMu.isPending ? "Applying…" : "Apply"}
          </PrimaryButton>
          <button type="button" className="nerd-btn text-xs" onClick={onClose}>
            Close
          </button>
        </div>

        {previewMu.error ? <ErrorBox message={(previewMu.error as Error).message} /> : null}
        {applyMu.error ? <ErrorBox message={(applyMu.error as Error).message} /> : null}

        {lastResult && lastDryRun !== null ? (
          <div className="space-y-3">
            <div className="text-[11px] text-zinc-500">
              {lastDryRun ? "Preview" : "Applied"}: {lastResult.from_project}{" "}
              <ArrowRight className="inline w-3 h-3" /> {lastResult.to_project}
            </div>
            <div className="grid md:grid-cols-2 gap-x-6 gap-y-3">
              {ENTITY_KEYS.map((k) => {
                const stats = lastResult[k];
                if (!stats) return null;
                return (
                  <PromoteStatsPanel
                    key={k}
                    kind={k}
                    stats={stats}
                    dryRun={lastDryRun}
                  />
                );
              })}
            </div>
            <details className="text-[11px]">
              <summary className="cursor-pointer text-zinc-500">Raw response</summary>
              <div className="mt-2">
                <JsonView data={lastResult} maxHeight="40vh" />
              </div>
            </details>
          </div>
        ) : null}
      </div>
    </Drawer>
  );
}

// ── Import dialog ────────────────────────────────────────────────────

interface ImportItemEnvelope {
  type: string;
  status: string;
  name?: string;
  error?: string;
}

interface ImportResponse {
  target_project: string;
  target_model: string;
  source_model: string;
  dry_run: boolean;
  overwrite: boolean;
  imported: { items: ImportItemEnvelope[]; summary: Record<string, number> };
}

export function ImportDialog({
  initialProject,
  initialModel,
  onClose,
  onImported,
}: {
  initialProject: string;
  initialModel: string;
  onClose: () => void;
  onImported: () => void;
}) {
  const [project, setProject] = useState(initialProject);
  const [model, setModel] = useState(initialModel);
  const [snapshot, setSnapshot] = useState<{ name: string; content: unknown } | null>(null);
  const [types, setTypes] = useState<EntityKey[]>([...ENTITY_KEYS]);
  const [overwrite, setOverwrite] = useState(false);

  const previewMu = useMutation<ImportResponse, Error>({
    mutationFn: () =>
      api.post("/semantic-layer/import", {
        project,
        model: model || undefined,
        snapshot: snapshot?.content,
        types: types.length === ENTITY_KEYS.length ? undefined : types,
        dry_run: true,
        overwrite,
      }),
  });
  const applyMu = useMutation<ImportResponse, Error>({
    mutationFn: () =>
      api.post("/semantic-layer/import", {
        project,
        model: model || undefined,
        snapshot: snapshot?.content,
        types: types.length === ENTITY_KEYS.length ? undefined : types,
        dry_run: false,
        overwrite,
      }),
    onSuccess: () => onImported(),
  });

  const ready = !!project && !!snapshot && types.length > 0;
  const last = applyMu.data ?? previewMu.data;
  const lastDryRun = applyMu.data ? false : previewMu.data ? true : null;

  return (
    <Drawer open onClose={onClose} title="Import snapshot" width="70vw">
      <div className="space-y-4">
        <div className="grid md:grid-cols-2 gap-3">
          <div className="border border-zinc-200 dark:border-zinc-900 rounded p-3 space-y-2">
            <span className="text-xs font-bold text-keboola">Target</span>
            <SectionLabel>Project</SectionLabel>
            <ProjectPicker
              value={project}
              onChange={(p) => {
                setProject(p);
                setModel("");
              }}
            />
            <SectionLabel>Model (optional — defaults to single model)</SectionLabel>
            <ModelPicker project={project} value={model} onChange={setModel} />
          </div>
          <div className="border border-zinc-200 dark:border-zinc-900 rounded p-3 space-y-2">
            <span className="text-xs font-bold text-keboola">Snapshot</span>
            <label className="nerd-btn text-xs flex items-center gap-1 cursor-pointer w-fit">
              <FileUp className="w-3 h-3" /> {snapshot ? snapshot.name : "Pick JSON snapshot…"}
              <input
                type="file"
                accept=".json,application/json"
                className="hidden"
                onChange={async (e) => {
                  const f = e.target.files?.[0];
                  if (!f) return;
                  try {
                    const json = await readFileAsJson(f);
                    setSnapshot({ name: f.name, content: json });
                  } catch (err) {
                    alert(err instanceof Error ? err.message : String(err));
                  }
                  e.target.value = "";
                }}
              />
            </label>
            <div className="text-[10px] text-zinc-500">
              The snapshot is sent inline in the request body (no upload endpoint). Produced by
              the Export button or `kbagent sl export`.
            </div>
            <label className="flex items-center gap-2 text-xs text-zinc-600 dark:text-zinc-400">
              <input
                type="checkbox"
                checked={overwrite}
                onChange={(e) => setOverwrite(e.target.checked)}
              />
              Overwrite existing items (otherwise: skip on conflict)
            </label>
          </div>
        </div>

        <div className="space-y-1">
          <SectionLabel>Entity types to import</SectionLabel>
          <TypeFilterCheckboxes selected={types} onChange={setTypes} />
        </div>

        <div className="flex items-center gap-2 pt-2 border-t border-zinc-200 dark:border-zinc-900">
          <PrimaryButton
            onClick={() => previewMu.mutate()}
            disabled={!ready || previewMu.isPending || applyMu.isPending}
          >
            <Sparkles className="w-3 h-3" />
            {previewMu.isPending ? "Previewing…" : "Preview (dry-run)"}
          </PrimaryButton>
          <PrimaryButton
            onClick={() => {
              if (
                confirm(
                  `Import snapshot into ${project}? ${
                    overwrite ? "EXISTING items will be overwritten." : "Conflicts will be skipped."
                  }`,
                )
              ) {
                applyMu.mutate();
              }
            }}
            disabled={!ready || previewMu.isPending || applyMu.isPending}
          >
            <Upload className="w-3 h-3" />
            {applyMu.isPending ? "Importing…" : "Import"}
          </PrimaryButton>
          <button type="button" className="nerd-btn text-xs" onClick={onClose}>
            Close
          </button>
        </div>

        {previewMu.error ? <ErrorBox message={(previewMu.error as Error).message} /> : null}
        {applyMu.error ? <ErrorBox message={(applyMu.error as Error).message} /> : null}

        {last && lastDryRun !== null ? (
          <div className="space-y-2">
            <div className="text-[11px] text-zinc-500">
              {lastDryRun ? "Preview" : "Imported"}: {last.target_project} ← snapshot{" "}
              <span className="font-mono">{last.source_model || "(unknown)"}</span>
            </div>
            <div className="grid md:grid-cols-3 gap-2">
              {Object.entries(last.imported.summary).map(([k, v]) => (
                <div
                  key={k}
                  className="text-[11px] flex items-center justify-between border border-zinc-200 dark:border-zinc-900 rounded px-2 py-1"
                >
                  <span className="text-zinc-500">{k}</span>
                  <span className="font-mono">{v}</span>
                </div>
              ))}
            </div>
            <details className="text-[11px]">
              <summary className="cursor-pointer text-zinc-500">
                Item-by-item ({last.imported.items.length})
              </summary>
              <div className="mt-2 max-h-72 overflow-auto space-y-0.5 font-mono text-[11px]">
                {last.imported.items.map((it, i) => (
                  <div
                    key={`${it.type}-${it.name ?? i}`}
                    className={
                      it.status === "imported" || it.status === "would-import"
                        ? "text-emerald-500"
                        : it.status === "overwritten" || it.status === "would-overwrite"
                          ? "text-amber-500"
                          : it.status === "skipped"
                            ? "text-zinc-500"
                            : "text-red-500"
                    }
                  >
                    {it.status} {it.type}: {it.name ?? "?"}
                    {it.error ? ` — ${it.error}` : ""}
                  </div>
                ))}
              </div>
            </details>
          </div>
        ) : null}
      </div>
    </Drawer>
  );
}

// ── Build dialog ─────────────────────────────────────────────────────

interface BuildValidation {
  errors: { code?: string; message?: string }[];
  warnings: { code?: string; message?: string }[];
}

interface BuildResponse {
  project: string;
  dry_run: boolean;
  keep_on_failure: boolean;
  fallback_used: string;
  fetch_errors: { table: string; error: string }[];
  generated: { datasets: unknown[]; metrics: unknown[]; relationships: unknown[]; constraints: unknown[]; glossary: unknown[] };
  validation: BuildValidation;
  validated: boolean;
  output_path?: string;
  pushed?: { counts: Record<string, number>; model_uuid: string };
}

function TablePicker({
  project,
  selected,
  onChange,
}: {
  project: string;
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  const [bucketFilter, setBucketFilter] = useState("");
  const bucketsQ = useQuery<BucketsResponse>({
    queryKey: ["sl-build-buckets", project],
    queryFn: () => api.get("/storage/buckets", { query: { project } }),
    enabled: !!project,
  });
  const tablesQ = useQuery<TablesResponse>({
    queryKey: ["sl-build-tables", project, bucketFilter],
    queryFn: () =>
      api.get("/storage/tables", {
        query: { project, ...(bucketFilter ? { bucket_id: bucketFilter } : {}) },
      }),
    enabled: !!project,
  });

  const allTables = useMemo(() => {
    return (tablesQ.data?.tables ?? []).map((t) => t.id);
  }, [tablesQ.data]);

  const toggleAll = () => {
    if (selected.length === allTables.length) {
      onChange([]);
    } else {
      onChange(allTables);
    }
  };

  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-2 flex-wrap">
        <SectionLabel>Filter by bucket</SectionLabel>
        <select
          className="nerd-input text-xs py-0.5"
          value={bucketFilter}
          onChange={(e) => setBucketFilter(e.target.value)}
        >
          <option value="">(all buckets)</option>
          {(bucketsQ.data?.buckets ?? []).map((b) => (
            <option key={b.id} value={b.id}>
              {b.id}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="nerd-btn text-[10px]"
          onClick={toggleAll}
          disabled={allTables.length === 0}
        >
          {selected.length === allTables.length && allTables.length > 0
            ? "Unselect all"
            : "Select all"}
        </button>
        <span className="text-[10px] text-zinc-500">
          {selected.length} / {allTables.length} selected
        </span>
      </div>

      {tablesQ.isLoading ? <Loading label="loading tables" /> : null}
      {tablesQ.error ? <ErrorBox message={(tablesQ.error as Error).message} /> : null}

      {tablesQ.data ? (
        <div className="border border-zinc-200 dark:border-zinc-900 rounded p-2 max-h-72 overflow-auto space-y-0.5 font-mono text-[11px]">
          {allTables.length === 0 ? (
            <div className="text-zinc-500 italic">No tables in this scope.</div>
          ) : (
            allTables.map((tid) => (
              <label key={tid} className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={selected.includes(tid)}
                  onChange={(e) => {
                    if (e.target.checked) {
                      onChange([...selected, tid]);
                    } else {
                      onChange(selected.filter((x) => x !== tid));
                    }
                  }}
                />
                <span>{tid}</span>
              </label>
            ))
          )}
        </div>
      ) : null}
    </div>
  );
}

export function BuildDialog({
  initialProject,
  initialModel,
  onClose,
  onBuilt,
}: {
  initialProject: string;
  initialModel: string;
  onClose: () => void;
  onBuilt: () => void;
}) {
  const [project, setProject] = useState(initialProject);
  const [model, setModel] = useState(initialModel);
  const [tables, setTables] = useState<string[]>([]);
  const [name, setName] = useState("");
  const [keepOnFailure, setKeepOnFailure] = useState(false);

  const ready = !!project && tables.length > 0;

  const previewMu = useMutation<BuildResponse, Error>({
    mutationFn: () =>
      api.post("/semantic-layer/build", {
        project,
        model: model || undefined,
        tables,
        name: name || undefined,
        dry_run: true,
        keep_on_failure: keepOnFailure,
      }),
  });
  const applyMu = useMutation<BuildResponse, Error>({
    mutationFn: () =>
      api.post("/semantic-layer/build", {
        project,
        model: model || undefined,
        tables,
        name: name || undefined,
        dry_run: false,
        keep_on_failure: keepOnFailure,
      }),
    onSuccess: () => onBuilt(),
  });

  const last = applyMu.data ?? previewMu.data;
  const lastDryRun = applyMu.data ? false : previewMu.data ? true : null;

  return (
    <Drawer open onClose={onClose} title="Build model from tables" width="75vw">
      <div className="space-y-4">
        <div className="text-[11px] text-zinc-500">
          Heuristic greenfield builder — fetches the storage schema for each table, classifies
          columns into role-buckets, and synthesises one dataset per table with a COUNT(*) metric.
          Validation runs locally before any push. AI-assisted variant is CLI-only for now.
        </div>

        <div className="grid md:grid-cols-2 gap-3">
          <div className="space-y-2">
            <SectionLabel>Project</SectionLabel>
            <ProjectPicker
              value={project}
              onChange={(p) => {
                setProject(p);
                setModel("");
                setTables([]);
              }}
            />
            <SectionLabel>Target model (optional)</SectionLabel>
            <ModelPicker project={project} value={model} onChange={setModel} />
            <div className="text-[10px] text-zinc-500">
              Empty means "create a fresh model with the name below."
            </div>
          </div>
          <div className="space-y-2">
            <SectionLabel>Model name (if creating a fresh one)</SectionLabel>
            <input
              className="nerd-input w-full text-sm"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. analytics_core"
            />
            <label className="flex items-center gap-2 text-xs text-zinc-600 dark:text-zinc-400">
              <input
                type="checkbox"
                checked={keepOnFailure}
                onChange={(e) => setKeepOnFailure(e.target.checked)}
              />
              Keep on failure (don't rollback if push fails — for forensic inspection)
            </label>
          </div>
        </div>

        <div className="space-y-1">
          <SectionLabel>Source tables</SectionLabel>
          {project ? (
            <TablePicker project={project} selected={tables} onChange={setTables} />
          ) : (
            <div className="text-[11px] text-zinc-500 italic">Pick a project first.</div>
          )}
        </div>

        <div className="flex items-center gap-2 pt-2 border-t border-zinc-200 dark:border-zinc-900">
          <PrimaryButton
            onClick={() => previewMu.mutate()}
            disabled={!ready || previewMu.isPending || applyMu.isPending}
          >
            <Sparkles className="w-3 h-3" />
            {previewMu.isPending ? "Previewing…" : "Preview (dry-run)"}
          </PrimaryButton>
          <PrimaryButton
            onClick={() => {
              if (
                confirm(
                  `Build & push model from ${tables.length} table(s) into ${project}?`,
                )
              ) {
                applyMu.mutate();
              }
            }}
            disabled={!ready || previewMu.isPending || applyMu.isPending}
          >
            <PackagePlus className="w-3 h-3" />
            {applyMu.isPending ? "Building…" : "Build"}
          </PrimaryButton>
          <button type="button" className="nerd-btn text-xs" onClick={onClose}>
            Close
          </button>
        </div>

        {previewMu.error ? <ErrorBox message={(previewMu.error as Error).message} /> : null}
        {applyMu.error ? <ErrorBox message={(applyMu.error as Error).message} /> : null}

        {last && lastDryRun !== null ? (
          <div className="space-y-3">
            <div className="text-[11px] text-zinc-500">
              {lastDryRun ? "Preview" : "Pushed"}: fallback={last.fallback_used}, validated=
              {last.validated ? "yes" : "no"}
            </div>
            {last.validation.errors.length > 0 ? (
              <div className="text-[11px] space-y-0.5">
                <div className="text-red-500 font-bold">
                  Errors ({last.validation.errors.length})
                </div>
                {last.validation.errors.map((e, i) => (
                  <div key={i} className="text-red-500">
                    [{e.code ?? "ERR"}] {e.message}
                  </div>
                ))}
              </div>
            ) : null}
            {last.validation.warnings.length > 0 ? (
              <div className="text-[11px] space-y-0.5">
                <div className="text-amber-500 font-bold">
                  Warnings ({last.validation.warnings.length})
                </div>
                {last.validation.warnings.map((w, i) => (
                  <div key={i} className="text-amber-500">
                    [{w.code ?? "WARN"}] {w.message}
                  </div>
                ))}
              </div>
            ) : null}
            <div className="grid md:grid-cols-5 gap-2">
              {ENTITY_KEYS.map((k) => (
                <div
                  key={k}
                  className="text-[11px] flex items-center justify-between border border-zinc-200 dark:border-zinc-900 rounded px-2 py-1"
                >
                  <span className="text-zinc-500 capitalize">{k}</span>
                  <span className="font-mono">{last.generated[k]?.length ?? 0}</span>
                </div>
              ))}
            </div>
            {last.pushed ? (
              <div className="text-[11px] text-emerald-500">
                Pushed model {last.pushed.model_uuid}: {JSON.stringify(last.pushed.counts)}
              </div>
            ) : null}
            {last.fetch_errors.length > 0 ? (
              <details className="text-[11px]">
                <summary className="cursor-pointer text-amber-500">
                  Fetch errors ({last.fetch_errors.length})
                </summary>
                <div className="mt-1 space-y-0.5">
                  {last.fetch_errors.map((fe) => (
                    <div key={fe.table} className="text-amber-500">
                      {fe.table}: {fe.error}
                    </div>
                  ))}
                </div>
              </details>
            ) : null}
            <details className="text-[11px]">
              <summary className="cursor-pointer text-zinc-500">Raw response</summary>
              <div className="mt-2">
                <JsonView data={last} maxHeight="40vh" />
              </div>
            </details>
          </div>
        ) : null}
      </div>
    </Drawer>
  );
}

// ── Token encrypt dialog ─────────────────────────────────────────────

interface TokenEncryptResponse {
  project: string;
  component_id: string;
  // The metastore service wraps the ciphertext in a property-bag dict the
  // transformation user_properties block consumes verbatim. Most callers
  // want only the inner string, but the dict is the canonical wire shape.
  encrypted: Record<string, string>;
}

export function TokenEncryptDialog({
  initialProject,
  onClose,
}: {
  initialProject: string;
  onClose: () => void;
}) {
  const [project, setProject] = useState(initialProject);
  // Component the encrypted token will be embedded in. KBC::Project::*
  // ciphertext is scoped per component, so the value the user copy/pastes
  // into the transformation config MUST match the component the transform
  // uses (typically keboola.snowflake-transformation for snowflake).
  const [componentId, setComponentId] = useState("keboola.snowflake-transformation");
  const [copied, setCopied] = useState(false);

  const mu = useMutation<TokenEncryptResponse, Error>({
    mutationFn: () =>
      api.post("/semantic-layer/token/encrypt", {
        project,
        component_id: componentId,
      }),
  });

  const copyToClipboard = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch (err) {
      alert(`Clipboard write failed: ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  return (
    <Drawer open onClose={onClose} title="Encrypt project storage token" width="55vw">
      <div className="space-y-4">
        <div className="text-[11px] text-zinc-500">
          Encrypts the project's storage token as a <span className="font-mono">KBC::Project::*</span>{" "}
          ciphertext bound to a specific component. Paste the resulting value into the
          transformation config's <span className="font-mono">parameters.user_properties.token</span>{" "}
          so the metastore-aware transformation can read it without seeing the plaintext token.
        </div>

        <div className="grid md:grid-cols-2 gap-3">
          <div className="space-y-1.5">
            <SectionLabel>Project</SectionLabel>
            <ProjectPicker value={project} onChange={setProject} />
          </div>
          <div className="space-y-1.5">
            <SectionLabel>Component ID</SectionLabel>
            <input
              className="nerd-input w-full text-sm"
              value={componentId}
              onChange={(e) => setComponentId(e.target.value)}
              placeholder="e.g. keboola.snowflake-transformation"
            />
            <div className="text-[10px] text-zinc-500">
              Must match the component the transformation actually uses (per-component scope).
            </div>
          </div>
        </div>

        <div className="flex items-center gap-2 pt-2 border-t border-zinc-200 dark:border-zinc-900">
          <PrimaryButton
            onClick={() => mu.mutate()}
            disabled={!project || !componentId || mu.isPending}
          >
            <KeyRound className="w-3 h-3" />
            {mu.isPending ? "Encrypting…" : "Encrypt token"}
          </PrimaryButton>
          <button type="button" className="nerd-btn text-xs" onClick={onClose}>
            Close
          </button>
        </div>

        {mu.error ? <ErrorBox message={(mu.error as Error).message} /> : null}

        {mu.data ? (
          <div className="space-y-3">
            <SectionLabel>Encrypted user_properties</SectionLabel>
            <div className="text-[11px] text-zinc-500">
              Paste this block into the transformation's{" "}
              <span className="font-mono">parameters.user_properties</span>. The key
              (e.g. <span className="font-mono">#metastore_token</span>) is what the
              transformation reads at runtime.
            </div>
            <textarea
              readOnly
              rows={6}
              className="nerd-input w-full font-mono text-[11px] break-all"
              value={JSON.stringify(mu.data.encrypted, null, 2)}
              onFocus={(e) => e.target.select()}
            />
            <div className="flex items-center gap-2 flex-wrap">
              <PrimaryButton
                onClick={() =>
                  copyToClipboard(JSON.stringify(mu.data.encrypted, null, 2))
                }
              >
                <Copy className="w-3 h-3" />
                {copied ? "Copied!" : "Copy block"}
              </PrimaryButton>
              {Object.entries(mu.data.encrypted).map(([k, v]) => (
                <button
                  key={k}
                  type="button"
                  onClick={() => copyToClipboard(v)}
                  className="nerd-btn text-xs"
                  title={`Copy just the ciphertext for ${k}`}
                >
                  Copy {k}
                </button>
              ))}
              <span className="text-[10px] text-zinc-500">
                Component scope:{" "}
                <span className="font-mono">{mu.data.component_id}</span>
              </span>
            </div>
          </div>
        ) : null}
      </div>
    </Drawer>
  );
}
