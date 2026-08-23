import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Check,
  Download,
  Eye,
  Info,
  Layers,
  Loader2,
  Pencil,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api/client";
import { Drawer } from "../components/Drawer";
import { Empty, ErrorBox, Loading, PageTitle } from "../components/Empty";
import { JsonView } from "../components/JsonView";
import { DataTable } from "../components/Table";
import { useUIState } from "../state";
import { useHashSelection } from "../useHashSelection";
import type { Branch, Bucket, ProjectError, Table as TableT } from "../types";

type StorageTab = "buckets" | "tables" | "files";

const STORAGE_TABS: readonly StorageTab[] = ["buckets", "tables", "files"];

/**
 * This page's `?sel=` grammar: `<tab>`, `tables/<tableId>` or
 * `bucket/<bucketId>`.
 *
 * The tab is part of the selection because it is what the link has to restore
 * before anything can be opened -- the tables query is gated on it. Only the
 * tables tab has a detail view (the table drawer), so it is the only one that
 * carries a table id.
 *
 * The `bucket/` form encodes the bucket FILTER, which arrived with the command
 * palette: picking a bucket there is a NAVIGATION target ("show me this
 * bucket"), not the transient narrowing it is when you click a row on the way
 * down the list -- and a destination has to survive a reload and a paste into
 * chat. Both entry points write it, so the filter chip you see always matches
 * the URL you can copy.
 *
 * A table id already carries its bucket (`in.c-oltp.orders`), so an open
 * drawer needs no separate bucket part: `tables/<tableId>` wins over the
 * filter, and restoring it leaves the list unfiltered, exactly as a table
 * deep link behaved before the bucket form existed.
 */
export function parseStorageSel(sel: string | null): {
  tab: StorageTab;
  tableId: string | null;
  bucketId: string | null;
} {
  if (!sel) return { tab: "buckets", tableId: null, bucketId: null };
  const slash = sel.indexOf("/");
  const head = slash === -1 ? sel : sel.slice(0, slash);
  const rest = slash === -1 ? "" : sel.slice(slash + 1);
  if (head === "bucket") {
    // A bucket-less `bucket/` is meaningless; fall back to the plain list.
    return rest
      ? { tab: "tables", tableId: null, bucketId: rest }
      : { tab: "buckets", tableId: null, bucketId: null };
  }
  const tab = (STORAGE_TABS as readonly string[]).includes(head)
    ? (head as StorageTab)
    : "buckets";
  return { tab, tableId: tab === "tables" && rest ? rest : null, bucketId: null };
}

export function buildStorageSel(
  tab: StorageTab,
  tableId: string | null,
  bucketId: string | null = null,
): string | null {
  if (tab === "tables" && tableId) return `tables/${tableId}`;
  if (tab === "tables" && bucketId) return `bucket/${bucketId}`;
  // The landing view needs no `sel` at all -- keeps a plain project link clean.
  return tab === "buckets" ? null : tab;
}

interface TablePreview {
  header: string[];
  rows: string[][];
  row_count: number;
}

interface TableDetail {
  project_alias: string;
  table_id: string;
  name: string;
  bucket_id: string;
  backend: string;
  description: string;
  columns: string[];
  column_details: Array<{
    name: string;
    type?: string;
    native_type?: string;
    length?: string;
    nullable?: boolean;
    default?: string;
    description?: string;
  }>;
  primary_key: string[];
  rows_count: number;
  data_size_bytes: number;
  is_alias: boolean;
  last_import_date: string;
  last_change_date: string;
  created: string;
  metadata: Array<Record<string, unknown>>;
  /**
   * Raw Storage API `definition`, passed through verbatim (issue #621). On
   * BigQuery it is the ONLY readable record of the registered partition /
   * clustering layout. Present on EVERY response -- an untyped table gets one
   * too -- so `null` means the stack omitted the key, never "untyped".
   */
  definition?: {
    timePartitioning?: { type?: string; field?: string; expirationMs?: string | number } | null;
    rangePartitioning?: {
      field?: string;
      range?: { start?: string | number; end?: string | number; interval?: string | number };
    } | null;
    clustering?: { fields?: string[] } | null;
    requirePartitionFilter?: boolean | null;
    partitions?: Array<Record<string, unknown>> | null;
    [key: string]: unknown;
  } | null;
  legacy_column_descriptions?: string[];
}

