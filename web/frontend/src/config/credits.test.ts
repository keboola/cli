/**
 * The credit model is a table of substrings applied IN ORDER, so the risky
 * part is not the arithmetic -- it is which rule a component id lands on.
 * These cases pin the orderings that are easy to break by re-sorting the
 * table (redshift vs the generic `r-`, transformations vs `ex-`/`wr-`).
 */
import { describe, expect, it } from "vitest";
import {
  calculateJobCredits,
  creditRate,
  formatCredits,
  getContainerSize,
  rateForComponent,
  sumJobCredits,
} from "./credits";

describe("rateForComponent", () => {
  it("rates SQL transformations at the warehouse tier", () => {
    expect(rateForComponent("keboola.snowflake-transformation")).toEqual({
      xsmall: 6,
      small: 6,
      medium: 12,
      large: 26,
    });
  });

  it("keeps redshift on the SQL tier, not the script tier", () => {
    // Near miss worth pinning: the script rule matches the literal
    // `r-transformation`, and redshift's id reads `...t-transformation`, so it
    // escapes by one character. Rule 1 naming redshift explicitly is what
    // actually decides it -- this asserts the tier, not the spelling luck.
    expect(creditRate("keboola.redshift-transformation", "medium")).toBe(12);
  });

  it("rates script transformations far cheaper", () => {
    expect(creditRate("keboola.python-transformation-v2", "small")).toBe(0.4);
    expect(creditRate("keboola.r-transformation-v2", "large")).toBe(2);
  });

  it("does not mistake snowflake for an r-transformation", () => {
    expect(creditRate("keboola.snowflake-transformation", "small")).toBe(6);
  });

  it("rates dbt remote below local, and unknown sizes at its small rate", () => {
    expect(creditRate("keboola.dbt-transformation-snowflake", "remote")).toBe(2);
    expect(creditRate("keboola.dbt-transformation-snowflake", "small")).toBe(6);
    expect(creditRate("keboola.dbt-transformation-snowflake", "jumbo")).toBe(6);
  });

  it("flat-rates writers at 1 and extractors at 2, every size", () => {
    for (const size of ["xsmall", "small", "medium", "large"]) {
      expect(creditRate("keboola.wr-google-bigquery", size)).toBe(1);
      expect(creditRate("keboola.ex-db-mysql", size)).toBe(2);
    }
    expect(creditRate("some.vendor-extractor", "medium")).toBe(2);
  });

  it("takes the FIRST matching rule when an id matches two", () => {
    // Synthetic id, chosen because it genuinely matches both the extractor
    // rule ("ex-") and the sandbox rule ("sandbox"). The extractor rule is
    // listed first, so 2/hr wins over the sandbox tier's 26 at large.
    expect(creditRate("vendor.ex-sandbox-loader", "large")).toBe(2);
  });

  it("rates sandboxes and data apps on their own tiers", () => {
    expect(creditRate("keboola.sandboxes", "xsmall")).toBe(0.2);
    expect(creditRate("keboola.sandboxes", "large")).toBe(26);
    expect(creditRate("keboola.data-apps", "medium")).toBe(0.5);
    expect(creditRate("some.streamlit-runner", "xsmall")).toBe(0.1);
  });

  it("rates orchestration containers at zero -- children are billed", () => {
    expect(creditRate("keboola.orchestrator", "small")).toBe(0);
    expect(creditRate("keboola.flow", "large")).toBe(0);
  });

  it("falls back to a flat 1 for an unknown component", () => {
    expect(creditRate("acme.something-new", "medium")).toBe(1);
    expect(rateForComponent("acme.something-new")).toEqual({ small: 1 });
  });

  it("matches case-insensitively", () => {
    expect(creditRate("Keboola.EX-DB-MySQL", "SMALL")).toBe(2);
  });
});

describe("getContainerSize", () => {
  it("prefers containerSize", () => {
    expect(getContainerSize({ backend: { containerSize: "large", size: "small" } })).toBe("large");
  });

  it("falls back to the legacy size key", () => {
    expect(getContainerSize({ backend: { size: "medium" } })).toBe("medium");
  });

  it("defaults to small when metrics are missing or empty", () => {
    expect(getContainerSize(undefined)).toBe("small");
    expect(getContainerSize(null)).toBe("small");
    expect(getContainerSize({})).toBe("small");
    expect(getContainerSize({ backend: {} })).toBe("small");
    expect(getContainerSize({ backend: { containerSize: "" } })).toBe("small");
  });
});

describe("calculateJobCredits", () => {
  it("bills duration against the (component, size) rate", () => {
    // 1h on a medium Snowflake transformation = 12 credits.
    expect(
      calculateJobCredits({
        component: "keboola.snowflake-transformation",
        durationSeconds: 3600,
        metrics: { backend: { containerSize: "medium" } },
      }),
    ).toBeCloseTo(12, 10);
  });

  it("uses the small rate when the job reports no backend", () => {
    // 30 min on an extractor at the flat 2/hr rate.
    expect(
      calculateJobCredits({ component: "keboola.ex-db-mysql", durationSeconds: 1800 }),
    ).toBeCloseTo(1, 10);
  });

  it("estimates nothing for a job with no usable duration", () => {
    expect(calculateJobCredits({ component: "keboola.ex-db-mysql" })).toBe(0);
    expect(calculateJobCredits({ component: "keboola.ex-db-mysql", durationSeconds: 0 })).toBe(0);
    expect(
      calculateJobCredits({ component: "keboola.ex-db-mysql", durationSeconds: Number.NaN }),
    ).toBe(0);
  });

  it("never bills a flow for its children", () => {
    expect(
      calculateJobCredits({ component: "keboola.flow", durationSeconds: 7200 }),
    ).toBe(0);
  });
});

describe("sumJobCredits", () => {
  it("adds up a mixed list", () => {
    const total = sumJobCredits([
      // 12 credits
      {
        component: "keboola.snowflake-transformation",
        durationSeconds: 3600,
        metrics: { backend: { containerSize: "medium" } },
      },
      // 2 credits
      { component: "keboola.ex-db-mysql", durationSeconds: 3600 },
      // 0 -- orchestration container
      { component: "keboola.flow", durationSeconds: 3600 },
      // 0 -- never started
      { component: "keboola.wr-db-snowflake" },
    ]);
    expect(total).toBeCloseTo(14, 10);
  });

  it("is 0 for an empty list", () => {
    expect(sumJobCredits([])).toBe(0);
  });
});

describe("formatCredits", () => {
  it("collapses nothing-at-all to a bare 0", () => {
    expect(formatCredits(0)).toBe("0");
    expect(formatCredits(-1)).toBe("0");
    expect(formatCredits(Number.NaN)).toBe("0");
  });

  it("marks a sub-cent value rather than rendering it as 0.00", () => {
    expect(formatCredits(0.0001)).toBe("<0.01");
    expect(formatCredits(0.009)).toBe("<0.01");
  });

  it("shows two decimals from 0.01 up to 1", () => {
    expect(formatCredits(0.01)).toBe("0.01");
    expect(formatCredits(0.09)).toBe("0.09");
    expect(formatCredits(0.994)).toBe("0.99");
  });

  it("shows one decimal from 1 up to 10", () => {
    expect(formatCredits(1)).toBe("1.0");
    expect(formatCredits(9.94)).toBe("9.9");
  });

  it("rounds and groups from 10 up", () => {
    expect(formatCredits(10)).toBe("10");
    expect(formatCredits(1234.6)).toBe("1,235");
  });
});
