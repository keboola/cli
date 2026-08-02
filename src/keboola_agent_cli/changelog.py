"""Changelog data for kbagent releases.

Run ``make changelog`` to scaffold new entries from GitHub releases.

Authoring contract (keep entries scannable -- ``kbagent changelog`` shows a
one-line summary per version by default):

* One *logical* change per bullet -- split a release into several entries
  instead of cramming everything into one paragraph.
* Start each bullet with a recognised prefix so the renderer can colour it and
  ``headline()`` can summarise it: ``BREAKING:``, ``New:``, ``Fix:``,
  ``Change:``, ``Note:``, ``Security:``, ``UX:`` ... (see
  ``commands/changelog.py:_PREFIX_STYLES``). The prefix may carry a ``(#274)``
  decoration.
* Lead with a self-contained first sentence -- that sentence becomes the
  default summary; everything after it is detail shown only under ``--full``.
"""

from __future__ import annotations

import re

from .constants import CHANGELOG_HEADLINE_MAX_CHARS

# Ordered newest-first.  Each value is a list of brief one-line descriptions.
CHANGELOG: dict[str, list[str]] = {
    "0.79.0": [
        "Fix: the standalone `kbagent` binary no longer tries to update itself with "
        "`uv tool install`. kbagent ships both as a Python distribution and as a "
        "self-contained PyInstaller binary delivered by Chocolatey, WinGet, Homebrew, apt, "
        "dnf or a signed zip, but nothing detected the difference -- so both update paths "
        "planned a uv/pip reinstall, which cannot upgrade a package-manager-owned binary. "
        "It installs a SECOND, unrelated kbagent into the uv tool directory, which usually "
        "precedes the package manager's directory on PATH: the user silently starts running "
        "a different install than the one `choco` / `brew` / `apt` tracks, while the real "
        "binary stays stale. With no Python on the machine it simply fails on every startup. "
        "`sys.frozen` / `sys._MEIPASS` is now detected explicitly and the channel is "
        "identified from the running binary's own path, so the self-update is replaced by a "
        "notification carrying the command that channel actually accepts -- "
        "`choco upgrade keboola-cli2`, `winget upgrade Keboola.KeboolaCLI2`, "
        "`brew upgrade keboola-cli2`, `sudo apt-get install --only-upgrade keboola-cli2`, "
        "`sudo dnf upgrade keboola-cli2`, or the GitHub release page for a hand-unpacked "
        "archive. An unattributable path degrades to the release page rather than guessing. "
        "This covers the deferred Windows helper added in 0.78.0 too: the guard sits ahead "
        "of the `should_defer()` branch, so a frozen binary is never scheduled for an "
        "install it cannot receive. uv / pip installs behave exactly as before.",
        "Fix: a frozen binary is no longer mistaken for a developer checkout, which had "
        "been silently disabling the startup update check inside every shipped artifact. "
        "The release workflow freezes with `pyinstaller --collect-all keboola_agent_cli`, "
        "and `collect_all()` is a superset of `--copy-metadata`, so the whole `.dist-info` "
        "is bundled -- including `direct_url.json`, which records how the project was "
        "installed on the BUILD machine. CI freezes from an editable `uv run` sync, so that "
        'file says `"editable": true` inside every released binary and the dev-install '
        "probe returned True for all of them. A frozen build is now never treated as a dev "
        "tree, whatever its bundled metadata claims.",
        "New: `kbagent update` on a standalone binary reports the channel's own upgrade "
        "command instead of running uv, and no longer misreports that deliberate refusal as "
        "a failed update. `kbagent version` advertises the same command in place of "
        "`(run: kbagent update)`. Both add `install_channel` and `upgrade_hint` keys under "
        "`kbagent` in `--json`; `upgrade_command` stays runnable-or-empty (it is empty for a "
        "hand-unpacked archive, where the sentence lives in `upgrade_hint`), so a consumer "
        "shelling out to it never executes prose. All three keys are absent for uv / pip "
        "installs, leaving their JSON shape byte-identical.",
        "Note: `keboola-mcp-server` still auto-updates on a frozen build, by design. It is a "
        "separate Python distribution that the binary only ever spawns as a subprocess, so "
        "upgrading it neither touches nor depends on the frozen kbagent. A pure-binary user "
        "with no Python tooling is unaffected either way -- install-method detection returns "
        "`none` and the stage does nothing.",
    ],
    "0.78.0": [
        "Fix (#546): `kbagent --json` no longer crashes with `UnicodeEncodeError` on Windows "
        "consoles using a non-UTF-8 codepage (cp1250 on Czech/Polish/Hungarian Windows). Any "
        "non-ASCII character in the data -- an arrow in a flow name was the report -- made "
        "machine-readable output unusable, because pydantic's `model_dump_json` emits raw UTF-8 "
        "and `sys.stdout` then encoded it through the console codepage. All machine output, "
        "including the `--stream` NDJSON from `kbagent agent run`, is now written as UTF-8 bytes "
        "independent of the console. The `PYTHONUTF8=1` workaround is no longer needed. JSON "
        "lines now end LF rather than CRLF on Windows. Thanks to @MichalProchazka for the report.",
        "Fix (#528): the Windows self-update no longer corrupts the uv tool environment. "
        "`uv tool install` recreates a tool environment by REMOVING it and then building a fresh "
        "venv at the same path -- it is not atomic and has no rollback. On POSIX that is harmless, "
        "but on Windows uv's `kbagent.exe` trampoline holds the venv interpreter locked, so the "
        "removal deletes what it can, hits a locked file, and aborts -- leaving a gutted venv "
        "(`No module named 'rich._windows'`, `cannot import name 'rich_utils' from 'typer'`). "
        "The v0.76.2 fix reordered the update but still ran the install from inside the "
        "environment being replaced, so the corruption survived it. kbagent now hands the "
        "reinstall to a detached helper that waits until every kbagent process has exited and "
        "only then installs; the outcome (including a copy-paste recovery command on failure) is "
        "reported on the next launch. POSIX keeps the proven inline install plus re-exec. "
        "Thanks to @papousek-radan for three rounds of precise Windows reports.",
        "Fix (#528): a slow install is no longer killed mid-write. Every update path used "
        "`subprocess.run(timeout=...)`, which terminates the child when the deadline passes -- on "
        "Windows a hard `TerminateProcess` of uv part-way through recreating a venv, producing "
        "exactly the same half-deleted environment a file lock does. The deadline now bounds only "
        "how long kbagent waits; the installer is left to finish the transaction it started, and "
        "the banner says so instead of offering a recovery command that would start a second "
        "installer against the same environment. This covers the keboola-mcp-server upgrade too: "
        "that environment is not the one kbagent runs from, so no lock is involved, but a killed "
        "installer leaves it just as broken and `kbagent tool call` is what stops working. "
        "Read-only version probes still time out normally -- killing a probe is harmless.",
        "New (#528): `KBAGENT_DEFER_UPDATE` forces the out-of-process update path on (`1`) or off "
        "(`0`), overriding the platform default. Intended for reproducing the deferred flow on "
        "POSIX and as an escape hatch on Windows.",
    ],
    "0.77.0": [
        "New (#505): `kbagent config update` and `kbagent config row-update` accept "
        "`--change-description TEXT`, which writes the new config version's "
        "`changeDescription` -- the version-history audit line -- instead of the generic "
        "auto-generated default. On shared production configs that history is the paper "
        "trail, so a change can now say *why* it happened. Omitting the flag preserves the "
        "previous default verbatim, and `--dry-run` echoes the description that would be "
        "sent (`change_description` in `--json`). Note this is distinct from `--description`, "
        "which sets the configuration's display description. Both `kbagent serve` PATCH "
        "routes mirror the flag. Thanks to @jordanrburger.",
    ],
    "0.76.3": [
        "Security: bump npm dependencies flagged by Dependabot in `web/backend` and "
        "`web/frontend` -- `@fastify/static` (path traversal / auth bypass), "
        "`brace-expansion` and `find-my-way` (DoS), `postcss` (source-map path traversal), "
        "and `dompurify` (XSS/sanitization bypass, pinned via an npm override on the "
        "`monaco-editor`/`mermaid` transitive copy). No behavior change.",
    ],
    "0.76.2": [
        "Fix (#528, #530): self-update no longer risks leaving the running Windows uv tool "
        "environment partially upgraded. kbagent now completes all network checks and command "
        "planning before mutation, upgrades the independent keboola-mcp-server environment "
        "first, caches the result, then performs an exact-version full reinstall as the terminal "
        "step and immediately re-executes. Failed or timed-out updates print a copy-paste recovery "
        "command instead of continuing without repair guidance. Thanks to @papousek-radan for "
        "the detailed corruption reports.",
        "Fix (#529, #531): `kbagent semantic-layer export` now writes snapshots on Windows. "
        "The writer conditionally enables platform-specific `O_NOFOLLOW` and `O_BINARY` flags, "
        "so Windows no longer raises `AttributeError` while POSIX keeps its existing final-path "
        "symlink protection; the real Windows export path is covered by CI. Thanks to "
        "@papousek-radan for the report.",
    ],
    "0.76.1": [
        "Fix (#522, #526): `kbagent serve --ui` no longer crashes on startup on Windows "
        "consoles with a non-UTF-8 codepage (cp1250 on Czech/Polish/Hungarian Windows). The "
        "startup banner's box-drawing glyphs (`├─` / `└─`) raised `UnicodeEncodeError` from "
        "`sys.stdout.write` before uvicorn bound the port, so the server never started. They "
        "now degrade to ASCII (`|-` / `` `- ``) when the console can't encode them -- the same "
        "UTF-8/ASCII fallback `install.sh` already uses -- with a belt-and-braces `try/except` "
        "so a cosmetic banner can never abort startup. Modern UTF-8 terminals are unchanged. "
        "The `set PYTHONUTF8=1` workaround is no longer needed. Thanks to @papousek-radan for "
        "the detailed report.",
    ],
    "0.76.0": [
        "Change (#520): the ~3,960-LOC `client.py` is split into a `client/` package by "
        "endpoint family (storage tables/files, configs, queue, tokens, branches, stream, "
        "query, workspaces, misc, plus `_core`/`_transfer`). `KeboolaClient` stays a single "
        "class composed from per-family mixins, so the public import surface and the SDK "
        "`Client.raw` contract are byte-identical -- verified as a pure move (every method "
        "body unchanged) plus a full live E2E pass.",
        "Note: this is an internal-only release -- no user-facing behavior changes. It bundles "
        "the client refactor above with E2E test-suite hardening; every `client/*.py` module is "
        "under the CONTRIBUTING.md file-size ceiling, and new HTTP methods now go into the "
        "relevant `client/*.py` mixin instead of the old monolith.",
        "Fix (tests, #521 #523): repaired the long-standing nightly E2E flakes -- clone "
        "(missing bucket create on the default branch), config-secret (response-envelope path), "
        "swap + file-list (read-after-write / read-after-DDL eventual consistency, now polled), "
        "and stream (unique per-run source name to dodge a wedged orphan). The nightly E2E is "
        "green again after ~7 weeks.",
    ],
    "0.75.0": [
        "New (#512): table snapshots -- `kbagent storage table-from-snapshot` creates a NEW "
        "table from an existing snapshot (restore), plus the full lifecycle: "
        "`snapshot-create`, `snapshots` (list), `snapshot-detail`, `snapshot-delete`. "
        "A snapshot captures a table's data, columns, and primary key at a point in time; "
        "restore rebuilds them into a fresh table in any existing bucket.",
        "Note (#512): `table-from-snapshot --name` is REQUIRED -- the live API rejects an "
        "omitted/empty name (the reference PHP client's 'defaults to snapshot name' docblock "
        "is stale). Restore goes through the classic `tables-async` endpoint, not "
        "`tables-definition`, which is why it is a dedicated command instead of a "
        "`create-table` flag. No overwrite semantics: restore under a new name, verify, then "
        "swap or delete the old table yourself.",
        "Permissions: `storage.snapshots`/`snapshot-detail` classify as read, "
        "`snapshot-create`/`table-from-snapshot` as write, `snapshot-delete` as destructive "
        "(it forecloses restores; source tables are untouched).",
    ],
    "0.74.0": [
        "MCP passthrough deprecation (#478 phase 2, epic #390): `tool call` / `tool list` "
        "/ `agent --type mcp_tool` are now formally deprecated in favor of native commands. "
        "Nothing breaks yet -- everything keeps working through the deprecation window.",
        "`tool call` warns with the EXACT native replacement for the tool being called "
        "(stderr in human mode; additive `deprecation` key in the `--json` envelope). "
        "`tool list` gains a `cli_equivalent` column/field sourced from the new parity map.",
        "New: `src/keboola_agent_cli/mcp_parity.py` -- the tool->command parity map as code, "
        "with offline tests pinning every entry to a registered CLI operation, and a weekly "
        "`mcp-parity-canary` GitHub workflow (`make parity-check`) that diffs the live "
        "keboola-mcp-server catalog against it so a new upstream tool turns the canary red "
        "instead of silently widening the gap.",
        "`agent create/update/test --type mcp_tool` warn and point at `--type cli_command` "
        "with the native command; existing mcp_tool tasks keep running unchanged.",
        "Serve: `/mcp/tools*` routes are marked deprecated in OpenAPI "
        "(`/mcp/server-status` stays -- it reports embedded-server health).",
    ],
    "0.73.0": [
        "MCP parity + fail-closed firewall (#478 phase 0, epic #390 phase 1): six native "
        "commands port the remaining keboola-mcp-server tools, and MCP tool classification "
        "fails closed.",
        "Security (#478): unknown MCP tool names now classify as `destructive` (strictest) "
        "instead of falling through to `read`. Catalog tools `run_job`, `run_sync_action`, "
        "`modify_*`, `deploy_*` move from read to write -- they previously passed "
        "`--deny-writes` and fanned out to every configured project. Multi-project dispatch "
        "is fail-closed too: only known-read tools fan out.",
        "Security (#478): `tool call` now enforces the SESSION firewall per tool name -- "
        "`--deny-destructive` blocks `tool call delete_bucket` (previously only the persisted "
        "policy was checked at tool granularity).",
        'New (#392): `kbagent docs query "QUESTION"` -- answers from the Keboola '
        "documentation via the AI Service (ports `docs_query`).",
        "New (#393): `kbagent config examples --component-id ID` -- sample root/row "
        "configurations for a component (ports `get_config_examples`).",
        "New (#394): `kbagent semantic-layer schema --type metric,dataset,...` -- live JSON "
        "schemas of semantic object types from the metastore (ports `get_semantic_schema`).",
        "New (#395): `kbagent component sync-action ACTION` -- run synchronous component "
        "actions like testConnection (ports `run_sync_action`; shallow root+row config merge "
        "identical to the MCP tool).",
        "New (#396): `kbagent transformation create|show|edit` -- SQL transformation "
        "authoring with the 9-op block/code edit engine (ports create/update_sql_"
        "transformation; synthetic b{i}/b{i}.c{j} ids, dialect from project default_backend).",
        "New (#397): `kbagent flow examples` + `flow schema` now serves the authoritative "
        "bundled conditional-flow schema (ports `get_flow_examples`; fixes schema drift).",
    ],
    "0.72.0": [
        "Sync trust cluster (#466, #467, #472, #497): four reliability fixes that make "
        "`kbagent sync` safe to run against production trees edited by other people.",
        'New: `sync pull --theirs` (#466) -- the supported "discard local changes, take '
        'production" reconcile path. Overwrites locally-modified configs and rows, restores '
        "deleted/missing files, and resolves true merge conflicts by taking the remote version "
        "instead of aborting. No more hand-editing `.keboola/manifest.json` to reconcile a "
        "drifted tree; the SYNC_CONFLICT error message now points at it.",
        "Fixed (#472): `sync push --force` can no longer plan a DELETE of a remote config "
        "that was never fetched. A manifest entry with an empty `pull_hash` and no local files "
        "(a phantom left by a pre-0.72 name-collision pull) is excluded from delete planning "
        "and surfaced as a `never_fetched` warning on diff/push; the next `sync pull` "
        "materializes it. Deleting a properly-pulled config locally still deletes on push.",
        "Fixed (#466/#472): `sync pull` enforces the manifest<->disk invariant -- a tracked "
        "config whose local directory was deleted is re-materialized on the next pull even "
        'when the remote is unchanged (previously: silent "Already up to date"), and pull '
        "can no longer register a manifest entry without writing its files.",
        "New (#467): config-level `isDisabled` round-trips through sync. Pull writes a sparse "
        "`is_disabled: true` into `_config.yml` (absent key = enabled, so existing trees do "
        "not mass-diff), `sync diff` surfaces enabled/disabled drift (a flow disabled in "
        'production no longer reports "in sync"), and push updates the remote state when '
        "the key is present (absent key leaves remote untouched). Rows too; `config new "
        "--push`/`sync clone` create disabled configs when the local file says so.",
        "Fixed (#497): pushing an untracked local config whose `_keboola.config_id` resolves "
        "on the target branch (adopted-by-id update, #482) now writes the manifest entry with "
        "fresh hashes, so follow-up diffs are stable and a later local deletion is detected.",
        '`sync status` all-clear output now says "No local changes detected ... Local check '
        'only" and points at `sync diff` -- status never contacts the API, so it cannot see '
        "remote drift (#466).",
    ],
    "0.71.0": [
        "Note: catch-up release -- the first published release since v0.66.1. Versions 0.67.0 "
        "through 0.70.1 were merged to main with their changelog entries but never tagged or "
        "published (no GitHub Release, so auto-update never picked them up); updating to 0.71.0 "
        "ships everything listed under those versions in one step.",
        "New (web UI): the `kbagent serve --ui` table detail now has a **Repartition** tab "
        "for BigQuery tables. Pick a time or integer-range partitioning layout plus optional "
        "clustering fields, and the UI copies the table into the new layout (`create-table "
        "--source-table-id`) and atomically swaps it into place (`swap-tables`) -- the same "
        "supported repartition flow as the CLI. After the swap it offers to delete the leftover "
        "old table. Runs in the branch selected in the top bar; with no dev branch selected it "
        "repartitions production behind an explicit confirm.",
        "The `serve` create-table endpoint (`POST /storage/tables/{project}`) now forwards the "
        "source-copy and BigQuery partition/clustering fields (`source_table_id`, "
        "`time_partitioning_*`, `range_partitioning_*`, `clustering_fields`) and makes `columns` "
        "optional, matching the CLI. Table detail responses now include the owning bucket's "
        "`backend` so the UI can gate BigQuery-only features.",
        "New (#500): every GitHub Release now ships a `command-reference.md` asset generated "
        "from the live Typer command tree of the exact built wheel (238 commands, 27 groups) -- "
        "a zero-drift command reference that cannot disagree with the shipped CLI. Locally: "
        "`make gen-command-reference`.",
    ],
    "0.70.1": [
        "Fix: config.json reliability hardening (issue #477). Every rewrite now first copies the "
        "previous config.json to `config.json.bak` (0600, same directory), so stored project "
        "tokens are recoverable if the config is ever lost or clobbered -- no more re-entering "
        "tokens by hand. The backup is best-effort and never blocks the save.",
        "Fix: file locking moved from config.json itself to a sidecar `config.json.lock`. "
        "Pre-fix, `save()` opened config.json with O_CREAT to lock it, so a save that failed "
        "before the atomic rename left behind an empty 0-byte config.json that broke the next "
        "load with 'not valid JSON'. A failed save now leaves no artifact at all (the temp file "
        "is cleaned up too) and the previous config survives intact. Side effect: the Windows "
        "close-before-replace special case is gone -- the lock fd never points at config.json.",
        "Fix: config mutations are now transactional. `ConfigStore.transaction()` holds the "
        "exclusive lock across the whole load -> mutate -> save cycle (reentrant per thread), "
        "closing the lost-update race where two concurrent kbagent processes doing "
        "read-modify-write silently dropped each other's changes -- e.g. a freshly added "
        "project vanishing from config.json when another command saved a stale snapshot. All "
        "ConfigStore mutation methods, `permissions set/reset`, the default-project pin, and "
        "the org-metadata backfill now run inside a transaction.",
        "Fix (#482): `sync push` on a dev branch no longer creates duplicates of configs "
        "inherited from main. After `branch use <dev>` + `sync pull`, the manifest is replaced "
        "with the dev branch's entries, orphaning the previously pulled `main/` tree on disk; "
        "the untracked-config walker kept that tree in scope, so every inherited config "
        "surfaced as `added` (empty config_id) and a push CREATEd a duplicate on the branch. "
        "diff/push now scan for untracked configs only in the resolved source-branch subtree, "
        "and an untracked config whose id resolves on the target branch and is unclaimed by "
        "the manifest diffs against the existing remote config (adopt-by-id) instead of "
        "creating a duplicate.",
        "New (#487): `semantic-layer build` resolves column types for alias / linked-bucket "
        "tables from the warehouse INFORMATION_SCHEMA, fixing their all-`dimension` field "
        "classification (an alias table carries no per-column datatype metadata in Storage, so "
        "the role heuristic saw empty types). Pass `--types-workspace ID` explicitly or "
        "`--auto-types-workspace` to auto-pick a read-only Query-Service-capable workspace per "
        "backend; the `serve` UI build auto-resolves by default. A table with no resolvable "
        "workspace is reported non-fatally in `type_resolution_errors` and the build proceeds.",
        "New (#488): `semantic-layer build` emits one metric per `measure` field instead of a "
        "single COUNT(*) placeholder per table. The aggregate is guessed from the column name "
        "(avg/mean/rate/pct/percent/ratio/share -> AVG; max/peak -> MAX; min -> MIN; default "
        "SUM), mirroring the semantic-layer toolkit's generator.",
        "Improved (#492): cloud-storage upload failures now surface the provider's short error "
        "code -- `Cloud storage upload failed (HTTP 403, AccessDenied)` instead of a bare "
        "`HTTP 403` -- and `--verbose` logs the raw provider error body (truncated to 1500 "
        "chars) at DEBUG before raising. Applies to `storage file-upload`, `storage "
        "upload-table`, and every other upload path through the shared cloud-upload helper.",
        "Fix (#495): names containing square brackets are no longer swallowed in human-mode "
        "output. A project named `[e2e] - kbagent bigquery` rendered as ` - kbagent bigquery` "
        "(`[e2e]` was parsed as a Rich markup tag) in `project add`/`list`/`status`/`info` and "
        "storage tables; user- and API-sourced names are now escaped via `rich.markup.escape()` "
        "across the project and storage groups. `--json` output always carried the correct "
        "value.",
        "Fix (#493, issue #447): the kbagent Claude Code skill loads again in Claude Desktop. "
        "The SKILL.md frontmatter `description` had grown to 5069 characters; the Agent Skills "
        "spec caps it at 1024 and Claude Desktop enforces the cap at load time, so the skill "
        "failed to load entirely. Rewritten to 965 characters, still naming every command "
        "domain and the highest-value trigger keywords.",
        "Improved: `Project '<alias>' not found` errors now name the RESOLVED config file and "
        "its source, e.g. `Project 'x' not found in /home/u/.kbagent/config.json (source: "
        "local). Run 'kbagent project list' to see configured projects.` Config resolution is "
        "cwd- and env-dependent (--config-dir > KBAGENT_CONFIG_DIR > nearest .kbagent/ walking "
        "up from cwd > global), so two shells can silently talk to different configs; the "
        "enriched error makes that split-brain visible instead of opaque (issue #477).",
    ],
    "0.70.0": [
        "BREAKING: Removed `data-app git-branches` and `data-app git-entrypoints` (and their "
        "`kbagent serve` endpoints). The sandboxes-service backend dropped the underlying "
        "`GET /apps/{id}/git-repo/branches` and `/git-repo/entrypoints` endpoints -- they cannot "
        "work for managed git repos and will be reworked later via git-service. This CLI was the "
        "sole remaining consumer. `data-app git-repo` (clone-URL introspection) and the "
        "`git-credentials` / `git-credentials-create` commands are unchanged.",
        "Fix (#486): `semantic-layer build` and `add dataset --deep-fields` no longer classify "
        "numeric measure columns as `dimension`. Field-role classification now normalizes "
        "column types before matching, so Keboola's `NUMERIC` basetype and BigQuery-native "
        "`INT64`/`FLOAT64`/`BIGNUMERIC` count as numeric (-> `measure`) and temporal variants "
        "map to `timestamp`, on both Snowflake and BigQuery.",
    ],
    "0.69.0": [
        "New: `kbagent search --regex` opts into regex mode on the global-search endpoint "
        "(DMD-1716). Forwards `mode=regex` to the Storage API -- a case-insensitive whole-term "
        "match against ENTITY NAMES only (`report` does not match `monthly_report`; use "
        "`.*report.*`). Textual-search only: combining it with `--search-type config-based` is a "
        "usage error. Regex does NOT match column names, so `matched_columns` is always empty "
        "under `--regex`.",
        "New: textual search results now report which column names matched (DMD-1717). Table "
        "results matched via a column name surface the API's `matchedColumns`: a `matched_columns` "
        "field on every result in `--json` (always present; `[]` when the entity name itself "
        'matched) and a "Matched columns" column in the human table, shown only when at least one '
        "result actually matched via a column, so it never adds an empty column.",
        "Note: the Global Search re-architecture's rebuilt index / ranking upgrade is server-side "
        "and flows through the CLI unchanged. Both new contracts were verified live against a "
        "real stack before release.",
    ],
    "0.68.0": [
        "New: bulk-remove projects from the `kbagent serve` Web UI. The Projects table now has "
        "per-row checkboxes plus a select-all header, and a `Remove from kbagent` action that "
        "unregisters several projects at once. A styled confirmation modal lists the affected "
        "aliases and makes clear this only edits the local kbagent config -- it does NOT delete "
        "the Keboola projects. Backed by a new `POST /projects/bulk-delete` REST endpoint "
        "(`ProjectService.bulk_remove_projects`) with per-alias error accumulation and a "
        "`dry_run` mode; one bad alias never blocks the rest.",
    ],
    "0.67.0": [
        "New: `storage create-table` can copy from an existing table and apply a "
        "BigQuery partition/clustering layout. `--source-table-id` (with optional "
        "`--source-branch-id`) derives the new table's schema from a source table and "
        "copies its rows into the requested layout -- the supported way to repartition "
        "a populated BigQuery table, then flip it into place with `storage swap-tables`. "
        "`--column` is now optional and mutually exclusive with `--source-table-id`. "
        "New layout flags (also usable on a plain columns create): "
        "`--time-partitioning-type`/`-field`/`-expiration-ms`, `--range-partitioning-field`/"
        "`-start`/`-end`/`-interval`, and `--clustering-field` (repeatable). Time and range "
        "partitioning are mutually exclusive. Mirrors keboola/connection#7697.",
        "Note: the source-copy and partition/clustering flags are BigQuery-only. "
        "`create-table` runs a one-call backend pre-flight (token verify) when any of them "
        "is used and fails fast with a clear message on a non-BigQuery project, before "
        "issuing the create. A plain columns create is unaffected (no extra call).",
        "New (#465): secret-write commands' `--json` envelopes now carry a `plaintext_written` "
        "field -- the list of secret key-paths (never values) left in plaintext when "
        "`--allow-plaintext-on-encrypt-failure` fell back; `[]` when encryption succeeded. "
        "Covers `config update` / `config new --push` / `config row-create` / `config "
        "row-update` / `config variables-set` and `data-app create` / `data-app secrets-set`. "
        "GHSA-7jrf follow-up: agents and scripts see the leak in the envelope, not only in the "
        "stderr warning.",
        "Note (#490): `docs/error-codes.md` now documents all 66 `ErrorCode` members (was 46 -- "
        "everything added since ~0.22.0 was missing: Flow, Data Apps, Developer Portal, the "
        "`kbagent serve` envelope codes, and the newer Sync/Auth codes). A new `make "
        "check-error-codes` gate keeps the doc in lockstep with the enum.",
    ],
    "0.66.1": [
        "Fix (#479): `flow schedule` now activates the schedule on the Scheduler Service, so the cron "
        "trigger actually fires. Previously the command only wrote the `keboola.scheduler` Storage "
        "config; the schedule looked `enabled` but never ran until re-saved in the UI. The command "
        "now calls `POST /schedules` on the Scheduler Service after the config upsert (also for "
        "`--disabled`, which deregisters the trigger). An activation failure -- e.g. a token without "
        "the schedule-management privilege -- keeps the config written, reports `activated: false` + "
        "a warning, and exits 0. Schedules created by older kbagent versions stay dormant until "
        "`flow schedule` is re-run on 0.66.1+.",
        "Fix (#479): `flow schedule-remove` now deregisters each schedule from the Scheduler Service "
        "(`DELETE /configurations/{id}`) before deleting its Storage config, so removed schedules "
        "stop firing. Deregistration failures other than 404 are surfaced as warnings and do not "
        "block the config deletion.",
    ],
    "0.66.0": [
        "New: device-enrollment primitives on the importable library -- a hosted Data App can now mint "
        "per-device credentials in-process (no CLI subprocess, no master token on the device). "
        "`Client(url, token)` / `.raw` (`KeboolaClient`) gains `create_scoped_token`, `delete_token`, "
        "`refresh_token`, and per-device Data Streams `create_stream_source` / `get_stream_source` / "
        "`list_stream_sources` / `delete_stream_source`. The facade returns typed `ScopedTokenResult` / "
        "`StreamSourceResult` (exported from the package root); `.raw` returns plain dicts. Built for "
        "keboola/jasnost device enrollment (ADR 0005).",
        "New: `create_scoped_token(description, bucket_permissions, component_access, "
        "can_read_all_file_uploads, expires_in)` generalises `create_short_lived_token` beyond "
        "component scope -- it maps straight to `POST /v2/storage/tokens` so you can mint the narrow "
        "'upload Files + write one sink bucket, expiring' token a capture device needs (the Keboola "
        "single-bucket-write pattern). The acting token must carry `canManageTokens`.",
        "New: `create_stream_source` provisions a per-device OTLP source AND (for `otlp`, default) its "
        "logs/metrics/traces sinks + `in.c-otlp-<sourceId>` bucket, and returns `sink_bucket_id` so the "
        "scoped device token can be granted write on exactly that bucket. Per-device sources are the "
        "unit of isolated event-plane revocation (`delete_stream_source`) -- no shared-secret rotation. "
        "`otlp_url` carries the ingest secret unmasked (revealed to the device once, never persisted).",
        "New: `kbagent token` command group (create | delete | refresh) mirrors the scoped-token "
        "primitives on the CLI (Project Management). `token create` prints the secret once; `token "
        "delete` revokes immediately; `token refresh` rotates and invalidates the old value. These are "
        "Storage-API operations (no manage token).",
        "Note: contrary to earlier assumptions, a Data Streams source create is authenticated with a "
        "normal per-project Storage token (NOT a master token) and there is no `masterTokenRequired` "
        "error code; and a Files upload is NOT gated by `componentAccess` / `canReadAllFileUploads` -- "
        "any valid Storage token can upload its own Files. `canReadAllFileUploads` only widens reading "
        "files uploaded by OTHER tokens.",
    ],
    "0.65.1": [
        "BREAKING: Removed `data-app git-bind-credential` (and its `kbagent serve` endpoint). It shipped in "
        "0.65.0 on a misdiagnosis: managed-repo deploys were failing and we believed the platform "
        "did not inject the git-clone credential, so the command wired an encrypted credential into "
        "`parameters.dataApp.git`. A clean reproduction on `data-science.us-east4.gcp` confirmed the "
        "platform DOES inject the clone credential at deploy time (matching the sandboxes-service "
        "`testManagedGitRepo.sh` contract) -- the command was unnecessary. The real fix was the "
        "0.65.0 `configVersion`-omit change; pinning a managed app's no-git-block config is what made "
        "the runtime demand `dataApp.git.repository` and revert the deploy to stopped.",
        "Change: Corrected the managed-repo guidance everywhere: the canonical flow is `data-app create "
        "--use-managed-git-repo` -> `git-credentials-create --type http_token --permissions readWrite` "
        "+ `git push` your code -> `data-app deploy`. No credential wiring is needed. The earlier "
        "'platform does not inject credentials' / `could not read Username` framing was wrong "
        "(GitHub issue #454 closed as not-a-bug).",
    ],
    "0.65.0": [
        "New: deploy a data app from a Keboola-MANAGED git repository end-to-end. "
        "`data-app create --use-managed-git-repo` provisions an EMPTY Keboola-hosted repo (POST "
        "`useManagedGitRepo:true`, no `parameters.dataApp.git` block, forces `--no-deploy`; "
        "mutually exclusive with `--git-repo` and every `--git-*`/PAT flag). The full flow to a "
        "running app: create -> `git-credentials-create --type http_token --permissions readWrite` "
        "+ `git push` to the managed URL -> `data-app git-bind-credential` -> `data-app deploy`. "
        "Verified live (tic-tac-toe serving from a managed repo).",
        "New: `data-app git-bind-credential --project P --app-id ID [--branch-name main] "
        "[--permissions readOnly|readWrite]` mints an `http_token` ON the app, encrypts it under "
        "the project KMS, and writes `parameters.dataApp.git` (repository + placeholder username + "
        "encrypted `#password` + branch). Required on stacks that do not inject managed-repo "
        "credentials at deploy time -- without it the runtime's `git clone` of the managed repo "
        "fails `could not read Username` and the deploy reverts to stopped. The token is encrypted "
        "in place and never printed; `--dry-run` previews what would be wired without minting the "
        "one-time credential or touching the config. (Removed in 0.65.1 -- this was based on a "
        "misdiagnosis; the platform does inject the clone credential, so the command was "
        "unnecessary. See the 0.65.1 notes.)",
        "New: `data-app runs --project P --app-id ID [--limit N]` lists a data app's recent "
        "deployment attempts with `failure_reason` + `startup_logs`, including setup-phase failures "
        "(e.g. a git-clone error during `app_setup`) that produce no container logs. It works on "
        "never-started / failed apps where `data-app logs` returns HTTP 400 -- the canonical way to "
        "find out why a deploy reverted to `stopped` without the app ever serving.",
        "Fix: `data-app deploy` now resolves the deployed `configVersion` by source location -- it "
        "pins the latest Storage version when a git block is present (external repos AND a "
        "credential-wired managed repo) so the operator reads the current source, and omits "
        "`configVersion` only for a pure managed repo (deploys from `app.managedGitRepoId`). "
        "Previously it always pinned, which pointed managed-repo deploys at a config snapshot with "
        "no git source and made them silently revert to stopped.",
        "UX: when `data-app deploy --wait` times out or the app reaches state=error, the error now "
        "auto-fetches the latest run's `failure_reason` (via /apps/{id}/runs) and includes it inline "
        '-- including setup-phase failures with no container logs -- instead of a bare "timed out" '
        "message. A managed-repo clone-auth failure (`could not read Username`) adds an actionable "
        "hint to run `data-app git-bind-credential`. The diagnostic is best-effort and never masks "
        "the original error.",
        "All of the above are mirrored 1:1 on the `kbagent serve` REST API "
        "(`/data-app/.../runs`, `/data-app/.../git-repo/bind-credential`, and the "
        "`use_managed_git_repo` field on create).",
        "New (#446): the `install.sh` bootstrap got a first-run onboarding pass -- it "
        "auto-installs `uv` when it is missing (opt out with `KBAGENT_NO_UV_BOOTSTRAP=1`), quiets "
        "the install output behind tidy step lines, and finishes with a Keboola banner plus a "
        "'next steps' footer (`kbagent project add`, `kbagent --help`) so first-time users know "
        "what to run next.",
    ],
    "0.64.0": [
        "New: data-app git-repo introspection + managed-repo credentials -- five `data-app "
        "git-*` commands over the sandboxes-service `/apps/{id}/git-repo/*` endpoints. "
        "`git-repo` shows the clone URLs + a managed flag, `git-branches` lists remote branches "
        "with commit metadata, `git-entrypoints` lists root-level `.py` files, `git-credentials` "
        "lists managed-repo credentials, and `git-credentials-create` mints an SSH key or HTTP "
        "token (a one-time secret for `http_token`). Mirrored 1:1 on the `kbagent serve` REST "
        "API. The read trio needs only the project storage token; the credential pair is "
        "managed-repo only (admin token). Gotcha: the introspection endpoints return 409 until "
        "the app has been deployed at least once -- the git block syncs from the Storage config "
        "into the Data Science app record at deploy time (first tagged 0.63.3, #414).",
        "Security: `kbagent serve --ui` now decides which paths require auth via the router "
        "match protocol and fails closed, fixing a fastapi-0.137 nested-router bypass that "
        "served `/doctor`, `/version`, `/changelog`, and `/agents` unauthenticated "
        "(GHSA-ffpq-prmh-3gx2); the temporary `fastapi<0.137` cap is lifted (0.63.4).",
        "Fix: invalid `--mode` / `--poll-strategy` (`job run`), `--role` / `--default-role` "
        "(`project invite`, `member-set-role`) and `--role-hint` (`dev-portal identity`) values "
        "fail with a clean exit-2 usage error again instead of an uncaught traceback -- they "
        "regressed under Typer >=0.25's vendored Click, so the choice options now use `StrEnum`. "
        "The interactive REPL `help` and tab-completion again list every command (0.63.4).",
        "Change: kbagent now ships as a self-contained native binary via Homebrew / apt / dnf / "
        "apk / Chocolatey / WinGet (PyInstaller + nfpm release pipeline), alongside the existing "
        "pip / uv install (#405).",
        "Internal: the data-app command and service layers were split into sibling modules "
        "(`commands/_data_app_git.py`, `commands/_data_app_runtime.py`, "
        "`services/data_app_git_service.py`) to keep `data_app.py` under the file-size budget -- "
        "no behavior change (#423). Dependency bumps: cryptography 48, starlette 1.3, "
        "python-multipart 0.0.31, pyjwt 2.13.",
    ],
    "0.63.4": [
        "Fix: the interactive REPL `help` command and tab-completion again list every "
        "command instead of only `help`/`exit`. `_build_command_tree` guarded its walk "
        "with `isinstance(x, click.Group)`; Typer >=0.25 vendors its own Click "
        "(`typer._click`), so the `TyperGroup` from `typer.main.get_command` is not a "
        "standalone `click.Group` subclass and the guard collapsed the tree to empty. "
        "Replaced with a structural `_is_group()` TypeGuard; the previously swallowed "
        "tree-build error is now written to stderr.",
        "Fix: invalid `--mode`/`--poll-strategy` (`job run`), `--role`/`--default-role` "
        "(`project invite`), `--role` (`project member-set-role`) and `--role-hint` "
        "(`dev-portal identity add/edit`) values again fail with a clean exit-2 usage "
        "error instead of an uncaught traceback. The options passed a standalone "
        "`click.Choice` into Typer; under a Click-vendoring Typer (>=0.25) the "
        "`BadParameter` it raises is a different class than the one Typer's parser "
        "catches, so it escaped unhandled. Replaced the `click.Choice` options with "
        "`StrEnum` types so Typer builds and validates the choice with its own Click. "
        "Valid values and `--help` were unaffected.",
        "Security: cap `fastapi<0.137` in the `[server]` extra. With fastapi 0.137 "
        "`serve --ui` stops requiring a token on protected endpoints -- `/doctor`, "
        "`/version`, `/changelog`, `/agents` become reachable unauthenticated, reopening "
        "GHSA-ffpq-prmh-3gx2 (fixed in an earlier release). Held until the `serve --ui` "
        "auth check is updated for the newer fastapi.",
        "Security: `serve --ui` now decides which paths need auth by asking the router's "
        "match protocol whether a GET resolves to a real endpoint, instead of scanning "
        "`app.routes` as a flat list -- and it fails closed (any error -> path treated as "
        "protected). fastapi 0.137 nests included routers into a lazy tree, so the old flat "
        "scan missed nested endpoints and served `/doctor`, `/version`, `/changelog`, "
        "`/agents` unauthenticated (GHSA-ffpq-prmh-3gx2). With the predicate fixed, the "
        "temporary `fastapi<0.137` cap above is lifted -- fastapi is back on the latest release.",
    ],
    "0.63.3": [
        "Fix: `kbagent context` no longer renders API path templates as "
        "`/apps/<built-in function id>/logs/tail`. `AGENT_CONTEXT` is an f-string (it "
        "interpolates the version), so unescaped `{id}` placeholders were evaluated against the "
        "Python builtin `id`; the literal braces are now escaped so paths render the OpenAPI-style "
        "`{id}` consistently (affected /jobs/{id}/kill, /tables/{id}/swap, /tables/{id}/pull, "
        "/apps/{id}/logs/tail).",
        "New: `data-app git-repo`, `data-app git-branches`, and `data-app git-entrypoints` introspect "
        "the git repository a data app is deployed from (sandboxes-service `/apps/{id}/git-repo/*`): "
        "clone URLs plus a managed flag, remote branches with commit metadata, and the root-level "
        "`.py` entrypoint files. These read endpoints work for any configured repo (managed or "
        'external) and need only the project storage token. Gotcha: they return 409 "no Git '
        'repository configured" until the app has been deployed at least once -- the git block is '
        "synced from the Storage config into the Data Science app record at deploy time, so a "
        "`--no-deploy` app has no git repo from the service's point of view.",
        "New: `data-app git-credentials` and `data-app git-credentials-create` list and mint git "
        "credentials (an SSH key or an HTTP token) for a *managed* git repo. Apps created via "
        "`data-app create --git-repo <url>` are external (not managed), so `git-credentials-create` "
        "returns 409 for them; credential management applies to managed repos and requires an admin "
        "storage token (CanManageAppRepoCredentials). For `--type http_token` the response carries a "
        "one-time secret that is printed once and can never be retrieved again (mirrors `data-app "
        "password`); `--type ssh_key` requires `--public-key` / `--public-key-file`.",
    ],
    "0.63.2": [
        "Docs: the in-process Python SDK is now documented. New `docs/sdk.md` is the deep "
        "guide -- the importable `Client` facade vs the CLI vs the `serve` REST API, where the "
        "facade sits in the 3-layer architecture, a full method reference "
        "(query / query_result / run_job / config_detail / upload_table / files / raw), the "
        'typed result-model contract (`extra="allow"`, `populate_by_name`, semver via '
        "`__all__`), `py.typed`, idempotent `run_job`, a gotchas cheat-sheet, and a contributor "
        "section on extending the SDK. A runnable curses Storage-browser demo "
        "(`examples/storage_tui/`) drives a real project entirely through the SDK, and "
        "`CONTRIBUTING.md` gains an 'Extending the importable SDK' section. Also fixes doc "
        "drift (dynamic `APP_NAME`, Python 3.12+, `__init__` documented as the public SDK "
        "entrypoint). No runtime code change -- the installed package is functionally identical "
        "to 0.63.1; docs and examples ship in the repo, not the wheel.",
    ],
    "0.63.1": [
        "Fix (#424): self-update is repaired for users still on <=0.62.0. The PyPI rename "
        "`keboola-agent-cli` -> `keboola-cli` broke `kbagent update` for every already-installed "
        "client: the immutable pre-0.63 code probes the release for the OLD wheel name "
        "`keboola_agent_cli-<version>-py3-none-any.whl`, which the renamed release no longer "
        "carried (404), then falls back to a `git+` build that uv aborts with `Executable already "
        "exists: kbagent`. The release workflow now ALSO ships a legacy-named compat wheel "
        "(`keboola_agent_cli-<version>-py3-none-any.whl`, identical code, distribution name "
        "unchanged) so those clients find their asset and upgrade in place. `APP_NAME` is now "
        "resolved dynamically (prefers `keboola-cli`, falls back to `keboola-agent-cli`) so "
        "`kbagent version` and the User-Agent keep working under either distribution. The on-disk "
        "config dir (`~/.config/keboola-agent-cli/`) is deliberately unchanged.",
        "Fix: a FAILED `kbagent update` is no longer reported as `already up to date`. "
        "`_compose_update_summary` masked any non-upgraded stage as success, so the self-update "
        "breakage above surfaced to users as `kbagent vX (already up to date)` while `kbagent "
        "version` correctly showed a newer release available. Failures now render as "
        "`kbagent vX update FAILED: <reason>` (full transcript stays in `--json` / `--verbose` "
        "output); only an explicit `up_to_date` short-circuit prints the up-to-date line.",
    ],
    "0.63.0": [
        "New (#428): the importable SDK is now statically typed -- a PEP 561 `py.typed` "
        "marker ships in the wheel and the high-traffic facade operations return typed "
        "pydantic models (`JobResult`, `QueryResult`, `UploadTableResult`, `SyncPushResult`, "
        "`ConfigDetailResult`, `CloneResult`) exported from the package root. Downstream "
        "`mypy`/`ty`/IDEs now treat `keboola_agent_cli` as typed, so an SDK contract change "
        'surfaces at type-check time instead of at runtime. Every model is `extra="allow"` '
        "(backend field drift never raises) and accepts the raw API key or the snake_case "
        "field name, so `Model.model_validate(service_dict)` works directly. Typed at the "
        "facade only: the dict-returning service layer and `--json` CLI output are unchanged. "
        "New typed wrappers: `Client.run_job` / `query_result` / `config_detail` / `upload_table`.",
        "New (#427): `kbagent job run --idempotency-key KEY [--force-rerun]` (and "
        "`Client.run_job(idempotency_key=..., idempotency_store=...)`) makes a replayed, "
        "interrupted build step safe -- a prior still-running or non-failed job is returned "
        "instead of creating a duplicate side effect; a prior failed run is re-run. The Queue "
        "API `POST /jobs` has no server-side idempotency token (verified against the live spec "
        "v1.3.8 and the job-queue server source -- the internal `deduplicationId` is "
        "daemon-only), so dedup is client-side: a `JobIdempotencyStore` (atomic, fcntl-locked, "
        "0600) at `<config-dir>/job_idempotency.json`. Reusing a key for a different "
        "component/config raises; dedup is scoped to one machine.",
        "New (#426): `kbagent sync clone --source DIR --target ALIAS --target-dir DIR` (and "
        "`SyncService.clone_project` -> `CloneResult`) builds a new project by cloning a "
        "reference synced tree and parameterizing it via declarative override files -- "
        "`--bucket-map` (rewrite storage input/output bucket prefixes), `--variable-values` "
        "(override keboola.variables rows), `--instance-rename` (rename config-path prefixes) "
        "-- then pushes so every config CREATEs fresh. A new push Phase D remaps `keboola.flow` "
        "task `configId`s reference->ULID via `created_id_map` (generic; benefits any "
        "fresh-create push), alongside the Phase-C variable-link remap. Cloning into a fresh "
        "target needs no id surgery; a fresh-target guard refuses a non-empty target and a "
        "re-run with an existing target-dir is idempotent (`no_changes`).",
    ],
    "0.62.0": [
        "New (#417): `storage download-table` gains server-side row filtering -- "
        "`--where-column` + `--where-value` (repeatable, OR within the set) + "
        "`--where-operator eq|neq`, plus `--changed-since` / `--changed-until` (unix ts "
        "or strtotime like `-2 days`) to export only rows imported in a time window. "
        "This is the credential-only, no-workspace way to pull a filtered or incremental "
        "slice of a table -- the Query Service path needs a live workspace. The filters "
        "thread through both `export_table_async` and `get_table_data_preview` via a "
        "shared `_apply_table_filters` helper, so the sync-preview and async-export "
        "endpoints honor an identical contract.",
        "New (#417): `storage add-column` adds a single column to an existing table, "
        "using the same `name:TYPE(length)` grammar as `create-table --column` (with "
        "`--not-null` and `--default`). This closes a long-standing asymmetry -- kbagent "
        "could drop a column (`delete-column`) but not add one. The Storage add-column "
        "endpoint is synchronous (no job to wait on); the operation is classified `write` "
        "in the permission registry.",
        "Change (#424): the PyPI distribution is renamed from `keboola-agent-cli` to "
        "`keboola-cli`; the prebuilt-wheel asset is now `keboola_cli-<version>-py3-none-any.whl`. "
        "The import package (`keboola_agent_cli`), the `kbagent` binary, and the config dir "
        "(`~/.config/keboola-agent-cli/`) are unchanged, so existing installs keep working. "
        "Only a literal `pip install keboola-agent-cli` from PyPI stops resolving -- use "
        "`keboola-cli`. The release CI, `install.sh`, and the self-update resolver all build "
        "the new wheel name end-to-end.",
    ],
    "0.61.1": [
        "Note (#416 follow-up): clarified the value-typing contract of the importable "
        "`Client.query()`. The Query Service `/results` endpoint serializes Snowflake "
        'scalars as JSON *strings* (`1` -> `"1"`, `1.5` -> `"1.5"`, `true` -> '
        '`"true"`; SQL NULL -> None), and the in-process facade returns them '
        "transparently without coercion. The 0.61.0 docstring and release notes wrongly "
        'claimed "native JSON types"; the `query()` docstring and the gotchas reference '
        "now document the real, stable contract so callers know to cast. No behavior "
        "change -- caught and verified by a live E2E round-trip against a Snowflake "
        "workspace.",
    ],
    "0.61.0": [
        "New (#415): kbagent now ships a stateless, importable library facade -- "
        "`from keboola_agent_cli import Client` -- so any in-process Python consumer (a Keboola "
        "Data App, a transformation, a hosted service) can run Query Service SQL and read/write "
        "Storage Files without a CLI subprocess, a `kbagent serve` daemon, or a config-dir. "
        "`Client(url, token)` wraps the existing `KeboolaClient`; `client.query(workspace_id, sql)` "
        "returns `list[dict]` rows over the fast inline `/results` path (corrected in 0.61.1: "
        "values are warehouse-serialized strings, not native types; truncation is warned, "
        "never silently capped), and `client.files` offers "
        "`upload(path_or_bytes)`, `read_bytes(file_id) -> bytes`, `list() -> list[FileEntry]` "
        "(one uniform shape, read via `read_bytes` so callers never branch on a signed URL) and "
        "`delete()`. The Query Service pagination helper moved from the workspace service into "
        "`client.py` (re-exported, no behavior change) so the CLI and the library share one "
        "implementation. Everything under `keboola_agent_cli.__all__` is committed public API. "
        "Addresses the jasnost feedback points 1, 2, and 4 (point 3 -- structured query results -- "
        "shipped in 0.59.0).",
    ],
    "0.60.4": [
        "Security: `kbagent serve --ui` no longer lets `GET /doctor`, `/version`, and `/changelog` "
        "(and any other registered endpoint) bypass bearer auth. In single-process UI mode the auth "
        "middleware consulted a hand-maintained prefix allow-list to decide which GETs were public "
        "SPA paths; that list had gone stale and omitted those health-router routes, so they "
        "executed unauthenticated -- `/doctor` in particular exposes project aliases, ids, stack "
        "URLs, and the config path. The predicate is now route-aware: it derives the protected set "
        "from the app's actually-registered routes, so every current and future endpoint requires "
        "auth while genuine client-side SPA paths still fall through to the public index.html shell. "
        "Only affects `--ui` mode; API-only `serve` was never exposed. Private advisory "
        "GHSA-ffpq-prmh-3gx2.",
    ],
    "0.60.3": [
        "Security: `kbagent sync pull` now sanitizes the API-supplied bucket id and table name "
        "before using them as filesystem paths when writing storage metadata + samples, and asserts "
        "the resolved path stays inside the sync workspace. `_write_storage_metadata` previously used "
        "the table `name` verbatim (and `bucket_id.replace('.', '-')`, which neutralizes `..` but not "
        "`/` or an absolute path), so a malicious or compromised Storage API response with a table "
        "named like `../../../../etc/cron.d/evil` could write attacker-controlled JSON outside the "
        "workspace. The config-write path already had this defense (`sanitize_path_segment` + "
        "`_ensure_within_branch`); the storage-metadata and samples writers now mirror it via "
        "`sanitize_path_segment(...)` plus a new `_ensure_path_within` containment check. Behavior is "
        "unchanged for legitimate data: the `in.c-foo` -> `in-c-foo` bucket-directory convention and "
        "the `<table>.json` filename are preserved. Private advisory GHSA-833q-c5wv-26r7.",
    ],
    "0.60.2": [
        "Security: scheduled `ai_agent` tasks (claude/codex/gemini spawned by `kbagent serve`) no "
        "longer inherit the manage (super-admin) or master tokens from the serve process "
        "environment. `agent_runner._build_subprocess_env` copied the full `os.environ` into every "
        "spawned child, so a prompt-injectable AI agent that was only meant to summarize jobs could "
        "read `KBC_MANAGE_API_TOKEN` / `KBC_MASTER_TOKEN*` from its own environment and exfiltrate "
        "the highest-value credentials. The AI-agent paths now strip every `KBC_MANAGE_*` / "
        "`KBC_MASTER_*` key (mirroring the MCP-child isolation in `mcp_transport._build_minimal_env` "
        "and the manage-token default-deny). The per-project storage token (`KBC_TOKEN`) is retained "
        "so headless `--project __env__` reads still work, and `cli_command` children -- which are "
        "`kbagent` itself and legitimately need the tokens for scheduled `project refresh` / "
        "`sharing` tasks -- are unchanged. Private advisory GHSA-wm54-r2hh-cxm9.",
        "Security: `ai_agent` tasks no longer forward `extra_args` to the underlying AI CLI "
        "(claude/codex/gemini) unless the kbagent process running the task is opted in via a truthy "
        "`KBAGENT_ALLOW_AI_EXTRA_ARGS`. `extra_args` were passed verbatim, so a task definition (or "
        "any holder of the serve bearer token, including the immediate `/agents/test` endpoint) "
        "could inject a rail-disabling flag -- e.g. a permission-skip / unrestricted-execution flag "
        "-- and turn a contained headless agent into arbitrary host command execution. They are now "
        "ignored by default and dropped with a loud warning; set `KBAGENT_ALLOW_AI_EXTRA_ARGS=1` to "
        "honor them, mirroring the `--allow-env-manage-token` opt-in. The gate fires in EVERY "
        "consumer of the agent runner -- scheduled serve tasks AND local `agent test` / `agent run` "
        "/ `prompt-improve --extra-arg` -- so a user passing `--extra-arg` on their own machine must "
        "set `KBAGENT_ALLOW_AI_EXTRA_ARGS` in that shell, or the args are silently dropped (with a "
        "warning). Private advisory GHSA-777j-6p95-qv3m.",
    ],
    "0.60.1": [
        "Security: `kbagent storage file-download` now contains the API-supplied file name under "
        "the target directory, refusing path-traversal escapes. When `--output` was omitted, the "
        "downloaded bytes were written to the server-provided `name` verbatim -- so a malicious or "
        "compromised Storage API response with a name like `../../../../.zshrc` (or an absolute "
        "path) could overwrite an arbitrary file on the user's machine with attacker-controlled "
        "content. Leading separators are now stripped (an absolute name can no longer override the "
        "target), legitimate nested subpaths are preserved, and the resolved path is asserted to "
        "stay within the chosen directory (CWD, or the `--output` directory) -- otherwise the "
        "download is rejected with `INVALID_ARGUMENT`. Reported via private advisory "
        "GHSA-6px9-99p6-7j7g.",
    ],
    "0.60.0": [
        "New (#353): kbagent installs and self-updates from a prebuilt wheel attached to each "
        "GitHub release instead of building from `git+` source. `uv tool install git+...` "
        "recompiled the bundled React SPA via `npm ci` + `vite build` on every install -- the uv "
        "cache never covered the npm step -- which took 2-4 minutes on WSL2 and tripped the "
        "auto-update timeout. The universal `py3-none-any` wheel is now built once in CI "
        "(`release.yml`, on `release: published`) and uploaded as a release asset; "
        "`build_kbagent_upgrade_command` installs it via a PEP 508 direct reference "
        "(`keboola-agent-cli[server] @ <wheel-url>`) when the asset exists, falling back to the "
        "`git+` source build for older releases without one. Both the startup auto-update hook and "
        "`kbagent update` benefit -- install/update drops from minutes to a seconds-long download.",
        "New (#353): a `curl -LsSf https://raw.githubusercontent.com/keboola/cli/main/install.sh | "
        "sh` bootstrap installer resolves the latest release and installs its prebuilt wheel -- no "
        "source build, no `gh` CLI (just `curl` + `uv`), matching the pattern the install guide "
        "already uses for uv and Claude Code. Set `KBAGENT_NO_SERVER=1` for a CLI-only install "
        "without the `[server]` extras.",
        "Fix (#353): the self-update subprocess timeout is no longer hardcoded at 120s in two "
        "places (the startup hook and `kbagent update`). It is a single `UPDATE_TIMEOUT_SECONDS` "
        "constant (raised to 300s) overridable via `KBAGENT_UPDATE_TIMEOUT` -- useful for the slow "
        "`git+` fallback build on WSL.",
        "Fix (#353): a slow update is no longer reported as a failure. The startup hook now "
        "distinguishes a build TIMEOUT (the git+ build outran the timeout; it finishes on a later "
        "run) from a genuine failure, printing 'still building' instead of 'Auto-update failed'. It "
        "also skips the auto-update when the subcommand is `update` / `version` even behind global "
        "flags (`kbagent --json update`), so the startup banner no longer disagrees with the "
        "explicit command's JSON output.",
    ],
    "0.59.0": [
        "Faster: `kbagent workspace query` now reads results via the Query Service's inline "
        "`GET /api/v1/queries/{job}/{stmt}/results` endpoint by default instead of materializing a "
        "CSV file through the warehouse UNLOAD path (`.../export?fileType=csv`). The inline path "
        "returns the already-computed result set as JSON -- no file export round-trip -- so interactive "
        "queries come back markedly faster. Each statement now carries structured `columns` + `rows` "
        "(plus `row_count`, `total_rows`, `truncated`) alongside a synthesized `csv_data`, so the CLI "
        "preview, web UI table, and any `--json` consumer keep working unchanged.",
        "New: `--limit N` (default 500) caps how many rows the fast inline path fetches; it pages "
        "through the result set by `offset` until the limit is reached, marking the result `truncated` "
        "when the warehouse has more. `--full` opts back into the complete CSV export (slower, "
        "uncapped) when you need every row -- e.g. piping `workspace query --full --json` for a bulk "
        "extract. The `kbagent serve` `/workspaces/{p}/{w}/query` REST endpoint defaults to `full=True` "
        "to preserve the web UI's complete-CSV download until the frontend learns to paginate.",
        "New (#404): `kbagent init --from-global --project ALIAS` copies only the named project(s) "
        "from the global config into the new local workspace instead of all of them. The flag is "
        "repeatable (`--project a --project b`) and implies `--from-global`, so `kbagent init "
        "--project kosik-test` is enough to seed a focused single-project workspace without "
        "re-entering a Storage API token you already have globally. An unknown alias fails fast "
        "(`CONFIG_ERROR`, exit 5) with the list of available aliases; if the global default project "
        "falls outside the selection, `default_project` is repointed to the first selected alias. "
        "Omitting `--project` preserves the existing copy-all behaviour.",
    ],
    "0.58.0": [
        "New: `kbagent workspace query` runs SQL against BigQuery workspaces, not just Snowflake. "
        "The Query Service path was always backend-agnostic (`POST "
        "/api/v1/branches/{b}/workspaces/{w}/queries` + CSV export are identical for both backends), so "
        "this was a classification + error-legibility fix rather than a new execution path -- verified "
        "live against project 9621 (e2e-bigquery) on connection.keboola.com, including a real-data "
        "`workspace load` + `query` round-trip. Mind the dialect: Snowflake quotes identifiers with "
        '`"..."`, BigQuery with backticks `` `...` ``.',
        "Fix: BigQuery workspaces are no longer mislabeled `qs_compatible: false`. `qs_compatible` is now "
        "keyed by (backend, loginType): BigQuery's `default` loginType is whitelisted via the new "
        "`QUERY_SERVICE_COMPATIBLE_LOGIN_TYPES_BIGQUERY`, kept separate from the Snowflake whitelist "
        "because Snowflake's own legacy `default` is rejected by the Query Service ('JWT token is "
        "invalid') -- the same string means compatible for BigQuery and incompatible for Snowflake. "
        "Pre-0.58.0 every BigQuery workspace was wrongly hidden by `workspace list --qs-compatible` and "
        "shown incompatible in `workspace detail`, even though queries ran fine.",
        "Change: `workspace create` on a BigQuery project now requests loginType `default` explicitly. "
        "It is the only BigQuery loginType and matches keboola-mcp-server, rather than omitting it and "
        "relying on the backend default; Snowflake key-pair creation is unchanged.",
        "Fix: BigQuery query errors now read as plain text instead of a serialized wrapper. The Query "
        'Service returns a failed BigQuery statement as `{Location: ...; Message: "..."; Reason: ...}`; '
        "the new `_unwrap_bigquery_error` (`client.py`) extracts the inner `Message` so the error box "
        "matches Snowflake's plain text (Snowflake errors have no wrapper and pass through untouched). "
        "Tests: `TestBigQueryQueryServiceSupport`, `TestUnwrapBigQueryError`, a BigQuery case in "
        "`TestExtractQueryJobError`, and a backend-aware `test_e2e.py` workspace query.",
        "New (#401): `kbagent changelog` now shows a one-line summary per version by default, with "
        "`--full` / `-v` to expand every note. Entries follow an authoring contract -- one logical change "
        "per prefixed bullet (`New:`/`Fix:`/`Change:`/...), leading with a self-contained first sentence "
        "-- so the default view and the post-update 'What's new' banner stay scannable instead of "
        "rendering a wall of text.",
    ],
    "0.57.0": [
        "BREAKING (flow / conditional flows): the `flow` command group now targets "
        "conditional flows (`keboola.flow`) ONLY; `keboola.orchestrator` support is "
        "dropped. `--component-id` is removed from every `flow` subcommand and from "
        "the `/flows` REST surface (FlowCreate/FlowUpdate/FlowSchedule models + "
        "query params). Conditional flows use **string IDs (not integers)**, "
        "`phases` with `next[].goto` transitions (a phase id or `null` to end) and "
        "optional `condition` objects "
        "(operator/function/phase/task/variable/const/array), and typed tasks "
        "(`job`/`notification`/`variable`). Execute one with "
        "`kbagent job run --component-id keboola.flow --config-id ID`.",
        "Change (flow validation): `flow new`/`flow update` now validate the body "
        "against the live conditional-flow JSON Schema (Draft7) plus semantic "
        "checks. The schema is fetched at runtime from the stack's component "
        "registry (AI Service `configurationSchema` for `keboola.flow` -- never "
        "bundled/vendored). Semantic checks: a phase with conditional transitions "
        "must end with a default (condition-less) transition; every phase needs "
        ">=1 enabled task; operator/function operand-arity is enforced; unreachable "
        "phases are reported as warnings (forward BFS from the first phase), and "
        "`goto` loops are legal (no cycle detection). Invalid bodies are rejected "
        "with `INVALID_FLOW_DEFINITION` (replaces `INVALID_FLOW_DAG`, removed from "
        "`ErrorCode`). When the schema "
        "fetch fails (network, KeboolaApiError, or empty/missing schema) the write "
        "is NOT blocked: structural validation is skipped, the semantic checks "
        "still run (the Storage API does not validate flow configs server-side), "
        "and a `structural schema validation skipped: <reason>` warning is "
        "surfaced.",
        "New: `flow validate --file @flow.yaml|- [--project ALIAS]` validates a "
        "definition without writing it. With `--project` it fetches the live schema "
        "for full structural + semantic validation (fetch failure degrades to "
        "semantic-only + a note); without `--project` it runs semantic-only and "
        "notes that structural schema validation was skipped (no schema source). "
        "Exit 0 valid / exit 2 on errors; `--json` lists "
        "`{valid, errors, warnings, notes}`. New permission `flow.validate` (read).",
        "Change (flow output): `flow schema --full --project ALIAS` fetches and "
        "dumps the live JSON Schema from the stack (`--full` without `--project` "
        "errors -- the schema is no longer bundled); plain `flow schema` still "
        "prints the offline, conditional-flow-shaped YAML template. `flow detail` "
        "human rendering is rewritten for conditional flows (per-phase transitions, "
        "task-type badges, retry); JSON output is the raw body, unchanged. "
        "`flow list` no longer lists legacy orchestrator configs -- it counts them "
        "and reports `legacy_orchestrator_count` (+ a warning) so a 'disappeared' "
        "flow is explained, and the `Component` column is dropped (every row is "
        "`keboola.flow`).",
        "Internal: new pure module `services/flow_validation.py` (structural "
        "validation takes an explicit optional `schema` parameter, semantic checks "
        "always run; no network, no bundled schema); `config new` flow scaffold now "
        "emits a conditional-flow skeleton (string ids, `phases`/`tasks`, a `job` "
        "task) and defaults to `keboola.flow`; dead `ORCHESTRATOR_COMPONENTS` "
        "removed from `sync/config_format.py`. Docs/agent surfaces synced: "
        "CLAUDE.md, AGENT_CONTEXT, keboola-expert.md, SKILL.md, "
        "commands-reference.md, flow-workflow.md (full rewrite), gotchas.md. Tests: "
        "`tests/test_flow_validation.py` (new), `tests/test_flow_service.py` + "
        "`tests/test_flow_cli.py` rewritten, `tests/test_e2e.py` flow round-trip "
        "uses a CF payload + `flow validate` and skips cleanly on "
        "`conditional_flows=false`.",
    ],
    "0.56.0": [
        "Maintenance re-release -- no code changes since 0.55.0. The `0.55.0` version number lived in "
        "`main` across three successive builds (#383 sync-secret audit, then #379 `semantic-layer "
        "reference-data`, then #388 its changelog backfill) before the `v0.55.0` tag was cut, so anyone "
        "who installed an interim 0.55.0 build did not receive the `reference-data` commands via "
        "`kbagent update`: the auto-update check compares version numbers only (`_is_up_to_date` -> "
        "`Version(local) >= Version(latest)`), so `0.55.0 >= 0.55.0` reads as already-up-to-date and "
        "skips the reinstall even though the `v0.55.0` tag points at a newer commit. Bumping to 0.56.0 "
        "gives auto-update a strictly-greater `/releases/latest` target, so the `reference-data` "
        "sub-app (#379) and the rest of the 0.55.0 changes reach every user on the next `kbagent "
        "update`. No behaviour change beyond the version string; the reference-data feature entry stays "
        "recorded under 0.55.0 (the release it first shipped in) and its `since v0.55.0` doc tags "
        "remain correct.",
    ],
    "0.55.0": [
        "New: `kbagent semantic-layer reference-data` (alias `kbagent sl reference-data`) -- a CRUD "
        "surface for the metastore `semantic-reference-data` type, a per-dimension member store that "
        "holds an entire dimension (e.g. a Chart of Accounts: the account list plus all attributes) as "
        "ONE record, with the members in a `members[]` array, in the semantic layer instead of a "
        "hardcoded Storage table. Pairs with keboola/go-monorepo#533 (the metastore JSON-Schema side); "
        "this is the kbagent client. Four leaves: `list --project P [--model M]` -> dimension summaries "
        "(id, dimension_name, model_uuid, dataset_id, member_count) [read]; `get --project P (--id ID | "
        "--dimension D)` -> one record with all members, resolved by record UUID or by the "
        "project-unique dimension name (passing both, or neither, is a usage error, exit 2) [read]; "
        "`set --project P [--model M] --dimension D --members-file PATH ('-' = stdin) [--dataset-id T] "
        "[--description X]` -> create-or-replace from a JSON array of member objects, idempotent on the "
        "dimension: an existing record is replaced in place via the metastore's revisioned `PUT` "
        "(meta.revision++, history preserved), otherwise a new record is POSTed [write]; "
        "`delete --project P --id ID [--yes]` -> server-side soft-delete; non-TTY without --yes refuses "
        "(exit 2) [destructive]. The dimension name is unique per project per type, so the "
        "create-or-replace lookup is project-wide by dimension (not model-scoped): `set` stays "
        "idempotent regardless of `--model`, and the resolved model is stored on the record rather than "
        "used as the key. Deliberately self-contained: reference-data is NOT AI-generated (its members "
        "come from DIM_COA, not the build heuristic), so it is kept OUT of `build` / `export` / `diff` / "
        "cascade / `PUSH_ORDER` -- zero blast radius on the existing model flows; deleting a model does "
        "not cascade-delete its reference-data records. New `MetastoreClient.put_item` (revisioned PUT "
        "envelope, distinct from the DELETE+POST the `edit` ops use) and `semantic-reference-data` "
        "registered in `SemanticType` / `SEMANTIC_TYPES`. New layers: "
        "`services/_semantic_layer_reference_data.py` (run_list/get/set/delete helpers, extracted to "
        "keep `semantic_layer_service.py` under the file-size ceiling) with thin delegators on "
        "`SemanticLayerService`; `commands/_semantic_layer_reference_data.py` (Typer sub-app -- the "
        "first to mix read/write/destructive permission classes under one group, enforced per leaf via "
        "`check_cli_permission`); and a 1:1 `kbagent serve` REST surface (GET /reference-data, GET "
        "/reference-data/{id}, PUT /reference-data, DELETE /reference-data/{id}). Permission registry: "
        "list/get = read, set = write, delete = destructive. Docs: AGENT_CONTEXT, CLAUDE.md command "
        "list, commands-reference.md, gotchas.md (since v0.55.0), SKILL.md (triggers + regenerated "
        "table). Tests: service CRUD + create-vs-replace + cross-model idempotency + members-not-list "
        "validation + NOT_FOUND + id-vs-dimension resolution + permission-registry asserts; CLI "
        "list/get/set/delete + bad-JSON exit-2 + non-TTY --yes gate; `metastore_client` TestPutItem "
        "(httpx_mock); serve-router parity; E2E (`tests/test_e2e.py` CLI lifecycle + "
        "`tests/test_server_semantic_layer_routes_e2e.py` HTTP-route hops) run live against project "
        "1143. Closes keboola/cli#379.",
        "Audit (`sync status` + `doctor` flag plaintext `#`-secrets in synced configs): a follow-up to the #378 fix, which was forward-looking only (it could not retroactively encrypt secrets written by older versions). `sync status` now scans every *in-sync* config/row in the working tree and, when a `#`-prefixed value is still plaintext on disk (i.e. it passed through the sync baseline unencrypted -- the remote holds it in plaintext), surfaces a `plaintext_secret_warnings` block (human) / array (`--json`) with the location and key paths (never the secret values). `doctor` gets a matching `sync_secrets` check that runs when the current directory is a sync working tree (`.keboola/manifest.json`), reporting `warn` with the affected configs or `pass`/`skip`. Detection reuses `_encryption.collect_secrets` via the new `find_plaintext_secret_keys` helper, so it counts only genuinely-unencrypted values (already-`KBC::` values are ignored). Pending local edits (file hash != manifest `pull_hash`) are deliberately NOT flagged -- a `sync push` on >=0.54.0 encrypts those on write, so warning about them would be noise. Read-only, filesystem + manifest only, no API. The warning text points at the real remediation: re-push on >=0.54.0 to encrypt AND rotate the credential, because config version history keeps the old plaintext. Tests: `tests/test_sync_plaintext_audit.py`.",
        'Removal: the `--hint client|service` global flag and its entire `hints/` code-generation subsystem are gone. Deprecated since 0.45.0 in favour of the `kbagent serve` REST API (which covers every command, not just the ~45 that had hint definitions), the flag now no longer exists -- passing `--hint` errors as an unknown option. Deleted: `src/keboola_agent_cli/hints/` (registry, renderer, models, and all 21 `definitions/*.py`), the `should_hint`/`emit_hint`/`_resolve_hint_stack_url` helpers in `commands/_helpers.py`, the `if should_hint(ctx): emit_hint(...)` guard at the head of every command, the `--hint` option + `hint_mode` plumbing in `cli.py`, `docs/hint-mode.md`, `plugins/kbagent/skills/kbagent/references/programming-with-cli.md`, and `tests/test_hints.py` (plus all `--hint` test classes across `test_cli.py`, `test_data_app_cli.py`, `test_data_app_secrets_cli.py`, `test_member_cli.py`, `test_e2e_lineage_deep.py`). Docs/agent surfaces scrubbed: `AGENT_CONTEXT` (`kbagent context`), `SKILL.md`, `keboola-expert.md`, `kbagent-pr-reviewer.md`, `gotchas.md`, `commands-reference.md`, `storage-types-workflow.md`, `README.md`, `CONTRIBUTING.md`, `docs/TUTORIAL.md`, and `CLAUDE.md`. Migration: run `kbagent serve` and call the equivalent REST endpoint instead of generating a one-off Python snippet. Unrelated "hint" surfaces are untouched: `--no-hint-next` (data-app secrets), `--role-hint` (dev-portal), and error-message hints. Historical changelog entries that mention `--hint` are left as-is -- they record what shipped in those releases.',
    ],
    "0.54.0": [
        "Security fix (`config create/update` + rows stored `#`-prefixed secrets as PLAINTEXT): the interactive config write paths -- `config new --push`, `config update`, `config row-create`, `config row-update` -- now encrypt `#`-prefixed secret values via the Encryption API before writing to Storage, matching the encrypt-before-write contract that `sync push` and the variables path already enforced. Reported in #378 (verified live on projects 4214 and 10539): the Storage API stores configuration JSON verbatim -- it does NOT encrypt `#`-values server-side, the client must pre-encrypt. Before this fix, an agent or human running e.g. `config update ... --configuration` with a `#password` left the credential readable in plaintext in Storage, in every config version, and re-exposed on the next read; a sync action (e.g. `testConnection`) would even run with the live plaintext credential -- the job failing closed (`Invalid cipher text`) only masks the leak, it is not a safeguard. The fix is service-layer (`ConfigService._encrypt_secrets_before_write`), so it also covers the `serve` REST config routes and the `kbagent tool` MCP passthrough, which funnel through the same service. Fail-closed by default: an encryption failure (or an unresolvable project scope) raises `ENCRYPTION_FAILED` instead of writing plaintext. The new `--allow-plaintext-on-encrypt-failure` flag (named consistently with `sync push`) downgrades that to a warning for bootstrap/debug. `project_id` for the Encryption API scope is read from the project config and falls back to `verify_token`, resolved only when secrets are actually present (secret-free writes skip the extra round-trip). Dry-run is intentionally NOT encrypted so the diff stays readable and deterministic. Tests: `tests/test_config_encryption.py` (encrypt-on-write for all four paths, fail-closed, escape hatch, secret-free skip, project_id fallback, dry-run plaintext).",
    ],
    "0.53.0": [
        'Fix (`sync pull --force`, silent baseline corruption -> data loss): a config with un-pushed local edits is no longer silently de-synced when a force-pull runs while the remote is unchanged. Repro (reported on v0.51.1, project 5785): pull a config, edit its `_config.yml` (`sync diff` correctly shows `1 to update`), then run `sync pull --force` -- typically to resolve an *unrelated* config\'s conflict. Pre-0.53.0, `--force` skipped the "locally modified" guard in `SyncService.pull()`, so for a config whose remote had not changed the `remote_unchanged` short-circuit re-stamped the manifest `pull_hash` from the *edited on-disk file*. Afterwards `sync diff` and `sync push` both reported "in sync" and a real `push` shipped nothing, while the live remote still held the old config -- the local edits were stranded with no visible signal. Root cause was an interaction of two individually-reasonable decisions: `--force` bypassing the overwrite guard, and the `diff` `local_override_hashes` optimization that skips re-reading a file whose hash matches `pull_hash` (so the edited content was never even compared). The fix splits `--force` behaviour by 3-way diff state, per the maintainer decision: (b) local edited + remote UNCHANGED -> the file AND its 3-way base (`pull_hash` + `pull_config_hash`) are preserved, so the pending delta stays visible to `sync push` (no data loss, no silent revert); (a) local edited + remote ALSO changed since the last pull (a true merge conflict) -> the pull aborts before writing anything with the new `SyncConflictError` (exit 1, error code `SYNC_CONFLICT`), listing every conflicting config/row so the user resolves it (`sync diff`, then `push` or discard, then pull again). A no-conflict force-pull (remote changed, local untouched) still takes remote as before. Applies at config and row granularity. Note: `--force` no longer discards un-pushed non-conflicting edits -- that was the dangerous behaviour; to intentionally drop local edits, delete the file (or the config dir) and pull. New: `errors.SyncConflictError` + `ErrorCode.SYNC_CONFLICT`; `SyncService._detect_force_pull_conflicts` / `_is_conflict` (read-only pre-pass that runs before any write); `commands/sync.py` catches the error and prints a red per-config conflict block (human) or a `SYNC_CONFLICT` envelope with a `details.conflicts` array (`--json`). The pull `--force` help now documents the preserve/abort semantics. `--all-projects` surfaces a per-project conflict as a structured entry (`error_code: SYNC_CONFLICT` + the `conflicts` list, matching the single-project envelope) without aborting the batch. Tests: `tests/test_sync_force_pull_baseline.py` (config + row, preserve case b, abort case a, remote-only-changed takes remote, `--all-projects` structured conflict), `tests/test_sync_cli.py` (exit 1 + human/JSON conflict envelope), and `tests/test_e2e.py::TestE2ESyncWorkflow::test_sync_force_pull_conflict_aware` (real Storage: preserve when remote unchanged, then `SYNC_CONFLICT` after a remote mutation).',
    ],
    "0.52.1": [
        'Fix (docs/UX, swap-tables wording): completes the swap-tables semantics correction shipped in 0.52.0, which left four co-located surfaces still claiming the swap is dev-branch-only or "rejected on production" -- now false after that fix. The user-facing `ConfigError` raised on a missing `--branch` (exit 5) no longer says "The Storage API rejects this on production"; it now reads "swap-tables requires a branch ... Any branch works, including the default/production branch." The same stale wording was corrected in `commands-reference.md`, the `swap-tables` command docstring (which feeds the auto-generated `SKILL.md` decision table via `make skill-gen`), and the `AGENT_CONTEXT` block (`kbagent context`). A CLI test that mocked and asserted the old phantom "dev branch" error string (it passed only because the mock short-circuited the real service) was fixed to match the real message, and the `swap_tables` `Args` docstring "Dev branch ID" became "any branch accepted, including the default/production branch". No behaviour change: `branch_id` stays mandatory (the swap is branch-scoped). `clone-table` wording is intentionally untouched -- clone legitimately targets a dev branch. Surfaced by the `kbagent-pr-reviewer` self-review passes on keboola/cli#368 and keboola/cli#373.',
        "Maintenance: dependency bumps merged since 0.52.0 -- `pip` 26.0.1->26.1 and `python-multipart` 0.0.26->0.0.27 (the latter on the `kbagent serve` multipart path). Build/transitive only; no API or behaviour change.",
    ],
    "0.52.0": [
        'New: `kbagent storage clone-table --project P --table-id ID --branch ID [--dry-run]` -- pulls (clones) a production table into a development branch via the Storage API `POST /v2/storage/branch/{branch}/tables/{id}/pull` endpoint (operationName `devBranchTablePull`, the same call the platform issues on a branch\'s first write to a prod table). On `storage-branches` projects a dev branch reads production tables transparently (copy-on-write) until the first write, so a schema mutation in the branch -- `swap-tables`, dropping a column -- fails with a misleading "bucket not found" until the table is materialized branch-local. `clone-table` performs that materialization. The pull is one-way (default -> branch); the service refuses with exit 5 (`ConfigError`) before any HTTP call when neither `--branch` nor an active branch (via `kbagent branch use`) is set. The API returns a queued storage job which the client polls to completion before returning, mirroring `swap-tables` semantics. Permission class: `write` (creates a branch-local copy; never deletes). New layers: `KeboolaClient.pull_table`, `StorageService.clone_table`, `commands/storage.py` `clone-table`, hint `storage.clone-table`, and a 1:1 `kbagent serve` REST route (`POST /storage/tables/{project}/{table_id}/pull`). Tests: `tests/test_storage_clone.py` (13: client/service/CLI) + `tests/test_e2e.py::TestE2EStorageCloneTable` (3). Live-validated against project 10539 (storage-branches ON): clone a prod table into a dev branch -> table materialized -> in-branch `swap-tables` then succeeds (it previously failed with "bucket not found") -> production left untouched. Addresses the clone-prod-table-into-branch request in keboola/cli#362.',
        'Docs/correctness: corrected the typify workflow and `swap-tables` guidance after live verification (keboola/cli#362). (1) A dev-branch swap does NOT reach production via merge -- Keboola dev-branch merge propagates only configurations, not storage table schema (confirmed by the storage-branches design + Keboola public docs). `typify-table-workflow.md` is reworked into a two-stage model: rehearse in a dev branch (profile, build, swap, validate downstream), then repeat the real build + swap in the production (default) branch; the prior "merge promotes the typed schema to production" Phase 8 was wrong and is removed. (2) `swap-tables` does NOT "reject on production" -- a swap on the default/production branch is supported (verified live on project 10539) and is the way a typed rebuild is applied to prod. Corrected the swap docstrings (client/service), command help, hint, `context`, `gotchas.md`, and `storage-types-workflow.md`; the historical 0.28.0 changelog entry is left as-is. No code-behavior change: `branch_id` is still mandatory (the swap is branch-scoped); only the documentation was wrong.',
    ],
    "0.51.1": [
        "Fix (dev-portal): admin-role PATCH routing. `complexity`, `categories`, `forwardToken`, `forwardTokenDetails`, `injectEnvironment`, `processTimeout`, `requiredMemory`, `features`, and `category` are `.forbidden()` on the apps-api vendor schema (`clientAppSchema` in keboola/developer-portal:src/lib/validation.js) but settable on the admin schema. The vendor PATCH returns a misleading 422 (`Parameter complexity must be one of: easy, medium, hard`) because the enum-validation `.error()` annotation is attached on the shared admin schema before `clientAppSchema()` overrides with `.forbidden()`. `DeveloperPortalIdentity.role_hint` becomes a real validator (`vendor`/`admin`, case-folded, typos raise); `DeveloperPortalClient.patch_app` now reads the role and routes admin identities to `PATCH /admin/apps/{app}` (permissive schema); `DeveloperPortalService.prepare_patch` preflights vendor-role + admin-only-field combinations with a fail-fast error that names every offending field, explains why the 422 is misleading, and tells the user the exact command to switch identity. Admin role bypasses the preflight entirely. Reads, create, upload-icon, deprecate keep vendor-endpoint behaviour -- only PATCH has a meaningful admin variant on the server.",
        "Fix (dev-portal): MFA login. The apiary spec calls `challenge` optional with default `SOFTWARE_TOKEN_MFA`, but in practice the server 404s on personal-account TOTP logins when it is omitted -- users saw `Error: Developer Portal MFA login failed (HTTP 404)` with no diagnostic body. The field is now sent explicitly. Single attempt only: an earlier experiment retried with `SMS_MFA` on the same session, but `/auth/login` consumes the session, so the retry always 404'd with `Invalid code or auth state for the user` and masked the real first failure (most often a stale 30-second TOTP code). The raised `KeboolaApiError` now includes the server response body (truncated to 500 chars) plus a hint about TOTP rotation, so users can distinguish wrong-code from stale-code from expired-session.",
        "Fix (dev-portal): `--password-stdin` no longer hangs interactively. The old code did `sys.stdin.read().strip()` unconditionally, which waits for EOF (Ctrl-D) rather than for Enter -- users who pasted a password and pressed Enter were stuck until they Ctrl-C'd. The new `_read_password_stdin()` helper branches on `sys.stdin.isatty()`: TTY uses `getpass.getpass()` (hidden, line-based, Enter to confirm); pipe still does `read() -> strip()`. Both `identity add --password-stdin` and `identity edit --password-stdin` route through it. Help text updated to describe the dual-mode behaviour.",
    ],
    "0.51.0": [
        "New: Data Streams web UI. The `stream` command group (OTLP/HTTP sources, shipped in 0.50.0) now has a page in the kbagent web UI (`kbagent serve --ui`) under Browse -> Data Streams: list sources, create an OTLP/HTTP source (with sink auto-provisioning + if-not-exists), inspect endpoints/destination with a reveal toggle for the masked OTLP secret, and delete. Full parity with the `kbagent stream *` CLI and the `/stream/*` REST surface.",
        "Fix: `stream` is now documented in the `kbagent serve` OpenAPI schema. The router was registered and callable, but its tag was missing from `OPENAPI_TAGS`, so `/docs#/stream` rendered as a bare, description-less section outside its logical Data group. A new smoke test asserts every router tag has an OpenAPI description block, so a new router can't ship invisible in `/docs` again.",
    ],
    "0.50.0": [
        "New: headless / token-only invocation (issue #359). Set `KBAGENT_PROJECT_FROM_ENV=1` together with `KBC_TOKEN` + `KBC_STORAGE_API_URL` and kbagent synthesizes an in-memory project under the reserved alias `__env__` -- no `kbagent project add`, no `config.json` on disk. Lets a daemon (e.g. the jasnost bridge), a container, or a CI job run any storage/job/config command with `--project __env__`, or talk to a `kbagent serve` started the same way. Both the CLI and `serve` resolve the project through the same `ConfigStore.load()` chokepoint, so both work from the single env-injection.",
        "UX: stack URLs are now normalized everywhere a project is created (`project add`, `project edit --url`, and the headless `__env__` injection). A bare host (`connection.keboola.com`), a trailing slash, surrounding whitespace, or even a full project deep-link (`https://connection.keboola.com/admin/projects/10105/dashboard`) are all reduced to the clean `https://<host>` base instead of erroring. Explicit non-https schemes (`http://`, `file://`, ...) are still rejected as an SSRF / protocol-abuse guard, and an unusable URL in `KBC_STORAGE_API_URL` now fails fast with a clean config error rather than a raw pydantic traceback.",
        "Security: the env-synthesized `__env__` project lives in memory only. It is marked `ephemeral` and stripped by `ConfigStore.save()`, so the `KBC_TOKEN` from the environment is never written to `config.json`. Opt-in is explicit (the `KBAGENT_PROJECT_FROM_ENV` flag, not the mere presence of `KBC_TOKEN`) to avoid a phantom project surprising a developer who exported the token only for `project add`. If the flag is set but the credential vars are missing, the CLI fails fast with a clear error instead of silently skipping. Mutating ops that target the synthesized project (`project remove/edit/rename`, branch switch) are rejected with an actionable message rather than reporting a success that silently vanishes on the next load. `project list` recovers the `project_id` offline from the token prefix; the real project name shows via `project status` / `project info`. The org-info backfill that `project status` runs skips the ephemeral project, so even `project status` writes nothing to disk in headless mode.",
        "New: `kbagent stream` command group for Keboola Data Streams (OpenTelemetry / OTLP). Provisions and introspects Stream sources from the CLI so an OTLP ingest endpoint can be created and read in one command instead of copy-pasting it out of the UI. Four subcommands: `stream list --project ALIAS [--branch N]` (sources with id/name/type/base endpoint); `stream create-source --project ALIAS --name NAME [--type otlp|http] [--branch N] [--if-not-exists] [--reveal]` (creates a source, polls the async task, returns the endpoint; `--if-not-exists` returns the existing source as `skipped`); `stream detail [SOURCE_ID | --name N] --project ALIAS [--branch N] [--reveal]` (assembles the base OTLP endpoint, per-signal endpoints `/v1/logs|/v1/traces|/v1/metrics`, protocol `http/protobuf`, and the destination bucket/tables read from the source's sinks); and `stream delete SOURCE_ID --project ALIAS [--branch N] [--dry-run] [--yes|--force]` (destructive, async task polled to completion). The Stream control-plane API lives on a separate host derived from the project's Storage URL (`connection.<region>` -> `stream.<region>`, same scheme as `ai.`/`queue.`) and authenticates with the per-project Storage API token (`X-StorageApi-Token`) -- no manage token, no extra prompt. The OTLP **ingestion** endpoint (`stream-in.<region>/otlp/<projectId>/<sourceName>/<secret>`) embeds its secret in the URL path; it is returned by the API in `source.otlp.url` (never derived) and is **masked by default** in every surface (endpoint, per-signal endpoints, and the raw `--json` source echo) -- pass `--reveal` to print the real secret, e.g. so a daemon can wire `OTEL_EXPORTER_OTLP_ENDPOINT` from `stream detail --reveal --json`. Creating an OTLP source via the raw Stream API creates only the bare source (no sinks), so -- matching the Keboola UI -- `stream create-source --type otlp` **auto-provisions the three standard sinks** (logs/metrics/traces) into bucket `in.c-otlp-<sourceId>` (table per signal, mapping = `uuid id` + ingest `datetime` + a `body` column holding the full flattened OTLP record as JSON) so data actually lands in Storage; the provisioning is idempotent (only missing signals are added, so a re-run or `--if-not-exists` against a half-set-up source heals it) and `--no-sinks` opts out for a bare source. `stream detail` reports an empty destination only when no sinks exist (no invented defaults). Permission classes: `stream.list` / `stream.detail` = read, `stream.create-source` = write, `stream.delete` = destructive. New layers: `stream_client.py` (`StreamClient`, source/sink CRUD + async task polling), `services/stream_service.py` (`StreamService`, alias resolution + secret masking + detail assembly), `commands/stream.py`, and a 1:1 `kbagent serve` REST router (`server/routers/stream.py`, 4 endpoints). Tests: `tests/test_stream_client.py` (14), `tests/test_stream_service.py` (16), `tests/test_stream_cli.py` (11); full create->detail->delete E2E `tests/test_e2e.py::test_stream_otlp_e2e` (opt-in via `make test-e2e-stream`). Live-validated against project 10539: create source -> 3 sinks provisioned -> POST OTLP/HTTP JSON logs to `/v1/logs` -> 3 rows landed in `in.c-otlp-<name>.logs` -> read back via `workspace query`. Closes keboola/cli#357.",
    ],
    "0.49.0": [
        "New: `kbagent dev-portal` command group — v1 operations against the Keboola Developer Portal (`apps-api.keboola.com`). Lets component developers inspect and update portal entries without leaving the terminal. Read commands (`dev-portal list --vendor V`, `dev-portal get --app VENDOR.APP_ID`) are unrestricted and support peer-config research (pull reference schemas from existing extractors/writers for design reference). Write commands (`dev-portal create`, `dev-portal patch`, `dev-portal upload-icon`, `dev-portal publish`, `dev-portal deprecate`) always print the full pending request diff and then require the user to type a random hex code on a real terminal; there is no `--yes` flag and no env-var bypass; non-TTY shells exit 6 (`EXIT_PERMISSION_DENIED`). `--dry-run` produces the same preview and exits 0 -- the agent-safe path.",
        "New: multi-identity credential storage for the Developer Portal. Portal logins (email + password, with optional MFA for personal accounts) are stored per-alias in the same `config.json` as KB project tokens under 0600 protection. Identity lifecycle: `dev-portal identity add --alias A --username U [--password P | --password-stdin] [--role-hint vendor|admin] [--vendor V]`, `identity list`, `identity remove`, `identity edit`, `identity use ALIAS`, `identity current`, `identity verify`.",
        "Refactor: `require_random_code_confirmation()` extracted to `commands/_helpers.py` as a single shared implementation. Used by `permissions set`, `permissions reset`, and all five Developer Portal write subcommands. Previously each call site maintained its own copy of the TTY check + prompt loop.",
        "New: 15 `dev-portal.*` permission-registry operations (`dev-portal.list`, `dev-portal.get`, `dev-portal.create`, `dev-portal.patch`, `dev-portal.upload-icon`, `dev-portal.publish`, `dev-portal.deprecate`, `dev-portal.identity.add`, `dev-portal.identity.list`, `dev-portal.identity.remove`, `dev-portal.identity.edit`, `dev-portal.identity.use`, `dev-portal.identity.current`, `dev-portal.identity.verify`, `dev-portal.identity.get`) registered with appropriate risk categories in the firewall permission engine.",
    ],
    "0.48.0": [
        "New: `kbagent feature` command group for managing Keboola feature flags via the Manage API (requires a super-admin manage token, the same kind `org setup` uses). Seven subcommands: `feature list --project ALIAS` (the stack-wide feature catalogue, GET /manage/features); `feature project-show --project ALIAS` (features assigned to a project, read from the project object's `features` array); `feature project-add` / `feature project-remove --project ALIAS --feature NAME [--dry-run] [--yes]` (POST/DELETE /manage/projects/{id}/features); and `feature user-show` / `feature user-add` / `feature user-remove --project ALIAS --email EMAIL [--feature NAME]` for per-user features (GET/POST/DELETE /manage/users/{email}/features). The `--project ALIAS` resolves the stack URL (and, for project ops, the numeric project_id) from the kbagent config -- the alias is the only handle needed. The manage token follows the same default-deny policy as `org`: read from an interactive hidden prompt, never persisted, never a CLI argument; pass the top-level `--allow-env-manage-token` to read `KBC_MANAGE_API_TOKEN` from env (CI/CD). Write paths support `--dry-run` and an interactive confirm (skip with `--yes`). Permission classes: `list` / `*-show` = read, `*-add` = admin, `*-remove` = destructive. The Manage API has no published feature schema, so the new `Feature` model treats only `name` as stable and passes extras through unmodified; project/user `features` arrays returned as bare strings are normalised to `{name: ...}`. New layers: `ManageClient.list_features` / `add_project_feature` / `remove_project_feature` / `get_user` / `add_user_feature` / `remove_user_feature` (email + feature url-encoded in the path; the POST add paths tolerate a 204 No Content body), `FeatureService`, `commands/feature.py`, and a 1:1 `kbagent serve` REST router (`server/routers/feature.py`, 7 endpoints, each requiring the `X-Manage-Token` header). Human-mode tables are adaptive: the stack catalogue keeps Title/Type/Description, while project/user views (returned by the Manage API as bare strings) collapse to just Name. Tests: `tests/test_feature_service.py` (19), `tests/test_feature_cli.py` (21), `tests/test_manage_client.py` + `tests/test_models.py` extensions; read-only E2E `tests/test_e2e.py::test_feature_flags_read_e2e` (opt-in via `make test-e2e-feature`).",
    ],
    "0.47.2": [
        "Fix (`sync push`, fresh-CREATE variable binding): a transformation scaffolded alongside its sibling `keboola.variables` config + default-values row is now runnable after a single `sync push`. Three defects in the create pass are fixed together. (KFR-04) The row's `values: [...]` array was silently dropped because `local_row_to_api` only hoisted `values` into the API body when the row file already carried a `_keboola.component_id`; the two push callers now pass the known `component_id` explicitly, so a scaffold row without a `_keboola` block still hoists. (KFR-05) Rows whose parent `keboola.variables` config was created **in the same push** raised `PARENT_CONFIG_NOT_TRACKED` (or POSTed against a non-existent placeholder id): `push()` now runs in ordered phases -- configs first, then rows -- capturing each created config's placeholder id -> assigned ULID and remapping every row's `parent_config_id` to the ULID before the manifest lookup and `create_config_row(config_id=...)`. (KFR-03) The transformation's remote `configuration.variables_id` / `variables_values_id` stayed as placeholder dirnames (so `job run` failed with `Variable configuration \"<placeholder>\" not found`); a new Phase-C backfill resolves each placeholder to the ULID assigned this push and PUTs the corrected configuration body via `update_config` (NOT `set_variables`, which would create a second variables config), then rewrites the local `_configuration_extra` and refreshes the manifest `pull_hash` / `pull_config_hash` / `pull_extra_hashes` so a re-push is clean. When the placeholder key misses but exactly one `keboola.variables` config was created this push, it binds to that one with a warning; zero or ambiguous (>1) matches accumulate a `variable_link` error rather than writing a broken link. Downstream (FIIA) can delete its post-push `config variables-set` workaround. Tests: `tests/test_sync_config_format.py::TestLocalRowToApiComponentIdParam`, `tests/test_sync_service.py::TestFreshCreateVariableBinding` (end-to-end bindings, idempotent re-push, single-config fallback, ambiguous-config error), plus an E2E (`job run --wait` -> success) in `tests/test_e2e.py`.",
        "Fix (`sync push --branch <id>`, KFR-07): pushing the local default tree to a target dev branch no longer errors with `Config file not found`. Source (where files live on disk) and target (where the API writes) are now decoupled: when no materialized `<branch_name>/` subtree exists for the target branch, the default tree (`main/`) is read as the source and promoted to the target branch; all API calls still target the branch id. A new `SyncService._resolve_source_branch_path` drives the local-read path in `push` / `diff` / `_push_create` / `_push_update` / `_push_row_change`; per-config tracked reads continue to use each entry's own `branch_id`. Backward-compatible: when the per-branch subtree exists (multi-branch-directory users), behaviour is unchanged. Tests: `tests/test_sync_service.py::TestFreshCreateVariableBinding::test_resolve_source_branch_path_promotes_default_tree`.",
        "Fix (docs, `sync/diff_engine.normalize_for_comparison`): corrected a stale docstring that claimed `_configuration_extra` is stripped during normalization. It is **not** stripped -- it carries real config payload (`keboola.flow` phases/tasks, a transformation's `variables_id` / `variables_values_id` links) and is part of `config_hash`. The docstring now documents that any code mutating `_configuration_extra` must refresh the stored `pull_config_hash` afterwards or `sync diff` reports a conflict. No logic change.",
    ],
    "0.47.1": [
        'Fix (`storage create-table --if-not-exists`, keboola/cli#349): the `action: "skipped"` envelope now reports the EXISTING table\'s actual schema instead of re-echoing the caller\'s request. Pre-0.47.1, `columns` / `primary_key` / `name` on a skip mirrored the args the caller passed in, so a caller probing the skipped envelope to discover the real shape of a pre-existing table got the wrong values whenever the existing table differed from the request. The `get_table_detail(target_id)` lookup that already runs to confirm the table exists is now also the source of the returned schema. The caller\'s requested values are preserved under two new fields, `requested_columns` and `requested_primary_key`, and a new `schema_drift: bool` flags when the existing table diverges from the request (set comparison on columns and primary key). Human-mode output prints the actual schema on a skip and emits a `Warning:` line when `schema_drift` is true. `action: "created"` envelope is unchanged. No new flag, no signature change. Tests: `tests/test_storage_write.py` (skipped returns actual schema, drift flag set on divergence, no drift on match, human-mode warning render); `tests/test_e2e.py::TestE2E_0_47_0_NewSurfaces` extended to assert the skipped envelope reports actual columns + `requested_*` mirror.',
        'Fix (`workspace create`, keboola/cli#351): new Snowflake sandbox workspaces now request `loginType: "snowflake-person-keypair"` and generate a local RSA key pair for the Storage API `publicKey` field, so the created workspace uses the Query-Service-compatible login type instead of the backend default. The one-time creation envelope now includes `private_key` for Snowflake workspaces and keeps `password` for compatibility, usually empty on key-pair workspaces; human output prints the private key and warns that it cannot be retrieved later. BigQuery workspaces still omit `loginType` and `publicKey`. Tests cover the Storage client payload, service-layer Snowflake/BigQuery branching, CLI JSON/human output, and Snowflake E2E `private_key` presence.',
    ],
    "0.47.0": [
        "Fix (sync push, fresh-CREATE): pre-existing placeholder manifest entries -- the FIIA / scaffold emit pattern, where a downstream tool seeds `.keboola/manifest.json` with placeholder ids and (optionally) `KBC.configuration.*` metadata before the first push -- are now updated **in place** by the create path instead of unconditionally appended. Pre-0.47.0 every create did `manifest.configurations.append(ManifestConfiguration(...))` (and `parent.rows.append(...)` for rows), so N placeholders -> 2N manifest entries after one push, every placeholder still looked `added` on re-push (spurious duplicates on remote), and any `metadata.KBC.configuration.folderName` declared in the placeholder was silently dropped on the floor. Two new private helpers do the work: `SyncService._writeback_create_config_in_manifest(...)` finds the placeholder by `(branch_id, component_id, path)` -- branch is part of the key so a multi-branch manifest with the same logical path under two branches updates the right entry -- and refreshes its id + pull_hash / pull_config_hash while preserving every non-bookkeeping metadata key; `SyncService._writeback_create_row_in_manifest(...)` does the same for rows under their parent. Idempotency on re-push falls out for free: the now-real config id flows through the existing diff engine and the second push reports `status: no_changes, created: 0`. Tests: `tests/test_sync_service.py::TestFreshCreateWriteback` (7 cases incl. an end-to-end placeholder + KBC-metadata round-trip). Manifest contract change for downstream parsers: a single CREATE now produces a single manifest entry (not placeholder + new). Downstream tooling that has been working around the duplication by post-processing must drop that workaround. Live-validated against project 1143 / dev branch 388071: placeholder with `KBC.configuration.folderName: 'Area B E2E Folder'` -> `created=1, errors=0`, manifest length 1, folderName visible via `config metadata-list`, re-push -> `no_changes`.",
        "New: `kbagent semantic-layer search-context --project P [--pattern G ...] [--type model|dataset|metric|relationship|constraint|glossary|all] [--limit N]` and `kbagent semantic-layer get-context --project P --context-id ID`. Two project-wide read subcommands that mirror the upstream `keboola-mcp-server` semantic-context tools (`search_semantic_context`, `get_semantic_context`) so downstream callers (FIIA, scheduled agents, pre-flight scripts) can drop the MCP dependency for the common 'is the model populated?' + 'what's at this id?' lookups. `search-context` is project-wide (not model-scoped); patterns are repeatable, taking the union; case-sensitive `fnmatch` against `attributes.name`; `--limit` short-circuits both inner and outer loops. `get-context` probes `semantic-model` first (model hits short-circuit on the first probe) then every `CHILD_TYPES` entry until a 200 lands; raises `NOT_FOUND` after all 6 misses; non-404 errors (500, etc.) propagate immediately rather than being swallowed. Response envelope: `{project, contexts: [{id, type, name, description, attributes}], total_count}` for search; `{project, id, type, name, description, attributes}` for get. The wire-level `\"semantic-\"` prefix is stripped from the response `type` field for CLI ergonomics (`dataset` not `semantic-dataset`). Both registered as `read` operations in the permission engine. Sync surfaces touched: `commands/semantic_layer.py`, `services/semantic_layer_service.py`, `server/routers/semantic_layer.py` (1:1 CLI->HTTP), `hints/definitions/semantic_layer.py`, `permissions.py`. Tests: `tests/test_semantic_layer_service.py::TestSearchContext` (12) + `::TestGetContext` (6); `tests/test_semantic_layer_cli.py::TestSearchContext` (4) + `::TestGetContext` (3). Live-validated against project 1143: returns 8 contexts spanning 4 types; pattern `rev_*` + `--type metric` narrows to 1 hit; round-trip search -> get-context on the returned id resolves correctly; UUID `00000000-0000-0000-0000-000000000000` returns NOT_FOUND after probing all 6 types.",
        "New: `kbagent sync push --branch <id>`, `sync pull --branch <id>`, `sync diff --branch <id>`. Per-invocation dev-branch override that wins over `manifest.branches[0]`, `active_branch_id` (`kbagent branch use`), and the git-branching `branch-mapping.json` -- new priority 0 in `SyncService._resolve_branch_id`. Lets an operator or downstream tool target a freshly-created dev branch without first running `branch use` or `sync branch-link`. Validated mutually exclusive with `--all-projects` at the CLI layer (branch id is per-project). Symmetric across push / pull / diff for predictable UX. Threaded through `branch_override=` kwargs on `SyncService.push / pull / diff`. Live-validated against project 1143: `sync diff --branch 388072` reports `remote_only: 31` (configs visible on the dev branch); without `--branch` the same call reports no remote diff.",
        'New: `kbagent storage create-table --if-not-exists`. Opt-in idempotency flag for parallel-worker patterns (e.g. FIIA\'s 8-worker `scaffold_storage.py`). When set, catches the specific `STORAGE_JOB_FAILED` + \'already has the same display name\' error from the Storage API, probes `get_table_detail(target_id)` to confirm the table really exists at the expected id, and returns `{action: "skipped", skip_reason: "table already exists", table_id: ...}` instead of raising. A different table with the same display name still surfaces the original error (a real conflict to resolve). Defaults to `False` so existing callers are byte-for-byte unaffected. Response envelope gains `action: "created" | "skipped"` so programmatic callers can branch on outcome. Error-code gate uses `ErrorCode.STORAGE_JOB_FAILED` (no raw string literal -- `make check-error-codes` enforces enum usage). Live-validated against project 1143 / dev branch 388072: first create -> `action: created`; second create (same name, with flag) -> `action: skipped`; third create (same name, no flag) -> original `STORAGE_JOB_FAILED` envelope.',
        "New: `kbagent sync push --no-name-drift-warnings`. Opt-out flag that drops the cosmetic `name_drift_warnings` array from the result envelope when local directory names differ from the canonical kbagent naming (e.g. FIIA's `var-07-fi-daily-date-refresh` pattern). The underlying detection still runs, so flipping the flag does not lose any audit data; only the report is suppressed. Defaults to `False` so existing callers see the warnings exactly as before.",
        'Note (sync `serve` exposure): the four sync subcommands (`init`, `pull`, `push`, `diff`) remain filesystem-local and intentionally have no HTTP endpoints in `src/keboola_agent_cli/server/routers/`. The plugin-sync map permits this exemption for terminal-only / filesystem-bound commands (CONTRIBUTING.md "Plugin synchronization map"). The new `--branch` and `--no-name-drift-warnings` flags consequently also have no REST counterpart.',
    ],
    "0.46.1": [
        "Fix (plugin): the `kbagent` Claude Code skill and the `keboola-expert` subagent now surface `kbagent data-app logs` (shipped in 0.43.8). The SKILL.md `description:` trigger list gained `data-app logs, container logs, app logs, tail logs, build logs, app stdout, app stderr, troubleshoot data app, debug data app`, and the keboola-expert Tool Selection Matrix gained a row for `kbagent data-app logs --project P --app-id N [--lines N | --since ISO8601]` (0.43.8+). Before this, asking the agent for a data app's container logs fell back to the UI Terminal Log tab or the 20-line-capped `get_data_apps` MCP tool. No CLI behavior change. (#335 / #336)",
        "Chore (frontend dev tooling): bumped the `web/frontend` dev dependencies -- vite 5 -> 8, vitest 2 -> 4, and `@vitejs/plugin-react` 4 -> 5.2 to keep the peer range consistent with vite 8. The earlier Dependabot PRs (#337, #338) bumped only vite + vitest, which left plugin-react pinned below vite 8 and broke `npm ci` (ERESOLVE) in the Windows wheel-build job, silently shipping a UI-less wheel. No runtime change to the CLI or the bundled SPA. (#341)",
    ],
    "0.46.0": [
        "Change: the project's canonical home moved from `github.com/padak/keboola_agent_cli` to `github.com/keboola/cli`. The auto-update and self-install constants (`KBAGENT_GITHUB_REPO`, `KBAGENT_INSTALL_SOURCE` in `constants.py`) now point at the new repo, so the startup version check hits `api.github.com/repos/keboola/cli/releases/latest` and `kbagent update` installs from `git+https://github.com/keboola/cli`. The Claude Code plugin manifest (`plugin.json` homepage/repository), the marketplace install instructions (`/plugin marketplace add keboola/cli`), README / TUTORIAL / CONTRIBUTING, the `doctor` and `serve` install hints, and the AGENT_CONTEXT plugin-install snippet were all updated in lock-step. The marketplace name (`keboola-agent-cli`) and the `/plugin install kbagent@keboola-agent-cli` identifier are unchanged -- they are logical names, not repo paths.",
        "Note: existing installs migrate themselves in a single update cycle. GitHub serves a 301 from the old repo path to `keboola/cli`, and both auto-update phases follow it -- the version check uses `httpx` with `follow_redirects=True`, and `uv`/`git` follow the redirect on `clone`/`fetch` during install. Once a user is on >= 0.46.0 the baked-in constants already name the new repo, so the redirect is needed for at most one hop. The old `padak/keboola_agent_cli` path must NOT be re-created as a new repository, or the redirect dies and pre-0.46.0 installs are cut off from updates.",
    ],
    "0.45.0": [
        'Quality: cleared the entire `ty` (Astral type-checker) backlog -- 444 diagnostics down to 0 across `src/`, `tests/`, and `scripts/` (closes #280 PR-3). The post-edit + pre-commit `ty` gate is now flipped from warning-only to BLOCKING, so a newly introduced type error must be fixed before the edit completes; `make typecheck` (`uv run ty check`) exits 0 and is enforced going forward. Fixed systematically by category, not file-by-file: dynamic JSON surfaces annotated with explicit return types, `TypeGuard`s added for the `#secret` walkers in `_encryption.py` (`is_secret_key` / `_is_secret_name_value_pair`), `OutputFormatter.output`\'s `human_formatter` callback widened to `Callable[[Console, Any], object]` (the dual-output lambdas legitimately return a tuple of `console.print(...)` calls), and Pydantic `populate_by_name` constructors in `sync_service.py` switched to their camelCase aliases (`apiHost`, `defaultBranch`, `branchId`, ...) since `ty` models `__init__` from the alias, not the field name. `tests/` now resolves the shared `from helpers import ...` fixture via a new `[tool.ty.environment] extra-paths = ["tests"]`.',
        "Fix (real bugs surfaced by the `ty` pass): the `kbagent serve` REST router layer had silently drifted out of sync with the service-layer signatures it calls -- roughly a dozen endpoints would have raised `TypeError` on the first request because no per-endpoint test covered them. `POST .../rename` passed `new_name=` (service param is `name`); `POST .../folder` passed `folder=` (param is `folder_name`); `GET /configs/search` passed `regex=` (param is `use_regex`); `describe-column` passed `column_descriptions=` (param is `columns`); `file-download` passed a non-existent `output_dir=` (param is `output_path`, and `download_file` is now directory-aware so passing a dir saves the file inside it under its own name instead of clobbering the dir); `data-app password` never forwarded the required `manage_token` (now sourced from the `X-KBC-ManageApiToken` header via the existing `get_manage_token` dependency, the same secure path org/members endpoints use -- the token is still never read from env/argv by default); `variables-set` forwarded a `dry_run` the service does not implement (removed from the call and the request model). Also corrected `str|None`->`str` / `str`->`Path` coercions on `create_config` / `create_config_row` / `rename` request bodies. These were behaviour bugs, fixed by correcting the calls (the service layer is the source of truth), not by suppressing the checker.",
        "Deprecation: the `--hint client|service` global flag is now deprecated in favour of the `kbagent serve` REST API, which covers every command. The flag STILL WORKS but prints a deprecation warning to stderr (the generated Python still goes to stdout, so existing pipelines are unaffected) and will be removed in a future release. Rationale: the REST surface (live since the serve work) fully supersedes hint-mode code generation, and maintaining a parallel Python-codegen path for every command is redundant. Docs updated across `docs/hint-mode.md`, `README.md`, `commands/context.py` AGENT_CONTEXT, `CLAUDE.md`, and the plugin surfaces (`SKILL.md`, `keboola-expert.md`, `commands-reference.md`, `gotchas.md` with a `(since v0.45.0)` entry, `programming-with-cli.md`).",
        "Improved: the `User-Agent` that signs every Keboola API call is now built once in `BaseHttpClient` (new `build_user_agent()` helper) instead of being hardcoded five times across the HTTP clients, and is enriched from `keboola-agent-cli/<version>` to `keboola-agent-cli/<version> (<os> <release>; <arch>; <impl> <pyver>)` -- e.g. `keboola-agent-cli/0.45.0 (Darwin 25.3.0; arm64; CPython 3.12.7)`. Only neutral host metadata is sent (never `platform.node()` / hostname, which is PII). This lets Keboola's edge (DataDog access logs) segment CLI traffic by version and OS/arch for fleet observability; verified live against production (the enriched UA appears verbatim in the `job-queue-api` access log). Identity (which project/user) remains resolved server-side from the token -- the CLI never decodes or logs it. New `APP_NAME` constant centralises the distribution name for both the `importlib.metadata` version lookup and the UA product token.",
        'Security: replaced a realistic-looking (and, as verified, now-dead -- HTTP 401) Storage API token that had lived in ~20 test files since the very first commit of this PUBLIC repo with an obviously-fake placeholder (`901-55555-fakeTestTokenDoNotUseXXXXXXXX`). It was never a live credential risk at the time of this change, but it tripped secret scanners and modelled a bad pattern for contributors. The `901-` project-ID prefix is preserved because ~15 mock `client_factory` test doubles discriminate projects by `"901" in token`; the secret-looking suffix is what was scrubbed. `pytest-httpx` mocks never transmit the value regardless.',
    ],
    "0.44.0": [
        'New: `kbagent agent <verb>` -- full CLI parity for the `/agents` REST surface that `kbagent serve` has exposed since v0.40.0. Twelve subcommands: `list`, `show`, `create`, `update`, `delete`, `run [--stream] [--runtime-prompt | --runtime-input]`, `runs`, `run-detail`, `run-events`, `test [--stream]`, `cron-preview --cron "..." [--count N]`, `prompt-improve --goal "..." [--cli claude|codex|gemini] [--stream/--no-stream]`. Pure-local: `AgentService` reads/writes `<config_dir>/agents.json` directly, so CRUD + ad-hoc `run` work offline; the cron loop that fires scheduled tasks still requires `kbagent serve` running, but the on-disk format is identical -- a CLI-created task fires on its cron as soon as the server boots and reads the file. Three action flavours mirror the REST endpoint byte-for-byte (`ai_agent` / `cli_command` / `mcp_tool`); `--from-file PATH|@path|-` takes the full `{type, params}` JSON envelope, and convenience flags (`--cli + --prompt + --extra-arg`, `--argv` repeatable, `--tool + --input + --mcp-project + --mcp-branch`) cover the common single-action case. `--runtime-prompt` (ai_agent-only) appends ad-hoc text to the persisted prompt for one run; `--runtime-input` merges arbitrary JSON into action params. Streaming variants render events line-by-line in human mode and NDJSON under `--json`. Trigger chaining via `--trigger-task-id ID --trigger-on success|error|always` with the same cycle/self-loop validation the REST router applies. Every subcommand accepts its `TASK_ID` / `RUN_ID` positionally OR via `--id` / `--task-id` (and `--run-id` for `run-detail` / `run-events`), matching the rest of the CLI (`--job-id`, `--config-id`, ...). Boundary helpers `validate_trigger` + `merge_runtime_input` were extracted from `routers/agents.py` into `server/agents_store.py` so the REST router and `AgentService` share the exact same behaviour byte-for-byte; a new blocking `POST /agents/prompt/improve` mirrors the SSE variant for scripted callers. `croniter` moved from `[server]` extras to core dependencies (cron-preview validation runs outside serve); `server/__init__.py` was split into a PEP 562 lazy shim + new `server/app.py` so importing `from keboola_agent_cli.server.agents_store import AgentStore` no longer drags FastAPI/uvicorn into the CLI path -- the agent CLI works on plain installs without `[server]` extras. The permission registry adds 12 entries (`list / show / runs / run-detail / run-events / cron-preview` = read, `create / update / run / test / prompt-improve` = write, `delete` = destructive); hint definitions ship an intentionally-empty `agent.py` (agent CRUD is pure-local, with no Keboola HTTP API to mimic). Tooling hardening: `make version-sync` now also pins the `keboola-agent-cli` self-version in `uv.lock`, and `version-check` guards `plugin.json` + `marketplace.json` + `uv.lock` together. Tests: 23 unit (`test_agent_service.py`) + 29 CLI (`test_agent_cli.py`, every subcommand in human + `--json`, positional + `--id`/`--task-id`/`--run-id` alias + conflict/missing-id paths) + 3 E2E (`tests/test_e2e.py::TestE2EAgentTasks`). Closes the gap between the React UI sidebar "Agent Tasks" (live since v0.40.0) and CLI users who previously had to fall back to `kbagent http <verb> /agents...` from inside scheduled subprocesses.',
    ],
    "0.43.9": [
        "Fix: `kbagent data-app list` leaked workspace/sandbox deployments into the data-app listing. The Data Science `GET /apps` collection returns EVERY deployment in the project -- not just data apps but also interactive Snowflake/BigQuery workspaces (`componentId=keboola.sandboxes`, `type=snowflake`/`bigquery`, no name, a `*.snowflakecomputing.com` URL). `list_data_apps` merged the whole collection, so a project with sandboxes showed phantom unnamed `(snowflake)` rows that do NOT appear in the Apps UI (which filters to `keboola.data-apps`). **Fix:** `list_data_apps` now skips any deployment whose `componentId` is present and not `keboola.data-apps`; an item that omits `componentId` (older API shape) is kept rather than hidden, so we never drop a row we cannot classify. The list envelope gains a `component_id` field per app for transparency. Verified live against a project with 4 sandboxes + 1 data app: the listing went from 5 rows to the single real data app, matching the UI. Tests: `tests/test_data_app_plain_env_keys.py::TestListSandboxFilter`.",
        'Fix: `kbagent data-app secrets-get` and `secrets-remove` refused any key without a leading `#`, even though `secrets-list` enumerated those keys. The `parameters.dataApp.secrets` block legitimately holds BOTH `#`-prefixed encrypted secrets (value = `KBC::ProjectSecure*::...` ciphertext) AND plain unencrypted env-var config values (e.g. `ADMIN_EMAILS`, `SMTP_HOST`). `list_data_app_secrets` had no key validation and listed all of them; `get_data_app_secret`/`remove_data_app_secrets` ran `_validate_secret_key`, which enforced `SECRET_KEY_PATTERN = ^#[A-Za-z][A-Za-z0-9_-]{0,63}$` (mandatory `#`), so a plain key like `ADMIN_EMAILS` failed with *"Invalid secret key ... Keys must start with \'#\'"* -- listable but neither readable nor removable. **Fix:** `_validate_secret_key` gained a `require_hash` parameter (default `True`); a new `SECRET_OR_PLAIN_KEY_PATTERN = ^#?[A-Za-z][A-Za-z0-9_-]{0,63}$` (optional `#`) is used by the read/remove paths (`require_hash=False`), while `secrets-set` keeps `require_hash=True` because it encrypts and `#` carries meaning. `secrets-get` now dispatches on whether the stored value is a `KBC::` ciphertext: for an ENCRYPTED secret it stays metadata-only (`encrypted: true`, `value: null`, `fingerprint`/`encryption_prefix` -- the Encryption API has no decrypt endpoint, so the decrypted plaintext is still NEVER exposed), and for a PLAIN value it returns the literal value (`encrypted: false`, empty `fingerprint`/`encryption_prefix`) since that value is already stored in clear and visible via `config detail`. Lookup remains exact-match (no `#KEY`<->`KEY` fuzzing), so behaviour for existing `#` keys is unchanged. JSON envelope gains `encrypted` (bool) and `value` (string | null); human mode prints `fingerprint=... prefix=...` for encrypted keys and `value (plaintext, unencrypted): ...` for plain keys (with a stderr note that the value is unencrypted). Shape note for downstream consumers: `fingerprint`/`encryption_prefix` are now ALWAYS present but are EMPTY strings for plain keys (they used to be a reliable non-empty proxy for "is encrypted") -- the new `encrypted` bool is the canonical discriminator; `value` is `null` for encrypted keys and a string for plain keys. `secrets-set` is intentionally NOT changed -- adding a plain env var is `config update`, not `secrets-set`. Sync surfaces touched: `services/data_app_service.py`, `commands/data_app.py`, `commands/context.py` AGENT_CONTEXT, `CLAUDE.md ## All CLI Commands`, `plugins/kbagent/skills/kbagent/references/commands-reference.md`, `data-app-workflow.md`, and `gotchas.md` (new `(since v0.43.9)` entry; the existing "secrets-get NEVER echoes decrypted plaintext" gotcha clarified to scope it to encrypted values). The `data-app.secrets-get` hint description (`hints/definitions/data_app.py`) and the `secrets-remove` empty-keys error message were also updated to drop the now-inaccurate "always metadata-only" / "#KEY required" wording. Tests: service-layer coverage for get-plain (`encrypted=false` + value), get-encrypted (`encrypted=true`, `value=None`, metadata preserved), remove-plain, and continued rejection of malformed keys; CLI coverage for the plain-value human + JSON output; an E2E step in `test_e2e.py::test_data_app_secrets_round_trip` injects a plain key via `config update`, reads it back, and removes it (and the stale `removed == 1`/`== 0` assertions there were corrected -- `removed` is a list of env-var names, not a count).',
    ],
    "0.43.8": [
        'Fix: every `kbagent` invocation silently failed to auto-update `keboola-mcp-server` and printed a misleading two-line warning to stderr, leaving the fleet pinned to the stale MCP server v1.32.0 (closes #324). **Root cause:** `keboola-mcp-server` >= 1.55.0 declares a pre-release-only transitive dependency, `toon-format~=0.9.0b1`. On PyPI `toon-format` ships exactly two releases -- `0.1.0` (stable) and `0.9.0b1` (pre-release) -- so the `~=0.9.0b1` constraint can only be satisfied by the pre-release. uv refuses pre-releases by default, so the bare `uv tool upgrade keboola-mcp-server` could not resolve the latest MCP; instead of erroring, uv\'s resolver backtracked to v1.32.0 (the last release predating the pin) and exited 0. kbagent\'s post-upgrade version check (the #263 "Bug E" guard) then correctly saw `pre_version == post_version` and emitted a diagnostic -- but the text blamed Python/transitive-dep mismatch and pointed at `uv tool install --reinstall keboola-mcp-server`, which hits the identical wall. **Fix:** all three MCP install paths in `services/version_service.py` (`uv_tool` -> `uv tool upgrade`, `pip_env` -> `pip install --upgrade`, `uvx` -> `uv tool install --upgrade`) now pass the pre-release opt-in (`--prerelease=allow` for uv, `--pre` for pip), as does the user-facing `upgrade_command` shown by `kbagent version` (including the fresh-install `none` case). The same opt-in is applied to every other path that installs the MCP server: `mcp_service.ensure_mcp_installed` (run by `kbagent doctor --fix`), the uvx-fallback hint, the `kbagent doctor` MCP health-check install hint, and the first-time-setup command in `SKILL.md`. Note: `--prerelease=if-necessary` does NOT fix this -- a *stable* `toon-format` (0.1.0) exists, so uv judges a pre-release "unnecessary" and then fails the pin; only `--prerelease=allow` resolves it (verified empirically against uv 0.10.x). The opt-in is scoped to the MCP environment and never touches the kbagent self-update channel, which stays stable-only unless `--beta`.',
        "Fix: the auto-update diagnostic in `auto_update.py` no longer blames Python or recommends a remediation that fails the same way. When the resolver still backtracks (e.g. a future strict-equality pin), it now points at `uv tool install --reinstall --prerelease=allow keboola-mcp-server`. Both the upgrade flag and the diagnostic share a single self-documenting constant (`MCP_UV_PRERELEASE_FLAG`) that explains the toon-format pre-release pin.",
        "Tests: new regression coverage in `tests/test_version_service.py` asserts `--prerelease=allow` is present in the `uv_tool` and `uvx` upgrade commands and `--pre` in the `pip_env` command, that the user-facing `upgrade_command` for all four install methods carries the opt-in, and that `if-necessary` would be insufficient is documented inline. `tests/test_auto_update.py` asserts the corrected diagnostic string.",
        'New: `kbagent data-app logs --project ALIAS --app-id ID [--lines N | --since ISO8601]` -- thin wrapper around the Keboola Data Science `/apps/{app_id}/logs/tail` endpoint, returning the FULL container spin-up trace as plain text ([TIMING] git_clone, Cloning into /app, Using CPython 3.11.14, supervisord started with pid 1, runtime Node.js / Python stack traces, etc.). Closes a long-standing gap: the upstream `keboola-mcp-server` `get_data_apps` MCP tool hardcodes a 20-line cap (`_fetch_logs(..., lines=20)`), which is structurally too small to capture a healthy data-app spin-up -- the `uv install` + supervisord boot stanza alone is 30+ lines on a healthy `python-js` app; operators triaging a stuck deploy or a runtime crash had no CLI path and had to fall back to the Keboola web UI. The new command has no client-side cap (default `--lines 500`; `--lines 0` opts into the full current container buffer with no server-side limit -- translated to "send no params" at the command layer since the server rejects `lines=0` with a 400). `--lines` and `--since` are mutually exclusive on the server (both -> HTTP 400 `Only one of "since" or "lines" can be set`); the command rejects the combination locally with exit 2 + `USAGE_ERROR`, and `DataAppService.get_app_logs` has its own `INVALID_ARGUMENT` guard for programmatic / `--hint service` callers (defense at both audiences, not just defense-in-depth). `--since` is validated client-side via `datetime.fromisoformat` and requires an explicit timezone (`Z` or `+00:00`) -- naive datetimes are rejected before the round-trip with a clearer message than the server\'s bare `Invalid value` 400. Apps that have never started (or were created with `--no-deploy`) surface the server\'s `App "X" is not running` 400 verbatim -- recovery is `kbagent data-app start` or `data-app deploy`; no client-side reclassification because `BaseHttpClient._raise_api_error` already discards the `context.code` field, and a string-match on the message would be brittle. JSON envelope: `{project_alias, app_id, lines_requested, since_requested, lines_returned, text}` where `text` is the raw body (trailing `\\n` preserved as the server emits it) and `lines_returned` is `text.splitlines()` length; the request echo lets a downstream pipeline correlate envelopes to invocations. Human mode prints a styled header (`Logs for data app <id> in <project> (N lines)`) and the raw log body with `markup=False, highlight=False` so literal `[TIMING]`, `[INFO]`, and bracket-noise in the payload don\'t get interpreted as Rich tags and timestamps/URLs/IPs in lines aren\'t auto-colored. Permission registry: `data-app.logs = read` (Storage token only, no Manage token; safe under `--deny-writes` / `--deny-destructive`). Tests: 8 service-layer (`test_data_app_service.py::TestDataAppLogs` -- happy paths for `lines` / `since` / buffer-all, mutex `INVALID_ARGUMENT` rejection, empty-body, HTTP error propagation through finally, client cleanup-in-finally on `RuntimeError`, project resolution failure with `ConfigError`), 3 client-layer (`TestTailAppLogsClient` via `pytest-httpx` -- asserts URL composition, `+` URL-encoding in `since=`, clean URL when no params), 9 CLI (`test_data_app_cli.py::TestDataAppLogs` -- human + JSON output, mutex, invalid `--since` format, naive `--since` datetime, `--lines 0` -> `lines=None`, `--lines -5` rejection, API error -> exit 1, `--since` passthrough), plus 2 auto-fired hint compile checks via the `TestDataAppHintMode._SAMPLE_INVOCATIONS` parametrize for both `--hint client` and `--hint service`. **Security note: the log buffer can echo runtime secrets the app printed to stdout/stderr** (pandas tracebacks with connection strings, debug `os.environ` dumps, OAuth state, accidental `print(api_key)` in dev branches); the `--json` envelope reproduces the body verbatim with no masking -- false confidence is worse than honest passthrough; `gotchas.md` documents the consideration loudly. Sync surfaces touched: `commands/data_app.py` (new `data_app_logs`), `services/data_app_service.py` (new `get_app_logs`), `data_science_client.py` (new `tail_app_logs`), `hints/definitions/data_app.py` (new `data-app.logs` CommandHint), `permissions.py` (new `data-app.logs: read`), `commands/context.py` AGENT_CONTEXT, `CLAUDE.md ## All CLI Commands`, `plugins/kbagent/skills/kbagent/references/commands-reference.md`, `plugins/kbagent/skills/kbagent/references/gotchas.md` (new `(since v0.43.8)` section covering the MCP gap, mutex, secret-echo risk, JSON envelope). `make skill-gen` regenerates the SKILL.md decision table from the new command\'s docstring; `make version-sync` propagates `0.43.8` to `plugin.json`.',
    ],
    "0.43.7": [
        'Fix: Windows installation was completely broken -- two independent bugs in the wheel build hook (`hatch_build.py`) meant `uv tool install git+https://github.com/keboola/cli` had no working path on Windows (closes #320). **Bug 1 (npm present):** `npm` on Windows is a batch launcher (`npm.cmd`), and a bare `subprocess.check_call(["npm", ...], shell=False)` cannot resolve it -- `CreateProcess` raises `FileNotFoundError [WinError 2]`, which is a subclass of `OSError`, NOT `subprocess.CalledProcessError`, so the existing `except subprocess.CalledProcessError` let it propagate and kill the build. Two-part fix: (a) the hook now hands subprocess the *resolved* path from `shutil.which("npm")` (already computed for detection, previously thrown away) -- on Windows that is `...\\npm.cmd`, and `CreateProcess` runs a `.cmd` via the system shell even with `shell=False`, so the SPA actually builds; (b) the `except` is widened to `(subprocess.CalledProcessError, OSError)` so any spawn failure degrades to a UI-less wheel instead of aborting the build. **Bug 2 (npm absent):** the hook returned early without creating `src/keboola_agent_cli/_ui_dist/`, but `pyproject.toml`\'s unconditional `[tool.hatch.build.targets.wheel.force-include]` requires that path to exist -- hatchling failed the whole build with `Forced include not found`. Fix: every code path now guarantees `_ui_dist/` exists on return via `_ensure_target()` (an empty dir is enough -- hatchling includes zero files from it, and the runtime UI detector keys on `index.html`, which is absent, so `kbagent serve --ui` still surfaces the friendly "no UI bundled" error). CLI-only callers and the normal (UI-present) build path are byte-for-byte unchanged. New `KBAGENT_SKIP_UI_BUILD=1` build-time env var ships a deliberate CLI-only wheel (fast builds; exercising the no-UI path) -- checked before the prebuilt-dist probe so it wins even when a `dist/` exists. The build logic is extracted into a pure, hatchling-free `_bundle_ui(repo_root, log)` function so it is unit-testable in a plain dev venv (the `hatchling` import is guarded behind `TYPE_CHECKING` + a runtime `try/except` fallback to `object`).',
        'Tests: new `tests/test_build_hook.py` (20 tests) drives `_bundle_ui` directly to reproduce both bugs WITHOUT a Windows machine -- Bug 1\'s `FileNotFoundError` and `CalledProcessError` are simulated with mocks (asserting no propagation + that the resolved `npm.cmd` path, not the bare `"npm"`, reaches subprocess), and every early-return path is asserted to leave `_ui_dist/` existing (Bug 2). A new `windows-latest` CI job (`.github/workflows/ci.yml`) runs a real `uv build` on a free GitHub Windows runner (Node/npm preinstalled) and asserts via `scripts/check_wheel_ui.py` that a normal build bundles `_ui_dist/index.html` (Bug 1 truly fixed, not just degraded) while a `KBAGENT_SKIP_UI_BUILD=1` build produces a valid wheel with no SPA (Bug 2). The Bug-2 force-include + empty-dir interaction is OS-independent, so it is additionally proven by the local `uv build` path. No CLI command surface changed -- `## All CLI Commands`, `AGENT_CONTEXT`, `SKILL.md`, and `keboola-expert.md` are intentionally untouched (the only new knob is a build-time env var, not a runtime flag).',
        "Internal: `make changelog-check` (`scripts/generate_changelog.py --check`) now skips GitHub pre-release tags (PEP 440 betas/rcs, e.g. `v0.44.0b1`) instead of demanding a `CHANGELOG` entry for them. Per the CONTRIBUTING.md beta workflow a beta is tagged on its feature branch and its changelog entry rides along on that branch until the PR merges, so `main` must not require it -- mirroring the auto-update path, which only sees stable releases via GitHub's `/releases/latest` and ignores prereleases. Before this, an in-flight beta tag (`v0.44.0b1`, from the unmerged `kbagent agent` PR #310) made local `make check` red on every branch without ever turning CI red (`changelog-check` is local-only). The release-list filtering is extracted into a pure, I/O-free `audit_changelog_coverage(tags, changelog) -> (missing, checked, skipped)` helper, covered by 6 tests in `tests/test_changelog_check.py`; the surfaced count now reads e.g. `All 49 stable releases have changelog entries. (1 pre-release(s) skipped)`.",
    ],
    "0.43.6": [
        'New: `kbagent job run --mode run|debug` exposes the Queue API job `mode` body field, which was previously hard-coded to `"run"` inside `JobService.run_job` (the underlying `KeboolaClient.create_job` already accepted a `mode` kwarg but no service-layer or CLI path threaded it through). `--mode debug` flips the Queue worker into debug mode -- the component runs with the same configuration and inputs as a normal run, but its output stream is redirected into a Storage File tagged `debug-<jobId>` instead of into the destination buckets, so the run is safe to repeat on a production configuration for diagnostics (reproducing a failure, capturing the worker\'s actual output, A/B-comparing a flag change) without touching downstream tables. Default behaviour is unchanged: omit `--mode` and the body still carries `"mode": "run"`, the same wire shape every prior release sent. Validation lives at the service boundary (`KeboolaApiError` with `INVALID_ARGUMENT`) and the CLI also gates the flag with `click.Choice(sorted(VALID_JOB_MODES))`, so a typo (`--mode dry-run`) exits 2 with a Click usage error before any network round-trip -- it cannot reach the wire and surface as an opaque Queue API 422. The human-mode \'Running ...\' banner appends a bold-yellow `mode=debug` chip when the flag is non-default so operators see at a glance that a run is diagnostic, not production. The hint surface (`kbagent --hint client job run` / `--hint service`) emits the new `mode="..."` kwarg on both the `create_job` and `JobService.run_job` calls so AI agents that copy the rendered Python see the parameter inline. New constants `VALID_JOB_MODES = frozenset({"run", "debug"})` and `DEFAULT_JOB_MODE = "run"` in `constants.py`. Tests: 3 new service-layer tests in `test_services.py::TestJobServiceRunJobMode` (default lands as `mode="run"`, opt-in `mode="debug"` forwarded, unknown mode rejected at the service boundary and never reaches the wire), 3 new CLI tests in `test_cli.py::TestJobRun` (default, `--mode debug` forwarded, `--mode dry-run` exits 2 via the Click choice gate), and 2 new client-layer tests in `test_client.py::TestCreateJob` (`mode="run"` is in the POST /jobs body by default, `mode="debug"` reaches the body verbatim). Existing four `create_job.assert_called_once_with` assertions in `test_services.py` updated to include `mode="run"` since the call signature now always passes it. Plugin sync surfaces (silent-drift risks per convention #17): `CLAUDE.md ## All CLI Commands`, `commands/context.py::AGENT_CONTEXT`, `plugins/kbagent/skills/kbagent/references/commands-reference.md`, and `plugins/kbagent/skills/kbagent/references/gotchas.md` (new `(since v0.43.6)` entry) all updated so AI agents on the new version recommend `--mode debug` correctly.',
    ],
    "0.43.5": [
        "Fix: `MetastoreClient.post_item` now accepts both HTTP 409 (post go-monorepo PR #513) and the legacy HTTP 500 + `\"Failed to create meta object\"` body as the duplicate-name signal, normalising both into `ErrorCode.ALREADY_EXISTS` so command-layer error mapping (and the human-mode `'X with name Y already exists ...'` message) stays consistent across stacks during the metastore rollout. Before this release the workaround only matched the 500 shape; against a post-fix metastore the proper 409 Conflict would have bubbled up as a generic `API_ERROR` 'API error 409 ...' wrapper, blowing past the clean `ALREADY_EXISTS` path that command-layer error renderers special-case. Side benefit: 409 is not in `RETRYABLE_STATUS_CODES` (`constants.py`), so duplicate-name POSTs against a post-fix metastore stop being retried `MAX_RETRIES` times before the normalisation fires -- one round-trip instead of three. The 500 substring check is retained so unrelated 500s (DB outage, etc.) still surface as retryable `API_ERROR` rather than being miscategorised as a name collision. PATCH on a missing UUID is also fixed upstream (returns 404 now), but `BaseHttpClient._handle_error` already maps 404 -> `ErrorCode.NOT_FOUND`, so no client change is needed for that path. Docstring on `metastore_client.py` updated to describe both server-side shapes. New test in `test_metastore_client.py::TestDuplicateNameNormalization::test_duplicate_name_409_becomes_already_exists` registers a single 409 (asserting no retry happens) and verifies `error_code=ALREADY_EXISTS`, `status_code=409`, `retryable=False`, and the canonical user-facing message; the existing 500 + unrelated-500 tests stay green.",
        "Plugin docs: `plugins/kbagent/skills/kbagent/references/semantic-layer-workflow.md` documents the dual 409+500 shape so AI agents recommending raw HTTP calls (vs the `kbagent semantic-layer ...` group) know what shape to expect during the metastore rollout. No CLI surface change.",
    ],
    "0.43.4": [
        "Fix: `kbagent semantic-layer model delete` now cascade-deletes every child entity (dataset / metric / relationship / constraint / glossary term) before deleting the parent model (closes #306). Previously the call only DELETEd the parent row in the metastore; the children stayed on the wire pointing at the now-dead `modelUUID`. Because dataset names are unique **per project** (not per model), the next `kbagent semantic-layer build` or `import` that emitted a dataset of the same name (e.g. `addresses`) hit HTTP 422 `semantic-dataset with name 'X' already exists in the target model` with no UI / CLI escape -- the workaround was hand-enumerating `semantic-*` repository endpoints. The bug was always there but only became visible when the `http_base` error-body parser landed in 0.43.0 and started surfacing real metastore messages instead of bare `API error 422: 422`. `services/semantic_layer_service.py:delete_model` (and the extracted `_semantic_layer_cascade.py` helper) walk `reversed(PUSH_ORDER)` (constraints → glossary → relationships → metrics → datasets) and call `client.delete_item` per child before the parent. Partial-failure semantics match the `push_built_model` rollback envelope from PR #295: every child DELETE is wrapped in its own try/except so a sibling failure does not abort the cascade; if ANY child failed, the parent is **preserved** and a `KeboolaApiError` is raised with `details.cascade = {attempted, deleted, failures: [{type, id, name, error}], parent_deleted: False, model_uuid}` plus a recovery hint pointing at `kbagent semantic-layer model delete --project P --model <uuid>` to re-run. Success envelope adds a new `cascade.deleted` block with per-type counts. CLI prompt text updated from `If the model has datasets/metrics/etc. the API will refuse.` (stale -- the API did not refuse, it leaked) to `This cascade-deletes ALL child entities... This is irreversible.` Success renderer appends `+ cascaded N child(ren)` so operators see what was removed. The server router (`server/routers/semantic_layer.py:223`) inherits the new semantics with no code change.",
        "Deprecation: legacy `orphaned_children` top-level key on `semantic-layer model delete` JSON responses is deprecated as of this release; its shape is unchanged but its meaning flips from 'leaked count' to 'cascaded count'. Happy-path consumers always saw zeros on this key before this fix anyway -- the only way to populate it was the bug. New callers should read `cascade.deleted` (same per-type counts) plus the explicit `cascade.attempted` / `cascade.parent_deleted` / `cascade.failures` fields that disambiguate happy-path from partial-failure responses. Field removal is scheduled for a future minor release (not before v0.44.0); migration window is the gap between 0.43.4 and that release.",
        "Plugin docs: `plugins/kbagent/skills/kbagent/references/gotchas.md` adds a `(since v0.43.4)` entry noting `semantic-layer model delete` is cascade-by-default and that partial-failure responses carry `details.cascade.failures` for re-run targeting; `commands-reference.md` cascade row updated with the new envelope shape; `keboola-expert.md` tool-selection matrix flags the cascade behavior and the `orphaned_children` deprecation. No CLI surface change (flags / arg names unchanged), so `CLAUDE.md ## All CLI Commands` is unchanged.",
        "Tests: 282 lines of E2E coverage in `test_e2e.py::test_semantic_layer_delete_cascade` exercise the full regression loop (create model A + children → cascade-delete via CLI → assert envelope → create model B with same names → must succeed). 23 service-layer tests in `test_semantic_layer_service.py::TestDeleteModel` (happy path, partial failure, name conflict regression). 12 CLI tests in `test_semantic_layer_cli.py::TestModelDeleteCascade`. The cascade logic is extracted to `services/_semantic_layer_cascade.py` (146 LOC) keeping `semantic_layer_service.py` under the file-size budget. Total suite: 3341 passed, 26 skipped.",
    ],
    "0.43.3": [
        "New: `kbagent update --beta` (alternatively `KBAGENT_INCLUDE_PRERELEASE=1` per-shell env var) opts into pre-release versions. Default behaviour is unchanged -- the startup auto-update hook hits GitHub's `/releases/latest` endpoint, which is defined as the latest non-prerelease, non-draft release; betas marked `--prerelease` are invisible. With `--beta`, the version fetcher switches to `/releases` (plural) and picks the highest PEP 440 version including pre-releases (e.g. `0.44.0b1` beats `0.43.3`). The install command additionally propagates `--prerelease=allow` (uv) / `--pre` (pip) so the resolver accepts PEP 440 pre-release tags that it would otherwise refuse by default, AND appends `@v<version>` to the git+ install URL so uv installs the exact commit pointed to by the tag rather than the default branch (this matters when beta tags live on a feature branch, not main -- without `@v<version>` uv would always install the latest main commit, even though `_fetch_kbagent_latest_prerelease` advertised a different version). `kbagent version --beta` mirrors the same lookup for inspection. No `release_channel: beta` persistent config setting -- each opt-in is ad-hoc and explicit so a beta install is never a forgotten preference. CONTRIBUTING.md gets a new 'Releasing a beta' workflow section documenting the PEP 440 + `gh release create --prerelease` convention. 12 unit tests in `test_version_service.py` (default uses /releases/latest, prerelease uses /releases with PEP 440 sort, skips drafts, falls back to stable, ignores invalid tags, HTTP failure returns None, `build_kbagent_upgrade_command` propagates `--prerelease=allow` for uv + `--pre` for pip, prerelease+target_version appends `@v<version>` to git URL, stable install URL is unchanged when target_version not provided).",
    ],
    "0.43.2": [
        "UX: `kbagent changelog` now renders entries with Rich-styled prefixes (`New:` bold green, `Fix:` bold yellow, `Change:` bold blue, `UX:` bold magenta, `Note:` bold cyan, `Security:` bold red, `Closed:` bold blue; `Tests:` / `Plugin docs:` / `Internal:` / `Observability:` / `E2E:` / `Review fixes:` / `Why:` dim), cyan inline backtick spans (e.g. `kbagent serve --ui`), dim bullets, and a 4-space continuation indent under the bullet on the first line only. Body text is word-wrapped to terminal width via `Text.wrap()` (Rich's span-preserving wrap) so long entries no longer render as one wall of unwrapped text. Renderer-only change in `commands/changelog.py` (~70 lines added); the `CHANGELOG` data dict in `changelog.py` is untouched. JSON envelope (`--json changelog`) is byte-for-byte unchanged: data shape stays 1:1 with prior releases so AI agents that consume `kbagent changelog` as context see zero diff. Two implementation details that mattered: (1) wrap-output `Text` lines keep a trailing word-break space that surfaces as visible trailing whitespace on copy/paste -- fixed by calling `Text.rstrip()` in place on each wrapped line before printing; (2) the gutter `Text` is built styleless and the dim attribute is appended only to the bullet glyph itself, otherwise Rich's parent-style inheritance would dim the whole body line including colored prefixes. Existing regression test `tests/test_auto_update.py::TestChangelogCommandConsumesWhatsNewTrigger` (assertion is content-based, not style-based) continues to pass without modification; full suite 3373 passed.",
    ],
    "0.43.1": [
        "Fix: sandbox annotation from #304/#311 is now available to HTTP / REST callers, not only the CLI (closes #312). The original v0.42.0 fix placed the `keboola.sandboxes` `parameters.id` -> `storage_workspace_id` resolution in `commands/config.py` so it only fired on `kbagent config detail` invocations; `kbagent serve` callers (web UI, scheduled agents, `kbagent http get /configs/...`) hit the same `parameters.id` trap David Ešner reported in #304. The annotation logic moves to `ConfigService.get_config_detail` behind an opt-in `include_sandbox_annotation: bool = False` parameter so existing programmatic consumers see the unchanged response shape (zero regression risk), and a new `?include_sandbox_annotation=true` query parameter on `GET /configs/{project}/{component_id}/{config_id}` exposes it to REST callers. The CLI command unconditionally opts in to preserve v0.42.0 behavior. The pure-function workspace-list filter (`find_storage_workspace_for_sandbox_config(workspaces, config_id) -> int | None`) is extracted from `WorkspaceService.resolve_sandbox_workspace_id` to `services/workspace_service.py` module level so `ConfigService` can call it without taking a circular `ConfigService -> WorkspaceService` dependency in the DI graph. `WorkspaceService.resolve_sandbox_workspace_id` is retained as a one-line wrapper around the helper (still useful for direct callers). Error handling: a failed `list_workspaces` HTTP call no longer fails the detail fetch -- `storage_workspace_id` is set to `None` and the detail comes back as before, because the annotation is UX, not a contract. Bulk mode (`config_id=None`) silently ignores the flag because it would N+1 the workspace listing endpoint (one extra round-trip per config). Tests: 5 new in `test_services.py::TestConfigServiceSandboxAnnotation` covering default-off zero-regression, opt-in resolution, orphan (no matching workspace), non-sandbox-component skip, and graceful degradation on `list_workspaces` failure; 3 new in `test_serve_ui.py::TestConfigDetailSandboxAnnotation` covering HTTP router parameter binding (default-off, opt-in, non-sandbox no-op); 3 existing CLI tests in `test_cli.py::TestConfigDetail` updated to mock the new service-layer call path (now mock `client.list_workspaces` instead of `WorkspaceService.resolve_sandbox_workspace_id`). Total suite: 3381 passed, 104 skipped.",
    ],
    "0.43.0": [
        "New: full Semantic Layer management surface in `kbagent serve --ui` (closes #308). The web UI now mirrors every `kbagent semantic-layer` CLI operation 1:1 -- model CRUD (`/api/semantic-layer/models`), entity CRUD for all five kinds (metric / dataset / relationship / constraint / glossary), and the Phase-3 operations (validate, export, diff, promote, import, build, encrypt-token). The UI calls **zero** Metastore endpoints directly; every interaction goes through `/api/semantic-layer/*` on the same `kbagent serve` process so CLI parity is structural, not aspirational. Highlights: schema-driven add/edit drawers (one Pydantic schema per entity kind drives both Typer flags and the React form, no UI-side validation duplication); relationships view ships with a `flowchart TB` ERD (Mermaid) + a dataset-filter chip for hub-and-spoke drill-down + a parallel 'click to edit' edge list for hit-target reliability; constraints view groups rows by `constraintType` with collapsible `<details>` blocks and a 3-icon severity rail (critical / warning / info); datasets detail panel surfaces `fields[]` with role chips (`key=keboola/measure=green/dimension=zinc`). Builder/Importer/Promoter/Diff/Encrypt-Token are dedicated dialogs (`SemanticLayerDialogs.tsx`) with dry-run preview where the CLI offers it. The relationships ERD ships as `flowchart TB` (not `erDiagram`): erDiagram has no rankdir and laid every hub-and-spoke model out as a wide thin strip wasting ~70% of the canvas. flowchart TB puts the hub above its dependents, edge labels are trimmed to just the join type (`left` / `inner`; full relationship names live in the edge list below), auto-fit chooses `Math.min(fitX, fitY)` (cap 2.5, floor 0.4) so 80-edge overviews shrink to ~40% and 15-edge hub drill-downs land at ~63%, both fully readable.",
        'Fix: `BaseHttpClient._raise_api_error` now correctly surfaces Metastore validation messages instead of printing a bare HTTP status code. The Keboola Metastore answers 422 with `{"error": 422, "description": "..."}` (int in `error`, real text in `description`); the old parser used `body.get("error")` as the priority key, which evaluated to `422` and shadowed the real message -- the CLI rendered `API error 422: 422` and the operator had no actionable text. The new walker accepts `error` ONLY when it is a non-empty string, then falls through to `exception → message → description → detail → errors → json.dumps(body)` in priority order; FastAPI\'s `{"detail": [{loc, msg}]}` and Metastore\'s `{"errors": [{loc, msg}]}` list shapes are json-serialised so the message contains every diagnostic line, not the Python list repr. Four regression tests pin the new paths (int `error`, plain `description`, both list shapes) so the bare-status-code UX cannot return silently.',
        'Fix: `kbagent semantic-layer build` no longer HTTP-422s on legacy untyped Storage tables. The heuristic builder synthesises `fields[]` from a Storage `column_details[]` response; on legacy untyped tables the `basetype` is empty (`""`) and on typed tables it is warehouse-native (Snowflake `VARCHAR(255)`, `NUMBER(38,2)`, `TIMESTAMP_NTZ`, BigQuery `STRING`, ...). The Metastore only accepts a closed lowercase set (`string` / `integer` / `decimal` / `boolean` / `date` / `datetime` / `json`) for `fields[*].type`, so the heuristic builder used to push `""` or `"VARCHAR"` verbatim and 422 on every legacy table. New `_normalize_field_type(basetype)` strips parameter brackets and case-folds before mapping through `_FIELD_TYPE_MAP` (~30 warehouse aliases); empty/None falls through to `"string"` (safest default for an untyped column). A parametrized `TestNormalizeFieldType` covers every output bucket, parameterised types, case variants, and the unknown-UDT fall-through; the existing `test_heuristic_fallback` now asserts the field type was normalized to `"decimal"` end-to-end so the heuristic builder cannot regress to the pre-fix shape. Plugin docs (`plugins/kbagent/skills/kbagent/references/gotchas.md`) gain a `(since v0.41.10)` note on the normalization so any AI agent on an older kbagent has a documented escape path.',
    ],
    "0.42.0": [
        "Fix: workspace discoverability gap for data-app local dev (closes #304). David Ešner spent ~30 min and 4 wrong workspace IDs (including the `parameters.id` red herring) bringing up a Streamlit data app that reads via the Query Service -- because four different signals were missing or actively misleading. This release closes all four. (1) `kbagent workspace list` and `workspace detail` now accept `--branch` and follow the same `Info: Using production branch for read (active dev branch X ignored; pass --branch X to override)` banner as `storage buckets` / `config list`. Previously the commands silently scoped to the alias's pinned branch (carried over across sessions), returning a different workspace set than against the same alias one shell ago -- the original incident's root cause. `--branch` requires exactly one `--project` (branch IDs are per-project), mirroring the storage commands. (2) Each entry in `workspace list` / `workspace detail` JSON now carries `login_type`, `read_only`, `qs_compatible`, `database` and `warehouse`. The Storage API has always returned `connection.loginType` (snowflake-service-keypair / snowflake-person-sso / snowflake-legacy-service / default) and `readOnlyStorageAccess`; kbagent simply threw them away. Now they surface as new `Login Type` / `RO` / `QS` columns in the human-mode Rich table plus a `Login type` / `Read-only` / `Query Service compatible` block in `workspace detail`. `qs_compatible` is derived from the new conservative `QUERY_SERVICE_COMPATIBLE_LOGIN_TYPES` whitelist in `constants.py` (currently `snowflake-service-keypair` + `snowflake-person-sso`; `snowflake-legacy-service` stays OFF because the original issue confirmed it is rejected on the GCP us-east4 stack with `code: storage.executeQuery.notSupportedLoginType` even though it works on `connection.keboola.com`). False-negative-over-false-positive semantics: a `?` cell tells the caller 'not on the confirmed list, may still work' rather than blocking them. (3) New `workspace list --qs-compatible` filter pre-selects RO + whitelisted-loginType workspaces -- the canonical shape for a Streamlit / Quix data-app reading via the Query Service. (4) `config detail --component-id keboola.sandboxes --config-id <ID>` now appends a `sandbox_annotation` block with `sandbox_service_id` (the misleading `parameters.id`) and `storage_workspace_id` (the actual Storage workspace ID resolved via `WorkspaceService.resolve_sandbox_workspace_id`). The annotation is JSON-structured and Rich-rendered; it appears ONLY in single-config mode to avoid N+1 in bulk fan-out. Empirically verified on padak-2-0 (project 10539) by pinning to dev branch 1297900: pre-fix `workspace list` returned 1 row from the dev branch with no banner, post-fix returns 22 rows from production WITH the banner explaining how to opt back in via `--branch 1297900`. New module-level `_classify_qs_compatibility` helper + `WorkspaceService.resolve_sandbox_workspace_id`. Tests: 3 in `test_workspace_cli.py::TestWorkspaceListIssue304` (branch flag propagation, multi-project rejection, qs filter propagation, active-branch banner), 1 in `test_workspace_cli.py::TestWorkspaceDetailIssue304`, 3 in `test_workspace_service.py::TestIssue304WorkspaceListEnrichment`, 3 in `test_workspace_service.py::TestIssue304ResolveSandboxWorkspaceId`, 1 in `test_workspace_service.py::TestIssue304GetWorkspaceEnrichment`, 3 in `test_cli.py::TestConfigDetail` for sandbox annotation (matching + orphan + non-sandbox-component negative).",
    ],
    "0.41.10": [
        "Fix: `kbagent semantic-layer build` now rolls back on push failure (closes #295). `push_built_model` (`services/_semantic_layer_internals.py`) tracks every successfully POSTed child in order and, on any subsequent POST failure, walks the list in reverse calling `client.delete_item` per child. If we created the model during this call (caller did not pass `--model`), the model itself is DELETEd last. Each cleanup DELETE is wrapped in its own try/except so a partial cleanup failure never masks the original error; the wrapped KeboolaApiError carries `details.rollback` with `{attempted, posted_children, deleted, failed_deletes, model_created_here, model_deleted, model_delete_error, model_uuid}` so operators have full diagnostics. New `--keep-on-failure` flag (mirrors `data-app create`) preserves the partial state for forensic inspection -- the wrapped error then carries `details.rollback.attempted=False, reason='keep_on_failure'` instead of running cleanup. Broad exception handling (`except Exception` with explicit `KeyboardInterrupt`/`SystemExit` re-raise) ensures rollback fires for httpx network errors too, not just `KeboolaApiError`. Per-child cleanup uses `logger.warning` (no traceback) + a single `logger.error` summary line to keep log volume bounded on bulk failure. `name` is resolved before `dict(item)` so the wrapped error tags the current row, not the previous one. Degenerate POST response (missing id or non-dict body) is logged + skipped from rollback tracking AND from the counts increment. HTTP-API parity: `BuildRequest.keep_on_failure` + handler in `server/routers/semantic_layer.py`. Before this release a build that failed mid-push left the model + N successful children in the metastore; retry returned ALREADY_EXISTS and `model delete` refused while children existed, forcing per-child manual teardown.",
        "Fix: `kbagent semantic-layer edit metric --new-name` now surfaces partial-state cascades explicitly (closes #294). The metric rename cascade (`services/_semantic_layer_crud.py:edit_metric_with_cascade`) was already per-item rollback-safe, but the partial-state condition (metric renamed, M of N dependent constraints failed to repoint) was buried inside `cascaded_constraints[]` -- callers had to scan the list to discover it. The response envelope now carries two new top-level keys: `partial_state: bool` (true when any cascade entry has `status=='failed'`) and `recovery_hint: str | None` (explains how to recover via `semantic-layer validate` + manual `edit constraint --new-metrics`). The human-mode CLI renderer prints a bright red `PARTIAL STATE` banner above the per-entry list when set so operators cannot miss it. Banner copy is parametrised on the entity label so the wording survives future cascade extensions to other entity types. `edit_simple` (the no-cascade body shared by edit_dataset / edit_constraint / edit_relationship / edit_glossary) carries the same keys with `partial_state=False, recovery_hint=None` for envelope uniformity. Atomic two-phase commit was rejected as disproportionate: the metastore has no PATCH endpoint, so every cascade 'stage' is itself a DELETE+POST that can fail -- true atomicity would require side-staging.",
    ],
    "0.41.9": [
        "Fix: Workspace detail Drawer scrim (#286). The earlier 90% opacity (`bg-zinc-950/90`) read as a broken layout -- Vojta reported 'the left half of the screen looks crashed'. The original concern (clickthrough on agent-task buttons beneath the scrim) was misdiagnosed: the click-catch `<div className='flex-1' onClick={onClose}>` blocks pointer events at the layout level regardless of scrim opacity, so the dimming is purely visual. New scrim: `bg-zinc-900/50` in light mode + `bg-black/70` in dark mode, both retain `backdrop-blur-sm`. Restores the 'I opened a modal on top' depth cue without breaking modality.",
        "New: Dashboard 'Scheduled agents' tile gets an inline `run` button per row (#292). The Agents page already had it; the dashboard tile didn't. New `ScheduledAgentRow` component extracted from the inline list; the button fires `POST /agents/{id}/run` (blocking variant) and invalidates the `['agents']` and `['agent-runs', id]` query keys so `last_run_at` + status pill flip live without a manual reload. Deliberate choice of blocking-vs-SSE: the tile is a glance-and-move-on surface, not a live progress viewer -- users who want to watch tool_use events use the full Run drawer on the Agents page.",
        "New: Dashboard 'Local AI' tile replaces the per-project 'Kai Chat' (#300, follow-up to #291 wontfix). New page at `/localai` is a generic chat surface backed by the user's local `claude` / `codex` / `gemini` CLI -- same `stream_ai_agent_events` machinery as the workspace SQL helper and agent prompt helper, built as the third instance of the same stateless-helper layer (`/agents/prompt/improve/stream` + `/workspaces/sql/improve/stream` + `/ai/chat/stream`). Why: Kai requires a master Storage token to work, and `kbagent org setup` produces non-master tokens by default for security reasons (#291 closed wontfix). The Local AI tile works against any Storage token kbagent already has, and -- unlike Kai -- handles cross-project work natively via `--project NAME` flags. The dashboard hero 'Ask <cli>' input drops the typed message into `UIState.pendingLocalAiMessage` and navigates to `/localai`, which auto-fires the request on mount; full chat plumbing (CLI selector, project picker, transparency panels for the meta-prompt + tool_use activity log, markdown rendering with code blocks, abort button) lives on the dedicated page. The `build_local_ai_meta_prompt` builder is the most generic of the three helper meta-prompts: no output-shape constraint (chat renders markdown verbatim, no fence-stripping needed), no single-task framing -- just 'you are running inside kbagent serve, here is the user's question, run real commands to answer'. The kbagent skill (~70 KB of docs) is NOT inlined into every request; the AI is told to run `kbagent context` on demand instead, mirroring how Claude Code's plugin loader bootstraps the skill. Kai backend endpoints (`/kai/*`) remain available for callers that explicitly want Kai's per-project chat with API session state; only the dashboard tile / left-nav entry was swapped. The old `pages/Kai.tsx` is deleted; the new `pages/LocalAi.tsx` is feature-equivalent (single-shot for v1, multi-turn history forwarded into the prompt is the v2 follow-up). 15 unit tests cover the meta-prompt content (user message verbatim, project / branch hints, serve-URL fast-path, no output-shape contract, markdown contract) and SSE endpoint integration (init carries meta_prompt, done event flows through unmodified -- no SQL-style post-processing, error path surfaces as `done` with `status: error`).",
        "Plugin docs: `plugins/kbagent/skills/kbagent/references/gotchas.md` gains `(since v0.41.9)` notes for the Local AI tile replacement (Kai backend stays but the nav entry moves) and the dashboard 'Run' button vs Agents page 'Run' button (the dashboard uses blocking `POST /agents/{id}/run`; the Agents page uses SSE `POST /agents/{id}/run/stream` with live progress + late-attach -- pick the right one for the UX). No new CLI commands; `CLAUDE.md ## All CLI Commands` unchanged.",
    ],
    "0.41.1": [
        "Fix: startup auto-update hook now preserves the optional `[server]` extras. Before this release, `kbagent serve --ui` could trigger an auto-update that ran a bare `uv tool install --upgrade git+...` (no `--with` flag), silently dropping the FastAPI + uvicorn extras a user originally installed with `--with 'keboola-agent-cli[server]'`. The next line of the same boot would then refuse to start with `ModuleNotFoundError: No module named 'fastapi'`. The fix in v0.40.2 only patched the explicit `kbagent update` command (`version_service._update_kbagent`); the startup hook in `auto_update._perform_update` was left running the old bare command. Now both paths delegate to a shared `build_kbagent_upgrade_command()` helper that probes `importlib.util.find_spec('fastapi')` and pairs `--with 'keboola-agent-cli[server]'` with `--force` when extras are detected. Two new tests pin the behavior in both directions (extras -> `--force --with`, no-extras -> plain `--upgrade`).",
        "Fix: `kbagent version` now persists the freshly-fetched `latest_version` (and MCP version + install method) to the auto-update cache. Before this release, `get_versions()` made a live GitHub round-trip but did NOT write the result back to `~/.config/keboola-agent-cli/version_cache.json`. The 1-hour TTL'd cache stayed pinned to whatever value the auto-update hook last wrote -- so `kbagent version` would correctly show `v0.41.0 available` while a follow-up `kbagent serve --ui` on the same machine still auto-updated to whatever stale version the cache held (e.g. 0.40.3). Combined with the extras-drop bug above, this produced the worst-case scenario reported by users: `kbagent version` says new release available, `kbagent serve --ui` upgrades to a different older release and breaks. Now `get_versions()` writes the cache (lazy-imported to avoid a circular import) at the end of every successful fetch; write failures are caught and logged at debug level so the version command never crashes on a read-only HOME / disk-full / permission edge case.",
    ],
    "0.41.0": [
        "New: `kbagent semantic-layer` (alias `kbagent sl`) -- first-class CLI surface for the Keboola Metastore (semantic layer). Folds every metastore operation that was previously a hand-rolled urllib script in the `sl-builder` Claude Code plugin into a permission-gated, JSON-capable, hint-aware kbagent command group. The metastore URL is derived from each project's stack URL automatically (`connection.` -> `metastore.` -- region/cloud-agnostic), and reuses the same `X-StorageApi-Token` credential kbagent already holds. Subcommands shipped in this release: `show` (list a model's entities -- read), `validate` (structural checks; `--deep` adds parallel Snowflake column probing via the in-process StorageService -- read), `export` (snapshot the model to JSON -- read), `diff` (project-vs-project, project-vs-file, or file-vs-file -- read), `model create` / `model delete` (model lifecycle -- write/destructive), `add metric|dataset|relationship|constraint|glossary` (single-entity creates with FQN derivation, role heuristics, and constraint-orphan validation -- write), `edit` (DELETE+POST with cascade rename of dependent constraints and explicit rollback -- write), `remove` (destructive with mandatory constraint-orphan pre-flight warning), `import` (replay a snapshot with conflict detection -- write), `promote` (cross-project copy with modelUUID rewrite and identical-vs-changed-vs-new classification -- write), `build` (non-interactive AI-assisted greenfield via the existing AI Service client; fixes the long-standing sl-build bug where `semantic-constraint` items were silently dropped from the push loop -- write), and `token --encrypt` (encrypt the project token for a transformation container's user_properties using the existing EncryptService -- write, parity with `encrypt.values`). Verified live against the metastore API on 2026-05-14: response shape `{data: [...]}` for lists / `{data: {type, id, attributes, meta}}` for items; POST envelope `{name, data, branch:'main', schemaVersion:'1.0.0', scope:'project'}`; DELETE returns 204; duplicate-name POST returns 500 with `Failed to create meta object` (normalized to ALREADY_EXISTS); constraint `rule` is a STRING expression (NOT the `ruleExpression` object sl-builder documents); `constraintType` enum is `inequality|equality|range|composition|exclusion|temporal|conditional`; constraint `name` regex is `^[a-z][a-z0-9_]*$`; 4-band health (`_critical/_warning/_healthy/_review`) lives only in the constraint name suffix because the API `severity` enum is 3-level. Permission registry adds entries for every subcommand (with `model`, `add`, `edit`, `remove` split into per-leaf keys so e.g. `model list` stays read-only under `--deny-writes` and every `remove.*` leaf is classified `destructive`). Hint definitions ship for every subcommand (iter-4: `kbagent --hint client semantic-layer <cmd>` and `--hint service` both emit Python snippets). `edit` and `remove` cover all five entity types in iter-4 (metric|dataset|constraint|relationship|glossary) -- the parity gap with `add` is closed. Plugin/agent/skill docs updated (commands-reference, gotchas tagged `(since v0.41.0)`, keboola-expert version gate + tool-selection matrix, new `semantic-layer-workflow.md`). E2E coverage in `tests/test_e2e.py` bootstraps a throwaway `kbagent_e2e_*` model on `e2e-1143`, exercises every command, and tears down in `finally`.",
    ],
    "0.40.3": [
        "New: `kbagent serve --ui` workspace SQL editor gains an AI-assisted SQL writer (#287). The 'Help me write this SQL' button opens an inline helper that spawns a local `claude` / `codex` / `gemini` CLI via the new `POST /workspaces/sql/improve/stream` SSE endpoint, feeds it a meta-prompt grounded in the user's workspace (project alias, backend, default schema, visible bucket catalog, backend-specific INFORMATION_SCHEMA recipes, and a MANDATORY-FIRST-STEP block forcing `kbagent storage bucket-detail` for linked-bucket FQN resolution), streams the response back, and pastes the cleaned SQL into the Monaco editor. Three transparency panels are surfaced: the full meta-prompt (so users can audit what the AI received), an Activity log (tool_use -> tool_result events the AI invoked: `-> Bash: kbagent storage bucket-detail ...`), and the final AI suggestion with copy-to-clipboard. Each panel carries an inline copy pill. The `clean_sql_helper_response` strip pipeline handles claude's Insight blocks (the user-set `explanatory` output style leaks them despite the OUTPUT CONTRACT), code fences, preambles, and JSONL duplication. Fix-mode: when a query Run fails, a 'Send to <cli> for fix' button re-opens the helper with the failing SQL + the warehouse error pre-filled; `build_sql_helper_meta_prompt` pivots framing to 'diagnose and fix'. The Snowflake backend hint mandates double-quoting of EVERY identifier including column / table / CTE aliases (`AS \"month\"` not `AS month`) -- Snowflake uppercases unquoted aliases and the resulting CSV columns came back MONTH / EMPLOYEE_COUNT instead of the lowercase names users expected.",
        "Fix: `wait_for_query_job` now extracts the real warehouse error from `statements[i].error` (a plain string on Snowflake, sometimes a dict on BigQuery), not from a top-level `error` field that is ABSENT on failures. The previous extractor emitted the useless 'Query job failed: Query execution failed' constant for every failure; the SQL editor's red error box and the AI fix-mode prompt now receive messages like 'SQL compilation error: Function DATE_TRUNC does not support VARCHAR(10) argument type' verbatim. New module-level `_extract_query_job_error` helper walks statements first (with one-line `Statement N:` prefix only when multiple statements failed), falls back through top-level (string OR dict-with-message), and finally an explicit `Query execution failed (no error details from Query Service)` so the caller never gets an empty error string. 6 unit tests pin the four input shapes plus the no-info fallback.",
        'New: `project status` / `project list` JSON output exposes per-project `org_id` (int) and `org_name` (str | None) fields (#290). `org_id` is parsed from the top-level `organization.id` of the `/v2/storage/tokens/verify` response and normalised from string (`"73"`) to int (`73`) so persisted `ProjectConfig.org_id` keeps its declared int type. `org_name` is **Manage-API-only** -- the Storage API only carries the id; the name is populated via `kbagent org setup` (which calls `/manage/organizations/{id}`) or by `kbagent project add` when a manage token is in scope. Opportunistic backfill: `/projects/status` writes the freshly-discovered `org_id` back to `config.json` in a single serial pass after the parallel status check completes, so the value sticks for projects registered before this release. The web UI Projects table and top-bar project picker render `Keboola Demo` when the name is known, `#73` (monospace) when only the id is known, and dash when neither is known. The React Query cache for `/projects` is invalidated automatically once `/projects/status` returns so the ORG column populates without a manual page reload. Tooltips on `#73` and dash explain how to populate the name. ProjectConfig migration is backward-compatible: legacy `config.json` files without the new fields load cleanly with both fields defaulting to `None`.',
        "New: `kbagent serve --ui` auto-generates a stable `KBAGENT_CONVERSATION_ID` for the session in the format `serve-<UTC-timestamp>-<8-hex>` (e.g. `serve-20260515T091949Z-699ea57b`) and exports it to env before `create_app()`. Child processes (MCP subprocess, AI agent CLI invocations, every scheduled `kbagent http` call) inherit it and emit `X-Conversation-ID` on every Keboola API request. `kbagent doctor` flips from `warn: Conversation ID not set` to `pass: X-Conversation-ID: serve-...`. The `serve-` prefix lets observability dashboards filter human-driven sessions; the timestamp makes log lookups by session-start trivial; the hex suffix disambiguates rapid restarts in the same second. A pre-set `KBAGENT_CONVERSATION_ID` in env is respected verbatim so CI / supervisor scripts can pin a stable id across restarts. The startup banner gains a `conv id` line in both UI and API-only mode, plus the `export KBAGENT_CONVERSATION_ID=...` hint for the second-terminal `kbagent http` workflow.",
        "Fix: Lineage / Sharing graph (#289). Three changes layered on top of each other. (1) Mermaid's `maxTextSize` config is bumped from the default 50 KB to ~5 MB so the typical 50+ project / 250+ edge graph renders natively instead of bouncing off the size guard. (2) When Mermaid still hits the guard, soft-error detection now post-checks the rendered SVG for the literal 'Maximum text size in diagram exceeded' marker -- the renderer does NOT throw on size limit, it embeds the failure text INSIDE the SVG, so the previous `.catch()` path never fired. The styled amber banner kicks in instead of a silent useless red box. The banner replaces the previous CLI hint (`kbagent lineage server --load ...`) with two in-UI buttons: `Open Deep Lineage tab` (one-click switch to the dedicated viewer) and `Download Mermaid source` (handoff to mermaid.live or any external renderer). (3) The diagram itself is now wrapped in a 600px fixed-height scrollable viewport with `overflow: auto` so scrolling stays INSIDE the box instead of pushing the entire page layout. Two `<select>` filter pickers (Source project / Target project, populated from unique aliases in the edge set) sit above the viewport and narrow the edge set with AND semantics; a `clear` button + edge counter (`128 / 252 edges`) round out the toolbar. Empty filter results render a friendly 'No edges match' instead of an empty Mermaid node.",
        "Fix: AI helper meta-prompt (`build_sql_helper_meta_prompt`) makes `bucket-detail` a MANDATORY FIRST STEP with the specific failure mode quoted: `Schema 'KBC_USE4_<workspace_project>.\"in.c-foo\"' does not exist`. The previous LINKED BUCKETS section was passive ('CRITICAL for correctness') and the AI routinely skipped `bucket-detail` once `table-detail` returned column info -- the workflow optimisation cost users one broken SQL run per first-attempt. The new framing is imperative, numbers the steps explicitly (bucket-detail -> column discovery -> write SQL), and spells out `table-detail gives you column names, NOT the correct database`. Paired with the warehouse-error extraction above, the fix-mode loop now has the real Snowflake error to correct the first-pass mistake.",
        "New: Workspace SQL editor Storage Explorer flags linked buckets with an inline `linked` pill + tooltip pointing to `kbagent storage bucket-detail` for the correct FQN. The result-table header gains `Download CSV` and `Copy as CSV` buttons per statement -- both work entirely client-side (the CSV is already on the page from `/workspaces/.../query`). Filename embeds the statement index + a UTC timestamp so consecutive Run cycles don't collide.",
        "Fix: Web UI Dashboard greeting (#285) drops the hardcoded `, Petr` and renders only the time-of-day phrase (`Good Morning` / `Good Afternoon` / `Good Evening`). The hardcoded name was visible to every user and violated the no-hardcoded-defaults rule.",
        "New: Top-bar project picker dropdown gains a quick-search input + wider 384px column (was 288px). The search appears only when there are at least 5 projects registered, filters case-insensitively across `alias`, `project_name`, and `org_name`, auto-focuses on open, Escape clears (or closes when empty), and Enter picks the only remaining match when the filter narrows the list to one. Multi-org setups stay distinguishable via the `org #73` suffix when name is unknown but id is.",
        "Closed: issue #288 (Multi-project Kai) closed as `wontfix`. Cross-project comparison, migration assistant, and lineage root-cause tracing already work via the local Claude + multi-project CLI (`--project NAME` flags) -- no value in duplicating that in the Kai chat, which is by design per-project. Comment posted on the issue with the reasoning.",
        "Tests: +37 unit tests across the release. `test_workspace_sql_helper.py` (31: meta-prompt content / discovery instructions / Snowflake quoting / BigQuery dataset path / linked-bucket MANDATORY FIRST STEP / fix-mode pivot, plus full extraction pipeline coverage including Insight chatter stripping, CTE detection, header-comment preservation, no-SQL passthrough, JSONL dedup; SSE endpoint integration with mocked `stream_ai_agent_events`). `test_client.py` (+2: top-level `organization.id` parsing + string-to-int normalisation + no-org-block fallback; +6 in `TestExtractQueryJobError` against the four input shapes). `test_services.py` (+3: opportunistic org-info backfill, no-backfill when already set, no-backfill when verify returned no org). `test_org_service.py` (+2: org_name populated from per-project payload, fallback to `get_organization` API call). `test_serve_conversation_id.py` (4: fresh ID generation, uniqueness across calls, env override, whitespace-only env fallback). Total suite: 3140 passed, 7 skipped.",
        "Plugin docs: `plugins/kbagent/skills/kbagent/references/gotchas.md` gains a `(since v0.40.3)` section on the new `project status` / `project list` `org_id` / `org_name` fields, the Storage-vs-Manage-API source split, and the `org_name=None` even-when-org_id-is-set quirk. `CLAUDE.md ## All CLI Commands` unchanged (no new CLI commands introduced; all new behaviors are web-UI / serve-side). `keboola-expert.md` Rule 6 VERSION GATE and Tool Selection Matrix unchanged. `SKILL.md` auto-regenerated.",
    ],
    "0.40.2": [
        "Fix: `kbagent update` (and the startup auto-update hook) now preserves the optional `[server]` extras across upgrades. Previously `_update_kbagent` ran `uv tool install --upgrade git+...` with no `--with` flag, so the FastAPI + uvicorn extras a user originally installed with `--with 'keboola-agent-cli[server]'` were silently dropped on every upgrade -- a user who ran `kbagent serve --ui` happily yesterday got `ModuleNotFoundError: No module named 'fastapi'` after auto-update today. Now `_update_kbagent` probes `importlib.util.find_spec('fastapi')` (a reliable proxy for the `[server]` extra, which is the only thing that drags FastAPI in) and, when present, runs `uv tool install --force --with 'keboola-agent-cli[server]' git+...` instead. `--force` is paired with `--with` because uv rejects `--upgrade --with` if the additional spec resolves to a different version than the existing tool environment; `--force` is uv's documented way to reapply both flags in one shot. Pip fallback uses the PEP 508 `keboola-agent-cli[server] @ git+...` spec syntax. CLI-only users (no `[server]` extras installed) see no change.",
        "UX: `kbagent serve --ui` startup banner now prints copy-paste-able `export KBAGENT_SERVE_URL=... ; export KBAGENT_SERVE_TOKEN=...` lines so you can call `kbagent http get /projects` from another terminal without grepping the previous banner for the token. The plain `kbagent serve` (no `--ui`) banner gets the same hint. Closes a paper-cut where `kbagent http` printed `requires KBAGENT_SERVE_URL and KBAGENT_SERVE_TOKEN env vars` but the serve banner didn't tell you how to set them.",
    ],
    "0.40.1": [
        "Fix: `kbagent serve --ui` now shows a friendly install hint instead of a Python traceback when the optional 'server' extras (FastAPI, uvicorn) are missing. The 0.40.0 guard only caught the `uvicorn` import; the FastAPI import inside `server/__init__.py` would still surface as a raw `ModuleNotFoundError: No module named 'fastapi'`. Both imports are now wrapped in a single guard that names the missing module and prints the correct reinstall command for both `uv tool install` (end users) and `uv pip install -e` (development).",
    ],
    "0.40.0": [
        "New: `kbagent http get|post|patch|delete PATH` -- thin self-call HTTP client against the running `kbagent serve`. Reads `KBAGENT_SERVE_URL` + `KBAGENT_SERVE_TOKEN` env vars (auto-injected by the scheduler into every scheduled-agent subprocess) and forwards JSON in/out via the new `HttpForwarderService` (lives in `services/`, not `commands/`, per CONTRIBUTING.md §3-Layer architecture). POST/PATCH accept `--body` in inline JSON, `@file.json`, or `-` (stdin) shapes. Outside a serve-subprocess context the command refuses with exit code 2 -- it has no meaningful target. Permission classification: `http.get` = read, `http.post/patch` = write, `http.delete` = destructive. New constants `ENV_KBAGENT_SERVE_URL`, `ENV_KBAGENT_SERVE_TOKEN`, `HTTP_DEFAULT_TIMEOUT`.",
        "New: `kbagent serve --ui` -- single-process web UI mode. Mounts the React SPA at `/`, sets a HttpOnly `kbagent_session` cookie (SameSite=Strict, Path=/) on `GET /` so the browser is auto-authenticated for every same-origin REST + SSE request, and adds an ASGI path-rewrite middleware so the SPA's existing `/api/*` calls reach bare API endpoints. No Node BFF needed at runtime. The cookie path replaces the earlier `<script>window.__KBAGENT_TOKEN=...</script>` injection (XSS-readable, JS heap surface) and the `?_kbagent_token=...` query fallback (lands in uvicorn access logs); the bearer token now never enters the JS heap, the URL, or any access log -- only the same-origin Cookie header. `--ui-dist PATH` overrides the dist location; `$KBAGENT_UI_DIST` env var works the same way.",
        "New: hatchling build hook (`hatch_build.py`) bundles the built React SPA inside the wheel (`keboola_agent_cli/_ui_dist/`). Before wheel collection: looks for an existing `web/frontend/dist`; if missing AND `npm` is on PATH, runs `npm ci && npm run build` automatically. `[tool.hatch.build.targets.wheel.force-include]` overrides `.gitignore` so the generated dist actually lands in the wheel. Result: `uv tool install --with 'keboola-agent-cli[server]' git+...` produces a self-contained install where `kbagent serve --ui` works without any subsequent `npm` invocation. CLI-only callers see no behaviour change; the SPA is a passive payload.",
        "New: scheduled AI-agent runs persist their full event timeline + cost / token / per-tool summary on completion. Per-run JSONL goes to `agent_runs/<task_id>/<run_id>.jsonl` (0600). New `pricing.py` module with per-model rates (Opus / Sonnet / Haiku 4.x) computes cost from claude stream-json `usage` blocks, preferring claude's authoritative `total_cost_usd` from the `result` event when available. New `AgentRun.summary` + `AgentRun.events_path` fields (optional / backward-compatible). New endpoints `GET /agents/{task_id}/runs/{run_id}` and `GET .../events` power the detail-drawer replay in the React SPA. Both UI-driven and cron-driven runs flow through the same persistence path so the shape is identical regardless of trigger.",
        "New: 3-panel agent-run UI in the React SPA. `AgentRunView` distills claude stream-json into UI-level steps (thinking / tool_use / text / result), pre-pairs `tool_use` with the matching `tool_result`, and renders three columns: Steps timeline (left, clickable rows) | Step detail (middle, input + output of the selected step) | Cost / Tokens / Tools card (right, live-aggregated during a run, replayed from the persisted `summary` for historical runs). Slack-style live-tail UX: clicking a historical step pins the view, a `↓ Live tail +N` chip in the timeline header re-attaches to the latest step. `AgentRunRaw` collapsed-by-default raw JSONL pane for power users.",
        "New: full light + dark theme across every web-UI page. `theme.tsx` ThemeProvider + `useTheme()` hook with localStorage persistence and `prefers-color-scheme` first-visit fallback. Anti-FOUC bootstrap script in `index.html` so the very first paint matches the chosen variant. Theme toggle in the TopBar. Mermaid diagrams (Lineage page) carry per-theme palettes -- light = white background + zinc text + keboola-green border + cyan-600 lines; dark = original neon. Refactored every page (Dashboard, Workspaces, Storage, Jobs, Flows, Lineage, MCP, Kai, Components, Doctor, Changelog, Projects, Agents) for proper light/dark contrast.",
        "Fix: `kbagent serve` scheduled-agent subprocesses (action types `cli_command` and `ai_agent`) now inherit a composed env from the serve, not the raw `os.environ`. `agent_runner._build_subprocess_env(registry)` overlays three keys: `KBAGENT_CONFIG_DIR` (so forked `kbagent <cmd>` reads the SAME config the serve uses -- closes the wrong-directory bug where an AI agent saw the global `~/.config/keboola-agent-cli/` while the operator had launched serve with `--config-dir /tmp/...`); `KBAGENT_SERVE_URL` + `KBAGENT_SERVE_TOKEN` (so the spawned process can call `kbagent http get/post` against the live API instead of forking another `kbagent` CLI process tree). Both `_run_cli` and `_run_ai_agent` now accept the registry and pass `env=` to `asyncio.create_subprocess_exec` (previously `env=None`, which silently inherited an UNRELATED env). `ServiceRegistry` grows two new fields (`serve_url`, `serve_token`).",
        "Fix: AI-agent prompts (claude / codex / gemini) are now prepended with a small `[kbagent serve runtime context]` preamble that tells the AI it is running inside `kbagent serve`, names the two ways to query Keboola (`kbagent http` for live HTTP API, `kbagent` CLI for local fallback -- both pointing at the same config), and explicitly notes that manage-token operations still require human interaction (no autonomous storage-token refresh). Prevents the 'expired storage token, can I run project refresh?' rabbit-hole observed in the 0.33.0 web UI scheduled-agent test runs.",
        "Fix: web-UI top-bar label changed from `connected to BFF` to `kbagent serve` -- BFF was internal jargon that surfaced needlessly to end-users.",
        "Fix: Kai page description corrected -- 'Requires the AI Agent Chat feature flag enabled on the project's Storage API token' (verified in `services/kai_service.py`); previous copy implied a manage token, which was wrong.",
        "Internal: new `ConfigStore.config_dir` public property (was private `_config_dir`). Used by `agent_runner._build_subprocess_env` to set `KBAGENT_CONFIG_DIR` on subprocess env. Existing `config_path` property unchanged.",
        "Internal: `services/http_forwarder_service.py` extracted from the inline httpx code in `commands/http_client.py` so the command stays a thin Typer wrapper (CONTRIBUTING.md §3-Layer compliance). New `ForwarderError` dataclass pairs an `ErrorCode` with a CLI-appropriate exit code so the command layer can map exceptions to `typer.Exit` with one handler. Wired into `cli.py` as `ctx.obj['http_forwarder_service']`.",
        "Tests: 90+ new tests across the release. `test_pricing.py` (24: rate lookup, compute_cost, aggregate_usage_from_events, extract_model, build_run_summary). `test_agents_store_events.py` (~10: append/load timeline, get_run, summary round-trip). `test_serve_ui.py` (10: cookie bootstrap + HttpOnly/SameSite/Path attributes, public bypass for SPA shell, /api/* alias only when --ui is on, cookie auth on subsequent requests, query-token path explicitly rejected, missing-dist warns + skips). `test_http_forwarder_service.py` (16: env resolution, body parsing, request happy/4xx/transport-error, JSON vs text response). `test_http_client_cmd.py` (7: existing CliRunner tests pass unchanged after refactor). `test_agent_runner.py` (6: env composition, parent-env immutability, prompt prefix injection). Total suite: 3061+ passed.",
        "Plugin docs: synced across all 7 silent-drift surfaces (CLAUDE.md #17). `commands/context.py` AGENT_CONTEXT gains the Self-call HTTP and `serve --ui` sections. `CLAUDE.md ## All CLI Commands` adds the 4 http verbs and the `--ui` flag on serve. `keboola-expert.md` Rule 6 VERSION GATE block adds `kbagent http` (0.40.0+), `kbagent serve --ui` (0.40.0+), and AI-agent run timeline persistence (0.40.0+); the Tool Selection Matrix `kbagent http` row's stale `(0.33.x+)` tag is corrected to `(0.40.0+)`. `SKILL.md` auto-regenerated via `scripts/generate_skill.py`. `commands-reference.md` adds the Self-Call HTTP section + two new env vars. `gotchas.md` adds a `(since v0.40.0)` section for `kbagent http` (corrected from the stale `v0.33.x` tag).",
    ],
    "0.33.0": [
        "New: `kbagent config new --push` -- one-shot remote configuration create via the Storage API. Previously `config new` was scaffold-only (writes files to disk, zero API calls); the AI agent docs (`keboola-expert.md` tool selection matrix, SKILL.md decision row) conflated this with API-create intent. `--push` adds the actual remote create as a single CLI call. Requires `--project` and a non-empty `--name`. `--no-files` skips the filesystem step entirely for FIIA-style 'empty shell, then patch via `config update --set`' workflows. Optional `--configuration JSON|@file|-` / `--configuration-file PATH` override the POSTed body (default is `{}`). `--dry-run` previews the planned POST and validation outcome without calling the API. `--branch ID` targets a specific dev branch. `--description D` sets the description. Resolves the F2 gap in `kbagent-feature-gaps.md`. Schema validation runs by default when `--configuration` provides an explicit body (`jsonschema.Draft7Validator` against the component's AI Service `configurationSchema`, fail-closed with exit 5); graceful skip if the AI Service has no schema; `--no-validate` opts out. Default empty-shell `{}` auto-skips validation (FIIA pattern). Snowflake transformation matrix fix: `tool call create_config` refuses `keboola.snowflake-transformation`; `config new --push` wraps the raw Storage API directly and does NOT inherit the refusal -- works for ALL component types. Without `--push`, today's scaffold-only behavior is byte-for-byte preserved -- pure regression-zero addition.",
        "Change: `kbagent --json data-app <subcommand>` envelopes now emit the data-app's own identifier under the key `app_id` (was bare `id`). Affects 12 subcommands: `list, detail, create, deploy, start, stop, delete, password, secrets-set, secrets-list, secrets-get, secrets-remove`. The companion `config_id` key is unchanged. The Storage config back-pointer at `parameters.id` (lives INSIDE the configuration body sent TO Storage) is unchanged. The auth-provider id (`auth_providers[].id == \"simpleAuth\"`) is unchanged. The pipe-friendly chain now works as the `--app-id` input flag implies: `kbagent --json data-app list | jq -r '.apps[].app_id' | xargs -I{} kbagent data-app deploy --project P --app-id {}`. Output-key rename only; the Data Science API still serves camelCase keys on the wire (verified 2026-05-12 on europe-west3.gcp across projects 1143/2738/2959). No deprecation alias; the breakage is surface-level and trivially scriptable (`jq -r '.apps[].id'` → `jq -r '.apps[].app_id'`). No in-tree consumers of `apps[].id` were found.",
        "Tests: 47 new tests across `tests/test_config_create_service.py` (15 service-level: happy paths, dry-run, schema validation ok/failed/skipped, malformed schemas, AI Service errors, client cleanup), `tests/test_config_create_cli.py` (20 CLI-level: flag-combination validation, push-mode happy paths, body parsing inline/@file/stdin, error propagation, dry-run envelopes, JSON-mode stdout correctness for `--push --output-dir`), and `tests/test_data_app_service.py` (12: `TestDataAppEnvelopesNoBareIdKey` regression class for the 11 affected subcommands + 1 list-rename coverage test). E2E flow extended with step 19b (`config new --push --dry-run` → real `--push --no-files` → `config detail` verify → `config update --set` patch → `config delete` cleanup, wrapped in try/finally + pre-registered safety-net teardown) and `test_data_app_lifecycle_public` extended with list-step round-trip `app_id` assertion. Existing `TestConfigNew` in `test_component_cli.py` continues to pass byte-for-byte (regression coverage for scaffold-only mode).",
        "Plugin docs: synced across all 7 silent-drift surfaces (CLAUDE.md #17). `commands/context.py` (`config new --push` inventory + `--app-id` mention), `CLAUDE.md ## All CLI Commands` (replaced `config new` line), `keboola-expert.md` Rule 6 VERSION GATE adds `config new --push needs 0.33.0+` and `data-app *` JSON output uses key `app_id` on 0.33.0+; the Snowflake matrix row is rewritten to surface `config new --push --no-files` as first-choice; new 'Create a new config (one-shot remote)' matrix row added. `SKILL.md` auto-regenerated. `commands-reference.md` adds the two-mode `config new --push` entry and the `data-app` rename note. `gotchas.md` adds two `(since v0.33.0)` sections: `config new --push` scaffold-vs-push split + MCP refusal nuance + validation behavior; `data-app` `app_id` rename with input/output symmetry rationale. `scaffold-workflow.md` adds the dual-mode callout + push examples in step 3.",
        "Review fixes: post-rebase against main (which had already shipped 0.32.0 via PR #277), `--push --output-dir` no longer emits a plain-text dim line above the JSON envelope when `--json` is set (the `_write_scaffold_to_disk` helper now honors `formatter.json_mode` instead of hardcoded `False`) -- closes a silent JSON-pipeline regression flagged in PR #278 review. The push-with-output-dir CLI test now asserts `json.loads(result.output)` parses cleanly.",
    ],
    "0.32.0": [
        'New: `kbagent storage truncate-table --project NAME --table-id ID [--table-id ...] [--dry-run] [--yes] [--branch ID]` -- row-level truncation that drops every row from one or more storage tables while preserving the table definition (columns, types, primary key, descriptions, sharing edges, and every downstream config reference). Closes the only confirmed FIIA-migration gap (Coverage Matrix Row 25; R5 per-phase reload invariant). Wraps `DELETE /v2/storage/[branch/{id}/]tables/{id}/rows?allowTruncate=1`. Notable departure from sibling destructive endpoints: the row-delete endpoint is inherently async on every branch and rejects `async=true` as an unknown field (verified live 2026-05-11: HTTP 400 `"async: This field was not expected."`). The client therefore omits `async=true` and lets the endpoint return its natural HTTP 202 + queued `tableRowsDelete` job, which `_wait_for_storage_job` polls to completion -- same machinery as `delete_table`, just without the query-param dance. Multi-target: per-table errors accumulate without aborting the batch (one missing table does not block the rest). JSON envelope mirrors `delete-tables`\'s naming with a richer per-target receipt: `{truncated: [{table_id, rows_before, rows_after, branch_id}], failed: [{id, error}], dry_run, project_alias, would_truncate?}`. `--dry-run` captures `rows_before` via `get_table_detail` without truncating. Idempotent (truncating an empty table is a no-op success). Permission classification: `destructive` (gated behind `--allow-destructive` / `cli:destructive` policies) alongside `delete-table` / `delete-column` / `delete-bucket` / `swap-tables` -- schema preservation does not downgrade row-data deletion. Use this over `delete-table` whenever the schema contract must survive (sharing edges, aliases, dependent transformations, primary keys, column descriptions).',
        "Tests: `tests/test_storage_truncate.py` adds 21 unit tests across three layers -- HTTP shape (5: URL+query-params with allowTruncate=1, branch_id URL prefix, async-poll roundtrip, URL encoding of dotted table IDs, 4xx propagation via `pytest_httpx`), service business logic (10: happy path, branch_id carried into truncated[] entries, non-numeric `rowsCount` defaults to 0, missing `rowsCount` defaults to 0, batch partial failure with NOT_FOUND on second target, dry-run skips `truncate_table`, branch_id propagation to both `get_table_detail` and `truncate_table`, unknown-project `ConfigError`, try/finally `close()` on API error, empty-list short-circuit), and CLI integration (6: JSON happy path with `--yes`, `--dry-run` JSON shape, `--branch` flag override, active-branch fallback, exit 1 on `failed[]`, exit 5 on `ConfigError`). E2E coverage in `tests/test_e2e.py::TestFullE2E` adds step 11.1 `_test_truncate_table_roundtrip`: snapshots schema (columns + primary key + identity) on the 8-row test table, dry-runs the truncate (verifies `would_truncate.rows_before` matches), applies it (verifies `rows_after=0`), re-verifies schema integrity (columns + PK + identity unchanged), then restores the 5+3 CSV pair so downstream hops see the original row count. Live-API smoke against project 1143 on `connection.europe-west3.gcp.keboola.com` 2026-05-11 confirmed the full flow including the async-only endpoint discovery.",
        "Plugin docs: synced across all 7 silent-drift surfaces (CLAUDE.md #17). `commands/context.py` AGENT_CONTEXT gains the storage-Lifecycle entry. `CLAUDE.md` `## All CLI Commands` lists the new signature. `keboola-expert.md` Rule 6 VERSION GATE adds `storage truncate-table needs 0.32.0+`, the Tool Selection Matrix adds a `Re-seed a table without losing its schema / PK / dependents` row, and a new inline gotcha clarifies the uniformly-async behavior + the `async=true` rejection. `SKILL.md` auto-regenerated via `make skill-gen`. `commands-reference.md` adds the bullet between `delete-table` and `delete-column`. `gotchas.md` adds a `(since v0.32.0)` section covering the `allowTruncate=1` opt-in, the live-API discovery that `async=true` is rejected, the uniform async-via-job behavior, idempotence, propagation timing, and permission classification. Hint registry adds two-step entry under `storage.truncate-table` so `--hint client` and `--hint service` emit reusable snippets.",
    ],
    "0.31.0": [
        "New: `kbagent project edit --new-alias NEW [--dry-run]` -- rename the alias of an existing project connection without going through `project remove` + `project add` (which forces token re-entry). Cascades the rename through everything that persists the alias on disk: the `config.json` `projects` dict key (`pop(old)` + insert under `new`) AND the `default_project` field if it matched the old alias. When a nested-layout sync workspace is present at `<cwd>/<old-alias>/.keboola/manifest.json`, the directory itself is also renamed to `<cwd>/<new-alias>/` -- mirrors the `kbagent config rename` precedent (`-2`-suffix collision handling, git-mv with shutil.move fallback). Skips the disk step when no sync workspace is present. Combined with `--url` and/or `--token` in a single invocation those mutations target the NEW alias post-rename, so `kbagent project edit --project foo --new-alias bar --token NEW` is one atomic operation with the expected ordering. Backed by the new `ConfigStore.rename_project(old, new)` method (atomic dict-key swap + `default_project` update saved as one transaction) and a fail-closed `ProjectService._rename_project_alias()` helper that validates collision before touching any state. Validation: empty `new_alias`, whitespace-only `new_alias`, and `new_alias` that already exists are all rejected with `ConfigError` exit code 5.",
        "New: `--dry-run` previews the rename (collision detection, planned disk-rename method `git_mv` vs `shutil_move`, lineage-cache warning) without mutating any state. Validation errors (`..` path-traversal, collision, invalid format) raise the same `ConfigError` exit-5 codes as the live path -- callers can rely on `--dry-run` as a 1:1 pre-flight. Token re-verification is also skipped in dry-run mode (no API hit). Result dict carries `dry_run: True` and a `planned` sub-dict. Backed by `_plan_project_alias_rename()` and `_plan_nested_sync_dir()` helpers in `services/project_service.py` -- pure read-only mirrors of the live `_rename_project_alias` / `_rename_nested_sync_dir`. Addresses PR #266 review NIT (UX consideration: even non-classically-destructive ops benefit from a dry-run pre-flight).",
        "Note: lineage cache JSON files (output of `kbagent lineage build --output X.json`) embed the alias inside FQN strings (`<alias>:<table_id>`) and are NOT auto-updated by the rename. Rebuild with `kbagent lineage build` after the rename if you have a cached `.lineage.json`. Lineage caches may live anywhere on disk (committed to git, in a sibling repo, etc.) so a partial rename is worse than no rename. Surfaced as a stderr warning at rename time when a `.lineage.json` is detected in the workspace.",
        "Fix: `kbagent config update` now re-splits SQL `script[]` elements that already arrived as a list but pack multiple `;`-separated statements into one entry. Closes #274 -- the remaining gap on the v0.28.0 (#245) normalization. The string-case (#245) was already covered: `script: 'CREATE ...; alter session ...;'` (str) -> list of 2. The list-case was NOT: `script: ['CREATE ...; alter session ...;']` (list of 1) passed through unchanged, the Storage API accepted it 200 OK, the version incremented, and the runtime crashed at job execution with `odbc_prepare(): SQL error: Actual statement count 2 did not match the desired statement count 1., SQL state 0A000 in SQLPrepare`. Fix runs each list element back through `split_statements()` (same state-machine #245 already wired up; respects `'...'` / `\"...\"` / `$$...$$` / `--` / `#` / `//` / `/* ... */`) and replaces multi-statement elements inline. Already-correct lists (one statement per element) are a no-op. Live-reproduced against project 901 (`padak`) config `01km0sd189fdrcnjwk89cd1fkc`: push CREATE+ALTER as single list element -> Storage API 200 OK, version 58->59, normalizations empty (pre-fix); job 1307622107 crashed with the exact ODBC error above; rollback restored v60. Post-fix the same input yields a 2-element list and the runtime succeeds.",
        "Observability (#274): each malformed element gets its own `normalizations` entry with `action: 'sql_resplit'`, `path: parameters.blocks[B].codes[C].script[E]` (the **original** element index on input, so users can map the warning back to their source payload even when later elements shift due to upstream splits), `before_length: 1`, `after_length: N`, `before_type: 'str'`, `after_type: 'list'`. Mirrors the existing `sql_split` / `wrap_array` envelope shape so the human-mode warning line and JSON consumers don't need new parsing logic. The CLI's existing yellow `Auto-normalized N script field(s)` warning continues to fire on resplit (the line is action-agnostic).",
        "Why #274 slipped through #245: that fix gated on `isinstance(script, str)` -- bullet-proof for the original failure (the Storage API takes a string and the runtime rejects it), but per-list-element validity was out of scope. The two production failures reported in #274 (SK->CZ migration of project 1507 transformation 745661351, 4 elements across 2 codes, both ODBC-state 0A000) confirmed list-shape inputs are produced in the wild by older UIs, hand-edited configs, and tools that round-trip through `keboola-as-code` without re-normalizing. The fix is the smallest possible: extend the same `is_sql_transformation_component()` gate and the same `split_statements()` helper to the list branch. Non-SQL transformations (Python / R / custom-Python apps) skip the resplit -- their `script` is application code, not SQL, and Python `;` is a valid intra-statement separator (`print('a'); print('b')`).",
        "Security: hardening from PR #266 review iteration 2. The `--new-alias` validator rejects path-traversal sequences (`..`), path separators (`/`, `\\`), NUL bytes, leading dot/dash, and anything outside `[A-Za-z0-9_.-]` -- regex `[A-Za-z0-9_][A-Za-z0-9_.-]*`. Stricter than `project add`'s no-op check; rationale is the rename's filesystem interaction (alias becomes a directory name). `search_root` is `Path.resolve()`-d once before the disk rename to collapse symlinks and close a malicious-cwd vector. Disk rename failures (`OSError`) trigger a config rollback so config and disk never end up out of sync. Lineage cache scan is depth-capped at 2 levels (top + `*/` + `*/*/`) to bound cost when `search_root` is `$HOME` or similar.",
        "E2E: `tests/test_e2e.py::_test_project_edit_and_remove` extended with a `--dry-run` preview (planned-block assertion) followed by a live `--new-alias` round-trip (rename + reverse-rename to baseline) before the existing `--url` step. Pinned by Padak's PR #266 review BLOCKING -- every CLI command must have E2E coverage per CONTRIBUTING.md / convention #16. Round-trip leaves `self.alias` unchanged so subsequent steps continue to work.",
        "Tests: 49 new unit/integration tests across the release. 38 new tests for `project edit --new-alias` -- 32 service-layer in `tests/test_project_edit.py` (alias-key swap, collision rejection, `default_project` cascade, sync-dir disk rename, sync-dir collision `-2` suffix, combined edit-and-rename, no-op cases, parametrized 9-input path-traversal validator, legal-shape aliases, OS failure rollback, rollback-also-fails surfacing, symlink target collision, `--dry-run` happy/error paths) and 6 CliRunner tests in `tests/test_project_edit_cli.py` (human + JSON shape, exit-5 on collision/no-changes, `--dry-run` planned block, `--dry-run` `DRY RUN` label). 11 new tests for `sql_resplit` in `tests/test_normalize_script.py::TestSqlResplitListElements` (canonical CREATE+ALTER, CREATE+SELECT, well-formed-list no-op, mixed bad+good with original-index reporting, multiple bad elements, `;` inside `/* ... */`/`'...'` not a separator, Python list passthrough, BigQuery list resplit, defensive `None`-element passthrough) plus 1 integration test (`test_sql_list_element_resplit_before_push`) pinning that the HTTP client receives the post-resplit payload. Total suite: 2895 passed, 4 skipped.",
    ],
    "0.30.6": [
        "UX (sec-20 follow-up): malformed `.keboola/branch-mapping.json` now surfaces as a clean JSON error envelope (exit 5, `CONFIG_ERROR`) instead of a raw Python traceback. v0.30.5 introduced the descriptive `Invalid branch ID in branch-mapping.json` message but `load_branch_mapping()` raised it as a bare `ValueError` -- which CLI commands did not catch, so an end user with a hand-edited mapping file saw a multi-frame traceback dumped to stderr instead of a one-line error. Fixed by raising `ConfigError` from `load_branch_mapping()` directly; existing CLI `except ConfigError` handlers now produce the standard error envelope. Found during v0.30.5 e2e smoke test against the kbagent-e2e project; not a security regression but a clear UX cleanup. The descriptive content of the error is unchanged; only the wrapper class differs.",
        "Tests: `test_load_branch_mapping_invalid_id_includes_path` updated to assert `ConfigError` instead of `ValueError`. `cleanup_branch_id_from_mapping()` extended to catch `ConfigError` alongside the legacy `ValueError` so its best-effort behavior is preserved. `BranchMapping.from_dict()` continues to raise `ValueError` (it's the data-parser layer); only `load_branch_mapping()` (the filesystem-aware wrapper) was promoted to `ConfigError`.",
    ],
    "0.30.5": [
        "Security (critical): `kbagent sync pull` no longer permits API-controlled `component_id` or `component_type` to escape the sync workspace via path traversal. `naming.config_path()` now passes both fields through a new `sanitize_path_segment()` that rejects `/`, `\\`, and parent-directory references (`..`) while preserving the dots, hyphens, and underscores in legitimate component IDs (`keboola.ex-db-mysql`, `kds-team.app-custom-python`). `services/sync_service.py:pull()` adds a defense-in-depth confinement check that raises ConfigError if a resolved config path is not contained in the branch directory. Issue #269 sec-01 / sec-07; threat actor: compromised stack or supply-chain attack on the project token. Pre-fix, `component_id = '../../../etc'` would write outside the project root.",
        "Security (high): MCP HTTP transport subprocess no longer inherits Keboola tokens from the kbagent process environment. `mcp_transport.py:_start()` previously used `subprocess.Popen(cmd, ...)` with no `env=` argument, so when `KBAGENT_MCP_TRANSPORT=http` was set, the MCP server inherited `KBC_MASTER_TOKEN`, `KBC_MASTER_TOKEN_<ALIAS>`, `KBC_MANAGE_API_TOKEN`, and `KBC_TOKEN`. New `_build_minimal_env()` allow-lists only the env vars needed for binary discovery and locale handling (PATH, HOME, USER, LANG, LC_*, UV_CACHE_DIR, PYTHONPATH, ...) and explicitly drops every `KBC_*` token. Per-project Storage tokens still flow through HTTP request headers as before. Issue #269 sec-02 / sec-08; closes the gap left by v0.29.0's manage-token default-deny on the HTTP transport path.",
        "Security (high): REPL history file (`~/.config/keboola-agent-cli/repl_history`) is now created with mode 0600. Pre-fix, `prompt_toolkit.FileHistory` created the file with the user's default umask (typically 0644), persisting any token typed at the prompt (e.g. `project add --token ...`) in plaintext readable by group/world. `_get_history_path()` now atomically pre-creates the file with 0o600 and tightens existing files via chmod. Issue #269 sec-04.",
        "Security (high): `kbagent lineage show --format er` (and the lineage server's ER view) no longer emits XSS-vulnerable HTML. `services/deep_lineage_service.py:render_er_diagram()` previously did `name.replace('\"', \"'\")` which left `<`, `>`, and `&` untouched -- a Keboola table or config named `</div><script>alert(1)</script>` would inject the script into the generated HTML body where the browser parses it before Mermaid runs. (`--format html` flowchart was already safe; it routes through `render_mermaid()` which escapes labels.) Fix uses `html.escape(s, quote=True)` consistently for every API-derived string embedded into the Mermaid body and surrounding HTML. Mermaid renders the entities back to their characters in SVG text so visible output is unchanged. Issue #269 sec-05.",
        "Security (high): `kbagent encrypt values --output-file PATH` now atomically creates the file with mode 0600. Pre-fix, `Path.write_text()` followed by `chmod(0o600)` left a race window where the encrypted secrets file was world-readable on systems with permissive umask (e.g. 0644 default). Replaced with `os.open(path, O_WRONLY|O_CREAT|O_TRUNC, 0o600)` + `os.write()`. Issue #269 sec-06.",
        "Security (medium): `max_parallel_workers` Pydantic field now requires `ge=1` in addition to `le=100`. Pre-fix, a config.json with `max_parallel_workers: 0` passed validation, then `ThreadPoolExecutor(max_workers=0)` crashed every multi-project operation with `ValueError`. `BaseService._resolve_max_workers()` also clamps to >= 1 defensively so a legacy on-disk config does not crash startup. Issue #269 sec-11.",
        "Security (low): `kbagent permissions check OPERATION` now reflects the EFFECTIVE policy for the current invocation -- the persisted policy MERGED with `--deny-writes` / `--deny-destructive` session flags -- matching `permissions list` semantics. Pre-fix, `permissions check` consulted only the persisted policy, so an AI agent inspecting its own self-imposed firewall got a misleading `allowed` answer for write ops. Issue #269 sec-19.",
        'Security (low): `_coerce_keboola_id()` and `load_branch_mapping()` now raise descriptive errors for malformed branch IDs in `branch-mapping.json`. Pre-fix, a hand-edited file with `"id": "not-a-number"` produced a raw `ValueError: invalid literal for int()` from deep inside the parser. New error names the offending file path and the bad value so users can fix it. Issue #269 sec-20.',
        "Tests: 33 new regression tests across `test_sync_naming.py` (sanitize_path_segment + config_path traversal-resistance), `test_mcp_transport.py` (env scrubbing for KBC_*), `test_repl.py` (history file 0600 + tighten existing), `test_deep_lineage_service.py` (XSS escape in ER diagram for table and config names), `test_models.py` (max_parallel_workers ge=1), `test_sync_branch_mapping.py` (descriptive ValueError + path-prefixed wrap), `test_permissions_cli.py` (--deny-writes / --deny-destructive applied by `permissions check`). Total suite: 2830 passed.",
        "Audit methodology: this release was driven by a three-stage automated audit (kbagent expert -> state-machine engineer -> security engineer), each as a sub-agent reading the previous output. The use-case map (28 KB) covered every command + 20 multi-command life situations + cross-cutting concerns. The state-machine doc (43 KB) traced every persistent / in-memory state with file:line references, command-by-command read/write/assert table, life-situation traces, and 9 forbidden state combinations. The security review (20 findings, 15 KB) prioritized 9 verified issues for this release; the remainder (sec-03 token-in-argv deprecation, sec-09 PTY-bypass design, sec-10 silent collisions, sec-12 cache atomicity, sec-13 @file restriction, sec-14 alias validation, sec-16 SRI on Mermaid CDN, sec-17 toggle staleness, sec-18 PATH for uv) are tracked in #269 as out-of-scope follow-ups.",
    ],
    "0.30.4": [
        "Fix: `kbagent sync pull` against a linked dev branch now writes files under the linked branch's directory (`branch-<id>/...` or its sanitized name), not under `main/`. Pre-fix, `branch_link` persisted Keboola branch IDs as **strings** in `.keboola/branch-mapping.json` (`kbc_branch_id = str(branch_info['id'])` at five call-sites in `services/sync_service.py`), but every comparison against the manifest read those IDs as the **int** they're typed as in `ManifestBranch.id: int` and on the Storage API. Cross-type `int == str` is always False in Python, so `_find_branch_path` fell back to the default branch (`manifest.branches[0].path == 'main'`) and `_ensure_branch_registered` re-registered the 'unknown' branch on every pull, appending a duplicate `branches[]` entry with a mangled `branch-<id>-<id>` path until the manifest was hand-cleaned. The same comparison failed in `_ensure_branch_registered`'s `b.get('id') == branch_id` API-name lookup, so the branch's human-readable name from the API was never used and the path always fell through to the numeric `branch-<id>` fallback (the 'side observation' from issue #267). Fix is end-to-end `int`: `branch_link` writes `int(branch_info['id'])`, `BranchMappingEntry.keboola_id: int | None` (was `str | None`), and `from_dict` silently coerces legacy string IDs on load so existing user workspaces upgrade without manual editing. Bug A from issue #267, reported externally on v0.27.0 and reproduced on v0.30.3.",
        "Fix: `kbagent sync pull` no longer re-writes every previously-tracked config on every invocation in git-branching mode. The `branch_switched` guard at `services/sync_service.py:489-491` compared `existing_branch_ids[lookup_key]` (int from manifest) against the polluted str return of `_resolve_branch_id`; cross-type `!=` was always True, so the idempotency check was completely defeated and `files_written` ticked up on every pull even when nothing changed. The Bug A end-to-end int fix automatically restores correct behaviour here -- this is Bug C from issue #267, fixed transitively. Regression test pins `pull-pull-pull` against an unchanged remote and asserts manifest stability.",
        "Fix: `kbagent sync diff` and `kbagent sync push --dry-run` now surface scaffolded local config directories on git-branching workspaces with empty `manifest.configurations[]`. Pre-fix, `_find_untracked_configs` (`services/sync_service.py:2612`) built its scope set exclusively from already-tracked configs (`active_branch_ids.add(cfg.branch_id)`); when configurations were empty, the scope set was empty, the walker `continue`d past every branch and returned `[]`, silently dropping the documented `ADDED -> push creates it` flow. Fix widens the scope to `tracked U {default} U {resolved}`: branches with tracked configs (today's protection against orphaned dirs), the default branch (push-to-main scaffold is legitimate), and the branch the caller resolved for this op (the linked feature branch the user is actively working on). `diff()` now passes `branch_id` into the walker so the resolved branch is in scope. Phantom-add protection for unrelated dev-branch dirs is preserved. Bug B from issue #267.",
        "Fix: `kbagent branch delete` and `kbagent branch merge` now clean up matching entries from `.keboola/branch-mapping.json` in the nearest enclosing sync workspace. Pre-fix, deleting a Keboola dev branch left the local mapping pointing at a now-non-existent branch, and every subsequent `sync pull/push` from the linked git branch hit a 404 from the Storage API -- a non-recoverable state until the user manually ran `sync branch-unlink`. New helper `cleanup_branch_id_from_mapping()` in `sync/branch_mapping.py` walks upward from cwd to find the workspace, removes every entry whose `keboola_id` equals the deleted/merged branch ID, and is wired into `BranchService.delete_branch` and `BranchService.get_merge_url`. Both surface a `mapping_cleanup` field plus an additive message line listing the unlinked git branches. Bug D from issue #267.",
        "Fix: `_resolve_branch_id` no longer raises `ConfigError` for the default git branch when `.keboola/branch-mapping.json` is missing. Pre-fix, an accidentally deleted (or `.gitignore`d) mapping file blocked even `sync pull` on the default branch with `Git branch 'main' is not linked to a Keboola branch` and there was no recovery path because `branch_link` explicitly forbids linking the default branch (`services/sync_service.py:1918`). Fix: when the mapping is missing or has no entry for the current branch AND the current branch is `manifest.git_branching.default_branch`, return `None` (production). Non-default branches with no mapping still raise `ConfigError` (intentionally narrow recovery: production is always reachable, dev branches still require explicit linking). Bug E from issue #267.",
        'Tests: 7 new regression tests under `TestIssue267Regressions` (covering branch_link int persistence, repeated-pull manifest stability, pull routing to feature dir, walker untracked-detection on empty configurations, walker phantom-add protection still holds, default-branch recovery on missing mapping, dev-branch error path on missing mapping) plus 6 new tests in `tests/test_sync_branch_mapping.py` covering legacy str-id migration, `find_sync_workspace` upward search, and `cleanup_branch_id_from_mapping` cases (matching id removal, unmatched no-op, no-workspace no-op). Existing tests in `TestBranchLink`, `TestBranchUnlink`, `TestBranchStatus`, and `test_sync_branch_mapping.py` updated from `assert keboola_id == "99999"` (the assertion that locked Bug A in) to `assert keboola_id == 99999`.',
        "Refactor: `_find_untracked_configs(project_root, manifest)` is now `_find_untracked_configs(project_root, manifest, resolved_branch_id=None)`. The scope-widening param defaults to `None` (keeps `status()` callsite at `services/sync_service.py:791` semantics-compatible). `diff()` callsite at line 905 now passes the resolved branch ID so the walker covers the linked feature branch dir. No behaviour change for non-git-branching workspaces.",
    ],
    "0.30.3": [
        "Fix: `_perform_mcp_update` for `uvx`-cache installs now promotes to `uv tool install --upgrade keboola-mcp-server` instead of running the broken `uvx --refresh --from <pkg> <bin> --version` chain. The trailing `--version` arg was rejected by the upstream MCP binary (no such flag), so the upgrade subprocess always exited non-zero and the user-facing banner reported failure even when the cache refresh itself worked. Promoting to `uv tool install --upgrade` does the equivalent refresh AND moves the binary to PATH so subsequent runs use the faster `uv_tool` detection path. Bug B fix from issue #263.",
        "Fix: `_maybe_update_mcp` now skips the upgrade attempt when the local-version probe returns `None`. Pre-fix, probe-`None` left `up_to_date == None` (not `True`), the short-circuit was bypassed, and the function fell through to a broken upgrade subprocess every TTL window. The user saw an `Updating ... vunknown -> v1.59.1` banner once per kbagent invocation. Post-fix, probe-`None` opts out of the upgrade for this TTL window; the next fresh-cache pass will retry detection. Cache TTL still ticks. Bug C fix from issue #263.",
        "Fix: `maybe_auto_update` now uses a process-level sentinel (`_AUTO_UPDATE_RAN`) to short-circuit subsequent in-process invocations. `kbagent repl` re-enters `main()` -> `maybe_auto_update()` on every prompt iteration; pre-fix, the auto-update banner re-fired once per command typed at the prompt. The sentinel flips to True BEFORE any work so a crash mid-flow still gates subsequent re-entries. Re-exec'd processes (kbagent self-upgrade -> `execvpe` to new binary) start with a fresh sentinel because the module is reloaded into a new interpreter, so the kbagent-self-upgrade -> re-exec -> MCP-stage chain from PR #257 is preserved. Bug D fix from issue #263.",
        "Fix: kbagent no longer reports a successful MCP upgrade when the subprocess returncode == 0 but the local version did not change. `uv tool upgrade keboola-mcp-server` exits 0 even when its dependency resolver backtracks to the previously installed version (real-world reproducer from issue #263: `keboola-mcp-server v1.59.1` declares a `fastmcp==3.2.0` strict-equality constraint that conflicts with the installed `fastmcp==2.13.0.2`, so uv silently resolves to v1.32.0 and exits clean). Pre-fix, kbagent printed `Updated keboola-mcp-server to v1.32.0.` -- the same version the user started from -- once per kbagent invocation. Post-fix, both `_maybe_update_mcp` (auto-update path) and `VersionService._update_mcp` (`kbagent update` path) compare pre-upgrade and post-upgrade versions; only declare success when they actually differ; otherwise emit a diagnostic pointing to `uv tool install --reinstall keboola-mcp-server` and surface the underlying constraint as the likely cause. Bug E fix from issue #263 (reported by @ottomansky on v0.30.2).",
        "Tests: 5 new regression tests pinning the four contracts. `TestPerformMcpUpdate.test_uvx_promotes_to_uv_tool_install` asserts the new uvx command shape and explicitly checks that `--version` is GONE from the cmd. `TestPerformMcpUpdate.test_uvx_promotion_requires_uv` covers the missing-`uv` failure path. `TestProbeNoneSkipsUpgrade.test_local_version_none_skips_upgrade` mocks the probe to return None and asserts `_perform_mcp_update` is never called. `TestProcessLevelSentinel.test_second_call_short_circuits` calls `maybe_auto_update()` three times in the same process and asserts the MCP stage runs exactly once. `TestProcessLevelSentinel.test_sentinel_is_set_even_when_body_raises` verifies the flag flips before any work so a flaky upstream PyPI fetch cannot re-fire the banner per prompt. `TestSelfUpdateTwoStage.test_subprocess_succeeds_but_version_unchanged_reports_not_updated` simulates the @ottomansky reproducer (pre and post both `1.32.0`) and asserts `result['mcp']['updated'] is False` plus a diagnostic message containing `uv tool install --reinstall`. Existing `TestMaybeAutoUpdate`, `TestMaybeAutoUpdateMcpIntegration`, and `TestReExecPathStillRunsMcp` autouse fixtures extended to reset `_AUTO_UPDATE_RAN` between tests so the sentinel does not gate the second test in each class.",
    ],
    "0.30.2": [
        "Fix: `kbagent version` now correctly reports the locally installed `keboola-mcp-server` version. v0.30.1's detection probed `keboola_mcp_server --version`, but the upstream MCP binary does NOT honour `--version` -- it prints its argparse usage block with returncode 0, so the regex found no match and the command displayed `local version unknown` despite a perfectly working install. Reported by an actual user on a fresh upgrade: `kbagent update` printed `keboola-mcp-server vunknown -> v1.59.1` and the version panel said `local version unknown`. The fix moves `uv tool list` to the **preferred** detection path (canonical for the kbagent doctor --fix install method, exact `keboola-mcp-server v1.59.1` line), with `importlib.metadata` and the existing `keboola_mcp_server --version` probe retained as fallbacks. The binary-probe fallback now also strips `usage:` lines before regex-matching so a future `python3.12.9` path component cannot be mistaken for a version. New helper `_uv_tool_list_get_mcp_version(stdout)` parses the `uv tool list` output line-by-line, requires exact first-token equality on the package name, validates the second token as semver-ish, and strips the leading `v`. 8 new unit tests in `TestUvToolListGetMcpVersion` plus 5 rewritten `TestGetLocalMcpVersion` tests including a real-world regression test pinning the upstream usage-help output verbatim.",
    ],
    "0.30.1": [
        "Fix: kbagent now auto-updates `keboola-mcp-server` on startup, the same way it self-updates kbagent itself. Closes the silent-staleness trap reported in #243: a user installed `keboola-mcp-server v1.49.0` once via `uv tool install`, then ran kbagent for months while the upstream MCP server shipped six minor versions; the locally cached schema was missing `configuration_row_ids` (added in MCP v1.55.0) and the user had no signal anything was behind. `auto_update.maybe_auto_update()` now runs two sequential stages: (1) the existing kbagent self-upgrade (re-execs the new binary), then (2) a fresh keboola-mcp-server upgrade. The MCP stage detects the install method (`uv_tool` / `pip_env` / `uvx`) and runs the matching upgrade command (`uv tool upgrade keboola-mcp-server` / `pip install --upgrade keboola-mcp-server` / `uvx --refresh --from keboola-mcp-server keboola_mcp_server --version`). No re-exec needed for the MCP path -- the server is spawned by `tool call` commands and the next spawn picks up the new version. Critical invariant: kbagent up-to-date does NOT short-circuit the MCP stage; both stages always run.",
        "Fix: `kbagent update` (`VersionService.self_update`) now upgrades both kbagent and keboola-mcp-server in a single command. Output reports both stages independently in JSON (`{kbagent: {...}, mcp: {...}, updated: bool, message: str}`) plus a human-readable summary line such as `kbagent v0.30.0 -> v0.30.1 | keboola-mcp-server v1.49.0 -> v1.59.1`. Previously `kbagent update` only ever upgraded kbagent itself, leaving the MCP server pinned to whatever PyPI was on the day the user originally installed it -- the user mental model (`update` = update the kbagent stack) was being silently violated.",
        "Fix: `kbagent version` now reports the locally installed `keboola-mcp-server` version (`version` field) and the up-to-date status (`up_to_date` field) for the dependency, in addition to the existing `latest_version`. Previously the `auto_updates: True` field and the docstring claimed `keboola-mcp-server - always runs latest via 'uvx keboola_mcp_server@latest'` which was incorrect: the actual `detect_mcp_server_command` in `mcp_service.py` deliberately omits `@latest` to avoid a 25s PyPI check on every invocation, so the cached/pinned version persisted indefinitely. After this release the field is once again accurate -- MCP IS auto-updated, but via the kbagent startup auto-update flow (and `kbagent update`), not via uvx-on-every-call.",
        "New: `_get_local_mcp_version()` and `_detect_mcp_install_method()` helpers in `services/version_service.py`. The version probe runs `keboola_mcp_server --version` as a subprocess (works for both direct binary installs and `uv tool install`-managed binaries; both publish a `keboola_mcp_server` script on PATH), with `importlib.metadata.version` as fallback for pip-in-current-env installs. The install-method detector reads `uv tool list` to distinguish `uv_tool` from a pip-installed binary on PATH, then falls back to `importlib.metadata.distribution`, then to uvx availability, then to `none`. The result drives which upgrade command runs in the auto-update stage.",
        "Cache: the version cache file (`~/.config/keboola-agent-cli/version_cache.json`) is extended with `mcp_latest_version` and `mcp_install_method` keys alongside the existing `latest_version` (kbagent). Backwards-compatible -- older cache files lacking the MCP keys are accepted and trigger a fresh fetch in the same run. At most two PyPI/GitHub round-trips per `AUTO_UPDATE_CHECK_INTERVAL`. Auto-install was deliberately NOT added to the startup flow: if MCP is not installed locally (`install_method == 'none'`), the auto-update startup hook reports the latest version to the cache but does NOT run `uv tool install` -- that decision belongs to `kbagent doctor --fix` which is the explicit install entry point.",
        "Tests: 25 new unit tests across `tests/test_version_service.py` (12) and `tests/test_auto_update.py` (13) covering: local version detection (binary stdout, binary stderr, importlib.metadata fallback, missing/timeout cases), install-method detection (uv_tool vs pip_env vs uvx vs none), each upgrade-command shape, the two-stage `self_update` orchestrator (both up-to-date / only-MCP-stale-still-runs / kbagent-stage-failure-still-runs-MCP / blanket exception swallow), and the cache schema migration. Existing `TestMaybeAutoUpdate` is updated to assert the new multi-key `_write_cache` signature and adds an autouse fixture stubbing the MCP helpers so kbagent-stage tests never touch the network or subprocess.",
        "Plugin docs: new `(since v0.30.1)` gotcha entry in `gotchas.md` -- 'kbagent now auto-updates keboola-mcp-server on startup; uv tool install pin is no longer a stale-version trap'. References issue #243 root cause + the install-method detection logic so AI agents can explain the new behaviour to users when asked.",
    ],
    "0.30.0": [
        "New: `kbagent search QUERY` -- top-level cross-project item search. Two modes: **textual** (default) calls the Storage API `GET /v2/storage/global-search` endpoint (name-based, fast, parallel multi-project fan-out via `BaseService._run_parallel()` with per-project error accumulation; one project failing does NOT stop others); **config-based** (`--search-type config-based`) delegates to the existing `ConfigService.search_configs()` for full JSON-body scanning. `--type` is repeatable and accepts `table`, `bucket`, `config`, `flow`, `data-app`, `transformation`; the user-facing names map to the API's `types[]` parameter (config/data-app both translate to `configuration`, with the data-app variant post-filtered to `component_id == 'keboola.data-apps'` so `--type data-app` no longer returns ALL configurations). `--limit` applies per project in textual mode (1-100, default 50). `--project` is repeatable for narrow scope; omitted means all configured projects. Pre-flight `has_feature('global-search')` check returns a clear per-project error rather than a raw 404 on stacks where the feature flag is off. Addresses the cross-project search use-case raised in #244.",
        "New: `kbagent project info --project NAME` -- returns full project metadata in a single call: project ID, project name, stack URL, default backend, the complete `features` list (used by AI agents to gate behavior, e.g. `storage-branches`, `global-search`, `queuev2`), quota limits, and usage metrics. Backed by `KeboolaClient.get_project_info()` which returns the raw `GET /v2/storage/tokens/verify` payload. Distinct from `project status` (connectivity ping) and `project list` (multi-project summary) -- `info` is the canonical single-project audit command. Hint definitions for both `--hint client` (`KeboolaClient.get_project_info()`) and `--hint service` (`ProjectService.get_info()`).",
        "New: `kbagent config row-create --project P --component-id C --config-id K --name NAME [--description D] [--configuration JSON|@file|-] [--is-disabled] [--branch ID]`, `kbagent config row-update --project P --component-id C --config-id K --row-id R [--name N] [--description D] [--configuration JSON|@file|-] [--set PATH=VALUE ...] [--merge] [--is-disabled | --is-enabled] [--dry-run] [--branch ID]`, and `kbagent config row-delete --project P --component-id C --config-id K --row-id R [--branch ID] [--yes]` -- first-class lifecycle for configuration rows. `row-create` returns the full row dict with the API-assigned `id`; capture it for subsequent updates. `row-update` preserves all unspecified fields and accepts the same `--set` / `--merge` / `--dry-run` semantics as `config update`. `--is-disabled` / `--is-enabled` are mutually exclusive on `row-update` and toggle the row's active state (Storage API `isDisabled` field). `row-delete` is destructive (permission level `destructive`, gated behind `--allow-destructive` if the session firewall is on); 404 surfaces as `NOT_FOUND` exit 1 -- deletion is NOT treated as idempotent success. Branch-aware.",
        "New: `kbagent config oauth-url --project P --component-id C --config-id K [--redirect-url URL]` -- generate the OAuth authorization URL for a component that uses OAuth. Mints a short-lived component-scoped Storage API token (1h expiry) via `create_short_lived_token()` and constructs the `https://external.keboola.com/oauth/index.html` URL with `token` + `sapiUrl` query params and `/component_id/config_id` URL fragment. Optional `--redirect-url` adds a `returnUrl` query param so the OAuth wizard redirects the user back to a custom URL after the flow completes. **Requires a master Storage API token** (`canManageTokens` privilege) -- non-master tokens fail-fast with `MISSING_MASTER_TOKEN` exit 3 on a pre-flight check before any HTTP write happens. The OAuth host is now exposed as `OAUTH_HOST` constant in `constants.py` (per Keboola hosting convention `external.keboola.com` is the wizard host across all stacks; the per-stack difference is reflected in the `sapiUrl` parameter, not the wizard host). Tracking issue #260 covers making `project add` / `project refresh` mint master tokens by default so OAuth flows work out of the box.",
        'Architecture: `kbagent search` is registered as a top-level `app.command("search")` rather than a sub-app via `app.add_typer()`. This avoids a Click-Group quirk where `allow_interspersed_args=False` (the default for groups) treats anything after the QUERY positional argument as a subcommand name and rejects `kbagent search test --type table` with exit code 2. The leaf-command registration parses options after positional args correctly. The other documented kbagent commands that use `app.add_typer()` (config, storage, job, ...) are unaffected because their callbacks have no positional args.',
        "UX: `kbagent config --help` now groups its 20 subcommands into 7 Rich help panels via `rich_help_panel`: Browse (list, detail, search), Lifecycle (update, rename, delete, new), Storage (set-default-bucket), Metadata (metadata-list, get-metadata, set-metadata, delete-metadata, set-folder), Variables (variables-set / -get / -clear), Rows (row-create, row-update, row-delete), OAuth (oauth-url). `oauth-url`'s short help line leads with 'Requires master token.' so AI agents see the prerequisite at a glance.",
        "Permissions: new entries in `OPERATION_REGISTRY` -- `search` (read), `config.row-create` (write), `config.row-update` (write), `config.row-delete` (destructive), `config.oauth-url` (read), `project.info` (read). The `--deny-writes` and `--deny-destructive` session firewalls now correctly recognize these commands. Coverage enforced by `tests/test_permissions.py::test_all_subapp_commands_registered` so future drift will fail CI.",
        "New: `ErrorCode.MISSING_MASTER_TOKEN` (`src/keboola_agent_cli/errors.py`) maps to authentication category and exit code 3 (mirrors `INVALID_TOKEN` semantics). Used exclusively by `config oauth-url` pre-flight today; reserved for future commands that mint child tokens.",
        "Tests: ~80 new tests across 4 new test files (`tests/test_search_cli.py`, `tests/test_search_service.py`, `tests/test_config_row_cli.py`, `tests/test_config_row_service.py`, `tests/test_project_info_cli.py`, `tests/test_project_info_service.py`) plus extensions to `tests/test_hints.py` (5 new hint short-circuit tests). All three layers covered (client / service / CLI), per-project error accumulation in textual search verified against multi-project fixtures, master-token pre-flight in oauth-url verified for both branches, data-app post-filter and feature-gate verified, and `should_hint` short-circuit verified for every new command. E2E coverage: live-validated against `kbagent-e2e` and `padak` projects -- 12 of 12 row-create / row-update variants (incl. `--set`, `--merge`, `--dry-run`, `--configuration` with inline / @file / stdin, `--is-disabled` / `--is-enabled`, validation), `row-delete` happy path + 404 on re-delete, `oauth-url` with master token (URL generated with embedded short-lived child token).",
        "Plugin: `keboola-expert.md` tool selection matrix gains 8 rows for the new commands (search-name, search-config-bodies, project info, row-create, row-update, row-delete, oauth-url, config-help groups); VERSION GATE Rule 6 lists `search`, `project info`, `config row-create / row-update / row-delete / oauth-url` as `0.30.0+`. New `(since v0.30.0)` `gotchas.md` entries: `search` is a top-level command (not `config search`); options must follow the QUERY argument; row CRUD lifecycle (capture id from row-create, --is-disabled/--is-enabled mutex on row-update, row-delete is destructive); `config oauth-url` requires a master token. `commands-reference.md`, `context.py` AGENT_CONTEXT, and `CLAUDE.md` `## All CLI Commands` all updated. `SKILL.md` decision table auto-regenerates via the pre-commit hook (CI-checked).",
    ],
    "0.29.0": [
        "BREAKING: `KBC_MANAGE_API_TOKEN` is now ignored by default. The three commands that consume it (`org setup`, `project refresh`, `data-app password`) prompt for the token on a TTY by default. Pass the new top-level flag `--allow-env-manage-token` to restore the legacy env-var behaviour (e.g. for CI/CD). Without the flag and without a TTY, the resolver exits 2 with an actionable message naming the flag. The change closes the AI-exfiltration risk where any subprocess running as the same user (including the AI agent itself) inherits the manage token via env. Migration: prepend `--allow-env-manage-token` to existing CI invocations. Storage tokens (`KBC_TOKEN`) are unaffected. Closes the manage-token UX flagged on #236; supersedes the per-stack design discussed in #238.",
        "Security: `resolve_manage_token` (`src/keboola_agent_cli/commands/_helpers.py`) refactored to default-deny env, TTY-first. When the env var is set but the flag is not passed, a one-shot stderr warning fires (`Warning: KBC_MANAGE_API_TOKEN found in environment but ignored. Pass --allow-env-manage-token to opt in.`) and the resolver falls through to the TTY prompt. No cache, no keyring, no temp file -- next invocation prompts again. The bulk-prompt-once contract (`project refresh --all`) is preserved by construction: the resolver lives at command entry, before any per-project loop.",
        "New: top-level CLI flag `--allow-env-manage-token` (session-only, mirrors `--deny-writes` / `--deny-destructive`). Plumbed via `ctx.obj['allow_env_manage_token']` and forwarded by the three call sites into `resolve_manage_token(allow_env=...)`. Not persisted, no env-var equivalent (intentional; an env-var equivalent would re-create the AI-exfiltration hole this default-deny is closing).",
        "Tests: 12 new (`tests/test_helpers.py::TestResolveManageToken` x7 covering allow_env-True/False x env-set/unset x TTY/non-TTY combinations + token-leak regression pin; `tests/test_manage_token_cli.py::TestAllowEnvManageTokenFlag` x4 covering project-refresh / org-setup / data-app-password through CliRunner with services mocked; `tests/test_manage_token_bulk.py::TestBulkPromptOnce` pinning the contract that `project refresh --all` resolves the token exactly once at command entry, not per-project).",
        "Docs: `commands/context.py` AGENT_CONTEXT updated (org-setup example + env-var help block); `CLAUDE.md` convention #12 + global-flag list; `keboola-expert.md` Rule 6 VERSION GATE adds the 0.29.0+ env-flag requirement, tool-selection-matrix updated, new inline-gotcha block; `gotchas.md` new `(since v0.29.0)` entry naming the warning text and the one-line CI fix; `commands-reference.md` updated for `org setup`, `data-app password`, env-var table.",
        "New: project member & invitation lifecycle. Closes the long-standing Manage API gap that forced every Keboola-internal automation (most recently `17_CuestaDemo/scripts/replicate_master.py` and `invite_participants.py`) to bypass kbagent and POST raw HTTP at `/manage/projects/{id}/invitations`. Seven new commands under `kbagent project`: `invite` (single-shot or `--from-csv` bulk with `ThreadPoolExecutor` parallelism, default 8 workers), `member-list` (active members, `--include-pending` adds pending invitations), `invitation-list`, `invitation-cancel` (resolves invitation_id by email lookup so callers don't have to), `member-remove` (destructive; resolves user_id by email), `member-set-role` (PATCH `/manage/projects/{id}/users/{userId}` with `{role}`). All seven require `KBC_MANAGE_API_TOKEN`; the manage token is never logged, never persisted, never accepted on the CLI line. Permission registry: `member-remove` is `destructive`, `member-list` / `invitation-list` are `read`, the rest are `admin`.",
        "New: role whitelist `PROJECT_ROLES = ('admin', 'guest', 'readOnly', 'share')` in `constants.py`, lifted verbatim from the Manage API's own validation error message (verified empirically on 2026-05-01 against `connection.us-east4.gcp.keboola.com`). Typer enforces the whitelist via `click.Choice` at the command layer; `MemberService` double-checks for defence-in-depth. Invalid role values now fail-fast with `Role 'X' is not valid. Allowed roles are: admin, guest, readOnly, share` instead of letting the API return an opaque 400.",
        "New: `MemberService` (`src/keboola_agent_cli/services/member_service.py`) wrapping six new `ManageClient` methods (`create_project_invitation`, `list_project_invitations`, `cancel_project_invitation`, `list_project_members`, `remove_project_member`, `update_project_member_role`). Resolves project alias -> (stack_url, project_id) via `ConfigStore`; resolves email -> numeric user_id / invitation_id by listing + matching case-insensitively. Treats the Manage API's HTTP 400 'already been invited' / 'already a member' responses as `status=noop` rather than errors (the heuristic the orchestrator scripts had to do via substring matching, now typed to `status_code == 400` AND message-substring marker constants). `--from-csv` enforces a single-stack-URL invariant per file (rows referencing multiple stacks raise `ConfigError` upfront).",
        "New: hint definitions (`hints/definitions/member.py`) for all seven commands. Both `--hint client` (direct `ManageClient` calls) and `--hint service` (`MemberService` calls) generate runnable Python.",
        "New: e2e marker `e2e_invite` (registered in `pyproject.toml`). `make test-e2e-invite` runs `tests/test_e2e.py::test_project_invite_e2e` against a real Manage API; gated on `E2E_MANAGE_TOKEN` + `E2E_INVITE_PROJECT_ID` (skips cleanly when missing). The test invites `ottomansky.max@gmail.com` (override via `E2E_INVITE_EMAIL`) as `guest`, asserts the invitation appears in `invitation-list`, then cancels it -- the same run that proves the system can send confirms it can clean up.",
        "Docs (members): new `references/member-workflow.md` (golden paths for single invite, bulk invite, audit, role change, remove). `gotchas.md` gains three `(since v0.29.0)` entries -- 'already invited / already member' returns HTTP 400 not 422; role-change is PATCH not PUT (PUT returns 404 even on real members); bulk-invite ordering is not deterministic (parallel workers). `keboola-expert.md` adds seven matrix rows under 'Project administration' plus a Rule 6 VERSION GATE entry. `commands-reference.md` adds a 'Project members & invitations' section.",
        "New: `kbagent data-app secrets-set / secrets-list / secrets-get / secrets-remove` — manage `#`-prefixed app-runtime secrets in `parameters.dataApp.secrets`. Encryption is per-project KMS via the existing `EncryptService` (same fail-closed semantics as `--git-pat-encrypted`: refuses to write plaintext if the Encryption API does not return a project-scoped ciphertext). Read-modify-write at the service layer (NOT Storage `merge=True` — that flag is shallow at the top level only and would clobber sibling keys nested inside `parameters.dataApp.secrets`). The runtime exposes each key as an env var with `#` stripped, `-` replaced with `_`, and uppercased (`#my-api-key` → `MY_API_KEY` per help.keboola.com/data-apps/python-js/). `secrets-get` is metadata-only — never echoes decrypted plaintext to stdout / stderr / logs / change descriptions; the Encryption API is one-way and the CLI does not attempt to decrypt under any branch. `secrets-remove` is idempotent (missing keys exit 0 with `removed: 0`). `secrets-set` warns when a derived env-var name collides with `RESERVED_RUNTIME_ENV_VARS` (KBC_TOKEN, KBC_URL — verified canon floor; full runtime list TODO follow-up). Adding/removing a secret bumps the Storage version but the running container keeps the OLD config until the next `data-app deploy`.",
        "New: `kbagent data-app validate-repo --git-repo URL [--git-branch BRANCH] [--git-public/--no-git-public] [--git-pat-env VAR | --git-pat-file PATH] [--type python-js] [--strict]` — pre-flight check that a git repo follows the documented Golden Rule (https://help.keboola.com/data-apps/python-js/) BEFORE `data-app create` so operators don't burn a deploy cycle on a misconfigured repo. Each check emits BLOCKING / WARN / OK with a help-doc citation: `keboola-config/nginx/sites/default.conf` exists, `keboola-config/supervisord/services/app.conf` exists, `pyproject.toml` at root, `keboola-config/setup.sh` content has no `pip install` (BLOCKING per the help canon's pip prohibition) and contains `uv sync` if `pyproject.toml` declares deps, `requires-python` consistent with the runtime image (when the pin is available), nginx `proxy_pass` port matches `app.conf` declared port. Uses `GET /repos/{owner}/{repo}/git/trees/{ref}?recursive=1` (one call) + up to 4 `GET .../contents/{path}` for files whose contents the rules need to inspect — total ≤5 GitHub API calls (1 tree + 0-4 contents) regardless of repo size, sidesteps the 60/hour unauth rate limit for typical use. `--git-pat-env` / `--git-pat-file` raises the limit to 5,000/hour. Read-only; never touches a Keboola project. `--type` is restricted to `python-js` in 0.29.0; streamlit / pure-Python / R / Node-only follow-up.",
        "New: `RepoValidateService` (`src/keboola_agent_cli/services/repo_validate_service.py`) — pure validation function `validate_keboola_repo(snapshot, type_, runtime_python_pin)` plus a tiny `GitHubContentsClient` (HTTPS GET to `api.github.com`, optional bearer PAT, no token persistence). Service module is the only place GitHub HTTP lives; the rest of kbagent stays Keboola-API-only. (Future refactor: extract to `src/keboola_agent_cli/github_client.py` to follow the existing 3-layer architecture; `github_client_factory` injection preserves test coverage today.)",
        "New: `ErrorCode` entries `DATA_APP_INVALID_SECRET`, `DATA_APP_INVALID_REPO`, `DATA_APP_REPO_VALIDATION_BLOCKING`. Permission registry entries `data-app.secrets-set` (write), `data-app.secrets-list` / `data-app.secrets-get` (read), `data-app.secrets-remove` (destructive — removing a secret can break a running app), `data-app.validate-repo` (read).",
        "New: `--hint client/service` for all five new data-app commands. `secrets-get` hint snippet asserts the metadata-only contract; `validate-repo` snippet uses `RepoValidateService.validate_repo(...)` and the hint comment notes that GitHub-side detail is not shown.",
        "Fix: `kbagent data-app create --auth public` now writes the canonical `noneProxyAuthorization` shape (kbc-ui exact constant: `auth_providers: []` + `auth_rules: [{type: pathPrefix, value: /, auth_required: false}]`). v0.27.0 wrote NO `authorization` key when `--auth public`, leaving the Keboola app-proxy unable to route (HTTP 503) and the UI Authentication Type selector blank — silently broken. Authoritative source: the public backend validator at `keboola/job-queue-job-configuration` `AppProxyDefinition.php` (when `auth_required=false`, `auth` MUST NOT be set). The private `keboola/ui` repo `apps/kbc-ui/src/scripts/modules/data-apps/constants.ts` corroborates: its `noneProxyAuthorization` constant exports this exact shape for the None UI option (Keboola org members can verify; external readers rely on the validator). Live-validated end-to-end on a real project: HTTP 200 on the resulting URL, written block bit-identical to canon, UI auth selector now shows None pre-selected. Existing `--auth password` behaviour unchanged.",
        "Tests (data-app secrets / validate-repo): 27 secrets service tests + 20 validate-repo service tests + 22 CLI tests (13 secrets/validate-repo CLI methods + 9 hint-compile AST-parse cases) + 4 new auth-block tests (`TestDataAppCreateAuthBlock` asserts both `--auth public` and `--auth password` write the canonical shape on POST `/apps` AND PUT Storage). E2E coverage in `tests/test_e2e.py::TestE2EDataAppLifecycle::test_data_app_secrets_round_trip` and `::test_data_app_validate_repo_against_public_repo` exercises the full path. Sibling-preservation regression test for `secrets-set` asserts every untouched key under `parameters.dataApp.secrets`, `parameters.dataApp` (slug, git block), `parameters` (id), and the top-level config (`runtime`, `authorization`, `storage`) is preserved bit-identical after the read-modify-write.",
        'Plugin: `keboola-expert.md` matrix gains five new data-app rows (one per `secrets-set / -list / -get / -remove + validate-repo`); §1 Rule 6 VERSION GATE example updated for `secrets / validate-repo need 0.29.0+`. New `(since v0.29.0)` `gotchas.md` entries: (a) secrets are per-project KMS encrypted, `secrets-remove` on missing key is exit 0, `secrets-get` never echoes decrypted plaintext, `#KBC_TOKEN` is silently shadowed by the runtime; (b) `validate-repo` GitHub-only Golden-Rule check; (c) `--auth public` writes the canonical `noneProxyAuthorization` shape (fixes v0.27.0 silent 503). New "Managing app-runtime secrets" + "Pre-flight repo validation" recipe sections in `data-app-workflow.md`. Logs / auto-log-dump deferred to issue #240 (the Data Science API does not expose Terminal Logs as JSON per help canon).',
    ],
    "0.28.0": [
        'Fix: `kbagent config update` now auto-normalizes `parameters.blocks[].codes[].script` from string to array before pushing to the Storage API. Closes #245. The Storage API silently accepts a string for `script` while the runtime schema validator requires an array (`Invalid type for path "root.parameters.blocks.0.codes.X.script". Expected "array", but got "string"`); the broken push lands silently and crashes only at job-run time, often hours later, with no attribution back to the offending write. The CLI now closes the gap on the write side: SQL transformations (`keboola.snowflake-transformation`, `keboola.synapse-transformation`, `keboola.oracle-transformation`, `keboola.redshift-sql-transformation`, `keboola.google-bigquery-transformation`, `keboola.duckdb-transformation`, plus fragment-fallback for self-hosted variants like `*-exasol-transformation` / `*-teradata-transformation`) get statement-level split via the existing `split_statements()` state-machine (respects `\'...\'` / `"..."` / `$$...$$` / `--` / `#` / `//` / `/* ... */`); Python / R / `kds-team.app-custom-python` and any other component sharing the schema get a single-element array wrap. Already-array `script` values pass through unchanged.',
        'Observability: every normalization is surfaced -- the JSON envelope gains a `normalizations: [{path, action: "sql_split"|"wrap_array", before_type, after_type, after_length}]` field per write (and on `--dry-run` the `new_configuration` reflects the post-normalize shape). Human mode prints a yellow `Auto-normalized N script field(s) to array (string -> list). See --json for details.` warning followed by a per-element trace, so the silent fix is observable to operators and AI agents alike. Default behaviour is silent normalize -- the issue\'s preferred design -- because the Keboola UI splitter and `keboola-as-code` produce the same array shape kbagent now writes; the audit fields exist precisely so callers who want to detect "my agent produced a string" can.',
        "Fix (silent gap): `SQL_TRANSFORMATION_COMPONENTS` in `src/keboola_agent_cli/sync/code_extraction.py` was missing `keboola.google-bigquery-transformation` and `keboola.duckdb-transformation`, so `kbagent sync push` previously did NOT split semicolons in BigQuery / DuckDB transformations -- it joined every statement into a single `script` element. Same failure shape as #119 (closed for Snowflake / Synapse / Oracle / Redshift), just on different backends. The fragment-based `is_sql_transformation_component()` helper now also matches `*-bigquery-transformation`, `*-duckdb-transformation`, `*-exasol-transformation`, `*-teradata-transformation`, so newer or self-hosted SQL backends do not require an edit to the exact set.",
        "Plumbing: new `normalize_blocks_codes_script(component_id, config) -> (config, normalizations)` helper in `src/keboola_agent_cli/sync/code_extraction.py`, called from `ConfigService.update_config` immediately after `_resolve_configuration` (before the Storage API write). 35 new unit tests in `tests/test_normalize_script.py` covering the registry detection (exact + fragment fallback), splitter edge cases (semicolons inside block comments and string literals), per-component dispatch (SQL split vs Python wrap vs already-array passthrough), `ConfigService` integration (write path, dry-run path, `--set` path), and CLI surfacing in both JSON and human modes. New E2E test class `TestE2EConfigUpdateNormalization` in `tests/test_e2e.py` exercising the full path against a real Snowflake transformation: push string-script -> Storage API stores array -> job runs to `success`. Live-validated against project 901 (`padak`).",
        "Plugin: new `(since v0.28.0)` gotcha in `gotchas.md`; `keboola-expert.md` Rule 6 VERSION GATE updated; `commands-reference.md` `config update` bullet annotated; `sql-migration-workflow.md` cross-references the new normalize behaviour next to the `MULTI_STATEMENT_COUNT` section. Upstream `update_sql_transformation` / `create_sql_transformation` MCP tools still need a parallel fix in `keboola-mcp-server` -- a separate issue is recommended.",
        "New: `kbagent storage swap-tables --project P --table-id A --target-table-id B [--branch ID] [--dry-run] [--yes]` -- thin wrapper around the Storage API `POST /v2/storage/branch/{branch}/tables/{id}/swap` endpoint. Both tables exchange physical positions; aliases are NOT transferred (they keep pointing at the same physical position and therefore expose the OTHER table's data after the swap). The Storage API queues this as an async storage job (`operationName: tableSwap`); the client polls to completion before returning, so callers can rely on the schemas already being exchanged on return (~10s observed on Snowflake). The API restricts this to dev branches; the service refuses with exit 5 (`ConfigError`) before any HTTP call when neither `--branch` nor an active branch (via `kbagent branch use`) is set. Same-source-and-target IDs also rejected pre-flight. The use case is: AI agent profiles a typeless table, builds a typed rebuild via CTAS in a workspace, then swaps the typed copy into the original name without touching downstream config references that point at the original table ID. Permission classification: `destructive` (gated behind `--allow-destructive`). The PHP reference client docstring claims a synchronous response, but live calls against the platform consistently return a queued job -- this client polls the job to completion to make the `delete_table` / `create_table` semantics consistent. Companion entry in `storage-types-workflow.md` explains the typify-via-CTAS pattern; gotchas + commands-reference + agent prompt all updated.",
        "Tests (swap-tables): `tests/test_storage_swap.py` (14 tests) covers all three layers -- HTTP shape (POST + body + URL encoding + immediate-success path + async-poll path + 4xx propagation via `pytest_httpx`), service business logic (success, dry-run, branch enforcement, same-id guard, API error propagation, unknown project), and CLI integration (JSON happy path, dry-run, explicit `--branch` overrides active, missing-branch error path with exit 5). E2E coverage in `tests/test_e2e.py::TestE2EStorageSwapTables` runs three scenarios against a live API: live swap of two tables with different VARCHAR lengths verifies definitions exchange in both directions; dry-run skips API call and `lastChangeDate` is unchanged; and the production-rejection path (no branch + no active branch) returns exit 5.",
        "Plugin docs: new `plugins/kbagent/skills/kbagent/references/typify-table-workflow.md` -- end-to-end procedure for converting a typeless Storage table (every column `STRING(16M)`) into one with proper Snowflake / BigQuery native types. 8 phases: (0) decide-or-skip rubric; (1) isolate in dev branch; (2) profile the typeless table in a workspace with length / cardinality / parse-failure / scale-precision queries + decision matrix mapping profile signals to Snowflake types; (3) build typed sibling via `storage create-table` + copy data via in-workspace INSERT or SQL transformation, with row-count / NULL-count verification; (4) validate downstream consumers in the dev branch (search configs that reference the table, run a representative transformation against the typeless source as baseline); (5) `swap-tables` (dry-run + actual + verify); (6) re-run downstream as smoke test; (7) cleanup the sibling after merge; (8) handoff protocol -- structured summary the AI agent hands to the user with phase-by-phase receipts, the merge URL, and rollback / cleanup commands. Cross-references `storage-types-workflow.md`, `branch-workflow.md`, `workspace-workflow.md`, `gotchas.md`. SKILL.md workflow-references table gains the new entry.",
    ],
    "0.27.0": [
        "New: `kbagent data-app` command group — first-class lifecycle for Keboola data apps (`keboola.data-apps` Storage component + Data Science API `/apps`). Eight subcommands: `list`, `detail`, `create`, `deploy`, `start`, `stop`, `delete`, `password`. The CLI encapsulates the **§9 redeploy contract** (always sends the `{desiredState=running, configVersion, restartIfRunning=true}` trio together; without it, `PATCH /apps {desiredState:running}` silently pins to the empty-shell v2 and the runner errors `dataApp.git.repository is required in /data/config.json`), per-project KMS encryption of git PATs (refuses to write plaintext if the Encryption API does not return a project-scoped ciphertext), cleanup-in-finally on initial-deploy failure (orphan shell deleted by default; `--keep-on-failure` opts out), and a poll loop that respects pitfall #1 — `state == stopped` is NOT terminal while `desiredState == running` (the platform transitions `created → stopped → starting → running` during initial deploy). `data-app create` accepts `--git-pat-env VAR` (recommended; no argv leak), `--git-pat-file PATH`, or `--git-pat-encrypted KBC::Project...` (must be encrypted under THIS project's KMS — ciphertext does not cross projects).",
        "New: `DataScienceClient` (`src/keboola_agent_cli/data_science_client.py`) — third HTTP client class alongside `KeboolaClient` and `AiServiceClient`. Auth via `X-StorageApi-Token`; URL derived as `data-science.{stack-suffix}` from the connection URL; inherits `BaseHttpClient` for retry/backoff/token-masking. `get_app_password()` accepts the Manage token per-call so it never lives on the persistent client.",
        "New: `ErrorCode` entries `DATA_APP_BUILD_FAILED`, `DATA_APP_DEPLOY_TIMEOUT`, `DATA_APP_INVALID_GIT` for surfacing data-app-specific failure modes; `data-app deploy` and `data-app create --wait` map to these on poll-loop terminal states. Existing codes (`NOT_FOUND`, `VALIDATION_ERROR`, `ENCRYPTION_FAILED`, `INVALID_TOKEN`) cover the rest.",
        "New: `--hint` mode supports `client_type=data_science`. `kbagent --hint client data-app deploy …` now generates `DataScienceClient` instantiation + `patch_app(...)` call with the §9 trio inline.",
        "Tests: 30 service-level tests in `tests/test_data_app_service.py` (validation, dry-run, happy-path orchestration, cleanup-in-finally, encryption-failure-aborts-loud, poll-loop semantics including the transient-stopped invariant), 10 CLI tests in `tests/test_data_app_cli.py` (mutual-exclusion validation, dual JSON+human output, `--yes` for delete, manage-token forwarding for password without leaking the token to stdout/stderr).",
        "Plugin: new `data-app-workflow.md` reference + two `(since v0.27.0)` gotcha entries (the §9 redeploy contract; cross-project KMS ciphertext mismatch). `keboola-expert.md` matrix gains five rows (`create`, `deploy`, `start`, `stop`, `delete`).",
    ],
    "0.26.0": [
        "New: `kbagent config set-default-bucket --bucket BUCKET_ID | --clear [--dry-run] [--branch ID]` -- discoverable wrapper around the raw-mode `storage.output.default_bucket` workaround documented at https://keboola.atlassian.net/wiki/spaces/SUP/pages/3770155030/ (epic KBCP-108). Read-modify-write that preserves all sibling keys under `storage.output` and the rest of the configuration. Same-value writes short-circuit with `{\"changed\": false}` (no API call, no version bump). `--clear` removes only the `default_bucket` key, leaving an empty `storage.output: {}` if no other siblings live there (intentional -- mirrors `set_nested_value`'s parent-creation semantics; Storage API treats `output: {}` and missing `output` identically as 'use the auto-derived bucket'). Live-validated end-to-end on three component types -- row-based GCS extractor, root-only `keboola.ex-cnb-exchange-rates`, and `ex-generic-v2` with multiple jobs -- output tables routed to the configured bucket at job runtime in every case. The per-table `destination` override (the second method shown in the support article) keeps using the existing `kbagent config update --set 'storage.output.tables=[...]'` -- no new wrapper there because per-table mappings have many fields that don't fit a single-purpose flag.",
        "Fix: `kbagent sync pull --with-samples` no longer crashes with `TypeError: '>' not supported between instances of 'NoneType' and 'int'` when one or more tables in the project return `rowsCount: null` from the Storage API (typical for newly-created or empty tables on some backends, reproduced live against `kosik-sales`). `dict.get(\"rowsCount\", 0)` returns the default `0` only when the key is **missing** -- if the key is present with a `null` value, `.get()` returns `None`, and the `> 0` comparison crashed Python 3 before any sample was fetched. The filter and sort key in `SyncService._fetch_samples()` now coerce `None` to `0` via a small `_rows()` helper used in both places (`t.get(\"rowsCount\") or 0`), so empty/null-rowcount tables are gracefully skipped exactly like `rowsCount: 0` ones. Closes #233.",
        "Fix: same `dict.get(k, 0)` -> `dict.get(k) or 0` defensive coercion applied to 5 sibling locations that did not crash but produced inconsistent JSON shapes (`null` instead of `0`) when the Storage API returned `rowsCount` / `dataSizeBytes` / `tablesCount` as null: `SyncService._write_storage_metadata()` (both the buckets-index summary at `storage/buckets.json` and per-table JSON files at `storage/tables/<bucket>/<table>.json`), `StorageService.list_buckets()`, `StorageService.list_tables()`, `StorageService.get_table_detail()`, and `SharingService.list_shared()`. Without this, JSON consumers (LLM agents reading `.keboola/` workspace files, downstream aggregation that does arithmetic) would see `null` for what is documented as an `int` field. Behavior is otherwise unchanged -- the fix only flips the Python serialization of API-returned-null from `null` to `0`.",
        "Tests: 7 new regression tests covering the full null-coercion surface: 2 in `tests/test_sync_storage_jobs.py::TestFetchSamples` (filter + sort paths -- both fail on 0.25.3 with the exact `TypeError` from the issue traceback), 1 in `tests/test_sync_storage_jobs.py::TestWriteStorageMetadata` (verifies `storage/buckets.json` and per-table JSON files surface `0` not `null`), 2 in `tests/test_storage_tables.py::TestStorageNullNumericCoercion` (`list_buckets` and `list_tables` service output), 1 in `tests/test_storage_describe_service.py::TestGetTableDetailDescriptionExtraction` (`get_table_detail`), and 1 in `tests/test_sharing_service.py::TestListShared` (`list_shared`).",
    ],
    "0.25.3": [
        'Fix: `kbagent storage bucket-detail` now emits backend-native direct-access paths instead of always returning Snowflake-style `"db"."schema"."table"` quoting. BigQuery buckets get `bigquery_dataset` + per-table `bigquery_path` quoted with backticks (`` `dataset`.`table` ``), and the misleading `snowflake_database` / `snowflake_schema` / `snowflake_path` keys are no longer included on BigQuery results -- they were syntactically invalid SQL on BQ and silently misled callers. Snowflake buckets keep the legacy keys unchanged (full backwards compatibility). New backend-agnostic keys `sql_dialect` (e.g. `"snowflake"`, `"bigquery"`) and per-table `sql_path` are always present, so callers can build the right path without branching on backend themselves. Also: the Snowflake-only `f"sapi_{project_id}"` fallback (used when `backendPath` is missing) no longer fires for BigQuery, where it would have produced a nonexistent identifier. Mirrors a parallel fix in keboola-mcp-server v1.59.0 (`create_sql_transformation` / `update_sql_transformation` dialect-aware quoting).',
        "Fix: BigQuery FQN handling -- when the Storage API surfaces `databaseName` (GCP project ID) on a BigQuery bucket, `bucket-detail` now emits a fully-qualified `` `project`.`dataset`.`table` `` path. When the API leaves `databaseName` empty (typical for Keboola-managed BQ projects), the path is dataset-qualified only and `bigquery_project` is the empty string -- callers requiring a full FQN must supply the GCP project name themselves.",
        "Tests: 4 new unit tests in `tests/test_storage_describe_service.py::TestGetBucketDetailBackendPaths` covering Snowflake (legacy keys preserved + new `sql_path`), BigQuery without `databaseName` (dataset-qualified path, no `snowflake_*` leakage), BigQuery with `databaseName` (full FQN), and Snowflake linked-bucket `backendPath`-wins-over-source-fallback.",
    ],
    "0.25.2": [
        "New: branch-aware storage writes detect projects without the `storage-branches` feature flag (legacy fake-branch projects, e.g. project 10539 `padak-2-0`) and surface `legacy_branch_storage: true` in the JSON response of `storage create-bucket --branch X` and `storage create-table --branch X`. Human mode prints a `[yellow]Warning:[/yellow]` line below the success summary explaining that the transformation runner ignores buckets created via `/v2/storage/branch/<id>/buckets` on such projects -- at job time the runner rewrites destinations to `out.c-<branch_id>-*` in the default branch, so the kbagent-materialized bucket is reachable from the branch view but is never written to by transformations. Behavior of the API call itself is unchanged; the warning is purely informational. Reproduced end-to-end against project 10539 (no feature) and 10546 (`kbagent-e2e`, feature ON) -- the metadata stamp from #224 fires on both, but only on storage-branches=ON projects does the runner consume the bucket.",
        'Client: `KeboolaClient` now caches project features (`get_project_features() -> frozenset[str]` and `has_feature(flag) -> bool`). Cache is populated lazily on the first `verify_token()` / `has_feature()` call and lives for the life of the CLI invocation -- callers branching on multiple feature flags pay one HTTP round-trip rather than N. New `STORAGE_BRANCHES_FEATURE = "storage-branches"` constant in `constants.py` keeps the flag string out of business logic.',
        'Docs: new §"Fake-branch vs `storage-branches`: when `--branch X` is a no-op for the runner" in `storage-types-workflow.md` (full mechanics + reproduction recipe). New `gotchas.md` entry tagged `(since 0.25.2)`. Updated `keboola-expert.md` inline gotchas with explicit guidance for AI agents seeing `legacy_branch_storage: true` ("do NOT plan downstream `look in out.c-foo` steps -- the runner writes to `out.c-<branch_id>-foo`"). `commands-reference.md` and `kbagent context` AGENT_CONTEXT updated for `create-bucket` / `create-table`.',
        "Plugin: new `kbagent-pr-reviewer` autonomous read-only PR reviewer subagent (`plugins/kbagent/agents/kbagent-pr-reviewer.md`). Walks the full review playbook from `CONTRIBUTING.md` (3-layer architecture, Plugin synchronization map, silent-drift hunt, test coverage, behavior verification) and posts ONE comment review per invocation via `gh pr review --comment --body-file`. Spawned by the new `/kbagent:review [PR#|URL]` slash command (`plugins/kbagent/commands/review.md`); auto-detects the open PR for the current branch when called with no argument, accepts trailing free text as a `<focus>` hint. Hard guardrails: tools limited to `Bash, Read, Grep, Glob` (no `Write`, `Edit`, `git checkout/push/merge`, or `gh pr review --approve / --request-changes / merge / close / ready`). Output contract: <=15 findings per review, every finding has `file:line` + severity (BLOCKING / NON-BLOCKING / NIT); verdict is advisory only -- the GitHub review state stays neutral so the human author makes the final call. The subagent posts English-only bodies to the GitHub side regardless of the parent agent's prompt language; only the brief 3-5 line in-process return summary can match the parent's language.",
        "Docs: `CONTRIBUTING.md` and `CLAUDE.md` now bind plugin & agent surfaces to the release process. New `## Plugin synchronization map` table in `CONTRIBUTING.md` enumerates every silent-drift surface that CI does NOT catch (`commands/context.py` AGENT_CONTEXT, `CLAUDE.md` `## All CLI Commands`, `keboola-expert.md` rules + matrix + gotchas, `gotchas.md` `(since vX.Y.Z)` version tags, per-topic workflow files, `permissions.py` OPERATION_REGISTRY, `hints/definitions/*.py`). New `## Releasing a new version` section codifies the 12-step release checklist. `CLAUDE.md` convention #17 updated to reflect the silent-drift risk inventory.",
        "Docs: new `## Self-review before tagging a human` policy in `CONTRIBUTING.md` mandating a `/kbagent:review` self-review pass on every PR before tagging a human reviewer. Plugin `CLAUDE.md` (`plugins/kbagent/.claude-plugin/CLAUDE.md`) documents the handoff protocol -- act on subagent's BLOCKING findings, NON-BLOCKING is a judgment call, NITs are optional.",
    ],
    "0.25.1": [
        'Fix: `kbagent storage create-table --branch <ID>` now stamps the auto-materialized bucket with `KBC.createdBy.branch.id = <branch>` (provider=`system`) immediately after creation. Without it, projects with the **branched storage** feature flag enabled fail every subsequent transformation output mapping with `Trying to create a table in the development bucket "X" on branch "Y" (ID "Z"), but the bucket is not assigned to any development branch.` -- the error surfaces from `keboola/output-mapping` (`Storage/BucketCreator::checkDevBucketMetadata`), which requires this exact metadata key. The same bug exists in the official Go CLI (`keboola-as-code/pkg/lib/operation/project/remote/table/import/operation.go::EnsureBucketExists`), but kbagent users hit it first because they tend to drive transformation runs from CLI rather than UI. Metadata write is best-effort: a 403/5xx is logged and the create-table call still proceeds, so users without bucket-metadata permission do not regress. Closes #224.',
        'Client: `KeboolaClient.set_bucket_metadata()` gains a `provider: str = "user"` keyword. Default unchanged for existing CLI describe paths; auto-materialize uses `provider="system"` (the API rejects user-provider writes on the reserved `KBC.*` namespace).',
    ],
    "0.25.0": [
        "New: `kbagent storage create-table` accepts native backend column types with length. Base types (STRING/INTEGER/NUMERIC/FLOAT/BOOLEAN/DATE/TIMESTAMP) still work unchanged; on top of them, any `--column name:TYPE(length)` spec flows through to the Storage API -- `pk:VARCHAR(40)`, `amount:NUMERIC(18,2)`, `ts:TIMESTAMP_TZ`, `meta:VARIANT`, `n:NUMBER(6,0)`, etc. The hard-coded whitelist (`VALID_COLUMN_TYPES` in `constants.py`) has been removed; type/length validation is delegated to Keboola, which returns precise per-backend errors (e.g. `'10' is not valid length for INTEGER`). Closes #192.",
        "New: `--not-null COLUMN` and `--default NAME=VALUE` flags on `storage create-table`. Both are repeatable; both fail fast (exit 2) if the referenced column is not defined by any `--column`. Boolean defaults must be lowercase (`--default flag=false`) per Keboola API validation.",
        "New: `storage create-table --branch <ID>` auto-materializes the target bucket in the dev branch when the branch has not yet been written to there. Mirrors the official Go CLI's `EnsureBucketExists` pattern (keboola-as-code: `pkg/lib/operation/project/remote/table/import/operation.go`). Response surfaces this via `auto_created_bucket: bool`; production writes (no `--branch`) never materialize anything. Closes #222.",
        "Service: `StorageService.create_table` gains `not_null_columns` and `defaults` keyword arguments. `--hint service` code-gen includes them. `--hint client` still generates raw CLI column strings and now includes a guidance note on converting them to the API's `[{'name': ..., 'definition': {...}}]` shape.",
        "Enhancement: `storage table-detail` `column_details` entries now surface `native_type` (Snowflake/BigQuery-level type name, e.g. `VARCHAR`, `NUMBER`, `TIMESTAMP_TZ`), `length` (e.g. `40`, `18,2`), and `default` (DEFAULT expression as stored). Previously only `type` (basetype) and `nullable` were exposed. Fully backwards-compatible -- existing `type`/`nullable`/`description` fields unchanged.",
        "Docs: new reference `plugins/kbagent/skills/kbagent/references/storage-types-workflow.md` (Snowflake type cheat sheet, attribute flags, dev-branch materialize contract, common gotchas). New SKILL.md workflow row. Updated `gotchas.md` with the 0.25.0 create-table behaviour. Extended `keboola-expert.md` tool matrix and inline gotchas.",
        'Docs: new §8 in `docs/TUTORIAL.md` -- "Advanced storage: native column types + dev-branch materialize". Includes a retype-after-profiling example, the branch materialize walkthrough, and a Snowflake type cheat sheet. New VHS demo `docs/demos/demo-storage-types.tape` + rendered `docs/assets/demo-storage-types.gif` showing the full round-trip (branch create -> create-table with native types -> table-detail -> cleanup).',
        "Tests: 13 new service-level tests in `tests/test_storage_write.py` covering the parser, native types with length, `--not-null` + `--default`, auto-materialize on 404, non-404 propagation, production no-op path, and unknown-column error guard. New E2E class `TestE2EStorageNativeTypesAndBranchMaterialize` in `tests/test_e2e.py` validating that `VARCHAR(40) / NUMERIC(18,2) / TIMESTAMP_TZ / VARIANT / BOOLEAN` round-trip through to Snowflake with `definition.length / nullable / default` intact.",
    ],
    "0.24.2": [
        "Docs: new §7 in `docs/TUTORIAL.md` -- GitOps workflow connecting `kbagent sync pull` with local git branches. Covers first-time setup (with the full on-disk file tree showing extracted `transform.sql` / `code.py` / `pyproject.toml` and preserved `KBC::ProjectSecure::` encrypted values), feature-branch edit loop, merge-back via `branch merge`, the git-branching safety model (linked/unlinked/main mapping table + hit-the-wall example), and common gotchas (locally modified skips, name drift, `--adopt-existing`, `--dry-run`, `--with-samples`).",
        "Docs: new VHS demo `docs/demos/demo-sync-pull.tape` + rendered `docs/assets/demo-sync-pull.gif` -- live recording of the full GitOps workflow (project add -> git init -> sync init --git-branching -> sync pull -> git checkout -b -> sync branch-link -> sync branch-status) against an isolated demo project. Tape uses `KBAGENT_CONFIG_DIR` to keep the sandbox isolated from the user's global config.",
    ],
    "0.24.1": [
        "New: top-level `--version` / `-V` flag on `kbagent` -- standard CLI convention that previously only worked as the `kbagent version` subcommand. Eager callback prints `kbagent vX.Y.Z` and exits before any further parsing.",
        "Docs: README and `docs/TUTORIAL.md` now embed four short animated terminal demos (VHS-generated GIFs under `docs/assets/`): hero demo at the top of README, 30-second workflow overview, `kbagent doctor` output in the tutorial prerequisites, and the `project add` flow in §1. Tape sources are under `docs/demos/*.tape` and can be regenerated with `vhs docs/demos/<name>.tape`.",
        "Fix: `kbagent changelog` no longer duplicates output right after an auto-update. The root callback previously printed the `What's new in vX` summary via `show_post_update_changelog()` AND the `changelog` command then printed the full changelog, so the same bullets appeared twice. The fix consumes the `KBAGENT_UPDATED_FROM` env var on `changelog` invocations (user will see the content in the command body) and falls through to the regular path for all other commands. Regression test in `tests/test_auto_update.py::TestChangelogCommandConsumesWhatsNewTrigger`.",
        "Build: `scripts/sync_version.py` now keeps `.claude-plugin/marketplace.json` in sync alongside `plugins/kbagent/.claude-plugin/plugin.json`. The kbagent entry inside `plugins[*]` was previously missing a `version` key entirely -- the script now writes and maintains it, placed immediately after `name` for easy review. The marketplace descriptor's top-level `version` is deliberately NOT touched (it is the catalogue-shape version, bumped only when plugins are added/removed). Idempotent; fail-safe when `marketplace.json` is absent or the kbagent entry is missing. Ten unit tests in `tests/test_sync_version_script.py` cover every branch.",
        "Docs: `scripts/sync_version.py` docstring rewritten to document all three version-bearing files and which ones the script owns.",
    ],
    "0.24.0": [
        "New: `kbagent doctor` now checks whether the Claude Code plugin is installed at `~/.claude/plugins/cache/keboola-agent-cli/kbagent/<version>/`. Reports 'skip' if Claude Code is not present on the host, 'warn' with copy-pasteable `/plugin marketplace add` + `/plugin install` commands when the plugin is missing, and 'pass' with the installed version (detected from the cache subdir name or from `plugin.json`). Flags CLI-vs-plugin version drift with a `/plugin update kbagent` hint.",
        'New: `.kbagent/config.json` now starts with a `_warning` field that steers any LLM reading the file away from direct REST calls ("THESE ARE KEBOOLA STORAGE API TOKENS. NEVER use them to call the Keboola REST API directly..."). Written on every save by `ConfigStore`; silently ignored by `AppConfig` on load (Pydantic default: extra = ignore). Guidance lives where the agent already looks when inspecting tokens.',
        "Plugin: new `kbagent:keboola-expert` specialist subagent (`plugins/kbagent/agents/keboola-expert.md`). Fresh context window, ~10k-token system prompt with hard rules + inline gotchas + tool selection matrix + output contract (JSON verification payload). Main agent delegates complex Keboola tasks to this subagent to avoid drift. See docs/plugin-agent.md §4-§5 for the architecture.",
        "Plugin: new `/keboola <task>` slash command (`plugins/kbagent/commands/keboola.md`) -- explicit user-invoked delegation to the keboola-expert subagent, bypasses skill-trigger uncertainty.",
        "Plugin: new plugin-level `CLAUDE.md` (`plugins/kbagent/.claude-plugin/CLAUDE.md`) -- always-loaded hint instructing the main agent to delegate Keboola work to the expert subagent, with a handoff protocol for `dry_run_only` / `refused` statuses.",
        "Test: static regression suite `tests/test_agent_prompt.py` (42 tests) verifying the pilot prompt contains all non-negotiable rules, inline gotchas, tool matrix rows, output contract fields, refusal format, and self-check section. Guards against future trimming that would re-introduce agent-compliance drift.",
        "Test: E2E `test_flow_update_preserves_behavior_onerror` proving that `kbagent flow update` preserves `behavior.onError` on partial updates (rename-only, description-only) and that `--file` is full-replace semantics that drops behavior silently if omitted. Resolves plan §10.1 open question.",
    ],
    "0.23.0": [
        "New: `kbagent config detail --component-id ID` (without --config-id) -- bulk mode returning an array of all configs for the component across one/many/all projects in parallel. Preserves single-config JSON shape when --config-id is also passed. Addresses the 102-subprocess audit pattern from #197.",
        "New: `kbagent config detail --with-state` -- attaches runtime state dict to each config. Bulk mode fetches state inline via include=state on the listing endpoint (no N+1). Single mode reads state directly from the detail endpoint response (no extra HTTP call -- Storage API embeds state inline; there is no standalone state resource).",
        "New: `kbagent config list --include-rows` -- opt-in flag that adds configuration+rows bodies to each row via list_components_with_configs(include=configuration,rows). Without the flag, list remains the summary-level endpoint.",
        "Client: `get_config_state` convenience wrapper over `get_config_detail().get('state', {})`; `list_components_with_configs` gained optional `include_state` parameter for bulk state fetching.",
        "Security: UNEXPECTED_ERROR envelopes now truncate exception messages to 256 chars with a trailing `...` sentinel before emission. Prevents OAuth refresh tokens, URL query strings, and other credential-bearing fragments from leaking into JSON error output under --with-state (CWE-209). Full message still reaches debug logs.",
        "New: `kbagent schedule list [--project ...] [--enabled-only] [--branch ID]` -- fleet-wide listing of keboola.scheduler configs across one/many/all projects in parallel; each row carries project_alias, schedule_id, schedule_name, parent_component_id, parent_config_id, parent_name, cron, timezone, enabled. Addresses the fleet-wide audit gap from #195.",
        "New: `kbagent schedule detail --project NAME --schedule-id ID [--branch ID]` -- single schedule with full cron + timezone + parent config link + enabled state; tolerates orphaned parent configs (parent_name empty).",
        "New: `kbagent schedule find [--cron-window START-END] [--not-run-since DAYS] [--project ...] [--branch ID]` -- audit filters combinable with AND; cron-window matches schedules whose hour field fires only within the window (hour-level approximation; minute-level ignored); not-run-since joins with latest `job list` for the parent config. Columns last_run_at and matches_cron_window are present in every row but populated only when the corresponding filter is active.",
        "New: `kbagent flow list --with-schedules` -- enrichment flag attaches schedules[] to each flow row via one extra list_component_configs(keboola.scheduler) call per project (not per flow). Partially closes #195.",
        "Fix: `schedule find --cron-window` now rejects malformed minute fields like `02:70` at parse time rather than silently accepting them. The matcher still works at hour-level granularity by design; this is purely a UX/error-message improvement.",
        "Fix: `schedule find` without filters now emits `matches_cron_window: None` (previously a hard-coded `True`) so LLM/agent consumers do not treat the column as a positive match signal when no window filter is active. Columns are always present; population is gated on the corresponding filter.",
    ],
    "0.22.0": [
        "New: `kbagent project use <alias>` -- pin a project as the default for subsequent commands. Persists `default_project` in config.json (the field already existed; now there is an explicit CLI verb to set it).",
        "New: `kbagent project current` -- print the effective default project and its source (env / pin / none). Reports both the env override and the persisted pin so misconfigurations are visible, not silent.",
        "New: `KBAGENT_PROJECT` env var overrides the persisted pin for a single shell/session. Resolution precedence for single-project ops: explicit `--project` > `KBAGENT_PROJECT` > pin > sole-project fallback > fail-hard with CONFIG_ERROR.",
        "New: top-level `--deny-writes` / `--deny-destructive` flags synthesize a session-only firewall that merges with any persisted permission policy. Never written to config.json. `--deny-writes` blocks the wide net (write+destructive+admin); `--deny-destructive` is narrower and blocks only data destruction.",
        "New: `ProjectService.resolve_pinned_alias()` plus `commands._helpers.resolve_project_alias()` -- single-project alias resolution contract for write/destructive commands. Public API for future PRs to adopt; FIIA P0-4 acceptance criterion.",
        "Fix: stale pin (default_project pointing at a deleted alias) now raises a repair-friendly CONFIG_ERROR with `kbagent project use <alias>` guidance instead of silently fanning out.",
        "New: `kbagent flow list` -- list all flows (keboola.orchestrator + keboola.flow) across one or all projects; supports --project, --branch",
        "New: `kbagent flow detail` -- full phase/task breakdown for a single flow config, including phase dependency graph and orphan detection",
        "New: `kbagent flow schema` -- print the YAML template for flow configuration (phases + tasks) for use with --file",
        "New: `kbagent flow new` -- create a flow with optional phases/tasks from a YAML/JSON --file; validates DAG before create",
        "New: `kbagent flow update` -- update flow name, description, or phases/tasks; validates DAG before write; fetches current config before partial update",
        "New: `kbagent flow delete` -- delete a flow config with --yes confirmation guard",
        "New: `kbagent flow schedule` -- attach a cron schedule via keboola.scheduler; supports timezone and enabled/disabled state",
        "New: `kbagent flow schedule-remove` -- remove all cron schedules attached to a flow; idempotent, --yes confirmation guard",
        "New: config metadata-list / get-metadata / set-metadata / delete-metadata -- CRUD for arbitrary metadata key/value pairs on any configuration, using the branch-aware Storage API metadata endpoint (FIIA P1-3)",
        "New: config set-folder -- sugar over set-metadata for KBC.configuration.folderName; organises configs into named folder groups visible in the Keboola UI (FIIA P1-3)",
        "New: workspace list --orphaned -- lists workspaces backed by keboola.sandboxes whose sandbox config no longer exists (FIIA P1-4)",
        "New: workspace gc [--dry-run] [--yes] -- deletes all orphaned workspaces; dry-run previews without touching anything; --yes skips interactive confirmation (FIIA P1-4)",
        "New: `kbagent storage describe-bucket` -- set KBC.description on a bucket via metadata POST (upsert-by-key, provider=user); surfaces on bucket-detail",
        "New: `kbagent storage describe-table` -- set KBC.description on a table; surfaces on table-detail",
        "New: `kbagent storage describe-column` -- set per-column descriptions using the `KBC.column.{name}.description` convention in table metadata; readable via table-detail column_details[].description",
        "New: `kbagent storage describe-batch --from-file YAML` -- apply bucket/table/column descriptions in one shot; failures collected without aborting the remaining items (progress spinner in human mode)",
        "Fix: `storage bucket-detail` now returns `description` and `metadata` fields extracted from the bucket metadata array; KBC.description (provider=user) wins over the native creation-time description field",
        "Fix: `storage table-detail` now returns `description` and `metadata` fields extracted from the table metadata array",
        "New: `kbagent job run --wait` uses an exponential polling curve (2s x 30 -> 5s x 48 -> 15s) instead of a fixed 1s interval (FIIA P0-3) -- matches the cadence used by the keboola-as-code Go CLI; pass `--poll-strategy fixed` to keep the legacy 1s behaviour for tests or very short jobs",
        "New: `--log-tail-lines N` on `job run` -- on FAILED / WARNING / TERMINATED jobs, kbagent fetches the last N Storage Events (/v2/storage/events?runId=...) and surfaces them as `logTail` in --json output or `details.logTail` on errors; default 200, max 5000, 0 disables",
        "New: `--timeout` on `job run --wait` now auto-cancels the remote job -- when the local deadline expires kbagent issues `kill_job` against the Queue and exits 7 (`JOB_TIMEOUT_TERMINATED`) with the cancelled job + logTail in error details; distinct from exit 4 (`QUEUE_JOB_TIMEOUT`) which signals the local kill ALSO failed and the remote may still be running",
        "Client: new `fetch_job_events(run_id, limit)` wraps Storage Events API; runId resolved from the job dict (Queue v2 jobs have runId == id)",
        "Error envelope: `KeboolaApiError` gained an optional `details: dict` payload; JSON output includes `error.details` only when non-empty so callers consume structured context without parsing the human message",
        "Refactor: `ErrorCode(StrEnum)` in errors.py -- 49 typed constants (46 original + 3 new: JOB_TIMEOUT_TERMINATED, INVALID_FLOW_DAG, SCHEDULE_DELETE_FAILED). Every `KeboolaApiError` / `formatter.error(...)` raise site migrated from string literals to `ErrorCode.<MEMBER>`. Wire format unchanged (StrEnum subclasses str)",
        "New: `docs/error-codes.md` -- reference for all ErrorCode members with semver policy (add=minor, rename/remove=major); `scripts/check_error_codes.py` CI guard (wired into `make check`) rejects new raw literals",
        "New: `kbagent sync init --adopt-existing` -- idempotently adopt a `.keboola/manifest.json` written by the kbc Go CLI without overwriting it; validates manifest `project_id` against the alias token and rejects mismatch with `CONFIG_ERROR` (exit 5); falls through to normal init when no manifest exists; safe to re-run (FIIA P2-2)",
    ],
    "0.21.2": [
        "Fix: `kbagent config search` now scans `rows[].configuration` in addition to the top-level configuration body (#196) -- queries like `--query '\"incremental\": false'` previously returned zero matches for row-based components (Snowflake/MySQL/BigQuery writers, DB extractors, Google Sheets) because the service only fetched `include=configuration`; match paths are now reported as `rows[N].configuration.parameters.<key>`",
        "Fix: `kbagent storage tables` now accepts zero-or-more `--project` flags and queries all connected projects in parallel (#198) -- matches the multi-project behaviour of `storage buckets`, `config list`, `job list`; JSON envelope now returns `{tables: [...], errors: [...]}` with per-row `project_alias`; `--branch` still requires exactly one `--project`",
        "Fix: storage READ commands (`buckets`, `bucket-detail`, `tables`, `table-detail`, `files`) no longer auto-scope to the implicit active dev branch set via `branch use` (#207) -- the Storage API branch endpoint only returns locally modified tables, so a fresh dev branch listed nothing; explicit `--branch ID` still overrides. Write/destructive commands remain branch-aware",
        "Fix: `kbagent lineage build` now supports the flat single-project sync layout (#208) -- previously `sync pull --project foo` followed by `lineage build` returned `0/0/0` because the scanner assumed the nested `<alias>/.keboola/` layout produced by `sync pull --all-projects`; lineage also emits a warning instead of silently returning an empty graph when zero projects are found",
        "Fix: `kbagent job run` rich-mode banner now reads `resolvedVariableValuesId` from the service response instead of echoing the raw `--variable-values-id` flag -- shows the auto-resolved row even when the flag was omitted",
        "Fix: `--variable-values-id` value is stripped of surrounding whitespace before reaching the service -- prevents a padded input from bypassing the empty-string guard",
        "Fix: `--hint client job run --branch ID` now threads `branch_id` through all three client calls (get_config_detail, list_config_rows, create_job) -- previously the branch arg was silently dropped, causing the hint to target production",
        "Chore: `.gitignore` whitelists `.env.example` and `.env.template` so documentation/scaffolding env templates can be tracked alongside the catch-all `.env.*` ignore rule",
        "Chore: `rich.markup.escape` import hoisted to module level in commands/job.py",
        "New: storage describe-bucket -- set KBC.description on a bucket via metadata POST (upsert-by-key, provider=user)",
        "New: storage describe-table -- set KBC.description on a table via metadata POST; description surfaces in table-detail",
        "New: storage describe-column -- set per-column descriptions using KBC.column.{name}.description convention in table metadata; readable via table-detail column_details[].description",
        "New: storage describe-batch --from-file -- apply bucket/table/column descriptions from a YAML file in one shot; failures collected, remaining items continue",
        "Fix: storage table-detail now returns 'description' and 'metadata' fields (extracted from table metadata array)",
        "Fix: storage bucket-detail now returns 'description' and 'metadata' fields (KBC.description in metadata takes precedence over native creation-time description field)",
        "New: Queue API polling parity with FIIA and the keboola-as-code Go CLI -- `kbagent job run --wait` now polls on an exponential curve (2s x 30 -> 5s x 48 -> 15s) instead of a fixed 1s interval. Preserves the legacy cadence behind `--poll-strategy fixed` for tests and very short jobs (FIIA P0-3).",
        "New: --log-tail-lines N on `job run` -- on FAILED / WARNING / TERMINATED jobs, kbagent fetches the last N Storage Events (via /v2/storage/events?runId=...) and surfaces them as `logTail` in --json output or `details.logTail` on errors. Default 200, max 5000, 0 disables.",
        "New: --timeout now auto-cancels the remote job -- when the local deadline expires under --wait, kbagent issues `kill_job` against the Queue and exits 7 (EXIT_JOB_TIMEOUT_TERMINATED) with the cancelled job + logTail in the error details. Distinct from exit 4 (QUEUE_JOB_TIMEOUT, retryable) which signals the local kill attempt ALSO failed and the remote may still be running.",
        "Client: new `fetch_job_events(run_id, limit)` wraps the Storage Events API -- runId is resolved from the job dict (Queue v2 jobs typically have runId == id). The Queue API has NO /jobs/{id}/events route despite the name; events live on Storage.",
        "Error envelope: KeboolaApiError gained an optional `details: dict` payload; JSON --mode output now includes `error.details` (only when non-empty) so callers can consume structured context without parsing the human message.",
        "New: ErrorCode enum (StrEnum) in errors.py -- all 46 error codes are now typed constants; "
        "every KeboolaApiError / formatter.error() raise site migrated from string literals to "
        "ErrorCode.<MEMBER>. Wire format is unchanged (str subtype). CI guard "
        "(scripts/check_error_codes.py, wired into 'make check') rejects new raw literals.",
        "New: docs/error-codes.md -- versioned reference for all ErrorCode members with "
        "add=minor / rename-remove=major semver policy.",
        "New: sync init --adopt-existing -- idempotently adopt a .keboola/manifest.json written "
        "by the kbc Go CLI (or an older kbagent version) without overwriting it. Validates "
        "manifest project_id against the alias token; rejects mismatch with ConfigError (exit 5). "
        "Falls through to normal init when no manifest exists. Safe to re-run.",
    ],
    "0.21.1": [
        "Fix: sync pull on a newly created dev branch now writes config rows (#193) -- idempotent skip guard for rows was missing a file-existence check, causing rows to be silently skipped when the branch directory was new (hash matched main because the branch is a clone)",
    ],
    "0.21.0": [
        "New: config variables-set / variables-get / variables-clear -- variables as a first-class attachment, not a resource to manage. Auto-creates the backing keboola.variables config + default row on first set, merges or replaces on update, encrypts #-prefixed values fail-closed, unlinks without deleting the backing config.",
        "New: sync push now deploys config rows (create/update/delete via /rows endpoints) -- previously rows edited locally were silently skipped (FIIA P0-1)",
        "New: #-prefixed secret values in row YAMLs are encrypted via the Encryption API before push, same fail-closed semantics as parent configs (FIIA P1-5)",
        "New: keboola.variables / keboola.shared-code row YAMLs hoist 'values' / 'code_content' to top level (matches kbc push convention) instead of hiding under _configuration_extra",
        "New: per-row 3-way diff -- sync status/diff now reports added/modified/deleted rows alongside parent configs; local row edits are preserved across pull",
        "New: ManifestConfigRow.metadata with pull_hash + pull_config_hash -- manifest schema bumped to v3 (v2 manifests load cleanly and upgrade in-place on next pull)",
        "Fix: _write_config_file now uses newline='' so Windows doesn't translate LF->CRLF on write, which previously caused every post-pull status to report every config as modified",
        "New: `kbagent job run` auto-resolves `variableValuesId` for configs with linked `keboola.variables` -- transformations now run against deployed values instead of empty `{{ placeholder }}` strings (FIIA runtime loop).",
        "New: `--variable-values-id ID` on `job run` to override the auto-resolved values row; `--no-variables` to skip resolution entirely (mutually exclusive).",
        "New: `NO_VARIABLE_ROWS` error code when a linked variables config has zero rows (fix via `kbagent config variables-set`); `MALFORMED_VARIABLES_ROW` when the Storage API returns a first row without a usable `id` -- fail loud instead of silently submitting with empty bindings.",
        'Reject: `--variable-values-id ""` (empty or whitespace) returns exit 2 / `INVALID_ARGUMENT` instead of silently dropping the Queue body field.',
        "Client: `create_job` gained `variable_values_id` parameter; omitted from body when unset so existing callers retain wire-level compatibility.",
        "Response: `kbagent --json job run` now carries `resolvedVariableValuesId` so callers can verify the binding without a second `job detail` round-trip.",
    ],
    "0.20.6": [
        "Fix: storage download-table / unload-table no longer OOM on multi-GB tables -- streamed downloads cap RAM at ~1 MiB regardless of table size (#187)",
        "Fix: _prepend_csv_header() no longer loads the full CSV into RAM (was the second OOM source after slice download)",
        "New: storage download-table --keep-slices -- save each slice as its own file under <output>/ (DuckDB/polars/Spark friendly), with a _columns.csv sidecar for the header",
        "New: storage unload-table --download --keep-slices -- same option for the file-export flow (CSV only; parquet has been sliced from day one)",
    ],
    "0.20.5": [
        "Docs: Parquet export covered in CLAUDE.md, skill commands-reference, storage-files-workflow, and gotchas (CONTRIBUTING.md compliance follow-up to 0.20.3)",
        "Test: new E2E case for 'unload-table --file-type parquet' (slice layout + _manifest.json + PAR1 magic bytes)",
    ],
    "0.20.4": [
        "Docs: 'kbagent context' now includes a worked Parquet export example for AI agents",
    ],
    "0.20.3": [
        "New: storage unload-table --file-type parquet -- export tables as Parquet (sliced)",
        "New: --download with parquet saves each slice as its own file + _manifest.json into a directory",
        "New: default parquet output path ./{project}/{table_id}.parquet/ (Hive-style, pyarrow-ready)",
        "New: storage file-download auto-detects sliced .parquet files and writes them per-slice",
        "New: client.download_sliced_file_to_dir() -- preserves slices instead of binary-concatenating (unsafe for parquet)",
    ],
    "0.20.2": [
        "New: job terminate -- kill Queue API jobs with --job-id or bulk --status filter (#181)",
        "New: --status any filter for terminating all killable jobs (created+waiting+processing)",
        "New: client helper kill_job + service terminate_jobs with partition response (killed/already_finished/not_found/failed)",
        "New: job.terminate permission (destructive class) for policy-based gating",
    ],
    "0.20.1": [
        "New: project description-get / description-set -- read/write the Keboola dashboard project description (markdown)",
        "New: branch metadata-list / metadata-get / metadata-set / metadata-delete -- generic CRUD over branch metadata (KBC.* keys)",
        "New: client helpers list/set/delete_branch_metadata + get_branch_metadata_value on KeboolaClient",
    ],
    "0.20.0": [
        "New: lineage build -- column-level lineage graph from sync'd data (SQL tokenizer + AI)",
        "New: lineage show -- query upstream/downstream with --columns, -c trace, --format mermaid/html/er",
        "New: lineage info -- inspect graph contents (projects, tables, top connections)",
        "New: lineage server -- interactive browser with mermaid/ER diagrams, click traversal",
        "New: sharing edges -- cross-project data flow edges (moved from old lineage show)",
        "New: 2-step AI flow -- --ai generates task file, AI agent processes, re-build applies",
        "New: storage delete-column --force for alias-referenced columns (#169)",
        "Fix: storage delete-column now waits for async job completion (#168)",
    ],
    "0.19.0": [
        "New: Kai (Keboola AI Assistant) -- kai ping, ask, chat, history (BETA) (#164)",
        "New: config rename -- rename via API + auto-rename local sync directory (#160)",
        "New: sync pull auto-rename -- detects remote name changes and renames local dirs (#160)",
        "New: sync push warning -- alerts when local dir names drift from config names (#160)",
        "New: storage delete-column -- remove columns from tables with --dry-run (#159)",
        "Fix: branch-scoped file operations (get_file_info, delete, tag, untag) (#161)",
        "Test: comprehensive E2E test suite covering all CLI commands (#158)",
    ],
    "0.18.6": [
        "New: config update --set PATH=VALUE -- set nested config keys without losing siblings (#156)",
        "New: config update --merge -- deep-merge partial JSON into existing configuration (#156)",
        "New: config update --dry-run -- preview changes before applying (#156)",
        "New: config update --configuration / --configuration-file -- update full config content (#156)",
        "Perf: 3-4x faster than MCP update_config (direct API, no subprocess overhead)",
    ],
    "0.18.5": [
        "New: --hint client|service flag -- generate Python code for any CLI command (#153)",
        "New: kbagent as Python SDK -- import KeboolaClient or service layer in your scripts",
        "New: 47 commands with hint support (config, storage, job, branch, workspace, sharing, tool...)",
        "Security: escape parameter values in generated code to prevent code injection (CWE-94)",
        "UX: commands without hints show clear 'no hint available' message",
        "Docs: programming-with-cli.md reference guide for SDK usage",
    ],
    "0.18.4": [
        "New: Storage Files commands -- files, file-detail, file-upload, file-download, file-tag, file-delete (#134)",
        "New: load-file -- import an uploaded Storage File into a table (#134)",
        "New: unload-table -- export a table to a Storage File with tags (#134)",
        "New: download by tag -- file-download --tag fetches latest matching file (#134)",
        "Fix: Azure sliced file download (azure:// URL handling in _CloudDownloader)",
        "UX: storage --help groups commands into Buckets/Tables/Files sections",
    ],
    "0.18.3": [
        "New: job run command with --row-id, --wait, --timeout (#135)",
    ],
    "0.18.2": [
        "New: storage download-table -- export table data to CSV (#130)",
        "New: storage table-detail -- show columns, types, primary key (#130)",
        "Fix: Azure upload uses absUploadParams with write-capable SAS (#131)",
        "Fix: AWS upload uses federation token with SigV4 signing (#131)",
        "Fix: sync status detects code file changes (transform.sql etc.) (#132)",
        "Fix: sync status no longer shows phantom configs after branch switch (#132)",
        "Fix: SQL parser preserves content between BLOCK and CODE markers (#132)",
    ],
    "0.18.1": [
        "Changelog command: kbagent changelog (#126)",
        "What's new display after auto-update",
    ],
    "0.18.0": [
        "Auto-update on startup (opt-out: KBAGENT_AUTO_UPDATE=false)",
        "Fix: sync pull dev-branch writes to correct directory (#121)",
        "Sync command is now stable (BETA removed)",
    ],
    "0.17.5": [
        "Fix: preserve multi-element script[] arrays in sync pull/push (#120)",
    ],
    "0.17.4": [
        "Encrypt command for Keboola Encryption API (#117)",
        "Fix: sync push no longer falls back to plaintext (#117)",
    ],
    "0.17.3": [
        "Branch support (--branch) for all storage commands (#114)",
    ],
    "0.17.2": [
        "Token refresh command: project refresh (#110)",
        "MCP server resolution fix (#109)",
    ],
    "0.17.1": [
        "Storage write operations: create-bucket, create-table, upload-table (#100)",
    ],
    "0.17.0": [
        "Permissions firewall for AI agent sandboxing",
        "Storage delete commands: delete-table, delete-bucket",
    ],
    "0.16.6": [
        "Snowflake gotchas and SQL migration guidance in plugin docs",
    ],
    "0.16.5": [
        "Fix: sync diff encrypted value false positives",
    ],
    "0.16.4": [
        "Fix: sync push config creation and update reliability",
    ],
    "0.16.3": [
        "Sync push: create, update, delete configs via API",
        "3-way diff engine for conflict detection",
    ],
    "0.16.2": [
        "Fix: sync status and diff edge cases",
    ],
    "0.16.1": [
        "Fix: sync pull row handling and manifest consistency",
    ],
    "0.16.0": [
        "Cross-project bucket sharing commands (#72)",
        "Self-update command: kbagent update (#73)",
    ],
    "0.15.5": [
        "Claude Code plugin with SKILL.md and reference docs",
    ],
    "0.15.4": [
        "Component scaffold: kbagent config new (#68)",
    ],
    "0.15.3": [
        "Fix: component list pagination",
    ],
    "0.15.2": [
        "Component discovery: component list, component detail",
    ],
    "0.15.1": [
        "Fix: retryable flag in error responses",
        "Deduplicate HTTP clients via BaseHttpClient",
    ],
    "0.15.0": [
        "Non-admin org setup via --project-ids",
    ],
    "0.14.0": [
        "Org setup: bulk onboarding via kbagent org setup",
    ],
    "0.13.1": [
        "Fix: workspace query error handling",
    ],
    "0.13.0": [
        "Workspace query: run SQL on Snowflake workspaces",
    ],
    "0.12.1": [
        "Fix: workspace create with read-only mode",
    ],
    "0.12.0": [
        "Workspace lifecycle: create, list, delete, load tables",
    ],
    "0.11.0": [
        "Branch lifecycle: create, use, reset, delete, merge",
    ],
    "0.10.0": [
        "MCP tool integration: tool list, tool call",
    ],
    "0.9.0": [
        "Cross-project data lineage: lineage show",
    ],
    "0.8.0": [
        "Job history: job list, job detail",
    ],
    "0.7.6": [
        "Fix: config search regex edge cases",
    ],
    "0.7.5": [
        "Fix: config detail output formatting",
    ],
    "0.7.4": [
        "Fix: multi-project parallel execution stability",
    ],
    "0.7.3": [
        "Fix: config list component type filtering",
    ],
    "0.7.2": [
        "Fix: project status connection timeout handling",
    ],
    "0.7.0": [
        "Config search with regex and multi-project support",
    ],
    "0.6.7": [
        "Fix: token masking for short tokens",
    ],
    "0.6.6": [
        "Fix: JSON output consistency across commands",
    ],
    "0.6.5": [
        "Fix: config list pagination for large projects",
    ],
    "0.6.0": [
        "Config browsing: config list, config detail",
    ],
    "0.5.0": [
        "Storage API: buckets, tables, bucket-detail",
    ],
    "0.4.1": [
        "Fix: project edit validation",
    ],
    "0.4.0": [
        "Project management: add, list, remove, edit, status",
    ],
}

