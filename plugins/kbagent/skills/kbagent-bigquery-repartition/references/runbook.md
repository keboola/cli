# Runbook: convert one BigQuery table to a partitioned and clustered layout

Follow the sections in order. One run converts one table. Rules that hold in
every section are in [../SKILL.md](../SKILL.md), "Guardrails": every write
waits for an explicit yes, `--json` on every call, a timeout is never answered
with a re-run, a person runs every `bq` command that writes.

Two kinds of people act in this runbook:

- **The operator** follows the runbook, runs the kbagent commands, and has
  BigQuery access to the project's dataset for the `bq` steps.
- **The owner** owns the table. The owner pauses and resumes the writers,
  changes writer configurations, and says yes to the drop on day 7.

## 0. Inputs and naming

Placeholders used in every command:

| Placeholder | Meaning |
|---|---|
| `ALIAS` | Project alias in kbagent |
| `B` | Bucket ID |
| `T` | Table name |
| `B.T` | Table ID of the table to convert |
| `T_repart` | Name of the new table, in the same bucket |
| `COL` | Partition column |
| `C1`, `C2` | Clustering columns (at most 4) |
| `PK` | Primary key column |
| `OTHER` | Every remaining column |
| `WS_ID` | ID of the helper workspace |
| `CFG_ID` | ID of the helper transformation (path B) |
| `JOB_ID` | ID of a Storage job |
| `GCP_PROJECT` | Google Cloud project of the Keboola project |
| `DATASET` | BigQuery dataset of bucket `B` |

`GCP_PROJECT` and `DATASET` are in `sql_path` of the table detail (section 1).

`T_repart` lives in the same bucket as `T`. The swap refuses a pair of tables
from two buckets, and it refuses an alias.

All writes run in production: the swap uses `--branch 0`. Check that no dev
branch is active for the project, and reset it when one is:

```bash
kbagent --json project status --project ALIAS
kbagent branch reset --project ALIAS
```

## 1. Preflight (read only)

Nothing in this section writes to the table. Record each value in the
preflight record at the end of the section.

### Table

```bash
kbagent --json storage table-detail --project ALIAS --table-id B.T \
  | jq '.data | {is_alias, rows_count, data_size_bytes, primary_key, last_import_date, sql_path, column_details, layout: (.definition | {timePartitioning, rangePartitioning, clustering}), policy_tags: [.definition.columns[] | {name, policyTagState}]}'
```

Never print `definition.partitions`. It has one entry per physical partition
and is unbounded.

Read from the result:

- `is_alias`. Refuse the table when it is true.
- `rows_count`, `data_size_bytes`, `primary_key`, `last_import_date`.
- `column_details[].native_type`: the type of every column. The type of `COL`
  decides the path. DATE, DATETIME or TIMESTAMP is path A. STRING is path B.
  On an untyped table every column is STRING, so an untyped table is path B.
- `definition.timePartitioning`, `rangePartitioning`, `clustering`: the
  current layout.
- `definition.columns[] | {name, policyTagState}`: `protected`, `unprotected`
  or `unknown` per column.
- `sql_path`: the BigQuery path of the table.

### Sharing, features, writers, readers, aliases

```bash
kbagent --json storage bucket-detail --project ALIAS --bucket-id B
kbagent --json sharing edges --project ALIAS
kbagent --json project info --project ALIAS
kbagent --json storage tables --project ALIAS --bucket-id B --include-usage
kbagent --json flow list --project ALIAS --with-schedules
kbagent --json storage tables --project ALIAS
```

- **Sharing.** Record `sharing` of the bucket and every edge in which the
  bucket is the source.
- **Policy tags.** Look for the project feature `bigquery-policy-tags` in
  `features` of the project info. When any column is `protected` and the
  feature is missing, refuse the table. The second condition of tag
  propagation is the backend capability `policyTagsEnabled`. kbagent cannot
  read it: it is in the Manage API and a project token does not reach it.
  Nobody has to read it up front. Section 4c checks the result on the copy
  instead, and the operator refuses the table there when a tag is missing.
- **Writers and readers.** Record `used_by` of `B.T`, and the flows with
  schedules that run those configurations.
- **Aliases.** kbagent shows that a table is an alias (`is_alias`), but not
  which table the alias points at. List every table of the project with
  `is_alias: true` from the last command, and ask the owner which of them
  point at `B.T`. Record them. Aliases in other projects exist only through
  linked buckets, which the sharing edges show.

### Partition values

Create a helper workspace and load the table as a view:

