# Sync Rows Workflow -- Row-level GitOps internals

`sync push` deploys config rows end-to-end starting from v0.21.0. This document
is the technical reference for agents and developers who need to understand
how row tracking, hashing, and encryption work beyond the TL;DR in
[`sync-workflow.md`](./sync-workflow.md).

If you just want to assign variable values without authoring YAML, use
`kbagent config variables-{set,get,clear}` -- see
[`variables-workflow.md`](./variables-workflow.md).

## When to use rows vs variables-set

Both code paths converge on the same API calls. Pick based on source-of-truth:

| Scenario | Prefer | Why |
|---|---|---|
| Config is git-tracked, PRs review changes | `sync push` | YAML diff is the review artifact |
| Agent-driven "set this key on that config" | `variables-set` | One CLI call, no filesystem |
| Shared `keboola.variables` across many configs | `variables-set --variables-id <shared>` | No accidental duplication |
| Bulk changes to `keboola.shared-code` snippets | `sync push` | Variables commands don't touch shared-code |
| Bootstrapping a new project | `sync push` | Pull once, diff, push -- rows land in manifest |

`keboola.variables` and `keboola.shared-code` are the only components that
get row-hoisted payloads (see `ROW_HOIST_COMPONENTS`). All other row-bearing
components (transformations, orchestrators, etc.) round-trip via the standard
`parameters` / `storage` / `processors` shape.

## Manifest v3

Pull-time state is recorded in `.keboola/manifest.json`:

```json
{
  "version": 3,
  "configurations": [
    {
      "componentId": "keboola.snowflake-transformation",
      "id": "15815157",
      "path": "main/transformation/snowflake/my-transform",
      "metadata": {"pull_hash": "...", "pull_config_hash": "..."},
      "rows": [
        {
          "id": "789123",
          "path": "rows/main",
          "metadata": {"pull_hash": "...", "pull_config_hash": "..."}
        }
      ]
    }
  ]
}
```

- **`version: 3`** -- bumped from v2 when rows landed. Older manifests load
  via `extra="allow"` (backward compatible); row `metadata` is seeded on the
  next pull.
- **`rows[].metadata.pull_hash`** -- SHA of the raw `_config.yml` bytes at
  pull time. Used to detect **local edits** (current file hash != stored).
- **`rows[].metadata.pull_config_hash`** -- stable hash of the semantic
  `{name, description, configuration}` shape. Used to detect **remote drift**
  without raising false positives on cosmetic changes (comment removal,
  key reordering, encrypted-value nonce changes).

The dual-hash design mirrors parent-config metadata. One hash owns
"did local change?", the other owns "did remote change?". Together they
produce the 3-way diff matrix without needing a separate `base/` snapshot
directory.

### Hash invariants

- A fresh pull writes both hashes; the next `sync diff` shows zero changes.
- Editing the YAML but not the semantic payload (e.g. fixing indentation)
  flips `pull_hash` but leaves `pull_config_hash` equal -- diff reports the
  file as unchanged.
- Re-encrypting the same secret produces different ciphertext (fresh nonce).
  `config_hash` strips `KBC::ProjectSecure::` prefixes before hashing, so
  this is a no-op for diff purposes.
- Windows `\r\n` line endings: `_write_config_file` opens files with
  `newline=""` to pin LF output. Mixing shells on Windows won't churn hashes.

## Row-hoisted components

For components in `ROW_HOIST_COMPONENTS = {"keboola.variables", "keboola.shared-code"}`,
the API's `configuration` object is atypical:

```python
# keboola.variables row API shape:
{"configuration": {"values": [{"name": "region", "value": "eu", "type": "string"}]}}

# keboola.shared-code row API shape:
{"configuration": {"code_content": ["SELECT * FROM t WHERE region = {{ region }}"]}}
```

Neither fits the standard `parameters` / `storage` / `processors` promotion.
So the local YAML hoists these top-level keys:

```yaml
# rows/main/_config.yml for keboola.variables
version: 2
name: main
description: ""
values:
  - {name: region, value: eu, type: string}
_keboola:
  component_id: keboola.variables
  row_id: "789123"
```

On push, `local_row_to_api` inverts the hoist: reserved keys (`version`, `name`,
`description`, `_keboola`, etc.) stay local, everything else is pulled into
`configuration`. The byte-for-byte round-trip invariant is locked down by
`TestVariablesRowRoundTrip` (5 tests).

**If you add another row-bearing component with a weird shape**, add it to
`ROW_HOIST_COMPONENTS` in `src/keboola_agent_cli/sync/config_format.py`. The
round-trip hoist works automatically; whether the encryption walker covers
the secret shape depends on the payload (see next section).

## Secret encryption in rows

Parent configs put secrets under `#`-prefixed **dict keys**:

```yaml
# Parent config
parameters:
  "#api_token": raw-plaintext
```

**`keboola.variables` rows** store secrets as `{name, value}` list elements
where the `#` lives in the **`name` field**, not as a dict key:

```yaml
# keboola.variables row
values:
  - {name: "#api_key", value: raw-plaintext}
```

**`keboola.shared-code` rows do NOT carry secrets.** The payload is
`code_content: [string, string, ...]`, a list of SQL/Python snippets. The
encryption walker simply never fires for shared-code rows, which is correct
and intentional. If you're debugging encryption and expect a shared-code
row to encrypt something, you're confused about the shape.

Both shapes that DO carry secrets (parent-config dict keys, and
`keboola.variables` `{name, value}` pairs) encrypt to
`KBC::ProjectSecure::...` before hitting Storage.

The encryption walker in `services/_encryption.py` detects the
`{name, value}` shape via `_is_secret_name_value_pair()`:

