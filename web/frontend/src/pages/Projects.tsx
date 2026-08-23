import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  CheckCircle2,
  Flag,
  Gauge,
  KeyRound,
  Plus,
  RefreshCw,
  Server,
  Trash2,
  XCircle,
} from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { ApiError, api } from "../api/client";
import { ConfirmModal } from "../components/ConfirmModal";
import { Drawer } from "../components/Drawer";
import { Empty, ErrorBox, Loading, PageTitle } from "../components/Empty";
import { KeyValueGrid } from "../components/KeyValueGrid";
import { PillList } from "../components/PillList";
import { RawDetail } from "../components/RawDetail";
import { DataTable } from "../components/Table";
import type { Project, ProjectStatus } from "../types";
import { useHashSelection } from "../useHashSelection";

interface BulkDeleteResult {
  removed: string[];
  failed: { alias: string; error: string }[];
  dry_run: boolean;
}

/**
 * `GET /projects/{alias}/info` — mirrors `ProjectService.get_info`, which
 * reshapes `/v2/storage/tokens/verify`. Everything past `stack_url` comes
 * straight from the stack, so each field is optional: an older stack may omit
 * it entirely and the overview must degrade rather than render "undefined".
 */
interface ProjectInfoPayload {
  alias?: string;
  project_id?: number | null;
  project_name?: string;
  stack_url?: string;
  auth_mode?: string;
  default_backend?: string;
  features?: string[];
  // Storage returns each limit as `{name, value}`, but older stacks (and some
  // limits) send the bare scalar — both shapes are unpacked by `limitValue`.
  limits?: Record<string, unknown>;
  metrics?: Record<string, unknown>;
  token_id?: string;
  token_description?: string;
  is_master_token?: boolean;
  token_expires?: string | null;
}

