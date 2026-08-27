# Merge requests — verified backend facts (all layers)

Everything here was verified directly against `keboola/connection` and is cited to a file and
line. Scope is the **non-SOX** flow (`branches-merge-requests`); SOX
(`protected-default-branch`) is out of scope. Layer-specific material lives in the siblings:
[`merge-requests-layer3.md`](merge-requests-layer3.md) (the shipped HTTP client),
[`merge-requests-layer2.md`](merge-requests-layer2.md) (service, DMD-1899),
[`merge-requests-layer1.md`](merge-requests-layer1.md) (commands UX).

## State machine

States and transitions are enums (`MergeRequestLifecycle/MergeRequestLifecycleState.php`,
`…Transition.php`):

- States: `development`, `in_review`, `approved`, `in_merge`, `published`, `canceled`.
- Transitions: `request_review`, `skip_review`, `approve`, `finish_review`, `merge`,
  `rollback_merge`, `request_changes`, `publish`, `cancel`.

`skip_review`, `finish_review`, `rollback_merge` and `publish` have **no endpoint** — they are
driven internally. The lifecycle is a Symfony Workflow `state_machine`, which matters for
retries (see *PUT transitions cannot double-apply* below).

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
`storage.mergeRequests.notReadyToMerge`, while a **conflict** raises `MergeValidationException`,
whose own string code is **`storage.mergeRequests.validation`** (`getStringCode`,
`MergeValidationException.php:174-177`) -- serialized top-level as `code` by
`ExceptionConverter` (`legacy-app/.../ExceptionConverter.php:99-125`), alongside the human
message in `error` and **the conflicting configurations in `params.errors`** (the
HttpException context). "Not ready" vs "conflicted" is a code-vs-code match, not
code-vs-absence -- an earlier reading of `MergeAction` missed the converter and recorded the
conflict 409 as code-less (corrected by the Opus wire review, 2026-08-27).

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
time — there is no keep-the-branch option. Consequences:

- **Only the merged configurations survive**, applied to the default branch. Everything else
  scoped to the dev branch — its buckets, tables, files, workspaces — is dropped with it.
- **It is a second, separate async job with no job handle returned.** `merge_requests.merge()`
  awaits the *merge* job; when that returns `success` the MR is `published`, but the branch
  deletion has only just been enqueued. Callers must not assume the branch is already gone —
  nor that it still exists.
- **Every local reference to the branch goes stale** — cleaning that up is Layer 2's job (see
  the layer2 doc, *Post-merge cleanup*).

There is **no cancel endpoint**: an MR is canceled only as a side effect of deleting its source
branch (`legacy-app/src/Storage/Job/DevBranch/DevBranchDelete.php:201` →
`mergeRequestService->cancel`). Deletion is thus how every MR lifecycle ends — published or
canceled, the branch ceases to exist. Related create-time nuance: the existence check
(`MergeRequestsModel::fetchForBranchFrom`) filters by `branchFromId` **only — no state filter**
— so a branch has at most one MR *ever*, not merely one *open* MR. In practice the readings
coincide because both terminal states end with branch deletion, but the code's rule is the
stronger one.

