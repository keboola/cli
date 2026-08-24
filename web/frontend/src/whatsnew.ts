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
 * A release with no entry of its own is NOT silent: `whatsNewFor` falls back
 * to the newest entry at or below the running version, so users still get the
 * most recent curated reel (capped at one showing each by the seen-marker).
 * Silence happens only when NO entry is <= the running version -- i.e. before
 * the first curated release. See `whatsNewFor` for why exact matching would
 * make the feature ship dark.
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
    version: "0.90.1",
    items: [
      {
        title: "All your jobs, one feed",
        body:
          "The Jobs page now has an All projects button that merges every registered " +
          "project into a single chronological feed \u2014 with a project column, credit " +
          "estimates per row and a running total over whatever the filter is showing. " +
          "The detail drawer, re-run and terminate all work from there.",
      },
      {
        title: "Audit every token at once",
        body:
          "Tokens gained the same cross-project view: one read-only list over all projects, " +
          "dormant tokens sorted first so reading order is cleanup order. Click a row to " +
          "jump into that project, where mint, rotate and revoke live.",
      },
      {
        title: "Search that says what it skipped",
        body:
          "The Search page is now a proper console \u2014 one input, a names/config-bodies " +
          "toggle and type filter pills. Projects it could not search (expired session, " +
          "missing feature) are called out instead of silently rendering zero hits, and " +
          "every result deep-links to where it lives.",
      },
      {
        title: "A changelog you can skim",
        body:
          "The Changelog page reads as a release timeline: each note collapses to its " +
          "headline, badges tell New from Fix from BREAKING at a glance, PR numbers link " +
          "out, and the version you are running is marked on the rail.",
      },
    ],
  },
  {
    version: "0.90.0",
    items: [
      {
        title: "Every view has a link",
        body:
          "The URL now tracks the page, project, branch and whatever you have open, so the " +
          "exact state you are looking at can be pasted to a colleague. Back and Forward " +
          "walk pages, and a shared link reopens the drawer on a cold load.",
      },
      {
        title: "The palette finds your data",
        body:
          "Ctrl+K searches storage buckets and tables across every registered project, not " +
          "just pages and actions. Enter lands on the object with its filter applied, " +
          "switching project first if it lives elsewhere. Still nothing waits on the network " +
          "while you type.",
        hint: "ctrl+k / \u2318k",
      },
      {
        title: "Details you can read",
        body:
          "Projects, configs, components, data apps and jobs answer a click with a rendered " +
          "overview instead of a JSON dump \u2014 and keep the untouched payload one tab away, " +
          "so nothing is hidden.",
      },
      {
        title: "Run and terminate jobs",
        body:
          "Re-run a job or start one straight from a configuration, and terminate anything " +
          "still queued or running. A re-run uses the configuration as it stands now.",
      },
      {
        title: "Deleting a config is undoable",
        body:
          "Configs now have a Trash tab listing what was deleted, with per-row restore. " +
          "Delete is soft, and the confirm dialog says so.",
      },
      {
        title: "Tokens, without the web UI",
        body:
          "A new Tokens page creates, rotates and revokes scoped Storage tokens. The secret " +
          "is shown once at mint; the opt-in last-used pass sorts dormant tokens first, so " +
          "reading order is cleanup order.",
      },
      {
        title: "Your session survives a restart",
        body:
          "Restarting the server no longer leaves a tab quietly showing empty lists. The UI " +
          "now re-authenticates itself on the first rejected request, and tells you plainly " +
          "when it genuinely cannot.",
      },
    ],
  },
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

/** Compare two plain `X.Y.Z` versions. Negative when `a` sorts before `b`. */
function compareVersions(a: string, b: string): number {
  const pa = a.split(".").map((n) => Number.parseInt(n, 10) || 0);
  const pb = b.split(".").map((n) => Number.parseInt(n, 10) || 0);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const diff = (pa[i] ?? 0) - (pb[i] ?? 0);
    if (diff !== 0) return diff;
  }
  return 0;
}

/**
 * Pick the curated reel to show on a given running kbagent version: the
 * NEWEST entry at or below it.
 *
 * Not an exact match, deliberately. An exact match makes the feature ship
 * dark: this popup first runs in the release AFTER the one whose highlights
 * seeded the list, so on day one the running version would have no entry and
 * nobody would ever see a reel. "Newest entry <= running version" also
 * degrades correctly in every other direction -- a user who skipped a release
 * still gets the most recent curated reel rather than nothing, and once a
 * release PR adds an entry for the version actually shipping, that entry wins
 * immediately. The seen-marker still caps it at one showing per reel.
 *
 * A PEP 440 pre-release suffix on the RUNNING version is stripped before
 * comparing, so `0.90.0b1` is treated as `0.90.0`. Entries themselves are
 * always keyed by the plain release version.
 */
export function whatsNewFor(version: string | undefined): WhatsNewRelease | undefined {
  if (!version) return undefined;
  const base = version.trim().replace(/(a|b|rc)\d+$/i, "");
  let best: WhatsNewRelease | undefined;
  for (const release of WHATS_NEW) {
    if (compareVersions(release.version, base) > 0) continue;
    if (!best || compareVersions(release.version, best.version) > 0) best = release;
  }
  return best;
}
