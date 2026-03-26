# Refactoring Plan

Code quality review identified 8 improvements, prioritized by impact.

## Priority 1: Fix missing `retryable` flag (BUG)

**File:** `src/keboola_agent_cli/commands/config.py` (lines 272, 336)

`config_update` and `config_delete` error handlers omit `retryable=exc.retryable`.
In JSON mode, network errors (timeout, 503) are always reported as `retryable: false`,
causing agents to not retry recoverable failures.

**Fix:** Add `retryable=exc.retryable` to both `formatter.error()` calls.

## Priority 2: Deduplicate URL derivation

**File:** `src/keboola_agent_cli/client.py` (lines 62-102)

Four nearly identical `_*_base_url` properties copy-paste the same `hostname.replace("connection.", "X.", 1)` pattern. `ai_client.py` already has a proper `_derive_ai_url()` static method.

**Fix:** Add `_derive_service_url(stack_url, prefix)` to `BaseHttpClient` in `http_base.py`.
Replace all four properties with one-liner calls.

## Priority 3: Deduplicate lazy client init

**File:** `src/keboola_agent_cli/client.py` (lines 124-170)

`_queue_request`, `_query_request`, `_encrypt_request` are structurally identical 15-line
methods. They differ only in the URL attribute and headers.

**Fix:** Extract `_get_or_create_sub_client(attr, base_url, headers)` helper method.

## Priority 4: Fix architectural dependency inversion

**Files:** `src/keboola_agent_cli/output.py` (line 83), `src/keboola_agent_cli/commands/_helpers.py` (lines 27-40)

`output.py` (foundation layer) imports from `commands._helpers` (higher layer) via a deferred
import. This inverts the dependency graph and creates circular dependency risk.

**Fix:** Move `_ERROR_CODE_TO_TYPE` dict and `map_error_code_to_type()` function from
`commands/_helpers.py` to `errors.py`. Update imports in `output.py` and `_helpers.py`.

## Priority 5: Replace tuple-length protocol in `_run_parallel` (future)

**File:** `src/keboola_agent_cli/services/base.py` (line 145)

`if len(result) == 2` convention to distinguish errors from successes is fragile.
Replace with `WorkerSuccess` / `WorkerError` dataclasses and structural pattern matching.

## Priority 6: Remove duplicated constants (future)

**File:** `src/keboola_agent_cli/commands/sync.py` (lines 17-18)

`KEBOOLA_DIR` and `MANIFEST_FILE` are redefined instead of importing from `constants.py`.

## Priority 7: Deduplicate MCP auto-expand logic (future)

**File:** `src/keboola_agent_cli/services/mcp_service.py`

`_http_auto_expand` and `_connect_and_auto_expand` share ~35 lines of identical logic.

## Priority 8: ConfigStore read caching (future, nice-to-have)

**File:** `src/keboola_agent_cli/config_store.py`

Each operation reads the file from disk. A single `edit_project` call triggers 3 reads + 1 write.
