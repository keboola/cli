import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { ErrorBox, Loading, PageTitle } from "../components/Empty";
import { DataTable } from "../components/Table";
import type { ProjectError, SharedBucket } from "../types";

interface SharedResp {
  shared_buckets: Array<SharedBucket & { project_alias?: string; bucket_id?: string; bucket_name?: string }>;
  errors: ProjectError[];
}

export function SharingPage() {
  const q = useQuery<SharedResp>({
    queryKey: ["sharing-all"],
    queryFn: () => api.get("/sharing"),
  });
  return (
    <div className="space-y-4">
      <PageTitle title="Bucket sharing" description="Buckets shared across the org or to specific projects/users." />
      {q.isLoading ? <Loading /> : null}
      {q.error ? <ErrorBox message={(q.error as Error).message} /> : null}
      <DataTable
        rows={q.data?.shared_buckets ?? []}
        rowKey={(b) => `${b.source_project_id}/${b.source_bucket_id}`}
        columns={[
          { header: "Source project", cell: (b) => <span className="text-keboola">{b.source_project_id}</span> },
          { header: "Bucket", cell: (b) => <span className="text-accent">{b.source_bucket_id}</span> },
          { header: "Name", cell: (b) => <span>{b.source_bucket_name}</span> },
          { header: "Type", cell: (b) => <span className="nerd-pill">{b.sharing_type}</span> },
        ]}
      />
    </div>
  );
}
