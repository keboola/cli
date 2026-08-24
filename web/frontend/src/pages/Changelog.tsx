/**
 * Changelog page -- the release rail.
 *
 * Renders `GET /changelog` the way the CLI renders `kbagent changelog`, but
 * readable: each version is a node on a vertical timeline, every note is
 * collapsed to its first sentence behind a typed badge (New / Fix / BREAKING
 * ... in the CLI's own colours), PR references become GitHub links, and the
 * running version is highlighted on the rail. Parsing lives in
 * `../changelogNotes.ts`, shared semantics with `commands/changelog.py`.
 */
import { useQuery } from "@tanstack/react-query";
import { ChevronRight } from "lucide-react";
import { useState } from "react";
import { api } from "../api/client";
import { type NoteTone, type TextRun, parseNote, toRuns } from "../changelogNotes";
import { ErrorBox, Loading, PageTitle } from "../components/Empty";

interface ChangelogResp {
  entries: Array<{ version: string; highlights: string[] }>;
}

interface VersionResp {
  kbagent: { version: string };
}

const BADGE_TONES: Record<NoteTone, string> = {
  red: "border-red-300 text-red-700 dark:border-red-700/50 dark:text-red-400",
  green: "border-keboola/40 text-keboola-600 dark:text-keboola",
  amber: "border-neon-amber/50 text-amber-700 dark:border-neon-amber/40 dark:text-neon-amber",
  blue: "border-sky-300 text-sky-700 dark:border-sky-700/50 dark:text-sky-400",
  magenta: "border-fuchsia-300 text-fuchsia-700 dark:border-fuchsia-700/50 dark:text-fuchsia-400",
  cyan: "border-cyan-300 text-cyan-700 dark:border-cyan-700/50 dark:text-cyan-400",
  dim: "border-zinc-300 text-zinc-500 dark:border-zinc-700 dark:text-zinc-500",
};

/** Inline text with `code` spans rendered like the rest of the UI's inline code. */
function Runs({ runs }: { runs: TextRun[] }) {
  return (
    <>
      {runs.map((r, i) =>
        r.code ? (
          <code
            key={i}
            className="px-1 py-px rounded bg-zinc-100 border border-zinc-200 text-[0.92em] text-zinc-700 dark:bg-zinc-950 dark:border-zinc-800 dark:text-zinc-300"
          >
            {r.text}
          </code>
        ) : (
          <span key={i}>{r.text}</span>
        ),
      )}
    </>
  );
}

function Note({ note, expanded, onToggle }: { note: string; expanded: boolean; onToggle: () => void }) {
  const p = parseNote(note);
  const hasDetail = p.rest.length > 0;
  return (
    <li className="group">
      <button
        type="button"
        onClick={onToggle}
        disabled={!hasDetail}
        aria-expanded={hasDetail ? expanded : undefined}
        className={`w-full text-left flex items-start gap-2 rounded px-2 py-1.5 -mx-2 ${
          hasDetail ? "hover:bg-zinc-100/70 dark:hover:bg-zinc-800/40 cursor-pointer" : "cursor-default"
        }`}
      >
        <ChevronRight
          className={`w-3.5 h-3.5 mt-1 shrink-0 transition-transform ${
            hasDetail
              ? `text-zinc-400 group-hover:text-zinc-600 dark:group-hover:text-zinc-300 ${expanded ? "rotate-90" : ""}`
              : "invisible"
          }`}
        />
        {p.label ? (
          <span
            className={`inline-flex shrink-0 mt-0.5 px-1.5 py-px rounded border text-[10px] font-bold uppercase tracking-wide ${BADGE_TONES[p.tone]}`}
          >
            {p.label}
          </span>
        ) : null}
        <span className="text-sm leading-relaxed text-zinc-800 dark:text-zinc-200 min-w-0">
          <Runs runs={toRuns(p.headline)} />
          {p.prs.map((pr) => (
            <a
              key={pr}
              href={`https://github.com/keboola/cli/pull/${pr}`}
              target="_blank"
              rel="noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="ml-1.5 text-xs text-zinc-400 hover:text-keboola transition-colors"
            >
              #{pr}
            </a>
          ))}
          {hasDetail && !expanded ? <span className="ml-1.5 text-xs text-zinc-400">&hellip;</span> : null}
        </span>
      </button>
      {hasDetail && expanded ? (
        <p className="ml-[3.75rem] mr-2 mb-1.5 text-xs leading-relaxed text-zinc-500 dark:text-zinc-400">
          <Runs runs={toRuns(p.rest)} />
        </p>
      ) : null}
    </li>
  );
}

