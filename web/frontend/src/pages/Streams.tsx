import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, Eye, EyeOff, Plus, Radio, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { Drawer } from "../components/Drawer";
import { Empty, ErrorBox, Loading, PageTitle } from "../components/Empty";
import { JsonView } from "../components/JsonView";
import { DataTable } from "../components/Table";
import { useUIState } from "../state";
import { useHashSelection } from "../useHashSelection";
import type { DataStreamDetail, DataStreamSource } from "../types";

/**
 * Data Streams (OpenTelemetry / OTLP). Mirrors `kbagent stream *`:
 * list sources, create an OTLP/HTTP source (auto-provisioning the
 * logs/metrics/traces sinks), inspect a source's endpoints + destination,
 * and delete one. The OTLP ingest URL embeds a secret that the backend
 * masks unless `reveal=true` -- the detail drawer exposes a reveal toggle.
 */
interface StreamListResp {
  alias: string;
  branch_id: string;
  sources: DataStreamSource[];
}

/**
 * The stream control-plane API types its branch ref as a string. The UI's
 * global `branchId` is a numeric Storage branch ID; numeric IDs are valid refs
 * (see `test_branch_override` in `tests/test_stream_service.py`, which drives
 * `branch_id="1234"`), so we stringify here to match the API contract
 * explicitly rather than lean on JSON/query coercion. `null` (default branch)
 * maps to `undefined`, letting the backend fall back to its `"default"` ref.
 */
function branchRef(branchId: number | null): string | undefined {
  return branchId != null ? String(branchId) : undefined;
}

