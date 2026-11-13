---
name: kbagent-bigquery-repartition
description: >
  Use when converting one existing Keboola Storage table on BigQuery to a
  partitioned and/or clustered layout. Covers: the copy into a new layout and
  the swap into the production name, a date held in a STRING column (the
  type-change path), the preflight of security objects (policy tags, table IAM
  bindings, row access policies, search indexes), validation before the swap,
  rollback with a second swap, and recovery of a half-moved swap. Triggers:
  partition table, repartition, recluster, clustering, BigQuery partitioning,
  time partitioning, swap tables, convert table layout, partition by date,
  STRING date to DATE.
---

# Convert a BigQuery table to a partitioned and clustered layout

Converts **one** existing Storage table on a BigQuery project to a partitioned
and/or clustered layout. The conversion runs from the client over the existing
Storage API: copy the table into a new table with the requested layout,
validate the copy, swap the two tables, keep the old table for 7 days.

The procedure is in [references/runbook.md](references/runbook.md). Follow it
section by section, in order. This file holds the scope, the classification,
the inputs and the guardrails.

Requires kbagent 1.0.0 or later. The runbook relies on the table detail
returning the Storage `definition` (layout and policy tag state), on
`--timeout` of the copy and the swap, and on `swap-tables --branch 0`.

General kbagent usage (output parsing, exit codes, other workflows) is in the
main skill: [../kbagent/SKILL.md](../kbagent/SKILL.md).

## What this skill does and does not do

It does:

- Convert one table per run.
- Copy, validate, swap, and keep the old table for 7 days.
- Roll back with a second swap while the old table exists.

It does not:

- Change a writer configuration. The owner does that.
- Pause or resume writers. The owner does that.
- Delete the old table before day 7.
- Run a `bq` command that writes. A person runs it (see Guardrails).
- Set a policy tag on the BigQuery schema directly. That bypasses Storage.

## Classification

Classify the table before anything else. The class decides the path.

| Class | Condition | Path |
|---|---|---|
| Cluster only | The table needs clustering columns and no partitioning. | Copy and swap (runbook section 4, path A, without the partitioning flags). This stays the path until the in-place clustering change ships. |
| Partition, column ready | The partition column is DATE, DATETIME or TIMESTAMP. | Copy path (runbook section 4, path A). |
| Partition, column not ready | The date is held in a STRING column, or the table is untyped. | Type-change path (runbook section 4, path B). |
| No key | No partition column and no clustering column is confirmed. | Stop. Ask the table owner. |

## How to run this

Ask the operator for every input before any write. Do not guess one.

1. Project alias.
2. Table ID.
3. Partition column. None for a cluster-only table.
4. Granularity. The runbook decides it in section 2 from the data.
5. Clustering columns, at most 4, in order.
6. Primary key of the table.
7. The owner who pauses and resumes the writers.
8. Confirmation that the operator has BigQuery access to the project's
   dataset for the `bq` steps. Without it the runbook refuses the table.

Then follow [references/runbook.md](references/runbook.md) from section 0.

## Guardrails

- **Every write waits for a yes.** Show the command, say what it changes, and
  wait for an explicit yes from the operator. One yes covers one command.
- **`--json` on every call**, before the subcommand.
- **A timeout is not a failure.** `STORAGE_JOB_TIMEOUT` (exit 4) means the job
  still runs on the server. Follow it with `storage job-detail --wait`. Never
  re-run a swap: a second swap swaps the tables back. Never re-run a copy: it
  starts a second copy.
- **A half-moved swap stops the run.** On the error
  `storage.tables.swapMetadataFailed`, stop and go to runbook section 9. Do
  not re-run the swap and do not write to either table.
- **A person runs every `bq` write.** The runbook prints each `bq` command
  that writes (table IAM policy, row access policy, search index) in full,
  with an explicit `--project_id=GCP_PROJECT` and the full `DATASET.TABLE`.
  The skill and the agent that follows it never run such a command. They wait
  for the person to confirm that it was run, then read the result.
- **Refuse instead of improvising.** The runbook lists the conditions under
  which a table is refused (section 1). A refused table stays as it is.
