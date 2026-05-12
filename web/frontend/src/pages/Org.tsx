import { useMutation } from "@tanstack/react-query";
import { Layers } from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import { ManageTokenModal } from "../components/ManageTokenModal";
import { ErrorBox, PageTitle } from "../components/Empty";
import { JsonView } from "../components/JsonView";

export function OrgPage() {
  const [stackUrl, setStackUrl] = useState("https://connection.keboola.com");
  const [orgId, setOrgId] = useState("");
  const [projectIds, setProjectIds] = useState("");
  const [dryRun, setDryRun] = useState(true);
  const [tokenPrompt, setTokenPrompt] = useState(false);
  const [result, setResult] = useState<unknown | null>(null);

  const mu = useMutation({
    mutationFn: ({ manageToken }: { manageToken: string }) =>
      api.post(
        "/org/setup",
        {
          stack_url: stackUrl,
          org_id: orgId ? Number(orgId) : null,
          project_ids: projectIds
            ? projectIds
                .split(",")
                .map((s) => Number(s.trim()))
                .filter(Boolean)
            : null,
          dry_run: dryRun,
        },
        { manageToken },
      ),
    onSuccess: (data) => setResult(data),
  });

  return (
    <div className="space-y-4">
      <PageTitle
        title="Organization setup"
        description="Bulk-register every project in an org -- requires a manage token / PAT."
      />
      <form
        className="nerd-card space-y-3"
        onSubmit={(e) => {
          e.preventDefault();
          setTokenPrompt(true);
        }}
      >
        <div className="grid grid-cols-2 gap-3">
          <label className="text-xs text-zinc-400">
            Stack URL
            <input
              className="nerd-input w-full mt-1"
              value={stackUrl}
              onChange={(e) => setStackUrl(e.target.value)}
            />
          </label>
          <label className="text-xs text-zinc-400">
            Org ID (org-admin mode)
            <input
              className="nerd-input w-full mt-1"
              value={orgId}
              onChange={(e) => setOrgId(e.target.value)}
              placeholder="12345"
            />
          </label>
        </div>
        <label className="text-xs text-zinc-400 block">
          OR Project IDs (PAT mode, comma-separated)
          <input
            className="nerd-input w-full mt-1"
            value={projectIds}
            onChange={(e) => setProjectIds(e.target.value)}
            placeholder="123, 456, 789"
          />
        </label>
        <label className="flex items-center gap-2 text-xs text-zinc-400">
          <input type="checkbox" checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} />
          Dry run (preview without applying)
        </label>
        <div className="flex gap-2">
          <button type="submit" className="nerd-btn flex items-center gap-1 hover:text-keboola">
            <Layers className="w-3 h-3" /> {mu.isPending ? "running..." : "Run setup"}
          </button>
        </div>
      </form>
      {mu.error ? <ErrorBox message={(mu.error as Error).message} /> : null}
      {result ? (
        <div className="nerd-card">
          <h3 className="font-bold text-keboola mb-2">Result</h3>
          <JsonView data={result} />
        </div>
      ) : null}
      {tokenPrompt ? (
        <ManageTokenModal
          reason="Org setup needs a Keboola manage token (or PAT) to enumerate projects and create storage tokens."
          onCancel={() => setTokenPrompt(false)}
          onSubmit={(t) => {
            setTokenPrompt(false);
            mu.mutate({ manageToken: t });
          }}
        />
      ) : null}
    </div>
  );
}
