import { describe, expect, it } from "vitest";
import { buildHash, DEFAULT_PAGE, parseHash, type RouteState } from "./router";
import { PAGE_IDS } from "./state";

const EMPTY: RouteState = { page: "dashboard", project: null, branchId: null, sel: null };

function route(over: Partial<RouteState>): RouteState {
  return { ...EMPTY, ...over };
}

describe("parseHash", () => {
  it("falls back to the dashboard for an empty hash", () => {
    expect(parseHash("")).toEqual(EMPTY);
    expect(parseHash("#")).toEqual(EMPTY);
    expect(parseHash("#/")).toEqual(EMPTY);
    expect(parseHash("#///")).toEqual(EMPTY);
  });

  it("parses a bare page", () => {
    expect(parseHash("#/doctor")).toEqual(route({ page: "doctor" }));
    expect(parseHash("#/semantic-layer")).toEqual(route({ page: "semantic-layer" }));
  });

  it("accepts a hash with or without the leading marker", () => {
    expect(parseHash("/jobs")).toEqual(parseHash("#/jobs"));
  });

  it("tolerates a trailing slash", () => {
    expect(parseHash("#/jobs/")).toEqual(route({ page: "jobs" }));
    expect(parseHash("#/p/acme/jobs/")).toEqual(route({ page: "jobs", project: "acme" }));
  });

  it("parses a project-scoped page", () => {
    expect(parseHash("#/p/acme/storage")).toEqual(route({ page: "storage", project: "acme" }));
  });

  it("keeps the project when the page part is missing", () => {
    expect(parseHash("#/p/acme")).toEqual(route({ page: DEFAULT_PAGE, project: "acme" }));
    expect(parseHash("#/p/acme/")).toEqual(route({ page: DEFAULT_PAGE, project: "acme" }));
  });

  it("treats a bare /p as no project at all", () => {
    expect(parseHash("#/p")).toEqual(EMPTY);
    expect(parseHash("#/p/")).toEqual(EMPTY);
  });

  it("does not shift an empty project slot into the page slot", () => {
    // `#/p//jobs` must NOT parse `jobs` as the project alias.
    expect(parseHash("#/p//jobs")).toEqual(route({ page: "jobs", project: null }));
  });

  it("parses the branch query param", () => {
    expect(parseHash("#/p/acme/configs?branch=1234")).toEqual(
      route({ page: "configs", project: "acme", branchId: 1234 }),
    );
  });

  it("rejects a non-positive-integer branch", () => {
    for (const raw of ["abc", "0", "-5", "1.5", "12a", ""]) {
      expect(parseHash(`#/p/acme/configs?branch=${raw}`).branchId).toBeNull();
    }
  });

  it("parses the sel query param", () => {
    expect(parseHash("#/p/acme/jobs?sel=1234567890")).toEqual(
      route({ page: "jobs", project: "acme", sel: "1234567890" }),
    );
  });

  it("decodes a sel containing slashes", () => {
    expect(parseHash("#/p/acme/storage?sel=tables%2Fin.c-main.orders").sel).toBe(
      "tables/in.c-main.orders",
    );
  });

  it("decodes a sel containing reserved and unicode characters", () => {
    const encoded = encodeURIComponent("keboola.ex-db-snowflake/01ky4 pga?&#=+ěš");
    expect(parseHash(`#/p/acme/configs?sel=${encoded}`).sel).toBe(
      "keboola.ex-db-snowflake/01ky4 pga?&#=+ěš",
    );
  });

  it("treats an empty sel as no selection", () => {
    expect(parseHash("#/p/acme/jobs?sel=").sel).toBeNull();
  });

  it("parses branch and sel together, in any order", () => {
    const a = parseHash("#/p/acme/storage?branch=42&sel=tables%2Ft1");
    const b = parseHash("#/p/acme/storage?sel=tables%2Ft1&branch=42");
    expect(a).toEqual(route({ page: "storage", project: "acme", branchId: 42, sel: "tables/t1" }));
    expect(b).toEqual(a);
  });

  it("ignores unknown query params", () => {
    expect(parseHash("#/p/acme/jobs?foo=bar&branch=7")).toEqual(
      route({ page: "jobs", project: "acme", branchId: 7 }),
    );
  });

  it("decodes an encoded project alias", () => {
    expect(parseHash("#/p/my%20proj%2Fa/jobs")).toEqual(
      route({ page: "jobs", project: "my proj/a" }),
    );
  });

  it("survives malformed percent escapes", () => {
    expect(parseHash("#/p/100%/jobs")).toEqual(route({ page: "jobs", project: "100%" }));
  });

  it("falls back to the dashboard for an unknown page", () => {
    expect(parseHash("#/nope")).toEqual(EMPTY);
    // ... while keeping the project context, so the top bar does not reset.
    expect(parseHash("#/p/acme/nope?branch=9")).toEqual(
      route({ page: DEFAULT_PAGE, project: "acme", branchId: 9 }),
    );
  });

  it("never throws on garbage", () => {
    for (const junk of ["#!!!", "#/?&&=", "#%%%", "#/p/%/%/%", "#////?branch=", "#?sel=x"]) {
      expect(() => parseHash(junk)).not.toThrow();
      expect(PAGE_IDS).toContain(parseHash(junk).page);
    }
  });
});

