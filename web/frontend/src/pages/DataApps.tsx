import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Copy, Pause, Play, RefreshCw, Rocket, Trash2 } from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { ConfirmModal } from "../components/ConfirmModal";
import { DetailTabs } from "../components/DetailTabs";
import { Drawer } from "../components/Drawer";
import { Empty, ErrorBox, Loading, PageTitle } from "../components/Empty";
import { JsonView } from "../components/JsonView";
import { KeyValueGrid } from "../components/KeyValueGrid";
import { DataTable } from "../components/Table";
import { useUIState } from "../state";
import type { DataApp, ProjectError } from "../types";
import { useHashSelection } from "../useHashSelection";

interface DataAppsResp {
  apps: DataApp[];
  errors: ProjectError[];
}

/**
 * `GET /data-apps/{project}/{app_id}` — mirrors `DataAppService.get_data_app`,
 * which merges the Data Science deployment record (state / url / deployed
 * version) with the Storage configuration (name / description / slug / git).
 * Every field is optional: an app whose Storage config was deleted still has a
 * deployment record, and the merge simply leaves those keys empty.
 */
interface DataAppDetailPayload {
  project_alias?: string;
  app_id?: string;
  config_id?: string;
  config_version_storage?: string;
  config_version_deployed?: string;
  name?: string;
  description?: string;
  type?: string;
  state?: string;
  desired_state?: string;
  url?: string;
  size?: string;
  auto_suspend_after_seconds?: number | null;
  last_start_timestamp?: string | null;
  slug?: string;
  git?: Record<string, unknown>;
  raw?: Record<string, unknown>;
}

/** `GET /data-apps/{project}/{app_id}/logs` — mirrors `get_app_logs`. */
interface DataAppLogsPayload {
  project_alias?: string;
  app_id?: string;
  lines_requested?: number | null;
  since_requested?: string | null;
  lines_returned?: number;
  text?: string;
}

/** Tail depth requested by the Logs tab. The route caps nothing by default. */
const LOG_TAIL_LINES = 200;

const STATE_STYLE: Record<string, string> = {
  running: "nerd-pill-green",
  starting: "nerd-pill-amber",
  stopping: "nerd-pill-amber",
  stopped: "nerd-pill",
  error: "nerd-pill-red",
};

function statePill(state: string | undefined): ReactNode {
  if (!state) return "";
  return <span className={STATE_STYLE[state] ?? "nerd-pill"}>{state}</span>;
}

