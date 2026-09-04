# Programming against kbagent: the in-process Python SDK

`kbagent` is three things at once, and you can program against any of them:

| Surface | Import / entrypoint | Use it when | Reference |
|---|---|---|---|
| **CLI** | `kbagent ...` (subprocess) | shell scripts, AI agents, CI steps, anything that shells out | [README](../README.md), `kbagent --help` |
| **In-process SDK** | `from keboola_agent_cli import Client` | a Python process that already has the token and wants Keboola data **without** spawning a subprocess or a daemon | **this document** |
| **HTTP REST** | `kbagent serve` + any HTTP client | a separate process / language (JS, Go), a Web UI, a Slack bot | [build-your-own-client.md](build-your-own-client.md) |

This guide is the deep reference for the **middle row**: the importable, stateless `Client` facade introduced in [issue #415](https://github.com/keboola/cli/issues/415) and given typed return contracts in [#428](https://github.com/keboola/cli/issues/428).

> **Working demo:** [`examples/storage_tui/`](../examples/storage_tui/) is a runnable terminal app (curses) that browses a real project's Storage entirely through this SDK. Read it alongside this doc.

---

## 1. What the SDK is (and is not)

```python
import os
from keboola_agent_cli import Client

with Client(url=os.environ["KBC_URL"], token=os.environ["KBC_TOKEN"]) as kbc:
    rows = kbc.query(workspace_id, "SELECT id, name FROM customers")  # list[dict]
    meta = kbc.files.upload(b"hello", name="greeting.txt", tags=["demo"])
    data = kbc.files.read_bytes(meta.id)  # bytes
    job = kbc.run_job("keboola.ex-db-snowflake", "12345", wait=True)  # JobResult
```

**It is:**

- **Stateless.** A `Client` holds the stack URL, the token, one pooled HTTP client (with the shared retry/backoff), and an optional idempotency store. Nothing else.
- **Config-dir-free.** No `~/.config/keboola-agent-cli/`, no `kbagent project add`, no `config.json`. Auth is the token you pass in (12-factor: you read `KBC_TOKEN` yourself).
- **In-process.** No CLI subprocess, no `kbagent serve` daemon. Just function calls.
- **Typed.** `py.typed` ships in the wheel; the high-traffic operations return pydantic models (`JobResult`, `QueryResult`, ...). Your `mypy`/`ty`/IDE sees the shapes.

**It is not:**

- **Not the whole CLI.** The facade exposes the high-value operations (Query Service, Storage Files, run-job, config detail, table upload). For everything else there is [`Client.raw`](#7-clientraw-the-escape-hatch).
- **Not config-aware.** It does not resolve project aliases, default branches from a config file, or linked variable values. You pass IDs explicitly.
- **Not auto-creating.** `upload_table` requires the table to already exist (the CLI's auto-create-bucket-and-table path lives in the service layer, which the facade deliberately does not carry).

---

## 2. Where the SDK sits in the architecture

kbagent is a strict 3-layer codebase (see [CONTRIBUTING.md](../CONTRIBUTING.md#3-layer-architecture--respect-the-boundaries)):

```
 CLI command  ─┐
 REST route   ─┼─►  Service (services/*.py)  ─►  KeboolaClient (client/ pkg)  ─►  HTTP
 SDK facade   ─┘         business logic              endpoints, retry
   (lib.py)
```

The SDK **facade** (`lib.py`) is a *fourth caller* that sits beside the CLI and REST layers, but it **skips the service layer** and talks to `KeboolaClient` directly, re-assembling the high-level shapes (`list[dict]` rows, `bytes`, `FileEntry`, the typed models) that the service layer would otherwise build for the CLI.

**Consequence for contributors:** a facade method is intentionally *thinner* than the matching service method. `Client.run_job` does not auto-resolve linked variable values the way `JobService.run_job` does; `Client.upload_table` does not auto-create the bucket/table the way `StorageService.upload_table` does. When you add or change a facade method, decide consciously which service conveniences to replicate and which to omit — and document the omission in the docstring (the existing methods do this).

---

## 3. Quick start

```python
import os
from keboola_agent_cli import Client

# 12-factor: the token is your responsibility to source. Never hardcode it.
kbc = Client(url=os.environ["KBC_URL"], token=os.environ["KBC_TOKEN"])
try:
    for bucket in kbc.raw.list_buckets():
        print(bucket["id"], bucket.get("description", ""))
finally:
    kbc.close()

# ...or as a context manager (preferred — closes the HTTP client for you):
with Client(url=os.environ["KBC_URL"], token=os.environ["KBC_TOKEN"]) as kbc:
    ...
```

`Client(...)` raises `ValueError` immediately if `url` or `token` is empty — fail-fast, no silent default.

The SDK requires a **static Storage token**. A browser-login (session) project stores the sentinel `kbc-session://{project_id}` in place of a token, and passing that sentinel raises `SessionAuthUnsupportedError` (carrying `error_code` `AUTH_NOT_SUPPORTED_ON_STACK`) from the constructor; since neither it nor its `ConfigError` base is in `__all__`, catch it as `keboola_agent_cli.errors.ConfigError` (an uncommitted import path) or let it propagate. Register the project with a static token for SDK use — see [auth.md](auth.md) for the full list of surfaces sessions do not reach.

To scope every call to a dev branch, pass `branch_id`:

```python
with Client(url=URL, token=TOKEN, branch_id=1234) as kbc:
    rows = kbc.query(ws_id, "SELECT 1")  # runs on branch 1234
```

`branch_id=None` (the default) targets **production**: Storage Files use the production scope and `query()` resolves the project's *default* branch on first use.

---

## 4. `Client` method reference

Every method below is committed public API; signatures change only under semver.

### `query(workspace_id, sql, *, transactional=False, limit=500) -> list[dict]`

Runs SQL in a workspace via the Query Service inline-results fast path (no CSV materialization) and returns rows as dicts keyed by column name.

```python
rows = kbc.query(workspace_id, "SELECT id, name FROM customers LIMIT 10")
# [{"ID": "1", "NAME": "Acme"}, ...]
```

Three gotchas baked into the contract:

- **Snowflake string-typing.** Values are returned exactly as the Query Service serializes them and are **not** coerced by the facade. For Snowflake every scalar comes back as a JSON string (`1` → `"1"`, `1.5` → `"1.5"`, `true` → `"true"`), with SQL `NULL` as `None`. Cast on your side.
- **Column-name case.** Snowflake folds unquoted aliases to UPPERCASE — `SELECT id AS foo` keys the dict as `"FOO"`. Quote the alias (`AS "foo"`) for lowercase.
- **Multi-statement.** With several statements, the rows of the **last** statement that produced a result set win (`USE ...; SELECT ...` yields the SELECT). Statements without a result set yield `[]`.

Over-`limit` results are capped and a warning is logged — raise `limit` to fetch more.

### `query_result(workspace_id, sql, *, transactional=False, limit=500) -> QueryResult`

Same execution as `query`, but returns the full typed [`QueryResult`](#5-typed-result-models) — `columns` (in warehouse order), `rows`, `truncated`, `total_rows`, and a `.row_count` property. Use it when you need column ordering or want to detect a `limit` cap:

```python
res = kbc.query_result(ws_id, "SELECT * FROM big_table", limit=500)
if res.truncated:
    print(f"showing {res.row_count} of {res.total_rows} rows")
print(res.columns)  # ["ID", "NAME", "CREATED_AT"]
```

### `run_job(component_id, config_id, *, wait=False, timeout=300.0, ...) -> JobResult`

Creates a Queue API job and — with `wait=True` — polls until terminal (or `timeout`). Returns a typed [`JobResult`](#5-typed-result-models) with `.succeeded` / `.failed` convenience properties.

```python
job = kbc.run_job("keboola.ex-db-snowflake", "12345", wait=True, timeout=600)
if job.failed:
    raise RuntimeError(f"job {job.id} ended {job.status}")
```

Full signature knobs: `config_row_ids`, `variable_values_id` (the facade does **not** auto-resolve linked variable values — pass it explicitly if the config needs one), `branch_id`, `mode` (`run`/`debug`/`forceRun`), `poll_strategy`, and the idempotency pair below.

**Idempotency** ([#427](https://github.com/keboola/cli/issues/427)). The Queue API has no server-side idempotency, so the facade does it client-side against a store *you* supply (it has no config-dir to default to):

```python
from keboola_agent_cli import JobIdempotencyStore

store = JobIdempotencyStore(path="/my/app/checkpoints/jobs.json")
with Client(url=URL, token=TOKEN, idempotency_store=store) as kbc:
    job = kbc.run_job(
        "keboola.ex-db-snowflake", "12345", wait=True, idempotency_key="nightly-2026-06-17"
    )
    if job.idempotent_replay:
        print("a prior run for this key was returned — no new side effect")
```

A prior still-running or non-failed job is returned (with `idempotent_replay=True`) instead of firing a duplicate; a prior *failed* run is re-run; `force_rerun=True` always creates fresh. Passing `idempotency_key` without a store raises `ValueError`.

### `config_detail(component_id, config_id, *, branch_id=None) -> ConfigDetailResult`

Fetches one configuration's detail as a typed [`ConfigDetailResult`](#5-typed-result-models) (`id`, `name`, `description`, `version`, `configuration`, `rows`, ...).

```python
cfg = kbc.config_detail("keboola.ex-db-snowflake", "12345")
print(cfg.name, cfg.version)
host = cfg.configuration["parameters"]["db"]["host"]
```

### `upload_table(table_id, file_path, *, incremental=False, ...) -> UploadTableResult`

Imports a CSV into an **existing** Storage table. Unlike the CLI, it does **not** auto-create a missing bucket/table — use `kbagent storage upload-table` for that.

```python
res = kbc.upload_table("in.c-crm.customers", "customers.csv", incremental=True)
print(res.imported_rows, res.warnings)
```

### Device-enrollment primitives (`0.66.0+`)

Seven methods for the "provision an OTLP ingest endpoint, then mint a narrowly-scoped Storage token a device can hold" flow. They live on the facade and delegate straight to `KeboolaClient`; the token/stream ones return the typed models in [§5](#5-typed-result-models).

**Scoped Storage tokens** — mint, list, revoke, rotate:

- **`create_scoped_token(*, description, bucket_permissions=None, component_access=None, can_read_all_file_uploads=False, expires_in=None) -> ScopedTokenResult`** — `POST /v2/storage/tokens`. `bucket_permissions` is `{bucket_id: "read"|"write"}`; `expires_in` is seconds. The acting token must be a **master (admin) token**: a non-master token carrying `canManageTokens` gets a generic 500 (`CreateTokenVoter` `LogicException`, issue #599), one without the flag gets a 403. The SDK facade has no pre-flight guard (only the CLI/service layer does), so you hit the raw API behavior.
- **`list_tokens() -> list[TokenListEntryResult]`** — `GET /v2/storage/tokens` (`0.86.0+`). Where the `token_id` for `delete_token` / `refresh_token` comes from. **Secrets are stripped before validation**: a project carrying the `force-decrypted-token` feature has the API embed live values in the listing, and `create_scoped_token` is meant to be the only reveal. The acting token needs `canManageTokens`.
- **`list_tokens(*, with_last_used=False)`** — `with_last_used` (`0.88.0+`) additionally derives each token's most recent activity from `GET /v2/storage/tokens/{id}/events`, populating `last_used` / `last_used_event` / `last_used_status` and returning the entries dormant-first. **Opt-in: one extra request per token**, run in parallel. Read `last_used_status` rather than a bare `last_used`: `used` (timestamp is real), `never` (**proven** unused — minted inside the ~6-month event-retention window with no activity), `unknown` (older than retention, so the API cannot say — do *not* treat as never), `error` (that token's lookup failed; the entry degrades, the call still returns). Activity inside a **development branch is invisible** to this feed, so a branch-only token reads as dormant.
- **`delete_token(token_id) -> None`** — `DELETE /v2/storage/tokens/{id}` (204, no body). Revokes.
- **`refresh_token(token_id) -> ScopedTokenResult`** — `POST .../tokens/{id}/refresh`. Rotates the secret in place; the returned `.token` is the new secret. Any token may refresh itself; refreshing another token needs `canManageTokens`. No master token required — the `CreateTokenVoter` defect is create-only, so neither this facade nor the CLI guards it.

```python
tok = kbc.create_scoped_token(
    description="device-42",
    bucket_permissions={"in.c-otlp-my-source": "write"},
    expires_in=3600,
)
device_secret = tok.token  # ONE-TIME reveal — see the gotcha below
```

**Stream (OTLP Data Streams) sources** — provision, read, list, delete:

- **`create_stream_source(name, *, source_type="otlp", description="", branch_id="default", provision_sinks=True) -> StreamSourceResult`** — creates a Data Streams source over the derived `stream.<region>` host. With `source_type="otlp"` and `provision_sinks=True` (default) it auto-provisions the logs/metrics/traces sinks so data actually lands, landing in bucket **`in.c-otlp-<source_id>`** (surfaced as `sink_bucket_id`). Pass `provision_sinks=False` for a bare source.
- **`get_stream_source(source_id, *, branch_id="default") -> StreamSourceResult`** — normalized shape; `sink_bucket_id` is derived for OTLP sources.
- **`list_stream_sources(*, branch_id="default") -> list[dict]`** — raw source dicts.
- **`delete_stream_source(source_id, *, branch_id="default") -> None`** — DELETE (202 task, polled).

**The two-call enrollment example** — provision the endpoint, then mint the device's token scoped to exactly the sink bucket:

```python
with Client(url=URL, token=TOKEN) as kbc:  # TOKEN must be a master token for create
    src = kbc.create_stream_source("my-source")  # StreamSourceResult
    # hand the device its ingest endpoint:
    print(src.otlp_url)  # carries the ingest secret in the path — UNMASKED
    # mint the narrowly-scoped token the device uploads Files with:
    tok = kbc.create_scoped_token(
        description="device-42",
        bucket_permissions={src.sink_bucket_id: "write"},
        expires_in=3600,
    )
    print(tok.id, tok.token)  # persist tok.id + tok.expires; tok.token is one-time
```

Four gotchas that bite:

- **The token secret is a one-time reveal.** `create_scoped_token` / `refresh_token` return the plaintext secret **once** in `.token`. It is never retrievable again — persist only `.id` (to revoke/refresh later) and `.expires`. If you lose the secret, `refresh_token(id)` mints a new one.
- **`otlp_url` carries the ingest secret UNMASKED.** Unlike the CLI (`stream detail` masks it unless `--reveal`), the facade returns `otlp_url` / `otlp_secret` in the clear — the SDK caller is trusted. Treat `StreamSourceResult` as sensitive; do not log it.
- **`sink_bucket_id` is the bucket to grant the device write on.** OTLP data lands in `in.c-otlp-<source_id>`; that is the `bucket_permissions` key a device token needs (`{sink_bucket_id: "write"}`).
- **Files upload is NOT gated by `component_access` / `can_read_all_file_uploads`.** Any valid Storage token can upload its *own* Files; `can_read_all_file_uploads` only governs *reading other tokens'* uploads, and `component_access` is unrelated to Files entirely. For a device that uploads to a bucket, grant `bucket_permissions` **write** on the sink bucket — that is the load-bearing scope.

### `files` — Storage Files (`Client.files`)

A `Files` helper bound to the client's project/branch. See [§6](#6-files-storage-files).

### `raw` — the underlying `KeboolaClient`

Escape hatch for endpoints the facade omits. See [§7](#7-clientraw-the-escape-hatch).

### `close()` / context manager

`close()` closes the pooled HTTP client. Prefer `with Client(...) as kbc:` so it always runs.

---

## 5. Typed result models

`result_models.py` defines the **stable return shapes** (`JobResult`, `QueryResult`, `UploadTableResult`, `ConfigDetailResult`, `SyncPushResult`, `CloneResult`, the `0.66.0+` device-enrollment pair `ScopedTokenResult` / `StreamSourceResult`, and `TokenListEntryResult` from `0.86.0+`), all re-exported from the package root. They exist so a downstream consumer types against a **semver-versioned contract** instead of an undocumented `dict[str, Any]` — a contract change then surfaces at *type-check* time, not at runtime against a customer build.

Two design rules every model follows (`_ApiResultModel` base):

- **`extra="allow"` — forward-compatible.** The backing Keboola APIs grow fields across stack versions. An SDK contract must never *raise* because the server returned more than the documented subset. Only the **named** fields are the committed surface; everything else is preserved as model extras (reachable via attribute access and `model_dump()`), so nothing is lost.

  ```python
  job = kbc.run_job("keboola.ex-db-snowflake", "12345", wait=True)
  job.status  # named field — committed contract
  job.model_extra  # {"branchId": ..., "durationSeconds": ..., "runId": ...} — preserved, not typed
  ```

- **`populate_by_name=True` — alias-tolerant.** Each model accepts both the snake_case field name and the raw API key (declared via `AliasChoices`), so `Model.model_validate(service_dict)` works directly on a service-layer dict without renaming. `JobResult` reads `isFinished`/`is_finished`, `componentId`/`component`/`component_id`, etc.

Convenience properties carry semantic meaning so callers don't re-derive it: `JobResult.succeeded` / `.failed`, `QueryResult.row_count`, `SyncPushResult.ok`, `CloneResult.ok`.

The two device-enrollment models (`0.66.0+`) commit these named fields:

- **`ScopedTokenResult`** — `id`, `token` (the one-time secret, see the gotcha in §4), `description`, `expires` (`str | None`), `can_read_all_file_uploads` (alias `canReadAllFileUploads`).
- **`StreamSourceResult`** — `id`, `source_id`, `name`, `type`, `description`, `branch_id` (default `"default"`), `otlp_url` (ingest URL, secret in the path — unmasked), `otlp_secret`, `base_endpoint`, `sink_bucket_id` (`str | None`; the `in.c-otlp-<id>` bucket to grant a device token write on).

`TokenListEntryResult` (`0.86.0+`) commits `id`, `description`, `created` (`str | None`), `expires` (`str | None`), `is_expired` (alias `isExpired`), `is_master_token` (alias `isMasterToken`), and (`0.88.0+`) `last_used` (alias `lastUsed`), `last_used_event` (alias `lastUsedEvent`) and `last_used_status` (alias `lastUsedStatus`) — all three `str | None`, `None` unless `list_tokens(with_last_used=True)` was used. It has **no secret field by design** — see `list_tokens` above.

> **The committed surface is `__all__`.** Anything exported from `keboola_agent_cli` (`Client`, `Files`, `FileEntry`, the result models, `JobIdempotencyStore`, `__version__`) is public API under semver. Renaming or removing a named field, or tightening a type, is a breaking change.

---

## 6. `files` — Storage Files

`Client.files` is a `Files` instance bound to the project (and branch). It returns a uniform `FileEntry` regardless of whether the API response happened to include a signed URL.

```python
# upload bytes (name required) or a path (name derived from basename)
meta = kbc.files.upload(b"col1,col2\n1,2\n", name="data.csv", tags=["nightly"])
meta = kbc.files.upload("report.pdf", tags=["report"], permanent=True)

# list as FileEntry records (tags AND-filter, name full-text query, pagination)
entries = kbc.files.list(tags=["nightly"], limit=50)
for e in entries:
    print(e.id, e.name, e.size_bytes, e.is_permanent)

# read the whole file into memory (handles sliced + gzip transparently)
raw_bytes = kbc.files.read_bytes(meta.id)

kbc.files.delete(meta.id)
```

`FileEntry` fields: `id`, `name`, `tags`, `created`, `size_bytes`, `is_permanent`, and `raw` (the untouched API dict for anything the facade doesn't surface).

**Memory note:** `read_bytes` holds the whole payload in RAM — fine for results, manifests, and small exports. For multi-GB tables, stream to disk via `Client.raw` instead.

---

## 7. `Client.raw` — the escape hatch

The facade is intentionally small. For any endpoint it omits — listing buckets/tables, table previews, workspaces, sharing, dev branches — reach the underlying `KeboolaClient`:

```python
with Client(url=URL, token=TOKEN) as kbc:
    buckets = kbc.raw.list_buckets()  # list[dict]
    tables = kbc.raw.list_tables(bucket_id="in.c-crm")  # list[dict]
    detail = kbc.raw.get_table_detail("in.c-crm.customers")  # dict
    csv = kbc.raw.get_table_data_preview("in.c-crm.customers", limit=20)  # CSV str
```

`raw` returns `dict`/`list[dict]`/`str` straight from the API — **not** the typed models. It is the lower-level, less-stable surface: useful, but you own the parsing. The [storage TUI demo](../examples/storage_tui/) is built almost entirely on these four `raw` methods.

---

## 8. The three integration modes — choosing one

```
                      already in a Python      separate process /     a shell / an
                      process with the token?   language?              AI agent?
  ───────────────────────────────────────────────────────────────────────────────
  in-process SDK            ✅ best
  kbagent serve (REST)                          ✅ best
  kbagent CLI                                                          ✅ best
```

- **SDK** when you are *already* a Python process holding the token — a Keboola **Data App**, a transformation, a hosted FastAPI service, a one-off automation script. Lowest latency (no subprocess, no daemon), typed, 12-factor auth.
- **REST (`kbagent serve`)** when the caller is a *different* process or language — a React/Next.js UI, a Go service, a Slack bot. One server, many clients, auth via a bearer token. Spec: [build-your-own-client.md](build-your-own-client.md).
- **CLI** when you are in a shell or an AI agent that shells out — scripted glue, CI steps, `claude`/`codex`/`gemini` tasks. Structured `--json` on every command.

They share the same `KeboolaClient` underneath, so behavior (retry, error shapes) is identical across all three.

---

## 9. Extending the SDK (for contributors)

The importable surface is a **committed contract**, so changing it is a deliberate act with its own checklist — parallel to ["Adding a New CLI Command"](../CONTRIBUTING.md#checklist-adding-a-new-cli-command) but smaller. Add a facade method (or a result model) when an in-process consumer would otherwise have to drop to `Client.raw` and hand-assemble a high-value shape.

**Adding a `Client` method:**

1. **Write it in `lib.py`**, calling `self._client` (the `KeboolaClient`) directly — do **not** import the service layer (that would drag config-dir/orchestration assumptions into a stateless facade). Re-assemble the high-level shape yourself, the way the existing methods do.
2. **Decide which service conveniences to omit** (auto-create, alias resolution, linked-variable resolution) and **state the omission in the docstring** — the existing methods set this precedent (`run_job`, `upload_table`).
3. **Return a typed model**, not a bare dict, for anything non-trivial (see below).
4. **Keyword-only for everything past the required positionals** (`*,`), matching the existing signatures — it keeps call sites readable and lets you add knobs without breaking positional callers.

**Adding a result model (`result_models.py`):**

1. Subclass `_ApiResultModel` (gives you `extra="allow"` + `populate_by_name`). **Never** set `extra="forbid"` — the API grows fields and the contract must not raise.
2. Type **only the stable subset** you are willing to commit to under semver. Everything else stays a model extra.
3. Use `Field(validation_alias=AliasChoices("rawApiKey", "snake_case_name"))` so `Model.model_validate(api_or_service_dict)` works without renaming.
4. Add semantic `@property` helpers (`.succeeded`, `.ok`) rather than making callers re-derive state.

**Then, the contract chores:**

5. **Export it** from `__init__.py` and add it to `__all__` — that list *is* the public API.
6. **`make typecheck` must stay clean** — the SDK is the one place where types are a user-facing promise, not just an internal aid.
7. **Test it** in `tests/` (facade tests mock `KeboolaClient`; model tests assert alias-acceptance and `extra` preservation).
8. **Document it here** in §4 / §5, and update the one-liner in the [README "Use as a library"](../README.md#use-as-a-library) section if it's a headline capability.
9. **Treat removals/renames as breaking** — a field rename or a type tightening is a semver-major-worthy change; prefer adding over mutating.

---

## 10. Gotchas cheat-sheet

| Gotcha | Why | What to do |
|---|---|---|
| Snowflake values are **strings** | Query Service serializes scalars as JSON strings; the facade does not coerce | cast on your side (`int(row["N"])`) |
| Column keys are **UPPERCASE** | Snowflake folds unquoted aliases | quote the alias for lowercase |
| `query` returns only the **last** result set | multi-statement SQL | split, or put the SELECT last |
| `run_job` ignores **linked variable values** | the facade is not config-aware | pass `variable_values_id` explicitly |
| `upload_table` **won't create** the table | no service/config context | create it first, or use the CLI |
| `idempotency_key` raises without a store | stateless facade has no config-dir | pass `idempotency_store=` |
| `read_bytes` loads the **whole file** into RAM | convenience over streaming | stream via `Client.raw` for huge tables |
| `branch_id=None` means **production** | default scope | pass a `branch_id` to target a dev branch |

---

## See also

- [`examples/storage_tui/`](../examples/storage_tui/) — runnable curses demo built on this SDK.
- [build-your-own-client.md](build-your-own-client.md) — the REST surface (`kbagent serve`) for non-Python callers.
- [CONTRIBUTING.md](../CONTRIBUTING.md) — full 3-layer architecture and coding conventions.
