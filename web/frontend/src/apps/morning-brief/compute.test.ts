import { describe, expect, it } from "vitest";
import type { Job } from "../../types";
import { computeBrief } from "./compute";

function job(over: Partial<Job> & Pick<Job, "id" | "component" | "configId">): Job {
  return {
    project_alias: "proj-a",
    status: "success",
    createdTime: new Date().toISOString(),
    durationSeconds: 10,
    ...over,
  } as Job;
}

describe("computeBrief", () => {
  it("returns zeros for an empty list", () => {
    const r = computeBrief([]);
    expect(r.totalCount).toBe(0);
    expect(r.last24hCount).toBe(0);
    expect(r.outliers).toEqual([]);
    expect(r.longestJob).toBeNull();
  });

  it("counts statuses correctly", () => {
    const r = computeBrief([
      job({ id: 1, component: "x", configId: "1", status: "success" }),
      job({ id: 2, component: "x", configId: "1", status: "error" }),
      job({ id: 3, component: "x", configId: "1", status: "warning" }),
      job({ id: 4, component: "x", configId: "1", status: "processing" }),
    ]);
    expect(r.successCount).toBe(1);
    expect(r.errorCount).toBe(1);
    expect(r.warningCount).toBe(1);
    expect(r.totalCount).toBe(4);
  });

  it("does not flag outliers below the sample threshold", () => {
    // Only 2 runs at 10s and one at 100s — group too small (need >= 3).
    const r = computeBrief([
      job({ id: 1, component: "x", configId: "1", durationSeconds: 10 }),
      job({ id: 2, component: "x", configId: "1", durationSeconds: 100 }),
    ]);
    expect(r.outliers).toHaveLength(0);
  });

  it("flags a >=2x-of-median job once samples are sufficient", () => {
    // 4 runs around 10s, one at 100s (10x median). Median = 10.
    const jobs = [
      job({ id: 1, component: "x", configId: "1", durationSeconds: 8 }),
      job({ id: 2, component: "x", configId: "1", durationSeconds: 12 }),
      job({ id: 3, component: "x", configId: "1", durationSeconds: 10 }),
      job({ id: 4, component: "x", configId: "1", durationSeconds: 11 }),
      job({ id: 5, component: "x", configId: "1", durationSeconds: 100 }),
    ];
    const r = computeBrief(jobs);
    expect(r.outliers).toHaveLength(1);
    expect(r.outliers[0].job.id).toBe(5);
    expect(r.outliers[0].factor).toBeGreaterThanOrEqual(2);
    expect(r.outliers[0].sampleSize).toBe(5);
  });

  it("groups by (project, component, config) so different configs do not pollute the median", () => {
    // Config A: 4 runs around 10s. Config B: 1 run at 100s.
    // The 100s run is in a 1-sample group => no outlier (threshold).
    const jobs = [
      job({ id: 1, component: "x", configId: "A", durationSeconds: 8 }),
      job({ id: 2, component: "x", configId: "A", durationSeconds: 10 }),
      job({ id: 3, component: "x", configId: "A", durationSeconds: 12 }),
      job({ id: 4, component: "x", configId: "A", durationSeconds: 11 }),
      job({ id: 5, component: "x", configId: "B", durationSeconds: 100 }),
    ];
    const r = computeBrief(jobs);
    expect(r.outliers).toHaveLength(0);
  });

  it("sorts outliers by factor desc, then by duration desc", () => {
    const jobs = [
      // Config A: median 10
      job({ id: 1, component: "x", configId: "A", durationSeconds: 10 }),
      job({ id: 2, component: "x", configId: "A", durationSeconds: 10 }),
      job({ id: 3, component: "x", configId: "A", durationSeconds: 10 }),
      job({ id: 4, component: "x", configId: "A", durationSeconds: 30 }), // 3x
      // Config B: median 5
      job({ id: 5, component: "x", configId: "B", durationSeconds: 5 }),
      job({ id: 6, component: "x", configId: "B", durationSeconds: 5 }),
      job({ id: 7, component: "x", configId: "B", durationSeconds: 5 }),
      job({ id: 8, component: "x", configId: "B", durationSeconds: 50 }), // 10x
    ];
    const r = computeBrief(jobs);
    expect(r.outliers.map((o) => o.job.id)).toEqual([8, 4]);
  });

  it("counts last 24h based on createdTime", () => {
    const old = new Date(Date.now() - 48 * 60 * 60 * 1000).toISOString();
    const recent = new Date(Date.now() - 1 * 60 * 60 * 1000).toISOString();
    const r = computeBrief([
      job({ id: 1, component: "x", configId: "1", createdTime: old }),
      job({ id: 2, component: "x", configId: "1", createdTime: recent }),
    ]);
    expect(r.last24hCount).toBe(1);
    expect(r.totalCount).toBe(2);
  });

  it("tracks the longest job", () => {
    const r = computeBrief([
      job({ id: 1, component: "x", configId: "1", durationSeconds: 5 }),
      job({ id: 2, component: "y", configId: "2", durationSeconds: 60 }),
      job({ id: 3, component: "z", configId: "3", durationSeconds: 30 }),
    ]);
    expect(r.longestJob?.id).toBe(2);
  });
});
