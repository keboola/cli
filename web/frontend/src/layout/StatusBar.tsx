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
    <footer className="h-6 border-t border-zinc-200 bg-white/80 backdrop-blur px-4 flex items-center text-[10px] text-zinc-500 gap-4 dark:border-zinc-900 dark:bg-zinc-950/60 dark:text-zinc-600">
      <span>kbagent serve {v?.version ?? "…"}</span>
      {v && !v.up_to_date ? (
        <span className="text-amber-700 dark:text-neon-amber">
          ⬆ {v.latest_version} available
        </span>
      ) : null}
      <span className="text-zinc-600 dark:text-zinc-500">
        <kbd className="font-mono">ctrl+k</kbd> — command palette
      </span>
      <span className="ml-auto">localhost only ・ bearer auth ・ kernel: python ・ ui: typescript</span>
    </footer>
  );
}
