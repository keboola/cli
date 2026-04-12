# Plan: Storage File Commands (#134)

## Goal

Add direct Storage Files API commands to `kbagent`. Currently files are only
used internally by `upload-table` / `download-table`. Users need standalone
file operations for:

- Uploading arbitrary files (ZIP, JSON, images, artifacts)
- Downloading component output files
- Listing and filtering files by tags
- Managing file tags (add/remove)
- Loading an uploaded file into a table (file → table import)
- Unloading a table into a file (table export → downloadable file)

## Existing Infrastructure

What already works in `client.py` (reusable as-is):

| Method | Line | What it does |
|--------|------|-------------|
| `prepare_file_upload()` | 904 | POST `/v2/storage/files/prepare` — registers file, returns presigned URL |
| `_upload_to_cloud()` | 928 | Uploads bytes to GCP/AWS/Azure using credentials from prepare |
| `get_file_info()` | 1186 | GET `/v2/storage/files/{id}?federationToken=1` — metadata + download URL |
| `download_file()` | 1264 | Downloads non-sliced file with gzip handling |
| `download_sliced_file()` | 1202 | Downloads sliced file (manifest + concatenate parts) |
| `import_table_async()` | 1026 | POST import-async — imports a prepared file into a table |
| `export_table_async()` | 1154 | POST export-async — exports table to a file |

What is **missing** in `client.py`:

| Method | API endpoint | Purpose |
|--------|-------------|---------|
| `list_files()` | GET `/v2/storage/files` | List files with filtering (tags, limit, offset, since) |
| `delete_file()` | DELETE `/v2/storage/files/{id}` | Delete a file |
| `tag_file()` | POST `/v2/storage/files/{id}/tags` | Add tags to existing file |
| `untag_file()` | DELETE `/v2/storage/files/{id}/tags/{tag}` | Remove tag from file |

The `prepare_file_upload()` method needs extension — currently only sends
`name` + `sizeBytes` + `federationToken`. Storage Files API also accepts:
`tags[]`, `isPermanent`, `isPublic`, `notify`.

---

## New CLI Commands

### 1. `storage file-list`

```bash
# List recent files (default limit 20)
kbagent storage file-list --project ALIAS

# Filter by tags (AND logic — all tags must match)
kbagent storage file-list --project ALIAS --tag output --tag monthly

# Pagination
kbagent storage file-list --project ALIAS --limit 50 --offset 100

# Since timestamp
kbagent storage file-list --project ALIAS --since 2026-04-01T00:00:00Z

# With branch
kbagent storage file-list --project ALIAS --branch 1234
```

JSON output per file: `id`, `name`, `sizeBytes`, `created`, `tags`,
`isSliced`, `isPermanent`, `creatorToken.description`.

### 2. `storage file-upload`

```bash
# Upload a file
kbagent storage file-upload --project ALIAS --file ./data.csv

# With tags and permanent flag
kbagent storage file-upload --project ALIAS --file ./report.zip \
  --tag report --tag 2026-Q1 --permanent

# Custom name (default = local filename)
kbagent storage file-upload --project ALIAS --file ./tmp_export.csv \
  --name "monthly-report.csv"

# With branch
kbagent storage file-upload --project ALIAS --file ./data.csv --branch 1234
```

Returns: file ID, name, size, tags, cloud URL.

### 3. `storage file-download`

```bash
# Download by file ID (output to current dir, original filename)
kbagent storage file-download --project ALIAS --file-id 12345

# Custom output path
kbagent storage file-download --project ALIAS --file-id 12345 --output ./report.csv
```

Handles both sliced and non-sliced files transparently
(reuses existing `download_file` / `download_sliced_file`).

### 4. `storage file-info`

```bash
# Show file metadata (without downloading)
kbagent storage file-info --project ALIAS --file-id 12345
```

Shows: id, name, size, created, tags, isSliced, isPermanent, component,
config, creator token description.

### 5. `storage file-delete`

```bash
# Delete a file
kbagent storage file-delete --project ALIAS --file-id 12345

# Batch delete
kbagent storage file-delete --project ALIAS --file-id 12345 --file-id 67890

# Dry-run
kbagent storage file-delete --project ALIAS --file-id 12345 --dry-run
```

### 6. `storage file-tag`

