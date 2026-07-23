# Design: Make kbagent self-update safe on Windows

**Issue:** [#528](https://github.com/keboola/cli/issues/528)  
**Status:** Proposed  
**Target:** `kbagent update` and startup auto-update

## Problem

Both self-update entry points currently mutate the `uv tool` environment from
which the running `kbagent` process was launched:

- startup auto-update calls `_perform_update()`, then continues in the old
  process long enough to write the cache and re-exec;
- explicit `kbagent update` updates kbagent first and then performs MCP
  detection, a PyPI request, and an MCP update from the already-mutated
  process.

On Windows, a running executable or dependency can be locked. Even without a
lock, a failed in-place dependency swap can leave the tool environment with a
mixture of old and new distributions. The reporter observed both failure
classes:

1. mismatched `rich` and `typer` after a failed startup update;
2. a missing `certifi/cacert.pem` after explicit update removed the old
   distribution and the same process subsequently attempted the MCP HTTPS
   check.

The updater must treat mutation of its own environment as a terminal action,
and it must use `uv tool install` as a full reinstall/re-resolution rather than
an incremental upgrade.

## Goals

- Never perform network access, dependency discovery, cache writes, or MCP
  work after the kbagent environment starts changing.
- Reinstall the complete kbagent tool environment from an exact release
  artifact when `uv` manages the install.
- Preserve `[server]` extras and the beta channel.
- Keep kbagent and MCP results independently visible in human and JSON output.
- Fail without claiming success, and provide a copy/paste recovery command.
- Keep startup auto-update best-effort: update failure must not intentionally
  abort the requested command.

## Non-goals

- Reimplement `uv`'s package resolver or tool-environment transaction logic.
- Update a tool while another long-running kbagent process is using the same
  Windows environment. A locked executable remains a reported, recoverable
  failure.
- Change MCP installation ownership or merge the MCP environment into
  kbagent's environment.
- Add a persistent background update daemon.

## Design

### 1. Separate planning from mutation

Introduce small immutable plan/result dataclasses in
`services/version_service.py`. The planning phase performs every operation that
can touch the network or inspect the current environment:

- fetch the latest kbagent version;
- resolve the exact release wheel URL, or an exact `@v<version>` git fallback;
- detect `[server]` extras and build the complete install command;
- detect the MCP install method and local version;
- fetch the latest MCP version;
- calculate which stages actually need work.

The apply phase receives only these prepared values. It must not call
`httpx`, `importlib`, package metadata probes, or command builders again.

### 2. Apply MCP before kbagent

For explicit `kbagent update`, execute the independent MCP stage first.
Re-probing the MCP version is allowed immediately after that stage because the
kbagent environment is still untouched.

The kbagent reinstall is always the final mutation. Its result is assembled
from the prepared plan and subprocess return value; no post-install version
probe or network request is made.

This removes the exact ordering bug behind the missing certifi CA bundle.

### 3. Recreate the uv tool environment

For every uv-managed kbagent path, build:

```text
uv tool install --force --reinstall "<keboola-cli spec pinned to the target release>"
```

The preferred spec is the versioned release wheel. The fallback source must
also be pinned to `@v<target_version>` for stable and beta releases; updating
from mutable `main` is not sufficiently reproducible for a recovery operation.
When server extras are present, encode them in the primary PEP 508 requirement
(`keboola-cli[server] @ ...`) so one resolution recreates the complete
environment.

`uv tool install` is the supported operation for recreating a tool environment;
`--reinstall` ensures every package is materialized again and `--force` allows
uv-owned entry points to be replaced. Pip remains a best-effort fallback, but
its result and recovery limitation must be explicit.

### 4. Make startup mutation terminal

Reorder startup auto-update:

1. perform all fresh kbagent and MCP lookups;
2. update MCP, if needed;
3. persist the complete cache;
4. reinstall kbagent;
5. on success, immediately re-exec with `KBAGENT_SKIP_UPDATE=1`.

There must be no cache write or MCP stage between steps 4 and 5. The
already-prepared cache lets the re-exec'd process skip all network work for
that cycle.

If the kbagent install subprocess fails or times out, emit a concise failure
and the exact forced-reinstall recovery command. Do not report a successful
update and do not run further update work from the potentially inconsistent
process.

### 5. Preserve the command contract

`kbagent update` keeps the current top-level result shape. Add enough fields to
make partial outcomes unambiguous:

- each stage reports `planned`, `updated`, `up_to_date`, and `message`;
- kbagent failure includes `recovery_command`;
- the top-level `updated` remains true when at least one stage changed;
- the summary reports an MCP success even if the final kbagent stage fails.

The existing `--beta` behavior, `[server]` preservation, timeouts, and
human/JSON rendering remain supported.

## Implementation map

- `src/keboola_agent_cli/services/version_service.py`
  - add update-plan dataclasses and prepare/apply helpers;
  - make exact-version `uv tool install --force --reinstall` the self-update
    command;
  - reorder explicit update to MCP first, kbagent last;
  - avoid post-kbagent probes.
- `src/keboola_agent_cli/auto_update.py`
  - prepare both stages before mutation;
  - move MCP update and cache persistence before `_perform_update`;
  - re-exec immediately after a successful kbagent install;
  - expose the forced-reinstall command on failure.
- `src/keboola_agent_cli/commands/version.py`
  - update command documentation and render partial/failure outcomes without
    accessing package metadata after the self-update stage.
- `src/keboola_agent_cli/changelog.py`
  - add the user-facing fix and recovery behavior to the next release entry.
- `tests/test_version_service.py`
  - pin planning/apply order and assert no network/probe calls after kbagent
    mutation;
  - cover MCP success plus kbagent failure;
  - cover stable, beta, server-extra, wheel, and git-fallback command shapes.
- `tests/test_auto_update.py`
  - assert MCP and cache work precede kbagent mutation;
  - assert successful install is followed directly by re-exec;
  - assert failure performs no later update work and prints recovery guidance.

## Acceptance criteria

1. Explicit update fetches both latest-version values before running either
   updater.
2. Explicit update applies MCP first and kbagent last.
3. No HTTP call, import/package probe, MCP update, or cache write occurs after
   the kbagent install command begins.
4. All uv kbagent installs use an exact target plus `tool install --force
   --reinstall`.
5. `[server]` and beta installs retain their current behavior.
6. Startup auto-update writes the prepared cache before self-mutation and
   immediately re-execs after success.
7. A failed kbagent stage is never described as up to date or successful and
   includes an exact recovery command.
8. Unit tests reproduce the incident-2 ordering failure by making any
   post-mutation HTTP call fail; the fixed flow performs no such call.
9. The focused update suites and the repository's lint, format, and type checks
   pass.

## Validation

Run locally:

```text
uv run pytest tests/test_version_service.py tests/test_auto_update.py -v
uv run ruff check src/ tests/
uv run ruff format . --check
uv run ty check
```

Before merge, exercise a release wheel on Windows 11:

1. install the previous release with `[server]` via `uv tool install`;
2. run explicit update and verify both reported versions;
3. repeat through startup auto-update;
4. interrupt or force a failing install and verify the recovery command restores
   a working `kbagent --version`;
5. keep `kbagent serve` open and verify the lock failure is clear and does not
   claim success.

## Risks and follow-up

- Atomic replacement is ultimately owned by `uv`; this change reduces our
  mutation to a full, exact reinstall and eliminates all post-mutation work.
- A future enhancement can use a detached bootstrapper if Windows lock failures
  remain reproducible with current uv. That is intentionally separate because
  process detachment/relaunch semantics need dedicated Windows integration
  coverage.
- The pip fallback cannot provide the same tool-environment recreation
  guarantee. Its failure message must recommend reinstalling with uv.
