# Config Metadata Workflow -- Tags, folders, and agent breadcrumbs

Keboola stores free-form `key / value / provider` metadata on every configuration.
The Keboola UI uses a handful of `KBC.*` keys for user-facing behavior, but the
surface is open-ended: agents can stamp their own keys to leave breadcrumbs for
later runs (e.g. `agent.owner`, `agent.lastAudit`, `agent.domain`).

The UI folder is **not** metadata any more: it is the configuration's own
`folder` field (since vNEXT, DMD-2348), set with `config set-folder`. Do not
write the old `KBC.configuration.folderName` key with `set-metadata`.

kbagent exposes the metadata CRUD surface plus the folder command on `kbagent config`:

```
metadata-list    -- list all entries (id, key, value, provider, timestamp)
get-metadata     -- read one value by key
set-metadata     -- upsert a single key/value
delete-metadata  -- remove an entry by its numeric id
set-folder       -- sets the configuration's folder field (not metadata)
```

All five are branch-aware; omit `--branch` to use the project's active branch.

## When to use this

- **Folder organization**: group related configs under a named folder in the
  Keboola UI (`set-folder`). Works across all component types; no schema change
  needed, no configuration version created.
- **Agent breadcrumbs**: tag configs an agent has touched so later runs can
  skip, re-audit, or attribute them (`set-metadata --key agent.* ...`).
- **Ownership / governance tags**: stamp `owner`, `domain`, `cost-center`,
  etc. and filter via `config list --json | jq` downstream.
- **Provenance tracking**: record when a config was last generated or
  refactored by an automated workflow.

## CLI cheatsheet

```bash
# List everything on a config (sorted by key)
kbagent --json config metadata-list --project prod \
  --component-id keboola.snowflake-transformation --config-id 15815157

# Read a specific key (exits 1 / NOT_FOUND if absent)
kbagent --json config get-metadata --project prod \
  --component-id keboola.snowflake-transformation --config-id 15815157 \
  --key agent.owner

# Upsert (create if new, overwrite if existing)
kbagent config set-metadata --project prod \
  --component-id keboola.snowflake-transformation --config-id 15815157 \
  --key agent.owner --value analytics-team

# Delete by numeric ID (from metadata-list)
kbagent config delete-metadata --project prod \
  --component-id keboola.snowflake-transformation --config-id 15815157 \
  --metadata-id 4281 --yes

# Folder (sets the configuration's folder field)
kbagent config set-folder --project prod \
  --component-id keboola.snowflake-transformation --config-id 15815157 \
  --name "Customer 360"

# Clear the folder assignment (empty string)
kbagent config set-folder --project prod \
  --component-id keboola.snowflake-transformation --config-id 15815157 \
  --name ""
```

## Folder organization pattern

The Keboola UI reads the configuration's `folder` field and groups configs
sharing the same value into a named folder under the component. This is purely
a presentation feature -- the configuration body is unchanged, no version is
created, and there are no parent/child resources to manage. That makes it ideal for programmatic
taxonomy:

```bash
# Tag every config in an onboarding flow with one folder
for cfg_id in 15815157 15815158 15815159 15815160; do
  kbagent config set-folder --project prod \
    --component-id keboola.snowflake-transformation \
    --config-id "$cfg_id" \
    --name "Customer 360 - Onboarding"
done
```

Guidelines:

- `set-folder --name ""` removes the grouping (the API stores an empty or
  whitespace-only name as "no folder", `folder: null`).
- Folder names are free-form strings, trimmed, case-sensitive, at most 400
  characters (longer is an HTTP 400); keep them short and stable -- the UI
  sorts alphabetically.
- Use `set-folder`, never `set-metadata --key KBC.configuration.folderName`:
  the folder is a configuration field, and no first-party client writes the
  key any more (the metadata API keeps accepting it server-side, the column
  mirrors it).
- On a stack without the field (before keboola/connection DMD-2347),
  `set-folder` fails with `FEATURE_NOT_ENABLED` (exit 5) and changes nothing.
- `config list` shows each config's `folder`; `config detail` returns it as
  the `folder` field (`null` = no folder).
- Folders are **per component**. Two configs under different components with
  the same folder name render as two separate folders in the UI -- this is
  intentional, not a bug.

