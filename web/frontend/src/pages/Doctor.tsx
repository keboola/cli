import { useQuery } from "@tanstack/react-query";
import { CheckCircle, RefreshCw, XCircle } from "lucide-react";
import { api } from "../api/client";
import { ErrorBox, Loading, PageTitle } from "../components/Empty";
import { JsonView } from "../components/JsonView";
import type { DoctorCheck } from "../types";

interface DoctorResp {
  checks: DoctorCheck[];
  summary: { total: number; passed: number; failed: number; warnings?: number };
}

export function DoctorPage() {
  const q = useQuery<DoctorResp>({
    queryKey: ["doctor"],
    queryFn: () => api.get("/doctor"),
  });
  return (
    <div className="space-y-4">
      <PageTitle
        title="kbagent doctor"
        description="Health checks: config, connectivity, version, plugins, conversation."
        actions={
          <button type="button" className="nerd-btn flex items-center gap-1" onClick={() => q.refetch()}>
            <RefreshCw className="w-3 h-3" /> Re-run
          </button>
        }
      />
      {q.isLoading ? <Loading /> : null}
      {q.error ? <ErrorBox message={(q.error as Error).message} /> : null}
      {q.data ? (
        <>
          <div className="grid grid-cols-4 gap-3">
            <Stat label="Total" value={q.data.summary.total} />
            <Stat label="Passed" value={q.data.summary.passed} color="text-keboola" />
            <Stat label="Failed" value={q.data.summary.failed} color="text-red-400" />
            <Stat label="Warnings" value={q.data.summary.warnings ?? 0} color="text-neon-amber" />
          </div>
          <div className="space-y-2">
            {q.data.checks.map((c) => (
              <div key={c.name} className="nerd-card flex items-start gap-3">
                <div className="mt-0.5">
                  {c.status === "pass" ? (
                    <CheckCircle className="w-4 h-4 text-keboola" />
                  ) : c.status === "fail" ? (
                    <XCircle className="w-4 h-4 text-red-400" />
                  ) : (
                    <CheckCircle className="w-4 h-4 text-neon-amber" />
                  )}
                </div>
                <div className="flex-1">
                  <div className="font-bold">{c.name}</div>
                  <div className="text-xs text-zinc-400">{c.message}</div>
                  {c.details ? (
                    <details className="mt-2">
                      <summary className="text-xs text-zinc-500 cursor-pointer">details</summary>
                      <JsonView data={c.details} maxHeight="200px" />
                    </details>
                  ) : null}
                </div>
              </div>
            ))}
          </div>
        </>
      ) : null}
    </div>
  );
}

function Stat({ label, value, color = "text-zinc-100" }: { label: string; value: number; color?: string }) {
  return (
    <div className="nerd-card">
      <div className="text-xs text-zinc-500 uppercase tracking-wider">{label}</div>
      <div className={`text-3xl font-bold mt-1 ${color}`}>{value}</div>
    </div>
  );
}
