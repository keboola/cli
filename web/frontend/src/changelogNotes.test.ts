import { describe, expect, it } from "vitest";
import { parseNote, splitHeadline, toRuns } from "./changelogNotes";

describe("parseNote", () => {
  it("extracts label, tone and PR numbers from a decorated prefix", () => {
    const n = parseNote("New (#658, #664): a Ctrl+K command palette. It jumps to any page.");
    expect(n.label).toBe("New");
    expect(n.tone).toBe("green");
    expect(n.prs).toEqual([658, 664]);
    expect(n.headline).toBe("a Ctrl+K command palette.");
    expect(n.rest).toBe("It jumps to any page.");
  });

  it("recognises prefixes case-insensitively and non-PR decorations", () => {
    const n = parseNote("BREAKING (sec-20 follow-up): tokens rotate.");
    expect(n.label).toBe("BREAKING");
    expect(n.tone).toBe("red");
    expect(n.prs).toEqual([]);
  });

  it("prefers the longest prefix alternative", () => {
    expect(parseNote("Plugin docs: refreshed.").label).toBe("Plugin docs");
    expect(parseNote("Plugin docs: refreshed.").tone).toBe("dim");
  });

  it("leaves unprefixed notes label-less with the full text as body", () => {
    const n = parseNote("Just a plain remark. With detail.");
    expect(n.label).toBeNull();
    expect(n.headline).toBe("Just a plain remark.");
    expect(n.rest).toBe("With detail.");
  });

  it("maps every documented prefix to its CLI tone", () => {
    expect(parseNote("Fix: x.").tone).toBe("amber");
    expect(parseNote("Change: x.").tone).toBe("blue");
    expect(parseNote("UX: x.").tone).toBe("magenta");
    expect(parseNote("Note: x.").tone).toBe("cyan");
    expect(parseNote("Security: x.").tone).toBe("red");
    expect(parseNote("Internal: x.").tone).toBe("dim");
  });
});

describe("splitHeadline", () => {
  it("does not break inside version numbers", () => {
    const { headline } = splitHeadline("since 0.57.0 the flow works. Detail here.");
    expect(headline).toBe("since 0.57.0 the flow works.");
  });

  it("does not break after e.g.", () => {
    const { headline, rest } = splitHeadline("some flags, e.g. `--json`, help. More.");
    expect(headline).toBe("some flags, e.g. `--json`, help.");
    expect(rest).toBe("More.");
  });

  it("returns the whole text when there is a single sentence", () => {
    const { headline, rest } = splitHeadline("One sentence only.");
    expect(headline).toBe("One sentence only.");
    expect(rest).toBe("");
  });

  it("treats ! and ? as sentence ends even after a digit", () => {
    const { headline } = splitHeadline("exit code 5! And more.");
    expect(headline).toBe("exit code 5!");
  });
});

describe("toRuns", () => {
  it("splits backtick spans into code runs", () => {
    expect(toRuns("run `kbagent serve --ui` today")).toEqual([
      { code: false, text: "run " },
      { code: true, text: "kbagent serve --ui" },
      { code: false, text: " today" },
    ]);
  });

  it("passes through text without backticks", () => {
    expect(toRuns("plain")).toEqual([{ code: false, text: "plain" }]);
  });
});
