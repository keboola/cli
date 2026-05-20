/**
 * Re-exports for OpenAPI-generated types.
 *
 * Generated.ts is auto-rebuilt by `npm run gen-types` (which calls
 * scripts/dump_openapi.py + openapi-typescript). Do not edit generated.ts
 * by hand -- it will be overwritten.
 *
 * Usage in apps/pages:
 *
 *     import type { components } from "@/api/types";
 *     import { api } from "@/api/client";
 *
 *     type Job = components["schemas"]["JobItem"];
 *     const jobs = await api.get<Job[]>("/jobs", { query: { project: "demo" } });
 *
 * Path-level types (parameters, request body, response shape) live under
 * the `paths` interface, keyed by the literal path string:
 *
 *     type ListJobsResponse =
 *       paths["/jobs"]["get"]["responses"]["200"]["content"]["application/json"];
 *
 * Prefer `components["schemas"][...]` where possible -- it's the canonical
 * model name and survives path renames.
 */

export type { components, operations, paths } from "./generated";
