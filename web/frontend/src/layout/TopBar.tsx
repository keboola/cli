import { useQuery } from "@tanstack/react-query";
import { ChevronDown, GitBranch, Layers, Server } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { useUIState } from "../state";
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
    <header className="relative z-40 border-b border-zinc-900 bg-zinc-950/40 backdrop-blur px-4 h-12 flex items-center gap-4">
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
        <span className="flex items-center gap-1">
          <Server className="w-3 h-3 text-keboola" /> connected to BFF
        </span>
      </div>
    </header>
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
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", onClick);
    return () => window.removeEventListener("mousedown", onClick);
  }, [open]);
  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1.5 text-sm hover:text-keboola px-2 py-1 rounded border border-zinc-800 hover:border-keboola/40"
      >
        <Layers className="w-3.5 h-3.5" />
        <span>{current ?? "select project"}</span>
        <ChevronDown className="w-3 h-3 opacity-60" />
      </button>
      {open ? (
        <div className="absolute top-full left-0 mt-1 z-50 w-72 max-h-96 overflow-y-auto rounded border border-zinc-800 bg-zinc-950 shadow-xl">
          {projects.length === 0 ? (
            <div className="px-3 py-4 text-xs text-zinc-500">
              No projects. Add one via the Projects page.
            </div>
          ) : (
            projects.map((p) => (
              <button
                key={p.alias}
                type="button"
                onClick={() => {
                  onChange(p.alias);
                  setOpen(false);
                }}
                className={`w-full text-left px-3 py-2 text-sm flex items-center justify-between hover:bg-zinc-900 ${
                  p.alias === current ? "text-keboola" : "text-zinc-300"
                }`}
              >
                <div>
                  <div className="font-medium">{p.alias}</div>
                  <div className="text-xs text-zinc-500">{p.project_name}</div>
                </div>
                {p.is_default ? <span className="nerd-pill-green">default</span> : null}
              </button>
            ))
          )}
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
            ? "border-neon-amber/60 bg-neon-amber/10 text-neon-amber hover:bg-neon-amber/20"
            : "border-zinc-800 text-zinc-400 hover:text-keboola hover:border-keboola/40"
        }`}
      >
        <GitBranch className="w-3.5 h-3.5" />
        <span className="font-medium">{label}</span>
        {isDevBranch ? (
          <span className="text-[9px] uppercase tracking-wider px-1 py-0.5 rounded bg-neon-amber/20 border border-neon-amber/40">
            DEV
          </span>
        ) : (
          <span className="text-[9px] uppercase tracking-wider text-zinc-600">prod</span>
        )}
        {loading ? <span className="text-xs text-zinc-600">...</span> : null}
        <ChevronDown className="w-3 h-3 opacity-60" />
      </button>
      {open ? (
        <div className="absolute top-full left-0 mt-1 z-50 w-64 max-h-72 overflow-y-auto rounded border border-zinc-800 bg-zinc-950 shadow-xl">
          <button
            type="button"
            onClick={() => {
              onChange(null);
              setOpen(false);
            }}
            className={`w-full text-left px-3 py-2 text-sm hover:bg-zinc-900 ${
              current === null ? "text-keboola" : "text-zinc-300"
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
                className={`w-full text-left px-3 py-2 text-sm hover:bg-zinc-900 ${
                  current === b.id ? "text-keboola" : "text-zinc-300"
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
