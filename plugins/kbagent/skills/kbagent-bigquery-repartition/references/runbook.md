# Runbook: convert one BigQuery table to a partitioned and clustered layout

Follow the sections in order. One run converts one table. Rules that hold in
every section are in [../SKILL.md](../SKILL.md), "Guardrails": every write
waits for an explicit yes, `--json` on every call, a timeout is never answered
with a re-run, a person runs every `bq` command that writes.

A cluster-only table that is converted in place is the exception: it runs
section 0 and then the in-place path of section 4, and skips sections 1 to
4d and 5 to 10. The in-place path holds its own preflight, verification and
rollback.

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
| `CFG_ID` | ID of the helper transformation (paths B and C) |
| `PART_OLD`, `PART_NEW` | Partition expression on the old and on the new table (section 5) |
| `START`, `END` | First partition of a range and first partition after it (sections 2a, 4 path C) |
| `T_sample`, `T_sample_repart` | Scratch tables of the size check (section 2a), in bucket `B` |
| `JOB_ID` | ID of a Storage job |
| `GCP_PROJECT` | Google Cloud project of the Keboola project |
| `DATASET` | BigQuery dataset of bucket `B` |

In the SQL of this runbook `T`, `T_repart`, `COL`, `PK`, `C1` and `OTHER`
stand for identifiers. A name that is not a plain identifier (it contains a
dash, or it is a reserved word such as `order`, `group` or `rows`) must be
quoted with backticks in BigQuery. A backtick inside a double-quoted shell
argument starts a command substitution, so pass such a query with
`--sql-file PATH` (`workspace query`, `transformation create`) and never
inside `--sql "..."`.

For a STRING partition column the owner gives the date format string, an
input of the skill. The default is `%Y-%m-%d`. Put it in place of
`%Y-%m-%d` in every query.

`DATASET` is in `sql_path` and `backend_path` of the table detail (section 1).
`GCP_PROJECT` is the first segment of `sql_path` only when the path has three
segments. Storage often leaves the project out, and then `sql_path` is
`` `DATASET`.`T` ``. So `GCP_PROJECT` comes from the helper workspace, which
section 1 creates before the `bq` reads: the filter there prints the project
of the workspace service account as `gcp_project`. Check it before any other
`bq` command: `bq --project_id=GCP_PROJECT show DATASET.T` must find the
table, and `tableReference.datasetId` in its output must equal `DATASET`.

A `workspace query` with `--full` returns the rows of a statement only as CSV
text with a header line, in `data.statements[].csv_data`. It has no `columns`
and no `rows`: a filter that reads `rows` sees nothing for every `--full`
query, and a check that expects an empty result then passes whatever the
data. Read the last statement with:

```bash
kbagent --json workspace query --project ALIAS --workspace-id WS_ID --full --sql "SELECT 1 AS x" | jq '.data.statements[-1] | {status, rows_affected, csv_data}'
```

`rows_affected` is the number of rows of the result. kbagent fetches the rows
only when it is above 0, so an empty result has no `csv_data` key at all,
with or without `--full`. A check that expects an empty result:

- **passes** when the envelope is a success, `status` is `completed` and
  `rows_affected` is 0. `csv_data` is then null.
- **fails** when the envelope is an error, when `status` is not `completed`,
  or when `rows_affected` is above 0. With rows, `csv_data` holds the header
  and the rows that the check found. When `rows_affected` is above 0 and
  `csv_data` is null, kbagent could not fetch the rows: the check still
  fails; run the query again to see them.

A `bq` command that ends with "not found" or with a permission error has read
nothing. It does not mean that there is nothing to carry. Treat the objects
as unknown and refuse the table (section 1, refuse rules).

`T_repart` lives in the same bucket as `T`. The swap refuses a pair of tables
from two buckets, and it refuses an alias. `swap-tables --dry-run` does not
show that: it only echoes the pair, and Storage checks it when the swap runs.

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
  | jq '.data | (.definition | if type=="object" then . else {} end) as $d | {backend, is_alias, typed_columns: ([($d.columns | if type=="array" then .[] else empty end) | select((.definition | type) == "object" and .definition.type != null)] | length), rows_count, data_size_bytes, primary_key, last_import_date, sql_path, backend_path, column_details, layout: ($d | {timePartitioning, rangePartitioning, clustering, requirePartitionFilter}), policy_tags: [($d.columns | if type=="array" then .[] else empty end) | {name, policyTagState}]}'
```

The filter prints `definition.requirePartitionFilter` as
`layout.requirePartitionFilter`. It still prints the other keys when
`definition` is null or is not an object (Storage has served `[]` in place of
an empty definition, mostly on untyped tables), or when `definition.columns`
is not a list: `layout` then holds nulls and `policy_tags` is empty.

Never print `definition.partitions`. It has one entry per physical partition
and is unbounded. Every other `storage table-detail` of this runbook drops
it with `jq 'del(.data.definition.partitions)'`.

Read from the result:

- `backend`. Refuse the table when it is not `bigquery`.
- `is_alias`. Refuse the table when it is true.
- `typed_columns`: the number of columns that carry a native type in
  `definition`. 0 means that the table is untyped. The table detail has no
  other key that says so, and on an untyped table `column_details` is not a
  schema: `native_type`, `nullable` and `length` are missing, or they are
  hints that a component wrote to the column metadata (a hint can say `DATE`
  on a column that BigQuery holds as STRING). So for an untyped table ignore
  those three keys and record every column as `STRING`, nullable, without a
  length and without a default. Sections 4b and 5 use the recorded values.
- `rows_count`, `data_size_bytes`, `primary_key`, `last_import_date`.
- `column_details[].native_type`: the type of every column. The type of `COL`
  decides the path. DATE, DATETIME or TIMESTAMP is path A. STRING is path B.
  On an untyped table every column is STRING, so an untyped table is path B.
- `column_details[].nullable`: whether each column accepts NULL. Record it
  per column. Section 5 compares it with the copy.
- `column_details[].length`: the length of a column type, where it has one
  (`100` of STRING(100), `38,9` of NUMERIC(38,9)). Record it per column.
  Path B passes it to the copy, and section 5 compares it.
  A table that has any `column_details[].default` is path B as well, whatever
  the type of `COL` is (section 4, path A, "Column defaults"). When `COL` is
  already DATE, DATETIME or TIMESTAMP, or the table is cluster only, it takes
  path B **without a type change** (section 4, path B).
- `definition.timePartitioning`, `rangePartitioning`, `clustering`: the
  current layout. Record `timePartitioning.expirationMs` when it is set: the
  copy does not inherit it (section 4).
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

The table detail does not say whether the bucket is linked from another
project: a table of a linked bucket has `is_alias: false`. Read the bucket:

```bash
kbagent --json storage bucket-detail --project ALIAS --bucket-id B | jq '.data | {backend, is_linked}'
```

Refuse the table when `is_linked` is true. A linked bucket is read-only in
this project, so it cannot hold `T_repart`.

Four refuse rules need only these two results: the backend is not BigQuery,
the bucket is linked, the table is an alias, or `requirePartitionFilter` is
true ("Refuse rules" below). Check them now. A table that is refused here
leaves nothing behind, because nothing was created yet.

A table with `data_size_bytes` over 5 TiB (5,497,558,138,880 bytes) is not
refused for its size. Section 2a measures a sample copy and decides between
path A (one copy, when it finishes in under 4 hours) and path C, the ranged
copy (section 4, path C): a single copy that runs past the 6-hour limit of a
BigQuery query fails. For a smaller table the owner may ask for the same
size check (for example a table just under 5 TiB that grows, or a pilot), or
choose path C directly. Record the reason for path C.

### Helper workspace

The `bq` reads below need `GCP_PROJECT`, and the helper workspace is its
source (section 0). So the workspace is created here, before those reads. It
is the first object of this run in the project:

```bash
kbagent --json workspace create --project ALIAS --name repartition-T \
  | jq 'if .data then {workspace_id: .data.workspace_id, gcp_project: (.data.user | fromjson | .project_id)} else {error} end'
