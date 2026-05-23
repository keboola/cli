import { describe, expect, it } from "vitest";
import { defaultTypeFor, profileColumn, profileTable } from "./profile";

describe("profileColumn", () => {
  it("returns empty for an all-null column", () => {
    const p = profileColumn("x", [null, "", undefined, null]);
    expect(p.inferredType).toBe("empty");
    expect(p.nullCount).toBe(4);
    expect(p.distinctCount).toBe(0);
    expect(p.samples).toEqual([]);
  });

  it("infers integer for clean numeric values", () => {
    const p = profileColumn("count", ["1", "2", "3", "100"]);
    expect(p.inferredType).toBe("integer");
    expect(p.nullCount).toBe(0);
    expect(p.distinctCount).toBe(4);
  });

  it("infers float for mixed int/float (widens to float)", () => {
    const p = profileColumn("amount", ["1", "2.5", "3.14", "100"]);
    expect(p.inferredType).toBe("float");
  });

  it("infers date and datetime separately", () => {
    expect(profileColumn("d", ["2024-01-01", "2024-02-15"]).inferredType).toBe("date");
    expect(profileColumn("ts", ["2024-01-01T10:00:00", "2024-02-15 12:30:00"]).inferredType).toBe(
      "datetime",
    );
    // Mixed widens to datetime.
    expect(
      profileColumn("mix", ["2024-01-01", "2024-02-15T10:00:00"]).inferredType,
    ).toBe("datetime");
  });

  it("infers boolean for textual true/false, not for 0/1", () => {
    expect(profileColumn("flag", ["true", "false", "True"]).inferredType).toBe("boolean");
    // "0"/"1" alone classify as integer — safer for accidental numeric flags
    expect(profileColumn("flag2", ["0", "1", "0"]).inferredType).toBe("integer");
  });

  it("falls back to string for mixed unrelated types", () => {
    expect(profileColumn("mix", ["1", "two", "3"]).inferredType).toBe("string");
  });

  it("captures null ratio and min/max length", () => {
    const p = profileColumn("name", ["joe", "alice", "", null, "bob"]);
    expect(p.nullCount).toBe(2);
    expect(p.nullRatio).toBeCloseTo(0.4);
    expect(p.minLength).toBe(3);
    expect(p.maxLength).toBe(5);
  });

  it("deduplicates samples and caps at 5", () => {
    const p = profileColumn(
      "x",
      ["a", "b", "a", "c", "d", "e", "f", "b"],
    );
    expect(p.samples).toHaveLength(5);
    expect(new Set(p.samples).size).toBe(5);
  });
});

describe("profileTable", () => {
  it("profiles all columns in header order", () => {
    const header = ["id", "name", "amount"];
    const rows = [
      ["1", "alice", "10.5"],
      ["2", "bob", "20"],
      ["3", "", "0"],
    ];
    const profiles = profileTable(header, rows);
    expect(profiles.map((p) => p.name)).toEqual(header);
    expect(profiles[0].inferredType).toBe("integer");
    expect(profiles[1].inferredType).toBe("string");
    expect(profiles[1].nullCount).toBe(1);
    expect(profiles[2].inferredType).toBe("float");
  });
});

describe("defaultTypeFor", () => {
  it("maps inferred types to canonical names", () => {
    const ints = profileColumn("x", ["1", "2"]);
    expect(defaultTypeFor(ints)).toBe("INTEGER");
    const floats = profileColumn("x", ["1.0", "2.5"]);
    expect(defaultTypeFor(floats)).toBe("FLOAT");
    const dates = profileColumn("x", ["2024-01-01"]);
    expect(defaultTypeFor(dates)).toBe("DATE");
    const bools = profileColumn("x", ["true", "false"]);
    expect(defaultTypeFor(bools)).toBe("BOOLEAN");
  });

  it("buckets VARCHAR sizes based on max observed length", () => {
    expect(defaultTypeFor(profileColumn("x", ["abc"]))).toBe("VARCHAR(32)");
    expect(defaultTypeFor(profileColumn("x", ["a".repeat(100)]))).toBe("VARCHAR(128)");
    expect(defaultTypeFor(profileColumn("x", ["a".repeat(500)]))).toBe("VARCHAR(1024)");
    expect(defaultTypeFor(profileColumn("x", ["a".repeat(5000)]))).toBe("STRING");
  });
});
