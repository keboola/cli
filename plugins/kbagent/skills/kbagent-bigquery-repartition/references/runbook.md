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
kbagent --json branch reset --project ALIAS
```

## 1. Preflight (no write to the table)

Nothing in this section writes to the table. Record each value in the
preflight record at the end of the section.

### Table

```bash
kbagent --json storage table-detail --project ALIAS --table-id B.T \
  | jq '.data | {is_alias, rows_count, data_size_bytes, primary_key, last_import_date, sql_path, column_details, layout: ((.definition // {}) | {timePartitioning, rangePartitioning, clustering, requirePartitionFilter}), policy_tags: [(.definition.columns // [])[] | {name, policyTagState}]}'
```

The filter prints `definition.requirePartitionFilter` as
`layout.requirePartitionFilter`. It still prints the other keys when
`definition` or `definition.columns` is null: `policy_tags` is then empty.

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
- `definition.requirePartitionFilter`. Refuse the table when it is true. Every
  query of this runbook (the partition-value profile, the row counts of
  sections 3 and 5) reads the whole table without a partition predicate, and
  BigQuery enforces that requirement through the Storage view as well, so the
  conversion cannot complete. Removing the requirement is the owner's
  decision, outside this runbook.
- `definition.columns[] | {name, policyTagState}`: `protected`, `unprotected`
  or `unknown` per column. This is the state that Storage observed last, not
  the state in BigQuery now, and `unknown` says nothing. Record it, but never
  compare two tables with it. The comparison uses the policy tags that the
  operator reads from BigQuery ("BigQuery-native objects" below).
- `sql_path`: the BigQuery path of the table.

### Row access policies (stop rule)

Read the row access policies of the table before anything is created.
kbagent cannot read them. The operator runs:

```bash
bq --project_id=GCP_PROJECT ls --row_access_policies --format=prettyjson DATASET.T
```

When the table has at least one row access policy, stop here. Create
nothing: no workspace, no copy. Name the policies to the owner. The table
stays as it is, and its owner decides what to do with it outside this skill.

The reason: the Storage copy reads the table through the policy. It copies
only the rows that the service account of the project can see, and it
reports no error. The validation counts through the same filter, so it can
pass on a copy that lacks rows.

When the operator cannot read the policies, refuse the table.

### BigQuery-native objects

The swap is a rename of two physical tables. Three more kinds of BigQuery
objects are outside Storage, and kbagent can neither read nor write them:
table IAM bindings, search indexes and the streaming buffer. The operator
reads them with `bq`:

```bash
bq --project_id=GCP_PROJECT get-iam-policy --format=prettyjson DATASET.T
bq --project_id=GCP_PROJECT query --use_legacy_sql=false 'SELECT index_name, index_status, ddl FROM DATASET.INFORMATION_SCHEMA.SEARCH_INDEXES WHERE table_name = "T"'
bq --project_id=GCP_PROJECT show --format=prettyjson DATASET.T
```

Record each object together with what is needed to create it again:

- **Table IAM bindings**: the policy JSON. An empty `bindings` list means
  there is nothing to carry.
- **Search indexes**: the `CREATE SEARCH INDEX` statement of each index.
  Check that each statement is complete: the index name, the indexed columns
  or `ALL COLUMNS`, and every option of the existing index. When a statement
  cannot be reconstructed completely, refuse the table.
- **Streaming buffer**: the key `streamingBuffer` in the output of the last
  command. Present means that a streaming writer wrote recently (section 3).
- **Policy tags per column**: from the output of the last command. This
  filter prints the tags of every column, with the tags of fields nested in
  a RECORD column counted to that column:

  ```bash
  bq --project_id=GCP_PROJECT show --format=prettyjson DATASET.T \
    | jq '[.schema.fields[] | {name, tags: ([.. | objects | .policyTags? | select(. != null) | .names[]] | sort)}]'
  ```

  Record the result. It is the reference for the copy (sections 4c and 5)
  and for the table after the swap (section 6). The policy-tag rules of the
  next subsection read it.

When the operator cannot read these objects, refuse the table.

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
  `features` of the project info. When any column carries a policy tag in
  BigQuery (the policy tags per column recorded in "BigQuery-native objects"
  above) and the feature is missing, refuse the table. The second condition of tag
  propagation is the backend capability `policyTagsEnabled`. kbagent cannot
  read it: it is in the Manage API and a project token does not reach it.
  When any column carries a policy tag, have a person with Manage API access
  read the capability of the project's backend now, and record the answer.
  Refuse the table when it is false or unknown. The project feature can stay
  enabled after the backend was disabled by hand, and a path A copy is
  created with its rows already in it: an untagged copy of protected data
  exists from that moment, readable by every dataset reader, and the check
  of section 4c can only refuse and drop it afterwards, not undo the read.
- **Writers and readers.** Record `used_by` of `B.T`, and the flows with
  schedules that run those configurations.
- **Aliases.** kbagent shows that a table is an alias (`is_alias`), but not
  which table the alias points at. List every table of the project with
  `is_alias: true` from the last command, and ask the owner which of them
  point at `B.T`. Record them. Aliases in other projects exist only through
  linked buckets, which the sharing edges show.

### Refuse rules

Refuse the table, and change nothing, when one of these holds:

- The table is an alias.
- `definition.requirePartitionFilter` is true.
- The table has at least one row access policy. Tables with row access
  policies are not converted in this version.
- A column carries a policy tag in BigQuery and the project has no
  `bigquery-policy-tags` feature.
- A column carries a policy tag in BigQuery and the backend capability
  `policyTagsEnabled` is false or could not be read (section 1).
- The BigQuery-native objects are unknown, because nobody could read them.
- The recorded statement of a search index is incomplete.
- The operator cannot apply the recorded IAM bindings to the copy.
- The copy was created without a policy tag that the source has (section 4c).
- No partition column and no clustering column is confirmed.
- `data_size_bytes` is over 5 TiB and the owner has not decided on the ranged
  transformation. Stop and hand the table to the owner.

Nothing above this line created anything in the project, so a table that is
refused here leaves nothing behind.

### Partition values (after the refuse rules pass)

Run this subsection only when no refuse rule above holds. It creates the
helper workspace, the first object of this run in the project:

```bash
kbagent --json workspace create --project ALIAS --name repartition-T
kbagent --json workspace load --project ALIAS --workspace-id WS_ID --table-id B.T --load-type view
kbagent --json workspace query --project ALIAS --workspace-id WS_ID --sql "SELECT COUNT(DISTINCT DAY_EXPR) AS days, COUNT(DISTINCT DATE_TRUNC(DAY_EXPR, MONTH)) AS months, MIN(COL) AS min_value, MAX(COL) AS max_value, COUNTIF(COL IS NULL) AS nulls FROM T"
```

`workspace create` and `workspace load` write to the project (a workspace and
a view), not to the table. They still wait for a yes.

`DAY_EXPR` is the day of a row as a DATE. It depends on the type of `COL`.
Replace it in every query of this runbook:

| Type of `COL` | `DAY_EXPR` |
|---|---|
| DATE | `COL` |
| DATETIME | `DATE(COL)` |
| TIMESTAMP | `DATE(COL)` (the UTC day, which is how BigQuery partitions a TIMESTAMP column) |
| STRING (path B) | `SAFE.PARSE_DATE('%Y-%m-%d', COL)`, with the owner's format string in place of `%Y-%m-%d` |

For a STRING column run the profile query of section 4a first.

A cluster-only table skips the query, but it still creates the workspace and
loads the view: sections 3 and 5 count rows through it.

From here on the workspace `WS_ID` exists. Every stop before the copy
(section 2, section 4a) ends with its deletion:

```bash
kbagent --json workspace delete --project ALIAS --workspace-id WS_ID --yes
```

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
requirePartitionFilter (must be false):
Column defaults (column_details[].default):
policyTagState per column (Storage, last observed):
Policy tags per column (bq show):
Project feature bigquery-policy-tags (yes / no):
Backend capability policyTagsEnabled (yes / no / unknown):
sql_path:
Bucket sharing:
Sharing edges with the bucket as source:
Writers (used_by, flows, schedules):
Readers:
Aliases that point at the table:
Distinct days / months, min, max, nulls of the partition column:
Table IAM policy JSON:
Row access policies (must be none):
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

On that stop, delete the helper workspace (the `workspace delete` command of
section 1). No other object exists yet.

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

- **Streaming.** Ask the owner for the time of the last streaming write and
  record it. Rows that were streamed recently can be invisible to the copy
  for up to 90 minutes. The copy starts only when both hold: at least 90
  minutes passed since the last streaming write, and `streamingBuffer` is
  absent. The operator reads it with the `bq show` command of section 1.
  BigQuery refuses to rename a table for about 5 hours after the last
  streaming write, sometimes longer. Section 6 checks this before the swap.
- **DML.** A running DML statement on the table blocks the rename.

Count the rows of the table right before the copy, with a query on the view
that section 1 loaded, and record the result as the row count before the
copy:

```bash
kbagent --json workspace query --project ALIAS --workspace-id WS_ID --sql "SELECT COUNT(*) AS row_count FROM T"
```

Do not use `rows_count` of the table detail for this. It is Storage
metadata, and this runbook does not rely on it to move after a write that
goes around a Storage import (a Direct Grant write, a streaming write). Read
`rows_count` and `last_import_date` once more as well (the table detail
command of section 1) and record both, as a second signal only.

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
query to the owner. No table is created. Delete the helper workspace (the
`workspace delete` command of section 1).

#### 4b. Create the empty typed table with the layout

Every column keeps its type from `column_details`. On an untyped table every
column is `STRING`. `COL` becomes `DATE`. NOT NULL and the primary key are the
same as on the source. Every column default recorded in section 1
(`column_details[].default`) is repeated with `--default name=value`: the copy
does not inherit a default, and a column that loses it writes NULL on the next
insert that omits it, or fails its NOT NULL constraint.

```bash
kbagent --json storage create-table --project ALIAS --bucket-id B --name T_repart \
  --column PK:STRING --column COL:DATE --column C1:STRING --column OTHER:STRING \
  --primary-key PK --not-null-column PK --default OTHER=0 \
  --time-partitioning-type DAY --time-partitioning-field COL --clustering-field C1
