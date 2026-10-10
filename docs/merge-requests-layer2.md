# Merge requests — Layer 2 (service), working notes

Linear: [DMD-1899](https://linear.app/keboola/issue/DMD-1899). Layer 3 shipped in #556 (see
[`merge-requests-layer3.md`](merge-requests-layer3.md)); backend behavior facts with citations
live in [`merge-requests-notes.md`](merge-requests-notes.md). Commands/UX material is in
[`merge-requests-layer1.md`](merge-requests-layer1.md). Scope stays **non-SOX**.

## Service shape

Decided 2026-08-26: the house pattern, no deviations.

- One `MergeRequestService` class in `services/merge_request_service.py` (DI: `ConfigStore` +
  `client_factory`, like every service).
- Method names are full `verb_noun` (`list_merge_requests`, `get_merge_request`,
  `create_merge_request`, …) — the convention of all ~30 services. The L3 namespace de-dup
  (`client.merge_requests.list()`) does not transfer: at L2 call sites the instance lives in a
  generic `service` variable, so the noun must be in the method name or it is nowhere.
  Methods whose name carries the noun another way stay short (`list_conflicts`,
  `resolve_conflict`, `get_config_diff`).
- The class holds orchestration and I/O only. Pure logic with no dependencies — state
  derivation, diff flattening, rebase-payload composition — goes into module-level functions
  (testable without mocks).
- Single file until `make loc-check` says otherwise; the natural split line is lifecycle
  (create/list/get/transitions/merge) vs. conflict resolution (conflicts/diff/rebase).
  Precedent for a second file: the non-1:1 services (`member_service`, `variables_service`, …).

## Derived status (decided 2026-08-26)

