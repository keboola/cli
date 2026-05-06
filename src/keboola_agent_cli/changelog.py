"""Changelog data for kbagent releases.

Maintained manually: one-line summaries per version.
Run ``make changelog`` to scaffold new entries from GitHub releases.
"""

from __future__ import annotations

# Ordered newest-first.  Each value is a list of brief one-line descriptions.
CHANGELOG: dict[str, list[str]] = {
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
