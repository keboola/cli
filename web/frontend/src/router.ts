/**
 * Hash-based routing: the pure URL <-> UI-state translation.
 *
 * Why the hash and not the History API: `kbagent serve` mounts this SPA at the
 * root of the SAME FastAPI app that serves the REST API (`GET /projects`
 * returns JSON, not the shell), so a history-mode path like `/projects` would
 * collide with an endpoint. Everything after `#` is never sent to the server,
 * so the static mount keeps working with zero server changes.
 *
 * Schema:
 *   #/<page>                                  page with no project context
 *   #/p/<projectAlias>/<page>                 page scoped to a project
 *   #/p/<projectAlias>/<page>?branch=<id>     ... on a non-default branch
 *   #/p/<projectAlias>/<page>?sel=<encoded>   ... with a page-owned selection
 *
 * `sel` is opaque to the router: the page that owns it decides what it means
 * (a job id, `<tab>/<tableId>`, ...). It is URL-encoded as a whole, so a
 * multi-part selection joined with `/` survives the round trip.
 *
 * This module is deliberately React-free and side-effect-free so it can be
 * unit tested directly (see `router.test.ts`); the wiring lives in `state.tsx`.
 */
import { PAGE_IDS, type PageId } from "./state";

/** Where an unknown / missing page lands. */
export const DEFAULT_PAGE: PageId = "dashboard";

export interface RouteState {
  page: PageId;
  /** Project alias, or null for a page shown without project context. */
  project: string | null;
  /** Non-default (dev) branch id, or null for production. */
  branchId: number | null;
  /** Opaque, page-owned selection. */
  sel: string | null;
}

/**
 * `PAGE_IDS` is read inside the function bodies on purpose. `state.tsx`
 * imports this module, so a module-level constant derived from it (a `Set`,
 * say) would evaluate while `state.tsx` is still initializing and hit the
 * temporal dead zone. A linear scan over ~two dozen ids costs nothing.
 */
function toPageId(raw: string | undefined): PageId {
  if (!raw) return DEFAULT_PAGE;
  return (PAGE_IDS as readonly string[]).includes(raw) ? (raw as PageId) : DEFAULT_PAGE;
}

/** `decodeURIComponent` that returns the input verbatim on malformed escapes. */
function safeDecode(raw: string): string {
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

/**
 * A branch id is a positive integer. Anything else (a name, `0`, a float,
 * garbage) is dropped rather than forwarded to the API as a bogus filter.
 */
function parseBranchId(raw: string | null): number | null {
  if (!raw || !/^\d+$/.test(raw)) return null;
  const n = Number(raw);
  return Number.isSafeInteger(n) && n > 0 ? n : null;
}

/**
 * Parse a location hash into route state. Never throws: an empty, partial or
 * malformed hash degrades to the dashboard with no project context.
 *
 * Accepts the value with or without the leading `#`, so both
 * `window.location.hash` and a bare path can be passed.
 */
export function parseHash(hash: string): RouteState {
  const withoutMarker = hash.startsWith("#") ? hash.slice(1) : hash;
  const queryStart = withoutMarker.indexOf("?");
  const rawPath = queryStart === -1 ? withoutMarker : withoutMarker.slice(0, queryStart);
  const rawQuery = queryStart === -1 ? "" : withoutMarker.slice(queryStart + 1);

  // Trim the delimiters at both ends only. Interior empties are KEPT so that
  // `#/p//jobs` parses as "no project, page jobs" instead of shifting `jobs`
  // into the project slot.
  const trimmed = rawPath.replace(/^\/+/, "").replace(/\/+$/, "");
  const segments = trimmed === "" ? [] : trimmed.split("/");

  let project: string | null = null;
  let pageSegment: string | undefined;
  if (segments[0] === "p") {
    const alias = safeDecode(segments[1] ?? "");
    project = alias === "" ? null : alias;
    pageSegment = segments[2] === undefined ? undefined : safeDecode(segments[2]);
  } else {
    pageSegment = segments[0] === undefined ? undefined : safeDecode(segments[0]);
  }

  const params = new URLSearchParams(rawQuery);
  const sel = params.get("sel");

  return {
    page: toPageId(pageSegment),
    project,
    branchId: parseBranchId(params.get("branch")),
    sel: sel ? sel : null,
  };
}

/**
 * Render route state back into a location hash (leading `#` included).
 *
 * The query string is assembled by hand rather than via
 * `URLSearchParams.toString()`: that encodes spaces as `+`, which reads badly
 * in a link people paste to each other. `encodeURIComponent` emits `%20`, and
 * `URLSearchParams` decodes both, so `parseHash(buildHash(x))` still round
 * trips.
 */
export function buildHash(route: RouteState): string {
  const page = route.page;
  const path = route.project
    ? `/p/${encodeURIComponent(route.project)}/${page}`
    : `/${page}`;

  const params: string[] = [];
  if (route.branchId != null) params.push(`branch=${route.branchId}`);
  if (route.sel) params.push(`sel=${encodeURIComponent(route.sel)}`);

  return `#${path}${params.length ? `?${params.join("&")}` : ""}`;
}
