# Storage Files Workflow

Storage Files is a staging area in Keboola for arbitrary files -- CSV data,
ZIP archives, JSON configs, component artifacts, and logs. Files are temporary
by default (auto-deleted after ~15 days) unless marked as permanent.

## Quick reference

| Command | Purpose |
|---------|---------|
| `storage files` | List files (with tag filtering) |
| `storage file-detail` | Show file metadata |
| `storage file-upload` | Upload a local file |
| `storage file-download` | Download a file (by ID or by tag) |
| `storage file-tag` | Add/remove tags |
| `storage file-delete` | Delete files |
| `storage load-file` | Import an uploaded file into a table |
| `storage unload-table` | Export a table to a Storage File |

## Upload a file with tags

```bash
kbagent --json storage file-upload \
  --project ALIAS \
  --file ./data.csv \
  --tag report --tag 2026-Q1 \
  --permanent
```

- `--tag` is repeatable (assigns multiple tags at upload time)
- `--permanent` prevents auto-deletion after 15 days
- `--name` overrides the filename (default: local file basename)
- Returns `id` -- save it for later operations

## List and filter files by tags

```bash
# All recent files
kbagent --json storage files --project ALIAS

# Filter by tags (AND logic -- all tags must match)
kbagent --json storage files --project ALIAS --tag report --tag 2026-Q1

# Full-text search on file name
kbagent --json storage files --project ALIAS --query "monthly"

# Pagination
kbagent --json storage files --project ALIAS --limit 50 --offset 100
```

## Download a file

```bash
# By file ID
kbagent --json storage file-download --project ALIAS --file-id 12345

# By tags (downloads the latest file matching ALL tags)
kbagent --json storage file-download --project ALIAS --tag report --tag 2026-Q1

# Custom output path
kbagent --json storage file-download --project ALIAS --file-id 12345 --output ./report.csv
```

Download by tag is useful for automated pipelines where the file ID is unknown
but the tag convention is stable.

## Manage tags

```bash
# Add tags
kbagent --json storage file-tag --project ALIAS --file-id 12345 --add final --add reviewed

# Remove tags
kbagent --json storage file-tag --project ALIAS --file-id 12345 --remove draft

# Both in one call
kbagent --json storage file-tag --project ALIAS --file-id 12345 --add final --remove draft
```

## Load a file into a table

When a file is already in Storage (uploaded by a component, or via `file-upload`),
import it directly into a table without re-uploading:

```bash
kbagent --json storage load-file \
  --project ALIAS \
  --file-id 12345 \
  --table-id in.c-data.users \
  --incremental
```

This is faster than `upload-table` because the file is already in the cloud --
no local upload step needed. The table must already exist.

## Export a table to a Storage File

Create a file in Keboola from table data. Useful for making data available
to other components, or for tagged snapshots:

```bash
# Export to file (stays in Keboola)
kbagent --json storage unload-table \
  --project ALIAS \
  --table-id in.c-data.users \
  --tag daily-snapshot --tag 2026-04-12

# Export + download locally in one step
kbagent --json storage unload-table \
  --project ALIAS \
  --table-id in.c-data.users \
  --download --output ./users-export.csv
```

### unload-table vs download-table

| | `download-table` | `unload-table` |
|---|---|---|
| File stays in Keboola | No (download only) | Yes (file persists) |
| Can tag the output | No | Yes (`--tag`) |
| Can download locally | Always | Optional (`--download`) |
| Parquet export | No (CSV only) | Yes (`--file-type parquet`) |
| Use case | Quick local export | Share data between components, snapshots |

### Parquet export

`unload-table --file-type parquet` produces a sliced Apache Parquet file in Storage Files.
This avoids the CSV round-trip for analytics data, preserves Keboola's typed backend
output, and is directly consumable by pyarrow, Spark, DuckDB and other Parquet readers.

```bash
# Export as Parquet (stays in Keboola)
kbagent --json storage unload-table \
  --project ALIAS \
  --table-id in.c-data.users \
  --file-type parquet --tag daily-snapshot

# Export + download -- creates a directory (default layout mirrors Keboola addressing)
kbagent storage unload-table \
  --project ALIAS \
  --table-id in.c-data.users \
  --file-type parquet --download
# -> ./ALIAS/in.c-data.users.parquet/
#      ├── <slice>.parquet       (one or more)
#      └── _manifest.json         (leading underscore -> skipped by parquet readers)
```

Read the dataset back in one line:

```python
import pyarrow.parquet as pq
t = pq.read_table("./ALIAS/in.c-data.users.parquet/")
```

**Downloading an existing Parquet Storage File** -- `storage file-download` auto-detects
sliced `.parquet` files and routes them to the per-slice downloader, so you cannot
accidentally corrupt the output by asking for a single-file save:

```bash
kbagent storage file-download --project ALIAS --file-id 123 --output ./dir/
# dir/ contains <slice>.parquet files + _manifest.json (same layout as unload-table --download)
```

**Constraints:**
- Parquet output is **always sliced** -- `--download` produces a directory, never a single file
- CSV concat logic is never used for parquet -- each slice is a self-contained file with its own footer
- Default download path: `./{project_alias}/{table_id}.parquet/` -- override with `--output`

## Typical workflows

### Artifact storage (CI/CD)

```bash
# Upload build artifact with tags
kbagent --json storage file-upload \
  --project prod \
  --file ./build-output.zip \
  --tag artifact --tag build-42 --tag latest \
  --permanent

# Later: download latest artifact by tag
kbagent --json storage file-download \
  --project prod \
  --tag artifact --tag latest \
  --output ./latest-build.zip
```

### Data exchange between components

```bash
# Component A exports results to a tagged file
kbagent --json storage unload-table \
  --project ALIAS \
  --table-id out.c-results.predictions \
  --tag ml-output --tag 2026-04-12

# Component B picks up the latest tagged file and loads it
FILE_ID=$(kbagent --json storage files --project ALIAS --tag ml-output --limit 1 \
  | jq '.data.files[0].id')
kbagent --json storage load-file \
  --project ALIAS \
  --file-id $FILE_ID \
  --table-id in.c-pipeline.predictions
```

## Key behaviors

- Files are **temporary by default** -- auto-deleted after ~15 days
- Use `--permanent` to keep files indefinitely
- Tag filtering uses **AND logic** -- all specified tags must match
- `file-download --tag` downloads the **most recent** matching file
- `load-file` skips the upload step -- the file is already in cloud storage
- `unload-table` creates a **sliced file** for large tables and always for Parquet (handled transparently on download; CSV slices are concatenated, Parquet slices are preserved as separate files)
- All file commands support `--branch` for dev branch operations
- Works across all cloud backends (AWS, GCP, Azure) transparently
