import type { ReactNode } from "react";

export interface KeyValueItem {
  label: string;
  value: ReactNode;
  /** Render the value in the mono/cyan register used for IDs and URLs. */
  mono?: boolean;
}

/**
 * Column-count -> Tailwind class. Written out as whole literals because the
 * JIT content scanner only sees class names that appear verbatim in the
 * source; an interpolated `lg:grid-cols-${n}` would never be generated.
 */
const COLS: Record<number, string> = {
  1: "grid-cols-1",
  2: "grid-cols-1 sm:grid-cols-2",
  3: "grid-cols-1 sm:grid-cols-2 lg:grid-cols-3",
  4: "grid-cols-2 lg:grid-cols-4",
};

/**
 * Label/value pairs in a responsive grid — the generalized form of the ad-hoc
 * `KV` helpers on the Jobs and Streams detail panels.
 *
 * Empty values (null / undefined / "") render a muted em dash rather than
 * disappearing: in a detail view "we asked and the answer was nothing" and
 * "this field does not exist" must not look the same.
 */
export function KeyValueGrid({
  items,
  columns = 2,
  className = "",
}: {
  items: KeyValueItem[];
  columns?: 1 | 2 | 3 | 4;
  className?: string;
}) {
  return (
    <div className={`grid ${COLS[columns]} gap-3 ${className}`}>
      {items.map((it) => (
        <div key={it.label} className="min-w-0">
          <div className="text-[10px] uppercase tracking-wider text-zinc-500">{it.label}</div>
          <div
            className={`text-xs mt-0.5 break-words ${
              it.mono ? "font-mono text-accent" : "text-zinc-800 dark:text-zinc-200"
            }`}
          >
            {isBlank(it.value) ? <span className="text-zinc-400 dark:text-zinc-600">—</span> : it.value}
          </div>
        </div>
      ))}
    </div>
  );
}

// `false` counts as blank because React renders it as nothing at all: a
// caller who meant the word "false" must stringify it, and one who wrote
// `cond && <x/>` gets the em dash instead of a silently empty cell.
function isBlank(value: ReactNode): boolean {
  return value === null || value === undefined || value === "" || value === false;
}
