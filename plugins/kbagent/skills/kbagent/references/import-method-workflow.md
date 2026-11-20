# Snowflake Import Method Workflow

How a Snowflake table import writes the destination table, and how to choose
it: per import with `--import-method` *(since vNEXT)*, or per output mapping
with the configuration key `import_method`. Check `kbagent version` first.

## The three values

| Value | What Storage does |
|-------|-------------------|
| `auto` (default) | The platform default for the project. kbagent sends nothing. |
| `single_statement` | One `MERGE` (incremental load with a primary key) or one `INSERT OVERWRITE` (full load). Atomic: a failure leaves the table as it was. A string primary key treats NULL and `''` as the same key. |
| `multi_statement` | A deduplication table and separate `UPDATE` / `DELETE` / `INSERT` statements (full load: `TRUNCATE` + `INSERT`). The older behaviour, kept for its duplicate semantics. |

For input without duplicate primary keys both methods give the same rows.
They differ when the input has duplicate primary keys (which row wins), for
NULL vs `''` in a string primary key, in the imported row count reported for
those cases, and in what is left after a failed import.

**Snowflake only.** Any value other than `auto` on a table of another backend
(BigQuery, ...) is refused by Storage with HTTP 400
`storage.tables.importMethodNotSupported`, and nothing is imported. `auto`
works on every backend.

## Direct imports: `storage upload-table` / `storage load-file`

```bash
kbagent --json storage upload-table --project P --table-id in.c-main.orders \
  --file orders.csv --incremental --import-method single_statement

kbagent --json storage load-file --project P --file-id 123 \
  --table-id in.c-main.orders --import-method multi_statement
```

- The result carries `import_method` (the value you passed; `auto` when
  omitted). To see which method Storage actually used, read the
  `tableImportDone` event of the table: its params `importMethod` and
  `importMethodSource` (`request`, `projectFeature` or `default`).
- A refusal on a non-Snowflake table is `INVALID_ARGUMENT`, exit 2. Run the
  import again without `--import-method`.
  - `upload-table` reads the bucket's backend first (a bucket it would
    auto-create gets the project's default backend) and refuses before it
    creates or uploads anything: `details` = `table_id`, `backend`,
    `import_method`.
  - `load-file`, and `upload-table` when no backend can be read, get the
    refusal from Storage: `details.api_error_code` =
    `storage.tables.importMethodNotSupported`. After `upload-table` the file
    is then already in Storage: the error names the `storage load-file`
    command that imports it without the method, and any bucket or table it
    auto-created.
- Over `kbagent serve`: the form field `import_method` on
  `POST /storage/tables/{project}/upload` and the body field `import_method`
  on `POST /storage/files/{project}/load-to-table`. A refusal is HTTP 400
  with `error.code` `INVALID_ARGUMENT`; an unknown value is HTTP 422.
- An unknown value is a usage error (exit 2) before any request.

## Output mapping: configuration key `import_method`

A component configuration chooses the method per output table with
`import_method` in `storage.output.tables[]`. The output mapping sends it to
Storage as `importMethod` when the job writes the table. kbagent does not
validate the key; `config update` writes it like any other configuration
value:

```bash
# Read the mapping first -- --set on a list index replaces only that entry's key
kbagent --json config detail --project P --component-id keboola.snowflake-transformation --config-id 123
kbagent --json config update --project P --component-id keboola.snowflake-transformation \
  --config-id 123 --set 'storage.output.tables.0.import_method=single_statement' --dry-run
```

Rules for the key:

- Omit it (or remove it) for the platform default. Do not write `auto`
  explicitly unless the user asks: an absent key and `auto` mean the same.
- Snowflake projects only. In a BigQuery (or other) project the key makes
  every job that writes that table fail with the 400 above. Remove the key
  when you copy a configuration into a project of another backend
  (`config clone --target-project`, `sync clone`).
- It has no effect on a table that the output mapping does not import:
  a Snowflake transformation with the `direct-grant` unload strategy, and
  `deduplication_strategy: insert` (a CTAS without deduplication, which wins
  over either method).
- The key needs an output-mapping library and job runner that know it. On a
  stack that does not have them yet, an unknown key can fail the job's
  configuration validation. If a job fails on `import_method`, remove the key
  and tell the user.
