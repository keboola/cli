# Merge requests — verified backend facts for Part 2 (Layers 1–2)

Companion to [`merge-requests-layer3-rfc.md`](merge-requests-layer3-rfc.md) (the Layer 3 HTTP
client). Everything here was verified directly against `keboola/connection` and is cited to a
file and line, recorded so Part 2 — the service (status derivation, pre-flight, cleanup) and
the commands (output, wording) — does not re-derive it. Scope is the non-SOX flow
(`branches-merge-requests`).

## State machine

States and transitions are enums (`MergeRequestLifecycle/MergeRequestLifecycleState.php`,
`…Transition.php`):

- States: `development`, `in_review`, `approved`, `in_merge`, `published`, `canceled`.
- Transitions: `request_review`, `skip_review`, `approve`, `finish_review`, `merge`,
  `rollback_merge`, `request_changes`, `publish`, `cancel`.

`skip_review`, `finish_review`, `rollback_merge` and `publish` have **no endpoint** — they are
driven internally.

## Merge behavior

`MergeProcessor::process` (`Storage/MergeRequests/Merge/MergeProcessor.php:45-80`) does more
than enqueue:

1. **If the MR is in `development` and already has enough approvals, it calls `skipReview`
   itself.** On a non-SOX project with the default of 0 required approvals this means `merge`
   works **directly from `development`** — no explicit `request-review` needed — and
   `skipReview` populates the change log on the way through
   (`MergeRequestService.php:130-137`). This materially shortens the CLI's happy path.
2. Acquires a **project-wide lock**; a held lock raises `BranchIsNotReadyToMerge`.
3. Checks the state machine can apply `merge`; otherwise `BranchIsNotReadyToMerge` with
   `Cannot merge, branch is in "<state>" state.`
4. Rejects if another MR in the project is already processing (`isOtherMrInProjectProcessing`).
5. Validates conflicts, then `setInMerge` and enqueues the job.

**409 therefore has four distinct causes, in two different response shapes**
(`MergeAction.php:97-109`): the three `BranchIsNotReadyToMerge` cases carry the machine-readable
`storage.mergeRequests.notReadyToMerge`, while a **conflict** raises `MergeValidationException`
and is thrown *without* that string code. Part 2 can tell "not ready" from "conflicted" on that
basis alone.

The merge itself is atomic: the job applies the configuration changes and transitions to
`published` in one transaction, rolling back to `approved` on failure (`MergeRequestService.php`
`publish:194` / `rollbackMerge:186`, both wrapped in `transactionManager->transactional`).
There is no publish endpoint.

## Conflicts are computed live

`DefaultConflictValidator::validateMergeRequest`
(`Storage/MergeRequests/Merge/DefaultConflictValidator.php:70-98`) compares each dev-branch
config's **version(1)** `versionIdentifier` against the default branch's current one. Not a
conflict when: the config exists only in the default branch; both sides are deleted; or the
identifiers match. Otherwise
`MergeValidationExceptionError::createConfigurationInDefaultBranchChanged(componentId,
configurationId, isDeleted, devVersionIdentifier, defaultVersionIdentifier)` — which is exactly
the shape `GET …/conflicts` returns.

Two consequences: a conflict requires the configuration to exist on **both** sides, so the
`theirs` side of a conflicting config's diff is always populated; and because the check runs on
every merge attempt, rebasing every conflicting config is sufficient to make the MR mergeable —
there is no MR-level "re-validate" step.

## A successful merge deletes the source branch

After the merge transaction commits, `MergeDevBranchJob` enqueues a `DevBranchDelete` job for
`branchFromId` (`Storage/Jobs/MergeDevBranchJob.php:179-187`). This is the happy path, every
time — there is no keep-the-branch option. Three consequences:

- **Only the merged configurations survive**, applied to the default branch. Everything else
  scoped to the dev branch — its buckets, tables, files, workspaces — is dropped with it.
- **It is a second, separate async job.** `merge_requests.merge()` awaits the *merge* job;
  when that returns `success` the MR is `published`, but the branch deletion has only just been
  enqueued. Callers must not assume the branch is already gone — nor that it still exists.
- **Every local reference to the branch goes stale**: `active_branch_id` in `config.json`, a
  sync `branch-mapping.json` entry, a workspace created on the branch. Cleaning these up is
  Part 2's job, and the precedent already exists — today's `branch merge` (the UI-URL escape
  hatch) resets the active branch and calls `cleanup_branch_id_from_mapping`
  (`services/branch_service.py:348-355`) for exactly this reason. The real merge command must
  do at least as much, and its output must say the branch was deleted.

