/**
 * Time formatting helpers shared across pages.
 */

const RELATIVE = new Intl.RelativeTimeFormat("en", { numeric: "auto" });

/**
 * "3 minutes ago" / "yesterday" for an ISO timestamp.
 *
 * `now` is injectable so callers (and tests) can pin the reference point
 * instead of depending on the wall clock.
 *
 * Truncation, not rounding: a job that finished 90 seconds ago reads
 * "1 minute ago", never "2 minutes ago" -- an elapsed time must not overstate
 * itself. An unparseable input is returned verbatim, so a malformed timestamp
 * shows the raw value rather than "Invalid Date".
 */
export function formatRelativeTime(iso: string, now: Date = new Date()): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return iso;

  const seconds = Math.trunc((then - now.getTime()) / 1000);
  if (Math.abs(seconds) < 60) return RELATIVE.format(seconds, "second");

  const minutes = Math.trunc(seconds / 60);
  if (Math.abs(minutes) < 60) return RELATIVE.format(minutes, "minute");

  const hours = Math.trunc(minutes / 60);
  if (Math.abs(hours) < 24) return RELATIVE.format(hours, "hour");

  return RELATIVE.format(Math.trunc(hours / 24), "day");
}
