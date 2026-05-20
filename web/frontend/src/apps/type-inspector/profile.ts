/**
 * Pure value-profiling logic. No React, no fetch, no DOM — kept here so it
 * is trivially unit-testable and the page file stays focused on layout.
 *
 * Inputs: a column's sample values (strings, since Storage preview returns
 * everything as text). Outputs: counts, ratios, inferred basic type, and
 * normalised samples.
 */

export type InferredType =
  | "empty"
  | "boolean"
  | "integer"
  | "float"
  | "date"
  | "datetime"
  | "string";

export interface ColumnProfile {
  name: string;
  sampleSize: number;
  nullCount: number;
  nullRatio: number;
  distinctCount: number;
  inferredType: InferredType;
  /** Up to 5 non-null sample values, in original order, deduplicated. */
  samples: string[];
  /** Min/max length over non-null string values. Useful for VARCHAR(n) sizing. */
  minLength: number | null;
  maxLength: number | null;
}

/**
 * "Null-ish" check. Storage preview returns "" for missing cells in CSV-
 * unloaded tables, so we treat empty string as null. Real null literal is
 * rare but possible from typed buckets.
 */
function isNullish(v: unknown): boolean {
  return v === null || v === undefined || v === "";
}

const INT_RE = /^-?\d+$/;
const FLOAT_RE = /^-?\d+\.\d+$/;
// ISO 8601 dates / datetimes. Lenient enough to cover common Keboola
// exports while rejecting obvious non-dates ("2-3 weeks").
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const DATETIME_RE = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$/;
const BOOL_VALUES = new Set(["true", "false", "True", "False", "TRUE", "FALSE", "0", "1"]);

function classifyValue(v: string): Exclude<InferredType, "empty"> {
  if (BOOL_VALUES.has(v)) {
    // Only commit to boolean when the value is unambiguously textual; "0"/"1"
    // are deferred to integer because they're more likely numeric in practice.
    if (v.length > 1) return "boolean";
    return "integer";
  }
  if (INT_RE.test(v)) return "integer";
  if (FLOAT_RE.test(v)) return "float";
  if (DATETIME_RE.test(v)) return "datetime";
  if (DATE_RE.test(v)) return "date";
  return "string";
}

/**
 * Conservative widening rule: if *every* non-null value classifies to the
 * same narrow type, return that type. If types mix, widen to the more
 * permissive umbrella (integer + float -> float; date + datetime -> datetime;
 * anything else mixed -> string).
 */
function aggregateTypes(types: Exclude<InferredType, "empty">[]): InferredType {
  if (types.length === 0) return "empty";
  const set = new Set(types);
  if (set.size === 1) return [...set][0];
  // Numeric widening
  if (set.size === 2 && set.has("integer") && set.has("float")) return "float";
  // Date widening
  if (set.size === 2 && set.has("date") && set.has("datetime")) return "datetime";
  // Boolean + integer (e.g. "0"/"1" mixed with "true"/"false") -> integer
  // is a safer common ground than guessing boolean.
  if (set.size === 2 && set.has("boolean") && set.has("integer")) return "integer";
  return "string";
}

export function profileColumn(name: string, values: unknown[]): ColumnProfile {
  const sampleSize = values.length;
  const nonNull: string[] = [];
  let nullCount = 0;
  for (const v of values) {
    if (isNullish(v)) {
      nullCount += 1;
    } else {
      nonNull.push(String(v));
    }
  }
  const distinct = new Set(nonNull);
  const samples = [...distinct].slice(0, 5);

  let minLength: number | null = null;
  let maxLength: number | null = null;
  for (const s of nonNull) {
    if (minLength === null || s.length < minLength) minLength = s.length;
    if (maxLength === null || s.length > maxLength) maxLength = s.length;
  }

  const inferred = aggregateTypes(nonNull.map(classifyValue));

  return {
    name,
    sampleSize,
    nullCount,
    nullRatio: sampleSize === 0 ? 0 : nullCount / sampleSize,
    distinctCount: distinct.size,
    inferredType: inferred,
    samples,
    minLength,
    maxLength,
  };
}

/**
 * Profile every column of a preview response. `header` is the column order,
 * `rows` is an array of value-arrays in that same order — the exact shape
 * `/storage/table-preview` returns.
 */
export function profileTable(
  header: string[],
  rows: unknown[][],
): ColumnProfile[] {
  return header.map((name, i) =>
    profileColumn(
      name,
      rows.map((r) => r[i]),
    ),
  );
}

/**
 * Map an inferred-type label to a sensible default Snowflake/BigQuery-ish
 * concrete type. Used as the *fallback* type when the user has not yet
 * asked the AI for a proposal. Conservative defaults: never narrower than
 * the data observed.
 */
export function defaultTypeFor(profile: ColumnProfile): string {
  switch (profile.inferredType) {
    case "boolean":
      return "BOOLEAN";
    case "integer":
      return "INTEGER";
    case "float":
      return "FLOAT";
    case "date":
      return "DATE";
    case "datetime":
      return "TIMESTAMP";
    case "empty":
      return "STRING";
    case "string":
    default: {
      // Bucket common lengths -- avoids "VARCHAR(7)" when slightly longer
      // values inevitably show up later. Round up to standard sizes.
      const max = profile.maxLength ?? 0;
      if (max <= 32) return "VARCHAR(32)";
      if (max <= 128) return "VARCHAR(128)";
      if (max <= 1024) return "VARCHAR(1024)";
      return "STRING";
    }
  }
}