export function StreamsPage() {
  const { project, branchId } = useUIState();
  const qc = useQueryClient();
  // Deep link: `?sel=<sourceId>` opens that source's detail drawer.
  const [sel, setSel] = useHashSelection();
  const [selected, setSelected] = useState<DataStreamSource | null>(null);
  const [showCreate, setShowCreate] = useState(false);

  const q = useQuery<StreamListResp>({
    queryKey: ["streams", project, branchId],
    queryFn: () =>
      api.get(`/stream/${encodeURIComponent(project ?? "")}/list`, {
        query: { branch: branchRef(branchId) },
      }),
    enabled: !!project,
  });

  // Restore a deep-linked source ONCE, after the first list load. A link to a
  // deleted source just leaves the list open.
  const restoredRef = useRef(false);
  useEffect(() => {
    if (restoredRef.current) return;
    if (!sel) {
      restoredRef.current = true;
      return;
    }
    if (!q.data) return;
    restoredRef.current = true;
    const hit = q.data.sources.find((s) => s.source_id === sel);
    if (hit) setSelected(hit);
  }, [sel, q.data]);

  const openSource = (s: DataStreamSource) => {
    setSelected(s);
    setSel(s.source_id);
  };
  const closeSource = () => {
    setSelected(null);
    setSel(null);
  };

  const deleteMu = useMutation({
    // `stream delete` is exposed as POST /delete (not HTTP DELETE) so the
    // dry-run flag can ride in the body alongside the source id.
    mutationFn: (sourceId: string) =>
      api.post(`/stream/${encodeURIComponent(project ?? "")}/delete`, {
        source_id: sourceId,
        branch_id: branchRef(branchId),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["streams"] });
      closeSource();
    },
  });

  return (
    <div className="space-y-4">
      <PageTitle
        title="Data Streams"
        description={`OpenTelemetry / OTLP ingest sources in ${project ?? "(no project)"}. Telemetry lands in the in.c-otlp-<source> bucket via auto-provisioned logs/metrics/traces sinks.`}
        actions={
          <button
            type="button"
            className="nerd-btn flex items-center gap-1 hover:text-keboola"
            disabled={!project}
            onClick={() => setShowCreate(true)}
          >
            <Plus className="w-3 h-3" /> New source
          </button>
        }
      />
      {deleteMu.error ? (
        <ErrorBox message={`Delete failed: ${(deleteMu.error as Error).message}`} />
      ) : null}
      {!project ? (
        <Empty title="Select a project" />
      ) : q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox message={(q.error as Error).message} />
      ) : (
        <DataTable
          rows={q.data?.sources ?? []}
          rowKey={(s) => s.source_id}
          onRowClick={openSource}
          emptyMessage="No Data Streams sources yet. Create one to get an OTLP ingest endpoint."
          columns={[
            {
              header: "Name",
              cell: (s) => <span className="font-bold text-accent">{s.name}</span>,
            },
            {
              header: "Source ID",
              cell: (s) => <span className="font-mono text-zinc-500 text-xs">{s.source_id}</span>,
            },
            {
              header: "Type",
              cell: (s) => <span className="nerd-pill uppercase">{s.type || "?"}</span>,
            },
            {
              header: "Endpoint",
              cell: (s) =>
                s.base_endpoint ? (
                  <span className="font-mono text-zinc-600 dark:text-zinc-400 text-xs break-all">
                    {s.base_endpoint}
                  </span>
                ) : (
                  <span className="text-xs text-zinc-500 dark:text-zinc-600">-</span>
                ),
            },
            {
              header: "",
              align: "right",
              cell: (s) => (
                <button
                  type="button"
                  className="nerd-btn text-xs hover:text-red-600 hover:border-red-300 dark:hover:text-red-400 dark:hover:border-red-700"
                  onClick={(e) => {
                    e.stopPropagation();
                    if (confirm(`Delete Data Stream source '${s.name}' (${s.source_id})?\n\nThis removes the source and its sinks. Data already in Storage stays.`)) {
                      deleteMu.mutate(s.source_id);
                    }
                  }}
                  title="Delete source"
                >
                  <Trash2 className="w-3 h-3" />
                </button>
              ),
            },
          ]}
        />
      )}
      {showCreate && project ? (
        <CreateSourceDrawer
          project={project}
          branchId={branchId}
          onClose={() => setShowCreate(false)}
          onCreated={(sourceId) => {
            setShowCreate(false);
            qc.invalidateQueries({ queryKey: ["streams"] });
            const created = q.data?.sources.find((s) => s.source_id === sourceId);
            // Open the detail drawer for the new source. If the list hasn't
            // refetched yet we synthesize a minimal row -- the drawer fetches
            // its own full detail by source_id regardless.
            openSource(
              created ?? {
                source_id: sourceId,
                name: sourceId,
                type: "otlp",
                description: "",
                base_endpoint: "",
              },
            );
          }}
        />
      ) : null}
      {selected && project ? (
        <SourceDetailDrawer
          project={project}
          branchId={branchId}
          source={selected}
          onClose={closeSource}
          onDelete={(sourceId) => {
            if (confirm(`Delete Data Stream source '${sourceId}'?`)) {
              deleteMu.mutate(sourceId);
            }
          }}
        />
      ) : null}
    </div>
  );
}

function CreateSourceDrawer({
  project,
  branchId,
  onClose,
  onCreated,
}: {
  project: string;
  branchId: number | null;
  onClose: () => void;
  onCreated: (sourceId: string) => void;
}) {
  const [name, setName] = useState("");
  const [sourceType, setSourceType] = useState<"otlp" | "http">("otlp");
  const [provisionSinks, setProvisionSinks] = useState(true);
  const [ifNotExists, setIfNotExists] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const createMu = useMutation({
    mutationFn: () =>
      api.post<DataStreamDetail>(`/stream/${encodeURIComponent(project)}/create-source`, {
        name,
        source_type: sourceType,
        branch_id: branchRef(branchId),
        provision_sinks: provisionSinks,
        if_not_exists: ifNotExists,
      }),
    onSuccess: (detail) => onCreated(detail.source_id),
    onError: (err) => setError((err as Error).message),
  });

  return (
    <Drawer
      open={true}
      onClose={onClose}
      title="New Data Stream source"
      subtitle={`Project: ${project}. Creates an ingest source and (for OTLP) the logs/metrics/traces sinks.`}
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
          <Radio className="w-3 h-3 inline mr-1" />
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
            placeholder="my-telemetry"
            required
          />
          <span className="text-zinc-500 dark:text-zinc-600">
            Used in the source id and the OTLP ingest URL path.
          </span>
        </label>

        <div className="text-xs text-zinc-600 dark:text-zinc-400">
          Type
          <div className="flex gap-2 mt-1">
            {(["otlp", "http"] as const).map((t) => (
              <button
                key={t}
                type="button"
                className={`nerd-btn text-xs uppercase ${sourceType === t ? "border-keboola text-keboola" : ""}`}
                onClick={() => setSourceType(t)}
              >
                {t}
              </button>
            ))}
          </div>
          <span className="text-zinc-500 dark:text-zinc-600">
            {sourceType === "otlp"
              ? "OpenTelemetry Protocol -- logs, metrics, and traces over http/protobuf."
              : "Generic HTTP ingest source."}
          </span>
        </div>

        {sourceType === "otlp" ? (
          <label className="flex items-center gap-2 text-xs text-zinc-600 dark:text-zinc-400">
            <input
              type="checkbox"
              checked={provisionSinks}
              onChange={(e) => setProvisionSinks(e.target.checked)}
            />
            Auto-provision logs / metrics / traces sinks (recommended -- without
            them data has nowhere to land)
          </label>
        ) : null}

        <label className="flex items-center gap-2 text-xs text-zinc-600 dark:text-zinc-400">
          <input
            type="checkbox"
            checked={ifNotExists}
            onChange={(e) => setIfNotExists(e.target.checked)}
          />
          If-not-exists (reuse an existing source with the same name instead of
          erroring)
        </label>

        {error ? <ErrorBox message={error} /> : null}
      </div>
    </Drawer>
  );
}

