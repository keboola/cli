/**
 * Ctrl+K / Cmd+K command palette.
 *
 * One keystroke to reach anything the shell can already do: jump to a page,
 * switch the active project, or fire a small action (theme, Swagger docs).
 * Deliberately NOT a search over Keboola data -- that is the Search page's
 * job, and mixing "navigate the app" with "query the project" makes both
 * slower. Everything here resolves locally, so the list never waits on a
 * network round-trip.
 *
 * The page list comes from the sidebar's exported SECTIONS, so a page can
 * never be reachable from one surface and invisible in the other.
 */
import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Boxes, CornerDownLeft, Search, Sparkles } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../api/client";
import { SECTIONS } from "../layout/Sidebar";
import { type PageId, useUIState } from "../state";
import { useTheme } from "../theme";
import type { Project } from "../types";

type CommandKind = "page" | "project" | "action";

interface Command {
  id: string;
  kind: CommandKind;
  /** Matched + rendered label. */
  label: string;
  /** Muted right-hand context (section name, project name, ...). */
  hint?: string;
  /** Extra text folded into the match haystack but not rendered. */
  keywords?: string;
  icon: React.ComponentType<{ className?: string }>;
  run: () => void;
}

/**
 * Subsequence ("fuzzy") match, case-insensitive. Returns the matched indices
 * so the caller can highlight them, or null when the query does not match.
 *
 * Scoring favours matches that start earlier and stay contiguous, which is
 * what makes "sto" rank Storage above "Semantic Layer" even though both
 * contain the letters.
 */
function fuzzyMatch(query: string, text: string): { score: number; indices: number[] } | null {
  if (!query) return { score: 0, indices: [] };
  const q = query.toLowerCase();
  const t = text.toLowerCase();
  const indices: number[] = [];
  let ti = 0;
  let score = 0;
  let lastHit = -2;
  for (let qi = 0; qi < q.length; qi++) {
    const ch = q[qi];
    const found = t.indexOf(ch, ti);
    if (found === -1) return null;
    // Contiguous runs and hits at the very start of the string are cheaper.
    score += found - ti;
    if (found !== lastHit + 1) score += 3;
    if (found === 0) score -= 2;
    indices.push(found);
    lastHit = found;
    ti = found + 1;
  }
  return { score, indices };
}

/** Render `text` with the fuzzy-matched characters tinted cyan. */
function Highlight({ text, indices }: { text: string; indices: number[] }) {
  if (indices.length === 0) return <>{text}</>;
  const hit = new Set(indices);
  return (
    <>
      {text.split("").map((ch, i) =>
        hit.has(i) ? (
          <span key={i} className="text-accent">
            {ch}
          </span>
        ) : (
          <span key={i}>{ch}</span>
        ),
      )}
    </>
  );
}

const KIND_LABEL: Record<CommandKind, string> = {
  page: "page",
  project: "project",
  action: "action",
};