```bash
kbagent --json workspace create --project ALIAS --name repartition-T
kbagent --json workspace load --project ALIAS --workspace-id WS_ID --table-id B.T --load-type view
kbagent --json workspace query --project ALIAS --workspace-id WS_ID --sql "SELECT COUNT(DISTINCT DATE(COL)) AS days, COUNT(DISTINCT DATE_TRUNC(DATE(COL), MONTH)) AS months, MIN(COL) AS min_value, MAX(COL) AS max_value, COUNTIF(COL IS NULL) AS nulls FROM T"
```

`workspace create` and `workspace load` write to the project (a workspace and
a view), not to the table. They still wait for a yes.

For a STRING column run the profile query of section 4a first. Then run the
query above with `SAFE.PARSE_DATE('%Y-%m-%d', COL)` in place of `DATE(COL)`.

A cluster-only table skips this query.

### BigQuery-native objects

The swap is a rename of two physical tables. Four kinds of BigQuery objects
are outside Storage, and kbagent can neither read nor write them: table IAM
bindings, row access policies, search indexes and the streaming buffer. The
operator reads them with `bq`:

```bash
bq --project_id=GCP_PROJECT get-iam-policy --format=prettyjson DATASET.T
bq --project_id=GCP_PROJECT ls --row_access_policies --format=prettyjson DATASET.T
bq --project_id=GCP_PROJECT query --use_legacy_sql=false 'SELECT index_name, index_status, ddl FROM DATASET.INFORMATION_SCHEMA.SEARCH_INDEXES WHERE table_name = "T"'
bq --project_id=GCP_PROJECT show --format=prettyjson DATASET.T
```

Record each object together with what is needed to create it again:

- **Table IAM bindings**: the policy JSON. An empty `bindings` list means
  there is nothing to carry.
- **Row access policies**: the `CREATE ROW ACCESS POLICY` statement of each
  policy (name, grantees, filter expression).
- **Search indexes**: the `CREATE SEARCH INDEX` statement of each index.
  Check that each statement is complete: the index name, the indexed columns
  or `ALL COLUMNS`, and every option of the existing index. When a statement
  cannot be reconstructed completely, refuse the table.
- **Streaming buffer**: the key `streamingBuffer` in the output of the last
  command. Present means that a streaming writer wrote recently (section 3).

When the operator cannot read these objects, refuse the table.

### Refuse rules

Refuse the table, and change nothing, when one of these holds:

- The table is an alias.
- A column is `protected` and the project has no `bigquery-policy-tags`
  feature.
- The BigQuery-native objects are unknown, because nobody could read them.
- The recorded statement of a search index is incomplete.
- The operator cannot apply the recorded IAM bindings or row access policies
  to the copy.
- The copy was created without a policy tag that the source has (section 4c).
- No partition column and no clustering column is confirmed.
- `data_size_bytes` is over 5 TiB and the owner has not decided on the ranged
  transformation. Stop and hand the table to the owner.

### Preflight record

```text
Project alias:
Table ID:
Class (cluster only / column ready / column not ready):
Path (A / B):
rows_count:
data_size_bytes:
primary_key:
last_import_date:
Columns and types:
Current layout (timePartitioning / rangePartitioning / clustering):
policyTagState per column:
Project feature bigquery-policy-tags (yes / no):
sql_path:
Bucket sharing:
Sharing edges with the bucket as source:
Writers (used_by, flows, schedules):
Readers:
Aliases that point at the table:
Distinct days / months, min, max, nulls of the partition column:
Table IAM policy JSON:
Row access policies (CREATE statements):
Search indexes (CREATE statements):
Streaming buffer (present / absent):
Owner who pauses the writers:
```

## 2. Choose granularity

Use the `days` and `months` values of section 1.

| Distinct values | Granularity | Flag |
|---|---|---|
| At most 4,000 days | DAY | `--time-partitioning-type DAY` |
| More than 4,000 days | MONTH | `--time-partitioning-type MONTH` |
| More than 4,000 months | Stop. Ask the owner. | |

BigQuery rejects a job that modifies more than 4,000 partitions. The limit is
per job. So it binds the copy, and it also binds the output-mapping load of
path B (section 4), which is a full load into the partitioned table.

## 3. Pause writers (owner)

The owner stops every writer recorded in section 1: loads, Direct Grant
output mapping writes, and streaming writers on the owner's side. The skill
changes no configuration and pauses nothing.

Check that no job runs before the copy:

```bash
kbagent --json job list --project ALIAS --status processing
```

- **Streaming.** The copy starts only when `streamingBuffer` is absent. The
  operator reads it with the `bq show` command of section 1. BigQuery may
  refuse the swap for about 5 hours after the last streaming write.
