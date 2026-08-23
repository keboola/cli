# Creating New Configurations (Scaffold Workflow)

> **Two modes, one command (since v0.33.0):**
> - `kbagent config new --output-dir DIR` (this workflow) -- generate
>   scaffold files to disk, edit them, then push later with
>   `kbagent sync push`. The "GitOps for configs" path.
> - `kbagent config new --push --no-files --project P --name N` -- one-shot
>   remote create via Storage API, no filesystem step. The FIIA "empty
>   shell, then patch via `config update --set ...`" path. See
>   `gotchas.md` "`kbagent config new --push` is one-shot remote create"
>   for the full side-by-side and the schema-validation behavior.

## When to use

- User wants to create a new extractor, writer, transformation, or application
- User asks "how do I set up a new Snowflake extractor" or similar
- User wants to scaffold config files for a component (default mode), OR
  wants a single CLI call that posts to the Storage API and returns the new
  config ID (use `--push --no-files`)

## Step-by-step workflow

### 1. Find the component

```bash
kbagent --json component list --project ALIAS --query "description of what they need"
```

Returns ranked suggestions with component_id, name, type, score.

If the user already knows the component ID, skip this step.

### 2. Inspect component (optional)

```bash
kbagent --json component detail --component-id COMPONENT_ID --project ALIAS
```

Shows schema summary, examples count, documentation URL.

### 3. Generate scaffold

```bash
# To stdout (inspect before writing)
kbagent --json config new --component-id COMPONENT_ID --project ALIAS --name "Config Name"

# To disk (auto-detects kbc project structure, writes under main/ if applicable)
kbagent config new --component-id COMPONENT_ID --project ALIAS --name "Config Name" --output-dir .

# One-shot remote create (since 0.33.0) -- no filesystem, just POST + return ID
kbagent --json config new --component-id COMPONENT_ID --project ALIAS --name "Config Name" \
  --push --no-files

# Scaffold AND remote create in one step (writes files AND POSTs)
# Since 0.89.0 the written scaffold records the created config's ID
# (_keboola.config_id) and lands in the subtree of the branch the config was
# created in -- the next `sync push` ADOPTS the config (reported as
# `modified` until you edit + push) instead of creating a duplicate.
# On older versions this combo wrote an ID-less scaffold: the next
# `sync push` DUPLICATED the config (issue #644). There, use the two-step
# path (scaffold without --push, edit, `sync push`) instead.
kbagent config new --component-id COMPONENT_ID --project ALIAS --name "Config Name" \
  --output-dir . --push
```

Generated files by component type:

| Type | Files |
|------|-------|
| Extractor/Writer | `_config.yml` |
| SQL transformation | `_config.yml` + `transform.sql` |
| Python transformation | `_config.yml` + `transform.py` + `pyproject.toml` |
| Custom Python app | `_config.yml` + `code.py` + `pyproject.toml` |
| Flow/Orchestrator | `_config.yml` (with phases/tasks/schedules) |

### 4. Edit the scaffold

- Fill in actual parameter values (hostnames, database names, etc.)
- Replace `<YOUR_SECRET>` placeholders with actual credentials
- Adjust storage input/output table mappings

### 5. Push to Keboola

```bash
kbagent sync push --project ALIAS
```

This automatically:
- Encrypts all `#`-prefixed secret fields (e.g. `#password`) via Encryption API
- Creates the configuration in Keboola and gets a config_id
- Writes back encrypted values + config_id to local `_config.yml`

## SQL transformation file structure

SQL transformations use a two-file layout. **SQL code lives ONLY in `transform.sql`,
never in `_config.yml`.**

```
my-transformation/
  _config.yml      # metadata + parameters: {} (empty! no blocks here)
  transform.sql    # all SQL code with block/code markers
```

### _config.yml for SQL transformations

```yaml
version: 2
name: My Transformation
description: ''
parameters: {}          # MUST be empty -- blocks are in transform.sql
output:
  tables:
  - source: out_table
    destination: out.c-bucket.table
_keboola:
  component_id: keboola.snowflake-transformation
```

### transform.sql format

SQL is organized into blocks and codes using marker comments:

```sql
/* ===== BLOCK: Staging ===== */

/* ===== CODE: Create staging table ===== */
CREATE TABLE "staging" AS
    SELECT *
    FROM "raw_data"
    WHERE "active" = true;

/* ===== BLOCK: Output ===== */

/* ===== CODE: Final output ===== */
CREATE TABLE "out_result" AS
    SELECT
        "id",
        "name",
        SUM("amount") AS "total"
    FROM "staging"
    GROUP BY "id", "name";
```

Rules:
- Each `/* ===== BLOCK: Name ===== */` starts a new block
- Each `/* ===== CODE: Name ===== */` starts a new code section within the current block
- Multi-line SQL is fine -- the entire code section is sent as one statement
- If no markers are present, the whole file is treated as a single block/code

## Important notes

- `_config.yml` format follows the kbc CLI dev-friendly YAML structure
- The `_keboola.component_id` field in `_config.yml` is required for push to work
- `config_id` is assigned by Keboola on first push -- don't set it manually.
  Exception: `config new --push --output-dir` (0.89.0+) writes it itself,
  because on that path the config already exists remotely
- Secret fields use Keboola convention: any key starting with `#` is a secret
- Scaffold marks secret placeholders with `# encrypted by Keboola on push` comments
- Encrypted values look like `KBC::ProjectSecure::...`
- Already-encrypted values are NOT re-encrypted on subsequent pushes
- If `--output-dir` points to a sync working directory with a `main/` prefix, the scaffold auto-detects it and nests files correctly
