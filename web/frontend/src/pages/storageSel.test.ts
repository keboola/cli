/**
 * The Storage page's `?sel=` grammar.
 *
 * Worth its own suite because two surfaces now write it -- the page's own row
 * clicks and the command palette's bucket/table rows -- so a change here
 * silently breaks a link that came from the other one.
 */
import { describe, expect, it } from "vitest";
import { buildStorageSel, parseStorageSel } from "./Storage";

describe("parseStorageSel", () => {
  it("lands on the buckets tab with no selection", () => {
    expect(parseStorageSel(null)).toEqual({ tab: "buckets", tableId: null, bucketId: null });
  });

  it("parses a bare tab", () => {
    expect(parseStorageSel("tables")).toEqual({ tab: "tables", tableId: null, bucketId: null });
    expect(parseStorageSel("files")).toEqual({ tab: "files", tableId: null, bucketId: null });
  });

  it("parses a table selection", () => {
    expect(parseStorageSel("tables/in.c-oltp.orders")).toEqual({
      tab: "tables",
      tableId: "in.c-oltp.orders",
      bucketId: null,
    });
  });

  it("parses a bucket filter onto the tables tab", () => {
    expect(parseStorageSel("bucket/in.c-oltp")).toEqual({
      tab: "tables",
      tableId: null,
      bucketId: "in.c-oltp",
    });
  });

  it("degrades an unknown or empty head to the buckets tab", () => {
    expect(parseStorageSel("nonsense")).toEqual({ tab: "buckets", tableId: null, bucketId: null });
    // A `bucket/` with nothing after it names no bucket, so there is nothing
    // to filter by -- the plain list is the honest fallback.
    expect(parseStorageSel("bucket/")).toEqual({ tab: "buckets", tableId: null, bucketId: null });
  });

  it("ignores a table id on a tab that has no detail view", () => {
    expect(parseStorageSel("files/in.c-oltp.orders")).toEqual({
      tab: "files",
      tableId: null,
      bucketId: null,
    });
  });
});

describe("buildStorageSel", () => {
  it("emits nothing for the landing view", () => {
    expect(buildStorageSel("buckets", null)).toBeNull();
    expect(buildStorageSel("buckets", null, "in.c-oltp")).toBeNull();
  });

  it("emits the bare tab when nothing is selected", () => {
    expect(buildStorageSel("tables", null)).toBe("tables");
    expect(buildStorageSel("files", null)).toBe("files");
  });

  it("emits a bucket filter", () => {
    expect(buildStorageSel("tables", null, "in.c-oltp")).toBe("bucket/in.c-oltp");
  });

  it("prefers the open table over the bucket filter", () => {
    // The table id already carries its bucket, so the filter adds nothing a
    // reader of the link would miss.
    expect(buildStorageSel("tables", "in.c-oltp.orders", "in.c-oltp")).toBe(
      "tables/in.c-oltp.orders",
    );
  });
});

describe("round trip", () => {
  it("survives parse(build(x)) for every form", () => {
    const cases: Array<[Parameters<typeof buildStorageSel>, string | null, string | null]> = [
      [["tables", "in.c-oltp.orders", null], "in.c-oltp.orders", null],
      [["tables", null, "in.c-oltp"], null, "in.c-oltp"],
      [["tables", null, null], null, null],
    ];
    for (const [args, tableId, bucketId] of cases) {
      const parsed = parseStorageSel(buildStorageSel(...args));
      expect(parsed.tab).toBe("tables");
      expect(parsed.tableId).toBe(tableId);
      expect(parsed.bucketId).toBe(bucketId);
    }
  });
});