- **DML.** A running DML statement on the table blocks the rename.

Read `rows_count` and `last_import_date` once more right before the copy
(the table detail command of section 1) and record both.

## 4. Copy

### Path A: the partition column is DATE, DATETIME or TIMESTAMP, or cluster only

```bash
kbagent --json storage create-table --project ALIAS --bucket-id B --name T_repart \
  --source-table-id B.T --time-partitioning-type DAY --time-partitioning-field COL \
  --clustering-field C1 --clustering-field C2 --primary-key PK --timeout 3600
```

Use the granularity of section 2. For a cluster-only table omit both
`--time-partitioning-*` flags.

The copy keeps the columns, their types and NOT NULL of the source. It keeps
the primary key that the command requests.

On exit 4 with `STORAGE_JOB_TIMEOUT` the job still runs. Wait for it, and
never re-run the copy:

```bash
kbagent --json storage job-detail --project ALIAS --job-id JOB_ID --wait --timeout 3600
```

Record the duration of the copy from the job times.

### Path B: type change (the date is a STRING, or the table is untyped)

#### 4a. Profile the column

The format string is an input from the owner. The default is `%Y-%m-%d`.

```bash
kbagent --json workspace query --project ALIAS --workspace-id WS_ID --sql "SELECT COUNTIF(COL IS NOT NULL AND SAFE.PARSE_DATE('%Y-%m-%d', COL) IS NULL) AS bad_values, COUNTIF(COL IS NULL) AS nulls, COUNT(*) AS total FROM T"
kbagent --json workspace query --project ALIAS --workspace-id WS_ID --sql "SELECT COL, COUNT(*) AS n FROM T WHERE COL IS NOT NULL AND SAFE.PARSE_DATE('%Y-%m-%d', COL) IS NULL GROUP BY COL ORDER BY n DESC LIMIT 20"
```

When `bad_values` is above 0: stop and report the sample values of the second
query to the owner. No table is created.

#### 4b. Create the empty typed table with the layout

Every column keeps its type from `column_details`. On an untyped table every
column is `STRING`. `COL` becomes `DATE`. NOT NULL and the primary key are the
same as on the source.

```bash
kbagent --json storage create-table --project ALIAS --bucket-id B --name T_repart \
  --column PK:STRING --column COL:DATE --column C1:STRING --column OTHER:STRING \
  --primary-key PK --not-null-column PK \
  --time-partitioning-type DAY --time-partitioning-field COL --clustering-field C1
```

List the columns in the order of the source.

#### Fill the table with a transformation (after 4b)

A BigQuery SQL transformation reads `B.T` and its output mapping loads into
`B.T_repart`. The load is a full load, and the table keeps its layout. Create
the transformation without `--created-table`, then set both mappings:

```bash
kbagent --json transformation create --project ALIAS --component-id keboola.google-bigquery-transformation \
  --name "repartition T" --sql "CREATE TABLE T_repart AS SELECT PK, SAFE.PARSE_DATE('%Y-%m-%d', COL) AS COL, C1, OTHER FROM T"
kbagent --json transformation edit --project ALIAS --config-id CFG_ID --change-description "map repartition tables" \
  --storage '{"input": {"tables": [{"source": "B.T", "destination": "T"}]}, "output": {"tables": [{"source": "T_repart", "destination": "B.T_repart"}]}}'
kbagent --json job run --project ALIAS --component-id keboola.google-bigquery-transformation --config-id CFG_ID --wait --timeout 3600
kbagent --json config delete --project ALIAS --component-id keboola.google-bigquery-transformation --config-id CFG_ID
```

The column list in the SELECT is the full column list of the source, in the
order of the source. Run `config delete` only after a successful job: it
moves the helper configuration to the trash.

kbagent has no command that imports a workspace table into a Storage table.
That is why the fill goes through a transformation and its output mapping.

After the swap the owner changes the writers of `B.T`, so that they write a
DATE into `COL`. The runbook does not change them.

## 4c. Secure the copy before the swap (a person with BigQuery access)

**A person runs every `bq` command in this section that writes. The agent
that follows this runbook prints the command in full, waits for the person to
confirm that it was run, and then reads the result. It never runs the command
itself.**

This section runs after the copy of path A or path B, and before section 5.
The table must never be under the production name without its policy tags,
its IAM bindings and its row access policies. A rename keeps all three on the
renamed table, so they are put on the copy first. Keep this order:

