# Contributing to kbagent

Guidelines for anyone contributing to this project -- human or AI agent.
Read this **before** writing code. It will save review rounds.

## Coding Style

### Python conventions

- **Python 3.12+** (`pyproject.toml` pins `requires-python = ">=3.12"`) -- use modern syntax (`str | None`, not `Optional[str]`)
- **Type hints** on all function signatures
- **f-strings** for string formatting (no `.format()` or `%`)
- **`pathlib.Path`** over `os.path` -- consistently used throughout the project
- **`httpx`** over `requests` for HTTP calls
- **PEP 8 naming**: `snake_case` for functions/variables, `PascalCase` for classes
- **Pydantic 2.x** (`BaseModel`) for all data models -- defined in `models.py`
- **Specific exception handling** -- never bare `except:`
- **`logging`** module for production logging, not `print()`
- Code is formatted with **ruff** -- run `make format` before committing

### 3-Layer architecture -- respect the boundaries

```
CLI Commands (commands/)  -->  Services (services/)  -->  API Client (client/, manage_client.py)
  Typer, output                 Business logic             HTTP, endpoints
```

| Layer | What goes here | What does NOT go here |
|-------|---------------|----------------------|
| **Commands** (`commands/`) | Typer option parsing, `OutputFormatter` calls, error-to-exit-code mapping | Business logic, HTTP calls, data transformation |
| **Services** (`services/`) | Orchestration, validation, data normalization, parallel execution | Typer imports, output formatting, raw HTTP |
| **Clients** (`client/` package, `manage_client.py`, etc.) | HTTP requests, URL construction, response parsing, retry logic | Business decisions, output formatting |

When adding a new feature, you will almost always touch all three layers.
If you find yourself importing `typer` in a service or calling `httpx` in a command, stop -- you're in the wrong layer.

### Thin commands, smart services

Commands are thin wrappers. All they do:
1. Parse Typer arguments
2. Call a service method
3. Format and output the result
4. Catch `KeboolaApiError` / `ConfigError` and map to exit codes

```python
# GOOD -- command is thin
@storage_app.command("create-bucket")
def storage_create_bucket(ctx, project, stage, name):
    formatter = get_formatter(ctx)
    service = get_service(ctx, "storage_service")
    try:
        result = service.create_bucket(alias=project, stage=stage, name=name)
    except KeboolaApiError as exc:
        formatter.error(message=exc.message, error_code=exc.error_code)
        raise typer.Exit(code=map_error_to_exit_code(exc)) from None
    formatter.output(result) if formatter.json_mode else ...

# BAD -- business logic leaked into command
@storage_app.command("create-bucket")
def storage_create_bucket(ctx, project, stage, name):
    if stage not in ("in", "out"):  # This belongs in service!
        ...
    client = KeboolaClient(...)     # This belongs in service!
    client.create_bucket(...)       # Commands don't call clients!
```

### Validate at system boundaries

User input coming through CLI arguments is a **system boundary** -- validate it.
Internal code passing data between layers is trusted -- don't over-validate.

Validation belongs in the **service layer** (not commands, not clients):
- Enum values (stage: `in`/`out`, column types, status filters)
- Format constraints (table ID format, bucket ID format)
- File existence checks
- Business rule validation

For CLI options with a small fixed set of values, prefer Typer/Click's `Choice`:
```python
stage: str = typer.Option(..., click_type=click.Choice(["in", "out"]))
```

### Dual output -- every command must support both modes

- `--json` mode: structured JSON via `formatter.output(data)`
- Human mode: Rich-formatted tables/text via `formatter.console.print()`

Never print raw text that breaks JSON parsing. Always check `formatter.json_mode`.

### Error handling

- Commands catch `KeboolaApiError` and `ConfigError`, map to exit codes
- Exit codes: 0=success, 1=general, 2=usage, 3=auth, 4=network, 5=config, 6=permission denied
- Multi-project operations accumulate errors -- one project failing doesn't stop others
- Use `raise typer.Exit(code=N) from None` to suppress traceback in CLI

### Constants -- no magic numbers

All configuration values go in `constants.py` or a dedicated config:
- Timeouts, retry counts, polling intervals
- Valid enum values (column types, stages)
- API endpoint paths if reused

```python
# BAD
time.sleep(2)
if retries > 3:

# GOOD
from .constants import POLL_INTERVAL, MAX_RETRIES
time.sleep(POLL_INTERVAL)
if retries > MAX_RETRIES:
```

## Code Quality Patterns

These are the *signal* patterns that distinguish hand-written quality code
from LLM-generated boilerplate. Every PR -- human or AI -- must adhere.
The `/kbagent:review` agent checks for these; the post-edit hooks in
`.claude/settings.json` run `ruff` + `ty` after every file write so drift is
caught immediately.

### Return values -- name them with dataclasses, not tuples

**Single value**: name the function after what it returns (`get_user_id`, `count_active_jobs`).

**Multiple values**: return a `@dataclass` (or `NamedTuple`/`BaseModel`) -- never a bare tuple beyond two values, and even two-element tuples should use a dataclass when the values are semantically distinct. Docstrings rot; dataclass field names do not.

```python
# BAD -- caller has to remember positional meaning
def resolve_project(alias: str | None) -> tuple[str, ProjectConfig]:
    ...

resolved_alias, project = resolve_project(alias)  # which is which?

# GOOD -- self-documenting at every call site
@dataclass(frozen=True)
class ResolvedProject:
    alias: str
    config: ProjectConfig

def resolve_project(alias: str | None) -> ResolvedProject:
    ...

resolved = resolve_project(alias)
resolved.alias, resolved.config  # unambiguous
```

Migration note: existing `tuple[...]` returns in services are grandfathered, but **do not add new ones**. When you touch one for an unrelated reason and the surface is small, convert it.

### Argument order -- stable first, variable last

Put **categorical / constant** arguments (error code, type, mode flag) BEFORE **dynamic / contextual** arguments (message text, payload). This matches the convention LLMs are trained on (most Python stdlib follows it: `logging.log(level, msg)`, `raise SomeError(code, message)`), so models will get the call sites right by default.

```python
# BAD -- LLMs will guess the order wrong
formatter.error(message="Bucket not found", error_code=ErrorCode.NOT_FOUND)

# GOOD -- category first, then the variable part
formatter.error(error_code=ErrorCode.NOT_FOUND, message="Bucket not found")

def log_failure(error_code: ErrorCode, message: str) -> None: ...
def raise_api_error(error_code: ErrorCode, *, message: str, status: int) -> None: ...
```

Required positional ordering ONLY when callers will pass positionally; otherwise keyword-only via `*,` and the order is moot at call sites but still matters in the signature for readability.

### Error codes -- enum only, never raw strings

All error codes go through `ErrorCode` (`src/keboola_agent_cli/errors.py`). Raw string literals like `"bucket_not_found"` or `"invalid_token"` are forbidden in `raise`, `formatter.error(error_code=...)`, and anywhere they cross a layer boundary. `make check-error-codes` rejects raw `error_code="..."` literals at CI time.

```python
# BAD
raise KeboolaApiError(message="...", error_code="not_found")

# GOOD
from .errors import ErrorCode
raise KeboolaApiError(error_code=ErrorCode.NOT_FOUND, message="...")
```

If a new category appears, **add it to `ErrorCode`** and `_ERROR_CODE_TO_TYPE` in the same PR. Do not introduce ad-hoc strings.

### File-size budgets -- split when concerns drift

Budgets are measured in **code lines**, not raw line count. Docstrings, comments and blank lines do **not** count.

```bash
make loc-check      # the gate; part of `make check`
make loc-report     # every module by code lines, largest first
make loc-baseline   # re-record grandfathered files AFTER a split
```

