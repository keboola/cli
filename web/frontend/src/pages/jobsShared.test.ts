import { describe, expect, it } from "vitest";
import type { Job } from "../types";
import { jobBranchId } from "./jobsShared";

const job = (branchId: unknown): Job =>
  ({ project_alias: "prod", id: 1, status: "success", component: "c", config: "1", branchId }) as Job;

describe("jobBranchId", () => {
  it("reads a job without a branch as production", () => {
    expect(jobBranchId(job(null))).toBe(0);
    expect(jobBranchId(job(undefined))).toBe(0);
  });

  it("passes a numeric string through as a number", () => {
    expect(jobBranchId(job("12"))).toBe(12);
  });

  it("reads a non-numeric branch as production", () => {
    expect(jobBranchId(job("x"))).toBe(0);
  });
});
