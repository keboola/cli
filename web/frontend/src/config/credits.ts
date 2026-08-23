/**
 * Client-side credit ESTIMATE for a Queue API job.
 *
 * The Queue API does not report what a job cost, and the billing endpoints
 * only expose a project-level PAYG balance -- there is no per-job figure to
 * fetch. So this is a MODEL, not a reading: it multiplies the job's wall-clock
 * duration by a published per-hour rate for the (component family, container
 * size) pair. Every surface that shows a number derived from here must label
 * it as an estimate.
 *
 * Known limits of the model, by construction:
 *  - Rates are a static table maintained here, not fetched from the platform,
 *    so a pricing change lands in the UI only when this file is updated.
 *  - `durationSeconds` is wall clock. Queue wait time is excluded (good), but
 *    so is any billing floor / minimum increment the platform applies (bad).
 *  - Flows and orchestrators are rated 0 on purpose: their children are the
 *    jobs that get billed, and they show up as their own rows. Counting the
 *    parent too would double-bill every orchestrated run.
 */

/**
 * Credits per HOUR, keyed by container size. `small` doubles as the fallback
 * for any size not listed (see `creditRate`), which is how a flat-rate family
 * is expressed: a single `small` entry.
 */
export type CreditRates = Record<string, number>;

export interface RateRule {
  /** Substrings tried against the component id, case-insensitively. */
  match: string[];
  rates: CreditRates;
}

/**
 * ORDER IS PART OF THE CONTRACT -- first match wins.
 *
 * `redshift-transformation` has to be tested before the generic
 * `r-transformation`, and the transformation families before the broad
 * `ex-` / `wr-` prefixes, or a rate would be picked by accident of spelling.
 */
export const RATE_RULES: RateRule[] = [
  {
    match: ["snowflake-transformation", "redshift-transformation"],
    rates: { xsmall: 6, small: 6, medium: 12, large: 26 },
  },
  {
    match: ["python-transformation", "r-transformation"],
    rates: { xsmall: 0.2, small: 0.4, medium: 0.6, large: 2 },
  },
  // dbt runs remote (against the warehouse) or locally sized; anything else
  // falls back to `small` like every other family.
  { match: ["dbt"], rates: { small: 6, remote: 2 } },
  // Writers and extractors are flat-rated: one entry, used for every size.
  { match: ["wr-"], rates: { small: 1 } },
  { match: ["ex-", "extractor"], rates: { small: 2 } },
  { match: ["sandbox"], rates: { xsmall: 0.2, small: 6, medium: 12, large: 26 } },
  { match: ["data-app", "streamlit"], rates: { xsmall: 0.1, small: 0.2, medium: 0.5, large: 1 } },
  // Orchestration containers themselves are free -- the children are billed.
  { match: ["orchestrator", "keboola.flow"], rates: { small: 0 } },
];

/** Applied to a component id that matches no rule at all. */
export const DEFAULT_RATES: CreditRates = { small: 1 };

/** Container size assumed when the job carries no metrics at all. */
export const DEFAULT_CONTAINER_SIZE = "small";

/** The rate table for a component id -- first matching rule, else the default. */
export function rateForComponent(componentId: string): CreditRates {
  const id = (componentId ?? "").toLowerCase();
  for (const rule of RATE_RULES) {
    if (rule.match.some((needle) => id.includes(needle))) return rule.rates;
  }
  return DEFAULT_RATES;
}

/**
 * Container size out of a job's `metrics` passthrough.
 *
 * The Queue API has used both spellings over time (`containerSize` is the
 * current one, `size` the older), and a job that never started carries
 * neither -- hence the documented default rather than a throw.
 */
export function getContainerSize(metrics: unknown): string {
  const backend = (metrics as { backend?: Record<string, unknown> } | undefined)?.backend;
  const raw = backend?.containerSize ?? backend?.size;
  if (raw === undefined || raw === null || raw === "") return DEFAULT_CONTAINER_SIZE;
  return String(raw);
}

/**
 * Credits-per-hour for one (component, size) pair. An unlisted size falls back
 * to the family's `small` rate -- `??`, not `||`, so a genuine 0 (flows) is
 * kept rather than treated as "missing".
 */
export function creditRate(componentId: string, size: string): number {
  const rates = rateForComponent(componentId);
  return rates[(size ?? "").toLowerCase()] ?? rates[DEFAULT_CONTAINER_SIZE] ?? 0;
}

/** The shape `calculateJobCredits` needs -- a structural subset of `Job`. */
export interface CreditableJob {
  component: string;
  durationSeconds?: number;
  metrics?: unknown;
}

/**
 * Estimated credits for one job. A job with no duration yet (queued, or still
 * running with no reported elapsed time) estimates to 0 rather than to a guess.
 */
export function calculateJobCredits(job: CreditableJob): number {
  const seconds = job.durationSeconds;
  if (seconds === undefined || seconds === null || !Number.isFinite(seconds) || seconds <= 0) {
    return 0;
  }
  return (seconds / 3600) * creditRate(job.component, getContainerSize(job.metrics));
}

/** Sum of the estimates over a set of jobs. */
export function sumJobCredits(jobs: CreditableJob[]): number {
  return jobs.reduce((total, job) => total + calculateJobCredits(job), 0);
}

/**
 * Render an estimate at a precision that matches its magnitude: a sub-cent
 * value never renders as a bare "0.00" (which reads as free), and a four-digit
 * total never renders with meaningless decimals.
 */
export function formatCredits(credits: number): string {
  if (!Number.isFinite(credits) || credits <= 0) return "0";
  if (credits < 0.01) return "<0.01";
  if (credits < 1) return credits.toFixed(2);
  if (credits < 10) return credits.toFixed(1);
  return Math.round(credits).toLocaleString("en-US");
}
