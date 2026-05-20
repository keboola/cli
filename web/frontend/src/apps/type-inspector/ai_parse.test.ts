import { describe, expect, it } from "vitest";
import { extractTypeFromAiResponse } from "./ai_parse";

describe("extractTypeFromAiResponse", () => {
  it("passes through a bare type literal", () => {
    expect(extractTypeFromAiResponse("VARCHAR(64)")).toBe("VARCHAR(64)");
    expect(extractTypeFromAiResponse("INTEGER")).toBe("INTEGER");
    expect(extractTypeFromAiResponse("  TIMESTAMP_NTZ  ")).toBe("TIMESTAMP_NTZ");
  });

  it("unwraps a fenced code block", () => {
    expect(extractTypeFromAiResponse("```\nVARCHAR(255)\n```")).toBe("VARCHAR(255)");
    expect(extractTypeFromAiResponse("```sql\nFLOAT\n```")).toBe("FLOAT");
  });

  it("unwraps inline backticks", () => {
    expect(extractTypeFromAiResponse("Use `VARCHAR(32)` for this.")).toBe("VARCHAR(32)");
  });

  it("extracts a bare type token from a chatty reply", () => {
    expect(extractTypeFromAiResponse("The type should be VARCHAR(128) here.")).toBe(
      "VARCHAR(128)",
    );
    expect(extractTypeFromAiResponse("I recommend NUMBER for ID columns.")).toBe("NUMBER");
  });

  it("falls back to the first line when nothing matches", () => {
    expect(extractTypeFromAiResponse("idk, varchar maybe?")).toBe("idk, varchar maybe?");
  });

  it("returns STRING for empty input", () => {
    expect(extractTypeFromAiResponse("")).toBe("STRING");
    expect(extractTypeFromAiResponse("\n\n")).toBe("STRING");
  });

  it("handles precision + scale: NUMBER(18,2)", () => {
    expect(extractTypeFromAiResponse("NUMBER(18,2)")).toBe("NUMBER(18,2)");
    expect(extractTypeFromAiResponse("`NUMBER(18, 2)` works")).toBe("NUMBER(18, 2)");
  });
});