```

List the columns in the order of the source.

#### Fill the table with a transformation (after 4b)

When any column of `B.T` carries a policy tag (section 1), check the tags of
the empty `B.T_repart` with the `bq show` filter of section 4c **before the
fill**, and refuse the table when a tag is missing. Output mapping does not
propagate policy tags, so a filled but untagged copy exposes the protected
values of every row to anyone with dataset read access, for the whole window
until section 4c refuses and the copy is dropped. On an empty table that
window carries no data.

A BigQuery SQL transformation reads `B.T` and its output mapping loads into
`B.T_repart`. The load is a full load, and the table keeps its layout. Create
the transformation without `--created-table`, then set both mappings:

```bash
kbagent --json transformation create --project ALIAS --component-id keboola.google-bigquery-transformation \
  --name "repartition T" --sql "CREATE TABLE T_repart AS SELECT PK, SAFE.PARSE_DATE('%Y-%m-%d', COL) AS COL, C1, OTHER FROM T"
kbagent --json transformation edit --project ALIAS --config-id CFG_ID --change-description "map repartition tables" \
  --storage '{"input": {"tables": [{"source": "B.T", "destination": "T"}]}, "output": {"tables": [{"source": "T_repart", "destination": "B.T_repart", "primary_key": ["PK"]}]}}'
