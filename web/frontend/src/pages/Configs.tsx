import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api/client";
import { Empty, ErrorBox, Loading, PageTitle } from "../components/Empty";
import { JsonView } from "../components/JsonView";
import { DataTable } from "../components/Table";
import { useUIState } from "../state";
import type { ConfigSummary, ProjectError } from "../types";

interface ConfigsResp {
  configs: ConfigSummary[];
  errors: ProjectError[];
}

export function ConfigsPage() {
  const { project, branchId } = useUIState();
  const [filterText, setFilterText] = useState("");
  const [selected, setSelected] = useState<ConfigSummary | null>(null);

  const q = useQuery<ConfigsResp>({
    queryKey: ["configs", project, branchId],
    queryFn: () =>
      api.get("/configs", {
        query: { project: project ?? undefined, branch_id: branchId ?? undefined },
      }),
    enabled: !!project,
  });

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
      <input
        className="nerd-input w-full max-w-md"
        placeholder="filter by name / id / component..."
        value={filterText}
        onChange={(e) => setFilterText(e.target.value)}
      />
      {!project ? (
        <Empty title="Select a project from the top bar" />
      ) : q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox message={(q.error as Error).message} />
      ) : (
        <>
          {q.data?.errors.length ? (
            <div className="text-amber-400 text-xs">
              {q.data.errors.length} project error(s) -- some configs may be missing.
            </div>
          ) : null}
          <DataTable
            rows={filtered}
            rowKey={(c) => `${c.project_alias}/${c.component_id}/${c.config_id}`}
            onRowClick={(c) => setSelected(c)}
            columns={[
              { header: "Component", cell: (c) => <span className="text-accent">{c.component_id}</span> },
              { header: "Config ID", cell: (c) => <span className="text-zinc-400">{c.config_id}</span> },
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
          onClose={() => setSelected(null)}
        />
      ) : null}
    </div>
  );
}

function ConfigDetail({
  alias,
  componentId,
  configId,
  onClose,
}: {
  alias: string;
  componentId: string;
  configId: string;
  onClose: () => void;
}) {
  const { branchId } = useUIState();
  const detailQ = useQuery({
    queryKey: ["config-detail", alias, componentId, configId, branchId],
    queryFn: () =>
      api.get(
        `/configs/${encodeURIComponent(alias)}/${encodeURIComponent(componentId)}/${encodeURIComponent(configId)}`,
        { query: { branch_id: branchId ?? undefined } },
      ),
  });
  return (
    <div className="nerd-card">
      <div className="flex items-center justify-between mb-3">
        <h3 className="font-bold text-keboola">
          {componentId} / {configId}
        </h3>
        <button type="button" className="nerd-btn text-xs" onClick={onClose}>
          Close
        </button>
      </div>
      {detailQ.isLoading ? <Loading /> : null}
      {detailQ.error ? <ErrorBox message={(detailQ.error as Error).message} /> : null}
      {detailQ.data ? <JsonView data={detailQ.data} /> : null}
    </div>
  );
}