export function ChangelogPage() {
  const q = useQuery<ChangelogResp>({
    queryKey: ["changelog"],
    queryFn: () => api.get("/changelog"),
  });
  // Same key + staleTime as the status bar, so this shares the cache.
  const versionQ = useQuery<VersionResp>({
    queryKey: ["version"],
    queryFn: () => api.get<VersionResp>("/version"),
    staleTime: 5 * 60_000,
  });
  const running = versionQ.data?.kbagent.version;

  const [expandAll, setExpandAll] = useState(false);
  // Per-note overrides on top of the global toggle; cleared when it flips.
  const [overrides, setOverrides] = useState<Record<string, boolean>>({});
  const isExpanded = (key: string) => overrides[key] ?? expandAll;
  const toggleNote = (key: string) =>
    setOverrides((o) => ({ ...o, [key]: !(o[key] ?? expandAll) }));
  const toggleAll = () => {
    setExpandAll((v) => !v);
    setOverrides({});
  };

  const entries = q.data?.entries ?? [];

  return (
    <div>
      <PageTitle
        title="Changelog"
        description="Release history of the kbagent kernel."
        actions={
          entries.length > 0 ? (
            <button type="button" className="nerd-btn text-xs" onClick={toggleAll}>
              {expandAll ? "collapse all" : "expand all"}
            </button>
          ) : undefined
        }
      />
      {q.isLoading ? <Loading /> : null}
      {q.error ? <ErrorBox message={(q.error as Error).message} /> : null}

      <ol className="relative ml-1.5 border-l border-zinc-200 dark:border-zinc-800">
        {entries.map((e, idx) => {
          const isRunning = e.version === running;
          return (
            <li key={e.version} className="relative pl-6 pb-8 last:pb-2">
              {/* Rail node: filled + glowing for the running version. */}
              <span
                aria-hidden
                className={`absolute -left-[5px] top-1.5 w-[9px] h-[9px] rounded-full border ${
                  isRunning
                    ? "bg-keboola border-keboola shadow-[0_0_6px_theme(colors.keboola.DEFAULT)]"
                    : "bg-white border-zinc-300 dark:bg-zinc-900 dark:border-zinc-600"
                }`}
              />
              <div className="flex items-baseline gap-2.5 flex-wrap">
                <h3
                  className={`font-bold text-base ${
                    isRunning ? "text-keboola" : "text-zinc-900 dark:text-zinc-100"
                  }`}
                >
                  v{e.version}
                </h3>
                {isRunning ? <span className="nerd-pill-green text-[10px]">running now</span> : null}
                {idx === 0 && !isRunning ? (
                  <span className="nerd-pill text-[10px]">latest</span>
                ) : null}
                <span className="text-xs text-zinc-400">
                  {e.highlights.length} {e.highlights.length === 1 ? "change" : "changes"}
                </span>
              </div>
              <ul className="mt-2 space-y-0.5">
                {e.highlights.map((h, i) => {
                  const key = `${e.version}:${i}`;
                  return (
                    <Note
                      key={key}
                      note={h}
                      expanded={isExpanded(key)}
                      onToggle={() => toggleNote(key)}
                    />
                  );
                })}
              </ul>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
