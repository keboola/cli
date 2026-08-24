/**
 * Ctrl+K / Cmd+K command palette.
 *
 * One keystroke to reach anything the shell can already do: jump to a page,
 * switch the active project, fire a small action (theme, Swagger docs) -- and
 * land on a specific storage bucket or table.
 *
 * The storage objects are folded in WITHOUT giving up the palette's core
 * contract: nothing waits on the network while you type. The data is fetched
 * once when the palette opens and matched locally. The active project's
 * buckets/tables reuse the exact react-query keys the Storage page already
 * populates, so opening the palette after visiting Storage normally costs no
 * request at all; the cross-project bucket list is its own key held at a 60s
 * staleTime. Cross-project TABLES are deliberately not loaded -- that haystack
 * grows with every registered project, and a foreign table is one keystroke
 * away via its bucket anyway.
 *
 * Anything the local haystack cannot answer escapes to the Search page via the
 * always-last "Search … across projects" row, which is where a real server-side
 * query belongs.
 *
 * Picking a storage object navigates by writing the Storage page's `?sel=`
 * selection (built with that page's own grammar helper, never spelled out
 * here), so a palette jump produces the same shareable URL as clicking the
 * row -- and lands nowhere the UI could not already be linked to.
 *
 * The page list comes from the sidebar's exported SECTIONS, so a page can
 * never be reachable from one surface and invisible in the other.
 */