Callers branch on data, not parsed prose. Long-term the derivation belongs to the backend —
one evaluation point, every client (UI, CLI, MCP) consumes it evaluated, the way GitHub
serializes `mergeable_state` / `reviewDecision` / `viewer*` instead of letting every client
re-derive them. Connection has no capacity now, so the CLI ships a **polyfill**: one pure
module-level function, server-first (`mr.get(...)` prefers the future serialized field),
local fallback implementing the tables below. The fallback carries a comment pointing at the
Connection issue — [DMD-1988](https://linear.app/keboola/issue/DMD-1988) — and is deleted
when the backend serializes. Precedent for the defensive
read: `changeLog`, and the required-approvals count
([DMD-1969](https://linear.app/keboola/issue/DMD-1969)).

Evidence the derivation must not live in clients: the UI already disagrees with itself — the
list badge (`MergeRequestRow.tsx`: `published`→"Merged", `canceled`→"Closed", `rejected`/
closed-by-creator derived from `reviewers[]`, no `in_merge` badge) vs. the detail panel
(`MergeRequestInfoPanel.tsx:12-19`: "Published", "Canceled", "Merging", no derivations at
all). The same MR shows "Rejected" in the list and "Development" in the panel.

Four derivates; all `--json` fields are additive, raw `state` is always emitted alongside
(derivation never replaces wire truth):

**1. `derived_state`** (list + detail) — the UI list badge's decision table, evaluated in
order; canonical vocabulary for all clients:

| value | derivation | GitHub analog |
|---|---|---|
| `rejected` | `development` + a non-creator reviewer with `status=rejected` | open + CHANGES_REQUESTED |
| `closed` | `canceled`, or `development` + creator self-rejection (the UI "cancel" trick) | closed |
| `in_development` | `development` otherwise | open |
| `in_review` | `in_review` | open + REVIEW_REQUIRED |
| `approved` | `approved` | open + APPROVED |
| `in_merge` | `in_merge` (the one state the UI badge omits — we name it) | — |
| `merged` | `published` | merged |

Reliability caveat (Opus wire review 2026-08-27, verified against Connection): the
`rejected` / self-`closed` rows depend on `reviewers[].status`, which the backend populates
only within a review round anchored by a real `request_review` activity event — and
`skip_review` writes none. With the non-SOX default of 0 required approvals, every
`request-review` takes the skip path, so `status` is always `null` and those two overrides
never fire; additionally, explicit reviewers shadow every non-reviewer's decision and the
creator can never *be* a reviewer (422). **The UI badge has the identical blind spot** —
this table is its port. The reliable source is the MR's **activity log**
(`changes_requested` events, un-anchored and un-shadowed), which is what DMD-1988 asks
Connection to derive `derivedState` from server-side. The CLI polyfill stays a best-effort
port of the UI on purpose: matching the UI's (flawed) behavior until the backend serializes
the truth beats maintaining a third, differently-wrong derivation.

**2. `merge_blockers`** (detail only; list omits it — conflicts are not fetched per row) — a
*list*, not a single enum, so concurrent blockers don't mask each other; plus sugar
`mergeable: bool` (= empty list). Purely mechanical, **not a guard** — the merge 409 stays
the authority:

| blocker | derivation |
|---|---|
| `conflicts` | live conflicts list non-empty (count + list emitted alongside) |
| `approvals` | `state == in_review` (the state collapses the requirement; quantitative "1 of 2" only when DMD-1969 lands — read defensively) |
| `state` | `in_merge` / `published` / `canceled` — merge not applicable |

Note the honest consequence of the backend facts: a `rejected` MR has **no** blocker — it
sits in `development` and a non-SOX merge from there succeeds (auto-`skipReview`). The story
is told by `derived_state`, not by a fake blocker.

**3. `allowed_actions`** (detail) — subset of `{request_review, approve, request_changes,
merge, update, resolve_conflicts}`, mechanically from the state machine. Corrected against
the real workflow (Opus wire review 2026-08-27): `approve` exists **only in `in_review`**
(its sole `from` place — from `approved` the backend answers 422; the UI button showing it
there is wrong), and even in `in_review` it is further gated by `AddApprovalGuard` (not the
creator, not already approved, required count not reached) — with the non-SOX default of 0
required approvals, `approve` is 422 in every state and `in_review` itself is unreachable.
`update` is blocked only in terminal states (an `in_merge` MR is still updatable);
`request_changes` from `in_review|approved`; send-for-review only in `development`. The
polyfill does *not* mix roles/features in (the pre-flight owns those); the backend adds them
when it takes over.

**4. `viewer`** (detail) — `{is_creator, has_approved}`, relative to the caller's identity
(admin id from `verify_token`, compared against `creator.id` and `approvals[].approverId`).
What the UI's approve button derives today (`ApproveMergeRequestButton.tsx:161`), and what an
MCP/agent response needs to phrase the next step: blocker `approvals` + `has_approved=true`
→ "wait for the other reviewers", + `is_creator=true` → "you cannot approve your own MR".

`approvals[]` gives *who* approved; `reviewers[].status` (`approved`/`rejected`/null) gives
*who is still pending* — both stay available raw in the detail output.

## Pre-flight feature check

`GET /merge-request` list/detail/conflicts are ungated; a write without the feature is a 403
byte-for-byte identical to a role denial. So the service calls
`has_feature(BRANCHES_MERGE_REQUESTS_FEATURE)` (`client/tokens.py:302`, cache populated on
every `verify_token`) before writes and words the "not enabled" error itself.

Caveats to carry into the implementation:

- **SOX fence assumption:** server-side, the six MR writes accept *either* feature; only
  `/rebase` requires `branches-merge-requests` specifically. The pre-flight fences off SOX
  projects **only if** a SOX project never also has `branches-merge-requests` — state that
  assumption explicitly in the code comment.
- The pre-flight is **stricter than the server** for a project with only
  `protected-default-branch`: kbagent refuses what the API would allow. Deliberate (the SOX
  approvals semantics are out of scope), but the error message should mention it.
- Constant name decided 2026-08-26: rename to `BRANCHES_MERGE_REQUESTS_FEATURE` when wiring
  the pre-flight — the file's dominant convention is the `…_FEATURE` suffix
  (`STORAGE_BRANCHES_FEATURE`, `GLOBAL_SEARCH_FEATURE`, `PAYG_FEATURE`); the prefix form is
  the lone outlier. Two touch points: `constants.py:439` + the docstring mention in
  `client/merge_requests.py:132`.

## Client-side `state` filtering

The list endpoint has no query parameters — a `--state` filter is the service's job.

## Merge: 409 handling and error codes

The merge 409 has four causes in two shapes (`MergeAction.php:97-109`): three "not ready"
cases carry the machine-readable `storage.mergeRequests.notReadyToMerge`; a conflict raises
`MergeValidationException` with its own code `storage.mergeRequests.validation` (plus the
conflicting configurations in `params.errors` -- see the notes doc, corrected 2026-08-27).
Today both fall through `http_base.py`'s
generic `API_ERROR` catch-all (`http_base.py:306-336`; neither 409 nor 422 is mapped, neither
retryable).

Decided 2026-08-26 (rationale corrected 2026-09-05 — the original text still carried the
superseded "code-less conflict" reading): **two new `ErrorCode` members, mapped in the
service** — both 409 shapes carry a machine string code (`storage.mergeRequests.notReadyToMerge`
vs `storage.mergeRequests.validation`, see the notes doc), but those codes are specific to the
merge endpoint, and only the service knows that is where the 409 came from; the generic
`http_base` layer maps by HTTP status alone and must not learn endpoint vocabularies:

- `MR_NOT_READY_TO_MERGE` — the 409 carrying `storage.mergeRequests.notReadyToMerge` (three
  causes: merge lock / wrong state / another MR processing; distinguishable only by message
  text, hence one code). Transient states → `retryable=True`.
- `MR_MERGE_CONFLICT` — the 409 carrying `storage.mergeRequests.validation` (matched by
  code; a code-less 409 falls back here for older stacks, any *other* code passes through
  unmapped). The body's `params.errors` lists the conflicting configurations and is passed
  through in details. `retryable=False`, message names the conflicts command as next step.

Names may be polished to the enum's convention at implementation time. Both must be
documented in `docs/error-codes.md` (`scripts/check_error_codes.py` enforces in CI).

## Post-merge cleanup

A successful merge always deletes the source branch (second async job, no handle — the await
covers the merge only; see notes doc).

Decided 2026-08-26 — mirror `delete_branch` (`services/branch_service.py:255-307`), which
already performs exactly this cleanup today. After a successful merge:

- reset `active_branch_id` **only if** it points at the merged source branch (the
  `was_active` logic of `delete_branch:286-288`; do *not* copy `get_merge_url:349`'s
  unconditional reset — that is the worse of the two precedents),
- read `branches.branchFromId` from the MR payload **before** calling merge — it is
  nullable once the MR is published,
- clean the sync branch mapping via `cleanup_branch_id_from_mapping`. The helper swallows
  read errors (returns `None`) but its final `save_branch_mapping` can raise on IO — so
  `merge()` wraps the whole cleanup block: a cleanup failure degrades to a warning in the
  result, never changes the success exit code,
- a failed merge does no cleanup (the branch is still alive),
- workspaces on the branch need nothing: the server drops them with the branch (notes doc);
  leftovers are `workspace list --orphaned` / `workspace gc` territory,
- output says the source branch "is being deleted" — never "is deleted" — and the result is
  structured like `delete_branch`'s (`was_active`, `mapping_cleanup`, `message`),
- cleanup failures land under **`warnings[]`** (decided 2026-09-03, with the Layer 1 RFC): the
  same key `resolve_conflict` uses, so the group has one soft-failure channel — "the operation
  landed, something secondary did not, exit stays 0". It was briefly `cleanup_warnings`; a
  renderer reading `warnings` would have silently dropped exactly the post-merge ones a user
  must act on.

## Additions made for Layer 1 (2026-09-03)

Three small additions decided while walking PR #703's review findings into the Layer 1 RFC
([`merge-requests-layer1.md`](merge-requests-layer1.md), "Layer 2 changes shipping with this
PR"). Each exists so Layer 1 does not re-derive something the service already knows:

- **`get_merge_request_row(alias, merge_request_id)`** — the row tier (`_enrich_row`:
  raw + `derived_state` + `allowed_actions`) addressed by id. `list` / `find` already return
  rows, but only by branch; the sole by-id method was the **detail**, which also spends a
  conflicts GET and a `verify_token` GET. Layer 1 needs one field *before* a write (is the MR
  armed for auto-merge? which branch will `merge` delete?) and must not inherit a dependency on
  the conflicts endpoint for it. Layer 3's `merge_requests.get()` was always this one GET.
- **`get_config_diff` → `resolution_candidate`** — the ours envelope through
  `_DIFF_CONTENT_KEYS` (`name`, `description`, `isDisabled`, `configuration`, `rows`;
  `description` as an explicit `null`; `changeDescription` excluded), or `null` when ours is
  absent / `isDeleted`. Composed here so the prefill `diff --output` writes and the five-key
  replace guard in `resolve_conflict` are fed by the same constant — a candidate built in Layer
  1 that dropped a null `description` would be a file kbagent writes and then refuses. Pinned
  by a round-trip test: the candidate passes `resolve_conflict(resolved=…)` unmodified.
- **`merge()` `cleanup_warnings` → `warnings`** — above.

Three more from the follow-ups (2026-09-04, `merge-requests-layer2-followups.md`): `get_merge_request`
carries **`feature_enabled`** (F5 — the detail already pays `verify_token`, so the features cache
is warm; `list` stays feature-blind on non-empty results); `merge()` carries **`cleanup_skipped:
true` + `branch_from_id_raw`** when the source branch id could not be read (F3 — the prose alone
left that result byte-identical to a legitimate published-MR null); `get_config_diff` reports an
**empty envelope on either side in `warnings`** (F2 — the classifier emits no rows for it, and
"no rows" must not read as "the conflict cleared").

## Rebase / conflict resolution semantics

Layer 3 deliberately does no payload validation (the signature covers structure). Service
concerns:

- `version` for a rebase comes from the diff's `theirs.version` (it is the default-branch
  version being re-anchored onto).
- Whether the config is in the MR's conflict set, and whether the resolved body is a sensible
  three-way merge, are service checks.
- A conflict requires the config to exist on both sides, so `theirs` of a conflicting config's
  diff is always populated; rebasing every conflicting config makes the MR mergeable (no
  re-validate step).
- Flattening the nested `base`/`ours`/`theirs` diff for presentation is Layer 2's job (each
  side may be null).

Decided 2026-08-26: `resolve_conflict` offers four modes, **all via the rebase endpoint**
(one uniform mechanism, no new Layer 3 method):

- `take=theirs` — the diff's theirs side (production content) rebased onto `theirs.version`;
- `take=ours` — the ours side (dev content) rebased onto `theirs.version`;
- `delete` — `rebase_config_delete` (the `{}` tombstone);
- a caller-supplied resolved body (JSON/@file) — pass-through with the conflict-set check,
  the escape hatch for a genuine manual three-way merge.

Edge case: an ours side with `isDeleted` turns `take=ours` into the delete resolution.

Known deviation from the UI: its "Keep production version" button calls
`POST …/reset-to-default` (the config drops out of the MR entirely — not in the changeLog,
untouched by the merge), while our `take=theirs` via rebase keeps the config in the changeset
(changeLog entry + a content-no-op write at merge). Which behavior is intended is DMD-1987;
if reset wins, Layer 3 gains a `reset_config_to_default` method and `take=theirs` switches.
A bulk "resolve all one way" is deliberately out of v1 — it is a trivial Layer 1 loop over
`list_conflicts` + `resolve_conflict`.

Presenting the three-way diff (decided 2026-08-26): no three panes — a **per-path change
classification**. A pure Layer 2 function computes two pairwise diffs (`base→ours`,
`base→theirs`) and tags every touched path `changed_by: ours | theirs | both`; only `both`
paths are the actual conflict. Tooling to steal: `json_utils.compute_diff` already has the
recursive walk but returns formatted strings — refactor it into a structured per-path
variant (entries as data) and keep the string output as a formatter over it
(`config_service` uses it today). The human rendering (Layer 1, DMD-1900 material) is a
table in three sections — *Both changed / Only you changed / Only production changed* —
with long values elided behind a `--format full`. `--json` carries the entries plus all
three raw sides. Manual merge stays marker-free: `diff --output resolved.json` writes an
ours-prefilled candidate, the caller edits it and submits via `resolve --file` — the
git-mergetool loop with a file as the third pane.

## ~~`mcp_parity.py` and the canary~~ (obsolete since 0.85.0)

This section predates 0.85.0 and no longer applies: the parity map (`mcp_parity.py`),
`scripts/check_mcp_parity.py` and the weekly `mcp-parity-canary` were all deleted with the
MCP passthrough removal (#609, 2026-08-19). No parity entries are needed anywhere for the
MR commands; the historical tool-to-command map lives in `docs/mcp-migration.md`. The
parallel `keboola-mcp-server` MR-tools effort continues independently, untracked by kbagent
CI.

## Open decisions

- ~~Whether `merge-request create` takes the source branch from `--branch` or from
  `active_branch_id`~~ — **decided 2026-08-26: the standard `resolve_branch()` idiom**, like
  every other branch-taking command. Explicit `--branch` wins, else `active_branch_id`; with
  neither, a readable error ("pass --branch or run branch use") — no further fallback. The
  create output must state which branch the MR was created from.
- The fate of `kbagent branch merge` (the UI-URL escape hatch, `branch_service.py:309`):
  deprecate-with-pointer, already committed as DMD-1900 scope (pattern: the #390 tool-group
  deprecation).
- SOX flow, branch creation/deletion changes, auto-merge scheduling UX beyond passing the
  fields through — all out of scope for now.
- E2E tests are mandatory with the commands (convention #16) — they need a
  `branches-merge-requests` project.
