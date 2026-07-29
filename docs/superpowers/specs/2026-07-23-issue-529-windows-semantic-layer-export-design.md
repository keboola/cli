# Design: Make semantic-layer snapshot export portable to Windows

**Issue:** [#529](https://github.com/keboola/cli/issues/529)
**Status:** Proposed
**Target:** `kbagent semantic-layer export` and every shared snapshot writer

## Problem

`write_snapshot_to_file()` builds its `os.open()` flags with
`os.O_NOFOLLOW`. That flag is available on POSIX platforms but is not defined
by Python's `os` module on Windows. Evaluating the flags therefore raises
`AttributeError` before the output file is opened.

The helper is shared by semantic-layer export and the optional build output
path, so the portability bug affects more than one command even though export
is the reported reproducer.

## Goals

- Make snapshot writes work on Windows without weakening the existing POSIX
  symlink protection.
- Continue writing UTF-8 JSON bytes with deterministic line endings.
- Preserve the current `0o644` permissions contract on POSIX.
- Cover both the Windows-compatible path and the POSIX hardening path with
  regression tests.

## Non-goals

- Add Windows ACL management; the numeric mode argument is intentionally
  ignored by Windows.
- Guarantee protection against every Windows reparse-point race. Python does
  not expose a direct `O_NOFOLLOW` equivalent through `os.open`.
- Change the snapshot schema, export result, default filename, or CLI output.

## Design

Build the flags incrementally from portable flags:

```python
flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
flags |= getattr(os, "O_NOFOLLOW", 0)
flags |= getattr(os, "O_BINARY", 0)
```

- On POSIX, `O_NOFOLLOW` remains active and a pre-existing final-component
  symlink is rejected.
- On Windows, the unavailable flag contributes zero, so `os.open()` succeeds.
- `O_BINARY` is a no-op where unavailable and prevents Windows text-mode
  newline translation because the helper writes an already encoded byte
  payload with `os.write()`.

Keep the descriptor lifecycle in the existing `try/finally`; do not replace it
with a text-mode `Path.write_text()` call, which would discard the POSIX
hardening and change the low-level write contract.

Update the docstring to describe conditional `O_NOFOLLOW` protection rather
than implying that every platform provides it.

## Implementation map

- `src/keboola_agent_cli/services/_semantic_layer_internals.py`
  - construct portable open flags with guarded platform-specific additions;
  - document POSIX and Windows behavior.
- `tests/test_semantic_layer_service.py`
  - simulate Windows by temporarily removing `os.O_NOFOLLOW` and verify export
    creates valid UTF-8 JSON;
  - where `O_NOFOLLOW` exists, verify a symlink output path is rejected and its
    target remains unchanged;
  - retain the existing content and POSIX permission checks.
- `.github/workflows/ci.yml`
  - run the focused semantic-layer export regression on the existing
    `windows-latest` job so the real Windows `os` flags are exercised.

No changelog entry is made in this PR: the repository has no unreleased
section, so the Windows export fix is recorded when the next release version is
prepared rather than being added to an already released entry.

## Acceptance criteria

1. Importing/evaluating `write_snapshot_to_file()` on Windows never accesses
   `os.O_NOFOLLOW` directly.
2. `kbagent semantic-layer export --output <path>` creates parseable UTF-8 JSON
   on Windows.
3. The shared build-output writer follows the same portable path.
4. POSIX exports still create mode `0o644` files.
5. On platforms with `O_NOFOLLOW`, a pre-existing symlink at the output path is
   rejected and the symlink target is not modified.
6. The regression is tested both by a platform-independent missing-flag unit
   test and on the existing Windows CI runner.
7. Focused tests plus lint, format, and type checks pass.

## Validation

Run locally:

```text
uv run pytest tests/test_semantic_layer_service.py -k "export" -v
uv run ruff check src/ tests/
uv run ruff format . --check
uv run ty check
```

On `windows-latest`, run the focused export regression against Python 3.12 and
the built project. Confirm the output contains non-ASCII data without newline
or encoding corruption.

## Risks

- Windows lacks the exact final-component symlink behavior provided by
  `O_NOFOLLOW`. This PR preserves the strongest protection Python exposes
  through the current low-level API on each platform and documents the gap.
- CI workflow time increases slightly from one focused pytest invocation; the
  existing Windows dependency/build setup is reused.