export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const { project, setProject, setBranchId, setPage, setWhatsNewForced } = useUIState();
  const { theme, toggle } = useTheme();

  // Projects are already cached by the top bar under this exact key, so
  // opening the palette normally costs no request.
  const projectsQ = useQuery<{ projects: Project[] }>({
    queryKey: ["projects"],
    queryFn: () => api.get("/projects"),
    enabled: open,
  });

  const close = useCallback(() => {
    setOpen(false);
    setQuery("");
    setCursor(0);
  }, []);

  // Global hotkey. Registered once on the shell so it works from any page.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((o) => !o);
        setQuery("");
        setCursor(0);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (!open) return;
    const t = setTimeout(() => inputRef.current?.focus(), 0);
    return () => clearTimeout(t);
  }, [open]);

  const commands: Command[] = useMemo(() => {
    const out: Command[] = [];
    for (const section of SECTIONS) {
      for (const item of section.items) {
        out.push({
          id: `page:${item.id}`,
          kind: "page",
          label: item.label,
          hint: section.title,
          keywords: item.id,
          icon: item.icon,
          run: () => setPage(item.id as PageId),
        });
      }
    }
    for (const p of projectsQ.data?.projects ?? []) {
      out.push({
        id: `project:${p.alias}`,
        kind: "project",
        label: p.alias,
        hint: p.project_name || p.org_name || "switch project",
        keywords: `${p.project_name ?? ""} ${p.org_name ?? ""} switch`,
        icon: Boxes,
        run: () => {
          setProject(p.alias);
          // A branch id is only meaningful inside its own project.
          setBranchId(null);
        },
      });
    }
    out.push({
      id: "action:theme",
      kind: "action",
      label: `Toggle theme (now ${theme})`,
      keywords: "dark light colour color scheme",
      icon: Sparkles,
      run: toggle,
    });
    out.push({
      id: "action:docs",
      kind: "action",
      label: "Open Swagger /docs",
      hint: "new tab",
      keywords: "openapi api schema swagger",
      icon: ArrowRight,
      run: () => window.open("/docs", "_blank", "noopener,noreferrer"),
    });
    out.push({
      id: "action:whatsnew",
      kind: "action",
      label: "What's new",
      hint: "release highlights",
      keywords: "changelog release highlights whatsnew version news",
      icon: Sparkles,
      run: () => setWhatsNewForced(true),
    });
    return out;
  }, [projectsQ.data, setPage, setProject, setBranchId, theme, toggle, setWhatsNewForced]);

  const results = useMemo(() => {
    const q = query.trim();
    const scored: Array<{ cmd: Command; indices: number[]; score: number }> = [];
    for (const cmd of commands) {
      const onLabel = fuzzyMatch(q, cmd.label);
      if (onLabel) {
        scored.push({ cmd, indices: onLabel.indices, score: onLabel.score });
        continue;
      }
      // Fall back to the invisible haystack (project name, page id, synonyms)
      // so "colour" finds the theme toggle -- but rank those below label hits.
      const haystack = `${cmd.label} ${cmd.hint ?? ""} ${cmd.keywords ?? ""}`;
      const onHaystack = fuzzyMatch(q, haystack);
      if (onHaystack) scored.push({ cmd, indices: [], score: onHaystack.score + 50 });
    }
    if (!q) return scored.slice(0, 50);
    return scored.sort((a, b) => a.score - b.score).slice(0, 50);
  }, [commands, query]);

  // Keep the cursor inside the (shrinking) result list as the user types.
  useEffect(() => {
    setCursor((c) => (c >= results.length ? 0 : c));
  }, [results.length]);

  useEffect(() => {
    if (!open) return;
    listRef.current
      ?.querySelector<HTMLElement>(`[data-idx="${cursor}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [cursor, open]);

  if (!open) return null;

  const runAt = (idx: number) => {
    const hit = results[idx];
    if (!hit) return;
    hit.cmd.run();
    close();
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      close();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setCursor((c) => (results.length === 0 ? 0 : (c + 1) % results.length));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setCursor((c) => (results.length === 0 ? 0 : (c - 1 + results.length) % results.length));
    } else if (e.key === "Enter") {
      e.preventDefault();
      runAt(cursor);
    }
  };

  return createPortal(
    <div
      className="fixed inset-0 z-[60] bg-zinc-900/50 backdrop-blur-sm flex items-start justify-center p-4 pt-[12vh] dark:bg-black/70"
      onClick={close}
      role="presentation"
    >
      <div
        className="nerd-card w-full max-w-xl p-0 overflow-hidden border-keboola/40 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 px-3 py-2.5 border-b border-zinc-200 dark:border-zinc-800">
          <Search className="w-4 h-4 text-keboola shrink-0" />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="jump to a page, switch project, run an action…"
            className="flex-1 bg-transparent text-sm focus:outline-none placeholder-zinc-400 dark:placeholder-zinc-600"
            aria-label="Command palette"
          />
          <span className="nerd-pill text-[10px] shrink-0">esc</span>
        </div>

        <div ref={listRef} className="max-h-[50vh] overflow-y-auto">
          {results.length === 0 ? (
            <div className="px-4 py-6 text-xs text-zinc-500 text-center">
              Nothing matches “{query}”.
            </div>
          ) : (
            results.map(({ cmd, indices }, i) => {
              const Icon = cmd.icon;
              const active = i === cursor;
              return (
                <button
                  key={cmd.id}
                  type="button"
                  data-idx={i}
                  onMouseEnter={() => setCursor(i)}
                  onClick={() => runAt(i)}
                  className={`w-full text-left px-3 py-2 flex items-center gap-2.5 text-sm border-l-2 ${
                    active
                      ? "border-keboola bg-keboola/10 text-keboola"
                      : "border-transparent text-zinc-700 hover:bg-zinc-100 dark:text-zinc-300 dark:hover:bg-zinc-900/60"
                  }`}
                >
                  <Icon className="w-3.5 h-3.5 shrink-0 opacity-80" />
                  <span className="truncate">
                    <Highlight text={cmd.label} indices={indices} />
                  </span>
                  {cmd.kind === "project" && cmd.label === project ? (
                    <span className="nerd-pill-green text-[10px] shrink-0">active</span>
                  ) : null}
                  <span className="ml-auto flex items-center gap-2 shrink-0">
                    {cmd.hint ? (
                      <span className="text-[10px] text-zinc-500 truncate max-w-[12rem]">
                        {cmd.hint}
                      </span>
                    ) : null}
                    <span className="text-[10px] uppercase tracking-wider text-zinc-500 dark:text-zinc-600">
                      {KIND_LABEL[cmd.kind]}
                    </span>
                  </span>
                </button>
              );
            })
          )}
        </div>

        <div className="flex items-center gap-3 px-3 py-1.5 border-t border-zinc-200 text-[10px] text-zinc-500 dark:border-zinc-800 dark:text-zinc-600">
          <span className="flex items-center gap-1">
            <CornerDownLeft className="w-3 h-3" /> run
          </span>
          <span>↑↓ move</span>
          <span>esc close</span>
          <span className="ml-auto">{results.length} result(s)</span>
        </div>
      </div>
    </div>,
    document.body,
  );
}
