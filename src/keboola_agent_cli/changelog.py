"""Changelog data for kbagent releases.

Maintained manually: one-line summaries per version.
Run ``make changelog`` to scaffold new entries from GitHub releases.
"""

from __future__ import annotations

# Ordered newest-first.  Each value is a list of brief one-line descriptions.
CHANGELOG: dict[str, list[str]] = {
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


def format_whats_new(old_version: str, new_version: str) -> str:
    """Format a brief 'What's new' message for display after auto-update.

    Shows entries for the new version only (not intermediate versions).
    """
    notes = get_version_notes(new_version)
    if not notes:
        return ""
    lines = [f"  What's new in v{new_version}:"]
    for note in notes:
        lines.append(f"    - {note}")
    return "\n".join(lines) + "\n"
