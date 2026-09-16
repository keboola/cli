# Zajca round 2 -- reply drafts (NOT posted; Martin approves)

## Thread 3984272224 (`_envelope_holes` vs the resolve guard)

Confirmed and fixed in e6954b4. `_envelope_holes` now applies the guard's own criterion -- a required key that is absent **or** `None` is a hole, and so is a blank `name` -- so for an ours envelope carrying `"name": null` the candidate is `null`, the reason lands in `warnings[]` ("carries no name -- ... no resolution candidate could be prefilled; author the resolution manually"), `diff --output` refuses instead of writing the file, and `classifiable()` excludes the side rather than classifying against `None`. Pinned in `test_merge_request_service.py` for both `None` and `"   "`; the existing end-to-end round-trip pin (`diff --output` -> `resolve --resolved @file` unmodified) still passes. The RFC now says the constant alone did not prevent the drift and records the shared criterion.

## Thread 3984272227 (`--external-id` exit 1 vs `--reason` exit 2)

Confirmed -- and my earlier "both surfaces map `INVALID_ARGUMENT`" was wrong for the CLI side; thanks for checking it. Took the narrow fix in e6954b4: `create` and `update` pre-check `--external-id` against `MERGE_REQUEST_EXTERNAL_ID_MAX_LENGTH` and exit 2 before any call, the same way `request-changes` does for `--reason`. The service keeps the cap as the single rule (serve still answers 400 from it). Pinned for all three flag/command pairs, asserting no service method was called.

I did not remap `INVALID_ARGUMENT` -> 2 in `map_error_to_exit_code`: that is a repo-wide behaviour change (~20 service raise sites across job/data-app/workspace/storage/repo-validate) with no test in the repo pinning either exit code for them, so it would be a silent behaviour change and belongs in its own PR if we want it. Consequence for this PR: `_require_in_conflict_set` and the absent-`branchFromId` case stay at exit 1 on the CLI (400 over serve), consistent with how every other group treats a service-raised `INVALID_ARGUMENT` today. The RFC records the choice and the alternative.
