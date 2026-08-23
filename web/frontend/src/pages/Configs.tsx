import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileCode, Layers, PlayCircle, RotateCcw, SlidersHorizontal, Trash2 } from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { ConfirmModal } from "../components/ConfirmModal";
import { Drawer } from "../components/Drawer";
import { Empty, ErrorBox, Loading, PageTitle } from "../components/Empty";
import { JsonView } from "../components/JsonView";
import { type KeyValueItem, KeyValueGrid } from "../components/KeyValueGrid";
import { PillList } from "../components/PillList";
import { RawDetail } from "../components/RawDetail";
import { DataTable } from "../components/Table";
import { useUIState } from "../state";
import { useHashSelection } from "../useHashSelection";
import type { ConfigSummary, ProjectError } from "../types";

interface ConfigsResp {
  configs: ConfigSummary[];
  errors: ProjectError[];
}

/**
 * `GET /configs/{project}/{component}/{config}` — the Storage API's own
 * configuration payload, flattened with `project_alias` + `branch_id` by
 * `ConfigService.get_config_detail`.
 *
 * Every field is optional on purpose: the body below `configuration` is
 * component-defined and older stacks omit blocks entirely, so the overview has
 * to degrade to an em dash rather than render "undefined".
 */
interface ConfigDetailPayload {
  id?: string;
  name?: string;
  description?: string;
  created?: string;
  creatorToken?: { id?: number | string; description?: string };
  version?: number;
  changeDescription?: string;
  isDisabled?: boolean;
  isDeleted?: boolean;
  configuration?: unknown;
  rows?: Array<Record<string, unknown>>;
  state?: unknown;
  currentVersion?: {
    created?: string;
    creatorToken?: { id?: number | string; description?: string };
    changeDescription?: string;
    versionIdentifier?: string;
  };
  project_alias?: string;
  branch_id?: number | null;
}

/** One row of `GET /configs/trash/{project}` (mirrors `shape_trash_entry`). */
interface TrashEntry {
  component_id: string;
  config_id: string;
  name: string;
  version: number | null;
  deleted_change_description: string | null;
  deleted_at: string | null;
}

interface TrashResp {
  project_alias: string;
  branch_id: number | null;
  component_id: string | null;
  trash: TrashEntry[];
}

type ConfigsTab = "configs" | "trash";

