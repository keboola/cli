import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { Empty, ErrorBox, Loading, PageTitle } from "../components/Empty";
import { JsonView } from "../components/JsonView";
import { DataTable } from "../components/Table";
import { useUIState } from "../state";
import type { Bucket, ProjectError, Table as TableT } from "../types";

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
  const [tab, setTab] = useState<"buckets" | "tables" | "files">("buckets");
  const [bucketFilter, setBucketFilter] = useState<string | null>(null);
  const [selectedTable, setSelectedTable] = useState<TableT | null>(null);

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

  return (
    <div className="space-y-4">
      <PageTitle
        title="Storage"
        description={`Buckets, tables and files in ${project ?? "(no project)"}`}
      />
      <div className="flex gap-2">
        {(["buckets", "tables", "files"] as const).map((t) => (
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
            className="nerd-btn text-xs hover:text-amber-400"
            onClick={() => setBucketFilter(null)}
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
            onRowClick={(b) => {
              setBucketFilter(b.id);
              setTab("tables");
            }}
            columns={[
              {
                header: "Bucket",
                cell: (b) => (
                  <span className="font-bold text-accent">
                    {b.id} {b.is_linked ? <span className="nerd-pill-amber">linked</span> : null}
                  </span>
                ),
              },
              { header: "Stage", cell: (b) => <span className="text-zinc-400">{b.stage}</span> },
              { header: "Backend", cell: (b) => <span className="text-zinc-400">{b.backend}</span> },
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
            onRowClick={(t) => setSelectedTable(t)}
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

      {selectedTable ? (
        <TableDetail
          table={selectedTable}
          onClose={() => setSelectedTable(null)}
        />
      ) : null}
    </div>
  );
}

function TableDetail({ table, onClose }: { table: TableT; onClose: () => void }) {
  const { branchId } = useUIState();
  const q = useQuery({
    queryKey: ["table-detail", table.project_alias, table.id, branchId],
    queryFn: () =>
      api.get(`/storage/tables/${encodeURIComponent(table.project_alias)}/${encodeURIComponent(table.id)}`, {
        query: { branch_id: branchId ?? undefined },
      }),
  });
  return (
    <div className="nerd-card">
      <div className="flex items-center justify-between mb-3">
        <h3 className="font-bold text-keboola">{table.id}</h3>
        <button type="button" className="nerd-btn text-xs" onClick={onClose}>
          Close
        </button>
      </div>
      {q.isLoading ? <Loading /> : null}
      {q.data ? <JsonView data={q.data} /> : null}
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
