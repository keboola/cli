/**
 * Curated "What's new" highlights, per release.
 * =============================================
 *
 * HAND-MAINTAINED. This is NOT the changelog -- `changelog.py` stays the
 * complete, authoritative record of every release note, and the Changelog page
 * renders it. This file is a short, curated reel of the *UI-visible* things a
 * returning user would want pointed out, written in the second person and kept
 * to a handful of items per version.
 *
 * MUST be updated in the release PR whenever a release ships user-visible UI
 * features. Add a new `WhatsNewRelease` entry keyed by the exact
 * `pyproject.toml` version, newest first.
 *
 * A version with NO entry here shows no popup at all. That is the intended
 * default, not a bug: a patch release that only touches CLI internals has
 * nothing to interrupt anyone about, and an empty modal is worse than silence.
 */

export interface WhatsNewItem {
  /** Short headline -- a few words, sentence case. */
  title: string;
  /** One or two sentences. What it is and why you'd reach for it. */
  body: string;
  /** Optional keyboard hint, rendered as an inline keycap (e.g. "ctrl+k"). */
  hint?: string;
}

export interface WhatsNewRelease {
  /** Exact release version, e.g. "0.89.0" (no pre-release suffix). */
  version: string;
  items: WhatsNewItem[];
}

export const WHATS_NEW: WhatsNewRelease[] = [
  {
    version: "0.89.0",
    items: [
      {
        title: "Command palette",
        body:
          "One keystroke to jump to any page, switch the active project, toggle the theme " +
          "or open the Swagger docs. It resolves locally, so the list never waits on a request.",
        hint: "ctrl+k / ⌘k",
      },
      {
        title: "Tokens page",
        body:
          "Create, rotate and revoke scoped Storage tokens without the web UI. The secret is " +
          "shown once at mint; the opt-in \"derive last-used\" pass sorts dormant tokens first, " +
          "so reading order is cleanup order.",
      },
      {
        title: "Trash & restore for configs",
        body:
          "Deleting a configuration is soft. The Configs page has a Trash tab listing what was " +
          "deleted, with per-row restore -- no more digging through the API to undo a mistake.",
      },
      {
        title: "Re-run and terminate jobs",
        body:
          "Both actions are available straight from the Jobs table and from the job drawer. " +
          "A re-run preserves the job's original branch, so a dev-branch job never silently " +
          "re-fires against production.",
      },
      {
        title: "Editable column descriptions + table layout",
        body:
          "Click any description in a table's Schema tab to edit it in place -- written through " +
          "the native endpoint the UI, the MCP server and the warehouse all read. The Info tab " +
          "now also shows BigQuery partitioning and clustering.",
      },
      {
        title: "PAYG credits tile",
        body:
          "The Dashboard shows remaining credits and minutes for the active project, so you " +
          "notice a draining balance before a job queue does.",
      },
      {
        title: "Flow notifications",
        body:
          "Every flow gets a read-only Notifications tab listing who actually gets paged, " +
          "including the project-wide catch-all subscriptions that fire for every job.",
      },
    ],
  },
];

/**
 * Look up the curated entry for a running kbagent version.
 *
 * Matching is exact on the release version, with one tolerance: a PEP 440
 * pre-release suffix on the RUNNING version is stripped before comparing, so
 * someone on `0.89.0b1` / `0.89.0rc2` / `0.89.0a1` sees the `0.89.0` reel.
 * Entries themselves are always keyed by the plain release version.
 */
export function whatsNewFor(version: string | undefined): WhatsNewRelease | undefined {
  if (!version) return undefined;
  const base = version.trim().replace(/(a|b|rc)\d+$/i, "");
  return WHATS_NEW.find((r) => r.version === base);
}
