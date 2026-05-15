import { useQuery } from "@tanstack/react-query";
import { ChevronDown, GitBranch, Layers, Moon, Search, Server, Sun } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api/client";
import { useUIState } from "../state";
import { useTheme } from "../theme";
import type { Branch, Project } from "../types";

interface ProjectsResponse {
  projects: Project[];
}
interface BranchesResponse {
  branches: Branch[];
  active_branches: Record<string, number | null>;
}

export function TopBar() {
  const { project, setProject, branchId, setBranchId } = useUIState();
  const projectsQ = useQuery<ProjectsResponse>({
    queryKey: ["projects"],
    queryFn: () => api.get<ProjectsResponse>("/projects"),
  });
  const branchesQ = useQuery<BranchesResponse>({
    queryKey: ["branches", project],
    queryFn: () => api.get<BranchesResponse>("/branches", { query: { project: project! } }),
    enabled: !!project,
  });

  // Auto-select default project on load
  useEffect(() => {
    if (project) return;
    const projects = projectsQ.data?.projects;
    if (!projects?.length) return;
    const def = projects.find((p) => p.is_default) ?? projects[0];
    setProject(def.alias);
  }, [project, projectsQ.data, setProject]);

  const branches = branchesQ.data?.branches ?? [];
  const branchLabel = branchId
    ? branches.find((b) => b.id === branchId)?.name ?? `#${branchId}`
    : "main";

  return (
    <header className="relative z-40 border-b border-zinc-200 bg-white/80 backdrop-blur px-4 h-12 flex items-center gap-4 dark:border-zinc-900 dark:bg-zinc-950/40">
      <ProjectPicker
        projects={projectsQ.data?.projects ?? []}
        current={project}
        onChange={(p) => {
          setProject(p);
          setBranchId(null);
        }}
      />
      {project ? (
        <BranchPicker
          branches={branches}
          current={branchId}
          label={branchLabel}
          loading={branchesQ.isFetching}
          onChange={setBranchId}
        />
      ) : null}
      <div className="ml-auto flex items-center gap-3 text-xs text-zinc-500">
        <ThemeToggle />
        <span className="flex items-center gap-1" title="Connected to kbagent serve via the local Node BFF (web/backend)">
          <Server className="w-3 h-3 text-keboola" /> kbagent serve
        </span>
      </div>
    </header>
  );
}

function ThemeToggle() {
  const { theme, toggle } = useTheme();
  const isDark = theme === "dark";
  return (
    <button
      type="button"
      onClick={toggle}
      title={`Switch to ${isDark ? "light" : "dark"} mode`}
      aria-label="Toggle color scheme"
      className="flex items-center gap-1 px-2 py-1 rounded border border-zinc-300 text-zinc-700 hover:border-keboola hover:text-keboola dark:border-zinc-700 dark:text-zinc-400"
    >
      {isDark ? <Sun className="w-3.5 h-3.5" /> : <Moon className="w-3.5 h-3.5" />}
      <span className="text-[10px] uppercase tracking-wider">{isDark ? "light" : "dark"}</span>
    </button>
  );
}