# Number of versions shown by default in ``kbagent changelog``
DEFAULT_CHANGELOG_LIMIT = 5

# Environment variable set by auto_update before re-exec
ENV_UPDATED_FROM = "KBAGENT_UPDATED_FROM"


def get_changelog(limit: int = DEFAULT_CHANGELOG_LIMIT) -> dict[str, list[str]]:
    """Return the *limit* most recent changelog entries."""
    items = list(CHANGELOG.items())[:limit]
    return dict(items)


def get_version_notes(version: str) -> list[str] | None:
    """Return changelog entries for a specific version, or None."""
    return CHANGELOG.get(version)


# Abbreviations whose trailing period must NOT be read as a sentence end when
# extracting a headline (otherwise "e.g. a Chart" splits after "e.g").
_HEADLINE_ABBREVIATIONS = frozenset({"e.g", "i.e", "vs", "etc", "cf", "no", "al", "inc"})

# A sentence boundary is a period (or other terminator) followed by whitespace.
_SENTENCE_BOUNDARY = re.compile(r"[.!?]\s")

# The final alphabetic token (incl. internal dots) immediately before a period,
# used to test it against the abbreviation list -- "(e.g" -> "e.g".
_TRAILING_TOKEN = re.compile(r"[A-Za-z][A-Za-z.]*$")