```bash
# Add tags to a file
kbagent storage file-tag --project ALIAS --file-id 12345 --add report --add 2026-Q1

# Remove tags from a file
kbagent storage file-tag --project ALIAS --file-id 12345 --remove draft

# Both in one call
kbagent storage file-tag --project ALIAS --file-id 12345 --add final --remove draft
```

### 7. `storage load-file` (file → table)

Load an already-uploaded file into a table. This is the "file-based import"
path — useful when the file is already in Storage Files (e.g., uploaded by
a component, or via `file-upload`).

```bash
# Load file into existing table
kbagent storage load-file --project ALIAS --file-id 12345 --table-id in.c-main.users

# Incremental load
kbagent storage load-file --project ALIAS --file-id 12345 --table-id in.c-main.users \
  --incremental

# With CSV options
kbagent storage load-file --project ALIAS --file-id 12345 --table-id in.c-main.users \
  --delimiter ";" --enclosure "'"

# With branch
kbagent storage load-file --project ALIAS --file-id 12345 --table-id in.c-main.users \
  --branch 1234
```

Internally: calls `import_table_async()` with the given `dataFileId`.

### 8. `storage unload-table` (table → file)

Export a table to a Storage File that can then be downloaded or used by
other components.

```bash
# Unload table to a file
kbagent storage unload-table --project ALIAS --table-id in.c-main.users

# With column filter and limit
kbagent storage unload-table --project ALIAS --table-id in.c-main.users \
  --columns name --columns email --limit 1000

# Tag the output file
kbagent storage unload-table --project ALIAS --table-id in.c-main.users \
  --tag export --tag daily-snapshot

# Unload + immediately download
kbagent storage unload-table --project ALIAS --table-id in.c-main.users \
  --download --output ./export.csv

# With branch
kbagent storage unload-table --project ALIAS --table-id in.c-main.users \
  --branch 1234
```

Internally: calls `export_table_async()` → gets file_id from result →
optionally tags the file → returns file info (or downloads if `--download`).

---

## Implementation Phases

### Phase 1: Client Layer (`client.py`)

New methods to add:

```python
def list_files(
    self,
    limit: int = 20,
    offset: int = 0,
    tags: list[str] | None = None,
    since: str | None = None,
    branch_id: int | None = None,
) -> list[dict[str, Any]]:
    """GET /v2/storage/files with query params."""

def upload_file(
    self,
    file_path: str,
    name: str | None = None,
    tags: list[str] | None = None,
    is_permanent: bool = False,
    notify: bool = False,
    branch_id: int | None = None,
) -> dict[str, Any]:
    """Public wrapper: prepare_file_upload + _upload_to_cloud.
    Returns file resource dict with id, name, sizeBytes, tags, url."""

def delete_file(self, file_id: int) -> None:
    """DELETE /v2/storage/files/{file_id}."""

def tag_file(self, file_id: int, tag: str) -> None:
    """POST /v2/storage/files/{file_id}/tags with body {tag: ...}."""

def untag_file(self, file_id: int, tag: str) -> None:
    """DELETE /v2/storage/files/{file_id}/tags/{tag}."""
```

Extend `prepare_file_upload()`:
- Add `tags`, `is_permanent`, `notify` parameters
- Pass `tags[]`, `isPermanent`, `notify` in POST body

Extend `import_table_async()` (if needed):
- Verify it accepts `dataFileId` parameter for file-based imports
  (vs. current inline `dataString` or prepared file from upload flow)

### Phase 2: Service Layer (`services/storage_service.py`)

New methods:

```python
def list_files(self, alias: str | None, limit, offset, tags, since) -> dict
def upload_file(self, alias: str, file_path, name, tags, is_permanent) -> dict
def download_file(self, alias: str, file_id: int, output_path: str | None) -> dict
def get_file_info(self, alias: str, file_id: int) -> dict
def delete_files(self, alias: str, file_ids: list[int], dry_run: bool) -> dict
def tag_file(self, alias: str, file_id: int, add: list[str], remove: list[str]) -> dict
def load_file_to_table(self, alias, file_id, table_id, incremental, delimiter, enclosure) -> dict
def unload_table_to_file(self, alias, table_id, columns, limit, tags, download, output_path) -> dict
```

Each method follows the standard pattern:
1. Resolve project from alias
2. Create client
3. Call client method(s)
4. Return structured result dict