function SourceDetailDrawer({
  project,
  branchId,
  source,
  onClose,
  onDelete,
}: {
  project: string;
  branchId: number | null;
  source: DataStreamSource;
  onClose: () => void;
  onDelete: (sourceId: string) => void;
}) {
  const [reveal, setReveal] = useState(false);
  const [tab, setTab] = useState<"overview" | "raw">("overview");

  const q = useQuery<DataStreamDetail>({
    queryKey: ["stream-detail", project, source.source_id, branchId, reveal],
    queryFn: () =>
      api.get(`/stream/${encodeURIComponent(project)}/detail`, {
        query: {
          source_id: source.source_id,
          branch: branchRef(branchId),
          reveal,
        },
      }),
  });

  const detail = q.data;
  const signals = detail ? Object.entries(detail.signal_endpoints ?? {}) : [];
  const tables = detail ? Object.entries(detail.destination?.tables ?? {}) : [];

  return (
    <Drawer
      open={true}
      onClose={onClose}
      title={source.name}
      subtitle={`${source.type || "?"} source ・ ${source.source_id}`}
      width="max-w-4xl"
      actions={
        <>
          <button
            type="button"
            className={`nerd-btn flex items-center gap-1 ${reveal ? "border-amber-400 text-amber-600 dark:text-neon-amber" : "hover:text-keboola"}`}
            onClick={() => setReveal((r) => !r)}
            title={reveal ? "Hide the secret in the endpoint URL" : "Reveal the secret embedded in the OTLP endpoint URL"}
          >
            {reveal ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
            {reveal ? "Hide secret" : "Reveal secret"}
          </button>
          <button
            type="button"
            className="nerd-btn flex items-center gap-1 hover:text-red-600 hover:border-red-300 dark:hover:text-red-400 dark:hover:border-red-700"
            onClick={() => onDelete(source.source_id)}
          >
            <Trash2 className="w-3.5 h-3.5" /> Delete
          </button>
        </>
      }
    >
      {q.isLoading ? <Loading /> : null}
      {q.error ? <ErrorBox message={(q.error as Error).message} /> : null}
      {detail ? (
        <>
          <div className="flex gap-2 mb-4">
            <button
              type="button"
              className={`nerd-btn text-xs ${tab === "overview" ? "border-keboola text-keboola" : ""}`}
              onClick={() => setTab("overview")}
            >
              Overview
            </button>
            <button
              type="button"
              className={`nerd-btn text-xs ${tab === "raw" ? "border-keboola text-keboola" : ""}`}
              onClick={() => setTab("raw")}
            >
              Raw JSON
            </button>
          </div>

          {tab === "overview" ? (
            <div className="space-y-4">
              <div className="nerd-card">
                <h3 className="text-sm font-bold text-keboola mb-3">Source</h3>
                <div className="grid grid-cols-2 gap-3 text-xs">
                  <KV label="Source ID" value={detail.source_id} mono />
                  <KV label="Name" value={detail.name} />
                  <KV label="Type" value={detail.type} />
                  <KV label="Protocol" value={detail.protocol || "-"} mono />
                  <KV label="Branch" value={detail.branch_id} mono />
                  <KV label="Description" value={detail.description || "-"} />
                </div>
              </div>

              <div className="nerd-card">
                <div className="flex items-center justify-between mb-2">
                  <h3 className="text-sm font-bold text-keboola">Ingest endpoint</h3>
                  {detail.secret_revealed ? (
                    <span className="nerd-pill-amber text-[10px]">secret revealed</span>
                  ) : (
                    <span className="nerd-pill text-[10px]">secret masked</span>
                  )}
                </div>
                <EndpointRow url={detail.endpoint} />
                {!detail.secret_revealed ? (
                  <div className="text-[11px] text-zinc-500 dark:text-zinc-600 mt-2">
                    The OTLP URL embeds a write secret (masked as <code>***</code>).
                    Use "Reveal secret" to copy the full URL into your OTLP
                    exporter -- treat it like a credential.
                  </div>
                ) : null}
                {signals.length > 0 ? (
                  <div className="mt-3 space-y-1">
                    <div className="text-[10px] uppercase tracking-wider text-zinc-500">
                      Per-signal endpoints
                    </div>
                    {signals.map(([signal, url]) => (
                      <div key={signal} className="flex items-center gap-2">
                        <span className="nerd-pill text-[10px] w-16 shrink-0 justify-center">{signal}</span>
                        <EndpointRow url={url} />
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>

              <div className="nerd-card">
                <h3 className="text-sm font-bold text-keboola mb-3">Destination</h3>
                {detail.destination?.bucket ||
                detail.destination?.buckets?.length ||
                tables.length > 0 ? (
                  <div className="space-y-2 text-xs">
                    {detail.destination?.bucket ? (
                      <KV label="Bucket" value={detail.destination.bucket} mono />
                    ) : detail.destination?.buckets?.length ? (
                      <KV label="Buckets" value={detail.destination.buckets.join(", ")} mono />
                    ) : null}
                    {tables.map(([signal, tableId]) => (
                      <div key={signal} className="flex items-center gap-2">
                        <span className="nerd-pill text-[10px] w-16 shrink-0 justify-center">{signal}</span>
                        <span className="font-mono text-accent break-all">{tableId}</span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="text-xs text-zinc-500">
                    No sinks yet -- this source has no destination tables. For
                    OTLP, create it with sink provisioning enabled.
                  </div>
                )}
              </div>

              {detail.import_conditions ? (
                <div className="nerd-card">
                  <h3 className="text-sm font-bold text-keboola mb-3">Import conditions</h3>
                  <JsonView data={detail.import_conditions} />
                </div>
              ) : null}
            </div>
          ) : (
            <JsonView data={detail} />
          )}
        </>
      ) : null}
    </Drawer>
  );
}

/** Endpoint URL with an inline copy button. Long URLs wrap (break-all). */
function EndpointRow({ url }: { url: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      /* Clipboard API blocked by browser permissions -- silent. */
    }
  };
  if (!url) {
    return <span className="text-xs text-zinc-500 dark:text-zinc-600">-</span>;
  }
  return (
    <div className="flex items-start gap-2 min-w-0">
      <code className="nerd-code flex-1 text-[11px] break-all">{url}</code>
      <button
        type="button"
        onClick={copy}
        className={`nerd-btn text-[10px] shrink-0 ${copied ? "border-keboola text-keboola" : "hover:text-keboola hover:border-keboola/60"}`}
        title="Copy endpoint to clipboard"
      >
        {copied ? "✓" : <Copy className="w-3 h-3" />}
      </button>
    </div>
  );
}

function KV({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-zinc-500">{label}</div>
      <div
        className={`text-xs mt-0.5 ${mono ? "font-mono text-accent" : "text-zinc-800 dark:text-zinc-200"} break-all`}
      >
        {value || "—"}
      </div>
    </div>
  );
}