function ProjectPicker({
  projects,
  current,
  onChange,
}: {
  projects: Project[];
  current: string | null;
  onChange: (p: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const ref = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", onClick);
    return () => window.removeEventListener("mousedown", onClick);
  }, [open]);

  // Reset query when closing and autofocus the search field when opening.
  useEffect(() => {
    if (open) {
      // Defer one tick: the input is mounted in the same render and
      // focusing it before paint occasionally drops on Safari/Firefox.
      const t = setTimeout(() => searchRef.current?.focus(), 0);
      return () => clearTimeout(t);
    }
    setQuery("");
  }, [open]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return projects;
    return projects.filter((p) => {
      const haystack = `${p.alias} ${p.project_name} ${p.org_name ?? ""}`.toLowerCase();
      return haystack.includes(q);
    });
  }, [projects, query]);

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1.5 text-sm hover:text-keboola px-2 py-1 rounded border border-zinc-300 hover:border-keboola/40 dark:border-zinc-800"
      >
        <Layers className="w-3.5 h-3.5" />
        <span>{current ?? "select project"}</span>
        <ChevronDown className="w-3 h-3 opacity-60" />
      </button>
      {open ? (
        <div className="absolute top-full left-0 mt-1 z-50 w-96 max-h-[28rem] flex flex-col rounded border border-zinc-200 bg-white shadow-xl dark:border-zinc-800 dark:bg-zinc-950">
          {projects.length > 5 ? (
            <div className="flex items-center gap-2 px-2 py-2 border-b border-zinc-200 dark:border-zinc-800">
              <Search className="w-3.5 h-3.5 text-zinc-400 flex-shrink-0" />
              <input
                ref={searchRef}
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Escape") {
                    if (query) setQuery("");
                    else setOpen(false);
                  } else if (e.key === "Enter" && filtered.length === 1) {
                    onChange(filtered[0].alias);
                    setOpen(false);
                  }
                }}
                placeholder={`filter ${projects.length} projects (alias, name, org)`}
                className="flex-1 bg-transparent text-sm focus:outline-none placeholder-zinc-400 dark:placeholder-zinc-600"
              />
              {query ? (
                <span className="text-[10px] text-zinc-500 flex-shrink-0">
                  {filtered.length}/{projects.length}
                </span>
              ) : null}
            </div>
          ) : null}
          <div className="overflow-y-auto">
            {projects.length === 0 ? (
              <div className="px-3 py-4 text-xs text-zinc-500">
                No projects. Add one via the Projects page.
              </div>
            ) : filtered.length === 0 ? (
              <div className="px-3 py-4 text-xs text-zinc-500">
                No project matches “{query}”.
              </div>
            ) : (
              filtered.map((p) => (
                <button
                  key={p.alias}
                  type="button"
                  onClick={() => {
                    onChange(p.alias);
                    setOpen(false);
                  }}
                  className={`w-full text-left px-3 py-2 text-sm flex items-center justify-between gap-2 hover:bg-zinc-100 dark:hover:bg-zinc-900 ${
                    p.alias === current ? "text-keboola" : "text-zinc-700 dark:text-zinc-300"
                  }`}
                >
                  <div className="min-w-0 flex-1">
                    <div className="font-medium truncate">{p.alias}</div>
                    <div className="text-xs text-zinc-500 truncate">
                      {p.project_name}
                      {p.org_name ? (
                        <span className="text-zinc-400"> · {p.org_name}</span>
                      ) : p.org_id != null ? (
                        // Org name unknown but id is, surface it so users
                        // with multiple orgs can still tell projects apart.
                        <span className="text-zinc-400"> · org #{p.org_id}</span>
                      ) : null}
                    </div>
                  </div>
                  {p.is_default ? (
                    <span className="nerd-pill-green flex-shrink-0">default</span>
                  ) : null}
                </button>
              ))
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function BranchPicker({
  branches,
  current,
  label,
  loading,
  onChange,
}: {
  branches: Branch[];
  current: number | null;
  label: string;
  loading: boolean;
  onChange: (b: number | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", onClick);
    return () => window.removeEventListener("mousedown", onClick);
  }, [open]);
  // When a non-default branch is selected, the indicator goes neon-amber so
  // the user can never miss that they're not on production.
  const isDevBranch = current !== null;
  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className={`flex items-center gap-1.5 text-sm px-2.5 py-1 rounded border ${
          isDevBranch
            ? "border-neon-amber/60 bg-neon-amber/10 text-amber-700 hover:bg-neon-amber/20 dark:text-neon-amber"
            : "border-zinc-300 text-zinc-600 hover:text-keboola hover:border-keboola/40 dark:border-zinc-800 dark:text-zinc-400"
        }`}
      >
        <GitBranch className="w-3.5 h-3.5" />
        <span className="font-medium">{label}</span>
        {isDevBranch ? (
          <span className="text-[9px] uppercase tracking-wider px-1 py-0.5 rounded bg-neon-amber/20 border border-neon-amber/40">
            DEV
          </span>
        ) : (
          <span className="text-[9px] uppercase tracking-wider text-zinc-500 dark:text-zinc-600">
            prod
          </span>
        )}
        {loading ? <span className="text-xs text-zinc-600">...</span> : null}
        <ChevronDown className="w-3 h-3 opacity-60" />
      </button>
      {open ? (
        <div className="absolute top-full left-0 mt-1 z-50 w-64 max-h-72 overflow-y-auto rounded border border-zinc-200 bg-white shadow-xl dark:border-zinc-800 dark:bg-zinc-950">
          <button
            type="button"
            onClick={() => {
              onChange(null);
              setOpen(false);
            }}
            className={`w-full text-left px-3 py-2 text-sm hover:bg-zinc-100 dark:hover:bg-zinc-900 ${
              current === null ? "text-keboola" : "text-zinc-700 dark:text-zinc-300"
            }`}
          >
            main (production)
          </button>
          {branches
            .filter((b) => !b.isDefault)
            .map((b) => (
              <button
                key={b.id}
                type="button"
                onClick={() => {
                  onChange(b.id);
                  setOpen(false);
                }}
                className={`w-full text-left px-3 py-2 text-sm hover:bg-zinc-100 dark:hover:bg-zinc-900 ${
                  current === b.id ? "text-keboola" : "text-zinc-700 dark:text-zinc-300"
                }`}
              >
                <div>{b.name}</div>
                <div className="text-xs text-zinc-500">#{b.id}</div>
              </button>
            ))}
        </div>
      ) : null}
    </div>
  );
}
