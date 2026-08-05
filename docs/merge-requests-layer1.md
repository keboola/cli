# Merge requests — Layer 1 (commands), working notes

Linear: [DMD-1900](https://linear.app/keboola/issue/DMD-1900). UX facts for the future
`merge-request` command group, all verified against the backend
(citations in [`merge-requests-notes.md`](merge-requests-notes.md)). Service-layer material is
in [`merge-requests-layer2.md`](merge-requests-layer2.md).

## Naming

- **`request-changes`, not `reject`.** The transition sends the MR back to `development` to be
  revised and resubmitted; the terminal negative outcome is branch deletion. Layer 3's method
  name already carries this care — the command name must too.
- **Rebase's `version` needs an explicit flag name.** It is the *default-branch* version being
  re-anchored onto (from the diff's `theirs.version`) — expose something like
  `--onto-version`, never a bare `--version`.
- The `update` command pairs with Layer 3's `merge_requests.update` (shipped per decision
  2026-08-18, so the method is not dead code) — auto-merge strategy, `external_id` (255 chars,
  correlates an MR with a ticket).

## Output and wording

- **Merge output must say the source branch was deleted** (it always is — no keep-the-branch
  option), without asserting it is already gone (second async job; see notes doc).
- **Branch names are not resolvable for finished MRs.** `branches.branchFromId` is nullable —
  the branch is deleted for both `published` and `canceled` MRs — so any "IDs → names"
  rendering must tolerate a missing branch.
- **Requesting review can email the whole project.** The `ReviewRequested` notification falls
  back to *all project members* when the MR has no selected reviewers and the project has no
  designated reviewers (`MergeRequestNotificationRecipientResolver.php:86-95`,
  `reviewRequestedPool`). The submit command's docs should say so; on a non-SOX project with 0
  required approvals, merging straight from `development` avoids the blast entirely.
- The happy path on non-SOX defaults is short: `merge` works **directly from `development`**
  (the backend auto-skips review when approvals suffice) — the command flow should not force
  an unnecessary `request-review` step.

## Flags and conveniences

- **Reviewer ids are obtainable in kbagent** via `project member-list`, so `--reviewer-id` is
  usable here (unlike in a chat context with no user-listing surface). Duplicates are
  de-duplicated server-side.
- **A resolve shortcut is worth considering**: `--take ours|theirs` composing the rebase
  payload from the diff's server-side data instead of requiring the caller to author the full
  resolved configuration. Layer 3 needs nothing for it — it is a convenience over
  `get_config_diff` + `rebase_config`.
- `reason` on request-changes: cap 1000 characters. `AutoMergeStrategy`:
  `immediately` | `scheduled` | `none`.
- `--state` filtering on list is client-side (no query params on the endpoint).
- **`--wait` on merge has no Layer 3 fire-and-forget to map to**: `merge()` always awaits the
  Storage job (`MERGE_JOB_MAX_WAIT` 600 s). A `--timeout` can map to `max_wait`; a `--no-wait`
  mode would need its own polling via `merge_requests.get()` — decide deliberately, don't copy
  the `job run --wait` idiom blindly.

## Bookkeeping when the commands land

- E2E test per command (convention #16), needs a `branches-merge-requests` project.
- All convention-#17 silent-drift surfaces: `commands/context.py`, CLAUDE.md command list,
  `keboola-expert.md`, `SKILL.md`, `commands-reference.md`, `gotchas.md`
  (`(since vX.Y.Z)` tags), a workflow reference doc.
- `mcp_parity.py` entries (see layer2 doc, canary sequencing).
- Dedicated error codes for the merge 409 must be documented in `docs/error-codes.md`.
- Decide the fate of `kbagent branch merge` in the same release (deprecate-with-pointer).