export function DataAppsPage() {
  const { project, branchId } = useUIState();
  const qc = useQueryClient();
  // Deep link: `?sel=<app_id>` opens that app's detail drawer. App ids are
  // globally unique platform-side, so the alias never has to be part of it
  // even though the listing spans projects.
  const [sel, setSel] = useHashSelection();
  const [selected, setSelected] = useState<DataApp | null>(null);
  // App pending a delete confirmation (row trash button).
  const [confirmApp, setConfirmApp] = useState<DataApp | null>(null);

  const q = useQuery<DataAppsResp>({
    queryKey: ["data-apps", project, branchId],
    queryFn: () =>
      api.get("/data-apps", { query: { project: project ?? undefined, branch_id: branchId ?? undefined } }),
    enabled: !!project,
  });
  const startMu = useMutation({
    mutationFn: ({ alias, appId }: { alias: string; appId: string }) =>
      api.post(`/data-apps/${encodeURIComponent(alias)}/${encodeURIComponent(appId)}/start`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["data-apps"] }),
  });
  const stopMu = useMutation({
    mutationFn: ({ alias, appId }: { alias: string; appId: string }) =>
      api.post(`/data-apps/${encodeURIComponent(alias)}/${encodeURIComponent(appId)}/stop`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["data-apps"] }),
  });
  const delMu = useMutation({
    mutationFn: ({ alias, appId }: { alias: string; appId: string }) =>
      api.delete(`/data-apps/${encodeURIComponent(alias)}/${encodeURIComponent(appId)}`),
    onSuccess: (_res, vars) => {
      // The drawer may be showing the app that was just deleted.
      if (selected?.app_id === vars.appId) closeApp();
      qc.invalidateQueries({ queryKey: ["data-apps"] });
    },
  });

  const apps = q.data?.apps ?? [];

  // Restore a deep-linked selection ONCE, after the first list load. Guarded
  // by a ref rather than by `selected` so closing the drawer does not re-open
  // it the next time the list is invalidated by a start/stop mutation.
  const restoredRef = useRef(false);
  useEffect(() => {
    if (restoredRef.current) return;
    if (!sel || !project) {
      restoredRef.current = true;
      return;
    }
    if (q.isLoading) return;
    restoredRef.current = true;
    const hit = apps.find((a) => a.app_id === sel);
    // The drawer needs the project alias to fetch anything, and that only
    // comes from the row — a link to an app outside the current project
    // selection cannot be resolved, so drop the stale id.
    if (hit) setSelected(hit);
    else setSel(null);
  }, [sel, project, q.isLoading, apps, setSel]);

  const openApp = (a: DataApp) => {
    setSelected(a);
    setSel(a.app_id);
  };
  function closeApp() {
    setSelected(null);
    setSel(null);
  }

  return (
    <div className="space-y-4">
      <PageTitle title="Data Apps" description="Custom Streamlit / Python data apps deployed on Keboola." />
      {!project ? (
        <Empty title="Select a project" />
      ) : q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox message={(q.error as Error).message} />
      ) : (
        <DataTable
          rows={apps}
          rowKey={(a) => `${a.project_alias}/${a.app_id}`}
          onRowClick={openApp}
          columns={[
            { header: "App", cell: (a) => <span className="font-bold">{a.name}</span> },
            { header: "ID", cell: (a) => <span className="text-zinc-500 text-xs">{a.app_id}</span> },
            { header: "Type", cell: (a) => <span className="text-zinc-400">{a.type}</span> },
            {
              header: "State",
              cell: (a) => <span className={STATE_STYLE[a.state] ?? "nerd-pill"}>{a.state}</span>,
            },
            { header: "Size", cell: (a) => <span className="text-zinc-500">{a.size}</span> },
            { header: "URL", cell: (a) => a.url ? <a className="text-accent text-xs" href={a.url} target="_blank" rel="noreferrer">open</a> : null },
            {
              header: "",
              align: "right",
              cell: (a) => (
                <div className="flex justify-end gap-1">
                  {a.state === "running" ? (
                    <button
                      type="button"
                      className="nerd-btn text-xs"
                      onClick={(e) => {
                        e.stopPropagation();
                        stopMu.mutate({ alias: a.project_alias, appId: a.app_id });
                      }}
                    >
                      <Pause className="w-3 h-3" />
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="nerd-btn text-xs hover:text-keboola"
                      onClick={(e) => {
                        e.stopPropagation();
                        startMu.mutate({ alias: a.project_alias, appId: a.app_id });
                      }}
                    >
                      <Play className="w-3 h-3" />
                    </button>
                  )}
                  <button
                    type="button"
                    className="nerd-btn text-xs hover:text-red-400 hover:border-red-700"
                    onClick={(e) => {
                      e.stopPropagation();
                      setConfirmApp(a);
                    }}
                  >
                    <Trash2 className="w-3 h-3" />
                  </button>
                </div>
              ),
            },
          ]}
        />
      )}

      {selected ? <DataAppDrawer app={selected} onClose={closeApp} /> : null}

      {confirmApp ? (
        <ConfirmModal
          danger
          busy={delMu.isPending}
          title="Delete data app?"
          body={
            <>
              <span className="font-mono text-accent">{confirmApp.name}</span> (
              <span className="font-mono">{confirmApp.app_id}</span>) is deleted from the Keboola
              platform together with its configuration. This is <strong>not</strong> reversible
              from here — the app URL stops resolving and the deployment record is gone.
            </>
          }
          items={[`${confirmApp.project_alias} / ${confirmApp.app_id}`]}
          confirmLabel="Delete app"
          onConfirm={() =>
            delMu.mutate(
              { alias: confirmApp.project_alias, appId: confirmApp.app_id },
              { onSettled: () => setConfirmApp(null) },
            )
          }
          onCancel={() => setConfirmApp(null)}
        />
      ) : null}
    </div>
  );
}

function DataAppDrawer({ app, onClose }: { app: DataApp; onClose: () => void }) {
  const { branchId } = useUIState();
  const [tab, setTab] = useState("overview");

  const detailQ = useQuery<DataAppDetailPayload>({
    queryKey: ["data-app-detail", app.project_alias, app.app_id, branchId],
    queryFn: () =>
      api.get(
        `/data-apps/${encodeURIComponent(app.project_alias)}/${encodeURIComponent(app.app_id)}`,
        { query: { branch_id: branchId ?? undefined } },
      ),
  });

  const subtitle = `${app.project_alias} ・ ${app.app_id}`;

  return (
    <Drawer open wide title={detailQ.data?.name || app.name} subtitle={subtitle} onClose={onClose}>
      <DetailTabs
        tabs={[
          { id: "overview", label: "Overview" },
          { id: "logs", label: "Logs" },
          { id: "raw", label: "Raw JSON" },
        ]}
        active={tab}
        onChange={setTab}
      />
      {tab === "logs" ? (
        <DataAppLogs alias={app.project_alias} appId={app.app_id} />
      ) : detailQ.isLoading ? (
        <Loading />
      ) : detailQ.error ? (
        <ErrorBox message={(detailQ.error as Error).message} />
      ) : !detailQ.data ? null : tab === "overview" ? (
        <DataAppOverview detail={detailQ.data} fallback={app} branchId={branchId} />
      ) : (
        <div className="space-y-2">
          <CopyJsonButton data={detailQ.data} />
          <JsonView data={detailQ.data} maxHeight="calc(100vh - 14rem)" />
        </div>
      )}
    </Drawer>
  );
}