```

**Never run `workspace create` without this filter, and never print its
response.** The response holds the key of the workspace service account
(`data.user` is the key JSON, with `private_key`). The filter prints only
`WS_ID` and `GCP_PROJECT`, or the error when the command failed.

The same key is in the result of the `workspaceCreate` Storage job. When
`workspace create` ends with `STORAGE_JOB_TIMEOUT`, never run `storage
job-detail` on that job without a filter. Wait for it with:

```bash
kbagent --json storage job-detail --project ALIAS --job-id JOB_ID --wait | jq '.data | {status, error}'
```

Then find the workspace by its name with `workspace list --project ALIAS`,
delete it, and run the filtered `workspace create` again: that is the only
way to read `GCP_PROJECT` without printing the key.

`workspace create` writes to the project, not to the table. It still waits
for a yes.

From here on the workspace `WS_ID` exists. Every refusal and every stop
before the copy (the rest of this section, section 2, section 4a) ends with
its deletion:

```bash
kbagent --json workspace delete --project ALIAS --workspace-id WS_ID --yes
```

### Row access policies (stop rule)

Read the row access policies of the table before any copy is created.
kbagent cannot read them. The operator runs:

```bash
bq --project_id=GCP_PROJECT ls --row_access_policies --format=prettyjson DATASET.T
```

A table without a policy prints the plain text `No row access policies
found.`, not JSON. That line is the "none" answer.

When the table has at least one row access policy, stop here. Delete the
helper workspace and create no copy. Name the policies to the owner. The table
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
kbagent --json sharing edges --project ALIAS
kbagent --json project info --project ALIAS
kbagent --json storage tables --project ALIAS --bucket-id B --include-usage
kbagent --json flow list --project ALIAS --with-schedules
kbagent --json schedule list --project ALIAS
kbagent --json storage tables --project ALIAS
```

- **Sharing.** Record every edge in which the bucket is the source, and the
  bucket's entry in `shared_buckets` of the same output: a bucket that is
  shared but not linked yet has no edge. The bucket detail has no `sharing`
  key, so this output is the record. It is empty, without an error, for a
  project that kbagent holds without a project ID (`project list` shows
  none): then ask the owner.
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
  schedules that run those configurations. Three more sources, because
  `flow list --with-schedules` shows only the schedules of `keboola.flow`
  flows: `schedule list` shows every schedule, also one that runs a
  configuration directly; `legacy_orchestrator_count` above 0 in the flow
  list means that the project has orchestrator flows that the list leaves
  out; and `flow triggers --project ALIAS --flow-id ID` shows a table
  trigger of a flow. `used_by` reads input and output mappings only. It does
  not see a transformation that reads `DATASET.T` directly in SQL: ask the
  owner for those.
- **Aliases.** kbagent shows that a table is an alias (`is_alias`), but not
  which table the alias points at. List every table of the project with
  `is_alias: true` from the last command, and ask the owner which of them
  point at `B.T`. Record them. Aliases in other projects exist only through
  linked buckets, which the sharing edges show.

  `swap-tables` does not move aliases. An alias keeps its physical table, so
  after the swap it reads the old, unpartitioned table, which is then named
  `B.T_repart` and is dropped on day 7. kbagent has no command that creates
  an alias. So for every alias of `B.T` the owner records its definition
  (bucket, name, columns, filter, column sync) and agrees to delete it and
  create it again on `B.T` right after the swap (section 6, step 4). Without
  that agreement the table is refused.

### Refuse rules

Refuse the table, and change nothing on it, when one of these holds. When
the helper workspace already exists, delete it (the `workspace delete`
command above):

- The backend of the table is not BigQuery.
- The bucket is linked from another project.
- The table is an alias.
- `definition.requirePartitionFilter` is true.
- The table has at least one row access policy. Tables with row access
  policies are not converted in this version.
- A column carries a policy tag in BigQuery and the project has no
  `bigquery-policy-tags` feature.
- A column carries a policy tag in BigQuery and the backend capability
  `policyTagsEnabled` is false or could not be read (section 1).
- At least one alias points at the table, and the owner has not recorded its
  definition or has not agreed to create it again after the swap.
- The owner cannot say which aliases point at the table.
- The BigQuery-native objects are unknown, because nobody could read them.
- The recorded statement of a search index is incomplete.
- The operator cannot apply the recorded IAM bindings to the copy.
- The copy was created without a policy tag that the source has (section 4c).
- No partition column and no clustering column is confirmed.
- The table goes through path B and has a RECORD (STRUCT) column or a
  REPEATED column (`type` and `mode` of `schema.fields` in the `bq show`
  output). `create-table --column` takes a name, a type and a length only,
  so path B cannot create such a column.
