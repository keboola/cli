/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";
import { projectScope } from "./projectScope";

describe("projectScope", () => {
  it("asks for all projects in the All projects view", () => {
    expect(projectScope(null)).toEqual({ all_projects: true });
    expect(projectScope(undefined)).toEqual({ all_projects: true });
  });

  it("names a selected project", () => {
    expect(projectScope("prod")).toEqual({ project: "prod" });
  });
});

/**
 * Guard for CLI-49: a list page must not drop `project` to mean all projects.
 * Without `project` a list route reads only the active project.
 */
describe("page sources", () => {
  const sources = import.meta.glob(["../pages/*.tsx", "../components/*.tsx"], {
    query: "?raw",
    import: "default",
    eager: true,
  }) as Record<string, string>;

  it("send no dropped project", () => {
    const offenders = Object.entries(sources)
      .filter(([, text]) => /project: project \?\? undefined/.test(text))
      .map(([path]) => path);
    expect(offenders).toEqual([]);
  });
});
