import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { ErrorBox, Loading, PageTitle } from "../components/Empty";

interface ChangelogResp {
  entries: Array<{ version: string; highlights: string[] }>;
}

export function ChangelogPage() {
  const q = useQuery<ChangelogResp>({
    queryKey: ["changelog"],
    queryFn: () => api.get("/changelog"),
  });
  return (
    <div className="space-y-4">
      <PageTitle title="Changelog" description="Release history of the kbagent kernel." />
      {q.isLoading ? <Loading /> : null}
      {q.error ? <ErrorBox message={(q.error as Error).message} /> : null}
      <div className="space-y-3">
        {(q.data?.entries ?? []).map((e) => (
          <div key={e.version} className="nerd-card">
            <h3 className="font-bold text-keboola">v{e.version}</h3>
            <ul className="mt-2 text-sm space-y-1">
              {e.highlights.map((h, i) => (
                <li key={i} className="text-zinc-300">
                  • {h}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </div>
  );
}