kbagent --json job run --project ALIAS --component-id keboola.google-bigquery-transformation --config-id CFG_ID --wait --timeout 3600
kbagent --json config delete --project ALIAS --component-id keboola.google-bigquery-transformation --config-id CFG_ID
```

`primary_key` of the output mapping lists the same columns, in the same
order, as `--primary-key` of section 4b. An output mapping whose key differs
from the key of the destination table does not load as it is. Omit
`primary_key` only when the table has no primary key.

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
The table must never be under the production name without its policy tags
and its IAM bindings. A rename keeps both on the renamed table, so they are
put on the copy first. A table with a row access policy never reaches this
section (section 1). Keep this order:

1. **Policy tags.** The operator reads the tags of the copy from BigQuery,
   with the filter of section 1:

   ```bash
   bq --project_id=GCP_PROJECT show --format=prettyjson DATASET.T_repart \
     | jq '[.schema.fields[] | {name, tags: ([.. | objects | .policyTags? | select(. != null) | .names[]] | sort)}]'
   ```

   Compare the result with the one recorded for `B.T` in section 1, column
   by column. When a column of the copy lacks a tag that the same column of
   `B.T` has, stop and refuse the table. Do not compare `policyTagState` of
   the two tables: it is the last observed state, and `unknown` on both
   sides proves nothing. Do not set the tag outside Storage: a tag that is
   written to the BigQuery schema directly bypasses Storage.

2. **Table IAM bindings.** A person applies the recorded policy to the copy.
   Save the policy JSON of section 1 to a file and remove its `etag` key
   first. A person runs:

   ```bash
   bq --project_id=GCP_PROJECT set-iam-policy DATASET.T_repart T.iam.json
   ```

3. **Search indexes** are not created here. A rename drops them, so they are
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

- **Row count.** `old_rows` equals `new_rows`, and both equal the row count
  recorded right before the copy (section 3). A difference means that a
  write landed.
- **Columns and types.** The column list and the types are equal. Path B:
  only `COL` differs, and it is `DATE`.
- **Primary key.** `primary_key` is equal.
- **Column defaults.** `column_details[].default` of each column equals the
  value recorded for `B.T` in section 1. A missing default is a mismatch.
- **Layout.** `definition.timePartitioning` and `definition.clustering` of the
  copy are as requested.
- **Policy tags.** The tags of each column, read from BigQuery in section
  4c, equal the tags recorded for the source in section 1. A source column
  with a tag that the copy lacks stops the conversion. `policyTagState` is
  not part of this check.
- **Table IAM bindings.** The bindings of the copy equal the recorded ones.
- **Path B, NULL count.** The count of NULL in `COL` is equal on both tables.

kbagent cannot read IAM bindings. The operator reads them on the copy:

```bash
bq --project_id=GCP_PROJECT get-iam-policy --format=prettyjson DATASET.T_repart
```

Tables over 5 TiB, and every path B table, also get a row count per
partition. A cluster-only table has no partitions and skips it.

```bash
kbagent --json workspace query --project ALIAS --workspace-id WS_ID --full --sql "SELECT p, SUM(old_c) AS old_rows, SUM(new_c) AS new_rows FROM (SELECT PART_OLD AS p, COUNT(*) AS old_c, 0 AS new_c FROM T GROUP BY p UNION ALL SELECT PART_NEW AS p, 0 AS old_c, COUNT(*) AS new_c FROM T_repart GROUP BY p) GROUP BY p HAVING SUM(old_c) != SUM(new_c)"
```

`PART_OLD` and `PART_NEW` are the partition of a row as a DATE, on the old
and on the new table. Both follow the granularity that section 2 chose, so
the query counts per partition of the new table:

| Granularity | Partition expression |
|---|---|
| DAY | `DAY_EXPR` |
| MONTH | `DATE_TRUNC(DAY_EXPR, MONTH)` |

Build both from `DAY_EXPR` of section 1, with the type that `COL` has on
that table:

- Path A: `COL` has the same type on both tables, so `PART_OLD` and
  `PART_NEW` are the same expression.
- Path B: on the old table `COL` is a STRING, so `PART_OLD` uses
  `SAFE.PARSE_DATE` with the format string of section 4a. On the new table
  `COL` is a DATE, so `PART_NEW` uses `COL`.

Rows with NULL in `COL` form one group with `p` NULL on each table, and the
query compares the two groups like any other partition. An empty result
passes.

Right before the swap, count the rows of both tables once more with the
count query at the top of this section. It is the last command before the
swap commands of section 6. `old_rows` and `new_rows` must both equal the
row count recorded before the copy. On a difference, stop and do not swap: a
write landed after the copy. `rows_count` and `last_import_date` of `B.T`
must also equal the values recorded before the copy, but equal values there
do not replace the count.

## 6. Swap

**A person runs every `bq` command in this section that writes. The agent
that follows this runbook prints the command in full, waits for the person to
confirm that it was run, and then reads the result. It never runs the command
itself.**

### Before the swap: BigQuery allows the rename

A table that received streaming writes (section 3) is swapped only when all
of these hold:

- At least 5 hours passed since the last streaming write that the owner
  recorded in section 3.
- `streamingBuffer` is absent on `B.T`. The operator reads it again:

  ```bash
  bq --project_id=GCP_PROJECT show --format=prettyjson DATASET.T
  ```

- No DML statement runs on the table, and no job of the project writes it
  (the `job list` command of section 3).

When one of them does not hold, wait and check again. Do not swap. The 5
hours are a lower bound: when the swap job still fails because BigQuery
refuses the rename, do not re-run it at once. Read both tables first (the
commands of section 9) and confirm that neither table moved. Then wait, and
swap again only with the owner's yes.

A table that never received streaming writes skips the first two checks.

### Before the swap: outside the consumers' schedule

The swap is a chain of renames, so for a moment no table exists under the
name `T`. A reader that runs in that moment fails. This also holds for a
reader in a project that links the bucket. Run the swap at a time when no
reader recorded in section 1 is scheduled, and agree on that time with the
owner. Check that no reader runs now with the `job list` command of
section 3.

### The swap

```bash
kbagent --json storage swap-tables --project ALIAS --table-id B.T --target-table-id B.T_repart --branch 0 --dry-run
kbagent --json storage swap-tables --project ALIAS --table-id B.T --target-table-id B.T_repart --branch 0 --yes --timeout 3600
kbagent --json storage table-detail --project ALIAS --table-id B.T
```

After the swap `B.T` holds the new layout and `B.T_repart` holds the old
table. Then, in this order:

1. **Layout.** Verify `definition.timePartitioning` and `clustering` on
   `B.T`.
2. **Security objects.** The IAM bindings of section 4c and the propagated
   policy tags travelled with the rename. The operator reads them once more
   on `B.T` (the `get-iam-policy` and policy tag commands of section 1).
   Stop on a difference from the values recorded in section 1. The old
   physical table, now `T_repart`, keeps its own bindings.
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

Watch the next read of each consumer too. For every reader recorded in
section 1, wait for its next run after the swap and confirm that it ended
with success (the `job list` command above; `job detail` for a failed run).
kbagent does not see a reader in another project that links the bucket, or a
reader outside Keboola. Ask the owner to confirm the next read of each of
them.

When the table loads incrementally and its primary key contains `COL`,
recommend `partition_aware_import` in the output mapping to the owner. The
runbook does not change the configuration.

## 8. Keep the old table 7 days, then drop it

For these 7 days `B.T_repart` is a second copy of the data, personal data
included. A deletion request that arrives in this time applies to both
tables. Tell the owner: the owner applies it to `B.T` and to `B.T_repart`.

While `B.T_repart` exists, the rollback is a second swap. Before it:

- The owner pauses the writers again, as in section 3, and the operator
  runs the two checks at the top of section 6 (BigQuery allows the rename;
  outside the consumers' schedule).
- Rows that were loaded after the first swap exist only in the new table. A
  rollback does not move them: after it they are in `B.T_repart`, and `B.T`
  holds the old table as it was at the first swap. Tell the owner before the
  rollback. The owner loads those rows into `B.T` again after it, and
  `B.T_repart` is not dropped before that is done.

```bash
kbagent --json storage swap-tables --project ALIAS --table-id B.T --target-table-id B.T_repart --branch 0 --yes --timeout 3600
kbagent --json storage table-detail --project ALIAS --table-id B.T
```

After a rollback the original physical table is back at `B.T`, with its IAM
bindings. A person creates the search indexes again
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

Collect evidence. Nothing here writes to either table. The table detail
commands and the query only read. `workspace load` writes to the helper
workspace: it replaces the two views there, so it waits for a yes like every
other write.

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
