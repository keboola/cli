import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

interface VersionResp {
  kbagent: { version: string; latest_version: string; up_to_date: boolean };
}

export function StatusBar() {
  const versionQ = useQuery<VersionResp>({
    queryKey: ["version"],
    queryFn: () => api.get<VersionResp>("/version"),
    staleTime: 5 * 60_000,
  });
  const v = versionQ.data?.kbagent;
  return (
    <footer className="h-6 border-t border-zinc-900 bg-zinc-950/60 backdrop-blur px-4 flex items-center text-[10px] text-zinc-600 gap-4">
      <span>kbagent serve {v?.version ?? "…"}</span>
      {v && !v.up_to_date ? (
        <span className="text-neon-amber">⬆ {v.latest_version} available</span>
      ) : null}
      <span className="ml-auto">localhost only ・ bearer auth ・ kernel: python ・ ui: typescript</span>
    </footer>
  );
}
