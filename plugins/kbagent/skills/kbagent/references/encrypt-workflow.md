# Encryption Workflow

Encrypt secret values before passing them to Keboola configs via MCP tools or API.

## Why you need this

MCP write tools (`update_config`, `create_config`) accept a complete config JSON
and forward it to the Storage API. They do NOT encrypt anything. If your payload
contains `#`-prefixed credentials, the ciphertext must be ready BEFORE the tool call.

`kbagent encrypt values` calls the Keboola Encryption API and returns ciphertext
that Keboola components can decrypt at runtime. The API is **one-way** -- there is
no decrypt endpoint.

## Encryption scope

All encryption uses **ComponentSecure** scope (project + component). This means:
- Encrypted values work in ANY config of that component within the project
- You can clone configs freely -- secrets still decrypt
- You can merge dev branches to main -- secrets still decrypt

Do NOT try to narrow the scope (e.g. per-config or per-branch) -- it breaks
config cloning and branch merging.

## Basic usage

```bash
# Encrypt a single secret
kbagent --json encrypt values \
  --project my-proj \
  --component-id keboola.ex-db-snowflake \
  --input '{"#password": "my-secret-password"}'

# Output: {"#password": "KBC::ProjectSecure::...encrypted..."}
```

## Pipe workflow (MCP tool calls)

The primary use case -- encrypt, then pass to a write tool:

```bash
# Step 1: Encrypt the secret
CIPHER=$(kbagent --json encrypt values \
  --project my-proj \
  --component-id keboola.python-transformation-v2 \
  --input '{"#api_token": "rotated-value"}' \
  | jq -r '.data["#api_token"]')

# Step 2: Use in a tool call
kbagent --json tool call update_config --project my-proj \
  --input "{\"component_id\": \"keboola.python-transformation-v2\", \"configuration_id\": \"123\", \"parameters\": {\"#api_token\": \"$CIPHER\"}}"
```

Or encrypt from a file:

```bash
# secrets.json: {"#db_password": "new-pass", "#api_key": "new-key"}
kbagent --json encrypt values \
  --project prod \
  --component-id keboola.ex-db-snowflake \
  --input @secrets.json \
  --output-file encrypted.json
```

## Input formats

| Format | Example | When to use |
|--------|---------|-------------|
| Inline JSON | `--input '{"#key": "val"}'` | Quick single-value encryption |
| File | `--input @secrets.json` | Multiple values, avoid shell escaping |
| Stdin | `--input -` | Pipe from another command, scripting |
| Output file | `--output-file enc.json` | Save encrypted values (0600 permissions) |

## Rules

1. **All keys must start with `#`** -- the Encryption API requires this
2. **All values must be strings** -- no nested objects, no numbers
3. **Already-encrypted values pass through** -- values starting with `KBC::` are returned unchanged (idempotent)
4. **One project at a time** -- each project has its own encryption key, no `--all-projects`

## Common errors

| Error | Cause | Fix |
|-------|-------|-----|
| Key 'password' must start with '#' | Missing `#` prefix | Use `#password` not `password` |
| Value for '#key' must be a string | Non-string value (int, dict, etc.) | Convert to string first |
| 403 / auth error | Token lacks encryption permissions | Check token scopes |
| Connection error to encryption API | Network issue or wrong stack URL | Run `kbagent doctor` |

## Sync push and encryption

`sync push` auto-encrypts `#`-prefixed plaintext values before pushing to Keboola.
If the Encryption API fails, push is **refused** (plaintext secrets are never sent).

Use `--allow-plaintext-on-encrypt-failure` only if you understand the risk:
```bash
# DANGEROUS: secrets may be stored as plaintext in Keboola
kbagent sync push --project my-proj --allow-plaintext-on-encrypt-failure
```
