# Variables Workflow -- First-class attachment, not a separate resource

Variables in Keboola are stored server-side as `keboola.variables` configurations
with rows. **That's an implementation detail.** From an agent or user perspective,
variables are just a flat KEY=VALUE dict you assign to a config. kbagent hides
the config-as-resource / row-link / schema-sync plumbing behind three commands.

## TL;DR

```bash
# Assign (auto-creates the backing keboola.variables config on first call).
kbagent config variables-set --project prod \
  --component-id keboola.snowflake-transformation --config-id 15815157 \
  --var year_start=2016 --var region=eu

# Read
kbagent --json config variables-get --project prod \
  --component-id keboola.snowflake-transformation --config-id 15815157

# Change a single value (merges; other keys preserved)
kbagent config variables-set --project prod \
  --component-id keboola.snowflake-transformation --config-id 15815157 \
  --var region=us-west

# Replace ALL values (drops keys not in --var)
kbagent config variables-set --project prod \
  --component-id keboola.snowflake-transformation --config-id 15815157 \
  --var only_key=only_value --replace

# Detach (does NOT delete the backing keboola.variables config)
kbagent config variables-clear --project prod \
  --component-id keboola.snowflake-transformation --config-id 15815157 --yes
```

## What the CLI does behind the scenes

### First `variables-set` on an unlinked parent (auto-create)

1. GET the parent config → no `variables_id` set.
2. POST `/components/keboola.variables/configs` → create sibling named
   `<parent-name>-vars` with schema derived from the `--var` keys.
3. POST `/components/keboola.variables/configs/{id}/rows` → default row named
   `default` with the values payload (encrypted where `#`-prefixed).
4. PUT parent config → add `variables_id` + `variables_values_id`.

Result: one CLI call, four API calls, hidden from the caller.

### Subsequent `variables-set` (update existing)

1. GET parent → `variables_id` + `variables_values_id` already set.
2. GET variables config → resolve the target row.
3. Merge (default) or replace (`--replace`) the values.
4. Encrypt any new `#`-prefixed values via the Encryption API.
5. PUT the row.
6. If new keys were introduced, extend the variables config's schema
   (cosmetic; UI-only, non-fatal if it fails).

### `variables-clear`

Strips both `variables_id` and `variables_values_id` from the parent config.
**Does not delete the backing `keboola.variables` config** -- it may be shared
with other configs, and deletion is a distinct destructive op. Use
`kbagent config delete` explicitly to remove the backing config after verifying
nothing else references it.

## Secrets

Prefix the key with `#` to mark a value as a secret:

```bash
kbagent config variables-set --project prod \
  --component-id keboola.python-transformation-v2 --config-id 42 \
  --var '#api_token=raw-plaintext-here'
```

Behavior:

- kbagent sends the plaintext to the Encryption API, receives `KBC::ComponentSecure::...`
  back, and writes only the ciphertext into the row's `values` array.
- **Fail-closed**: if the Encryption API is unreachable, the command aborts
  with `ENCRYPTION_FAILED` (exit non-zero). The plaintext never lands on Storage.
- Escape hatch: `--allow-plaintext-on-encrypt-failure` falls back to plaintext
  (**not recommended**; only for bootstrap/debugging scenarios).

## Attaching to an existing variables config

By default, `variables-set` auto-creates a sibling config. To attach the parent
to an **existing** `keboola.variables` config (e.g. shared across multiple
transformations in a pipeline):

```bash
kbagent config variables-set --project prod \
  --component-id keboola.snowflake-transformation --config-id 15815157 \
  --variables-id 01kpn7sak9kwkn61h7j1y4pz3z \
  --var region=eu
```

Optionally pin a specific values row with `--values-id ROW_ID` (defaults to the
first row, which is the Keboola convention for the "default" row).

## Dry-run preview

`--dry-run` shows what would change without touching the server. The output is
a diff of current vs. proposed values:

```bash
kbagent config variables-set --project prod \
  --component-id keboola.snowflake-transformation --config-id 15815157 \
  --var region=us-west --dry-run
```

Human mode prints `+ new_key`, `~ changed_key: old -> new`, `- dropped_key`,
`= unchanged_key`. JSON mode returns `{"dry_run": true, "current_values": ...,
"would_write": ..., "action": "would_create"|"would_update"}`.

## Response shapes (for `--json` agents)

### `variables-set`
```json
{
  "status": "ok",
  "data": {
    "project_alias": "prod",
    "parent_component_id": "keboola.snowflake-transformation",
    "parent_config_id": "15815157",
    "variables_id": "01kpn7sak9kwkn61h7j1y4pz3z",
    "values_id": "01kpn7sat48jmhvx20svaqdnf9",
    "action": "created",
    "values": {"year_start": "2016", "region": "eu"},
    "encrypted_keys": ["#api_token"]
  }
}
```

### `variables-get`
```json
{
  "status": "ok",
  "data": {
    "project_alias": "prod",
    "parent_component_id": "keboola.snowflake-transformation",
    "parent_config_id": "15815157",
    "variables_id": "01kpn7sak9kwkn61h7j1y4pz3z",
    "values_id": "01kpn7sat48jmhvx20svaqdnf9",
    "values": {"year_start": "2016", "region": "eu"},
    "linked": true
  }
}
```

`linked=false` means the parent has no `variables_id` -- the other fields will
be `null` and `values` will be `{}`.

### `variables-clear`
```json
{
  "status": "ok",
  "data": {
    "project_alias": "prod",
    "parent_component_id": "keboola.snowflake-transformation",
    "parent_config_id": "15815157",
    "was_linked": true,
    "unlinked_variables_id": "01kpn7sak9kwkn61h7j1y4pz3z",
    "unlinked_values_id": "01kpn7sat48jmhvx20svaqdnf9"
  }
}
```

## Relation to `sync push`

`sync push` deploys `keboola.variables` configs + rows as regular configs via
the GitOps flow (edit YAML -> push). `variables-{set,get,clear}` is the
ergonomic alternative for when you don't want to author YAML: one-line
deployments, no manifest bookkeeping.

Both are supported; pick whichever fits your workflow:

| Use case | Prefer |
|---|---|
| Git-tracked config as source of truth | `sync push` |
| Quick "set a value on this transformation" | `variables-set` |
| Same variable definitions across many transformations | `variables-set --variables-id <shared>` |
| Agent-driven pipelines with programmatic var changes | `variables-set` |

They converge on the same API calls; `sync push` just routes through local YAML
first.
