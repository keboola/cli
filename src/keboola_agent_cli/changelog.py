"""Changelog data for kbagent releases.

Maintained manually: one-line summaries per version.
Run ``make changelog`` to scaffold new entries from GitHub releases.
"""

from __future__ import annotations

# Ordered newest-first.  Each value is a list of brief one-line descriptions.
CHANGELOG: dict[str, list[str]] = {
    "0.26.0": [
        "New: `kbagent config set-default-bucket --bucket BUCKET_ID | --clear [--dry-run] [--branch ID]` -- discoverable wrapper around the raw-mode `storage.output.default_bucket` workaround documented at https://keboola.atlassian.net/wiki/spaces/SUP/pages/3770155030/ (epic KBCP-108). Read-modify-write that preserves all sibling keys under `storage.output` and the rest of the configuration. Same-value writes short-circuit with `{\"changed\": false}` (no API call, no version bump). `--clear` removes only the `default_bucket` key, leaving an empty `storage.output: {}` if no other siblings live there (intentional -- mirrors `set_nested_value`'s parent-creation semantics; Storage API treats `output: {}` and missing `output` identically as 'use the auto-derived bucket'). Live-validated end-to-end on three component types -- row-based GCS extractor, root-only `keboola.ex-cnb-exchange-rates`, and `ex-generic-v2` with multiple jobs -- output tables routed to the configured bucket at job runtime in every case. The per-table `destination` override (the second method shown in the support article) keeps using the existing `kbagent config update --set 'storage.output.tables=[...]'` -- no new wrapper there because per-table mappings have many fields that don't fit a single-purpose flag.",
        "Fix: `kbagent sync pull --with-samples` no longer crashes with `TypeError: '>' not supported between instances of 'NoneType' and 'int'` when one or more tables in the project return `rowsCount: null` from the Storage API (typical for newly-created or empty tables on some backends, reproduced live against `kosik-sales`). `dict.get(\"rowsCount\", 0)` returns the default `0` only when the key is **missing** -- if the key is present with a `null` value, `.get()` returns `None`, and the `> 0` comparison crashed Python 3 before any sample was fetched. The filter and sort key in `SyncService._fetch_samples()` now coerce `None` to `0` via a small `_rows()` helper used in both places (`t.get(\"rowsCount\") or 0`), so empty/null-rowcount tables are gracefully skipped exactly like `rowsCount: 0` ones. Closes #233.",
        "Tests: 2 new regression tests in `tests/test_sync_storage_jobs.py::TestFetchSamples` -- `test_tables_with_none_rows_count_skipped` (covers the filter path: `rowsCount: None` and missing-key tables are excluded from sampling alongside `rowsCount: 0`) and `test_mixed_none_and_numeric_rows_count_sorts_correctly` (covers the sort path: even after filtering, the sort key must not crash if any `None` slips through). Both tests fail on 0.25.3 with the exact `TypeError` from the issue traceback and pass after the fix.",
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