interface BucketsResp {
  buckets: Bucket[];
  errors: ProjectError[];
}
interface TablesResp {
  tables: TableT[];
  errors: ProjectError[];
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

export function StoragePage() {
  const { project, branchId } = useUIState();
  // Deep link: `?sel=tables/<tableId>` restores the tab AND opens the drawer;
  // `?sel=bucket/<bucketId>` restores the tab AND the bucket filter.
  const [sel, setSel] = useHashSelection();
  const [tab, setTabState] = useState<StorageTab>(() => parseStorageSel(sel).tab);
  const [bucketFilter, setBucketFilter] = useState<string | null>(
    () => parseStorageSel(sel).bucketId,
  );
  const [selectedTable, setSelectedTable] = useState<TableT | null>(null);

  // Switching tabs drops the open table: the drawer belongs to the tables tab.
  // The bucket filter survives the trip (it is still shown as a chip), so it
  // stays in the URL whenever the tables tab is the one being shown.
  const setTab = (t: StorageTab) => {
    setTabState(t);
    setSelectedTable(null);
    setSel(buildStorageSel(t, null, bucketFilter));
  };
  const openTable = (t: TableT) => {
    setSelectedTable(t);
    setSel(buildStorageSel("tables", t.id));
  };
  const closeTable = () => {
    setSelectedTable(null);
    setSel(buildStorageSel(tab, null, bucketFilter));
  };
  // Narrowing to a bucket -- from a row click here or from the command
  // palette's deep link -- lands on the tables tab with the filter applied.
  // Written as one function because `setTab` would otherwise capture the
  // pre-update `bucketFilter` and drop it from the URL.
  const openBucket = (bucketId: string) => {
    setBucketFilter(bucketId);
    setTabState("tables");
    setSelectedTable(null);
    setSel(buildStorageSel("tables", null, bucketId));
  };
  const clearBucketFilter = () => {
    setBucketFilter(null);
    setSel(buildStorageSel(tab, null, null));
  };

  const bucketsQ = useQuery<BucketsResp>({
    queryKey: ["buckets", project, branchId],
    queryFn: () =>
      api.get("/storage/buckets", { query: { project: project ?? undefined, branch_id: branchId ?? undefined } }),
    enabled: !!project,
  });
  const tablesQ = useQuery<TablesResp>({
    queryKey: ["tables", project, bucketFilter, branchId],
    queryFn: () =>
      api.get("/storage/tables", {
        query: {
          project: project ?? undefined,
          bucket_id: bucketFilter ?? undefined,
          branch_id: branchId ?? undefined,
        },
      }),
    enabled: !!project && tab === "tables",
  });

  // Restore a deep-linked table ONCE, after the first tables load. The list is
  // unfiltered, so every table in the project is a candidate; a link to a table
  // that no longer exists simply leaves the list open.
  const restoredRef = useRef(false);
  useEffect(() => {
    if (restoredRef.current) return;
    const wanted = parseStorageSel(sel).tableId;
    if (!wanted) {
      restoredRef.current = true;
      return;
    }
    if (!tablesQ.data) return;
    restoredRef.current = true;
    const hit = tablesQ.data.tables.find((t) => t.id === wanted);
    if (hit) setSelectedTable(hit);
  }, [sel, tablesQ.data]);

  return (
    <div className="space-y-4">
      <PageTitle
        title="Storage"
        description={`Buckets, tables and files in ${project ?? "(no project)"}`}
      />
      <div className="flex gap-2">
        {STORAGE_TABS.map((t) => (
          <button
            key={t}
            type="button"
            className={`nerd-btn ${tab === t ? "border-keboola text-keboola" : ""}`}
            onClick={() => setTab(t)}
          >
            {t}
          </button>
        ))}
        {bucketFilter ? (
          <button
            type="button"
            className="nerd-btn text-xs hover:text-amber-700 dark:hover:text-amber-400"
            onClick={clearBucketFilter}
          >
            ✕ filter: {bucketFilter}
          </button>
        ) : null}
      </div>

      {!project ? (
        <Empty title="Select a project from the top bar" />
      ) : tab === "buckets" ? (
        bucketsQ.isLoading ? (
          <Loading />
        ) : bucketsQ.error ? (
          <ErrorBox message={(bucketsQ.error as Error).message} />
        ) : (
          <DataTable
            rows={bucketsQ.data?.buckets ?? []}
            rowKey={(b) => `${b.project_alias}/${b.id}`}
            onRowClick={(b) => openBucket(b.id)}
            columns={[
              {
                header: "Bucket",
                cell: (b) => (
                  <span className="font-bold text-accent">
                    {b.id} {b.is_linked ? <span className="nerd-pill-amber">linked</span> : null}
                  </span>
                ),
              },
              { header: "Stage", cell: (b) => <span className="text-zinc-600 dark:text-zinc-400">{b.stage}</span> },
              { header: "Backend", cell: (b) => <span className="text-zinc-600 dark:text-zinc-400">{b.backend}</span> },
              { header: "Rows", align: "right", cell: (b) => b.rows_count.toLocaleString() },
              { header: "Size", align: "right", cell: (b) => formatBytes(b.data_size_bytes) },
            ]}
          />
        )
      ) : tab === "tables" ? (
        tablesQ.isLoading ? (
          <Loading />
        ) : tablesQ.error ? (
          <ErrorBox message={(tablesQ.error as Error).message} />
        ) : (
          <DataTable
            rows={tablesQ.data?.tables ?? []}
            rowKey={(t) => `${t.project_alias}/${t.id}`}
            onRowClick={openTable}
            columns={[
              { header: "Table", cell: (t) => <span className="text-accent">{t.id}</span> },
              { header: "Name", cell: (t) => t.display_name },
              { header: "Bucket", cell: (t) => <span className="text-zinc-500">{t.bucket_id}</span> },
              { header: "Rows", align: "right", cell: (t) => t.rows_count.toLocaleString() },
              { header: "Size", align: "right", cell: (t) => formatBytes(t.data_size_bytes) },
              { header: "Last import", cell: (t) => <span className="text-zinc-500 text-xs">{t.last_import_date}</span> },
            ]}
          />
        )
      ) : (
        <FilesTab />
      )}

      <TableDetailDrawer table={selectedTable} onClose={closeTable} />
    </div>
  );
}

function TableDetailDrawer({
  table,
  onClose,
}: {
  table: TableT | null;
  onClose: () => void;
}) {
  const { branchId } = useUIState();
  const [tab, setTab] = useState<"info" | "schema" | "preview" | "raw" | "repartition">("info");

  const detailQ = useQuery<TableDetail>({
    queryKey: ["table-detail", table?.project_alias, table?.id, branchId],
    queryFn: () =>
      api.get(
        `/storage/table-detail/${encodeURIComponent(table!.project_alias)}/${encodeURIComponent(table!.id)}`,
        { query: { branch_id: branchId ?? undefined } },
      ),
    enabled: !!table,
  });

  const previewQ = useQuery<TablePreview>({
    queryKey: ["table-preview", table?.project_alias, table?.id],
    queryFn: () =>
      api.get(
        `/storage/table-preview/${encodeURIComponent(table!.project_alias)}/${encodeURIComponent(table!.id)}`,
        { query: { limit: 100 } },
      ),
    enabled: !!table && tab === "preview",
  });

  const downloadHref = table
    ? `/api/storage/table-download/${encodeURIComponent(table.project_alias)}/${encodeURIComponent(table.id)}${
        branchId ? `?branch_id=${branchId}` : ""
      }`
    : "#";

  return (
    <Drawer
      open={!!table}
      onClose={onClose}
      title={table?.id ?? ""}
      subtitle={
        table
          ? `${table.rows_count.toLocaleString()} rows ・ ${formatBytes(table.data_size_bytes)} ・ ${
              table.is_alias ? "alias" : "table"
            }`
          : undefined
      }
      width="max-w-5xl"
      actions={
        <a
          href={downloadHref}
          download
          className="nerd-btn flex items-center gap-1 hover:text-keboola"
        >
          <Download className="w-3 h-3" /> CSV
        </a>
      }
    >
      <div className="space-y-4">
        <div className="flex gap-2">
          {tabsFor(detailQ.data).map((t) => (
            <button
              key={t}
              type="button"
              className={`nerd-btn text-xs ${
                tab === t ? "border-keboola text-keboola" : ""
              }`}
              onClick={() => setTab(t)}
            >
              {t === "info" ? (
                <Info className="w-3 h-3 inline mr-1" />
              ) : t === "preview" ? (
                <Eye className="w-3 h-3 inline mr-1" />
              ) : t === "repartition" ? (
                <Layers className="w-3 h-3 inline mr-1" />
              ) : null}
              {t}
            </button>
          ))}
        </div>

        {detailQ.isLoading ? <Loading /> : null}
        {detailQ.error ? (
          <ErrorBox message={(detailQ.error as Error).message} />
        ) : null}

        {detailQ.data && tab === "info" ? <InfoTab d={detailQ.data} /> : null}
        {detailQ.data && tab === "schema" ? <SchemaTab d={detailQ.data} /> : null}
        {tab === "preview" ? (
          previewQ.isLoading ? (
            <Loading label="loading 100 rows..." />
          ) : previewQ.error ? (
            <ErrorBox message={(previewQ.error as Error).message} />
          ) : previewQ.data ? (
            <PreviewTab p={previewQ.data} />
          ) : null
        ) : null}
        {detailQ.data && tab === "raw" ? <JsonView data={detailQ.data} /> : null}
        {detailQ.data && tab === "repartition" ? (
          <RepartitionTab d={detailQ.data} onClose={onClose} />
        ) : null}
      </div>
    </Drawer>
  );
}

const REPARTITION_TAB = "repartition" as const;
type DrawerTab = "info" | "schema" | "preview" | "raw" | typeof REPARTITION_TAB;

// The repartition tab is BigQuery-only: partition/clustering layout changes are
// a BigQuery feature, and the server enforces this with a pre-flight backend
// guard anyway. Hide the tab elsewhere so the UI never offers an action that
// the backend will reject.
function tabsFor(d: TableDetail | undefined): DrawerTab[] {
  const base: DrawerTab[] = ["info", "schema", "preview", "raw"];
  if (d && d.backend.toLowerCase() === "bigquery" && !d.is_alias) {
    base.push(REPARTITION_TAB);
  }
  return base;
}

const repartInputCls =
  "w-full bg-transparent border border-zinc-200 dark:border-zinc-800 rounded px-2 py-1 text-xs text-zinc-700 dark:text-zinc-300 focus:outline-none focus:border-keboola disabled:opacity-50 disabled:cursor-not-allowed";

// BigQuery allows at most 4 clustering fields. We cap the picker so the server
// never has to reject an over-long list with a less obvious error.
const MAX_CLUSTERING_FIELDS = 4;

// Repartition a (BigQuery) table into a new partition/clustering layout.
//
// There is no in-place "ALTER TABLE ... PARTITION BY" on a populated table; the
// supported path is copy-into-new-layout then atomic swap:
//   1. create-table --source-table-id <orig> --time/range-partitioning ... --clustering
//      -> a sibling table (<name>_repartition) with the desired layout + copied rows
//   2. swap-tables <orig> <sibling>  -> the original id now exposes the new layout
//
// swap-tables is branch-scoped; the active branch from the top bar is used.
// Production (the default branch) is allowed and is the only branch whose swap
// reaches the live table -- a dev-branch swap never merges back -- so we run in
// production by default and gate it behind an explicit confirm.
//
// After the swap the OLD data/layout lives under the sibling id; we surface it
// and let the user delete it (per their choice) rather than dropping it silently.
function RepartitionTab({ d, onClose }: { d: TableDetail; onClose: () => void }) {
  const { project, branchId } = useUIState();
  const qc = useQueryClient();

  // swap-tables needs a concrete branch id. When no dev branch is pinned in the
  // top bar (branchId === null => production), resolve the project's default
  // branch id from /branches.
  const branchesQ = useQuery<{ branches: Branch[] }>({
    queryKey: ["branches", project],
    queryFn: () => api.get("/branches", { query: { project: project! } }),
    enabled: !!project && branchId === null,
  });
  const defaultBranch = branchesQ.data?.branches.find((b) => b.isDefault) ?? null;
  const effectiveBranchId = branchId ?? defaultBranch?.id ?? null;
  const isProduction = branchId === null;

  const [mode, setMode] = useState<"time" | "range" | "none">("time");
  const [timeType, setTimeType] = useState("DAY");
  const [timeField, setTimeField] = useState("");
  const [timeExpirationMs, setTimeExpirationMs] = useState("");
  const [rangeField, setRangeField] = useState("");
  const [rangeStart, setRangeStart] = useState("");
  const [rangeEnd, setRangeEnd] = useState("");
  const [rangeInterval, setRangeInterval] = useState("");
  const [clustering, setClustering] = useState<string[]>([]);
  const tempName = `${d.name}_repartition`;
  const tempTableId = `${d.bucket_id}.${tempName}`;

  const [confirmOpen, setConfirmOpen] = useState(false);
  const [phase, setPhase] = useState<"idle" | "creating" | "swapping" | "done">("idle");
  const [error, setError] = useState<string | null>(null);
  // True once the copy exists but the swap failed: the sibling table is left
  // behind, so we offer an explicit cleanup. (The copy itself is idempotent via
  // if_not_exists, so plain "Repartition" also safely retries just the swap.)
  const [swapFailed, setSwapFailed] = useState(false);

  const rangeComplete = !!(rangeField && rangeStart && rangeEnd && rangeInterval);
  // "none" => no partitioning (de-partition; clustering optional) and is always
  // valid on its own. Time needs a type; range needs all four bounds.
  const layoutValid = mode === "none" ? true : mode === "time" ? !!timeType : rangeComplete;
  const branchReady = effectiveBranchId !== null;
  const canSubmit = !!project && branchReady && layoutValid && phase === "idle";

  const toggleCluster = (col: string) =>
    setClustering((cur) => (cur.includes(col) ? cur.filter((c) => c !== col) : [...cur, col]));

  const run = useMutation({
    mutationFn: async () => {
      setError(null);
      setSwapFailed(false);
      // 1) Create the new-layout copy from the source (original) table.
      //    if_not_exists makes this idempotent: if a previous attempt already
      //    created the copy (e.g. the swap then failed), the retry skips the
      //    create and proceeds straight to the swap instead of erroring with
      //    "table already exists".
      setPhase("creating");
      const createBody: Record<string, unknown> = {
        bucket_id: d.bucket_id,
        name: tempName,
        source_table_id: d.table_id,
        branch_id: effectiveBranchId,
        if_not_exists: true,
        primary_key: d.primary_key.length ? d.primary_key : undefined,
        clustering_fields: clustering.length ? clustering : undefined,
      };
      if (mode === "time") {
        createBody.time_partitioning_type = timeType;
        if (timeField) createBody.time_partitioning_field = timeField;
        if (timeExpirationMs) createBody.time_partitioning_expiration_ms = timeExpirationMs;
      } else if (mode === "range") {
        createBody.range_partitioning_field = rangeField;
        createBody.range_partitioning_start = rangeStart;
        createBody.range_partitioning_end = rangeEnd;
        createBody.range_partitioning_interval = rangeInterval;
      }
      // mode === "none": no partitioning fields -- the copy is unpartitioned
      // (clustering, if any, still applies).
      await api.post(`/storage/tables/${encodeURIComponent(project!)}`, createBody);

      // 2) Swap the new-layout copy into the original's place. If this fails the
      //    copy is left behind, so flag it for the cleanup affordance.
      setPhase("swapping");
      try {
        await api.post(
          `/storage/tables/${encodeURIComponent(project!)}/${encodeURIComponent(d.table_id)}/swap`,
          { target_table_id: tempTableId, branch_id: effectiveBranchId },
        );
      } catch (e) {
        setSwapFailed(true);
        throw e;
      }
      setPhase("done");
    },
    onError: (e) => {
      setError(e instanceof ApiError ? e.message : String(e));
      setPhase("idle");
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tables"] });
      qc.invalidateQueries({ queryKey: ["table-detail"] });
    },
  });

  // After the swap the sibling id holds the OLD data/layout. Deleting it is the
  // user's call (they may want to verify the new table first).
  const del = useMutation({
    mutationFn: () =>
      api.delete(`/storage/tables/${encodeURIComponent(project!)}`, {
        query: {
          table_id: tempTableId,
          branch_id: effectiveBranchId ?? undefined,
          force: true,
        },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tables"] });
      onClose();
    },
  });

  // Cleanup after a failed swap: remove the leftover copy and return to the
  // form so the user can adjust and try again (does not close the drawer).
  const cleanup = useMutation({
    mutationFn: () =>
      api.delete(`/storage/tables/${encodeURIComponent(project!)}`, {
        query: {
          table_id: tempTableId,
          branch_id: effectiveBranchId ?? undefined,
          force: true,
        },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["tables"] });
      setSwapFailed(false);
      setError(null);
    },
  });

  const submit = () => {
    if (isProduction && !confirmOpen) {
      setConfirmOpen(true);
      return;
    }
    setConfirmOpen(false);
    run.mutate();
  };

  if (phase === "done") {
    return (
      <div className="space-y-4">
        <div className="flex items-start gap-2 rounded border border-green-300 dark:border-green-800 bg-green-50 dark:bg-green-950/30 px-3 py-2 text-sm text-green-800 dark:text-green-300">
          <Check className="w-4 h-4 mt-0.5 shrink-0" />
          <div>
            <div className="font-bold">Repartition complete.</div>
            <div className="text-xs mt-1">
              <span className="font-mono text-accent">{d.table_id}</span> now uses the new layout.
              The previous data &amp; layout are preserved under{" "}
              <span className="font-mono text-accent">{tempTableId}</span>.
            </div>
          </div>
        </div>
        {del.isError ? <ErrorBox message={(del.error as Error).message} /> : null}
        <div>
          <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-2">
            Delete the old table?
          </div>
          <div className="flex gap-2">
            <button
              type="button"
              className="nerd-btn text-xs flex items-center gap-1 hover:text-red-600 dark:hover:text-red-400"
              disabled={del.isPending}
              onClick={() => del.mutate()}
            >
              {del.isPending ? (
                <Loader2 className="w-3 h-3 animate-spin" />
              ) : (
                <Trash2 className="w-3 h-3" />
              )}
              Delete {tempName}
            </button>
            <button type="button" className="nerd-btn text-xs" onClick={onClose}>
              Keep it
            </button>
          </div>
        </div>
      </div>
    );
  }

  const busy = phase === "creating" || phase === "swapping";

  return (
    <div className="space-y-4">
      <div className="text-xs text-zinc-500">
        Copy <span className="font-mono text-accent">{d.table_id}</span> into a new
        partition/clustering layout, then atomically swap it into place.
      </div>

      {isProduction ? (
        <div className="flex items-start gap-2 rounded border border-amber-300 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/30 px-3 py-2 text-xs text-amber-800 dark:text-amber-300">
          <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
          <span>
            No dev branch is selected — this runs against{" "}
            <strong>production{defaultBranch ? ` (branch #${defaultBranch.id})` : ""}</strong> and
            changes the live table. A swap in a dev branch never merges back, so production is the
            only branch that actually repartitions the real table.
          </span>
        </div>
      ) : (
        <div className="text-[10px] uppercase tracking-wider text-zinc-500">
          Runs in branch #{effectiveBranchId}
        </div>
      )}

      {/* Partitioning mode */}
      <div className="space-y-2">
        <div className="text-[10px] uppercase tracking-wider text-zinc-500">Partitioning</div>
        <div className="flex gap-2">
          {(["time", "range", "none"] as const).map((m) => (
            <button
              key={m}
              type="button"
              className={`nerd-btn text-xs disabled:opacity-50 disabled:cursor-not-allowed ${
                mode === m ? "border-keboola text-keboola" : ""
              }`}
              disabled={busy}
              onClick={() => setMode(m)}
            >
              {m === "time" ? "Time" : m === "range" ? "Range (integer)" : "None"}
            </button>
          ))}
        </div>

        {mode === "none" ? (
          <div className="text-xs text-zinc-500">
            No partitioning — the table is copied unpartitioned (clustering below still
            applies). Use this to remove an existing partition layout.
          </div>
        ) : mode === "time" ? (
          <div className="grid grid-cols-3 gap-2">
            <label className="space-y-1">
              <span className="text-[10px] text-zinc-500">Type</span>
              <select
                className={repartInputCls}
                value={timeType}
                disabled={busy}
                onChange={(e) => setTimeType(e.target.value)}
              >
                {["DAY", "HOUR", "MONTH", "YEAR"].map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </label>
            <label className="space-y-1">
              <span className="text-[10px] text-zinc-500">Field (blank = ingestion time)</span>
              <select
                className={repartInputCls}
                value={timeField}
                disabled={busy}
                onChange={(e) => setTimeField(e.target.value)}
              >
                <option value="">(ingestion time)</option>
                {d.columns.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </label>
            <label className="space-y-1">
              <span className="text-[10px] text-zinc-500">Expiration ms (optional)</span>
              <input
                className={repartInputCls}
                value={timeExpirationMs}
                disabled={busy}
                onChange={(e) => setTimeExpirationMs(e.target.value)}
                placeholder="e.g. 7776000000"
              />
            </label>
          </div>
        ) : (
          <div className="grid grid-cols-4 gap-2">
            <label className="space-y-1">
              <span className="text-[10px] text-zinc-500">Field</span>
              <select
                className={repartInputCls}
                value={rangeField}
                disabled={busy}
                onChange={(e) => setRangeField(e.target.value)}
              >
                <option value="">(select)</option>
                {d.columns.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </label>
            <label className="space-y-1">
              <span className="text-[10px] text-zinc-500">Start</span>
              <input
                className={repartInputCls}
                value={rangeStart}
                disabled={busy}
                onChange={(e) => setRangeStart(e.target.value)}
                placeholder="0"
              />
            </label>
            <label className="space-y-1">
              <span className="text-[10px] text-zinc-500">End</span>
              <input
                className={repartInputCls}
                value={rangeEnd}
                disabled={busy}
                onChange={(e) => setRangeEnd(e.target.value)}
                placeholder="100000"
              />
            </label>
            <label className="space-y-1">
              <span className="text-[10px] text-zinc-500">Interval</span>
              <input
                className={repartInputCls}
                value={rangeInterval}
                disabled={busy}
                onChange={(e) => setRangeInterval(e.target.value)}
                placeholder="1000"
              />
            </label>
          </div>
        )}
      </div>

      {/* Clustering */}
      <div className="space-y-2">
        <div className="text-[10px] uppercase tracking-wider text-zinc-500">
          Clustering fields (optional, ordered by selection, max {MAX_CLUSTERING_FIELDS})
        </div>
        <div className="flex flex-wrap gap-1.5">
          {d.columns.map((c) => {
            const idx = clustering.indexOf(c);
            const on = idx !== -1;
            const atLimit = !on && clustering.length >= MAX_CLUSTERING_FIELDS;
            return (
              <button
                key={c}
                type="button"
                className={`nerd-btn text-xs disabled:opacity-50 disabled:cursor-not-allowed ${
                  on ? "border-keboola text-keboola" : ""
                }`}
                disabled={busy || atLimit}
                onClick={() => toggleCluster(c)}
              >
                {on ? `${idx + 1}. ` : ""}
                {c}
              </button>
            );
          })}
        </div>
      </div>

      {error ? <ErrorBox message={error} /> : null}

      {swapFailed ? (
        <div className="flex items-start gap-2 rounded border border-amber-300 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/30 px-3 py-2 text-xs text-amber-800 dark:text-amber-300">
          <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
          <div className="space-y-2">
            <div>
              The copy <span className="font-mono">{tempTableId}</span> was created but the swap
              failed. Click <strong>Repartition</strong> to retry just the swap, or delete the
              leftover copy.
            </div>
            <button
              type="button"
              className="nerd-btn text-xs flex items-center gap-1 hover:text-red-600 dark:hover:text-red-400"
              disabled={cleanup.isPending}
              onClick={() => cleanup.mutate()}
            >
              {cleanup.isPending ? (
                <Loader2 className="w-3 h-3 animate-spin" />
              ) : (
                <Trash2 className="w-3 h-3" />
              )}
              Delete leftover {tempName}
            </button>
          </div>
        </div>
      ) : null}

      {confirmOpen ? (
        <div className="flex items-start gap-2 rounded border border-amber-300 dark:border-amber-800 bg-amber-50 dark:bg-amber-950/30 px-3 py-2 text-xs text-amber-800 dark:text-amber-300">
          <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
          <div className="space-y-2">
            <div>
              This swaps the <strong>production</strong> table{" "}
              <span className="font-mono">{d.table_id}</span> into the new layout. Continue?
            </div>
            <div className="flex gap-2">
              <button
                type="button"
                className="nerd-btn text-xs border-keboola text-keboola"
                onClick={submit}
              >
                Yes, repartition production
              </button>
              <button type="button" className="nerd-btn text-xs" onClick={() => setConfirmOpen(false)}>
                Cancel
              </button>
            </div>
          </div>
        </div>
      ) : null}

      <div className="flex items-center gap-3 pt-1">
        <button
          type="button"
          className="nerd-btn text-xs flex items-center gap-1 hover:text-keboola disabled:opacity-50 disabled:cursor-not-allowed"
          disabled={!canSubmit}
          onClick={submit}
        >
          {busy ? <Loader2 className="w-3 h-3 animate-spin" /> : <Layers className="w-3 h-3" />}
          Repartition
        </button>
        <span className="text-[10px] text-zinc-500">
          {busy
            ? phase === "creating"
              ? `Creating ${tempName} (copying rows)...`
              : "Swapping into place..."
            : branchReady
              ? `Creates ${tempName}, then swaps it into ${d.name}.`
              : "Resolving branch..."}
        </span>
      </div>
    </div>
  );
}

function InfoTab({ d }: { d: TableDetail }) {
  return (
    <div className="space-y-3">
      <Field label="Bucket" value={d.bucket_id} mono />
      <Field label="Name" value={d.name} />
      {d.description ? <Field label="Description" value={d.description} /> : null}
      <div className="grid grid-cols-2 gap-3">
        <Field label="Rows" value={d.rows_count.toLocaleString()} />
        <Field label="Size" value={formatBytes(d.data_size_bytes)} />
        <Field
          label="Primary key"
          value={d.primary_key.length ? d.primary_key.join(", ") : "-"}
          mono
        />
        <Field label="Columns" value={String(d.columns.length)} />
        <Field label="Created" value={d.created} mono />
        <Field label="Last import" value={d.last_import_date || "-"} mono />
      </div>
      <TableLayout definition={d.definition} />
    </div>
  );
}

// Render the raw Storage API `definition` (issue #621). On BigQuery this object
// is the only readable record of the registered partition/clustering layout, so
// it is how a create-table + swap-tables repartition is VERIFIED -- the table id
// is unchanged either way. Every sub-key is optional, and a table with no layout
// at all must render NOTHING (not an empty heading), so each row is guarded and
// the section is dropped when none of them produced anything.
function TableLayout({ definition }: { definition: TableDetail["definition"] }) {
  if (!definition) return null;

  const rows: Array<{ label: string; value: string; mono?: boolean }> = [];

  const tp = definition.timePartitioning;
  if (tp) {
    const type = tp.type ?? "?";
    let value = tp.field ? `${type} on ${tp.field}` : `${type} (ingestion time)`;
    if (tp.expirationMs !== undefined && tp.expirationMs !== null && tp.expirationMs !== "") {
      value += ` ・ expires ${tp.expirationMs} ms`;
    }
    rows.push({ label: "Time partitioning", value, mono: true });
  }

  const rp = definition.rangePartitioning;
  if (rp) {
    const range = rp.range ?? {};
    const bounds = `[${range.start ?? "?"}, ${range.end ?? "?"})`;
    rows.push({
      label: "Range partitioning",
      value: `${rp.field ?? "?"} ${bounds} step ${range.interval ?? "?"}`,
      mono: true,
    });
  }

  const clusteringFields = definition.clustering?.fields;
  if (clusteringFields && clusteringFields.length > 0) {
    rows.push({ label: "Clustering", value: clusteringFields.join(", "), mono: true });
  }

  if (typeof definition.requirePartitionFilter === "boolean") {
    rows.push({
      label: "Partition filter required",
      value: definition.requirePartitionFilter ? "yes" : "no",
    });
  }

  // The COUNT only: `partitions` is unbounded (one entry per physical partition
  // from INFORMATION_SCHEMA.PARTITIONS) and must never be dumped into the grid.
  // Non-empty only, matching the CLI's `render_table_layout`: an empty list is
  // "no physical partitions reported", not a meaningful count of zero.
  if (definition.partitions && definition.partitions.length > 0) {
    rows.push({ label: "Partitions", value: definition.partitions.length.toLocaleString() });
  }

  if (rows.length === 0) return null;

  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">Table layout</div>
      <div className="grid grid-cols-2 gap-3">
        {rows.map((r) => (
          <Field key={r.label} label={r.label} value={r.value} mono={r.mono} />
        ))}
      </div>
    </div>
  );
}

function Field({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-zinc-500 mb-1">{label}</div>
      <div className={`text-sm ${mono ? "font-mono text-accent" : "text-zinc-800 dark:text-zinc-200"}`}>{value}</div>
    </div>
  );
}

interface DescribeVars {
  column: string;
  description: string;
  /** Override in effect before the optimistic write, for rollback on error. */
  previous?: string;
}

function SchemaTab({ d }: { d: TableDetail }) {
  const { project, branchId } = useUIState();
  const qc = useQueryClient();

  // Only one column is editable at a time; `overrides` is the optimistic layer
  // merged over the server's `c.description` until the refetch lands.
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [overrides, setOverrides] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);

  // Before 0.88.0 kbagent wrote a flat `KBC.column.{name}.description` key on the
  // TABLE's metadata -- read by nothing but kbagent, so a documented column still
  // looked blank in the UI, the MCP server and the warehouse. This route is the
  // NATIVE write (PUT .../tables/{id}/definition, isDescriptionSystemManaged:
  // false), which the backend mirrors into columnMetadata KBC.description -- so
  // everything downstream sees it, and the next Output Mapping run does not
  // overwrite it.
  const describe = useMutation({
    mutationFn: (vars: DescribeVars) =>
      api.post(
        `/storage/columns/${encodeURIComponent(project!)}/${encodeURIComponent(d.table_id)}/describe`,
        { columns: { [vars.column]: vars.description }, branch_id: branchId ?? undefined },
      ),
    onError: (e, vars) => {
      setError(e instanceof ApiError ? e.message : String(e));
      setOverrides((cur) => {
        const next = { ...cur };
        if (vars.previous === undefined) delete next[vars.column];
        else next[vars.column] = vars.previous;
        return next;
      });
    },
    onSuccess: async (_data, vars) => {
      // Drop the optimistic entry only AFTER the refetch lands, otherwise the
      // cell flashes the stale server description for a render. Leaving it in
      // place is worse still: it would mask every later server value for that
      // column for as long as the drawer stays mounted.
      await qc.invalidateQueries({ queryKey: ["table-detail"] });
      setOverrides((cur) => {
        const next = { ...cur };
        delete next[vars.column];
        return next;
      });
    },
  });

  const save = (column: string) => {
    const previous = overrides[column];
    setOverrides((cur) => ({ ...cur, [column]: draft }));
    setEditing(null);
    setError(null);
    describe.mutate({ column, description: draft, previous });
  };

  const legacyCount = d.legacy_column_descriptions?.length ?? 0;

  return (
    <div className="space-y-2">
      {legacyCount > 0 ? (
        <div className="flex items-center gap-1.5 text-xs text-amber-700 dark:text-neon-amber">
          <AlertTriangle className="w-3 h-3 shrink-0" />
          <span>
            {legacyCount} column(s) still carry a legacy description key — run{" "}
            <code className="px-1 py-0.5 rounded bg-zinc-100 border border-zinc-200 dark:bg-zinc-950 dark:border-zinc-800">
              kbagent storage describe-migrate
            </code>
            .
          </span>
        </div>
      ) : null}
      {error ? <ErrorBox message={error} /> : null}
      <div className="overflow-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-zinc-200 dark:border-zinc-800 text-zinc-500 uppercase tracking-wider">
              <th className="px-3 py-2 text-left font-normal">Column</th>
              <th className="px-3 py-2 text-left font-normal">Type</th>
              <th className="px-3 py-2 text-left font-normal">Native</th>
              <th className="px-3 py-2 text-left font-normal">Length</th>
              <th className="px-3 py-2 text-center font-normal">Null</th>
              <th className="px-3 py-2 text-center font-normal">PK</th>
              <th className="px-3 py-2 text-left font-normal">Default</th>
              <th className="px-3 py-2 text-left font-normal">Description</th>
            </tr>
          </thead>
          <tbody>
            {d.column_details.map((c) => {
              const current = overrides[c.name] ?? c.description ?? "";
              return (
                <tr
                  key={c.name}
                  className="group border-b border-zinc-200 dark:border-zinc-900/50"
                >
                  <td className="px-3 py-2 font-bold text-accent">{c.name}</td>
                  <td className="px-3 py-2 text-zinc-600 dark:text-zinc-400">{c.type ?? "-"}</td>
                  <td className="px-3 py-2 text-zinc-500">{c.native_type ?? "-"}</td>
                  <td className="px-3 py-2 text-zinc-500">{c.length ?? "-"}</td>
                  <td className="px-3 py-2 text-center">
                    {c.nullable === undefined ? "-" : c.nullable ? "✓" : ""}
                  </td>
                  <td className="px-3 py-2 text-center">
                    {d.primary_key.includes(c.name) ? "🔑" : ""}
                  </td>
                  <td className="px-3 py-2 text-zinc-500">{c.default ?? "-"}</td>
                  <td className="px-3 py-2 text-zinc-600 dark:text-zinc-400 text-xs">
                    {editing === c.name ? (
                      <div className="flex items-center gap-1">
                        <input
                          className={repartInputCls}
                          value={draft}
                          // Click-to-edit: put the caret straight in the cell the user just clicked.
                          autoFocus
                          placeholder="column description"
                          onChange={(e) => setDraft(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") {
                              e.preventDefault();
                              save(c.name);
                            } else if (e.key === "Escape") {
                              e.preventDefault();
                              setEditing(null);
                            }
                          }}
                        />
                        <button
                          type="button"
                          className="nerd-btn text-xs hover:text-keboola"
                          title="Save"
                          onClick={() => save(c.name)}
                        >
                          <Check className="w-3 h-3" />
                        </button>
                        <button
                          type="button"
                          className="nerd-btn text-xs"
                          title="Cancel"
                          onClick={() => setEditing(null)}
                        >
                          <X className="w-3 h-3" />
                        </button>
                      </div>
                    ) : (
                      <button
                        type="button"
                        className="w-full text-left flex items-center gap-1.5 disabled:cursor-not-allowed"
                        disabled={!project}
                        title={project ? "Edit description" : "Select a project to edit"}
                        onClick={() => {
                          setEditing(c.name);
                          setDraft(current);
                        }}
                      >
                        <span className={current ? "" : "text-zinc-400 dark:text-zinc-600"}>
                          {current || "—"}
                        </span>
                        {project ? (
                          <Pencil className="w-3 h-3 shrink-0 text-zinc-400 dark:text-zinc-600 opacity-0 group-hover:opacity-100" />
                        ) : null}
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function PreviewTab({ p }: { p: TablePreview }) {
  // Per-column filter inputs + sortable headers, mimicking the Keboola UI
  // Data Sample tab. State lives here so flipping tabs in the drawer
  // resets it -- intentional.
  const [filters, setFilters] = useState<string[]>(() => p.header.map(() => ""));
  const [sortCol, setSortCol] = useState<number | null>(null);
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");

  if (p.row_count === 0) {
    return <Empty title="Table is empty (no rows returned)" />;
  }

  // Filter -> sort -> render. Filters are case-insensitive substring matches
  // per column, ANDed together (so the filter row works like a query builder).
  let visible = p.rows.filter((row) =>
    filters.every((f, i) => !f || row[i]?.toLowerCase().includes(f.toLowerCase())),
  );
  if (sortCol !== null) {
    visible = [...visible].sort((a, b) => {
      const av = a[sortCol] ?? "";
      const bv = b[sortCol] ?? "";
      // Try numeric sort first; fall back to string compare.
      const an = Number(av);
      const bn = Number(bv);
      const numeric = !Number.isNaN(an) && !Number.isNaN(bn);
      const cmp = numeric ? an - bn : av.localeCompare(bv);
      return sortDir === "asc" ? cmp : -cmp;
    });
  }

  const toggleSort = (i: number) => {
    if (sortCol === i) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortCol(i);
      setSortDir("asc");
    }
  };

  return (
    <div className="space-y-2">
      <div className="text-xs text-zinc-500 flex justify-between items-center">
        <span>
          Showing {visible.length} of {p.row_count} row(s)
          {sortCol !== null ? (
            <span className="ml-2 text-zinc-500 dark:text-zinc-600">
              ・ sorted by {p.header[sortCol]} {sortDir === "asc" ? "↑" : "↓"}
            </span>
          ) : null}
        </span>
        {filters.some((f) => f) || sortCol !== null ? (
          <button
            type="button"
            className="text-xs text-zinc-500 hover:text-keboola"
            onClick={() => {
              setFilters(p.header.map(() => ""));
              setSortCol(null);
            }}
          >
            clear filters + sort
          </button>
        ) : null}
      </div>
      <div className="overflow-auto border border-zinc-200 dark:border-zinc-800 rounded">
        <table className="w-full text-xs font-mono">
          <thead className="bg-zinc-100 dark:bg-zinc-900/60 sticky top-0">
            <tr>
              {p.header.map((h, i) => (
                <th
                  key={i}
                  className="px-3 py-1.5 text-left text-keboola border-b border-zinc-200 dark:border-zinc-800 whitespace-nowrap cursor-pointer hover:bg-zinc-200 dark:hover:bg-zinc-900"
                  onClick={() => toggleSort(i)}
                >
                  <span className="inline-flex items-center gap-1">
                    {h}
                    <span className="text-zinc-500 dark:text-zinc-600 text-[10px]">
                      {sortCol === i ? (sortDir === "asc" ? "↑" : "↓") : "↕"}
                    </span>
                  </span>
                </th>
              ))}
            </tr>
            <tr className="bg-zinc-50 dark:bg-zinc-950">
              {p.header.map((_, i) => (
                <th
                  key={i}
                  className="px-2 py-1 border-b border-zinc-200 dark:border-zinc-800 font-normal"
                >
                  <input
                    type="text"
                    placeholder="filter..."
                    value={filters[i]}
                    onChange={(e) => {
                      const next = [...filters];
                      next[i] = e.target.value;
                      setFilters(next);
                    }}
                    className="w-full bg-transparent border border-zinc-200 dark:border-zinc-800 rounded px-1 py-0.5 text-[10px] text-zinc-700 dark:text-zinc-300 focus:outline-none focus:border-keboola"
                  />
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visible.map((r, i) => (
              <tr key={i} className="border-b border-zinc-200 dark:border-zinc-900/40 hover:bg-zinc-100 dark:hover:bg-zinc-900/30">
                {r.map((c, j) => (
                  <td key={j} className="px-3 py-1 text-zinc-700 dark:text-zinc-300 whitespace-nowrap">
                    {c}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        {visible.length === 0 ? (
          <div className="text-center text-xs text-zinc-500 py-6">
            No rows match the active filters.
          </div>
        ) : null}
      </div>
    </div>
  );
}

function FilesTab() {
  const { project } = useUIState();
  const q = useQuery<{ files: Array<Record<string, unknown>>; total?: number }>({
    queryKey: ["files", project],
    queryFn: () => api.get("/storage/files", { query: { project: project ?? undefined, limit: 50 } }),
    enabled: !!project,
  });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox message={(q.error as Error).message} />;
  const files = q.data?.files ?? [];
  if (files.length === 0) return <Empty title="No files in this project" />;
  return (
    <DataTable
      rows={files}
      rowKey={(f) => String(f.id)}
      columns={[
        { header: "ID", cell: (f) => <span className="text-zinc-500">{String(f.id)}</span> },
        { header: "Name", cell: (f) => <span>{String(f.name ?? "")}</span> },
        { header: "Size", align: "right", cell: (f) => String(f.sizeBytes ?? f.size_bytes ?? "") },
        { header: "Tags", cell: (f) => <span className="text-xs text-zinc-500">{(f.tags as string[] | undefined)?.join(", ") ?? ""}</span> },
        { header: "Created", cell: (f) => <span className="text-xs text-zinc-500">{String(f.created ?? "")}</span> },
      ]}
    />
  );
}