1. **Policy tags.** Read the state on the copy:

   ```bash
   kbagent --json storage table-detail --project ALIAS --table-id B.T_repart \
     | jq '[.data.definition.columns[] | {name, policyTagState}]'
   ```

   When a column that is `protected` on `B.T` is not `protected` on the copy,
   stop and refuse the table. Do not set the tag outside Storage: a tag that
   is written to the BigQuery schema directly bypasses Storage.

2. **Table IAM bindings.** A person applies the recorded policy to the copy.
   Save the policy JSON of section 1 to a file and remove its `etag` key
   first. A person runs:

   ```bash
   bq --project_id=GCP_PROJECT set-iam-policy DATASET.T_repart T.iam.json
   ```

3. **Row access policies.** A person runs each recorded statement against the
   copy, with the table name changed to the copy. A person runs:

   ```bash
   bq --project_id=GCP_PROJECT query --use_legacy_sql=false 'CREATE ROW ACCESS POLICY POLICY_NAME ON DATASET.T_repart GRANT TO ("GRANTEE") FILTER USING (FILTER_EXPRESSION)'
   ```

4. **Search indexes** are not created here. A rename drops them, so they are
   created again in section 6.

When one of these steps cannot be done, stop. Do not swap. Drop `B.T_repart`
with the owner's yes, and refuse the table.

## 5. Validate (refuse the swap on any mismatch)

```bash
kbagent --json storage table-detail --project ALIAS --table-id B.T_repart
kbagent --json workspace load --project ALIAS --workspace-id WS_ID --table-id B.T --table-id B.T_repart --load-type view
kbagent --json workspace query --project ALIAS --workspace-id WS_ID --sql "SELECT (SELECT COUNT(*) FROM T) AS old_rows, (SELECT COUNT(*) FROM T_repart) AS new_rows"
```

Every check must pass. On a mismatch, stop and do not swap.

- **Row count.** `old_rows` equals `new_rows`, and both equal the `rows_count`
  recorded right before the copy. A difference means that a write landed.
- **Columns and types.** The column list and the types are equal. Path B:
  only `COL` differs, and it is `DATE`.
- **Primary key.** `primary_key` is equal.
- **Layout.** `definition.timePartitioning` and `definition.clustering` of the
  copy are as requested.
- **Policy tags.** `policyTagState` of each column equals the source. A
  `protected` source column that is not `protected` on the copy stops the
  conversion.
- **Table IAM bindings.** The bindings of the copy equal the recorded ones.
- **Row access policies.** The policies of the copy equal the recorded ones.
- **Path B, NULL count.** The count of NULL in `COL` is equal on both tables.

kbagent cannot read IAM bindings and row access policies. The operator reads
both on the copy:

```bash
bq --project_id=GCP_PROJECT get-iam-policy --format=prettyjson DATASET.T_repart
bq --project_id=GCP_PROJECT ls --row_access_policies --format=prettyjson DATASET.T_repart
```

Tables over 5 TiB, and every path B table, also get a row count per
partition:

```bash
kbagent --json workspace query --project ALIAS --workspace-id WS_ID --full --sql "SELECT COALESCE(o.p, n.p) AS p, o.c AS old_rows, n.c AS new_rows FROM (SELECT DATE_TRUNC(DATE(COL), MONTH) AS p, COUNT(*) AS c FROM T GROUP BY p) o FULL OUTER JOIN (SELECT DATE_TRUNC(COL, MONTH) AS p, COUNT(*) AS c FROM T_repart GROUP BY p) n ON o.p = n.p WHERE o.c IS DISTINCT FROM n.c"
```

An empty result passes. On a path B table whose format is not `%Y-%m-%d`,
replace `DATE(COL)` of the old table with `SAFE.PARSE_DATE` and the owner's
format string.

Right before the swap, read `rows_count` and `last_import_date` of `B.T`
again. Both must equal the values recorded before the copy.

## 6. Swap

**A person runs every `bq` command in this section that writes. The agent
that follows this runbook prints the command in full, waits for the person to
confirm that it was run, and then reads the result. It never runs the command
itself.**

```bash
kbagent --json storage swap-tables --project ALIAS --table-id B.T --target-table-id B.T_repart --branch 0 --dry-run
kbagent --json storage swap-tables --project ALIAS --table-id B.T --target-table-id B.T_repart --branch 0 --yes --timeout 3600
kbagent --json storage table-detail --project ALIAS --table-id B.T
```

After the swap `B.T` holds the new layout and `B.T_repart` holds the old
table. Then, in this order:

1. **Layout.** Verify `definition.timePartitioning` and `clustering` on
   `B.T`.
