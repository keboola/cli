# Layer 2 findings surfaced while writing the Layer 1 RFC (DMD-1900 → DMD-1899)

Context: `docs/merge-requests-layer1.md` (PR #708) is the Layer 1 command RFC, written against
`MergeRequestService` as it stands on `ms/dmd-1899/cli-layer-2` (PR #703). Designing the
commands surfaced seven things that belong to Layer 2 or to the docs Layer 2 owns. Two are
defects, one is a safety fact nobody has recorded anywhere, four are documentation gaps.

Everything below was verified against the code / the local Connection checkout
(`/home/martin/keboola/connection`). Nothing here is a style preference.

---

## 1. DEFECT — `get_config_diff` takes `branch_id` from the caller; `resolve_conflict` proves it must not

`services/merge_request_service.py:742-744`:

```python
def get_config_diff(self, alias: str, component_id: str, config_id: str, branch_id: int)
```

`resolve_conflict` (`:834-853`) deliberately does the opposite — its docstring says so:

> The branch is NOT a parameter: it is derived from the merge request itself
> (`branches.branchFromId`), so the conflict-set guard and the branch being written to can
> never disagree -- a caller-supplied branch id could point the rebase at an unrelated dev
> branch that the guard never checked (rebase REPLACES; that would be silent data loss).

That reasoning came out of the PR #703 self-review. `get_config_diff` has the identical shape
and never got the same treatment: it reads whatever branch the caller names, with no relation
to any merge request.

**Why it matters for Layer 1.** The RFC's `diff` command is documented as "branch derived from
the MR". With the service as-is, Layer 1 cannot do that: when the user passes `--mr-id`
explicitly, the command layer holds only an integer and would have to call
`get_merge_request()` just to read `branches.branchFromId` — which additionally fires a
conflicts GET and a `verify_token` GET it has no use for (`:383-397`). Three round trips for
one integer, with the branch→MR relation re-derived in the command layer, i.e. business logic
in Layer 1.

**Suggested fix (either works):**
- change the signature to `get_config_diff(alias, merge_request_id, component_id, config_id)`
  and derive the branch via the existing `_branch_from_id_of` — full symmetry with
  `resolve_conflict`; or
- keep the signature and add a small public accessor (`branch_from_id(alias, merge_request_id)`)
  wrapping `_branch_from_id_of`, so Layer 1 gets the integer in one cheap call.

The first is better: it makes the wrong call unrepresentable, which is the argument
`resolve_conflict`'s docstring already makes.

## 2. DEFECT — `allowed_actions` is produced by `get_merge_request` only

`derive_allowed_actions` is applied in `get_merge_request` (`:408`) and nowhere else.
`_enrich_row` (`:236-239`) adds **only** `derived_state`, so every other return —
`create_merge_request:463-468`, `update_merge_request:499`, `request_review:516`,
`approve:527`, `request_changes:546`, and each `list_merge_requests` row (`:314`) — carries no
action data. `merge` (`:649-664`) carries neither.

**Why it matters.** A `--json` consumer of `create` (an agent deciding what to do next) gets
`derived_state` and nothing else. The natural next-step question — *can I merge now, or does
this need review?* — is answerable from the same state the row already carries, so the data is
free; it is simply not applied.

**Suggested fix:** apply `derive_allowed_actions` inside `_enrich_row` so every command's JSON
carries it. One line, additive, no behaviour change for existing consumers. If list rows should
stay lean, at minimum apply it to the transition returns.

## 3. SAFETY FACT, recorded nowhere — `autoMergeStrategy: immediately` makes the backend merge on its own

Verified in Connection:

```
AutoMerge/AutoMergeCandidateRepository.php:44-47
    WHERE mr.state = :approved
      AND (mr.autoMergeStrategy = :immediately
           OR (mr.autoMergeStrategy = :scheduled AND mr.autoMergeAt <= :now))

AutoMerge/AutoMergeTickHandler.php:86
    $this->mergeProcessor->process($legacyRow, new SystemToken($candidate->projectId));
```

It is the **same `MergeProcessor`** the `PUT …/{id}/merge` endpoint uses, driven by a background
tick under a system token.

**It is polling, not an event on `approve`.** `AutoMergeScheduleProvider.php:24-26` is an
`#[AsSchedule('auto_merge')]` dispatching `AutoMergeTickMessage` every `AUTO_MERGE_INTERVAL`
(`config/services.php:265`, an env var — per-deployment, unknowable from source). Nothing else
dispatches that message anywhere in `src/`. `MergeRequestService` touches auto-merge only in
`applyAutoMerge` (`:226-236`, called from create `:63` and update `:107`), which merely
persists the strategy on the entity; the approve path has no auto-merge hook at all. So the
chain is: the MR reaches `approved` → the **next tick** merges it, after re-checking state,
strategy and schedule (`AutoMergeTickHandler.php:63-83`).

Three consequences:

- **Order does not matter.** The strategy can be set *before* the MR is approved — it sits on
  the entity and fires the moment the state arrives. That is what makes the sequence below
  work: neither write command is the thing that merges.
- On a non-SOX project with the default 0 required approvals there is no "last approve" at
  all — `request_review` lands the MR straight in `approved` via skip_review, so that is the
  trigger. `create(auto_merge_strategy="immediately")` + `request_review()` is enough;
  `update(auto_merge_strategy="immediately")` on an already-approved MR is enough on its own.
- **The tick retries indefinitely.** A conflict or a "not ready" does not cancel the auto-merge
  — `AutoMergeTickHandler.php:88-99` logs and retries every subsequent tick until it succeeds
  (notifying once per blocked episode). Unlike a CLI `merge`, which is one shot and returns the
  error, an auto-merge cannot be left to fail; it stops only when the strategy is set to `none`.

For kbagent this also means `update --auto-merge-strategy immediately` returns 200 and the
merge happens later, invisibly: no job handle to await, nothing to report.

Nothing in the CLI records this. `docs/merge-requests-notes.md:183` says only
"`AutoMergeStrategy` is exactly `immediately` | `scheduled` | `none`"; `create_merge_request`
(`:409-437`) and `update_merge_request` (`:461-484`) pass the field through without comment.

**Why it matters.** It means two *write*-class operations can cause a production merge and a
source-branch deletion without anything ever calling `merge()`. Layer 1 handles the permission
consequence on its side (escalating the flag to `destructive`), but the underlying fact belongs
in the notes doc and in both service docstrings — a reader of `create_merge_request` currently
has no way to know the parameter is anything but metadata.

**Suggested fix:** record it in `docs/merge-requests-notes.md` with the two citations above, and
add one sentence to both docstrings.

## 4. DOCS — the wire field list in the notes doc is incomplete

`docs/merge-requests-notes.md:111-114` records the `approvals` and `reviewers` sub-shapes but
never the full response. The authoritative serializer is
`Storage/MergeRequests/MergeRequestResponseProvider.php:86-117` (`getCreateMRResponseArray`),
and it emits:

```
id, creator{id,name}, title, description, state,
branches{branchFromId, branchIntoId},
merge{mergedAt, mergerId, mergerName},          <- NESTED, not a flat mergerName
createdAt,                                       <- the MR's own creation time
externalId, autoMergeStrategy, autoMergeAt,
approvals[]{approverId, approverName, createdAt},
reviewers[]{id, name, status}
```

`getListMRResponseWithoutChangeLogArray` (`:132-139`) maps the same builder per row, so list and
detail have a byte-identical item shape; detail adds `changeLog` (`:122-127`) and, with
`?include=activityLog`, `activityLog`.

Three of these are absent from every kbagent doc: the top-level `createdAt`, the `merge{}`
envelope, and `autoMergeStrategy` / `autoMergeAt` as **response** fields (they are documented
only as request parameters). The Layer 1 RFC got the field list wrong as a direct result — it
asserted there were no timestamps at all and that `mergerName` was top-level.

**Suggested fix:** put the full serializer shape in the notes doc's wire-truth table with the
`MergeRequestResponseProvider.php:86-117` citation.

## 5. DOCS — the scoped-token access claim needs a citation and a reconciliation

`MergeRequestVoter.php:49-56` returns false when `$authorizedTokenRow->getAdmin() === null`,
and `MergeRequestService::requireMergeRequest` runs it for every `/merge-request/{id}` route —
including the read-only detail and conflicts actions. So a scoped Storage token (no admin
identity) gets 403 there, while `GET /v2/storage/merge-request` has no voter and works.

This is *not* contradicted by `docs/merge-requests-notes.md:137-138` ("Reads (list, detail,
conflicts) are `#[AsReadOnlyAction]` with no role whitelist") — that is about **role**
whitelisting, a different axis from **admin identity**. But the two read together as a
contradiction, and the voter is cited nowhere in a doc set whose whole convention is file:line
evidence.

Related: `docs/merge-requests-layer3.md:22,28` lists the notable failures of
`GET …/{id}` and `GET …/{id}/conflicts` as **404 only**. Every write row lists 403; the read
rows should too.

Note also that `derive_viewer`'s docstring (`:210-234`) reasons about a scoped token reaching it
with no admin identity — via `get_merge_request` that path is unreachable, because the call
fails at the voter first. The None-flags branch is still correct as defence in depth; the
docstring just overstates how it is reached.

**Suggested fix:** add the voter to the notes doc with its citation and one sentence separating
the two axes; add 403 to the two read rows in the Layer 3 doc; soften the `derive_viewer`
docstring.

## 6. MINOR — `list` on a project without the feature is indistinguishable from an empty project

`list_merge_requests` (`:295-333`) runs no pre-flight (correctly — the read endpoints are
ungated), so on a project lacking `branches-merge-requests` it returns HTTP 200 with
`count: 0`. Identical to a project that simply has no merge requests. Every subsequent write
then fails with `FEATURE_NOT_ENABLED`.

**Suggested fix (optional, Layer 2 is the cheap place):** when the result is empty, include a
`feature_enabled: bool` from the already-warm `has_feature` cache, so the renderer can say
"merge requests are not enabled on this project" instead of "No merge requests". Purely
additive.

## 7. MINOR — two vocabularies are private / inline, so Layer 1 must duplicate them

- `_STATE_FILTER_VOCABULARY` (`:78`) is private, but the `--state` help text needs to enumerate
  the accepted values.
- The take modes are inline literals (`:877`, `("ours", "theirs", "delete")`).

The house precedent is public names imported by the command layer:
`from ..services.notification_service import KNOWN_EVENTS, SCOPE_PROJECT_WIDE, VALID_CHANNELS`
(`commands/notification.py:33`).

**Suggested fix:** rename to `STATE_FILTER_VOCABULARY` and add a `TAKE_MODES` tuple, both
public. Pure rename, no behaviour change — and it lets Layer 1 pre-validate to exit 2
(`INVALID_ARGUMENT`, matching every other bad-enum flag in the repo) instead of letting a typo
reach the service and exit 5.

---

## Not Layer 2's problem — listed so it is not re-reported

- `rebase_config_delete` (`client/configs.py:766-772`) has no `change_description`, so
  `resolve --take delete --change-description "..."` would silently drop the text. Layer 1
  rejects the combination at exit 2; Layer 3 is fine as built (the wire body is
  `{"version": N, "diff": {}}`).
- Permission classification, `FLAG_ESCALATIONS`, `--json` confirmation semantics and Rich
  markup escaping are all Layer 1 concerns and are handled in the Layer 1 RFC.

---

# Resolution (2026-08-28, ověřeno proti Connection, zapracováno)

Všech 7 nálezů OPRÁVNĚNÝCH (ověřeno: AutoMergeCandidateRepository::findCandidates
WHERE state=approved AND (immediately OR scheduled<=now); AutoMergeTickHandler.php:86
mergeProcessor->process(..., new SystemToken(...)); MergeRequestResponseProvider:86-117
merge{} vnořené + createdAt top-level; MergeRequestVoter:49-56). Jediná korekce
odůvodnění: u #6 features cache NENÍ warm (list verify_token nevolá) — has_feature
stojí 1 GET, utrácí se jen při prázdném výsledku.

1. FIX — get_config_diff(alias, merge_request_id, component_id, config_id): branch
   derivována přes _branch_from_id_of, echo branch_id ve výsledku; closed MR → čitelná
   VALIDATION_ERROR. Test navíc pinuje wiring (diff volán s branchFromId) — původní testy
   po změně signatury FALEŠNĚ prošly (MagicMock spolkl posunuté argy), přepsáno.
2. FIX — _enrich_row přidává i allowed_actions (každý return: list/find/create/update/
   transitions; merge přidává k post-merge stavu).
3. DOCS — nová sekce "Auto-merge" v notes (s citacemi) + věty v docstringech create/update.
   L1 eskalace flagu na destructive zůstává na DMD-1900.
4. DOCS — plný tvar MergeRequestResponse do wire-truth tabulky (merge{} vnořené,
   createdAt, autoMerge* jako response pole; list==detail item shape).
5. DOCS — voter odstavec v notes (osa admin-identity vs. role), 403 na obou read
   řádcích v layer3, derive_viewer docstring změkčen (defense in depth).
6. FIX — list: feature_enabled bool při prázdném výsledku (has_feature jen tehdy;
   non-empty neplatí GET navíc — pinnuto testem).
7. FIX — STATE_FILTER_VOCABULARY public, TAKE_MODES public tuple (resolve je používá).

94 testů zelených; commit na větvi PR #703.