```python
def _is_secret_name_value_pair(item):
    return (
        isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and is_secret_key(item["name"])
        and isinstance(item.get("value"), str)
    )
```

`collect_secrets`, `apply_encrypted`, and `apply_encrypted_to_local` all
detect this shape. The emitted encryption key for such entries is
`#<parent_path>[<index>].<name>` (e.g. `#values.[0].#api_key`), which the
Encryption API accepts like any other flat secret map.

**Fail-closed**: on encryption API failure, push aborts with `ENCRYPTION_FAILED`
and exit non-zero. Plaintext never lands on Storage. `--allow-plaintext-on-encrypt-failure`
exists as a bootstrap escape hatch; **do not use in production**.

## Untracked row detection

You can drop a hand-crafted row directory under a tracked config, and push
will POST it:

```bash
# Structure after pull:
main/transformation/snowflake/my-transform/
├── _config.yml
└── rows/
    ├── main/        # tracked (in manifest)
    │   └── _config.yml
    └── new-row/     # NEW -- not in manifest
        └── _config.yml

# Push picks it up:
kbagent sync push --project prod
# → POST /components/keboola.snowflake-transformation/configs/15815157/rows
# → new row gets an API-assigned id, written back into the manifest
```

`_find_untracked_rows` in `services/sync_service.py` (paralleling
`_find_untracked_configs`) walks each tracked config's `rows/` dir, finds
subdirs with a `_config.yml` that aren't in `manifest.configurations[].rows`,
and surfaces them as `diff` state `"added"`. Push routes them through
`_push_create_row`, which:

1. Loads the YAML, runs `local_row_to_api` to split out `name` /
   `description` / `configuration`.
2. Encrypts `#`-prefixed values (both parent-config and row-hoisted shapes).
3. POSTs to `/components/{component_id}/configs/{config_id}/rows`.
4. Writes the API-assigned row id back into the manifest along with fresh
   `pull_hash` + `pull_config_hash`.
5. Writes encrypted ciphertext back into the local YAML so the next diff
   sees `local == remote`.

The `row_id` landing in `_keboola` in the local YAML matches the API response;
no collision with existing rows because the API assigns it. Test coverage:
`test_push_untracked_row_dir_calls_create_config_row`.

## 3-way diff at the row level

Row diff is the same 3-way engine as parent configs, just keyed by row id:

| `pull_hash == current_local_hash`? | `pull_config_hash == remote_config_hash`? | Diff state |
|---|---|---|
| yes | yes | `unchanged` |
| no | yes | `modified` (push will update) |
| yes | no | `remote_modified` (pull to refresh) |
| no | no | `conflict` (manual resolve) |

Added rows (filesystem-only) show as `added`; removed rows (manifest-only
after file deletion) show as `deleted` -- push DELETEs them via
`_push_delete_row`.

Human-mode diff output prints row-level changes with the same `+`/`~`/`-`/`=`
prefixes as parent configs, e.g.:

```
keboola.snowflake-transformation/15815157
  ~ rows/main: values[0].value changed: '2024' -> '2025'
  + rows/new-row: added (will POST on push)
```

## Tests you should know about

Every row-level behavior has a named test; if you're extending this area,
add/update alongside:

| Behavior | Test |
|---|---|
| Row create/update/delete client methods | `tests/test_client.py::TestConfigRowMethods` (9 tests) |
| Sync push row create + hash bookkeeping | `tests/test_sync_service.py::TestPushRows` (5 tests) |
| Encryption fail-closed on push | `TestPushRows::test_push_encryption_failure_aborts_fail_closed` |
| Row-hoisted secret encryption (regression) | `TestPushRows::test_push_update_row_encrypts_variables_row` |
| Untracked row directory detection | `test_push_untracked_row_dir_calls_create_config_row` |
| YAML round-trip for hoisted payloads | `TestVariablesRowRoundTrip` (5 tests) |
| Manifest v2 -> v3 backward-compat load | `tests/test_sync_manifest.py` |
| CLI-level push rows (human + JSON) | `tests/test_sync_cli.py::TestSyncPushCli` |
| Encryption walker row-hoist unit tests | `tests/test_encryption.py` |
| E2E round-trip vs live Keboola project | `test_e2e.py::TestE2ESyncWorkflow::test_sync_push_variable_row_round_trip` |

## Response shapes (for `--json` agents)

### `sync push` with row changes

```json
{
  "status": "ok",
  "data": {
    "project_alias": "prod",
    "branch_id": null,
    "pushed": [
      {
        "component_id": "keboola.variables",
        "config_id": "15815157",
        "action": "updated"
      }
    ],
    "pushed_rows": [
      {
        "component_id": "keboola.variables",
        "parent_config_id": "15815157",
        "row_id": "789123",
        "path": "rows/main",
        "action": "updated"
      },
      {
        "component_id": "keboola.variables",
        "parent_config_id": "15815157",
        "row_id": "789456",
        "path": "rows/new-row",
        "action": "created"
      }
    ],
    "skipped": [],
    "errors": []
  }
}
```

`action` is one of `created`, `updated`, `deleted`. On `ENCRYPTION_FAILED` or
API errors, the offending row lands in `errors[]` with the same shape and
an `error_code` / `message` pair; successful siblings still make it to
`pushed_rows[]` because push processes rows independently.

## Related reading

- [`sync-workflow.md`](./sync-workflow.md) -- general `sync pull` / `sync push`
  flow, branch mapping, 3-way diff for parent configs.
- [`variables-workflow.md`](./variables-workflow.md) -- ergonomic alternative
  to row YAML editing.
- [`gotchas.md`](./gotchas.md) -- quick-reference footguns, including the
  row-level encryption caveat.