def _truncate_headline(text: str, max_chars: int) -> str:
    """Cut *text* to at most *max_chars* on a word boundary, adding an ellipsis.

    A dangling unbalanced backtick (from cutting mid-code-span) is dropped so
    the renderer does not mistake the rest of the line for inline code.
    """
    text = text.strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:-")
    if cut.count("`") % 2:
        cut = cut.rsplit("`", 1)[0].rstrip()
    return f"{cut} …"


def headline(note: str, max_chars: int = CHANGELOG_HEADLINE_MAX_CHARS) -> str:
    """Return a one-line summary of a changelog *note*.

    The headline is the note's first sentence, capped at *max_chars*. Sentence
    detection skips periods inside version numbers (``0.57.0``) and common
    abbreviations (``e.g.``) so the summary is a complete thought, not a
    fragment.
    """
    first = note
    for match in _SENTENCE_BOUNDARY.finditer(note):
        dot = match.start()
        before = note[dot - 1] if dot > 0 else ""
        # Only a *period* after a digit is suspect (a version number like
        # 0.57.0); a digit before "!" or "?" -- e.g. "exit code 5!" -- is a
        # genuine sentence end and must not be skipped.
        if note[dot] == "." and before.isdigit():
            continue
        token_match = _TRAILING_TOKEN.search(note[:dot])
        token = token_match.group(0).rstrip(".").lower() if token_match else ""
        if token in _HEADLINE_ABBREVIATIONS:
            continue
        first = note[: dot + 1]
        break
    return _truncate_headline(first, max_chars)


def format_whats_new(old_version: str, new_version: str) -> str:
    """Format a brief 'What's new' message for display after auto-update.

    Shows a one-line headline per entry for the new version only (not
    intermediate versions); run ``kbagent changelog --full`` for the detail.
    """
    notes = get_version_notes(new_version)
    if not notes:
        return ""
    lines = [f"  What's new in v{new_version}:"]
    for note in notes:
        lines.append(f"    - {headline(note)}")
    return "\n".join(lines) + "\n"