There is **no cancel endpoint**: an MR is canceled only as a side effect of deleting its source
branch (`legacy-app/src/Storage/Job/DevBranch/DevBranchDelete.php:201` →
`mergeRequestService->cancel`). Deletion is thus how every MR lifecycle ends — published or
canceled, the branch ceases to exist. Related create-time nuance: the existence check
(`MergeRequestsModel::fetchForBranchFrom`) filters by `branchFromId` **only — no state filter**
— so a branch has at most one MR *ever*, not merely one *open* MR. In practice the readings
coincide because both terminal states end with branch deletion, but the code's rule is the
stronger one.

## Approvals — and the status object they complicate

Part 2 wants a derived status ("mergeable / blocked because conflicts / blocked because
approvals") so callers branch on data rather than parsed prose. Its inputs are the six states
and the live conflicts list — **not** the required-approvals count. The behavioral facts:

- **The state machine collapses the approvals requirement into the state.** An MR sits in
  `in_review` only while approvals are insufficient; the moment the requirement is met the
  backend auto-transitions to `approved` (internal `finish_review`). With the non-SOX default
  of **0** required approvals, `request-review` lands straight in `approved` and the approve
  step never runs. The `state` field is therefore the authoritative answer to "are approvals
  satisfied?" — no count needed.
- **Approvals are deleted on `request_changes` and on `cancel`**
  (`MergeRequestService.php:139-152`, `:163-176`, both
  `approvalRepository->deleteAllForMergeRequest`) and nowhere else — so a rebase does not cost
  you an approval.
- **The required count itself is unreadable with a Storage token** — a documented trap. It is
  **project** metadata (`KBC.branches-merge-requests.required-approvals-count`), exposed only
  on the Manage API; branch metadata is a different store entirely, so
  `get_branch_metadata_value(key, branch_id="default")` would not fail, it would quietly
  report the key as absent. Nor is the count in any MR response: `MergeRequestResponse`
  carries `approvals` (`{approverId, approverName, createdAt}`) and `reviewers`
  (`{id, name, email, status}` with `status` ∈ `approved`/`rejected`/null), but no
  required-count field.
- The count's mechanics, so nobody re-derives them: `RequiredApprovalsCountProvider` computes
  `hasEnoughApprovals = given >= required`, defaults 0 (non-SOX) / 2 (SOX), reading project
  metadata (provider `user`) via `Controller/Manage/Projects/ProjectListMetadataAction.php:24`.
  The Keboola UI *can* show "1 of 2 approvals" because it runs as an admin session and reads
  that Manage endpoint as a side channel. kbagent deliberately does not chase that parity —
  its manage-token policy is default-deny with an interactive prompt (convention #12), and
  requiring a manage token to render a status line would invert it for cosmetics.

So Part 2 derives what it needs from behaviour: the state collapses the requirement
(`in_review` = not enough, `approved` = enough), `approvals[]` gives *who* approved,
`reviewers[].status` gives *who is still pending*, and the backend stays the authority via the
merge 409. Quantitative wording ("1 of 2") is out until the count is readable — **Connection is
expected to add it to the Storage API** (Linear issue to follow); the recommended shape is a
field serialized into `MergeRequestResponse` (the provider already exists server-side), because
Layer 3 returns raw dicts and a new response field flows through with zero client change.
Part 2 should read it defensively (`.get(...)`, falling back to the state-derived logic on
stacks that predate it) — the same pattern as for `changeLog` below.

## Roles (non-SOX)

Every write carries `#[MergeRequestsAllowedRoles(roles: [ProjectRole::ROLE_ADMIN,
ProjectRole::ROLE_SHARE])]` — verified on all six: create (`:39`), update (`:44`),
request-review (`:43`), approve (`:43`), reject (`:49`), merge (`:40`). The `reviewer`,
`developer` and `production_manager` roles appear only in the sibling
`#[ProtectedBranchAllowedRoles]` attribute, which `StorageRouteGuard` selects for the **SOX**
feature — so those roles carry no MR privileges in a non-SOX project. Reads (list, detail,
conflicts) are `#[AsReadOnlyAction]` with no role whitelist.

The dev branch is locked for editing only while the MR is `in_merge`
(`Core/Storage/RouteGuard/StorageRouteGuard.php:108`, `:125` — `$isBranchLocked =
$mr->isInMerge()`), so editing and rebasing are allowed in `development`, `in_review` and
`approved`.

A failed feature check makes the route guard return false, which `RouteGuardListener` turns
into `AccessDeniedException` (`RouteGuardListener.php:75`) — **HTTP 403, byte-for-byte
indistinguishable from a role denial**. Only the service's client-side pre-flight
(`has_feature("branches-merge-requests")`, RFC D9) can produce the right "not enabled" message
— which also fences off SOX projects, since server-side either feature passes the write gate.

## The change log

`Model_Row_MergeRequest::updateChangeLog` (`legacy-app/src/Model/Row/MergeRequest.php:324-331`)
writes `$changeLog['configurations'] = $changes`, and is called from `requestReview`
(`MergeRequestService.php:120`) and `skipReview` (`:134`) — **not** at merge. So the change
list is legitimately empty while the MR sits in `development`, and appears the moment it is
sent for review (or skipped past review by a merge from `development`, per *Merge behavior*).
Read it defensively.

## UX facts for the commands

- **Requesting review can email the whole project.** The `ReviewRequested` notification falls
  back to *all project members* when the MR has no selected reviewers and the project has no
  designated reviewers (`MergeRequestNotificationRecipientResolver.php:86-95`,
  `reviewRequestedPool`). The submit command's docs should say so; on a non-SOX project with 0
  required approvals, merging straight from `development` avoids the blast entirely.
- **Branch names are not resolvable for finished MRs.** `branches.branchFromId` is nullable in
  `MergeRequestResponse` — the branch is deleted for both `published` and `canceled` MRs — so
  any "IDs → names" rendering must tolerate a missing branch.
- **Reviewer ids are obtainable in kbagent** via `project member-list`, so `--reviewer-id` is
  usable here (unlike in a chat context with no user-listing surface).
- **`request_changes` is not "reject".** The transition sends the MR back to `development` to
  be revised and resubmitted; the terminal negative outcome is branch deletion. The command
  name should carry the same care as the client method name does.
- **Rebase's `version` needs an explicit flag name.** It is the *default-branch* version being
  re-anchored onto (from the diff's `theirs.version`) — expose something like `--onto-version`,
  never a bare `--version`.
- **A resolve-shortcut is worth considering**: a `--take ours|theirs` mode that composes the
  rebase payload from the diff's server-side data instead of requiring the caller to author
  the full resolved configuration. Layer 3 needs nothing for it — it is a convenience over
  `get_config_diff` + `rebase_config`.
- `reason` on request-changes is capped at 1000 characters; `reviewerIds` duplicates are
  de-duplicated server-side; `AutoMergeStrategy` is `immediately` | `scheduled` | `none`.

## Error codes

Note what the existing mapping actually does (`http_base.py:306-336`): 401 / 403 / 404 get
bespoke codes (`INVALID_TOKEN`, `ACCESS_DENIED`, `NOT_FOUND`), while **409 and 422 fall through
to the generic `API_ERROR` catch-all**, indistinguishable from any other unclassified status
(neither is in `RETRYABLE_STATUS_CODES`, `constants.py:53`). Tolerable for Part 1, which has no
user-facing surface — but Part 2 almost certainly wants dedicated codes for the merge 409, and
given it has *two* shapes (`storage.mergeRequests.notReadyToMerge` versus a bare conflict
validation error), quite possibly two. Any new member must also be documented in
`docs/error-codes.md`, which `scripts/check_error_codes.py` enforces in CI.

## `mcp_parity.py` and the canary

A parallel effort adds merge-request tools to `keboola-mcp-server`, and `mcp_parity.py`'s
docstring is explicit that "an unmapped tool is a parity BUG by definition". The map currently
holds 39 entries and none mention merge requests. The nightly `mcp-parity-canary`
(`make parity-check`, *not* part of `make check`) diffs the live server catalogue against that
map, so if the server ships its tools before kbagent ships Part 2's commands, the canary goes
red through no fault of any kbagent change and stays red for the length of that window. Part 2
— or an interim commit, if the server lands first — must add the entries. A red canary in the
interim is expected sequencing, not a kbagent regression.

## Open Part 2 questions

- Whether `merge-request create` takes the source branch from `--branch` or from
  `active_branch_id` (Layer 3 only ever receives it explicitly).
- The fate of `kbagent branch merge` (the UI-URL escape hatch). It becomes redundant once the
  real commands exist; deprecate-with-pointer is the likely answer.
- Post-merge cleanup mechanics: reset `active_branch_id`, clean the sync branch mapping
  (reusing `cleanup_branch_id_from_mapping`), and say in the output that the branch was
  deleted (per *A successful merge deletes the source branch*).
- SOX flow, branch creation/deletion changes, auto-merge scheduling UX beyond passing the
  fields through.