### Phase 3: Command Layer (`commands/storage.py`)

Add 8 new commands to `storage_app`:
- `file-list`, `file-upload`, `file-download`, `file-info`
- `file-delete`, `file-tag`
- `load-file`, `unload-table`

Each follows the existing command pattern:
1. Thin Typer function — parse args, call service, format output
2. Dual output: `--json` (structured) + Rich (human-readable tables)
3. Error handling: catch `KeboolaApiError` / `ConfigError` → exit code

### Phase 4: Tests

New test files:
- `tests/test_storage_files_cli.py` — CLI tests via CliRunner for all 8 commands
- Extend `tests/test_client.py` — client method tests with mocked HTTP
- Extend `tests/test_services.py` or new `tests/test_storage_service.py` — service logic

### Phase 5: Plugin Update

- Update `plugins/kbagent/skills/kbagent/SKILL.md` — add file commands to decision table
- Update CLAUDE.md CLI command list

---

## Delivery Order

| Step | Scope | Depends on |
|------|-------|-----------|
| 1 | `client.py`: `list_files`, `upload_file`, `delete_file`, `tag_file`, `untag_file` | — |
| 2 | `client.py`: extend `prepare_file_upload` with tags/permanent params | Step 1 |
| 3 | `storage_service.py`: file CRUD methods (list, upload, download, info, delete, tag) | Steps 1-2 |
| 4 | `commands/storage.py`: `file-list`, `file-upload`, `file-download`, `file-info`, `file-delete`, `file-tag` | Step 3 |
| 5 | `storage_service.py`: `load_file_to_table`, `unload_table_to_file` | Step 3 |
| 6 | `commands/storage.py`: `load-file`, `unload-table` | Step 5 |
| 7 | Tests for all of the above | Steps 1-6 |
| 8 | Plugin SKILL.md + CLAUDE.md update | Steps 4, 6 |

---

## Edge Cases and Design Decisions

### Sliced files
Large exports produce sliced files (multiple parts). `download_sliced_file()`
already handles this. `file-download` must check `isSliced` and route accordingly.

### File size display
Use human-readable sizes in Rich output (e.g., "42.5 MB") but exact bytes in JSON.

### Tags as repeatable option
Use `--tag TAG` (repeatable) consistent with how `--tables` works in workspace commands.
NOT `--tags "a,b,c"` — Typer's `list[str]` handles repeatable options cleanly.

### Default file naming
`file-download` defaults output filename to the file's `name` field from API.
`file-upload` defaults `name` to the local file's basename.

### Permanent vs. temporary
Storage Files are temporary by default (auto-deleted after ~15 days).
`--permanent` flag makes them permanent. This is important for artifact storage.

### load-file vs upload-table
- `upload-table` = local file → cloud upload → table import (the existing command)
- `load-file` = Storage File (already uploaded) → table import (new command)

These serve different use cases. `upload-table` is for local files.
`load-file` is for files already in Keboola (component outputs, previous uploads).

### unload-table vs download-table
- `download-table` = table → export → download to local file (existing command)
- `unload-table` = table → export → Storage File (stays in Keboola, optional download)

`unload-table` is useful when you want to keep the export as a file in Keboola
for other components to consume, or to tag it for later retrieval.

### Branch support
All file commands support `--branch ID` via `resolve_branch()` helper.
Branch-scoped file operations use `/v2/storage/branch/{id}/files` prefix.

---

## API Reference

Storage Files API endpoints used:

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/v2/storage/files` | List files (query: `limit`, `offset`, `tags[]`, `sinceId`, `q`) |
| POST | `/v2/storage/files/prepare` | Prepare upload (body: `name`, `sizeBytes`, `tags[]`, `isPermanent`, `federationToken`) |
| GET | `/v2/storage/files/{id}?federationToken=1` | File detail with download credentials |
| DELETE | `/v2/storage/files/{id}` | Delete file |
| POST | `/v2/storage/files/{id}/tags` | Add tag (body: `tag`) |
| DELETE | `/v2/storage/files/{id}/tags/{tag}` | Remove tag |

Table import from file:
| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | `/v2/storage/tables/{tableId}/import-async` | Import with `dataFileId` param |

Table export to file:
| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | `/v2/storage/tables/{tableId}/export-async` | Returns job with file in results |
