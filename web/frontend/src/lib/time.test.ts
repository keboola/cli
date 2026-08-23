/**
 * `now` is injected in every case -- a relative-time helper tested against the
 * wall clock is a flaky test waiting for a slow CI box.
 */
import { describe, expect, it } from "vitest";
import { formatRelativeTime } from "./time";

const NOW = new Date("2026-08-24T12:00:00.000Z");
const ago = (seconds: number) => new Date(NOW.getTime() - seconds * 1000).toISOString();

describe("formatRelativeTime", () => {
  it("reads the present as 'now'", () => {
    expect(formatRelativeTime(NOW.toISOString(), NOW)).toBe("now");
  });

  it("counts seconds below a minute", () => {
    expect(formatRelativeTime(ago(5), NOW)).toBe("5 seconds ago");
    expect(formatRelativeTime(ago(59), NOW)).toBe("59 seconds ago");
  });

  it("switches to minutes at 60s and truncates rather than rounds up", () => {
    expect(formatRelativeTime(ago(60), NOW)).toBe("1 minute ago");
    // 90s is closer to 2 minutes, but an elapsed time must not overstate.
    expect(formatRelativeTime(ago(90), NOW)).toBe("1 minute ago");
    expect(formatRelativeTime(ago(59 * 60), NOW)).toBe("59 minutes ago");
  });

  it("switches to hours at an hour", () => {
    expect(formatRelativeTime(ago(3600), NOW)).toBe("1 hour ago");
    expect(formatRelativeTime(ago(23 * 3600), NOW)).toBe("23 hours ago");
  });

  it("switches to days at a day, using the friendly form", () => {
    expect(formatRelativeTime(ago(24 * 3600), NOW)).toBe("yesterday");
    expect(formatRelativeTime(ago(3 * 24 * 3600), NOW)).toBe("3 days ago");
    expect(formatRelativeTime(ago(40 * 24 * 3600), NOW)).toBe("40 days ago");
  });

  it("handles a future timestamp (clock skew between stack and browser)", () => {
    expect(formatRelativeTime(ago(-90), NOW)).toBe("in 1 minute");
    expect(formatRelativeTime(ago(-24 * 3600), NOW)).toBe("tomorrow");
  });

  it("passes an unparseable value through instead of showing 'Invalid Date'", () => {
    expect(formatRelativeTime("not-a-date", NOW)).toBe("not-a-date");
    expect(formatRelativeTime("", NOW)).toBe("");
  });
});
