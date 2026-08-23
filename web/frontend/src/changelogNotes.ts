/**
 * Client-side parser for changelog notes.
 * =======================================
 *
 * `GET /changelog` returns the raw bullet strings from `changelog.py`, which
 * follow the authoring contract the CLI renderer already exploits
 * (`commands/changelog.py`): a leading `Prefix (#PR):` tag, a self-contained
 * first sentence, and inline backtick spans. This module ports that parsing
 * so the web page can render the same semantics -- typed badges, PR links,
 * headline-first collapsing -- instead of dumping the raw text.
 *
 * Keep the prefix list and sentence rules in sync with
 * `commands/changelog.py` (`_PREFIX_RE`) and `changelog.py` (`headline()`).
 */

/** Visual grouping for a note type; maps 1:1 to the CLI's Rich styles. */
export type NoteTone = "red" | "green" | "amber" | "blue" | "magenta" | "cyan" | "dim";

export interface ParsedNote {
  /** Canonical prefix label, e.g. "New", "Fix", "BREAKING". Null when the note has no recognised prefix. */
  label: string | null;
  tone: NoteTone;
  /** PR numbers pulled from the prefix decoration, e.g. "(#658, #664)" -> [658, 664]. */
  prs: number[];
  /** First sentence of the body (prefix stripped) -- the collapsed view. */
  headline: string;
  /** Body text after the headline; empty string when the headline is the whole note. */
  rest: string;
}

/** One piece of a rendered text run: plain text or an inline code span. */
export interface TextRun {
  code: boolean;
  text: string;
}

// Longest-alternative-first, mirroring _PREFIX_RE in commands/changelog.py.
const PREFIX_RE = new RegExp(
  "^(Plugin docs|Review fixes|Observability|Breaking|Security|Closed|Tests|" +
    "Internal|Change|Note|Fix|New|UX|E2E|Why)" +
    "(\\s*\\(([^)]*)\\))?" + // optional "(#274)" / "(sec-20 follow-up)" decoration
    ":\\s+",
  "i",
);

const TONES: Record<string, NoteTone> = {
  breaking: "red",
  security: "red",
  new: "green",
  fix: "amber",
  change: "blue",
  closed: "blue",
  ux: "magenta",
  note: "cyan",
  tests: "dim",
  "plugin docs": "dim",
  internal: "dim",
  observability: "dim",
  e2e: "dim",
  "review fixes": "dim",
  why: "dim",
};

/** Render the label the way the changelog writes it: BREAKING stays shouted, the rest title-case. */
const LABELS: Record<string, string> = {
  breaking: "BREAKING",
  security: "Security",
  new: "New",
  fix: "Fix",
  change: "Change",
  closed: "Closed",
  ux: "UX",
  note: "Note",
  tests: "Tests",
  "plugin docs": "Plugin docs",
  internal: "Internal",
  observability: "Observability",
  e2e: "E2E",
  "review fixes": "Review fixes",
  why: "Why",
};

// Abbreviations whose trailing period is not a sentence end; subset of
// _HEADLINE_ABBREVIATIONS in changelog.py that actually occurs in notes.
const ABBREVIATIONS = new Set(["e.g", "i.e", "etc", "vs", "cf", "incl", "resp"]);

/**
 * First-sentence split, porting `headline()`'s rules: a `.`/`!`/`?` followed
 * by whitespace ends the sentence, unless the period sits inside a version
 * number (digit before it) or terminates a known abbreviation.
 */
export function splitHeadline(text: string): { headline: string; rest: string } {
  const boundary = /[.!?](?=\s)/g;
  let m: RegExpExecArray | null = boundary.exec(text);
  while (m !== null) {
    const dot = m.index;
    const before = dot > 0 ? text[dot - 1] : "";
    const isDigitPeriod = text[dot] === "." && before >= "0" && before <= "9";
    const tokenMatch = /[\w.]+$/.exec(text.slice(0, dot));
    const token = tokenMatch ? tokenMatch[0].replace(/\.+$/, "").toLowerCase() : "";
    if (!isDigitPeriod && !ABBREVIATIONS.has(token)) {
      return {
        headline: text.slice(0, dot + 1),
        rest: text.slice(dot + 1).trim(),
      };
    }
    m = boundary.exec(text);
  }
  return { headline: text, rest: "" };
}

export function parseNote(note: string): ParsedNote {
  const m = PREFIX_RE.exec(note);
  let label: string | null = null;
  let tone: NoteTone = "dim";
  let prs: number[] = [];
  let body = note;
  if (m) {
    const key = m[1].toLowerCase();
    label = LABELS[key] ?? m[1];
    tone = TONES[key] ?? "dim";
    prs = [...(m[3] ?? "").matchAll(/#(\d+)/g)].map((g) => Number(g[1]));
    body = note.slice(m[0].length);
  }
  const { headline, rest } = splitHeadline(body);
  return { label, tone, prs, headline, rest };
}

/** Split text into plain/inline-code runs on backtick spans (`` `like this` ``). */
export function toRuns(text: string): TextRun[] {
  const runs: TextRun[] = [];
  for (const part of text.split(/(`[^`\n]+`)/)) {
    if (!part) continue;
    if (part.startsWith("`") && part.endsWith("`") && part.length >= 2) {
      runs.push({ code: true, text: part.slice(1, -1) });
    } else {
      runs.push({ code: false, text: part });
    }
  }
  return runs;
}
