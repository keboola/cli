# Python Library Workflow (`from keboola_agent_cli import Client`)

Besides the CLI and the `kbagent serve` daemon, kbagent ships a **stateless,
importable Python library** (since 0.61.0). It lets an in-process consumer -- a
Keboola Data App, a transformation, a hosted service -- run Query Service SQL and
read/write Storage Files with **no CLI subprocess, no daemon, and no config-dir**.

Use this when you are **already inside Python** and want fixed, typed operations.
For AI-driven exploration across projects, use MCP tools instead; for one-off
shell operations, use the `kbagent` CLI.

## Quick reference

| Symbol | Purpose |
|--------|---------|
| `Client(url, token, *, branch_id=None)` | Stateless entry point to one project; context manager |
| `Client.query(workspace_id, sql, *, transactional=False, limit=500)` | Run SQL in a workspace -> `list[dict]` |
| `Client.query_result(workspace_id, sql, ...)` | Same, but typed -> `QueryResult` (columns + truncation) |
| `Client.run_job(component_id, config_id, *, wait=False, ...)` | Run a Queue job -> `JobResult` |
| `Client.config_detail(component_id, config_id, *, branch_id=None)` | One config's detail -> `ConfigDetailResult` |
| `Client.upload_table(table_id, file_path, *, incremental=False, ...)` | Import a CSV into an existing table -> `UploadTableResult` |
| `Client.files.upload(source, *, name=None, tags=None, permanent=False)` | Upload a path **or** bytes -> `FileEntry` |
| `Client.files.read_bytes(file_id)` | Download a file fully into memory -> `bytes` |
| `Client.files.list(*, tags=None, query=None, limit=100, ...)` | List files -> `list[FileEntry]` |
| `Client.files.delete(file_id)` | Delete a file |
| `Client.raw` | The underlying `KeboolaClient` for endpoints the facade omits |
| `FileEntry` | Uniform file shape: `id, name, tags, created, size_bytes, is_permanent, raw` |

Everything exported from `keboola_agent_cli` (`Client`, `Files`, `FileEntry`, and
the typed result models `JobResult`, `QueryResult`, `UploadTableResult`,
`SyncPushResult`, `ConfigDetailResult`) is committed public API and follows
semver. Since 0.63.0 the package ships a **`py.typed`** marker (PEP 561), so
`mypy` / `ty` / IDEs treat the SDK as typed -- a contract change surfaces at
type-check time, not at runtime.

## Typed result models

The high-traffic operations return pydantic models (`result_models.py`) instead
of bare dicts, so you get autocomplete and a versioned contract:

```python
job = kbc.run_job("keboola.ex-db-snowflake", "12345", wait=True)
if job.succeeded:                       # -> JobResult
    print(job.id, job.result)

res = kbc.query_result(ws, 'SELECT id, name FROM t')   # -> QueryResult
print(res.columns, res.row_count, res.truncated)

cfg = kbc.config_detail("keboola.ex-http", "98765")    # -> ConfigDetailResult
print(cfg.name, cfg.version, cfg.configuration)
```

Every model is **tolerant of extra fields** (`extra="allow"`): the named fields
are the stable surface, but anything else the API returns is preserved
(reachable via attribute access and `model_dump()`), so a new backend field
never raises. They also accept the raw API key *or* the snake_case field name, so
`JobResult.model_validate(service_dict)` works directly on a service-layer dict.

## Auth & construction

Auth is the storage token you pass in (12-factor) -- nothing is persisted to disk.

```python
import os
from keboola_agent_cli import Client

with Client(url=os.environ["KBC_URL"], token=os.environ["KBC_TOKEN"]) as kbc:
    ...  # use kbc; the `with` block closes the HTTP client on exit
```

- `url` is the stack URL, e.g. `https://connection.keboola.com`.
- `branch_id=None` (default) targets **production**: Storage Files use the
  production scope and `query()` resolves the project's default branch lazily on
  first use (one extra `list_dev_branches` call, then cached). Pass `branch_id=`
  to target a dev branch and skip that lookup.
- Missing `url`/`token` raise `ValueError` (fail fast).

## Querying a workspace

```python
rows = kbc.query(workspace_id, 'SELECT id, name FROM customers')
# -> [{"id": "1", "name": "Alice"}, {"id": "2", "name": "Bob"}]
```

**GOTCHA -- values come back as strings, not native types.** The Query Service
`/results` endpoint serializes Snowflake scalars as JSON **strings** (`1` -> `"1"`,
`1.5` -> `"1.5"`, `true` -> `"true"`), with SQL `NULL` -> `None`. The facade is
transparent and does **not** coerce, so cast on your side:

```python
total = sum(int(r["amount"]) for r in kbc.query(ws, 'SELECT amount FROM sales'))
```

Other `query()` facts:

- Keys are the result column names **exactly as the warehouse reports them** --
  Snowflake folds unquoted aliases to UPPERCASE, so quote (`AS "id"`) for
  lowercase keys.
- Results are capped at `limit` (default 500) with a logged **warning** when the
  warehouse has more -- never silently truncated. Raise `limit=` to fetch more.
- `workspace_id` must already exist -- `query()` does **not** create a workspace
  (make one via `kbagent workspace create` or the Storage API first).
- With multiple statements, the rows of the **last** result-producing statement
  are returned (`USE ...; SELECT ...` yields the SELECT). No result set -> `[]`.

## Storage Files

```python
# Upload from a path or from in-memory bytes (bytes need an explicit name)
meta = kbc.files.upload(b"hello world", name="greeting.txt", tags=["demo"], permanent=True)
meta = kbc.files.upload("/tmp/report.csv", tags=["demo"])          # name defaults to basename

# Read a file fully into memory (handles sliced files + gzip internally)
data: bytes = kbc.files.read_bytes(meta.id)

# List as a uniform shape -- read via read_bytes(id), never branch on a signed URL
for f in kbc.files.list(tags=["demo"]):
    print(f.id, f.name, f.tags, f.created, f.size_bytes)

kbc.files.delete(meta.id)
```

- `upload(bytes, ...)` requires `name=` (Storage needs a file name; bytes have no
  path to derive it from) -- otherwise `ValueError`.
- `read_bytes` holds the whole payload in RAM -- fine for results/manifests/small
  exports; for multi-GB tables stream to disk via `Client.raw` instead.
- `FileEntry` deliberately omits a download `url`: the single read path is
  `read_bytes(id)`, so callers never branch on "does this item have a signed
  URL?" (and there is no expiring-URL footgun). `FileEntry.raw` keeps the full
  API dict as an escape hatch.

## Lower-level access

For endpoints the facade does not wrap (buckets, tables, branches, job polling
internals, ...), reach for the underlying client:

```python
client = kbc.raw                      # a KeboolaClient
buckets = client.list_buckets()
```

## When NOT to use the library

| Situation | Use instead |
|---|---|
| AI-driven exploration across one or many projects | MCP tools (`kbagent tool call ...`) |
| One-off shell / scripted ops, CI steps | the `kbagent` CLI |
| You need a long-lived HTTP API / Web UI | `kbagent serve` |
| Shelling out to the `kbagent` binary from a Python process you control | import the library (this doc) |

## Related

- [Storage Files via the CLI](storage-files-workflow.md)
- [Workspace SQL debugging](workspace-workflow.md)
- [Response parsing gotchas](gotchas.md) -- incl. the `query()` string-typing entry