import { useQuery } from "@tanstack/react-query";
import {
  ArrowRight,
  Boxes,
  CornerDownLeft,
  Database,
  Search,
  Sparkles,
  Table2,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../api/client";
import { PALETTE_ONLY_PAGES, SECTIONS } from "../layout/Sidebar";
import { buildStorageSel } from "../pages/Storage";
import { type PageId, useUIState } from "../state";
import { useTheme } from "../theme";
import type { Bucket, Project, Table } from "../types";

type CommandKind = "page" | "project" | "action" | "bucket" | "table" | "search";

interface Command {
  id: string;
  kind: CommandKind;
  /** Matched + rendered label. */
  label: string;
  /** Muted right-hand context (section name, project name, ...). */
  hint?: string;
  /** Extra text folded into the match haystack but not rendered. */
  keywords?: string;
  /** Added to the match score (higher = ranked lower). See DATA_SCORE_BIAS. */
  bias?: number;
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
  bucket: "bucket",
  table: "table",
  search: "search",
};

/**
 * Ranking bias (higher = worse; results sort ascending). Storage objects
 * outnumber pages and actions by orders of magnitude, so without a bias a
 * two-letter query would bury "Storage" under fifty buckets that happen to
 * contain those letters. A specific query like "oltp" still wins on raw match
 * quality, which is exactly the trade we want.
 */
const DATA_SCORE_BIAS = 25;
/** Foreign buckets rank below the active project's own on an equal match. */
const FOREIGN_PROJECT_SCORE_BIAS = 10;

export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const {
    project,
    branchId,
    setProject,
    setBranchId,
    setPage,
    setSel,
    setWhatsNewForced,
    setPendingSearchQuery,
  } = useUIState();
  const { theme, toggle } = useTheme();

  // Projects are already cached by the top bar under this exact key, so
  // opening the palette normally costs no request.
  const projectsQ = useQuery<{ projects: Project[] }>({
    queryKey: ["projects"],
    queryFn: () => api.get("/projects"),
    enabled: open,
  });

  // Same key shape as the Storage page's buckets query -- a shared cache
  // entry, not a second fetch, whenever Storage has already been visited.
  const bucketsQ = useQuery<{ buckets: Bucket[]; errors: unknown[] }>({
    queryKey: ["buckets", project, branchId],
    queryFn: () =>
      api.get("/storage/buckets", {
        query: { project: project ?? undefined, branch_id: branchId ?? undefined },
      }),
    enabled: open && !!project,
  });

  // Mirrors the Storage page's UNFILTERED tables query (bucket filter = null).
  const tablesQ = useQuery<{ tables: Table[]; errors: unknown[] }>({
    queryKey: ["tables", project, null, branchId],
    queryFn: () =>
      api.get("/storage/tables", {
        query: { project: project ?? undefined, branch_id: branchId ?? undefined },
      }),
    enabled: open && !!project,
  });

  // Every registered project at once. No branch_id: branch ids are numbered
  // per project, so one value cannot mean anything across a fan-out. The
  // server degrades per project via `errors`, which the palette ignores --
  // a project that failed simply contributes no rows, and saying so here
  // would be noise in a list you are typing through.
  const allBucketsQ = useQuery<{ buckets: Bucket[]; errors: unknown[] }>({
    queryKey: ["buckets-all"],
    queryFn: () => api.get("/storage/buckets"),
    enabled: open,
    staleTime: 60_000,
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
    for (const item of PALETTE_ONLY_PAGES) {
      out.push({
        id: `page:${item.id}`,
        kind: "page",
        label: item.label,
        hint: "All projects",
        keywords: item.id,
        icon: item.icon,
        run: () => setPage(item.id as PageId),
      });
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

    /**
     * Navigate to a storage object. The `sel` string is built by the Storage
     * page's own grammar helper rather than spelled out here -- the page owns
     * the meaning of its selection, the palette only picks a target.
     *
     * ORDER MATTERS: setProject / setBranchId / setPage each clear `sel` (a
     * selection from another context is meaningless), so the selection has to
     * be written LAST or it would be wiped by the navigation that precedes it.
     */
    const openStorage = (alias: string, sel: string | null) => {
      if (alias !== project) {
        setProject(alias);
        // A branch id is only meaningful inside its own project.
        setBranchId(null);
      }
      setPage("storage");
      setSel(sel);
    };
    const bucketCommand = (b: Bucket, foreign: boolean): Command => ({
      id: `bucket:${b.project_alias}/${b.id}`,
      kind: "bucket",
      label: b.display_name || b.id,
      hint: `${b.project_alias} · ${b.id}`,
      keywords: `${b.id} ${b.stage}`,
      bias: DATA_SCORE_BIAS + (foreign ? FOREIGN_PROJECT_SCORE_BIAS : 0),
      icon: Database,
      run: () => openStorage(b.project_alias, buildStorageSel("tables", null, b.id)),
    });

    for (const b of bucketsQ.data?.buckets ?? []) out.push(bucketCommand(b, false));
    // The active project's own rows come from the branch-aware query above and
    // are authoritative for it; the fan-out contributes only foreign projects,
    // which also dedupes the two lists against each other.
    for (const b of allBucketsQ.data?.buckets ?? []) {
      if (b.project_alias === project) continue;
      out.push(bucketCommand(b, true));
    }

    // Active project only -- see the file header on why foreign tables are not
    // loaded (bounded haystack; reachable through their bucket).
    for (const t of tablesQ.data?.tables ?? []) {
      out.push({
        id: `table:${t.project_alias}/${t.id}`,
        kind: "table",
        label: t.display_name || t.name,
        hint: `${t.project_alias} · ${t.bucket_id}`,
        keywords: t.id,
        bias: DATA_SCORE_BIAS,
        icon: Table2,
        run: () => openStorage(t.project_alias, buildStorageSel("tables", t.id)),
      });
    }
    return out;
  }, [
    projectsQ.data,
    bucketsQ.data,
    tablesQ.data,
    allBucketsQ.data,
    project,
    setPage,
    setProject,
    setBranchId,
    setSel,
    setWhatsNewForced,
    theme,
    toggle,
  ]);

  const results = useMemo(() => {
    const q = query.trim();
    const scored: Array<{ cmd: Command; indices: number[]; score: number }> = [];
    for (const cmd of commands) {
      // With no query there is nothing to rank by, so storage objects would
      // simply flood the idle list and push the pages out of it. The resting
      // palette stays what it always was: pages, projects, actions.
      if (!q && (cmd.kind === "bucket" || cmd.kind === "table")) continue;
      const bias = cmd.bias ?? 0;
      const onLabel = fuzzyMatch(q, cmd.label);
      if (onLabel) {
        scored.push({ cmd, indices: onLabel.indices, score: onLabel.score + bias });
        continue;
      }
      // Fall back to the invisible haystack (project name, page id, synonyms)
      // so "colour" finds the theme toggle -- but rank those below label hits.
      const haystack = `${cmd.label} ${cmd.hint ?? ""} ${cmd.keywords ?? ""}`;
      const onHaystack = fuzzyMatch(q, haystack);
      if (onHaystack) scored.push({ cmd, indices: [], score: onHaystack.score + 50 + bias });
    }
    if (!q) return scored.slice(0, 50);
    const top = scored.sort((a, b) => a.score - b.score).slice(0, 50);
    // Appended AFTER sorting and slicing: the escape hatch is not a match, it
    // is the answer to "the local haystack does not have it". Being part of
    // `results` is what makes it reachable with ↓ and Enter like any other row.
    top.push({
      cmd: {
        id: "search:global",
        kind: "search",
        label: `Search "${q}" across projects`,
        hint: "Search page",
        icon: Search,
        run: () => {
          setPendingSearchQuery(q);
          setPage("search");
        },
      },
      indices: [],
      score: Number.MAX_SAFE_INTEGER,
    });
    return top;
  }, [commands, query, setPage, setPendingSearchQuery]);

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
            placeholder="jump to a page, find a bucket or table, run an action…"
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
