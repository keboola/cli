/**
 * Pure helpers behind both token surfaces.
 *
 * Worth their own suite because both feed a security decision: `expiresLabel`
 * decides whether a row reads as "fine" or "clean this up", and
 * `lastUsedStatusOf` decides whether a token is reported as PROVEN unused
 * (safe to revoke) or merely UNKNOWN (the API was never asked / cannot say).
 * Collapsing either distinction is silent and destructive.
 */
import { describe, expect, it } from "vitest";
import {
  describeLastUsed,
  EXPIRY_SOON_DAYS,
  expiresLabel,
  lastUsedStatusOf,
  STATUS_TITLES,
  type TokenEntry,
} from "./tokensShared";

const NOW = Date.parse("2026-08-24T12:00:00Z");
const DAY = 86_400_000;

function token(extra: Partial<TokenEntry> = {}): TokenEntry {
  return { id: 1, ...extra };
}

describe("expiresLabel", () => {
  it("treats a missing expiry as 'never', not as a problem", () => {
    for (const empty of [null, undefined, ""]) {
      expect(expiresLabel(empty, NOW)).toEqual({ text: "never", tone: "none" });
    }
  });

  it("flags a lapsed token as expired", () => {
    expect(expiresLabel("2026-08-24T11:59:00Z", NOW)).toEqual({
      text: "expired",
      tone: "expired",
    });
  });

  it("treats the exact expiry instant as already expired", () => {
    // A token whose expiry equals `now` is dead, not "in 0d".
    expect(expiresLabel("2026-08-24T12:00:00Z", NOW).tone).toBe("expired");
  });

  it("warns on an expiry inside the soon window, in whole days", () => {
    expect(expiresLabel(new Date(NOW + 3 * DAY).toISOString(), NOW)).toEqual({
      text: "in 3d",
      tone: "soon",
    });
    // Partial days round UP -- 36h left is "in 2d", never "in 1d".
    expect(expiresLabel(new Date(NOW + 1.5 * DAY).toISOString(), NOW).text).toBe("in 2d");
  });

  it("puts the soon/later boundary at EXPIRY_SOON_DAYS inclusive", () => {
    expect(expiresLabel(new Date(NOW + EXPIRY_SOON_DAYS * DAY).toISOString(), NOW).tone).toBe(
      "soon",
    );
    expect(
      expiresLabel(new Date(NOW + (EXPIRY_SOON_DAYS + 1) * DAY).toISOString(), NOW).tone,
    ).toBe("later");
  });

  it("shows a far-off expiry as a plain calendar date", () => {
    expect(expiresLabel("2027-01-15T08:30:00Z", NOW)).toEqual({
      text: "2027-01-15",
      tone: "later",
    });
  });

  it("reports an unparsable value verbatim instead of guessing", () => {
    // Calling garbage "never" would hide exactly the row worth investigating.
    expect(expiresLabel("not-a-date", NOW)).toEqual({ text: "not-a-date", tone: "unknown" });
  });
});

describe("lastUsedStatusOf", () => {
  it("trusts an explicit status from the server", () => {
    expect(lastUsedStatusOf(token({ lastUsedStatus: "never" }))).toBe("never");
    expect(lastUsedStatusOf(token({ lastUsedStatus: "error" }))).toBe("error");
    // Explicit `never` wins even if a stale date rode along.
    expect(lastUsedStatusOf(token({ lastUsedStatus: "never", lastUsed: "2026-01-01" }))).toBe(
      "never",
    );
  });

  it("infers 'used' from a bare date", () => {
    expect(lastUsedStatusOf(token({ lastUsed: "2026-08-01T00:00:00Z" }))).toBe("used");
  });

  it("falls back to 'unknown', never to 'never', when nothing was derived", () => {
    // A listing fetched WITHOUT with_last_used carries no evidence at all;
    // reporting that as "never" would read as "proven unused, safe to revoke".
    expect(lastUsedStatusOf(token())).toBe("unknown");
    expect(lastUsedStatusOf(token({ lastUsed: null }))).toBe("unknown");
  });
});

describe("describeLastUsed", () => {
  it("keeps 'never' and 'unknown' distinguishable in the hover text", () => {
    expect(describeLastUsed("never")).toBe(STATUS_TITLES.never);
    expect(describeLastUsed("unknown")).toBe(STATUS_TITLES.unknown);
    expect(describeLastUsed("never")).not.toBe(describeLastUsed("unknown"));
  });

  it("lets an unrecognized status describe itself", () => {
    expect(describeLastUsed("brand-new-status")).toBe("brand-new-status");
  });
});