## Full lifecycle example

```bash
PROJECT=prod
COMPONENT=keboola.snowflake-transformation
CONFIG=15815157

# 1. Inspect what's already on the config
kbagent --json config metadata-list \
  --project "$PROJECT" --component-id "$COMPONENT" --config-id "$CONFIG"

# 2. Stamp an agent breadcrumb
kbagent config set-metadata \
  --project "$PROJECT" --component-id "$COMPONENT" --config-id "$CONFIG" \
  --key agent.lastAudit --value "2026-04-23"

# 3. Read it back
kbagent --json config get-metadata \
  --project "$PROJECT" --component-id "$COMPONENT" --config-id "$CONFIG" \
  --key agent.lastAudit

# 4. File it into the Customer 360 folder
kbagent config set-folder \
  --project "$PROJECT" --component-id "$COMPONENT" --config-id "$CONFIG" \
  --name "Customer 360"

# 5. Later: clean the breadcrumb (list to find its numeric id, then delete)
METADATA_ID=$(kbagent --json config metadata-list \
  --project "$PROJECT" --component-id "$COMPONENT" --config-id "$CONFIG" \
  | jq -r '.data.metadata[] | select(.key=="agent.lastAudit") | .id')

kbagent config delete-metadata \
  --project "$PROJECT" --component-id "$COMPONENT" --config-id "$CONFIG" \
  --metadata-id "$METADATA_ID" --yes
```

## Response shapes (`--json` mode)

### `metadata-list`
```json
{
  "status": "ok",
  "data": {
    "project_alias": "prod",
    "component_id": "keboola.snowflake-transformation",
    "config_id": "15815157",
    "branch_id": 12345,
    "metadata": [
      {"id": "4281", "key": "agent.owner", "value": "analytics-team", "provider": "user", "timestamp": "2026-04-23T10:15:00Z"},
      {"id": "4282", "key": "agent.lastAudit", "value": "2026-04-23", "provider": "user", "timestamp": "2026-04-23T10:16:02Z"}
    ]
  }
}
```

The `metadata` list is key-sorted for deterministic output.

### `get-metadata`
```json
{
  "status": "ok",
  "data": {
    "project_alias": "prod",
    "component_id": "keboola.snowflake-transformation",
    "config_id": "15815157",
    "branch_id": 12345,
    "key": "agent.lastAudit",
    "value": "2026-04-23",
    "metadata_id": "4282"
  }
}
```

Returns `NOT_FOUND` (exit 1) when the key is absent -- there is no sentinel
"empty" value; missing means missing.

### `set-metadata` / `set-folder` / `delete-metadata`
Each returns `project_alias`, `component_id`, `config_id`, `branch_id`, and a
human-readable `message`. `set-folder` additionally returns `folder` (the
folder as stored, `""` when cleared) so callers don't have to re-parse the
message string. It also keeps `key` (frozen at `"KBC.configuration.folderName"`),
`value` (same as `folder`) and `result` (`{id, name, folder}` from the config
PUT) from the time it wrote the metadata key.

## Provider semantics

The `provider` field on each metadata entry distinguishes `user` (anything you
wrote via CLI/API/UI) from `system` (set by the Keboola platform itself).
kbagent never filters these out -- `metadata-list` surfaces both. Do not
attempt to `set-metadata` on a system-provider key; the API will return a
validation error and `delete-metadata` on a system entry is likewise rejected.

## Relation to `config update`

`config update` mutates the configuration body (`parameters`, `storage`,
`processors`, etc.). `config set-metadata` mutates the sibling `metadata`
array on the same configuration resource. They touch different endpoints and
never conflict. Use `config update` for anything that affects runtime
behavior; use `set-metadata` for tags and audit breadcrumbs, and
`set-folder` for folders.

## Branch awareness

All five commands resolve `--branch` the same way the rest of `kbagent config`
does:

1. Explicit `--branch ID` wins.
2. Otherwise the project's **active branch** (set by `kbagent branch use` /
   `branch create`) is used.
3. If the project has no active branch, the main / default branch is used.

Metadata and the folder in a dev branch are independent of production; a merge
request carries them back to production with the rest of the branch. See [branch-workflow.md](branch-workflow.md).
