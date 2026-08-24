import { ApiError } from "../api/client";

/**
 * Shared vocabulary for the two token surfaces: the per-project `Tokens` page
 * (list + mint + rotate + revoke) and the cross-project `TokensAll` audit page.
 *
 * Only the READ half lives here. Minting, rotating and revoking stay on the
 * per-project page, because every one of those calls is scoped to a single
 * project's token and there is no cross-project equivalent to share.
 *
 * The two non-obvious facts both surfaces depend on:
 *
 * 1. **`lastUsed` is DERIVED, not read.** The Storage API's token listing
 *    carries no `lastUsed` field at all. The backend synthesizes it per token
 *    from that token's OWN event feed -- one extra API call PER TOKEN, which is
 *    why it is opt-in behind a toggle on both pages (and why the cost is
 *    multiplied by the project count on the cross-project page).
 * 2. **Secrets are never in a listing.** Only `create` / `refresh` responses
 *    ever carry a token value, and only on the per-project page.
 */

/** Days of remaining lifetime under which an expiry is worth flagging. */
export const EXPIRY_SOON_DAYS = 30;

const MS_PER_DAY = 86_400_000;

export interface TokenEntry {
  id: string | number;
  description?: string;
  created?: string;
  refreshed?: string;
  expires?: string | null;
  isMasterToken?: boolean;
  canManageTokens?: boolean;
  canReadAllFileUploads?: boolean;
  bucketPermissions?: Record<string, string>;
  componentAccess?: string[];
  // present only with with_last_used=true
  lastUsed?: string | null;
  lastUsedEvent?: string | null;
  lastUsedStatus?: LastUsedStatus;
  /** Stamped by the cross-project listing only; absent on a single-project row. */
  project_alias?: string;
  [key: string]: unknown;
}

export type LastUsedStatus = "used" | "never" | "unknown" | "error";

export const LAST_USED_CAVEAT =
  "one extra API call per token ・ dev-branch activity is invisible (the events endpoint always resolves to the default branch)";

/** Same caveat, plus the cost multiplier that only bites on the global view. */
export const LAST_USED_CAVEAT_ALL_PROJECTS =
  "one extra API call per token, across EVERY registered project ・ dev-branch activity is invisible (the events endpoint always resolves to the default branch)";

export const STATUS_TITLES: Record<string, string> = {
  used: "This token performed at least one event -- the date is its most recent one.",
  never:
    "Minted INSIDE the ~6-month event retention window with no activity since -- proven unused.",
  unknown:
    "Older than the ~6-month event retention window -- the API cannot say whether it was used.",
  error: "The per-token lookup failed; this row degraded so the rest of the audit could complete.",
};

export function errMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof Error) return err.message;
  return String(err);
}

/**
 * The status a row should render under.
 *
 * A row that carries no explicit `lastUsedStatus` (an older backend, or a
 * listing fetched without `with_last_used`) is `unknown`, NOT `never`: "the
 * API was never asked" and "the API answered no activity" lead to opposite
 * decisions, and only the latter is evidence a token is safe to revoke.
 */
export function lastUsedStatusOf(t: TokenEntry): LastUsedStatus {
  if (t.lastUsedStatus) return t.lastUsedStatus;
  return t.lastUsed ? "used" : "unknown";
}

/** Hover text for a status pill; unrecognized values describe themselves. */
export function describeLastUsed(status: string): string {
  return STATUS_TITLES[status] ?? status;
}

export type ExpiryTone = "none" | "expired" | "soon" | "later" | "unknown";

export interface ExpiryLabel {
  text: string;
  tone: ExpiryTone;
}

/**
 * Turn a raw `expires` value into an audit-readable label.
 *
 * The raw timestamp answers "when", but a cross-project audit asks "is this a
 * problem": a token that already lapsed is dead weight to clean up, one
 * lapsing within {@link EXPIRY_SOON_DAYS} is a break waiting to happen in
 * whatever CI job holds it, and a never-expiring token is the normal case, not
 * an alarm. An unparsable value is reported verbatim rather than guessed at --
 * silently calling it "never" would hide exactly the row worth looking at.
 */
export function expiresLabel(
  expires: string | null | undefined,
  now: number = Date.now(),
): ExpiryLabel {
  if (!expires) return { text: "never", tone: "none" };
  const ms = Date.parse(expires);
  if (Number.isNaN(ms)) return { text: expires, tone: "unknown" };
  if (ms <= now) return { text: "expired", tone: "expired" };
  const days = Math.ceil((ms - now) / MS_PER_DAY);
  if (days <= EXPIRY_SOON_DAYS) return { text: `in ${days}d`, tone: "soon" };
  return { text: new Date(ms).toISOString().slice(0, 10), tone: "later" };
}

export function ScopeCell({ t }: { t: TokenEntry }) {
  const buckets = Object.keys(t.bucketPermissions ?? {}).length;
  const components = (t.componentAccess ?? []).length;
  if (t.isMasterToken) return <span className="nerd-pill-amber">master</span>;
  if (t.canManageTokens) return <span className="nerd-pill-amber">manage tokens</span>;
  if (buckets === 0 && components === 0) {
    return <span className="text-zinc-500 dark:text-zinc-600">—</span>;
  }
  return (
    <span className="text-xs text-zinc-600 dark:text-zinc-400">
      {buckets > 0 ? `${buckets} bucket(s)` : "—"}
      {components > 0 ? ` ・ ${components} component(s)` : ""}
    </span>
  );
}

export function StatusCell({ t }: { t: TokenEntry }) {
  // A real date (or an explicit "used") is the only green case. `never` and
  // `unknown` are deliberately NOT collapsed -- "proven unused, safe to revoke"
  // and "the API cannot say" lead to opposite decisions.
  const status = lastUsedStatusOf(t);
  const cls =
    status === "used"
      ? "nerd-pill-green"
      : status === "never"
        ? "nerd-pill-amber"
        : status === "error"
          ? "nerd-pill-red"
          : "nerd-pill";
  return (
    <span className={cls} title={describeLastUsed(status)}>
      {status}
    </span>
  );
}
