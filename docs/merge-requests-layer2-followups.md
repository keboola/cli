# Merge requests — Layer 2 follow-ups inherited by Layer 1

Layer 2 shipped as PR [#703](https://github.com/keboola/cli/pull/703) (DMD-1899), approved by
Zajca on 2026-09-03 with the verdict *"nothing in this round loses data or fails silently"*. The
PR was deliberately frozen at that point. Everything below is what the review rounds left as
**non-blocking**, plus the items earlier rounds explicitly deferred — collected in one place so
Layer 1 (DMD-1900, branch `ms/dmd-1900/cli-layer-1`, RFC `merge-requests-layer1.md`) can pick
them up, since several of them are only visible from the command layer anyway.

Every item names its origin (review round), the exact code site on `7cd1855` (PR #703 head),
a recommended fix, and — where Layer 1 already moves in that direction — the current state on
the L1 branch. None of them changes a wire contract; two of them (F3, F5) change the `--json`
shape additively.

## Ownership rule

Layer 1 lands **after** Layer 2. Where Layer 1 already renamed or reshaped something (F1), the
L1 branch owns the reconciliation; where a fix is a pure Layer 2 change (F2–F8), it still goes
through the L1 PR — a second Layer 2 PR for cosmetics would be more review traffic than the
findings warrant. Operational note, now spent: the L1 branch carried `7b2bba9` (the third review round's fix
as first authored on the L1 tree) beside its pure-L2 port `7cd1855`; the L1 rebase onto the
merged L2 (2026-09-03) collapsed it -- nothing of it survived that was not already in `7cd1855`.

## F1 — Two soft-failure keys: `cleanup_warnings` (merge) vs `warnings` (resolve_conflict)

*Origin: Zajca approval, "Cross-PR".* On L2, `merge()` reports post-merge cleanup problems under
`cleanup_warnings` while `resolve_conflict()` uses `warnings`. The L1 branch (`f32d013`) already
unified `merge()` onto `warnings` and documents it as "the group's one soft-failure key", and its
`_emit_warnings` renderer reads only `warnings`. **State: done on L1** (rebase onto the merged L2 completed
2026-09-03; the two L2 tests assert `result["warnings"]`; `git grep cleanup_warnings` returns
nothing on the L1 branch).

## F2 — `_classify_three_way` docstring claims a shared criterion it does not share

*Origin: Zajca approval, #1.* `merge_request_service.py:887-905`. The docstring says the skip is
"the same 'no content to take' criterion `resolve_conflict` uses". True for `isDeleted`; the
`bool(side.get("diff"))` clause (empty envelope) is not shared — on the same shape
`resolve_conflict(take=…)` raises `VALIDATION_ERROR` naming the missing keys. The reviewer's
verdict, which this RFC adopts: **do not change behaviour** (collapsing an empty envelope to the
delete resolution would destroy a configuration; refusing it in `resolve_conflict` is the safe
direction). Fix the docstring to say the classifier is *stricter* than the resolver on purpose,
and add the missing test — `test_tombstoned_side_yields_no_classification_rows` covers the
tombstone half only; the empty-envelope half (`{"version": 4, "isDeleted": False, "diff": {}}`
→ `changes: []`, `ours_deleted: False`) has none. **State: done on L1** (docstring rewritten,
`test_empty_envelope_side_yields_no_rows_and_a_warning`; beyond the ask, an empty envelope on
either side is now reported in `warnings`, so the diff renderer says "no classification possible"
instead of "the conflict cleared").

## F3 — `merge()` records the branch-id degradation in prose only

*Origin: Zajca approval, #3.* `merge_request_service.py:693-704`. When `branchFromId` does not
coerce to int, the result is byte-identical to the legitimate published-MR null
(`branch_from_id: null`, `was_active: false`) — a `--json` consumer can only tell by
string-matching the warning. And on that path `message` says nothing about the branch while the
warning beside it talks about "the merged branch". Fix (additive): a structured key,
`cleanup_skipped: true`, plus echo the raw value (`branch_from_id_raw: "0123x"`); optionally a
neutral message sentence ("Source branch id could not be read; see warnings."). Layer 1's merge
renderer should then key on `cleanup_skipped`, not on warning text. **State: done on L1**
(`cleanup_skipped: true`, `branch_from_id_raw`, the neutral message sentence; the CLI merge
renderer keys on the flag and points at `branch reset` + `sync branch-unlink`).

## F4 — `find_default_branch_id` collapses "absent" and "not numeric" into one `None`

*Origin: Zajca approval, #2.* `services/base.py:128-136`. The same commit split those two cases
in `_branch_from_id_of` precisely because one message contradicted the state printed beside it;
the shared helper still folds them, and its callers then print "reports no default branch"
(`merge_request_service.py:624`) / "No default branch found" (`workspace_service.py:239`) for a
project that *did* report one. Strictly better than the previous `ValueError` mid-operation, just
asymmetric. Fix: `logger.warning("isDefault branch carries a non-numeric id %r -- skipped", …)`
on the skip path; optionally let callers word "default branch has an unusable id" when the
branch list was non-empty. Related, deferred since review round 1: `sync init` writes an empty
`branches` list and exits 0 when the helper returns `None` (`sync_service.py:332-339`) — decide
explicitly whether that should be an error; it predates the hoist (the old inline scan had the
identical `None` path), so it is a Layer-1-visible UX decision, not a regression. **State: the
log line is done on L1**; the `sync init` decision is still open — not taken by the L1 PR.

## F5 — `allowed_actions` is feature-blind (documented decision, revisit from L1)

*Origin: Zajca round 2, #2 (declined on L2 with reasoning).* `_enrich_row` derives actions from
state only. On a project that once had merge requests and lost the feature, rows still advertise
write actions the pre-flight will refuse. Declined on L2 because closing it costs a
`verify_token` GET on every non-empty list for a rare configuration, and the pre-flight answers
any attempted write with the precise `FEATURE_NOT_ENABLED`. Layer 1 now has the context L2 did
not: the row tier (`get_merge_request_row`) is read *before* writes, and the detail tier already
pays `verify_token`. If the L1 UX wants honest actions in the detail view, the features cache is
warm there and the fix is free for detail only. The durable fix stays DMD-1988 (server-side
`allowedActions` can honor features). **State: done on L1 for `detail`** (`feature_enabled` on the
payload; the panel says so and hint-next refuses to recommend a write that cannot succeed).
`list` stays feature-blind on non-empty results, as decided on L2.

## F6 — `cleanup_branch_id_from_mapping` matches on the numeric id alone

*Origin: Zajca round 2, nit; deferred.* `sync/branch_mapping.py`. A sync workspace of a
*different* project in the CWD with the same branch id gets unlinked. Inherited from
`BranchService.delete_branch`; `merge()` extended it to a second call site. Fix belongs to both
call sites at once (scope the match on project id, which the mapping entry would need to carry)
— a small standalone PR, not an L1 concern, listed here so it is not lost. **State: open.**

## F7 — Small ends from the approval

*Origin: Zajca approval, #4.*
- `config_service.py:172` still tests `if folder_branch_id:` by truthiness; `sync_service.py:338`
  was tightened to `is not None`. One site left out of the sweep.
- In `resolve_conflict`, the `isDisabled` type check runs before the `missing` report, so a body
  missing `name` and carrying `"false"` reports only the `isDisabled` fault. Errors are not
  accumulated. Noted, not necessarily to change — a caller fixing one fault at a time is the
  house pattern elsewhere.
- The outcome-gated "Active branch reset to main." message has only the negative test
  assertion; nothing asserts the sentence appears when the reset succeeds (the reset itself is
  covered via `active_branch_id is None`). One positive assertion closes it.
- `merge_request_service.py:130` is a 110-char docstring line against `line-length = 100`
  (`E501` is ignored and `ruff format` does not rewrap docstrings — no gate catches it).

**State: done on L1** for the truthiness sweep, the positive reset assertion and the docstring line;
the `isDisabled`-before-`missing` ordering is left as noted.

## F8 — Test fragility on main: `tests/test_changelog_render.py` under `FORCE_COLOR`

*Origin: PR #703 CI caveat; unrelated to merge requests.* Two tests assert plain substrings on
Rich output; with `FORCE_COLOR` set (Warp exports `FORCE_COLOR=3`) Rich emits ANSI inside the
asserted text (`New: ` renders bold, splitting `"New: alpha thing."`). Reproduces on a clean main
checkout. Fix on main: render through a `Console(no_color=True, force_terminal=False)` in the
test helper, or assert on `Text.plain`. Listed so the next person hitting it does not re-diagnose.

## External follow-ups (backend), for completeness

- **DMD-1984** — machine-readable string codes on every merge-request endpoint error.
- **DMD-1987** — UI "Keep production version" uses reset-to-default while the CLI resolves via
  rebase; which behaviour is intended.
- **DMD-1988** — serialize the derived MR status server-side; the comment on the issue says why
  it must come from the activity log, not `reviewers[]`.
