/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";
import { branchParam } from "./branch";

describe("branchParam", () => {
  it("asks for production in the production view", () => {
    expect(branchParam(null)).toBe(0);
  });

  it("passes a selected dev branch through", () => {
    expect(branchParam(456)).toBe(456);
  });
});

/**
 * Guard for CLI-43: in the production view a page must send `branch_id: 0`,
 * never drop the parameter. A route without `branch_id` can use the active
 * branch saved by `kbagent branch use` (the routes listed in spec rule V2 from 1.0.0).
 */
describe("page sources", () => {
  const sources = import.meta.glob(["../pages/*.tsx", "../components/*.tsx"], {
    query: "?raw",
    import: "default",
    eager: true,
  }) as Record<string, string>;

  it("finds the page sources", () => {
    expect(Object.keys(sources).length).toBeGreaterThan(10);
  });

  it.each([
    ["a dropped branch", /branchId \?\? undefined/],
    ["a null branch_id", /branch_id: null/],
  ])("send no %s", (_label, pattern) => {
    const offenders = Object.entries(sources)
      .filter(([, text]) => pattern.test(text))
      .map(([path]) => path);
    expect(offenders).toEqual([]);
  });
});