## Approvals

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
- The count's mechanics: `RequiredApprovalsCountProvider` computes
  `hasEnoughApprovals = given >= required`, defaults 0 (non-SOX) / 2 (SOX), reading project
  metadata (provider `user`) via `Controller/Manage/Projects/ProjectListMetadataAction.php:24`.
  The Keboola UI *can* show "1 of 2 approvals" because it runs as an admin session and reads
  that Manage endpoint as a side channel. kbagent deliberately does not chase that parity —
  its manage-token policy is default-deny (convention #12), and requiring a manage token to
  render a status line would invert it for cosmetics. **Connection is expected to add the
  count to the Storage API** ([DMD-1969](https://linear.app/keboola/issue/DMD-1969)) — the
  recommended shape is a field
  serialized into `MergeRequestResponse` (the provider already exists server-side), which
  flows through Layer 3's raw dicts with zero client change. Read it defensively.
- Server-side quirk (filed as keboola/connection#8209, surfaced by the #616 audit):
  `bi_rMergeRequestsApprovals` has no unique constraint on `(mergeRequestId, idAdmin)` and
  `hasEnoughApprovals()` counts rows rather than distinct admins.

## Auto-merge: `immediately` merges WITHOUT anyone calling merge

`autoMergeStrategy` is not metadata. A background tick selects every MR in `approved` whose
strategy is `immediately` (or `scheduled` with `autoMergeAt <= now`) --
`AutoMerge/AutoMergeCandidateRepository.php:38-47` (`findCandidates`: `WHERE mr.state =
:approved AND (mr.autoMergeStrategy = :immediately OR (:scheduled AND autoMergeAt <= :now))`)
-- and drives it through the **same `MergeProcessor`** the merge endpoint uses, under a
system token (`AutoMerge/AutoMergeTickHandler.php:86`:
`$this->mergeProcessor->process($legacyRow, new SystemToken(...))`). A conflict blocks the
scheduled merge and the tick retries until it clears (`:88-94`).

Consequence: on a non-SOX project with the default 0 required approvals,
`create(auto_merge_strategy="immediately")` + `request_review()` ends in a production merge
and the source branch's deletion, with `merge()` never called; an
`update(auto_merge_strategy="immediately")` on an already-approved MR is enough on its own.
Both service docstrings say so; Layer 1 escalates the flag's permission class accordingly.

## Roles and feature gating (non-SOX)

Every write carries `#[MergeRequestsAllowedRoles(roles: [ProjectRole::ROLE_ADMIN,
ProjectRole::ROLE_SHARE])]` — verified on all six: create (`:39`), update (`:44`),
request-review (`:43`), approve (`:43`), reject (`:49`), merge (`:40`). The `reviewer`,
`developer` and `production_manager` roles appear only in the sibling
`#[ProtectedBranchAllowedRoles]` attribute, which `StorageRouteGuard` selects for the **SOX**
feature — so those roles carry no MR privileges in a non-SOX project. Reads (list, detail,
conflicts) are `#[AsReadOnlyAction]` with no role whitelist.

Role whitelisting is not the only access axis, though: every `/merge-request/{id}` route --
the read-only detail and conflicts actions included -- runs `MergeRequestVoter`, which denies
a token with **no admin identity** (`Voters/MergeRequestVoter.php:49-56`, via
`MergeRequestService::requireMergeRequest`). A scoped Storage token therefore gets 403 on
detail/conflicts while the un-votered `GET /merge-request` list still works. Different axis
(admin identity vs. role), not a contradiction of the sentence above.

All six MR writes accept **either** `protected-default-branch` **or**
`branches-merge-requests`; the reads and `/diff` are ungated; `/rebase` alone requires
`branches-merge-requests` (`StorageRouteGuard::canAccessStorageScope`,
`Core/Storage/RouteGuard/StorageRouteGuard.php:158-180`). A failed feature check makes the
route guard return false, which `RouteGuardListener` turns into `AccessDeniedException`
(`RouteGuardListener.php:75`) — **HTTP 403, byte-for-byte indistinguishable from a role
denial**. Only a client-side pre-flight can produce the right "not enabled" message — hence
Layer 2's `has_feature` check (layer2 doc).

The dev branch is locked for editing only while the MR is `in_merge`
(`Core/Storage/RouteGuard/StorageRouteGuard.php:108`, `:125` — `$isBranchLocked =
$mr->isInMerge()`), so editing and rebasing are allowed in `development`, `in_review` and
`approved`.

## The change log

`Model_Row_MergeRequest::updateChangeLog` (`legacy-app/src/Model/Row/MergeRequest.php:324-331`)
writes `$changeLog['configurations'] = $changes`, and is called from `requestReview`
(`MergeRequestService.php:120`) and `skipReview` (`:134`) — **not** at merge. Shape:
`{configurations: [{componentId, configurationId, lastVersionIdentifier, isDeleted}]}`. So the change
list is legitimately empty while the MR sits in `development`, and appears the moment it is
sent for review (or skipped past review by a merge from `development`, per *Merge behavior*).
Read it defensively.

## PUT transitions cannot double-apply

kbagent's retry policy (`RETRY_SAFE_METHODS`, since #616) treats PUT as retry-safe, and the MR
client uses PUT for four action-style transitions (`/request-review`, `/approve`,
`/request-changes`, `/merge`). Verified against Connection (recorded in #617): a retried
transition **cannot fire twice**. Three of the four are refused structurally on a second call
(a Symfony state machine enables a transition only from its declared `from` place); `/approve`
is the one self-loop and carries `AddApprovalGuard` instead. Notifications ride
`workflow.merge_request_lifecycle.completed` from inside `apply()`, inside
`MergeRequestService`'s `transactional()` — no transition, no notification.

Caveat that survives: a retried PUT that succeeded but lost its response reports **attempt 2's
error** (a 422/409 on an operation that actually applied). That applies to every retried
PUT/DELETE in kbagent, not just merge requests.

## Misc limits

- `reason` on request-changes is capped at 1000 characters
  (`MergeRequestRejectRequest::REASON_MAX_LENGTH`); the body is `required: false`.
- `reviewerIds` duplicates are de-duplicated server-side (`array_unique` in
  `mapValidatedData`).
- `AutoMergeStrategy` is exactly `immediately` | `scheduled` | `none`.
- `externalId` max 255 (`Assert\Length(max: 255)`, create and update DTOs).
- update semantics: null = leave unchanged, absent ≡ null, no clear-to-null — but an **empty
  string** clears `description`/`externalId` (`?? null` mapping + `!== null` guards in
  `MergeRequestService::updateMergeRequest`). `PUT {}` is a no-op returning the MR.

## Wire-truth verification table

Re-verified against Martin's local `keboola/connection` checkout on 2026-08-19, during the
final Layer 3 review:

| Claim | Backend evidence |
|---|---|
| MR endpoints project-level, never branch-prefixed | `isAvailableInBranch: false` on every `Controller/Storage/MergeRequest/*Action.php` route |
| `branchFromId`/`branchIntoId` must be JSON ints | `Assert\Type('int')` in `MergeRequestCreateRequest::getConstraint()` |
| Non-default target & existing-MR-per-source-branch → **404** (not 400) | both throw `InvalidBranchException` in `MergeRequestCreateProcessor`, caught → `HTTP_NOT_FOUND` |
| merge answers 202 + Storage job | `JsonResponse($job->toApiResponse(...), 202)` in `MergeAction` |
| 409: "not ready" carries `storage.mergeRequests.notReadyToMerge`, a conflict carries `storage.mergeRequests.validation` + `params.errors` with the conflicting configs | 3 `BranchIsNotReadyToMerge` sites in `MergeProcessor`; `MergeValidationException::getStringCode` + `ExceptionConverter.php:99-125` (re-verified 2026-08-27; previously mis-recorded as code-less) |
| Failed merge rolls back the MR | `rollbackMerge` in `MergeDevBranchJob`'s catch |
| Source branch deleted as a second job, no handle returned | `createAndEnqueueJobFromJob(..., DevBranchDelete::OPERATION_NAME, ...)` after commit in `MergeDevBranchJob` |
| diff/rebase 400 on default branch | `ConfigurationRebaseNotAvailableOnDefaultBranchException` / diff OA doc → `createBadRequestException` |
| Diff shape `base`/`ours`/`theirs`, each nullable | `ConfigurationDiffResponse` |
| Each diff side = `{version, isDeleted, diff: {name, description, changeDescription, isDisabled, configuration, rows}}` -- content NESTED under `diff`, version/deletion as side metadata; all six `diff` keys `required` | `ConfigurationVersionResponse` + `ConfigurationDiffData` OA schemas (re-verified 2026-08-27; a flat-side assumption breaks every take/classify consumer) |
| Full `MergeRequestResponse` item: `id, creator{id,name}, title, description, state, branches{branchFromId,branchIntoId}, merge{mergedAt,mergerId,mergerName}, createdAt, externalId, autoMergeStrategy, autoMergeAt, approvals[], reviewers[]` -- `merge{}` is NESTED (no flat mergerName), `createdAt` is top-level; list and detail share this item shape byte-for-byte (detail adds `changeLog`, `?include=activityLog` adds `activityLog`) | `MergeRequestResponseProvider.php:86-117` (`getCreateMRResponseArray`), `:132-139` (list maps the same builder) |
| Rebase replaces; missing `configuration` → `{}`, `isDisabled` → `false`, `description` → null | `RebaseRequest::mapValidatedData` (`?? new stdClass()`, `?? false`, `isset` → null); "complete 3-way diff result … fully replaces" verbatim in `ConfigurationRebaseService` docblock |
| `rows` required for keep; `[]` deletes all rows; order = sort order | `validateDiffRows` + OA schema |
| Empty `diff` `{}` = delete resolution (tombstone) | `validateDiff` empty-stdClass branch → `isDelete: true` |
| `changeDescription` null → default rebase message | `ConfigurationRebaseService` line ~102 |
| Target version must be newer → 400 | `ConfigurationRebaseTargetVersionNotNewerException` → 400 (ULID comparison) |
| `protected-default-branch` passes the same feature gate | `StorageRouteGuard` loops `RequireFeature.features` with OR semantics; MR routes list both features, rebase lists only `branches-merge-requests` |

Resolved subtlety worth remembering: `RebaseRequest::validateDiff` expects `diff` as a *string*
and `json_decode`s it — which at first glance contradicts the client sending a real nested
object. It doesn't: the rebase action maps the body with
`#[MapRequestBody(realJsonMapProps: ['diff'])]`, and `JsonExtractor` re-encodes a nested `diff`
object back into a JSON string before validation (preserving the `{}`-vs-`[]` distinction). So
the nested-object body is correct, and `{}` survives as the delete sentinel while `[]` is
rejected ("diff must be an object").