2. **Security objects.** The IAM bindings and row access policies of section
   4c and the propagated policy tags travelled with the rename. The operator
   reads them once more on `B.T` (the `get-iam-policy` and
   `ls --row_access_policies` commands of section 1; `policyTagState` from the
   table detail). Stop on a difference. The old physical table, now
   `T_repart`, keeps its own bindings and policies.
3. **Search indexes.** A person creates each recorded index again on `B.T`.
   A person runs:

   ```bash
   bq --project_id=GCP_PROJECT query --use_legacy_sql=false 'CREATE SEARCH INDEX INDEX_NAME ON DATASET.T (ALL COLUMNS)'
   ```

   Use the recorded statement of each index, not this example. When a
   statement fails, retry it. When it fails again, report the missing index to
   the owner and leave the table swapped: the first swap already dropped the
   indexes on both tables, so a second swap restores nothing.
4. **Aliases.** Read every alias recorded in section 1 with
   `storage table-detail`, and compare its columns and `rows_count` with
   `B.T`. A mismatch is an alias view that did not refresh. Report it to the
   owner.
5. **Shared bucket.** Check the bucket with `storage bucket-detail`: the
   sharing is the same as recorded.

On exit 4 with `STORAGE_JOB_TIMEOUT`: wait with `storage job-detail --wait`
(the command of section 4) and never re-run the swap. On the error
`storage.tables.swapMetadataFailed`: go to section 9.

kbagent has no command that lists project events, so the swap is verified
from the table detail and not from the event stream.

## 7. Resume writers and watch (owner)

The owner resumes the writers. Watch the next load and confirm that the
layout stayed:

```bash
kbagent --json job list --project ALIAS --limit 20
kbagent --json storage table-detail --project ALIAS --table-id B.T
```

When the table loads incrementally and its primary key contains `COL`,
recommend `partition_aware_import` in the output mapping to the owner. The
runbook does not change the configuration.

## 8. Keep the old table 7 days, then drop it

While `B.T_repart` exists, the rollback is a second swap:

```bash
kbagent --json storage swap-tables --project ALIAS --table-id B.T --target-table-id B.T_repart --branch 0 --yes --timeout 3600
kbagent --json storage table-detail --project ALIAS --table-id B.T
```

After a rollback the original physical table is back at `B.T`, with its IAM
bindings and row access policies. A person creates the search indexes again
(section 6, step 3). After a path B rollback the owner also reverts the
writers to STRING.

Time travel of the new table starts at the copy. A restore to an earlier
point is possible only from the old table.

On day 7, with the owner's yes:

```bash
kbagent --json storage delete-table --project ALIAS --table-id B.T_repart --dry-run
kbagent --json storage delete-table --project ALIAS --table-id B.T_repart --yes
kbagent --json workspace delete --project ALIAS --workspace-id WS_ID --yes
```

After the drop, the old data is reachable through BigQuery time travel (2 to
7 days), and after that only through the 7-day fail-safe, which Google
support restores.

## 9. Recovery of a half-moved swap

**Symptom.** The swap job fails with `storage.tables.swapMetadataFailed`:
"The data is swapped; the metadata of both tables may be inconsistent."

**Rules.**

- Do not re-run the swap. It swaps the data back.
- Do not run any other write on either table.
- The owner keeps the writers paused.

Collect evidence, read only:

```bash
kbagent --json storage table-detail --project ALIAS --table-id B.T
kbagent --json storage table-detail --project ALIAS --table-id B.T_repart
kbagent --json workspace load --project ALIAS --workspace-id WS_ID --table-id B.T --table-id B.T_repart --load-type view
kbagent --json workspace query --project ALIAS --workspace-id WS_ID --sql "SELECT (SELECT COUNT(*) FROM T) AS rows_at_t, (SELECT COUNT(*) FROM T_repart) AS rows_at_t_repart"
```

Compare, for both tables, the physical state with the Storage rows:

| Physical state | Storage row |
|---|---|
| Row count of the query above | `rows_count` |
| Column list (`INFORMATION_SCHEMA.COLUMNS` of the dataset, or `bq show`) | `column_details` |
| Partitioning and clustering (`bq show`) | `definition` layout |
| | descriptions, `primary_key` |

kbagent has no bucket refresh and no other command that repairs the metadata
of a half-moved swap. Hand the evidence to the Connection maintainers
(Storage). They finish or undo the metadata move. Continue at section 6,
step 1, only after both tables match their physical state.

## 10. Cost and record

- The copy bills one full scan of the table.
- Storage is doubled until the drop on day 7.
- The copy is active storage for 90 days.

Record for the pilot: the copy duration, the swap duration, the row counts,
and the layout before and after.
