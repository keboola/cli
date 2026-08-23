import type { ReactNode } from "react";

export type PillTone = "default" | "green" | "amber" | "red";

const TONE_CLASS: Record<PillTone, string> = {
  default: "nerd-pill",
  green: "nerd-pill-green",
  amber: "nerd-pill-amber",
  red: "nerd-pill-red",
};

/**
 * A string array as wrap-around pills (feature flags, tags, capabilities).
 * Uses the shared `nerd-pill*` classes from index.css so the pills match the
 * status pills on the Jobs / Tokens tables.
 */
export function PillList({
  items,
  tone = "default",
  empty = "—",
}: {
  items: string[] | null | undefined;
  tone?: PillTone;
  /** Rendered when the list is missing or empty. */
  empty?: ReactNode;
}) {
  if (!items || items.length === 0) {
    return <span className="text-xs text-zinc-400 dark:text-zinc-600">{empty}</span>;
  }
  return (
    <div className="flex flex-wrap gap-1.5">
      {items.map((it) => (
        <span key={it} className={`${TONE_CLASS[tone]} text-[10px] break-all`}>
          {it}
        </span>
      ))}
    </div>
  );
}