describe("buildHash", () => {
  it("renders a bare page", () => {
    expect(buildHash(route({ page: "doctor" }))).toBe("#/doctor");
  });

  it("renders a project-scoped page", () => {
    expect(buildHash(route({ page: "storage", project: "acme" }))).toBe("#/p/acme/storage");
  });

  it("renders branch and sel", () => {
    expect(
      buildHash(route({ page: "storage", project: "acme", branchId: 42, sel: "tables/t1" })),
    ).toBe("#/p/acme/storage?branch=42&sel=tables%2Ft1");
  });

  it("omits an empty selection", () => {
    expect(buildHash(route({ page: "jobs", project: "acme", sel: "" }))).toBe("#/p/acme/jobs");
  });

  it("encodes the project alias", () => {
    expect(buildHash(route({ page: "jobs", project: "my proj/a" }))).toBe(
      "#/p/my%20proj%2Fa/jobs",
    );
  });

  it("encodes spaces as %20, not +", () => {
    const hash = buildHash(route({ page: "jobs", project: "acme", sel: "a b" }));
    expect(hash).toBe("#/p/acme/jobs?sel=a%20b");
    expect(hash).not.toContain("+");
  });
});

describe("round trips", () => {
  const cases: RouteState[] = [
    EMPTY,
    route({ page: "doctor" }),
    route({ page: "jobs", project: "acme" }),
    route({ page: "jobs", project: "acme", branchId: 1234 }),
    route({ page: "jobs", project: "acme", sel: "1122334455" }),
    route({ page: "storage", project: "acme", sel: "tables/in.c-main.orders" }),
    route({ page: "storage", project: "acme", branchId: 7, sel: "buckets" }),
    route({
      page: "configs",
      project: "acme",
      branchId: 99,
      sel: "keboola.ex-db-snowflake/01ky4pga8x9",
    }),
    route({ page: "flows", project: "p/roj ekt", sel: "a/b c?d&e=f#g+h" }),
    route({ page: "stream", project: "ěščř", sel: "zdroj/1" }),
  ];

  for (const c of cases) {
    it(`parseHash(buildHash(x)) === x for ${JSON.stringify(c)}`, () => {
      expect(parseHash(buildHash(c))).toEqual(c);
    });
  }

  it("is stable across a second pass", () => {
    for (const c of cases) {
      const once = buildHash(c);
      expect(buildHash(parseHash(once))).toBe(once);
    }
  });

  it("round trips every known page id", () => {
    for (const page of PAGE_IDS) {
      const r = route({ page, project: "acme", branchId: 5, sel: "x/y" });
      expect(parseHash(buildHash(r))).toEqual(r);
    }
  });
});