| Layer | Soft ceiling | Hard ceiling |
|-------|--------------|--------------|
| `commands/*.py` | 800 | 1200 |
| `services/*.py` | 1000 | 1500 |
| `client/*.py` (per module) / `manage_client.py` | 1500 | 2000 |
| `server/*.py` | 800 | 1200 |
| `sync/*.py` | 1000 | 1500 |
| everything else in the package | 1000 | 1500 |

**Why code lines and not LOC.** This codebase deliberately writes long rationale-carrying docstrings -- they are the reason it stays navigable, for humans and for the AI agents that work in it. A raw-LOC budget taxes exactly that and pushes toward *less* explanation, which is backwards. The gap is not marginal: `services/version_service.py` is 1252 lines but 705 lines of code (36% prose), and `constants.py` is 574 lines but 190 lines of code (56% prose). Run `make loc-report` for the current numbers rather than trusting these.

The line the metric draws: a **docstring** (the bare leading string of a module, class or function) is prose and is exempt. A string **assigned to a name** -- a SQL block, a template, the `CHANGELOG` tables -- is data, is counted, and cannot be used to hide content from the budget.

**Soft vs hard.** Crossing the **soft** ceiling means the next PR that adds material to the file should split it first; `loc-check` prints a warning but stays green. Crossing the **hard** ceiling fails the check: split before merging more functionality.

**The grandfather ratchet.** Files that were already over their hard ceiling when the gate landed are recorded in `scripts/file_size_baseline.json` at their then-current size. They are allowed to stay that big but **may only shrink** -- growing one fails `loc-check`. That is what lets the gate block on day one without demanding a repo-wide refactor first: it stops new debt and stops existing debt getting worse. After you split a baselined file, run `make loc-baseline` to re-record it. Never run it to silence a file you just grew -- the diff makes that obvious in review.

Two files are exempt outright (`scripts/check_file_size.py` `_EXEMPT`): `changelog.py` and `commands/context.py` are documentation payloads that happen to live in `.py` files, and a ceiling on them would only push prose out of the repo. Keep that list short -- an exemption is an admission the budget does not model the file.