function DataAppOverview({
  detail,
  fallback,
  branchId,
}: {
  detail: DataAppDetailPayload;
  /** The list row — used for the few fields the detail merge may leave empty. */
  fallback: DataApp;
  branchId: number | null;
}) {
  const url = detail.url || fallback.url;
  const autoSuspend = detail.auto_suspend_after_seconds;

  return (
    <div className="space-y-4">
      <Section icon={<Rocket className="w-3.5 h-3.5" />} label="App">
        <KeyValueGrid
          columns={3}
          items={[
            { label: "Name", value: detail.name || fallback.name },
            { label: "App ID", value: detail.app_id || fallback.app_id, mono: true },
            { label: "Type", value: detail.type || fallback.type },
            { label: "State", value: statePill(detail.state || fallback.state) },
            { label: "Desired state", value: detail.desired_state || fallback.desired_state },
            { label: "Size", value: detail.size || fallback.size },
            {
              label: "Auto-suspend",
              value: autoSuspend != null ? `${autoSuspend}s` : "",
              mono: true,
            },
            { label: "Project", value: detail.project_alias || fallback.project_alias, mono: true },
            { label: "Branch", value: branchId != null ? String(branchId) : "", mono: true },
            { label: "Config ID", value: detail.config_id || fallback.config_id, mono: true },
            {
              label: "Config version (deployed)",
              value: detail.config_version_deployed,
              mono: true,
            },
            { label: "Config version (storage)", value: detail.config_version_storage, mono: true },
            { label: "Slug", value: detail.slug, mono: true },
            { label: "Last start", value: detail.last_start_timestamp ?? "", mono: true },
          ]}
        />
        {detail.description ? (
          <p className="text-xs text-zinc-600 mt-3 dark:text-zinc-400">{detail.description}</p>
        ) : null}
        {url ? (
          <a
            href={url}
            target="_blank"
            rel="noreferrer"
            className="nerd-btn text-xs inline-flex items-center gap-1 mt-3 hover:text-keboola"
          >
            open app →
          </a>
        ) : null}
      </Section>
    </div>
  );
}

function DataAppLogs({ alias, appId }: { alias: string; appId: string }) {
  const q = useQuery<DataAppLogsPayload>({
    queryKey: ["data-app-logs", alias, appId],
    queryFn: () =>
      api.get(
        `/data-apps/${encodeURIComponent(alias)}/${encodeURIComponent(appId)}/logs`,
        { query: { lines: LOG_TAIL_LINES } },
      ),
    // Tab-activated: the endpoint answers HTTP 400 for an app that was never
    // deployed, so it must not fire just because the drawer opened.
    refetchOnWindowFocus: false,
  });

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <button
          type="button"
          className="nerd-btn text-xs flex items-center gap-1 hover:text-keboola"
          onClick={() => q.refetch()}
          disabled={q.isFetching}
        >
          <RefreshCw className={`w-3 h-3 ${q.isFetching ? "animate-spin" : ""}`} /> refresh
        </button>
        <span className="text-[11px] text-zinc-500">
          last {LOG_TAIL_LINES} lines
          {q.data?.lines_returned != null ? ` — ${q.data.lines_returned} returned` : ""}
        </span>
      </div>
      {q.isLoading ? <Loading /> : null}
      {/* A never-deployed app has no container, and the Data Science endpoint
          answers with an error rather than an empty buffer — surface it as-is
          instead of rendering a blank pane that looks like "no output". */}
      {q.error ? <ErrorBox message={(q.error as Error).message} /> : null}
      {q.data ? (
        q.data.text ? (
          <pre className="nerd-code whitespace-pre-wrap" style={{ maxHeight: "60vh" }}>
            {q.data.text}
          </pre>
        ) : (
          <div className="text-xs text-zinc-500">Container log buffer is empty.</div>
        )
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

/**
 * Local twin of the copy button `RawDetail` renders on its Raw tab (that one
 * is private to the module). Same contract: `navigator.clipboard` is undefined
 * on a non-secure origin — kbagent serve is plain http by default — so the
 * button degrades to a "select it manually" hint instead of throwing.
 */
function CopyJsonButton({ data }: { data: unknown }) {
  const [state, setState] = useState<"idle" | "copied" | "manual">("idle");
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  const onCopy = () => {
    const clip = navigator.clipboard;
    if (!clip || typeof clip.writeText !== "function") {
      setState("manual");
      return;
    }
    clip.writeText(JSON.stringify(data, null, 2)).then(
      () => {
        setState("copied");
        if (timerRef.current) clearTimeout(timerRef.current);
        timerRef.current = setTimeout(() => setState("idle"), 2000);
      },
      () => setState("manual"),
    );
  };

  return (
    <div className="flex items-center gap-2">
      <button
        type="button"
        className="nerd-btn text-xs flex items-center gap-1 hover:text-keboola"
        onClick={onCopy}
        title="Copy the raw JSON payload to the clipboard"
      >
        {state === "copied" ? (
          <>
            <Check className="w-3 h-3" /> copied
          </>
        ) : (
          <>
            <Copy className="w-3 h-3" /> copy JSON
          </>
        )}
      </button>
      {state === "manual" ? (
        <span className="text-[11px] text-amber-700 dark:text-neon-amber">
          Clipboard unavailable (non-secure origin) — select the JSON below and copy manually.
        </span>
      ) : null}
    </div>
  );
}