export function ConfigsPage() {
  const { project, branchId } = useUIState();
  // Deep link: `?sel=<componentId>/<configId>` opens that config's drawer.
  const [sel, setSel] = useHashSelection();
  const [tab, setTab] = useState<ConfigsTab>("configs");
  const [filterText, setFilterText] = useState("");
  const [selected, setSelected] = useState<ConfigSummary | null>(null);

  const q = useQuery<ConfigsResp>({
    queryKey: ["configs", project, branchId],
    queryFn: () =>
      api.get("/configs", {
        query: { project: project ?? undefined, branch_id: branchId ?? undefined },
      }),
    enabled: !!project && tab === "configs",
  });

  // Restore a deep-linked selection ONCE, after the first list load. Guarded
  // by a ref rather than by `selected`, so closing the drawer does not
  // immediately re-open it on the next render.
  const restoredRef = useRef(false);
  useEffect(() => {
    if (restoredRef.current) return;
    if (!sel || !project) {
      restoredRef.current = true;
      return;
    }
    if (q.isLoading) return;
    restoredRef.current = true;
    const hit = q.data?.configs.find((c) => selKey(c.component_id, c.config_id) === sel);
    // Unlike a job id, a config drawer cannot be opened from the id alone:
    // the row carries the project alias the detail request is addressed to.
    // A link to a config this project/branch does not have therefore drops
    // the selection instead of opening a drawer that could only 404.
    if (hit) setSelected(hit);
    else setSel(null);
  }, [sel, project, q.isLoading, q.data, setSel]);

  const openConfig = (c: ConfigSummary) => {
    setSelected(c);
    setSel(selKey(c.component_id, c.config_id));
  };
  const closeConfig = () => {
    setSelected(null);
    setSel(null);
  };
  const switchTab = (t: ConfigsTab) => {
    setTab(t);
    // The trash rows are a different id space (and open no drawer), so a
    // configs selection must not survive into that tab's URL.
    if (t === "trash") {
      setSelected(null);
      setSel(null);
    }
  };

  const filtered =
    q.data?.configs.filter((c) =>
      filterText
        ? `${c.config_name} ${c.config_id} ${c.component_id}`
            .toLowerCase()
            .includes(filterText.toLowerCase())
        : true,
    ) ?? [];

  return (
    <div className="space-y-4">
      <PageTitle
        title="Configurations"
        description={`Component configs in ${project ?? "(no project)"}${branchId ? ` (branch #${branchId})` : ""}`}
      />
      <div className="flex gap-2">
        {(["configs", "trash"] as const).map((t) => (
          <button
            key={t}
            type="button"
            className={`nerd-btn ${tab === t ? "border-keboola text-keboola" : ""}`}
            onClick={() => switchTab(t)}
          >
            {t === "trash" ? (
              <>
                <Trash2 className="w-3 h-3 inline mr-1" />
                trash
              </>
            ) : (
              t
            )}
          </button>
        ))}
      </div>

      {!project ? (
        <Empty title="Select a project from the top bar" />
      ) : tab === "trash" ? (
        <TrashTab />
      ) : q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox message={(q.error as Error).message} />
      ) : (
        <>
          <input
            className="nerd-input w-full max-w-md"
            placeholder="filter by name / id / component..."
            value={filterText}
            onChange={(e) => setFilterText(e.target.value)}
          />
          {q.data?.errors.length ? (
            <div className="text-amber-700 dark:text-neon-amber text-xs">
              {q.data.errors.length} project error(s) -- some configs may be missing.
            </div>
          ) : null}
          <DataTable
            rows={filtered}
            rowKey={(c) => `${c.project_alias}/${c.component_id}/${c.config_id}`}
            onRowClick={openConfig}
            columns={[
              { header: "Component", cell: (c) => <span className="text-accent">{c.component_id}</span> },
              { header: "Config ID", cell: (c) => <span className="text-zinc-500">{c.config_id}</span> },
              { header: "Name", cell: (c) => <span className="font-medium">{c.config_name}</span> },
              { header: "Folder", cell: (c) => <span className="text-zinc-500 text-xs">{c.folder ?? ""}</span> },
              { header: "Modified", cell: (c) => <span className="text-zinc-500 text-xs">{c.last_modified ?? ""}</span> },
            ]}
          />
        </>
      )}

      {selected ? (
        <ConfigDetail
          alias={selected.project_alias}
          componentId={selected.component_id}
          configId={selected.config_id}
          name={selected.config_name}
          onClose={closeConfig}
        />
      ) : null}
    </div>
  );
}

/**
 * The `?sel=` value for one configuration. A component id contains dots but
 * never a slash, and neither does a config id, so a single `/` is an
 * unambiguous separator. Restore compares whole keys built by this function
 * rather than splitting the URL value, so an unexpected extra `/` can only
 * fail to match — never silently address a different configuration.
 */
function selKey(componentId: string, configId: string): string {
  return `${componentId}/${configId}`;
}

/**
 * Trash view (#643). A `config delete` is a SOFT delete: the Storage API moves
 * the configuration here and it stays restorable. (The same DELETE issued at
 * something already in the trash purges it permanently, which is exactly why
 * the server refuses to re-delete and why this view only ever restores.)
 */
function TrashTab() {
  const { project, branchId } = useUIState();
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [restoring, setRestoring] = useState<string | null>(null);

  const q = useQuery<TrashResp>({
    queryKey: ["config-trash", project, branchId],
    queryFn: () =>
      api.get(`/configs/trash/${encodeURIComponent(project!)}`, {
        query: { branch_id: branchId ?? undefined },
      }),
    enabled: !!project,
  });

  const restore = useMutation({
    mutationFn: (entry: TrashEntry) =>
      api.post(
        `/configs/${encodeURIComponent(project!)}/${encodeURIComponent(entry.component_id)}/${encodeURIComponent(entry.config_id)}/restore`,
        undefined,
        { query: { branch_id: branchId ?? undefined } },
      ),
    onMutate: (entry) => {
      setError(null);
      setRestoring(`${entry.component_id}/${entry.config_id}`);
    },
    onError: (e) => setError((e as Error).message),
    onSettled: () => {
      setRestoring(null);
      qc.invalidateQueries({ queryKey: ["config-trash"] });
      qc.invalidateQueries({ queryKey: ["configs"] });
    },
  });

  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox message={(q.error as Error).message} />;

  const rows = q.data?.trash ?? [];
  if (rows.length === 0) {
    return <Empty title="Trash is empty — deletes are reversible here." />;
  }

  return (
    <div className="space-y-3">
      {error ? <ErrorBox message={error} /> : null}
      <DataTable
        rows={rows}
        rowKey={(t) => `${t.component_id}/${t.config_id}`}
        columns={[
          { header: "Component", cell: (t) => <span className="text-accent">{t.component_id}</span> },
          { header: "Config ID", cell: (t) => <span className="text-zinc-500">{t.config_id}</span> },
          { header: "Name", cell: (t) => <span className="font-medium">{t.name}</span> },
          {
            header: "Deleted at",
            cell: (t) => <span className="text-zinc-500 text-xs">{t.deleted_at ?? "—"}</span>,
          },
          {
            header: "Version",
            align: "right",
            cell: (t) => (
              <span className="text-zinc-500 text-xs">{t.version != null ? `v${t.version}` : "—"}</span>
            ),
          },
          {
            header: "Actions",
            align: "right",
            cell: (t) => {
              const key = `${t.component_id}/${t.config_id}`;
              return (
                <button
                  type="button"
                  className="nerd-btn text-[10px] py-0.5 px-1.5 flex items-center gap-1 hover:text-keboola disabled:opacity-50 ml-auto"
                  disabled={restoring === key}
                  onClick={() => restore.mutate(t)}
                  title={t.deleted_change_description ?? "Restore this configuration"}
                >
                  <RotateCcw className={`w-3 h-3 ${restoring === key ? "animate-spin" : ""}`} />
                  {restoring === key ? "restoring…" : "restore"}
                </button>
              );
            },
          },
        ]}
      />
    </div>
  );
}

function ConfigDetail({
  alias,
  componentId,
  configId,
  name,
  onClose,
}: {
  alias: string;
  componentId: string;
  configId: string;
  name: string;
  onClose: () => void;
}) {
  const { branchId, setPage } = useUIState();
  const qc = useQueryClient();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [startedJobId, setStartedJobId] = useState<string | null>(null);

  const detailQ = useQuery<ConfigDetailPayload>({
    queryKey: ["config-detail", alias, componentId, configId, branchId],
    queryFn: () =>
      api.get(
        `/configs/${encodeURIComponent(alias)}/${encodeURIComponent(componentId)}/${encodeURIComponent(configId)}`,
        { query: { branch_id: branchId ?? undefined } },
      ),
  });

  // Fire-and-return: `wait` stays false so the drawer never blocks on a job
  // that can run for an hour. The user follows it on the Jobs page instead.
  const runJob = useMutation<{ id?: string | number }>({
    mutationFn: () =>
      api.post(`/jobs/${encodeURIComponent(alias)}/run`, {
        component_id: componentId,
        config_id: configId,
        branch_id: branchId ?? undefined,
      }),
    onError: (e) => setActionError((e as Error).message),
    onSuccess: (data) => {
      setActionError(null);
      setStartedJobId(data?.id != null ? String(data.id) : "");
      qc.invalidateQueries({ queryKey: ["jobs"] });
      qc.invalidateQueries({ queryKey: ["dashboard-jobs"] });
    },
  });

  const del = useMutation({
    mutationFn: () =>
      api.delete(
        `/configs/${encodeURIComponent(alias)}/${encodeURIComponent(componentId)}/${encodeURIComponent(configId)}`,
        { query: { branch_id: branchId ?? undefined, dry_run: false } },
      ),
    onError: (e) => {
      setActionError((e as Error).message);
      setConfirmDelete(false);
    },
    onSuccess: () => {
      setConfirmDelete(false);
      qc.invalidateQueries({ queryKey: ["configs"] });
      qc.invalidateQueries({ queryKey: ["config-trash"] });
      onClose();
    },
  });

  return (
    <Drawer
      open={true}
      onClose={onClose}
      title={name || configId}
      subtitle={`${componentId} ・ ${configId}${branchId ? ` ・ branch #${branchId}` : ""}`}
      width="max-w-4xl"
      actions={
        <>
          <button
            type="button"
            className="nerd-btn text-xs flex items-center gap-1 hover:text-keboola disabled:opacity-50"
            disabled={runJob.isPending}
            onClick={() => runJob.mutate()}
            title={`Queue a job for ${componentId} / ${configId}`}
          >
            <PlayCircle className="w-3 h-3" />
            {runJob.isPending ? "starting…" : "Run job"}
          </button>
          <button
            type="button"
            className="nerd-btn text-xs flex items-center gap-1 hover:text-red-600 dark:hover:text-red-400 disabled:opacity-50"
            disabled={del.isPending}
            onClick={() => setConfirmDelete(true)}
          >
            <Trash2 className="w-3 h-3" /> Delete
          </button>
        </>
      }
    >
      <div className="space-y-3">
        {actionError ? <ErrorBox message={actionError} /> : null}
        {startedJobId !== null ? (
          <div className="nerd-card border-keboola/40 flex items-center gap-3 text-xs">
            <PlayCircle className="w-4 h-4 text-keboola shrink-0" />
            <span className="text-zinc-700 dark:text-zinc-300">
              Job {startedJobId ? <span className="font-mono text-accent">{startedJobId}</span> : null}{" "}
              queued. It runs asynchronously — follow it on the Jobs page.
            </span>
            <button
              type="button"
              className="nerd-btn text-xs hover:text-keboola ml-auto shrink-0"
              onClick={() => {
                onClose();
                setPage("jobs");
              }}
            >
              open Jobs →
            </button>
          </div>
        ) : null}
        {detailQ.isLoading ? <Loading /> : null}
        {detailQ.error ? <ErrorBox message={(detailQ.error as Error).message} /> : null}
        {detailQ.data ? (
          <RawDetail
            data={detailQ.data}
            overview={<ConfigOverview detail={detailQ.data} componentId={componentId} />}
          />
        ) : null}
      </div>

      {confirmDelete ? (
        <ConfirmModal
          danger
          busy={del.isPending}
          title="Delete configuration?"
          body={
            <>
              <span className="font-mono text-accent">
                {componentId}/{configId}
              </span>{" "}
              moves to the trash. This is reversible — restore it from the Trash tab. Any schedule
              or flow still pointing at it will start failing until it is restored.
            </>
          }
          confirmLabel="Move to trash"
          onConfirm={() => del.mutate()}
          onCancel={() => setConfirmDelete(false)}
        />
      ) : null}
    </Drawer>
  );
}

/**
 * Rendered body of a configuration detail.
 *
 * The metadata (who changed it, when, which version, is it disabled) is what a
 * reader is usually after, and it used to be buried at the top of a raw JSON
 * dump next to a component-defined `configuration` blob of arbitrary size.
 * That blob stays visible verbatim -- it is inherently freeform, so summarizing
 * it beyond naming its top-level blocks would be guessing -- but it no longer
 * hides the fields around it. The untouched payload is one tab away.
 */
function ConfigOverview({
  detail,
  componentId,
}: {
  detail: ConfigDetailPayload;
  componentId: string;
}) {
  const configuration = isRecord(detail.configuration) ? detail.configuration : {};
  const configurationKeys = Object.keys(configuration);
  const rows = detail.rows ?? [];
  const state = isRecord(detail.state) ? detail.state : {};
  const hasState = Object.keys(state).length > 0;

  // `currentVersion` describes the version actually served; the top-level
  // `changeDescription` is the same text on a fresh config but goes stale on
  // older stacks, so it is only the fallback.
  const lastChange = detail.currentVersion?.changeDescription ?? detail.changeDescription ?? "";
  const lastChangeAt = detail.currentVersion?.created ?? "";

  const items: KeyValueItem[] = [
    { label: "Name", value: detail.name },
    { label: "Config ID", value: detail.id, mono: true },
    { label: "Component ID", value: componentId, mono: true },
    { label: "Version", value: detail.version != null ? `v${detail.version}` : "", mono: true },
    { label: "Created", value: detail.created },
    { label: "Created by", value: detail.creatorToken?.description },
    {
      label: "Last change",
      value: lastChange ? (
        <>
          {lastChange}
          {lastChangeAt ? <span className="text-zinc-500"> ・ {lastChangeAt}</span> : null}
        </>
      ) : (
        ""
      ),
    },
    {
      label: "Branch",
      value: detail.branch_id != null ? `#${detail.branch_id}` : "production",
      mono: true,
    },
  ];
  // Only when it carries something: an empty description is the norm, and a
  // permanent em-dash cell would read as a missing field rather than a blank.
  if (detail.description) items.push({ label: "Description", value: detail.description });

  return (
    <div className="space-y-4">
      <Section icon={<FileCode className="w-3.5 h-3.5" />} label="Configuration">
        <KeyValueGrid columns={3} items={items} />
        {detail.isDisabled || detail.isDeleted ? (
          <div className="flex flex-wrap items-center gap-1.5 mt-3">
            {detail.isDisabled ? <PillList items={["disabled"]} tone="amber" /> : null}
            {detail.isDeleted ? <PillList items={["deleted"]} tone="red" /> : null}
          </div>
        ) : null}
      </Section>

      <Section icon={<SlidersHorizontal className="w-3.5 h-3.5" />} label="Parameters">
        <PillList
          items={configurationKeys}
          empty="Empty configuration body — this config carries no parameters."
        />
        {configurationKeys.length > 0 ? (
          <div className="mt-2">
            <JsonView data={configuration} maxHeight="40vh" />
          </div>
        ) : null}
      </Section>

      {rows.length > 0 ? (
        <Section icon={<Layers className="w-3.5 h-3.5" />} label={`Rows (${rows.length})`}>
          <DataTable
            rows={rows}
            rowKey={(r) => String(r.id ?? "")}
            columns={[
              { header: "Row ID", cell: (r) => <span className="text-accent">{String(r.id ?? "")}</span> },
              {
                header: "Name",
                cell: (r) => <span className="font-medium">{String(r.name ?? "")}</span>,
              },
              {
                header: "Status",
                align: "right",
                cell: (r) =>
                  r.isDisabled ? (
                    <span className="nerd-pill-amber">disabled</span>
                  ) : (
                    <span className="nerd-pill-green">enabled</span>
                  ),
              },
            ]}
          />
        </Section>
      ) : null}

      {hasState ? (
        <details>
          <summary className="text-xs text-zinc-500 cursor-pointer">state</summary>
          <JsonView data={state} maxHeight="40vh" />
        </details>
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

/** A JSON object (not an array, not null) — the shape both blobs must be. */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
