# Merge requests — Layer 3 (HTTP client), as built

**Status: shipped.** [DMD-1701](https://linear.app/keboola/issue/DMD-1701), PR
[#556](https://github.com/keboola/cli/pull/556), squash-merged to main as `b7b66af`
(2026-08-19), released in 0.86.0 (changelog completed by #619). This document is the as-built
record distilled from the original RFC and the review cycle; behavioral backend facts live in
[`merge-requests-notes.md`](merge-requests-notes.md).

Code: `client/merge_requests.py` (namespace + Protocol + adapter + mixin),
`client/configs.py` (diff/rebase), `constants.py` (`BRANCHES_MERGE_REQUESTS_FEATURE` --
renamed from `FEATURE_BRANCHES_MERGE_REQUESTS` when Layer 2 wired the pre-flight,
`MERGE_JOB_MAX_WAIT`), `tests/test_merge_request_client.py`.

## Backend contract (what shapes the client)

Project-level — `isAvailableInBranch: false`, so **never** branch-prefixed:

| Method / path | Body | Success | Notable failures |
|---|---|---|---|
| `GET /v2/storage/merge-request` | — | 200 | — |
| `POST /v2/storage/merge-request` | JSON | **201** | 404 invalid branch, 422 invalid reviewer, 403 |
| `GET /v2/storage/merge-request/{id}` | — | 200 | 404 |
| `PUT /v2/storage/merge-request/{id}` | JSON | 200 | 403, 404, 422 |
| `PUT …/{id}/request-review` | — | 200 | 403, 404, 422 |
| `PUT …/{id}/approve` | — | 200 | 403, 404, 422 |
| `PUT …/{id}/request-changes` | JSON `{reason?}` | 200 | 403, 404, 422 |
| `PUT …/{id}/merge` | — | **202** + a Job | **409**, 403, 404 |
| `GET …/{id}/conflicts` | — | 200 | 404 |

Branch-scoped — `isAvailableInBranch: true, isAvailableWithoutBranch: false`, so **always**
branch-prefixed:

| Method / path | Body | Success | Notable failures |
|---|---|---|---|
| `GET …/branch/{branch}/components/{c}/configs/{cfg}/diff` | — | 200 | 400 on default branch, 404 if absent in both branches |
| `POST …/branch/{branch}/components/{c}/configs/{cfg}/rebase` | JSON | 200 + the rebased configuration | 400 default branch / target version not newer / bad `diff`, 403, 404 |

`GET /merge-request` declares **no query parameters** — a `state` filter is necessarily
client-side. `GET /merge-request/{id}` takes `include=activityLog`. Only `merge` is
asynchronous.

**Bodies are JSON with real types** — `#[MapRequestBody]` accepts form data but
`FormDataExtractor` does no type coercion, and validators require real types (`branchFromId`
`Assert\Type('int')`, rebase `version` `Assert\Type('integer')`). Form-encoded values stay
strings and fail validation. No client-side `json.dumps` for nesting either — the rebase
action's `realJsonMapProps: ['diff']` re-encodes server-side, preserving `{}` vs `[]`.

**The rebase `diff` envelope** (connection#8040): keep =
`{"version": N, "diff": {"name", "rows", "configuration", "isDisabled", "description"?,
"changeDescription"?}}`; delete = `{"version": N, "diff": {}}`. `version` stays top-level and
is the **default-branch** version being re-anchored onto (from the diff's `theirs.version`) —
the wire name is a trap, kept for wire fidelity, spelled out in the docstring.

## Shipped surface

`client.merge_requests.*` (namespace, raw parsed JSON returns):

| Method | Endpoint |
|---|---|
| `list()` | `GET /v2/storage/merge-request` |
| `get(id, include_activity_log=False)` | `GET …/{id}[?include=activityLog]` |
| `conflicts(id)` | `GET …/{id}/conflicts` |
| `create(branch_from_id, branch_into_id, title, …)` | `POST /v2/storage/merge-request` |
| `update(id, …)` | `PUT …/{id}` |
| `request_review(id)` | `PUT …/{id}/request-review` |
| `approve(id)` | `PUT …/{id}/approve` |
| `request_changes(id, reason=None)` | `PUT …/{id}/request-changes` |
| `merge(id)` | `PUT …/{id}/merge` + awaits the Storage job |

On `_ConfigsMixin` (flat, config endpoints): `get_config_diff(component_id, config_id,
branch_id)`, `rebase_config(…, version, name, rows, configuration, is_disabled, description,
change_description=None)`, `rebase_config_delete(…, version)`.

## Design decisions, as built

- **Namespace over flat methods** (`client.merge_requests.*`). Flat naming collides
  (`request_merge_request_review`, `merge_merge_request`); the namespace keeps verbs
  wire-faithful. **Normative for new endpoint families only** — existing flat families stay
  flat (policy stated in the module docstring; consider promoting to CONTRIBUTING.md with
  Part 2).
- **The namespace depends on a `StorageRequester` Protocol, not on the client.**
  `_ClientRequester` is a marked temporary adapter delegating to `_CoreClient`'s protected
  methods; the client-split RFC (#595, branch `martinsifra/requestor`) builds the real
  transport under this seam later — swap is one line, the namespace and its tests stay
  byte-identical. Keep the Protocol minimal. (Honest caveat from review: `request()` returning
  `httpx.Response` ties the future transport to httpx — today it is more a rename of
  `_request` than an abstraction.)
- **JSON bodies throughout**, deviating from `configs.py`'s form idiom; every method's
  docstring says so. (Post-rebase note: #598 already added JSON-body methods to `configs.py`,
  so "unlike this file's idiom" phrasing was softened.)
- **`merge()` awaits implicitly** like every Storage-job method in `client/` — no `wait` flag.
  Budget `MERGE_JOB_MAX_WAIT` = 600 s (precedent `IMPORT/EXPORT_JOB_MAX_WAIT`): a many-config
  merge plausibly outlives the default 60 s, and a mid-merge `STORAGE_JOB_TIMEOUT` with
  `retryable=True` would be actively misleading. Returns the completed job dict whose
  `results` carry the MR incl. change log. **The await covers the merge outcome only** — the
  source-branch deletion is a second job with no handle (docstring states it).
- **Contract dependency on the poller (#603):** `wait_for_storage_job` must raise on a failed
  job whether the failure arrives in the initial body or polled — `merge()` does **not**
  re-check. History: the poller originally returned an already-terminal error body silently
  (a house-wide bug across all 19 call sites); `merge()` carried a local guard until the
  central check-then-fetch fix merged as #603 (`2f0544d`, v0.84.3) and the guard was dropped
  on rebase. The requirement is stated on the Protocol docstring and pinned by #603's
  `TestWaitForStorageJob`, not by a merge-level test.
- **diff/rebase live in `client/configs.py`** — `client/` is split by URL family (#520), not
  by feature. Their `branch_id: int` is **required with no production fallback** (both answer
  400 on the default branch) — production is unrepresentable rather than a runtime error,
  deliberately breaking the house `branch_id: int | None = None` idiom.
- **Keep and delete rebases are two methods.** `rebase_config` requires `name` and `rows`
  (matching `validateDiffName`/`validateDiffRows`); `rebase_config_delete` sends exactly
  `{"version": N, "diff": {}}`. The signature does the validation; no illegal combination is
  expressible.
- **Rebase REPLACES, so every replaced-body field is required** (`name`, `rows`,
  `configuration`, `is_disabled`, `description` — the last required-but-nullable). Optional
  params with defaults would make silent data loss the signature's default (wiped config body,
  re-enabled config). Landed via padak's #606 during review. `change_description` is the one
  genuine optional (not part of the replaced body; null selects a default message).
  Presence-detection (`is not None`, omit unset) stays correct for `create`/`update`, which
  genuinely patch; `_optional_mr_fields` is keyword-only (four of five params are
  `str | None`).
- **No feature-flag plumbing at Layer 3.** A missing feature is a 403 identical to a role
  denial — only a Layer 2 pre-flight can word the error. Part 1 contributed only the constant
  `BRANCHES_MERGE_REQUESTS_FEATURE`.
- Tried and reverted: keyword-only `rebase_config` (bare `*`). The "ids are positional
  house-wide" premise was false (Layer 2 call sites are mixed), so no placement had a
  consistency case; signature stays shaped like its `configs.py` siblings. If keyword-only is
  ever wanted, make it a house-wide CONTRIBUTING.md rule, not a one-method exception.
- Python gotcha hit: `MergeRequests.list` shadows the builtin in class-scope annotations →
  module-level aliases `_DictList`/`_IntList`.

## What the tests pin (`tests/test_merge_request_client.py`, 27 tests)

- Path construction: MR paths never branch-prefixed even with an active branch; diff/rebase
  always are.
- JSON encoding: content-type, real nested JSON, JSON ints for `version`/`branchFromId`,
  real booleans.
- The `diff` envelope: `version` top-level, content inside `diff`; delete sends exactly
  `{"version": N, "diff": {}}` (object, not null/`""`/`[]`); `rows=[]` is sent, not omitted.
- Required replaced-body params: a TypeError loop over each omitted field + a wire test that
  `configuration`/`isDisabled` always reach the body.
- `merge()` waits with `MERGE_JOB_MAX_WAIT`, not the default — pinned via a recording stub
  requester (httpx mocks can't see the kwarg).
- The seam: one test constructs `MergeRequests` against a stub `StorageRequester`, no HTTP.

## Deferred / follow-ups recorded during review

- ~~`FEATURE_BRANCHES_MERGE_REQUESTS` naming~~ -- resolved by Layer 2 (PR #703): renamed to
  `BRANCHES_MERGE_REQUESTS_FEATURE` when the pre-flight was wired. Original note: off the
  file's dominant convention (suffix:
  `STORAGE_BRANCHES_FEATURE`, `GLOBAL_SEARCH_FEATURE`, `PAYG_FEATURE`) and the constant is
  unused until Layer 2 calls it — decide rename vs. keep when wiring the pre-flight.
- SOX-fence caveat on the constant's comment: the fence holds only if a SOX project never also
  has `branches-merge-requests` — see the layer2 doc for the pre-flight consequences.
- Docstring nit: `request_review`/`approve` PUT an empty body while `request_changes` without
  a reason PUTs `{}` — both correct, difference undocumented.
- Retry policy: since #616 POST is no longer retried; PUT/DELETE are, and the four PUT
  transitions verifiably cannot double-apply (#617; see the notes doc, *PUT transitions cannot
  double-apply*). The residual lost-response-replay caveat (attempt 2's misleading 422/409) is
  house-wide, not MR-specific.