How to split:
- A client mixing multiple Keboola subsystems (Storage, Queue, Sandboxes, ...) → split by **endpoint family** into a package, e.g. `client/storage_tables.py`, `client/queue.py`, `client/configs.py`, composed into one class via mixins. Keep `BaseHttpClient` shared. (This is exactly what `client.py` -> the `client/` package was in #520.)
- A service crossing the ceiling almost always mixes orchestration with parsing/transformation → extract pure helpers into a sibling `_helpers.py` or `_transformers.py`.

This is a guideline driven by review feedback (kbagent 0.31.0: `client.py` ≈3000 LOC, `storage_service.py` ≈2180 LOC, `sync_service.py` ≈2765 LOC); the soft ceilings exist so the situation does not get worse before it gets better.

### Resource management -- `with` over lambdas

LLM-generated code routinely wraps `open()`/`httpx.Client()`/temp-file/lock creation in a lambda or a "create-and-forget" call, leaking file descriptors or connections. **Use a context manager every time the resource has `__enter__`/`__exit__`.**

```python
# BAD -- descriptor leaks if anything raises
opener = lambda: open(path, "r")  # noqa: avoid-lambda-as-resource
content = opener().read()

# BAD -- httpx client not closed on exception
client = httpx.Client()
response = client.get(url)

# GOOD
with open(path) as f:
    content = f.read()

with httpx.Client() as client:
    response = client.get(url)
```

### Named functions over throwaway lambdas

Single-expression `sort` keys and `filter` predicates are fine as lambdas. Anything else -- assigned to a variable, used multiple times, doing branching, or carrying domain meaning -- gets a named `def`. Names are the cheapest documentation in the codebase.

```python
# BAD
parse_row = lambda r: {"id": r[0], "name": r[1], "active": r[2] == "Y"}
rows = [parse_row(r) for r in raw]

# GOOD
def _parse_storage_row(raw: tuple[str, str, str]) -> dict[str, Any]:
    return {"id": raw[0], "name": raw[1], "active": raw[2] == "Y"}

rows = [_parse_storage_row(r) for r in raw]

# FINE -- single expression, throwaway, no domain meaning
items.sort(key=lambda x: x.priority)
```

### Type checking -- `ty` is mandatory and BLOCKING

We use Astral's [`ty`](https://github.com/astral-sh/ty) (same vendor as `uv` and `ruff`). It is fast (Rust), installs in <1s, and runs on every edit via the post-edit hook in `.claude/settings.json`. It also runs in the pre-commit hook and **blocks the commit on any type error** (the whole backlog was cleared in 0.45.0, see issue #280 PR-3), and is exposed via `make typecheck`.

Rules:
- **All code** -- `make typecheck` must stay clean (0 diagnostics). Adding any `# ty: ignore[rule]` requires a one-line comment explaining why (reserve it for genuinely dynamic surfaces, e.g. third-party stubs that mistype a runtime-valid argument).
- **No regressions** -- a newly introduced type error blocks both the post-edit and pre-commit hooks; fix it before continuing. Warnings (e.g. the downgraded `unresolved-import` rule) do not block.
- Type-hint every function signature (already a rule in "Python conventions" above); `ty` enforces that the hints are *correct*, not just present.

```bash
make typecheck         # full check, exit code reflects pass/fail
make typecheck-warn    # same, but always exit 0 (used by hooks)
```

## Keboola API Best Practices

### Reference implementation

The official Keboola CLI is written in Go: https://github.com/keboola/keboola-as-code

Before implementing any Keboola Storage API integration, **check how the official CLI does it**.
It is the authoritative source for correct API usage patterns -- endpoint selection,
async vs. sync behavior, polling strategies, and error handling. If our implementation
diverges from theirs, we need a documented reason why.

### Prefer async API endpoints over sync shortcuts

Many Storage API operations offer both sync and async variants. Sync endpoints are
simpler but have lower limits (e.g., file size caps, timeouts). Always use the async
variant for production code unless there is a specific reason not to.

Use `_wait_for_storage_job()` from the client (`client/_core.py`) for polling -- it already handles
intervals, backoff, timeout, and error extraction.

### Graceful resource creation (UX principle)

When the user's intent is clear (e.g., "upload data to this table"), don't force them
to manually create every prerequisite. If a bucket or table doesn't exist yet and can
be inferred from context, create it automatically and log what you did. The official
KBC CLI follows this pattern -- see `EnsureBucketExists()` in their codebase.

## Security Principles

### Permission engine -- register every new operation

Every CLI command must be registered in `OPERATION_REGISTRY` in `src/keboola_agent_cli/permissions.py`.
This maps operations to risk categories:

| Category | Examples | Risk |
|----------|----------|------|
| `read` | list, detail, status, search | No side effects |
| `write` | create, update, upload, load | Creates or modifies data |
| `destructive` | delete, force-delete | Destroys data |
| `admin` | org setup, project add/remove | Infrastructure-level |

**If you add a new command and forget to register it, the permission engine silently allows it** -- even when the user has set a restrictive policy. This is a security gap. Treat unregistered operations as a bug.

Pattern: `"{subapp}.{command}": "{category}"`, e.g.:
```python
"storage.create-bucket": "write",
"storage.create-table": "write",
"storage.upload-table": "write",
```

### Token security

- Tokens are **never** printed in full -- use `mask_token()` from `errors.py`
- Manage tokens: never persisted, never in CLI args, never logged
- Master tokens: via env var only (`KBC_MASTER_TOKEN` / `KBC_MASTER_TOKEN_{ALIAS}`)
- Never commit secrets to git -- not in code, not in docs, not in test fixtures

### Input validation for API-bound data

Validate user-supplied values before sending them to Keboola API:
- Column types against known base types: `STRING`, `INTEGER`, `NUMERIC`, `FLOAT`, `BOOLEAN`, `DATE`, `TIMESTAMP`
- Bucket stages: `in`, `out`
- File existence before upload attempts
- Table/bucket ID format where reasonable

Fail fast with clear error messages rather than letting the API return opaque errors.

## Checklist: Adding a New CLI Command

When adding a new command (e.g., `kbagent storage create-foo`), you must update **all** of these:

### Code changes

- [ ] **Client method** in the relevant `client/*.py` mixin (or `manage_client.py`) -- HTTP layer
- [ ] **Service method** in `services/` -- business logic, validation, orchestration
- [ ] **Command function** in `commands/` -- Typer options, formatter, error handling
- [ ] **Permission registration** in `permissions.py` (`OPERATION_REGISTRY` dict)
- [ ] **Service wiring** in `cli.py` if adding a new service class
- [ ] **HTTP API endpoint** in `src/keboola_agent_cli/server/routers/<group>.py` -- `kbagent serve` exposes the CLI as a REST API so external applications (Web UI, scheduled AI agents, Slack bots, Streamlit dashboards, CI pipelines) can call the platform without forking CLI subprocesses. The current convention is **1:1**: every command in a group has a matching endpoint in that group's router (e.g. `commands/flow.py` has 8 commands, `server/routers/flows.py` has 8 routes). If you add a new command, add the corresponding route. **Skip allowed** only for genuinely terminal-only commands (interactive prompts, Rich-rendered output that has no useful JSON shape, `doctor`/`init`/`update`-style infrastructure that manages kbagent itself rather than Keboola). Document any skip in the PR description with a one-line reason so reviewers don't flag it.

### Documentation changes (mandatory!)

CI catches drift in `SKILL.md` (decision table), `plugin.json` (version), and
`changelog.py` (release entries) -- but **NOT** in any of the hand-maintained
files below. Forgetting a hand-maintained file is a silent failure: tests pass,
lint passes, then three weeks later an AI agent quietly recommends a command
that does not exist (or refuses one that does). Treat the change as **not
done** until every box below is ticked.

#### CLI surface (kbagent itself)

- [ ] **`kbagent context`** -- update `AGENT_CONTEXT` string in `src/keboola_agent_cli/commands/context.py`. This is the primary reference loaded by AI agents at session start; if a command is missing here, agents will not know it exists.
- [ ] **`CLAUDE.md` `## All CLI Commands`** -- add the new command signature. Hand-maintained; must match `kbagent --help`.
- [ ] **`--help` text** -- Typer docstring and option help strings must read like a man-page entry. They are the ultimate fallback when documentation drifts.

#### Auto-generated (CI-checked, but you must run the generator)

- [ ] **`SKILL.md` decision table** -- run `make skill-gen` and commit the diff. **Do NOT edit the table by hand** -- the markers will be overwritten on the next `make skill-gen`. The pre-commit hook auto-regenerates and stages this file.
- [ ] **`plugin.json` version** -- bumped via `make version-sync` from `pyproject.toml`. The pre-commit hook auto-stages this file; you should never edit it by hand.

#### Plugin (`plugins/kbagent/`) -- HAND-MAINTAINED, NO CI CHECK

These files are how the Claude Code plugin teaches AI agents to use kbagent.
**None of them have a freshness check.** A failure here ships silently and
manifests as a drifted, unhelpful AI agent. Cross every one of them off
before the PR is mergeable.

- [ ] **`plugins/kbagent/agents/keboola-expert.md`** -- the subagent system prompt. **Highest silent-drift risk in the repo.** Update at minimum:
  - [ ] **§1 Rule 6 VERSION GATE** examples (e.g. `flow update needs 0.22.0+`) when adding a command that introduces or relaxes a minimum-version requirement, or when an example version reference is now stale enough to mislead.
  - [ ] **§2 Tool Selection Matrix** -- one row **per command GROUP**, not per command. When you add a new write/destructive *group* (e.g. `dev-portal`), give it a single row with `First choice / Fallback / NEVER`. Adding a command to an *existing* group needs no new row. Exhaustive per-command detail belongs in `AGENT_CONTEXT` (`kbagent context`), which is loaded dynamically on demand -- `keboola-expert.md` is a static system prompt loaded into every subagent run and carries a hard 70 000 B budget, so it must stay a high-signal decision matrix, not a command catalogue. If a one-row addition would push the file over budget, trim stale content first; do **not** raise the cap. The ceiling is enforced by `tests/test_agent_prompt.py::TestPilotAgentFile::test_agent_prompt_under_token_budget`, which is the single source of truth -- check with `wc -c plugins/kbagent/agents/keboola-expert.md` before adding, and remember CI builds the merge commit, so a PR that is individually under budget can still fail once `main` has moved. *Severity note:* authors are expected to add the group row, but `/kbagent:review` flags a missing row only **NON-BLOCKING** -- `AGENT_CONTEXT` (a BLOCKING surface above) is the authoritative command catalogue, so a missing matrix row degrades subagent ergonomics without making a command undiscoverable. Don't deprioritize it just because it's non-blocking.
  - [ ] **§3 Inline Gotchas** when behavior changed in a way the agent will get wrong by default (e.g. dev-branch auto-materialization, native column-type whitelisting).
- [ ] **`plugins/kbagent/skills/kbagent/SKILL.md`** non-table portions -- update the `description:` trigger keywords when introducing a new topic area (so description-matching auto-triggers the skill); add a workflow row to the bottom table if you created a new `references/<topic>-workflow.md`.
- [ ] **`plugins/kbagent/skills/kbagent/references/commands-reference.md`** -- add the new command bullet under the appropriate section. Hand-maintained, NOT auto-generated. (Yes, this partly duplicates the auto-generated SKILL.md table -- the reference carries denser per-command notes, the table is the at-a-glance picker.)
- [ ] **`plugins/kbagent/skills/kbagent/references/gotchas.md`** -- if the command's behavior is non-obvious, add an entry tagged with a version. In a feature PR that version is not known yet (the PR does not bump the version), so tag with the literal placeholder `(since vNEXT)` -- the release PR replaces every `vNEXT` with the version actually being released. Never guess a numeric version: `make version-gate-check` (per-PR CI) rejects any `(since vX.Y.Z)` whose version has no `changelog.py` entry. The version tag is **non-optional**; gotchas without versions are how AI agents end up recommending behavior that does not exist on older kbagent installs.
- [ ] **`plugins/kbagent/skills/kbagent/references/<topic>-workflow.md`** -- create a new file if the command introduces a new workflow or topic area (existing examples: `workspace-workflow.md`, `branch-workflow.md`, `sync-workflow.md`, `storage-files-workflow.md`, `storage-types-workflow.md`). Single-command additions go into an existing workflow file.
- [ ] **`plugins/kbagent/.claude-plugin/CLAUDE.md`** -- only update when the high-level delegation strategy changes (e.g. new "when NOT to delegate" cases). Most command additions do not touch this.
- [ ] **`plugins/kbagent/commands/*.md`** -- only update if a slash-command UX changes (`/keboola`, `/kbagent:setup`, `/kbagent:review`). Most command additions do not touch these. Adding a *new* slash-command file has its own follow-through list -- see the [Plugin synchronization map](#plugin-synchronization-map) row.

### Tests (mandatory!)

- [ ] **Service-layer tests** -- mock the client, test business logic, edge cases, error propagation
- [ ] **CLI-layer tests** -- use `CliRunner`, test JSON output, error exit codes
- [ ] **E2E tests** -- add a test in `tests/test_e2e.py` that exercises the command against a real Keboola project (requires `E2E_API_TOKEN` + `E2E_URL`). Run `make test-e2e` to verify. Every CLI command must have E2E coverage

  > **Running locally without exporting a token:** if the target project is already registered in a kbagent `config.json`, use config-dir mode -- `make test-e2e-local CONFIG_DIR=/path/to/.kbagent ALIAS=my-proj`. The harness reads the token from `config.json` at import time and promotes it into `E2E_API_TOKEN` / `E2E_URL`; an explicit `E2E_API_TOKEN` still wins.

- [ ] **Run `make check`** before committing (lint + format + full test suite)
- [ ] **Run `make typecheck`** -- `ty` must pass clean (0 diagnostics; the backlog was cleared in 0.45.0, so the gate is blocking, not warning-only)
- [ ] **No new `tuple[...]` returns** -- multi-value returns use a `@dataclass` ([Code Quality Patterns](#code-quality-patterns))
- [ ] **No raw error-code strings** -- `make check-error-codes` enforces `ErrorCode` enum usage
- [ ] **Session sentinels stay guarded** -- `make check-sentinel-guards` enforces it; if you added an HTTP client, declare `SESSION_AUTH_FEATURE` (or record why it is bearer-capable), and if you added a guard, add the matching entry to `SESSION_UNSUPPORTED_FEATURES`
- [ ] **File-size budgets respected** -- see the table in [Code Quality Patterns](#code-quality-patterns); split before crossing the hard ceiling

### UX considerations

- [ ] Human-mode output is informative (sizes, counts, progress indicators)
- [ ] JSON-mode output includes all relevant fields for programmatic consumption
- [ ] Error messages are actionable ("Bucket not found" not just "404")
- [ ] Destructive operations have `--dry-run` and `--yes` flags
- [ ] Write operations log what they did (created X, uploaded Y rows)

## Extending the importable SDK

Besides the CLI, kbagent ships an **in-process Python SDK** -- the importable
`Client` facade (`lib.py`) and its typed result models (`result_models.py`),
re-exported from the package root. A Keboola Data App, a transformation, or any
Python service can `from keboola_agent_cli import Client` and use Query Service
SQL, Storage Files, run-job, and config detail **without** a CLI subprocess or a
`kbagent serve` daemon.

**Everything exported from `keboola_agent_cli.__all__` is committed public API
under semver.** Changing it is a deliberate act, not a side effect of touching a
service. The full architecture, method reference, and the step-by-step checklist
for adding a facade method or a result model live in **[docs/sdk.md](docs/sdk.md)**
(see "Extending the SDK"). The short version:

- [ ] **Facade methods go in `lib.py`** and call `KeboolaClient` directly --
  never import the service layer (it carries config-dir / orchestration
  assumptions the stateless facade must not inherit). Re-assemble the
  high-level shape yourself, and **state in the docstring** which service
  conveniences you intentionally omit (auto-create, alias/variable resolution).
- [ ] **Return a typed model** (`result_models.py`), not a bare dict, for any
  non-trivial shape. Subclass `_ApiResultModel` (`extra="allow"` +
  `populate_by_name`); type **only** the stable subset, alias raw API keys via
  `AliasChoices`, and never `extra="forbid"` (the API grows fields and the
  contract must not raise).
- [ ] **Export it** from `__init__.py` + `__all__` -- that list *is* the public
  surface. Treat a field rename or a type tightening as a **breaking change**;
  prefer adding over mutating.
- [ ] **`make typecheck` stays clean** (types are a user-facing promise here),
  add facade/model tests in `tests/`, and **document the addition in
  [docs/sdk.md](docs/sdk.md)** (and the README "Use as a library" one-liner if
  it's a headline capability).

A runnable teaching example -- a curses Storage browser built entirely on the
SDK -- lives in [`examples/storage_tui/`](examples/storage_tui/).

## Plugin synchronization map

Single-glance reference for "I changed the CLI -- what else must follow?".
Use this table to cross-check the per-command checklist above and the
release checklist below.

| File | When to update | CI catches drift? |
|------|----------------|-------------------|
| `pyproject.toml` (`version`) | Every release -- in the dedicated release PR ONLY, never a feature PR | -- (single source of truth) |
| `src/keboola_agent_cli/changelog.py` | Every release -- in the dedicated release PR ONLY, never a feature PR | YES (`make changelog-check`, both directions -- every release has an entry AND every entry has a release) |
| `src/keboola_agent_cli/commands/context.py` (`AGENT_CONTEXT`) | Adding/removing/renaming commands; significant flag changes | NO |
| `src/keboola_agent_cli/server/routers/<group>.py` | Adding/removing/renaming commands -- `kbagent serve` mirrors the CLI 1:1 for external consumers (Web UI, scheduled agents, third-party apps). Skip only for terminal-only / kbagent-infrastructure commands; document skip in PR | NO -- callers get HTTP 404 instead of "command works in CLI but not via API" silent gap |
| `docs/web-server-endpoints.md` | Auto-generated by `make endpoints-gen` -- run it after adding/removing/renaming ANY route or changing its summary | YES (`make endpoints-check`) |
| `docs/web-server.md` | Architecture, auth, concepts, router categories. Never enumerate routes here -- that is the generated file's job (this doc drifted to "150+ endpoints" over a 226-operation server, issue #656) | NO (prose); the route list it used to carry is now gated |
| `CLAUDE.md` (`## All CLI Commands`) | Adding/removing/renaming commands | NO |
| `plugins/kbagent/.claude-plugin/plugin.json` | Every release (auto-synced) | YES (`make version-check`; pre-commit auto-stages) |
| `.claude-plugin/marketplace.json` (this repo -- **deprecated shim**) | Never by hand except the entry `description`. The `version` is auto-synced; the file and its `kbagent` entry MUST stay so installs made from the old `keboola-agent-cli` marketplace keep resolving updates. Planned removal: ~3 releases after vNEXT (the release that first ships the deprecation notice) | YES (`make version-check`; pre-commit auto-stages) |
| **`keboola/ai-kit` -> `.claude-plugin/marketplace.json`** (ANOTHER REPO) | Every stable release -- this is where the plugin is actually published (`keboola-claude-kit`, an external `git-subdir` entry pinned to the release tag). Automated: the `ai-kit-marketplace` job in `.github/workflows/release-kbagent.yml` rewrites `version` + `source.ref` and opens a PR against keboola/ai-kit. **Merging that PR is what ships the release to plugin users** -- a green release here does not move them | NO -- nothing in this repo can see ai-kit's catalogue. Check the opened PR after every release; if `secrets.AI_KIT_TOKEN` is missing or expired the job fails and no PR appears |
| `plugins/kbagent/.claude-plugin/CLAUDE.md` | Changing delegation strategy / when-to-delegate rules | NO |
| `plugins/kbagent/agents/keboola-expert.md` | New write/destructive command **group** (one matrix row per group, not per command -- file has a hard 70 000 B prompt budget); new minimum-version requirement (Rule 6 VERSION GATE); behavior change (gotchas) | NO -- **highest silent-drift risk** |
| `plugins/kbagent/commands/*.md` (`setup.md`, `keboola.md`, `review.md`) | Slash-command UX change (rare). **Adding a new slash-command file** also needs: the surfaces list + "For Claude Code users" block in `plugins/kbagent/.claude-plugin/CLAUDE.md`, `skills/kbagent/SKILL.md` prose if it changes the documented setup/usage path, and the user-facing flow in `README.md`, `docs/TUTORIAL.md`, `commands/context.py` `AGENT_CONTEXT` and `install.sh`'s "Next steps" | NO -- no CI gate or test reads `commands/*.md` at all |
| `plugins/kbagent/skills/kbagent/SKILL.md` -- table | Auto-generated by `make skill-gen` | YES (`make skill-check`; pre-commit auto-stages) |
| `plugins/kbagent/skills/kbagent/SKILL.md` -- description / rules / workflow links | New topic area in `description` triggers; new workflow file added to bottom table | NO |
| `plugins/kbagent/skills/kbagent/references/commands-reference.md` | Adding/removing/renaming commands; flag changes | NO |
| `plugins/kbagent/skills/kbagent/references/gotchas.md` | New non-obvious behavior -- always tag with a version (`(since vNEXT)` in feature PRs; the release PR rewrites it to `(since vX.Y.Z)`) | PARTLY (`make version-gate-check` proves a numeric tag names a released version, and a version-raising PR additionally fails on any unresolved `vNEXT`; whether the behavior deserves a tag AT ALL is still judgement, so a *missing* entry ships silently) |
| `plugins/kbagent/skills/kbagent/references/<topic>-workflow.md` | New workflow / topic area introduced | NO |
| `web/frontend/src/whatsnew.ts` | Every release that ships UI-visible features -- a `WhatsNewRelease` entry keyed by the exact new version (release PR only; a feature PR cannot know the version) | NO -- without an entry the release's UI work ships dark: the popup falls back to the previous reel, which returning users have already dismissed |
| `plugins/kbagent/skills/<sibling-skill>/` (e.g. `kbagent-promotion-pipeline`) | Adding a **sibling skill** -- a self-contained skill directory next to `kbagent/`, used when the topic ships executable `scripts/` + tests or needs its own `description` triggers rather than being one more `references/*.md`. Must ALSO be linked from `kbagent/SKILL.md`'s bottom table, otherwise an agent already inside the `kbagent` skill can never discover it | NO -- `make skill-check` only regenerates `kbagent/SKILL.md` and never looks at sibling skills |

Anything tagged "NO" in the right column is a **silent failure mode**: lint
passes, tests pass, the AI agent goes off the rails three weeks later. The
per-command checklist (above) and the per-release checklist (below) exist
to catch this before the change ships.

## Commit & PR Conventions

- **No `Co-Authored-By`** lines in commit messages
- **No AI attribution footers** in PR descriptions
- **No version bumps in feature PRs** -- `pyproject.toml`'s `version` and
  `src/keboola_agent_cli/changelog.py` are touched ONLY by a dedicated release
  PR (see [Releasing a new version](#releasing-a-new-version)). Parallel PRs
  each bumping the version collide on merge and silently renumber releases.
  Tag any new version-gated documentation with the `vNEXT` placeholder
  (`(since vNEXT)`); the release PR replaces it with the real version, and CI
  fails that PR if any placeholder survives.
- **Conventional commits**: `feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`
- **One logical change per commit** -- don't mix unrelated fixes
- **Pre-commit hook must pass** -- `ruff check` + `ruff format --check`. Install via `make hooks`
- **Never skip hooks** (`--no-verify`) -- fix the lint issue instead
- **Protected main branch** -- always work on a feature branch, create PR, merge via GitHub
- **Self-review with `/kbagent:review` before tagging a human** -- see
  [Self-review before tagging a human reviewer](#self-review-before-tagging-a-human-reviewer)
  for what it does and how to run it. CI does not catch the silent-drift
  surfaces (Plugin synchronization map); the self-review does.

For reference on commit style: https://github.com/padak/claude-code-kit/blob/main/CLAUDE.md

## Self-review before tagging a human reviewer

Before you ping a maintainer, **run the `/kbagent:review` slash command
against your open PR**. It is a read-only specialist subagent
(`kbagent-pr-reviewer`, shipped with the kbagent Claude Code plugin) that
walks the same playbook a careful human reviewer would: 3-layer compliance,
[Plugin synchronization map](#plugin-synchronization-map) silent-drift hunt,
test coverage, behavior verification, backward compatibility, security and
token discipline. It posts ONE structured comment review on the PR with
findings rated BLOCKING / NON-BLOCKING / NIT, each carrying a `file:line`
citation.

### How to run it

1. Push your branch and open the PR (`gh pr create ...`).
2. Stay checked out on the PR's branch with a clean working tree.
3. Confirm `gh auth status` is authenticated to the same fork as the PR.
4. In a Claude Code session in the repo root, type:

   ```
   /kbagent:review
   ```

   The slash command auto-detects the PR for the current branch. To target a
   different PR explicitly:

   ```
   /kbagent:review 234
   /kbagent:review https://github.com/keboola/cli/pull/234
   /kbagent:review 234 focus on the new cache semantics
   ```

5. The reviewer reads `CONTRIBUTING.md`, walks the diff, runs `make check`,
   attempts to reproduce the PR's claimed behavior, and posts a single
   `gh pr review --comment` to the PR. It NEVER approves, requests changes,
   merges, or pushes -- the verdict in the comment body is advice; you and
   the human reviewer retain every veto.

### What to do with the findings

- **BLOCKING** -- address before tagging a human, OR push back in a PR
  comment explaining why you disagree. Some BLOCKING findings are
  calibration mistakes; the reviewer defaults conservative, and a
  ~30-second human disposition is faster than a re-run.
- **NON-BLOCKING** -- address if quick; otherwise mention them in the PR
  description so the human reviewer knows they are not regressions hiding
  in the diff.
- **NIT** -- optional. Address if you agree.

### This is a courtesy, not a CI gate

The reviewer is intentionally NOT wired into CI. It depends on Claude Code
with the kbagent plugin installed and an authenticated `gh`, which is not
portable across all contributor setups. Running it remains a per-author
courtesy that:

- catches the silent-drift gaps (`OPERATION_REGISTRY`, `gotchas.md`
  version tags, `keboola-expert.md` matrix, `commands/context.py`
  `AGENT_CONTEXT`, `commands-reference.md`) that CI does not check;
- demonstrates to the human reviewer that you have walked the
  [Plugin synchronization map](#plugin-synchronization-map);
- saves a review round-trip when the reviewer would otherwise catch the
  same issues.

If you genuinely cannot run it (offline, no `gh` auth, plugin not
installed), say so explicitly in the PR description (`self-review
skipped: <reason>`) -- the human reviewer may run it on your behalf, or
ask you to address it before merge.

## Testing Guidelines

- Use `typer.testing.CliRunner` for CLI tests
- Use `unittest.mock.MagicMock` for mocking services and clients
- Use `pytest` fixtures from `conftest.py` (`tmp_config_dir`, `config_store`, etc.)
- Test both success and error paths
- Test JSON output parsing (`json.loads(result.output)["data"]`)
- Verify `client.close()` is called (via `mock_client.close.assert_called_once()`)
- Test edge cases: missing project alias, API errors, invalid input
- Match test file naming: `test_{feature}.py` or `test_{feature}_cli.py`

## Releasing a new version

A release is a **dedicated release PR** -- the only place `pyproject.toml`'s
`version` and `changelog.py` are ever touched. Feature PRs merge to `main`
without any version change; the release PR then batches **everything merged
since the last release** into one version bump, one changelog entry, and one
set of release notes.

> **Why not bump in each feature PR?** PRs are developed in parallel (often
> one AI session per issue). When each bumps the version, every merge is a
> `pyproject.toml`/`changelog.py` conflict, and resolving the conflicts by
> merging all of them renumbers releases after the fact. The
> `KNOWN_UNRELEASED` list in `scripts/generate_changelog.py` -- 18 versions
> whose content shipped silently, folded into a later release's wheel with no
> release notes -- is the accumulated damage of exactly that pattern.

Open the release PR from its own branch, walk this checklist end-to-end,
merge it, then tag the resulting `main` commit (the release pipeline renders
the GitHub release notes from the changelog entry via
`scripts/gen_release_notes.py`). The point of steps 7-11 is that **CI will not
catch you** if you skip them; they are the manual safety net for the
silent-drift risks summarized in the
[Plugin synchronization map](#plugin-synchronization-map) above.

> **Want to ship a beta first?** You can. PEP 440 pre-release versions
> (`0.43.0b1`, `0.43.0rc1`) are fully supported by `kbagent update --beta`
> since v0.42.0. The startup auto-update hook never silently lands on a
> beta -- only explicit opt-in installs them. See
> [Releasing a beta (pre-release) version](#releasing-a-beta-pre-release-version)
> below for the workflow.

1. **Collect the raw material** -- find the last released tag
   (`gh release list --limit 1` or `git describe --tags --abbrev=0`), then list
   every PR merged since it:
   ```bash
   git log v<last>..origin/main --oneline --first-parent
   ```
   (or `gh pr list --state merged --base main --search "merged:><last-release-date>"`).
   Those merged PRs are **exactly** the scope of the release: the changelog
   entry and the release notes must cover each of them, and nothing else.
2. **Edit `pyproject.toml`** -- bump `version = "X.Y.Z"`. Single source of truth; everything else derives from it. This is the release PR's defining change -- if you are doing this in a feature PR, stop and read the section intro above.
3. **Add a changelog entry** to `src/keboola_agent_cli/changelog.py` -- ONE entry for the new version, covering **every PR merged since the last release** (step 1), no exceptions. CI fails (`make changelog-check`) if this is missing. Author it as the file's docstring describes: **one logical change per bullet** (split the release into several list items rather than one mega-paragraph), each starting with a recognised prefix (`BREAKING:`, `New:`, `Fix:`, `Change:`, `Note:`, `Security:`, ...), carrying its `(#PR)` reference, and leading with a self-contained first sentence. `kbagent changelog` shows only that first sentence per version by default (the rest is revealed by `--full`), so a buried headline or a single wall-of-text bullet reads as an unscannable blob. The first sentence is also **capped at 160 characters**, enforced by `tests/test_changelog_render.py::TestLiveChangelogHeadlines::test_newest_release_notes_are_not_truncated` (so `make check` in step 12 catches it) -- past the cap the default view and the release page show it cut mid-clause. Write a short self-contained first sentence and put the detail in the sentences after it; 2 of 0.90.0's 13 bullets needed exactly this rewrite.
4. **Replace every `vNEXT` placeholder** left behind by the feature PRs with the version being released. Do it mechanically -- never by hand, and never with a repo-wide `sed`:
   ```bash
   make vnext-resolve VERSION=X.Y.Z
   make vnext-check
   ```
   `vnext-resolve` reuses the same scanner `vnext-check` does, so it rewrites
   exactly the live gates and leaves every backticked mention of the token
   alone -- including a line that carries both at once, which a line-level
   `sed` corrupts. It refuses any `VERSION` that disagrees with
   `pyproject.toml` (bump that first, in step 2): `packaging` happily parses
   `v0.91` and `0.91`, so a typo can look valid and then be stamped into every
   gate in the tree at once.
   A leftover `(since vNEXT)` ships agents a gate no installed version can
   ever satisfy -- strictly worse than no gate, because they then refuse a
   command the user has. The release PR is the only place it can be fixed.

   **This is now CI-enforced.** The `check` job runs the same check on any PR
   that RAISES `pyproject.toml`'s version, so a missed placeholder is a red
   build rather than a silent ship. A feature PR is unaffected: writing
   `vNEXT` there is required, and the gate stays disarmed until a version
   bump. (`make check` deliberately does NOT include it -- it would fail every
   feature PR run locally.)

   > This step used to be a hand-run `grep -rn "vNEXT" ...` documented as
   > "MUST come back empty". It never could: the process docs (this file,
   > `CLAUDE.md`) have to *mention* the placeholder they describe, so every
   > release forced a fresh eyeball-classification of each hit. The check
   > separates the two mechanically -- **a `vNEXT` inside an inline-code span
   > is prose quoting the token; outside one it is a live gate**. Validated
   > against the pre-0.90.0 tree: 16 real gates found, 4 prose mentions
   > ignored, no allowlist. Note the rule is deliberately *not* applied to
   > numeric gates -- `docs/sdk.md` writes 14 genuine ones as `` `0.66.0+` ``,
   > where backticks are ordinary typography rather than quotation.

   Version tags must stay **out of markdown headings**: a
   `### Foo *(since vNEXT)*` heading changes its generated anchor slug when the
   placeholder resolves, breaking each inbound `#foo-...` link (this bit 0.90.0 --
   the What's-new section's link broke the moment the placeholder resolved).
   Put the tag on the section's first body line instead; the gate checks scan
   whole files, not just headings, so nothing is lost.

   **This is CI-enforced on EVERY PR**, not just at release time -- a `vNEXT`
   inside an ATX heading in a `.md` file fails `make version-gate-check`
   (already part of `make check`). It is deliberately armed everywhere rather
   than only under `--release`, because the rule used to be a hand-run
   `grep -rn '^##.*vNEXT' plugins/` at release time and that grep **lost a
   merge race in 0.91.0**: PR #697 ran it two minutes before #694 and #696
   landed headings of their own, so all three shipped and had to be cleaned up
   after the tag. Any rule of the form "run this grep when releasing" loses
   that race eventually, because a release is exactly when parallel branches
   converge. Already-numeric headings are *not* flagged -- a resolved tag never
   changes again, so its slug is stable.
4b. **Retire gates below the floor** (periodic, not every release):
   ```bash
   make gate-floor-report                     # what is below the current floor
   ```
   A version gate earns its place only while some live install predates it.
   kbagent self-updates on startup, so that population shrinks to roughly
   nothing: pip/uv installs upgrade themselves, and only a standalone binary
   (brew/choco/apt/dnf, which self-update is disabled for), an explicit
   `KBAGENT_AUTO_UPDATE=false`, a dev tree, or a pip install stranded below
   0.62.0 by the #424 rename can sit on an old version. Meanwhile the stale
   gate keeps making the agent refuse a command the user actually has -- which
   this file already calls strictly worse than no gate.

   The two failure modes are asymmetric, and that is the whole argument for
   pruning: a **kept-too-long** gate fails silently and permanently (the user
   never learns the command exists), while a **removed-too-early** gate fails
   loudly and self-correctingly (`No such command 'x'`, and `kbagent context` /
   `--help` on the user's own install are authoritative anyway).

   **The floor is 0.80.0** as of the 0.91.0 cleanup. Retiring a gate means
   deleting the *tag*, never the content -- the guidance under it is almost
   always still true, and 0.91.0's pass kept every word while removing 223 tags.

   Four things are deliberately out of scope:

   - `changelog.py` -- the historical record; the version IS the content.
   - `src/**/*.py` except `commands/context.py` -- developer comments
     (`# DEPRECATED (since 0.43.4)`) are provenance, and no agent reads them.
   - `X+` written inside a sentence -- often load-bearing prose
     (`created by < 0.66.1 stay dormant until re-run on 0.66.1+`).
   - **Safety gates, at any age.** Keep the tag wherever not knowing the
     version causes silent data loss or a false assurance rather than an error
     message -- e.g. `sync pull --force` (pre-0.53.0 it silently stranded local
     edits), the `sync status` / `doctor` plaintext-secret audit (a false
     all-clear on a leaked credential), the manage-token default-deny, and the
     `--deny-writes` firewall.

5. **Run `make version-sync`** -- propagates the new version to `plugins/kbagent/.claude-plugin/plugin.json`. The pre-commit hook does this automatically on `git commit`, but running it explicitly lets you eyeball the diff.
6. **Run `make skill-gen`** -- regenerates the decision table in `SKILL.md`. Idempotent if no commands changed since the previous release.
7. **Add a curated What's-new entry** to `web/frontend/src/whatsnew.ts` when the release ships anything UI-visible -- a `WhatsNewRelease` element keyed by the **exact** new version, newest first. This is the reel the web UI shows once per version; it is deliberately *not* derived from `changelog.py` (see `docs/web-server.md` > "What's-new popup"). Skipping it does not error anywhere: `whatsNewFor` falls back to the previous release's reel, which returning users have already dismissed -- so the release's UI work ships **dark**. A release with no UI-visible changes correctly adds nothing. Only the release PR can write this entry (a feature PR cannot know the version), which is why it lives in this checklist and not the per-command one.
8. **Manually review `plugins/kbagent/agents/keboola-expert.md`**:
   - **§1 Rule 6 VERSION GATE examples** -- if any feature this release shipped (or any feature shipped in a previous release that you missed) was previously missing-and-now-present, document it with the right minimum version. Remove stale "since X.Y.Z" mentions that no longer matter to live users.
   - **§2 Tool Selection Matrix** -- did you add a new write/destructive command *group* since last release? Is it present with one `First choice / Fallback / NEVER` row (per group, not per command)? Mind the hard 70 000 B prompt budget: trim stale content rather than raising the cap. New commands inside an existing group need no new row.
   - **§3 Inline Gotchas** -- new behavior the agent would get wrong by default? Add it.
9. **Manually review `plugins/kbagent/skills/kbagent/references/gotchas.md`** -- every behavior introduced or changed this release that an AI agent would not infer from `--help` should have its own `(since vX.Y.Z)` entry (freshly rewritten from `vNEXT` in step 4, or added now if a feature PR forgot one). The version tag is non-optional.
10. **Manually review `CLAUDE.md` `## All CLI Commands`** -- diff against `kbagent --help` output (and against `kbagent context`). Hand-maintained; CI does not catch drift here.
11. **Manually review `plugins/kbagent/skills/kbagent/references/commands-reference.md`** -- same drill. Hand-maintained, no CI coverage.
12. **Run `make check`** -- lint + format + skill freshness + version sync + changelog completeness + error-code enum + full test suite.
13. **Run `make test-e2e`** if any command changed since the last release -- requires `E2E_API_TOKEN` and `E2E_URL`.
14. **Open the release PR** -- link the merged PRs it covers (step 1) and list every plugin file you touched in the description so reviewers can spot what was missed. Plugin files do not auto-show up in CI failures the way Python files do; reviewers are the second line of defence.
15. **Re-verify the scope against the commit you are about to tag:**
    ```bash
    make release-scope-check                       # in the release PR, before merging
    make release-scope-check SCOPE_ARGS="--head origin/main --ignore-pr <release-PR>"
    ```
    Step 1 collected the scope when the release PR was *opened*; this proves
    the changelog entry covers every PR the **tag will actually contain**. The
    two differ whenever a feature PR merges while the release PR is open --
    which is a structural window, not bad luck, since a release PR stays open
    for as long as its CI runs. It shipped in v0.91.0: #625 merged nine minutes
    before the release PR did, landing inside the tag's tree with no release
    note, and was caught only because the tag happened to be deferred.
    `make changelog-check` cannot see this: it proves every *released version*
    has an entry, never that an entry covers every *commit* under the tag.
    Run before merging and nothing needs ignoring -- the release PR's own
    number is not in the log until its merge commit exists.
16. **Merge via `gh pr merge`, then tag -- the tag push IS the release.** Never push directly to `main` (protected). The only manual action after the merge is:
    ```bash
    git fetch origin && git tag v<X.Y.Z> <merge-commit-sha> && git push origin v<X.Y.Z>
    ```
    The tag must point at the release PR's merge commit on `main` -- the pipeline's `gate` job fails the whole release if the tag's `pyproject.toml` disagrees with the tag name. Pushing it triggers `.github/workflows/release-kbagent.yml`, which does **everything else**: re-runs the gates, renders the release notes from `changelog.py` (`scripts/gen_release_notes.py` -- never write them by hand), publishes to PyPI, freezes the native binaries for all platforms, packages deb/rpm, creates the GitHub Release with every asset attached and fills its body, and updates Homebrew/Chocolatey/WinGet. Do **not** pre-create the GitHub Release by hand: the pipeline keeps a hand-written body untouched, which silently discards the changelog-rendered notes.
17. **Verify the publish** -- the pipeline guards against half-releases, but both guards exist because each failure shipped once (v0.66.1 went out with an empty body, v0.64.0 without a wheel), so look anyway:
    ```bash
    gh run watch $(gh run list --workflow release-kbagent.yml --limit 1 --json databaseId --jq '.[0].databaseId')
    ```
    then confirm `gh release view v<X.Y.Z>` shows a non-empty body rendered from the changelog and both wheels (`keboola_cli-*` + legacy `keboola_agent_cli-*`) among the assets. A `skipped` winget job is normal; any red job is a real signal.
18. **After the tag: merge the ai-kit publish PR.** The `ai-kit-marketplace` job opens
    `chore(kbagent): publish vX.Y.Z` against `keboola/ai-kit`, bumping the `kbagent`
    entry in the `keboola-claude-kit` marketplace to this tag. Until that PR merges,
    `/plugin install kbagent@keboola-claude-kit` still serves the PREVIOUS version --
    the release is not user-visible for plugin users. If no PR appeared, the job
    failed: check `secrets.AI_KIT_TOKEN` in the `release` environment.

If any of steps 8-11 reveal "I should have done this in the PR that introduced
the command, not at release time", **also patch the per-command checklist**
above so the next contributor catches the gap earlier.

### Releasing a beta (pre-release) version

Beta and release-candidate versions follow PEP 440: `X.Y.Zb1`, `X.Y.Zb2`,
`X.Y.Zrc1`, ... -- not the SemVer `-beta.1` form (hatchling and uv require
PEP 440 syntax in `pyproject.toml`'s `version` field).

A beta is the **one exception** to "version bumps only in the release PR":
the pre-release tag and GitHub Release are cut from the feature branch itself,
so the bump deliberately rides that branch -- for the duration of the beta,
the feature branch *is* the release PR. `main` stays on the stable channel
until the stable release PR ships the final version.

Two gates keep stable
users safe from accidentally landing on a beta:

1. **Version string itself.** PEP 440 marks any pre-release suffix as such;
   `pip install keboola-cli` and `uv tool install ...` default to
   **skipping** pre-releases unless the resolver is told otherwise (`--pre`
   for pip, `--prerelease=allow` for uv).
2. **GitHub Release `prerelease: true` flag.** The auto-update startup
   hook calls `GET /releases/latest`, which GitHub explicitly defines as
   "the most recent non-prerelease, non-draft release". Marking the release
   `--prerelease` makes it invisible to the auto-update path.

**Workflow:**

1. Bump `pyproject.toml` to the PEP 440 pre-release version
   (e.g. `0.43.0b1`).
2. Add a changelog entry under that key in `src/keboola_agent_cli/changelog.py`.
3. `make version-sync` propagates the version to `plugin.json`,
   `marketplace.json`, and the `uv.lock` self-version pin.
4. Tag and push: `git tag v0.43.0b1 && git push origin v0.43.0b1`.
5. Create the GitHub release **with the `--prerelease` flag**:
   ```bash
   gh release create v0.43.0b1 --prerelease \
       --title "v0.43.0 — Beta 1" \
       --notes-file release-notes-0.43.0b1.md
   ```
6. Test by installing yourself: `kbagent update --beta` (or set
   `KBAGENT_INCLUDE_PRERELEASE=1` in env). Users who do **not** opt in
   keep getting the latest stable; the new beta is invisible to them.
7. Once the beta cooks long enough, bump to the stable equivalent
   (`0.43.0`), retag, and create the release **without** `--prerelease`
   so auto-update picks it up.

**Rebasing a beta onto a moved `main`.** Tags are immutable and pinned to a
commit; rebasing the feature branch (to clear merge conflicts or pull in
newer `main` fixes) leaves the existing `vX.Y.Zb1` tag pointing at the
now-orphaned pre-rebase commit. Do **not** force-move a published tag --
cut the next pre-release number instead:

1. Rebase the branch and force-push it (`git push --force-with-lease`).
2. Bump `pyproject.toml` to the next beta (`0.44.0b1` -> `0.44.0b2`), add a
   short changelog entry noting "rebased onto current main, no behaviour
   change", and `make version-sync`.
3. Commit + push, then tag the rebased HEAD:
   `git tag v0.44.0b2 && git push origin v0.44.0b2`.
4. `gh release create v0.44.0b2 --prerelease ...`. Leave the old `b1`
   tag/release intact as history -- it documents the earlier base.

Every published tag stays immutable (a tester who pinned `b1` still gets
exactly what `b1` always was), while `kbagent update --beta` resolves to the
highest PEP 440 version -- the freshly rebased `b2`.

**Users opt in two ways:**

- One-shot: `kbagent update --beta` (resolver is told `--prerelease=allow`
  / `--pre`, GitHub query switches to `/releases` and picks the highest
  PEP 440 version including pre-releases).
- Per-session env var: `export KBAGENT_INCLUDE_PRERELEASE=1` -- every
  subsequent `kbagent update` / `kbagent version` in that shell treats
  betas as installable.

**Never persists.** There is no `release_channel: beta` config setting --
each invocation has to opt in. This is deliberate: betas should always be
an active choice, never a forgotten "I once typed --beta six months ago"
foot-gun.

## Running CI Locally

```bash
make check              # CI parity: lint + format + typecheck + skill + version + command-sync + endpoints + changelog + error-codes + sentinel-guards + test
make lint               # Just the ruff linter
make format             # Auto-format code
make typecheck          # Static type check (Astral `ty`)
make test               # Just the test suite (no coverage)
make test-cov           # Test suite + informational coverage report (term-missing)
make command-sync-check # Verify every CLI command is registered + documented
make endpoints-check    # Verify docs/web-server-endpoints.md matches the live FastAPI app
make endpoints-gen      # Regenerate it after adding a serve route
make check-sentinel-guards # Verify no kbc-session:// sentinel path is unguarded
make version-gate-check # Verify every (since vX.Y.Z) / X.Y.Z+ marker names a released version
make vnext-check        # Verify no unresolved placeholder survives -- run in the RELEASE PR
make skill-gen          # Regenerate SKILL.md from CLI command metadata
```

Always run `make check` before pushing. The PR won't pass CI if lint or tests fail.

**SKILL.md freshness check**: CI verifies that `plugins/kbagent/skills/kbagent/SKILL.md` matches the auto-generated output from `make skill-gen`. If you added, removed, or renamed any CLI command, run `make skill-gen` and commit the result. Manual edits to the decision table will be overwritten and will cause CI to fail.

## CI workflows

Two GitHub Actions workflows guard the repo:

### `.github/workflows/ci.yml` -- per-PR gate (push + pull_request to main)

- **`check` job** (one run, Python 3.12): the static half of `make check` --
  lint, format, `ty` type-check, SKILL.md freshness, version consistency, the
  command-sync and version-gate silent-drift gates, the serve endpoint-reference
  freshness check, and the error-code enum
  check. These are
  deterministic and interpreter-independent, so they do not fan out across the
  matrix. (`changelog-check` stays local-only: it needs `gh` auth and audits
  published releases, a release-time concern -- not a per-PR gate.)
- **`test` job** (matrix: Python 3.12 + 3.13): the unit/CLI suite
  (`-m "not integration"`; `e2e` self-skips without credentials). Coverage is
  printed (`--cov ... --cov-report=term-missing`) but **informational** --
  there is no `--cov-fail-under` threshold, so coverage never blocks a merge.
- **`build-windows` job**: real `uv build` wheel checks (issue #320).

`make check` runs the same gates as the `check` + `test` CI jobs locally and is
slightly *stricter*: its `test` target uses `-m "not e2e"`, so it also runs the
`integration` tests that CI's `test` job deselects (`-m "not integration"`).
Those integration tests skip or pass without credentials -- they never fail
offline -- so a green `make check` implies CI's narrower selection passes too.
Run it before pushing.

**Unresolved-placeholder gate** (`scripts/check_version_gates.py --release-if-newer-than`):
runs only on a PR that raises `pyproject.toml`'s version -- i.e. a release PR --
and fails if any `(since vNEXT)` / `vNEXT+` placeholder survives. It compares the
branch's version against the base branch's with PEP 440 ordering rather than
trusting the diff, because a two-dot diff (the only kind a shallow CI checkout
can do) also fires for a stale feature branch whose base has since been
released; arming there would tell a contributor to delete a placeholder the
process requires them to write.

**Command-sync silent-drift gate** (`scripts/check_command_sync.py`): treats the
live Typer command tree as the single source of truth and fails if any command
is missing from `permissions.py` `OPERATION_REGISTRY`, `CLAUDE.md`
`## All CLI Commands`, `commands/context.py` `AGENT_CONTEXT`, or
`commands-reference.md`. It also flags dead `OPERATION_REGISTRY` keys (renamed /
removed commands). A registry key that intentionally has no CLI leaf command --
e.g. a `kbagent serve`-only REST operation like `auth.projects` -- must be
added to `SERVE_ONLY_OPERATIONS` in `permissions.py`, which the script
subtracts before that dead-key check; otherwise it fails CI as if the command
had been renamed or removed. This is the deterministic half of the "Plugin
synchronization map" -- the judgement half (is a behaviour change worth a new
gotcha? is the `(since vX.Y.Z)` tag right?) is left to `/kbagent:review`.

### `.github/workflows/e2e.yml` -- nightly + on-demand (NOT per-PR)

The end-to-end suite hits a real Keboola API and mutates live resources, so it
is **not** wired into the PR gate (too slow, too flaky, and it would churn a
real project on every push). Instead it runs **nightly** (cron `17 3 * * *`
UTC) and **on demand** via the Actions tab (`workflow_dispatch`).

**One-time setup (maintainer):**

1. Create a **dedicated throwaway** Keboola project -- the suite creates and
   deletes buckets, tables, workspaces, and data apps, so never point it at a
   project whose data you care about.
2. Add two repository secrets (Settings > Secrets and variables > Actions):
   - `E2E_API_TOKEN` -- a Storage API token for that project,
   - `E2E_URL` -- the stack host, e.g. `connection.<region>.keboola.com`.
3. Optionally trigger a manual run from the Actions tab to verify the wiring.

If the secrets are absent the workflow still **succeeds** (green) but emits a
warning and skips the suite -- so a fork or an unconfigured repo never sees a
spurious red E2E failure. Fork PRs never receive secrets, by design.