export function ProjectsPage() {
  const qc = useQueryClient();
  const [showAdd, setShowAdd] = useState(false);
  // Deep link: `?sel=<alias>` opens that project's detail drawer.
  const [sel, setSel] = useHashSelection();
  const [selected, setSelected] = useState<Project | null>(null);
  const [selectedAliases, setSelectedAliases] = useState<Set<string>>(new Set());
  // Aliases pending a remove-confirmation (single trash button or bulk action).
  const [confirmAliases, setConfirmAliases] = useState<string[] | null>(null);
  // Inline banner for remove failures (partial or total request error).
  const [removeNotice, setRemoveNotice] = useState<string | null>(null);

  const projectsQ = useQuery<{ projects: Project[] }>({
    queryKey: ["projects"],
    queryFn: () => api.get("/projects"),
  });
  const statusQ = useQuery<{ status: ProjectStatus[] }>({
    queryKey: ["projects-status"],
    queryFn: () => api.get("/projects/status"),
  });

  // /projects/status performs an opportunistic backfill of org_id/org_name
  // on the backend for projects registered before #290. Invalidate the
  // /projects cache once status finishes so the ORG column picks up the
  // freshly persisted values without the user having to reload the page.
  useEffect(() => {
    if (statusQ.data) qc.invalidateQueries({ queryKey: ["projects"] });
  }, [statusQ.data, qc]);

  const useMu = useMutation({
    mutationFn: (alias: string) => api.post(`/projects/use/${encodeURIComponent(alias)}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["projects"] }),
  });

  const bulkDeleteMu = useMutation({
    mutationFn: (aliases: string[]) =>
      api.post<BulkDeleteResult>("/projects/bulk-delete", { aliases }),
    onSuccess: (res) => {
      setSelectedAliases(new Set());
      // The detail pane may be showing a project that was just removed.
      if (selected && res.removed.includes(selected.alias)) {
        setSelected(null);
        setSel(null);
      }
      qc.invalidateQueries({ queryKey: ["projects"] });
      if (res.failed.length > 0) {
        const lines = res.failed.map((f) => `${f.alias} (${f.error})`).join(", ");
        setRemoveNotice(
          `Removed ${res.removed.length}; ${res.failed.length} failed: ${lines}`,
        );
      } else {
        setRemoveNotice(null);
      }
    },
    onError: (err) => {
      // A total request failure (network / 5xx) would otherwise be silent --
      // the modal closes via onSettled with no feedback.
      setRemoveNotice(err instanceof ApiError ? err.message : (err as Error).message);
    },
  });

  const projects = projectsQ.data?.projects ?? [];

  // Restore a deep-linked selection ONCE, after the first list load. Guarded
  // by a ref rather than by `selected`, so closing the drawer is not undone
  // by a later refetch. An alias this config does not know clears the link.
  const restoredRef = useRef(false);
  useEffect(() => {
    if (restoredRef.current) return;
    if (!sel) {
      restoredRef.current = true;
      return;
    }
    if (projectsQ.isLoading) return;
    restoredRef.current = true;
    const hit = projects.find((p) => p.alias === sel);
    if (hit) setSelected(hit);
    else setSel(null);
  }, [sel, setSel, projects, projectsQ.isLoading]);

  const openProject = (p: Project) => {
    setSelected(p);
    setSel(p.alias);
  };
  const closeProject = () => {
    setSelected(null);
    setSel(null);
  };

  // Keep the selection in sync with the live project list: drop any alias that
  // no longer exists (e.g. removed in another tab) so stale keys never linger.
  useEffect(() => {
    setSelectedAliases((prev) => {
      const live = new Set(projects.map((p) => p.alias));
      const next = new Set([...prev].filter((a) => live.has(a)));
      return next.size === prev.size ? prev : next;
    });
  }, [projects]);

  const toggleRow = (alias: string, checked: boolean) =>
    setSelectedAliases((prev) => {
      const next = new Set(prev);
      if (checked) next.add(alias);
      else next.delete(alias);
      return next;
    });

  const toggleAll = (checked: boolean) =>
    setSelectedAliases(checked ? new Set(projects.map((p) => p.alias)) : new Set());

  const requestBulkRemove = () => {
    if (selectedAliases.size === 0) return;
    setConfirmAliases([...selectedAliases]);
  };

  const confirmRemove = () => {
    if (!confirmAliases) return;
    setRemoveNotice(null);
    bulkDeleteMu.mutate(confirmAliases, {
      onSettled: () => setConfirmAliases(null),
    });
  };

  const statusByAlias = new Map(statusQ.data?.status?.map((s) => [s.alias, s]) ?? []);

  return (
    <div className="space-y-4">
      <PageTitle
        title="Projects"
        description="Keboola projects registered in this kbagent config."
        actions={
          <>
            {selectedAliases.size > 0 ? (
              <button
                type="button"
                className="nerd-btn flex items-center gap-1 text-red-400 border-red-700 hover:bg-red-950/40"
                disabled={bulkDeleteMu.isPending}
                onClick={requestBulkRemove}
              >
                <Trash2 className="w-3 h-3" />{" "}
                {bulkDeleteMu.isPending ? "Removing..." : "Remove from kbagent"}
              </button>
            ) : null}
            <button
              type="button"
              className="nerd-btn flex items-center gap-1"
              onClick={() => qc.invalidateQueries({ queryKey: ["projects-status"] })}
            >
              <RefreshCw className="w-3 h-3" /> Refresh status
            </button>
            <button
              type="button"
              className="nerd-btn flex items-center gap-1 hover:text-keboola"
              onClick={() => setShowAdd((v) => !v)}
            >
              <Plus className="w-3 h-3" /> Add project
            </button>
          </>
        }
      />

      {removeNotice ? (
        <div className="flex items-start justify-between gap-2">
          <ErrorBox message={removeNotice} />
          <button
            type="button"
            className="nerd-btn text-xs shrink-0"
            onClick={() => setRemoveNotice(null)}
          >
            Dismiss
          </button>
        </div>
      ) : null}

      {showAdd ? (
        <AddProject
          onDone={() => {
            setShowAdd(false);
            qc.invalidateQueries({ queryKey: ["projects"] });
          }}
        />
      ) : null}

      {projectsQ.isLoading ? <Loading /> : null}
      {projectsQ.error ? (
        <ErrorBox message={(projectsQ.error as Error).message} />
      ) : projectsQ.data?.projects.length === 0 ? (
        <Empty
          title="No projects configured"
          hint="Click 'Add project' to register your first Keboola project."
        />
      ) : (
        <DataTable
          rows={projects}
          rowKey={(p) => p.alias}
          onRowClick={openProject}
          selectedKeys={selectedAliases}
          onToggleRow={toggleRow}
          onToggleAll={toggleAll}
          columns={[
            {
              header: "Alias",
              cell: (p) => (
                <span className="font-bold text-zinc-900 dark:text-zinc-100">
                  {p.alias} {p.is_default ? <span className="nerd-pill-green">default</span> : null}
                </span>
              ),
            },
            {
              header: "Org",
              cell: (p) => {
                // Org name comes from Manage API (via `org setup`); the
                // Storage token alone only returns the numeric id. We show
                // the name when we have it, fall back to "#73" so multi-org
                // setups are still distinguishable, and only render "—"
                // when neither is known (very old stacks without
                // organization in the verify response).
                if (p.org_name) {
                  return <span className="text-zinc-700 dark:text-zinc-300">{p.org_name}</span>;
                }
                if (p.org_id != null) {
                  return (
                    <span
                      className="text-zinc-500 font-mono"
                      title={`Organization #${p.org_id}. Name unknown — Storage API only exposes the id. Run \`kbagent org setup --org-id ${p.org_id} --url <stack>\` to populate the name.`}
                    >
                      #{p.org_id}
                    </span>
                  );
                }
                return (
                  <span
                    className="text-zinc-400"
                    title="Org info unavailable — older stacks omit the organization block from /v2/storage/tokens/verify. Use `kbagent org setup` to populate via the Manage API."
                  >
                    —
                  </span>
                );
              },
            },
            { header: "Project", cell: (p) => <span>{p.project_name}</span> },
            {
              header: "ID",
              cell: (p) => <span className="text-zinc-500">{p.project_id ?? "-"}</span>,
            },
            { header: "Stack", cell: (p) => <span className="text-zinc-500 text-xs">{p.stack_url}</span> },
            {
              header: "Status",
              cell: (p) => {
                const s = statusByAlias.get(p.alias);
                if (!s) return <span className="text-zinc-600">-</span>;
                return s.status === "ok" ? (
                  <span className="nerd-pill-green flex items-center gap-1 w-fit">
                    <CheckCircle2 className="w-3 h-3" /> {s.response_time_ms}ms
                  </span>
                ) : (
                  <span className="nerd-pill-red flex items-center gap-1 w-fit">
                    <XCircle className="w-3 h-3" /> {s.error_code}
                  </span>
                );
              },
            },
            {
              header: "Token",
              cell: (p) => <span className="text-zinc-500 text-xs">{p.token}</span>,
            },
            {
              header: "",
              align: "right",
              cell: (p) => (
                <div className="flex justify-end gap-1">
                  {!p.is_default ? (
                    <button
                      type="button"
                      className="nerd-btn text-xs"
                      onClick={(e) => {
                        e.stopPropagation();
                        useMu.mutate(p.alias);
                      }}
                    >
                      Make default
                    </button>
                  ) : null}
                  <button
                    type="button"
                    className="nerd-btn text-xs hover:text-red-400 hover:border-red-700"
                    onClick={(e) => {
                      e.stopPropagation();
                      setConfirmAliases([p.alias]);
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

      {selected ? (
        <ProjectDetailDrawer project={selected} onClose={closeProject} />
      ) : null}

      {confirmAliases ? (
        <ConfirmModal
          danger
          title={
            confirmAliases.length === 1
              ? "Remove project from kbagent"
              : `Remove ${confirmAliases.length} projects from kbagent`
          }
          body={
            <>
              This only unregisters{" "}
              {confirmAliases.length === 1 ? "this project" : "these projects"} locally (edits the
              kbagent config). It does <strong>not</strong> delete the Keboola{" "}
              {confirmAliases.length === 1 ? "project" : "projects"}.
            </>
          }
          items={confirmAliases}
          confirmLabel="Remove from kbagent"
          busy={bulkDeleteMu.isPending}
          onConfirm={confirmRemove}
          onCancel={() => setConfirmAliases(null)}
        />
      ) : null}
    </div>
  );
}

function ProjectDetailDrawer({
  project,
  onClose,
}: {
  project: Project;
  onClose: () => void;
}) {
  const infoQ = useQuery<ProjectInfoPayload>({
    queryKey: ["project-info", project.alias],
    queryFn: () => api.get(`/projects/${encodeURIComponent(project.alias)}/info`),
  });

  const subtitle = [project.project_name, project.project_id != null ? `#${project.project_id}` : null]
    .filter(Boolean)
    .join(" ・ ");

  return (
    <Drawer open wide title={project.alias} subtitle={subtitle} onClose={onClose}>
      {infoQ.isLoading ? <Loading /> : null}
      {infoQ.error ? <ErrorBox message={(infoQ.error as Error).message} /> : null}
      {infoQ.data ? (
        <RawDetail
          data={infoQ.data}
          overview={<ProjectOverview info={infoQ.data} project={project} />}
        />
      ) : null}
    </Drawer>
  );
}

function ProjectOverview({
  info,
  project,
}: {
  info: ProjectInfoPayload;
  project: Project;
}) {
  const stackUrl = info.stack_url ?? project.stack_url;
  const projectId = info.project_id ?? project.project_id;
  // The admin URL only resolves with a numeric id; a project registered
  // against a stack that never returned one gets no link rather than a 404.
  const adminUrl =
    stackUrl && projectId != null
      ? `${stackUrl.replace(/\/+$/, "")}/admin/projects/${projectId}`
      : null;
  const limits = Object.entries(info.limits ?? {});

  return (
    <div className="space-y-4">
      <Section icon={<Server className="w-3.5 h-3.5" />} label="Project">
        <KeyValueGrid
          columns={3}
          items={[
            { label: "Alias", value: info.alias ?? project.alias, mono: true },
            { label: "Project ID", value: projectId != null ? String(projectId) : "", mono: true },
            { label: "Name", value: info.project_name ?? project.project_name },
            {
              label: "Stack URL",
              value: stackUrl ? (
                <a href={stackUrl} target="_blank" rel="noreferrer" className="hover:underline">
                  {stackUrl}
                </a>
              ) : (
                ""
              ),
              mono: true,
            },
            { label: "Auth mode", value: info.auth_mode },
            { label: "Default backend", value: info.default_backend },
            { label: "Organization", value: project.org_name ?? orgLabel(project.org_id) },
          ]}
        />
        {adminUrl ? (
          <a
            href={adminUrl}
            target="_blank"
            rel="noreferrer"
            className="inline-block mt-3 text-xs text-accent hover:underline"
          >
            open in Keboola UI →
          </a>
        ) : null}
      </Section>

      <Section icon={<KeyRound className="w-3.5 h-3.5" />} label="Token">
        <KeyValueGrid
          columns={3}
          items={[
            { label: "Token ID", value: info.token_id, mono: true },
            { label: "Description", value: info.token_description },
            {
              label: "Scope",
              value: info.is_master_token ? (
                <span className="nerd-pill-green text-[10px]">master</span>
              ) : (
                <span className="nerd-pill text-[10px]">scoped</span>
              ),
            },
            { label: "Expires", value: info.token_expires ?? "never", mono: true },
            { label: "Masked value", value: project.token, mono: true },
          ]}
        />
      </Section>

      <Section icon={<Flag className="w-3.5 h-3.5" />} label={`Features (${info.features?.length ?? 0})`}>
        <PillList items={info.features} empty="No features enabled on this project." />
      </Section>

      {limits.length > 0 ? (
        <Section icon={<Gauge className="w-3.5 h-3.5" />} label={`Limits (${limits.length})`}>
          <table className="w-full text-xs">
            <tbody>
              {limits.map(([name, raw]) => (
                <tr key={name} className="border-b border-zinc-100 last:border-0 dark:border-zinc-900">
                  <td className="py-1 pr-3 text-zinc-600 break-all dark:text-zinc-400">{name}</td>
                  <td className="py-1 text-right font-mono text-zinc-800 dark:text-zinc-200">
                    {limitValue(raw)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      ) : null}
    </div>
  );
}

/** Card with the icon + micro-label header used by the Jobs detail drawer. */
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

function orgLabel(orgId: number | null): string {
  return orgId != null ? `#${orgId}` : "";
}

/** Storage sends a limit as either `{name, value}` or the bare scalar. */
function limitValue(raw: unknown): string {
  const value =
    raw !== null && typeof raw === "object" && "value" in raw
      ? (raw as { value: unknown }).value
      : raw;
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") return value.toLocaleString();
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function AddProject({ onDone }: { onDone: () => void }) {
  const [alias, setAlias] = useState("");
  const [stackUrl, setStackUrl] = useState("https://connection.keboola.com");
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);

  const mu = useMutation({
    mutationFn: () => api.post("/projects", { alias, stack_url: stackUrl, token }),
    onSuccess: () => {
      onDone();
      setAlias("");
      setToken("");
    },
    onError: (err) => {
      setError(err instanceof ApiError ? err.message : (err as Error).message);
    },
  });

  return (
    <form
      className="nerd-card space-y-3"
      onSubmit={(e) => {
        e.preventDefault();
        setError(null);
        mu.mutate();
      }}
    >
      <h3 className="font-bold text-keboola">Add new project</h3>
      <div className="grid grid-cols-3 gap-3">
        <label className="text-xs text-zinc-400">
          Alias
          <input
            className="nerd-input w-full mt-1"
            value={alias}
            onChange={(e) => setAlias(e.target.value)}
            required
            placeholder="my-project"
          />
        </label>
        <label className="text-xs text-zinc-400">
          Stack URL
          <input
            className="nerd-input w-full mt-1"
            value={stackUrl}
            onChange={(e) => setStackUrl(e.target.value)}
            required
          />
        </label>
        <label className="text-xs text-zinc-400">
          Storage Token
          <input
            type="password"
            className="nerd-input w-full mt-1"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            required
            placeholder="123-12345-..."
          />
        </label>
      </div>
      {error ? <div className="text-red-400 text-xs">{error}</div> : null}
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={mu.isPending}
          className="nerd-btn hover:text-keboola"
        >
          {mu.isPending ? "Adding..." : "Add project"}
        </button>
        <button type="button" className="nerd-btn" onClick={onDone}>
          Cancel
        </button>
      </div>
    </form>
  );
}