- The table takes path C (section 2a, or the owner's choice) and has no
  partition column: a cluster-only table has nothing to range on. Hand the
  table to the owner.
- The table takes path C and is partitioned by ingestion time
  (`definition.timePartitioning` without a `field`). Its partition is not a
  column, so neither the range filter nor the per-partition check can read
  it. Hand the table to the owner.

The only object that this section creates is the helper workspace, so a
refused table leaves nothing behind once the workspace is deleted.

### Partition values (after the refuse rules pass)

Run this subsection only when no refuse rule above holds. It loads the table
into the helper workspace as a view:

```bash
kbagent --json workspace load --project ALIAS --workspace-id WS_ID --table-id B.T --load-type view
kbagent --json workspace query --project ALIAS --workspace-id WS_ID --sql "SELECT COUNT(DISTINCT IF(DAY_EXPR BETWEEN DATE '1960-01-01' AND DATE '2159-12-31', DAY_EXPR, NULL)) AS days, COUNT(DISTINCT IF(DAY_EXPR BETWEEN DATE '1960-01-01' AND DATE '2159-12-31', DATE_TRUNC(DAY_EXPR, MONTH), NULL)) AS months, COUNT(DISTINCT IF(DAY_EXPR BETWEEN DATE '1960-01-01' AND DATE '2159-12-31', DATE_TRUNC(DAY_EXPR, YEAR), NULL)) AS years, COUNTIF(DAY_EXPR < DATE '1960-01-01' OR DAY_EXPR > DATE '2159-12-31') AS out_of_range, MIN(DAY_EXPR) AS min_value, MAX(DAY_EXPR) AS max_value, COUNTIF(COL IS NULL) AS null_count FROM T"
```

`workspace load` writes to the project (a view), not to the table. It still
waits for a yes.

`DAY_EXPR` is the day of a row as a DATE. It depends on the type of `COL`.
Replace it in every query of this runbook:

| Type of `COL` | `DAY_EXPR` |
|---|---|
| DATE | `COL` |
| DATETIME | `DATE(COL)` |
| TIMESTAMP | `DATE(COL)` (the UTC day, which is how BigQuery partitions a TIMESTAMP column) |
| STRING (paths B and C) | `SAFE.PARSE_DATE('%Y-%m-%d', COL)`, with the owner's format string in place of `%Y-%m-%d` |

For a STRING column run the profile query of section 4a first.

`min_value` and `max_value` are dates, also for a STRING column (a `MIN` of
the strings would be alphabetical). `days`, `months` and `years` count only
dates from 1960-01-01 to 2159-12-31. BigQuery puts every date outside that
range into one partition, `__UNPARTITIONED__`, and `out_of_range` counts
those rows.

A cluster-only table that goes through copy and swap skips the query, but it
still loads the view: sections 3 and 5 count rows through it. A cluster-only
table converted in place skips sections 1 to 4d and creates no workspace
(section 4, in-place path).

### Preflight record

```text
Project alias:
Table ID:
Class (cluster only / column ready / column not ready):
Path (A / B / C; for C the reason: size check of section 2a or the owner's choice):
Size check (over 5 TiB): F slot-hours, T_low, T_high, max_slots, measured concurrency:
rows_count:
data_size_bytes:
primary_key:
last_import_date:
Backend (must be bigquery):
Bucket linked (must be no):
Typed (typed_columns above 0) / untyped:
Columns and types:
Nullable per column (column_details[].nullable):
Length per column (column_details[].length):
Current layout (timePartitioning / rangePartitioning / clustering):
timePartitioning.expirationMs (when set):
requirePartitionFilter (must not be true; null on an unpartitioned table):
Column defaults (column_details[].default):
policyTagState per column (Storage, last observed):
Policy tags per column (bq show):
Project feature bigquery-policy-tags (yes / no):
Backend capability policyTagsEnabled (yes / no / unknown):
sql_path:
Sharing edges with the bucket as source:
Writers (used_by, flows, schedules):
Readers:
Aliases that point at the table (ID, columns, filter, column sync):
Owner creates the aliases again after the swap (yes / no aliases):
Distinct days / months / years, out_of_range, min, max, null_count of the partition column:
Table IAM policy JSON:
Row access policies (must be none):
Search indexes (CREATE statements):
Streaming buffer (present / absent):
Owner who pauses the writers:
```

## 2. Choose granularity

Use the `days`, `months`, `years`, `null_count` and `out_of_range` values of
section 1. Rows with NULL in `COL` go to one more partition (`__NULL__`), and
rows with a date outside 1960-01-01 to 2159-12-31 go to one more
(`__UNPARTITIONED__`). Both count toward the limit. So add 1 to each count
when `null_count` is above 0, and 1 more when `out_of_range` is above 0:

- `day_partitions` = `days`, plus those.
- `month_partitions` = `months`, plus those.
- `year_partitions` = `years`, plus those.

| Partitions | Granularity | Flag |
|---|---|---|
| `day_partitions` at most 4,000 | DAY | `--time-partitioning-type DAY` |
| `day_partitions` over 4,000, `month_partitions` at most 4,000 | MONTH | `--time-partitioning-type MONTH` |
| `month_partitions` over 4,000, `year_partitions` at most 4,000 | YEAR | `--time-partitioning-type YEAR` |
| `year_partitions` over 4,000 | Stop. Ask the owner. | |

The table gives the default. The owner may choose a coarser granularity than
the table gives (MONTH instead of DAY, or YEAR): record that the owner chose
it, and use `--time-partitioning-type MONTH` or `YEAR`. A finer granularity
than the table gives is never used.

On the stop of the last row, delete the helper workspace (the `workspace
delete` command of section 1). No other object exists yet.

BigQuery rejects a job that modifies more than 4,000 partitions. The limit is
per job. So it binds the copy, and it also binds the output-mapping load of
path B (section 4), which is a full load into the partitioned table. On path C
it binds each range: a range covers at most 4,000 partitions.

## 2a. Size check: path A or path C (tables over 5 TiB, or on the owner's request)

Run this section when `data_size_bytes` is over 5 TiB (5,497,558,138,880
bytes), or when the owner asks for it for a smaller table. It decides whether one path A copy finishes in
time, or whether the table is copied in ranges (path C). It runs before the
writers are paused: it reads `B.T` and writes only scratch tables.

A table over 5 TiB that section 1 sent to path B (a STRING or untyped
partition column, or a column default) skips the measurement and takes
path C: path B fills the whole table with one output-mapping load, the most
expensive form of the copy (see the measurement below), and path C is the
same fill split into ranges.

### Measured on 2026-11-25

A path A copy of a 9.23 GB, 40 M-row table into MONTH partitioning with two
clustering columns took 9 s: one BigQuery `INSERT` of 3.6 s and 302
slot-seconds, so 32.7 slot-s per GB, at an average of 84 slots. The job ran
on a Standard-edition reservation with autoscale up to 300 slots and no
baseline, so the cost is slot-hours, not bytes. One monthly range by
transformation read the whole unpartitioned source (8.9 GB for a range of
0.14 GB); its output mapping deduplicates on the primary key. A filtered
scan of the source cost 1.9 slot-s per GB, and the load through output
mapping 158.8 slot-s per GB written (32.7 for the `CREATE TABLE` plus 126.1
for the output mapping). Ranges cost about 5 times the slot time of a path A
copy.

These numbers come from one project and one table. The procedure below
measures the table at hand; use the numbers above only where it says so.

### Sample copy

The sample is one representative range of `B.T`, at least 10 GB: one recent
full month, or more months when one month is smaller. A smaller sample is
dominated by fixed overhead.

1. Create `B.T_sample` empty, with the columns of `B.T` and without a layout:
   the `create-table` of 4b with every column, its type, length, NOT NULL and
   primary key, and no `--time-partitioning-*` or `--clustering-field` flag.
2. When any column of `B.T` carries a policy tag (section 1), check the tags
   of the empty `B.T_sample` with the `bq show` filter of section 4c. When a
   tag is missing, drop `B.T_sample` (step 6) and do not fill it: take the
   rate and the concurrency of the 2026-11-25 measurement above instead, and
   tell the owner that the estimate rests on that measurement.
3. Fill it with one transformation job, as in path B, "Fill the table with a
   transformation", with these changes: the statement selects every column
   unchanged with the range filter, and the output mapping loads into
   `B.T_sample`.

   ```bash
   kbagent --json transformation create --project ALIAS --component-id keboola.google-bigquery-transformation \
     --name "repartition T sample" --sql "CREATE TABLE T_sample (PK STRING NOT NULL, COL DATE, C1 STRING, OTHER STRING) AS SELECT PK, COL, C1, OTHER FROM T WHERE PART_OLD >= DATE 'START' AND PART_OLD < DATE 'END'"
   kbagent --json transformation edit --project ALIAS --config-id CFG_ID --change-description "map repartition sample" \
     --storage '{"input": {"tables": [{"source": "B.T", "destination": "T"}]}, "output": {"tables": [{"source": "T_sample", "destination": "B.T_sample", "primary_key": ["PK"]}]}}'
   kbagent --json job run --project ALIAS --component-id keboola.google-bigquery-transformation --config-id CFG_ID
   kbagent --json job detail --project ALIAS --job-id JOB_ID | jq '.data | {id, status, isFinished, startTime, endTime, durationSeconds}'
   ```

   `PART_OLD` is the partition expression of section 5. Run `job run`
   without `--wait` and repeat `job detail` until `isFinished` is true (path
   C, step 5, says why). Then read `data_size_bytes` of `B.T_sample` with
   `storage table-detail`.
4. Copy the sample with the path A command into `B.T_sample_repart`, with
   the layout of section 2. Record the wall-clock time of the command, and
   the time just before and just after it in milliseconds since the epoch
   (`date +%s000`):

   ```bash
   kbagent --json storage create-table --project ALIAS --bucket-id B --name T_sample_repart \
     --source-table-id B.T_sample --time-partitioning-type DAY --time-partitioning-field COL \
     --clustering-field C1 --clustering-field C2 --primary-key PK --timeout 3600
   ```

5. The operator reads the BigQuery jobs of that window. Both commands only
   read:

   ```bash
   bq --project_id=GCP_PROJECT ls -j -a --min_creation_time=START_MS --max_creation_time=END_MS --format=prettyjson \
     | jq '[.[] | {id: .jobReference.jobId, statement: .statistics.query.statementType, start: .statistics.startTime, end: .statistics.endTime, slot_ms: .statistics.totalSlotMs, bytes: .statistics.query.totalBytesProcessed, reservation: .statistics.reservation_id}]'
   bq show --reservation --project_id=ADMIN_PROJECT --location=LOCATION RESERVATION
   ```

   The copy is the `INSERT` job of the window. `reservation` names the
   reservation as `ADMIN_PROJECT:LOCATION.RESERVATION`. The second command
   prints its slot capacity and its autoscale maximum: `max_slots` is the
   sum. When `reservation` is empty the job ran on demand; when the operator
   cannot read the reservation, ask the person who administers it. When
   neither is possible, the estimate uses only the measured concurrency.
6. Drop the scratch tables and the sample configuration:

   ```bash
   kbagent --json storage delete-table --project ALIAS --table-id B.T_sample --table-id B.T_sample_repart --dry-run
   kbagent --json storage delete-table --project ALIAS --table-id B.T_sample --table-id B.T_sample_repart --yes
   kbagent --json config delete --project ALIAS --component-id keboola.google-bigquery-transformation --config-id CFG_ID
   ```

   They hold a sample of the data, personal data included. Drop them right
   after the measurement, also when a step above failed. A sample job that
   has not finished is first read with `job detail` until `isFinished` is
   true, so its output mapping cannot create the table again after the drop.

### Estimate

From the `INSERT` job of step 5:

- `S` = `slot_ms` / 1000, the slot-seconds of the copy.
- `D` = (`end` - `start`) / 1000, the duration of the copy in seconds.
  `start`, `end` and `slot_ms` are milliseconds (since the epoch for the two
  times), printed as strings.
- `G_sample` = `data_size_bytes` of `B.T_sample` / 10^9.
- `G` = `data_size_bytes` of `B.T` / 10^9.
- rate `r` = `S` / `G_sample` (slot-s per GB; 32.7 on 2026-11-25).
- measured concurrency `c` = `S` / `D` (slots; 84 on 2026-11-25).
- slot time of the full copy `F` = 2 x `r` x `G` slot-seconds.
- `T_high` = `F` / `c`: the time when the copy gets only the slots that the
  sample got.
- `T_low` = `F` / `max_slots`: the time when the copy gets every slot of the
  reservation.

The factor 2 in `F` is a safety margin. The rate of the same copy on the same
table varied by a factor of 2 between runs: 25.7 slot-s per GB from a 1.7 GB
sample, 52.7 for the whole 9.2 GB table right after it, 32.7 on 2026-11-25.
One sample does not pin the rate closer than that. Keep the sample at 10 GB
or more: a smaller sample also gets fewer slots (21.9 against about 90 for
the whole table in the same run), which understates `c` and inflates
`T_high`.

Example with the 2026-11-25 numbers: 16 TiB (17,592 GB) gives `F` =
1,150,000 slot-s, 320 slot-hours with the factor, `T_high` = 3.8 h,
`T_low` = 1.1 h.

| Estimate | Path |
|---|---|
| `T_high` under 4 hours | Path A (section 4). |
| `T_high` 4 hours or more, `T_low` under 4 hours | The owner decides: path A when the reservation can give the copy more slots than the sample got for its whole duration, otherwise path C. |
| `T_low` 4 hours or more, or no `max_slots` and `T_high` 4 hours or more | Path C (section 4). |

Tell the owner `F` in slot-hours (with the factor 2, and the expected cost
without it), `T_low` and `T_high`, and the path, and record them. Assumptions that were not measured, to name with the estimate:

- The time grows linearly with the bytes. The sample is about 1,000 times
  smaller than the table; the sort of a clustered multi-TiB copy can grow
  faster than linearly.
- The reservation is shared, probably by other projects of the stack. A
  copy of several hours holds its slots for that time and slows the other
  projects on it; the owner agrees on the time.
- BigQuery stops a query after 6 hours. Whether the edition of the
  reservation changes that, and how long Connection lets a Storage job of
  `create-table --source-table-id` run, were not verified.

A path A copy of a table over 5 TiB runs as in section 4. Its `--timeout`
only bounds the wait of kbagent: on `STORAGE_JOB_TIMEOUT` the copy still
runs, and the operator waits for it with `storage job-detail --wait`. When
the copy fails because BigQuery stopped it, run the abort step (section
4d). A new attempt starts again at section 1 and takes path C, with the
failed copy recorded as the reason.

## 3. Pause writers (owner)

The owner stops every writer recorded in section 1: loads, Direct Grant
output mapping writes, and streaming writers on the owner's side. The skill
changes no configuration and pauses nothing.

On path C the copy runs range by range, one job each, and can take days. The
writers stay paused from the first range to the swap (section 6). Tell the
owner how long the window is from the estimate of section 4, path C, step 5,
before the owner pauses anything. That estimate needs the sample range, which
is copied while the writers already pause: the sample is the first range.

Check that no job runs before the copy:

```bash
kbagent --json job list --project ALIAS --limit 100 | jq '[.data.jobs[] | select(.isFinished != true) | {id, status}]'
```

The result must be empty. The filter also shows a job that is created or
waiting and has not started yet; `--status processing` would miss it.

- **Streaming.** Ask the owner for the time of the last streaming write and
  record it. Rows that were streamed recently can be invisible to the copy
  for up to 90 minutes. The copy starts only when both hold: at least 90
  minutes passed since the last streaming write, and `streamingBuffer` is
  absent. The operator reads it with the `bq show` command of section 1.
  BigQuery refuses to rename a table for about 5 hours after the last
  streaming write, sometimes longer. Section 6 checks this before the swap.
- **DML.** A running DML statement on the table blocks the rename.

Section 1 read the partition values before the writers were paused. Now
that they are paused, run the partition-value query of section 1 again, and
for a STRING column the two queries of section 4a. `bad_values` must be 0,
and the counts must give the granularity that section 2 chose. When they do
not, go back to section 2, or stop as section 4a says.

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

A cluster-only table that is converted in place is not copied. It uses the
in-place path below and skips the rest of this section.

### In-place path: change the clustering of a cluster-only table

*(since vNEXT)* `kbagent storage clustering-set` changes the clustering of
the table itself. This is the default path of a cluster-only table.

**When.** The table needs clustering columns and no partitioning change: no
time or range partitioning is requested, or the partitioning stays as it is.
The class of the table is "Cluster only" ([../SKILL.md](../SKILL.md)).

**What it does not need.** There is no copy, no swap and no old table to keep
for 7 days. The writers are not paused. The table keeps its ID and its data,
so its IAM bindings, row access policies, search indexes, policy tags and
aliases stay on the same table. No `bq` step is needed. The column defaults
stay too, so a table with a column default does not need path B here, and the
size check of section 2a does not apply to a table over 5 TiB: no data is
copied.

**Trade-off.** BigQuery clusters the data that is written after the change.
The rows that are already in the table keep their order until they are
rewritten, for example by a full load. Tell the owner before the change.
Applying a new clustering also returns a table that is in long-term storage
to active storage pricing. Tell the owner that as well.
When the owner wants every existing row reclustered at once, use copy and
swap instead: sections 1 to 10, with path A without the partitioning flags.

**Availability.** Check that the installed kbagent has the command:

```bash
kbagent storage clustering-set --help
```

When this fails (exit 2, no such command), the kbagent is older than the
release that ships the command. Tell the operator, and fall back to copy and
swap: sections 1 to 10, with path A without the partitioning flags.

**Preflight.** Nothing here writes. Section 0 still applies: no dev branch is
active for the project.

```bash
kbagent --json storage table-detail --project ALIAS --table-id B.T \
  | jq '.data | (.definition | if type=="object" then . else {} end) as $d | {is_alias, sql_path, column_details, layout: ($d | {timePartitioning, rangePartitioning, clustering})}'
kbagent --json storage bucket-detail --project ALIAS --bucket-id B
```

- `is_alias` is true: stop. An alias has no clustering of its own. Run this
  section on the source table of the alias, in the project that owns it.
- `is_linked` of the bucket is true, or the bucket is an external bucket:
  stop. The table belongs to another project or to a dataset outside
  Storage. The owner of the source changes the clustering there.
- `C1`, `C2` must be columns of the table (`column_details[].name`), at most
  4, in the order that the owner confirmed.
- Record `layout.clustering`: the current clustering columns in order
  (`clustering.fields`), or none. This is the value for the rollback.
- `layout.timePartitioning` and `layout.rangePartitioning` stay as they are.
  The command does not change them.

**Dry run, then the change.** The dry run writes nothing:

```bash
kbagent --json storage clustering-set --project ALIAS --table-id B.T \
  --clustering-field C1 --clustering-field C2 --dry-run
```

Check in the result that `clustering_before` equals the recorded clustering
and that `clustering` lists the requested columns in order. Show it to the
owner. After the owner's yes:

```bash
kbagent --json storage clustering-set --project ALIAS --table-id B.T \
  --clustering-field C1 --clustering-field C2
```

- `status` is `updated`: the job ran and the clustering is set.
- `status` is `unchanged`: the table already had the requested clustering,
  and nothing was written.
- Exit 2 with `INVALID_ARGUMENT`: Storage refused the change (not a BigQuery
  table, an alias, an external bucket, an unknown column, a column twice, or
  more than 4 columns). The table is unchanged. Stop and report the message.
- Exit 4 with `STORAGE_JOB_TIMEOUT`: the job still runs. Wait for it with
  the `storage job-detail --wait` command of path A below, then verify.

**Verify.**

```bash
kbagent --json storage table-detail --project ALIAS --table-id B.T \
  | jq '.data.definition | {timePartitioning, rangePartitioning, clustering}'
```

`clustering.fields` lists the requested columns in order, and the
partitioning equals the recorded one. Record the clustering before and
after.

Run the same command once more after the next full load of the table. The
table detail must still show the requested clustering: a full load rewrites
the rows, and this is the first point at which all of them are clustered.
When the clustering is missing or differs, stop and report it to the owner.

**Rollback.** Set the recorded previous clustering, with `P1`, `P2` as the
recorded columns in their order. When the table had no clustering before,
remove it with `--clear`. Each is a write and waits for a yes:

```bash
kbagent --json storage clustering-set --project ALIAS --table-id B.T \
  --clustering-field P1 --clustering-field P2
kbagent --json storage clustering-set --project ALIAS --table-id B.T --clear
```

Run one of the two, then verify as above. A rollback has the same
trade-off: rows that were written under the new clustering keep their order
until they are rewritten.

The run of a cluster-only table converted in place ends here. It skips
sections 4c to 10.

### Path A: the partition column is DATE, DATETIME or TIMESTAMP, or cluster only

```bash
kbagent --json storage create-table --project ALIAS --bucket-id B --name T_repart \
  --source-table-id B.T --time-partitioning-type DAY --time-partitioning-field COL \
  --clustering-field C1 --clustering-field C2 --primary-key PK --timeout 3600
```

Use the granularity of section 2. For a cluster-only table omit both
`--time-partitioning-*` flags. A cluster-only table takes this path only when
the owner wants the existing rows reclustered at once, or when the installed
kbagent has no `storage clustering-set` (the in-place path above).

`--primary-key` takes one column. Repeat it for every column of
`primary_key` recorded in section 1, in the same order, and omit it when the
table has no primary key. A comma-separated list is sent as one column name.

When section 1 recorded `timePartitioning.expirationMs`, add
`--time-partitioning-expiration-ms` with that value. The copy does not
inherit the expiration, and without the flag the new table keeps its
partitions for ever. Leave it out only when the owner asks to drop the
expiration. Both rules hold for the `create-table` of path B too (4b).

The copy keeps the columns, their types and NOT NULL of the source. It keeps
the primary key that the command requests.

**Column defaults.** `create-table --source-table-id` refuses `--default`
(`services/storage_service.py`: "`--default` is not valid with
`--source-table-id`"), and the copy does not inherit a default from the
source. So when any column recorded in section 1 has a
`column_details[].default`, a path A copy can never satisfy the column-default
check of section 5. Take the table through **path B** instead: it lists the
columns explicitly and repeats every default with `--default name=value`.
Refuse the table only when path B is not possible either.

On exit 4 with `STORAGE_JOB_TIMEOUT` the job still runs. Wait for it, and
never re-run the copy:

```bash
kbagent --json storage job-detail --project ALIAS --job-id JOB_ID --wait --timeout 3600
```

When the copy ends in any other error, or the job that you waited for ends
with `STORAGE_JOB_FAILED`: stop, and never re-run the copy. Run the abort
step (section 4d). It reads whether `B.T_repart` exists, drops it when it
does, and deletes the helper workspace. A new attempt starts again at
section 1.

Record the duration of the copy as the wall-clock time of the command. Its
output carries no job ID and no job times.

### Path B: typed empty table filled by a transformation

Path B has two variants. Decide which one applies before 4a:

| Variant | When | What differs |
|---|---|---|
| With a type change | `COL` is a STRING, or the table is untyped. | 4a runs. In 4b `COL` becomes `DATE`. The fill parses `COL`. |
| Without a type change | The table is here only because of a column default: `COL` is already DATE, DATETIME or TIMESTAMP, or the table is cluster only. | Skip 4a. In 4b every column, `COL` included, keeps its type. The fill selects every column unchanged. |

#### 4a. Profile the column (variant with a type change only)

The format string is the input from the owner (section 0). The default is
`%Y-%m-%d`.

```bash
kbagent --json workspace query --project ALIAS --workspace-id WS_ID --sql "SELECT COUNTIF(COL IS NOT NULL AND SAFE.PARSE_DATE('%Y-%m-%d', COL) IS NULL) AS bad_values, COUNTIF(COL IS NULL) AS null_count, COUNT(*) AS total FROM T"
kbagent --json workspace query --project ALIAS --workspace-id WS_ID --sql "SELECT COL, COUNT(*) AS n FROM T WHERE COL IS NOT NULL AND SAFE.PARSE_DATE('%Y-%m-%d', COL) IS NULL GROUP BY COL ORDER BY n DESC LIMIT 20"
```

When `bad_values` is above 0: stop and report the sample values of the second
query to the owner. No table is created. Delete the helper workspace (the
`workspace delete` command of section 1).

#### 4b. Create the empty typed table with the layout

Every column keeps its type from `column_details`, with its length: a column
recorded with a `length` in section 1 is passed as `--column NAME:TYPE(length)`,
for example `--column NAME:STRING(100)` or `--column AMOUNT:NUMERIC(38,9)`. A
bare `--column NAME:STRING` creates the column without the length limit of the
source. On an untyped table every column is `STRING`. With a type change `COL`
becomes `DATE`; without one it keeps its type and length like every other
column, and a cluster-only table gets no `--time-partitioning-*` flags. NOT NULL and the primary key are the
same as on the source: pass one `--not-null-column` for every column recorded
with `nullable: false` in section 1. Every column default recorded in section 1
(`column_details[].default`) is repeated with `--default name=value`: the copy
does not inherit a default, and a column that loses it writes NULL on the next
insert that omits it, or fails its NOT NULL constraint.

```bash
kbagent --json storage create-table --project ALIAS --bucket-id B --name T_repart \
  --column "PK:STRING(64)" --column COL:DATE --column C1:STRING --column OTHER:STRING \
  --primary-key PK --not-null-column PK --default OTHER=0 \
  --time-partitioning-type DAY --time-partitioning-field COL --clustering-field C1
```

List the columns in the order of the source.

#### Fill the table with a transformation (after 4b)

When any column of `B.T` carries a policy tag (section 1), check the tags of
the empty `B.T_repart` with the `bq show` filter of section 4c **before the
fill**, and refuse the table when a tag is missing (abort step, section 4d). Output mapping does not
propagate policy tags, so a filled but untagged copy exposes the protected
values of every row to anyone with dataset read access, for the whole window
until section 4c refuses and the copy is dropped. On an empty table that
window carries no data.

A BigQuery SQL transformation reads `B.T` and its output mapping loads into
`B.T_repart`. The load is a full load, and the table keeps its layout. Create
the transformation without `--created-table`, then set both mappings:

```bash
kbagent --json transformation create --project ALIAS --component-id keboola.google-bigquery-transformation \
  --name "repartition T" --sql "CREATE TABLE T_repart (PK STRING NOT NULL, COL DATE, C1 STRING, OTHER STRING) AS SELECT PK, PARSE_DATE('%Y-%m-%d', COL) AS COL, C1, OTHER FROM T"
kbagent --json transformation edit --project ALIAS --config-id CFG_ID --change-description "map repartition tables" \
  --storage '{"input": {"tables": [{"source": "B.T", "destination": "T"}]}, "output": {"tables": [{"source": "T_repart", "destination": "B.T_repart", "primary_key": ["PK"]}]}}'
kbagent --json job run --project ALIAS --component-id keboola.google-bigquery-transformation --config-id CFG_ID --wait --timeout 3600
kbagent --json config delete --project ALIAS --component-id keboola.google-bigquery-transformation --config-id CFG_ID
```

`primary_key` of the output mapping lists the same columns, in the same
order, as `--primary-key` of section 4b. An output mapping whose key differs
from the key of the destination table does not load as it is. Omit
`primary_key` only when the table has no primary key.

The fill uses `PARSE_DATE`, not `SAFE.PARSE_DATE`: a value that does not
parse fails the job instead of becoming NULL without a trace. Section 4a
found no such value, so a failure here means that a write landed after it.

The `CREATE TABLE` statement names every column with its type, and `NOT
NULL` for every column recorded with `nullable: false` in section 1. Use the
types of the definition in 4b, written as BigQuery SQL types: `INTEGER` is
`INT64`, `FLOAT` is `FLOAT64`, `BOOLEAN` is `BOOL`; `STRING`, `NUMERIC`,
`BIGNUMERIC`, `DATE`, `DATETIME`, `TIME`, `TIMESTAMP`, `BYTES`, `JSON` and
`GEOGRAPHY` keep their name. A plain `CREATE TABLE T_repart AS SELECT ...`
creates every column nullable; the output mapping then tries to change the
NOT NULL columns of the table from 4b, and the job fails ("column's mode
changed: REQUIRED -> NULLABLE"). Write the types without a length and
without a default. Whether the load keeps the length and the default that 4b
set was not verified: the Length and Column defaults checks of section 5
catch a loss.

Without a type change the SELECT lists every column unchanged, `COL`
included, and the column list keeps the type of `COL`:
`CREATE TABLE T_repart (PK STRING NOT NULL, COL DATE, C1 STRING, OTHER STRING) AS SELECT PK, COL, C1, OTHER FROM T`.

The column list in the SELECT is the full column list of the source, in the
order of the source. Run `config delete` only after a successful job: it
moves the helper configuration to the trash. When the job fails, stop and
run the abort step (section 4d), which drops the copy, this configuration
and the helper workspace.

A `job run --wait` that ends with exit 7 (`JOB_TIMEOUT_TERMINATED`) or exit
4 (`QUEUE_JOB_TIMEOUT`) did not see the job end: after a timeout kbagent
asks the queue to kill the job and waits only a few seconds. A job that is
still ending can finish its output mapping after the copy was dropped, and
create `B.T_repart` again without the layout and without the policy tags.
So before section 4d, read the job until it is finished:

```bash
kbagent --json job detail --project ALIAS --job-id QUEUE_JOB_ID | jq '.data | {status, isFinished}'
```

`QUEUE_JOB_ID` is the ID of the transformation job (in the output or in the
error details of `job run`). Start section 4d only when `isFinished` is
true.

kbagent has no command that imports a workspace table into a Storage table.
That is why the fill goes through a transformation and its output mapping.

With a type change, after the swap the owner changes the writers of `B.T`,
so that they write a DATE into `COL`. The runbook does not change them.
Without a type change the writers stay as they are.

### Path C: ranged copy

Path C applies when the size check of section 2a chose it, or when the
owner chose it (section 1). It copies the table in ranges of the partition
column, one transformation job per range, into the empty table of path B,
step 4b. Each range stays under 4 hours, below the 6-hour limit of a
BigQuery query. Section 1 refuses a path C table that is cluster only or
partitioned by ingestion time.

Path C reuses path B: the variant with or without a type change is decided
the same way (the table at the top of path B), 4a runs for the variant with a
type change, and the table is created by 4b. What differs is the fill: it
runs once per range, with an incremental output mapping.

`PART_OLD` and `PART_NEW` are the partition expressions of section 5, on the
old and on the new table. Both follow the granularity of section 2.

#### Step 1. Create the empty table

Run 4a (variant with a type change only) and 4b exactly as on path B. Then
load both tables into the helper workspace as views; the view of `T_repart`
counts the rows of each range after its load:

```bash
kbagent --json workspace load --project ALIAS --workspace-id WS_ID --table-id B.T --table-id B.T_repart --load-type view
```

#### Step 2. Secure the empty table

Run section 4c now, on the empty `B.T_repart`, not after the fill: the
policy tags check and the IAM bindings. The copy fills over hours or days, and
it must not hold rows without its tags and bindings in that time. When a step
of 4c cannot be done, run the abort step (section 4d).

#### Step 3. Plan the ranges

Count the rows per partition of the new layout:

```bash
kbagent --json workspace query --project ALIAS --workspace-id WS_ID --full --sql "SELECT PART_OLD AS p, COUNT(*) AS n FROM T GROUP BY p ORDER BY p"
```

`--full` returns every row: the default stops at 500. The rows are in
`csv_data` of the last statement, not in `rows`, and `rows_affected` counts
them (section 0). When `rows_affected` is above 0 and `csv_data` is null, the
rows were not fetched: run the query again. A row with `p` NULL (an empty
first field) is the NULL partition.

A range is a run of consecutive partitions from this list, half-open: it
holds the rows with `START <= PART_OLD < END`. `START` is the first partition of
the range, and `END` is the first partition of the next range. For the last
range `END` is one granularity unit after its last partition (the next day,
month or year). A range never splits a partition, so a failed range can be
removed without touching its neighbours (step 6). The NULL partition is its
own range, with the filter `COL IS NULL`. When section 1 counted
`out_of_range` above 0, the rows with a date outside 1960-01-01 to 2159-12-31
are one more range of their own, with the filter
`DAY_EXPR < DATE '1960-01-01' OR DAY_EXPR > DATE '2159-12-31'` (`DAY_EXPR` of
the table that is read). BigQuery holds them in one partition,
`__UNPARTITIONED__`. The other ranges cover only dates in that interval.

Each range covers at most 4,000 partitions (section 2). Start with one month
of data per range for the sample (step 5), and size the other ranges from its
measurement. The estimated size of a range is its rows divided by
`rows_count`, times `data_size_bytes`.

Write the range record and keep it up to date for the whole run:

```text
Range | START | END (exclusive) | Source rows | Copy rows | Job ID | Start time | End time | Duration | Status (todo / running / done / failed)
1     |       |                 |             |           |        |            |          |          |
NULL  | COL IS NULL                |       |           |        |            |          |          |
OUT   | outside 1960-01-01 to 2159-12-31 |  |           |        |            |          |          |
```

Source rows of a range are the sum of `n` of its partitions. Their sum over
all ranges equals the row count before the copy (section 3).

#### Step 4. Create the transformation for the first range

As in path B, "Fill the table with a transformation", with two differences:
the `SELECT` has a range filter, and the output mapping is incremental, so
each range adds rows to the copy instead of replacing it.

```bash
kbagent --json transformation create --project ALIAS --component-id keboola.google-bigquery-transformation \
  --name "repartition T ranges" --sql "CREATE TABLE T_repart (PK STRING NOT NULL, COL DATE, C1 STRING, OTHER STRING) AS SELECT PK, PARSE_DATE('%Y-%m-%d', COL) AS COL, C1, OTHER FROM T WHERE PART_OLD >= DATE 'START' AND PART_OLD < DATE 'END'"
kbagent --json transformation edit --project ALIAS --config-id CFG_ID --change-description "map repartition tables" \
  --storage '{"input": {"tables": [{"source": "B.T", "destination": "T"}]}, "output": {"tables": [{"source": "T_repart", "destination": "B.T_repart", "primary_key": ["PK"], "incremental": true}]}}'
kbagent --json transformation show --project ALIAS --config-id CFG_ID
```

The rules of path B for the `CREATE TABLE` column list, the full column list
of the `SELECT` and `primary_key` hold unchanged. Without a type change the
`SELECT` lists `COL` unchanged. The filter uses `PART_OLD`, the same expression
as the range plan, so a row lands in exactly one range. The NULL range and
the out-of-range range use their filters of step 3. The fill parses with
`PARSE_DATE`, as on path B: a value that does not parse fails the range.

`transformation show` prints the block and code IDs (`b0`, `b0.c0`) that
step 6 edits.

The output mapping has the primary key of the table, so each incremental load
merges on it. A row that is loaded twice is not duplicated. The load of a
later range compares against the rows already in the copy, so later ranges
may take longer than the sample: record the duration of each.

#### Step 5. Sample range

Run the first range and time it:

```bash
kbagent --json job run --project ALIAS --component-id keboola.google-bigquery-transformation --config-id CFG_ID
kbagent --json job detail --project ALIAS --job-id JOB_ID | jq '.data | {id, status, isFinished, startTime, endTime, durationSeconds}'
```

Run `job run` without `--wait`. With `--timeout`, kbagent kills the job when
the timeout expires (exit 7, `JOB_TIMEOUT_TERMINATED`), and a range may run
for hours. Take `JOB_ID` from the output of `job run`, and repeat
`job detail` until `isFinished` is true.

Then count the rows of the range in the copy:

```bash
kbagent --json workspace query --project ALIAS --workspace-id WS_ID --sql "SELECT COUNT(*) AS copy_rows FROM T_repart WHERE PART_NEW >= DATE 'START' AND PART_NEW < DATE 'END'"
```

The NULL range and the out-of-range range use their filters of step 3, with
`COL` of the new table. The range is `done`
when the job ended with `success` and `copy_rows` equals its source rows.
Record the job ID, the times and `durationSeconds` in the range record.

From the sample, size the other ranges so that each is estimated under 4
hours, with at most 4,000 partitions. Plan the fewest ranges that meet this:
every range scans the whole source (see "Cost" below). The time of a range
has a part that every range pays, the scan of the whole source, and a part
that grows with the range:

- range time = scan + write x (range GB / sample GB), where scan and write
  split `durationSeconds` of the sample.
- Split it with the 2026-11-25 measurement (section 2a): scan = 1.9 x
  source GB, write = 158.8 x sample GB, both in slot-seconds; scale both so
  that their sum, divided by the concurrency, plus 60 s of Keboola overhead,
  equals `durationSeconds` of the sample.
- Estimated total time = sum over the ranges. Tell the owner, together with
  the length of the writers' pause.

When the sample took more than 4 hours, stop and ask the owner before any
other range. The owner chooses smaller ranges or ends the conversion (abort,
section 4d).

The first measurement includes the input mapping of `B.T` into the
transformation. Whether a BigQuery transformation reads its input table as a
view or copies it was not verified for this runbook. When the sample is far
slower than its share of the table suggests, read the job events
(`kbagent --json job detail --project ALIAS --job-id JOB_ID --log-tail-lines 50`)
and stop when the input mapping copies the table: every range would then
copy all of it.

#### Step 6. The other ranges

For each range in order, set the filter of the range and run it:

```bash
kbagent --json transformation edit --project ALIAS --config-id CFG_ID --change-description "range START to END" --op-file range.json
kbagent --json job run --project ALIAS --component-id keboola.google-bigquery-transformation --config-id CFG_ID
kbagent --json job detail --project ALIAS --job-id JOB_ID | jq '.data | {id, status, isFinished, startTime, endTime, durationSeconds}'
```

`range.json` is a JSON array with one operation. It replaces the script of
the transformation with the statement of step 4 and the filter of this range:

```json
[{"op": "set_code", "block_id": "b0", "code_id": "b0.c0", "script": "CREATE TABLE T_repart (PK STRING NOT NULL, COL DATE, C1 STRING, OTHER STRING) AS SELECT PK, PARSE_DATE('%Y-%m-%d', COL) AS COL, C1, OTHER FROM T WHERE PART_OLD >= DATE 'START' AND PART_OLD < DATE 'END'"}]
```

Each `transformation edit` and each `job run` is a write and waits for its
own yes, like every write of this runbook.

After each job, count the copy rows of the range (step 5) and update the range
record. Start the next range only when this one is `done`.

When a range runs longer than 4 hours, stop and ask the owner. Do not start
the next range. The owner decides whether to wait or to end the job:

```bash
kbagent --json job terminate --project ALIAS --job-id JOB_ID --dry-run
kbagent --json job terminate --project ALIAS --job-id JOB_ID --yes
```

#### Resume after a failed range

A range whose job ended in any status other than `success`, or whose
`copy_rows` differs from its source rows, is `failed`. The ranges before it
stay `done`. Resume from the first range that is not `done`:

1. Confirm with `job detail` that the job of the failed range has
   `isFinished` true. Never start a range while one still runs.
2. Count the copy rows of the failed range (step 5). The load of a range is
   not one transaction, so a failed job can leave part of its rows in the
   copy.
3. When the count is above 0, a person removes them. This is a `bq` write: the
   agent prints the command, waits for the person to confirm that it ran, and
   counts again. A person runs:

   ```bash
   bq --project_id=GCP_PROJECT query --use_legacy_sql=false 'DELETE FROM DATASET.T_repart WHERE PART_NEW >= DATE "START" AND PART_NEW < DATE "END"'
   ```

   The NULL range and the out-of-range range use their filters of step 3.
   The count of step 5 must then be 0.
4. Run the range again (step 6), with the owner's yes for the retry.

When the same range fails twice, stop and ask the owner. Ending the
conversion is the abort step (section 4d).

The `DELETE` goes around Storage, so `rows_count` of `B.T_repart` may be
wrong until the next load. Section 5 counts the rows with a query and does not
rely on it.

#### Step 7. After the last range

Every range of the record is `done`. Delete the helper configuration:

```bash
kbagent --json config delete --project ALIAS --component-id keboola.google-bigquery-transformation --config-id CFG_ID
```

Then continue with section 5. Section 4c already ran on the empty table, and
section 5 checks its result again on the full copy.

#### Cost

- Each range reads the source. When the source is not partitioned by `COL`,
  each range bills a full scan of every column, so the scan cost is the number
  of ranges times one full scan. Plan the fewest ranges that stay under the
  time budget of step 5, and tell the owner the estimate before the loop.
- Measured on 2026-11-25 (section 2a): the scan costs 1.9 slot-s per GB of
  source per range, the load through output mapping 158.8 slot-s per GB
  written, against 32.7 for a path A copy. On 16 TiB that is 785-804
  slot-hours for path C against 160 for path A; 66 monthly ranges would scan
  1,056 TiB. Prefer few, large ranges.
- Each incremental load with a primary key merges into the copy, and its cost
  grows with the rows already in it.
- The writers stay paused for the whole copy, the validation and the swap.

## 4c. Secure the copy before the swap (a person with BigQuery access)

**A person runs every `bq` command in this section that writes. The agent
that follows this runbook prints the command in full, waits for the person to
confirm that it was run, and then reads the result. It never runs the command
itself.**

This section runs after the copy of path A or path B, and before section 5.
On path C it runs on the empty table, before the first range (section 4,
path C, step 2), and section 5 checks its result again.
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

2. **Table IAM bindings.** Skip this step when the recorded `bindings` list
   is empty: there is nothing to carry. Otherwise a person applies the
   recorded policy to the copy. Save the policy JSON of section 1 to a file and remove its `etag` key
   first. A person runs:

   ```bash
   bq --project_id=GCP_PROJECT set-iam-policy DATASET.T_repart T.iam.json
   ```

3. **Search indexes** are not created here. A rename drops them, so they are
   created again in section 6.

When one of these steps cannot be done, stop. Do not swap. Run the abort
step (section 4d), and refuse the table.

## 4d. Abort before the swap

Every stop between the copy and the swap ends here: a failed path A copy, a
failed path B job, a path C copy that the owner ends instead of resuming, a
missing policy tag (section 4b or 4c), a step of section 4c that cannot be
done, a failed check of section 5. Until this step runs, `B.T_repart` is a
full second copy of the data, personal data included, possibly without its
policy tags, and readable by every dataset reader.

Run it only before the swap. After the swap `B.T_repart` is the old
production table, and section 8 decides about it.

With the owner's yes, in this order:

```bash
kbagent --json storage table-detail --project ALIAS --table-id B.T_repart | jq 'del(.data.definition.partitions)'
kbagent --json storage delete-table --project ALIAS --table-id B.T_repart --dry-run
kbagent --json storage delete-table --project ALIAS --table-id B.T_repart --yes
kbagent --json config delete --project ALIAS --component-id keboola.google-bigquery-transformation --config-id CFG_ID
kbagent --json workspace delete --project ALIAS --workspace-id WS_ID --yes
```

- The copy goes first: it is the object that holds data. After a failed
  path A copy the table may not exist: when `table-detail` answers
  `NOT_FOUND`, skip both `delete-table` commands.
- `config delete` applies to paths B and C, and only when `CFG_ID` exists:
  skip it when the abort came before `transformation create` (the policy-tag
  check on the empty table), and when section 4 already deleted the
  configuration.
- After a failed or timed-out fill job, the job is finished before this
  step starts (section 4, "Fill the table with a transformation"). On path C
  this holds for the job of every range: `isFinished` is true (the
  `job detail` command of path C, step 5).
- Read `B.T` once more with `storage table-detail` and confirm that it is
  unchanged: the layout and `rows_count` of section 1.
- Tell the owner that the dropped copy stays reachable through BigQuery time
  travel for a time (section 8), and that the writers can resume.

## 5. Validate (refuse the swap on any mismatch)

```bash
kbagent --json storage table-detail --project ALIAS --table-id B.T_repart | jq 'del(.data.definition.partitions)'
kbagent --json workspace load --project ALIAS --workspace-id WS_ID --table-id B.T --table-id B.T_repart --load-type view
kbagent --json workspace query --project ALIAS --workspace-id WS_ID --sql "SELECT (SELECT COUNT(*) FROM T) AS old_rows, (SELECT COUNT(*) FROM T_repart) AS new_rows"
```

Every check must pass. On a mismatch, stop, do not swap, and run the abort
step (section 4d): it drops the copy, the helper configuration and the
helper workspace.

- **Row count.** `old_rows` equals `new_rows`, and both equal the row count
  recorded right before the copy (section 3). A difference means that a
  write landed.
- **Columns and types.** The column list and the types are equal. Path B or
  C with a type change: only `COL` differs, and it is `DATE`. Path B or C
  without a type change: no column differs.
- **Length.** `column_details[].length` of each column equals the value
  recorded for `B.T` in section 1. A column that has a length on one table
  and none, or another one, on the other table is a mismatch. Path B or C
  with a type change: `COL` is left out, because its type changes.
- **Nullability.** `column_details[].nullable` of each column equals the
  value recorded for `B.T` in section 1. A column that is NOT NULL on one
  table and nullable on the other is a mismatch: after the swap the table
  would accept or reject inserts differently.
- **Primary key.** `primary_key` is equal.
- **Column defaults.** `column_details[].default` of each column equals the
  value recorded for `B.T` in section 1. A missing default is a mismatch.
- **Path C, ranges.** Every range of the range record is `done`, and its copy
  rows equal its source rows.
- **Layout.** `definition.timePartitioning` and `definition.clustering` of the
  copy are as requested, `timePartitioning.expirationMs` included when
  section 1 recorded one.
- **Policy tags.** The tags of each column, read from BigQuery in section
  4c, equal the tags recorded for the source in section 1. A source column
  with a tag that the copy lacks stops the conversion. `policyTagState` is
  not part of this check.
- **Table IAM bindings.** The bindings of the copy equal the recorded ones.
- **Path B or C with a type change, NULL count.** The count of NULL in `COL` is
  equal on both tables. The per-partition query below cannot show this: it
  parses `COL` of the old table, so a string that does not parse and a NULL
  land in the same group. Count the NULLs directly:

  ```bash
  kbagent --json workspace query --project ALIAS --workspace-id WS_ID --sql "SELECT (SELECT COUNTIF(COL IS NULL) FROM T) AS old_nulls, (SELECT COUNTIF(COL IS NULL) FROM T_repart) AS new_nulls"
  ```

kbagent cannot read IAM bindings. The operator reads them on the copy:

```bash
bq --project_id=GCP_PROJECT get-iam-policy --format=prettyjson DATASET.T_repart
```

Every partitioned table, on paths A, B and C, also gets a row count per
partition. A cluster-only table that was copied has no partitions and skips
it. On every table over 5 TiB, path A or path C, the check is required, like
every other check here. A cluster-only table converted in place skips
sections 4c to 10 (section 4, in-place path).

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
| YEAR | `DATE_TRUNC(DAY_EXPR, YEAR)` |

Build both from `DAY_EXPR` of section 1, with the type that `COL` has on
that table:

- Path A: `COL` has the same type on both tables, so `PART_OLD` and
  `PART_NEW` are the same expression.
- Path B or C with a type change: on the old table `COL` is a STRING, so
  `PART_OLD` uses `SAFE.PARSE_DATE` with the format string of section 4a. On
  the new table `COL` is a DATE, so `PART_NEW` uses `COL`.
- Path B or C without a type change: as on path A, both are the same
  expression.

Rows with NULL in `COL` form one group with `p` NULL on each table, and the
query compares the two groups like any other partition. An empty result
passes: the last statement has `status` `completed` and `rows_affected` 0,
and no `csv_data` (section 0, with its filter). An error, another status, or
`rows_affected` above 0 fails the check; `csv_data` then lists the partitions
that differ.

Right before the swap, count the rows of both tables once more with the
count query at the top of this section. It is the last command before the
swap commands of section 6. `old_rows` and `new_rows` must both equal the
row count recorded before the copy. On a difference, stop, do not swap, and
run the abort step (section 4d): a write landed after the copy. `rows_count` and `last_import_date` of `B.T`
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
kbagent --json storage table-detail --project ALIAS --table-id B.T | jq 'del(.data.definition.partitions)'
```

The dry run only echoes the pair of tables. It validates nothing: Storage
checks the pair when the swap runs.

After the swap `B.T` holds the new layout and `B.T_repart` holds the old
table. The swap also moves `last_import_date` of both tables to the time of
the swap. Then, in this order:

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
4. **Aliases.** The swap did not move the aliases recorded in section 1:
   each of them still reads the old physical table, now `B.T_repart`. Before
   the writers resume, the owner deletes each alias and creates it again on
   `B.T` with the recorded definition. kbagent cannot do either step. Then
   read every alias with `storage table-detail` and compare its columns and
   `rows_count` with `B.T` and with `B.T_repart`. On path A both tables have
   the same columns and row count at this point, so this comparison cannot
   show which table an alias reads: the proof is the owner's confirmation
   for each alias. Repeat the comparison after the first load in section 7:
   the alias then equals `B.T` and differs from `B.T_repart`. An alias that
   follows `B.T_repart` was not created again. Stop the day-7 drop until the
   owner has done it.
5. **Shared bucket.** Read `sharing edges` again: the edges with the bucket
   as source are the same as recorded.

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
kbagent --json storage table-detail --project ALIAS --table-id B.T | jq 'del(.data.definition.partitions)'
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
kbagent --json storage swap-tables --project ALIAS --table-id B.T --target-table-id B.T_repart --branch 0 --dry-run
kbagent --json storage swap-tables --project ALIAS --table-id B.T --target-table-id B.T_repart --branch 0 --yes --timeout 3600
kbagent --json storage table-detail --project ALIAS --table-id B.T | jq 'del(.data.definition.partitions)'
```

After a rollback the original physical table is back at `B.T`, with its IAM
bindings. The operator reads the bindings and the policy tags once more on
`B.T` (the `get-iam-policy` and policy tag commands of section 1) and stops
on a difference from the values recorded in section 1. The aliases that the owner created again after the first swap now
read `B.T_repart`: the owner deletes them and creates them again on `B.T`
(section 6, step 4). A person creates the search indexes again
(section 6, step 3). After a rollback of path B or C with a type change the owner also reverts the
writers to STRING. The rows that were loaded after the first swap hold a
DATE in `COL`: the owner formats them back with the format string
(`FORMAT_DATE`) when loading them into `B.T` again.

Time travel of the new table starts at the copy. A restore to an earlier
point is possible only from the old table.

On day 7, with the owner's yes:

```bash
kbagent --json storage delete-table --project ALIAS --table-id B.T_repart --dry-run
kbagent --json storage delete-table --project ALIAS --table-id B.T_repart --yes
kbagent --json workspace delete --project ALIAS --workspace-id WS_ID --yes
```

Never add `--force` to this `delete-table`. Storage rejects the deletion of
a table that an alias still points at, and `--force` deletes those aliases
with the table. A rejection here means that an alias was missed: the owner
creates it again on `B.T` (section 6, step 4), and the drop is repeated.

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
kbagent --json storage table-detail --project ALIAS --table-id B.T | jq 'del(.data.definition.partitions)'
kbagent --json storage table-detail --project ALIAS --table-id B.T_repart | jq 'del(.data.definition.partitions)'
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
(Storage). They finish or undo the metadata move, and they say which of the
two they did. Continue only after both tables match their physical state.
Where to continue depends on which table `B.T` holds now, so read it first
with `storage table-detail` and compare `definition` with the layout
recorded in section 1:

- **`B.T` holds the new layout** (the first swap was finished, or the
  rollback swap was undone). Continue at section 6, step 1.
- **`B.T` holds the old layout and the original physical table** (the
  rollback swap was finished, or the first swap was undone). Do not use the
  steps of section 6. Continue with the steps after the rollback in section
  8: the IAM bindings and policy tags, the search indexes, the aliases and,
  on path B or C with a type change, the writers. After an undone first swap `B.T_repart` is the
  copy again: run the abort step (section 4d) or start again at section 5.

## 10. Cost and record

- The copy bills one full scan of the table. Path C bills one scan of the
  source per range, plus the load of each range (section 4, path C, "Cost").
- The size check of a table over 5 TiB (section 2a) bills one filtered scan
  of the source and the copy of the sample.
- Storage is doubled until the drop on day 7.
- The copy is active storage for 90 days.

Record for the pilot: the copy duration, the swap duration, the row counts,
and the layout before and after. Over 5 TiB also the size check and its
estimate against the real copy time; on path C the range record.
