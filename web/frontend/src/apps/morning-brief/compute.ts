/**
 * Pure functions for the Morning Brief computation. Kept separate from
 * the React component so they are trivially unit-testable and so the
 * UI file stays focused on layout.
 */
import type { Job } from "../../types";

export interface BriefRow {
  job: Job;
  medianSeconds: number;
  factor: number;
  sampleSize: number;
}

export interface BriefSummary {
  totalCount: number;
  last24hCount: number;
  successCount: number;
  errorCount: number;
  warningCount: number;
  totalDurationSeconds: number;
  longestJob: Job | null;
  outliers: BriefRow[];
}

const OUTLIER_FACTOR = 2.0;
const MIN_SAMPLES_FOR_OUTLIER = 3;

function parseCreatedTime(t: string | undefined): number | null {
  if (!t) return null;
  const d = Date.parse(t);
  return Number.isFinite(d) ? d : null;
}

function median(values: number[]): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0
    ? (sorted[mid - 1] + sorted[mid]) / 2
    : sorted[mid];
}

/**
 * Group durations by (project, component, configId) so an outlier check is
 * meaningful only against the same config's history. Cross-config medians
 * would mix CSV imports with full warehouse loads.
 */
function groupKey(j: Job): string {
  return `${j.project_alias}::${j.component}::${j.configId}`;
}

export function computeBrief(jobs: Job[]): BriefSummary {
  const now = Date.now();
  const dayAgo = now - 24 * 60 * 60 * 1000;

  let last24hCount = 0;
  let successCount = 0;
  let errorCount = 0;
  let warningCount = 0;
  let totalDurationSeconds = 0;
  let longestJob: Job | null = null;
  const groups = new Map<string, number[]>();

  for (const j of jobs) {
    const created = parseCreatedTime(j.createdTime);
    if (created !== null && created >= dayAgo) last24hCount += 1;

    if (j.status === "success") successCount += 1;
    else if (j.status === "error") errorCount += 1;
    else if (j.status === "warning") warningCount += 1;

    const dur = j.durationSeconds ?? 0;
    if (dur > 0) {
      totalDurationSeconds += dur;
      if (!longestJob || (longestJob.durationSeconds ?? 0) < dur) {
        longestJob = j;
      }
      const key = groupKey(j);
      const arr = groups.get(key) ?? [];
      arr.push(dur);
      groups.set(key, arr);
    }
  }

  // For each job, compare to the median of its group. Only flag if the
  // group has enough samples; with 1-2 runs "outlier" is meaningless.
  const outliers: BriefRow[] = [];
  for (const j of jobs) {
    const dur = j.durationSeconds ?? 0;
    if (dur <= 0) continue;
    const arr = groups.get(groupKey(j)) ?? [];
    if (arr.length < MIN_SAMPLES_FOR_OUTLIER) continue;
    const m = median(arr);
    if (m <= 0) continue;
    const factor = dur / m;
    if (factor >= OUTLIER_FACTOR) {
      outliers.push({ job: j, medianSeconds: m, factor, sampleSize: arr.length });
    }
  }
  // Highest factor first; ties broken by longer duration.
  outliers.sort((a, b) =>
    b.factor !== a.factor
      ? b.factor - a.factor
      : (b.job.durationSeconds ?? 0) - (a.job.durationSeconds ?? 0),
  );

  return {
    totalCount: jobs.length,
    last24hCount,
    successCount,
    errorCount,
    warningCount,
    totalDurationSeconds,
    longestJob,
    outliers,
  };
}
