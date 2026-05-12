import {
  Activity,
  Bot,
  Boxes,
  Braces,
  Calendar,
  Compass,
  Cpu,
  Database,
  GitBranch,
  Heart,
  Layers,
  Lock,
  MessageSquare,
  Network,
  PackageSearch,
  PlayCircle,
  Search,
  Sparkles,
  Terminal,
  Workflow,
} from "lucide-react";
import { clsx } from "clsx";
import { type PageId, useUIState } from "../state";

const SECTIONS: Array<{
  title: string;
  items: Array<{ id: PageId; label: string; icon: React.ComponentType<{ className?: string }> }>;
}> = [
  {
    title: "Manage",
    items: [
      { id: "projects", label: "Projects", icon: Boxes },
      { id: "branches", label: "Branches", icon: GitBranch },
      { id: "doctor", label: "Doctor", icon: Heart },
      { id: "changelog", label: "Changelog", icon: Activity },
    ],
  },
  {
    title: "Browse",
    items: [
      { id: "configs", label: "Configs", icon: Braces },
      { id: "components", label: "Components", icon: PackageSearch },
      { id: "storage", label: "Storage", icon: Database },
      { id: "jobs", label: "Jobs", icon: PlayCircle },
      { id: "search", label: "Search", icon: Search },
    ],
  },
  {
    title: "Develop",
    items: [
      { id: "workspaces", label: "SQL Workspaces", icon: Terminal },
      { id: "flows", label: "Flows", icon: Workflow },
      { id: "schedules", label: "Schedules", icon: Calendar },
      { id: "data-apps", label: "Data Apps", icon: Cpu },
    ],
  },
  {
    title: "Insights",
    items: [
      // Lineage page has both "Sharing graph" + "Deep lineage" tabs --
      // the standalone Sharing entry would be a duplicate.
      { id: "lineage", label: "Lineage", icon: Network },
    ],
  },
  {
    title: "AI / Tools",
    items: [
      { id: "mcp", label: "MCP Tools", icon: Sparkles },
      { id: "kai", label: "Kai Chat", icon: MessageSquare },
      { id: "agents", label: "Agent Tasks", icon: Bot },
    ],
  },
  {
    title: "Admin",
    items: [
      { id: "org", label: "Org Setup", icon: Layers },
      { id: "members", label: "Members", icon: Compass },
      { id: "encrypt", label: "Encrypt", icon: Lock },
    ],
  },
];

export function Sidebar() {
  const { page, setPage } = useUIState();
  return (
    <aside className="w-56 shrink-0 border-r border-zinc-900 bg-zinc-950/60 backdrop-blur p-3 overflow-y-auto">
      <div className="px-2 py-3 mb-3 border-b border-zinc-900">
        <div className="flex items-center gap-2">
          <div className="w-2 h-2 rounded-full bg-keboola animate-pulse" />
          <span className="text-keboola font-bold tracking-wider text-sm">kbagent</span>
          <span className="text-zinc-600 text-xs">// NERD UI</span>
        </div>
        <div className="text-zinc-500 text-[10px] mt-1">Keboola kernel + UI</div>
      </div>
      <nav className="space-y-4">
        {SECTIONS.map((section) => (
          <div key={section.title}>
            <div className="text-[10px] uppercase tracking-widest text-zinc-600 px-2 mb-1">
              {section.title}
            </div>
            <ul className="space-y-0.5">
              {section.items.map((item) => {
                const Icon = item.icon;
                const active = page === item.id;
                return (
                  <li key={item.id}>
                    <button
                      type="button"
                      onClick={() => setPage(item.id)}
                      className={clsx(
                        "w-full flex items-center gap-2 px-2 py-1.5 rounded text-left text-sm transition-colors",
                        active
                          ? "bg-keboola/10 text-keboola border border-keboola/30"
                          : "text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100 border border-transparent",
                      )}
                    >
                      <Icon className="w-3.5 h-3.5" />
                      <span>{item.label}</span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </nav>
    </aside>
  );
}
