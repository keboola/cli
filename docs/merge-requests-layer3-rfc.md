# RFC: Merge requests in kbagent — Part 1, Layer 3 (HTTP client)

Linear: [DMD-1701](https://linear.app/keboola/issue/DMD-1701) — milestone
["Branches 2.0"](https://linear.app/keboola/project/finalize-dev-branches-327e7756d2fd/overview).

Every backend claim here was verified directly against `keboola/connection` and is cited to a
file and line. The `/rebase` body follows
[connection#8040](https://github.com/keboola/connection/pull/8040) (DMD-1890, the `diff`
envelope); this RFC assumes that shape is deployed everywhere. Backend facts that matter to
Part 2 (service semantics, status derivation, UX) live in the companion
[`merge-requests-layer2-notes.md`](merge-requests-layer2-notes.md) — this document keeps only
what shapes the client.

## Problem

Keboola's "Branches 2.0" promotes work from a development branch to production through a
**merge request** (MR): create → request review → (approve) → merge, with a per-configuration
rebase when production has moved on. The Connection backend
(`FEATURE_BRANCHES_MERGE_REQUESTS`) and the UI implement the full non-SOX flow.

kbagent can create, use, reset and delete dev branches (`kbagent branch …`), but has no way to
promote one — today's `kbagent branch merge` composes a UI URL and tells the user to finish by
hand (`services/branch_service.py:309`), so any agent driving kbagent stops exactly when the
work is ready to ship.

This RFC covers **Layer 3 only** — the HTTP client methods over the Connection MR endpoints.
Layer 2 (service, status derivation) and Layer 1 (commands, output) are Part 2. Scope is the
**non-SOX** flow (`branches-merge-requests`); SOX (`protected-default-branch`) is out of scope.

## Backend contract

### Paths and status codes

Project-level — `isAvailableInBranch: false, isAvailableWithoutBranch: true`, so **never**
branch-prefixed. Paths from `openapi/storage.json`; flags from each action's `#[StorageRoute]`.

| Method / path | Body | Success | Notable failures |
|---|---|---|---|
| `GET /v2/storage/merge-request` | — | 200 | — |
| `POST /v2/storage/merge-request` | JSON | **201** | 404 invalid branch, 422 invalid reviewer, 403 |
| `GET /v2/storage/merge-request/{id}` | — | 200 | 404 |
| `PUT /v2/storage/merge-request/{id}` | JSON | 200 | 403, 404, 422 |
| `PUT /v2/storage/merge-request/{id}/request-review` | — | 200 | 403, 404, 422 |
| `PUT /v2/storage/merge-request/{id}/approve` | — | 200 | 403, 404, 422 |
| `PUT /v2/storage/merge-request/{id}/request-changes` | JSON `{reason?}` | 200 | 403, 404, 422 |
| `PUT /v2/storage/merge-request/{id}/merge` | — | **202** + a Job | **409**, 403, 404 |
| `GET /v2/storage/merge-request/{id}/conflicts` | — | 200 | 404 |

Branch-scoped — `isAvailableInBranch: true, isAvailableWithoutBranch: false`, so **always**
branch-prefixed:

| Method / path | Body | Success | Notable failures |
|---|---|---|---|
| `GET …/branch/{branch}/components/{c}/configs/{cfg}/diff` | — | 200 | 400 on default branch, 404 if absent in both branches |
| `POST …/branch/{branch}/components/{c}/configs/{cfg}/rebase` | JSON | **200 + the rebased configuration** | 400 default branch / target version not newer / missing-or-malformed `diff`, 403, 404 |

`GET /merge-request` declares **no query parameters**, so a `state` filter is necessarily
client-side. `GET /merge-request/{id}` takes `include` (→ `include=activityLog`).

Only `merge` is asynchronous (202 + a Storage job). Rebase returns the configuration
(`ConfigurationRebaseAction.php:91-94`), not a job, so it needs no waiting. The merge 409 has
four causes in two response shapes — three "not ready" cases carry the machine-readable
`storage.mergeRequests.notReadyToMerge`, a conflict does not (`MergeAction.php:97-109`); the
details are Part 2's concern (see the notes doc, *Merge behavior*).

### Request bodies are JSON — form encoding does not work

Four endpoints take a body: create, update, request-changes and rebase (approve, request-review
and merge take none). All four use `#[MapRequestBody]`, which accepts form data — but
`FormDataExtractor` does no type coercion (`PayloadExtractor/FormDataExtractor.php:22`), and the
validators require real types: `branchFromId` / `branchIntoId` are `Assert\Type('int')`
(`MergeRequestCreateRequest.php:78-83`), rebase's `version` is `Assert\Type('integer')`,
`reviewerIds` is an array of positive integers. Form-encoded values stay strings and fail
validation.

**So: always `json=`, with real nested objects.** No client-side `json.dumps` is needed for
nested values either — the rebase action declares `realJsonMapProps: [PARAM_DIFF]` and
`JsonExtractor` re-encodes that prop server-side, preserving `{}` vs `[]`
(`PayloadExtractor/JsonExtractor.php:33-52`). This is the opposite of the neighbouring
`client/configs.py` idiom (form data, `json.dumps`'d nesting, `"1"`/`"0"` booleans) — copying
it into `rebase_config` would fail validation on `version` alone.

### The rebase payload — the `diff` envelope

The resolved content is wrapped in a `diff` envelope (connection#8040), mirroring the shape
`/diff` returns each side in, so a resolved diff side can be posted back nearly 1:1.

Keep rebase:

```json
{"version": 42, "diff": {"name": "…", "rows": [], "description": "…",
                         "configuration": {}, "changeDescription": "…", "isDisabled": false}}
```

Delete rebase — an **empty envelope**:

```json
{"version": 42, "diff": {}}
```

Rules, from `RebaseRequest::validateDiff` and `mapValidatedData`
(`Storage/ComponentConfigurations/Rebase/Request/RebaseRequest.php`):

- **`diff` is required** (missing / `null` / malformed → 400). Delete is signalled by an
  **empty** `diff` — `{}` or an empty string; the official PHP client sends `(object) []`.
- **A keep rebase requires `diff.name`** (non-empty after trimming) **and `diff.rows`** (key
  present, an array; `[]` legitimately deletes all rows).
- `diff.description` / `diff.changeDescription` are string-or-null; `diff.isDisabled` defaults
  to `false`; `diff.configuration` must be an object, defaults to `{}`. Row objects are
  `{id?, name?, description?, isDisabled?, configuration?}` — missing/null `id` means a new
  row, duplicates are rejected, array order becomes sort order.
- `version` stays at the **top level** and is always required. The wire name is a trap: it is
  the **default-branch** version being re-anchored onto (taken from the diff's
  `theirs.version`), not the dev-branch config's version; a target version that is not newer
  is a 400. The client keeps the wire name (house style is wire fidelity) but the docstring
  must spell this out — and Part 2 should expose something explicit like `--onto-version`,
  not a bare `--version`.

### Feature gating and create-time guards

All six MR writes accept **either** `protected-default-branch` **or**
`branches-merge-requests`; the reads and `/diff` are ungated; `/rebase` alone requires
`branches-merge-requests` (`StorageRouteGuard::canAccessStorageScope`,
`Core/Storage/RouteGuard/StorageRouteGuard.php:158-180`). A failed feature check surfaces as a
**403 byte-for-byte indistinguishable from a role denial** — which is why the pre-flight check
belongs in Layer 2 (D9), not in error mapping here.

Create rejects a non-default target branch and a source branch that already has an MR — one MR
per source branch, ever; both guards are **404**, not 400 (`MergeRequestCreateAction.php:84-88`).
`reviewerIds` duplicates are de-duplicated server-side, `reason` on request-changes is capped at
1000 characters, `AutoMergeStrategy` is exactly `immediately` | `scheduled` | `none`. There is
**no cancel endpoint** — an MR is canceled only by deleting its source branch.

## Design decisions

**D1 — Explicit typed parameters, not payload dicts.** House style is named parameters with
presence detection inside the method — see `update_config` (`client/configs.py:352`, *"Only
provided (non-None) fields are sent"*).

Presence detection is the right idiom for `update_config`, which **patches**, and the wrong one
for `rebase_config`, which **replaces**: there, an omitted key is not "leave unchanged" but
"take the server-side default". `diff.configuration` defaults to `{}` and `diff.isDisabled` to
`false`, and an absent `diff.description` to null (`RebaseRequest::mapValidatedData`). So a
tri-state `is_disabled: bool | None` and optional `configuration` / `description` would make
silent data loss the signature's default — a caller resolving a conflict on a disabled config
and passing only `name` / `rows` would wipe the configuration body, drop the description and
re-enable the config, then merge that into production.

`ConfigurationRebaseService` settles which fields that covers: `$name` / `$description` /
`$configuration` / `$isDisabled` are *"the complete 3-way diff result"* and *"fully replace"* the
resolved version's body. All four are therefore **required** parameters, alongside `rows` (which
the backend rejects the request without). `description` is required-but-nullable — `None` is a
legitimate resolved value meaning "no description", and it omits the key rather than sending an
explicit null, which costs no expressiveness because the two are indistinguishable server-side.

`change_description` is the one genuine optional: it is not part of the replaced body, and null
selects a default rebase message rather than clearing anything.

The same reasoning applies to `_optional_mr_fields` in `client/merge_requests.py`, where
presence detection *is* correct (create and update genuinely patch): the helper is keyword-only,
because four of its five parameters are `str | None` and a positional transposition would
type-check cleanly and surface only as a backend 422.

**D2 — JSON bodies throughout, deviating from `configs.py`.** Per *Request bodies are JSON*:
`json=`, real nested objects, no `json.dumps`, no `"1"` / `"0"` booleans. Every method gets a
docstring line saying so, because the surrounding file teaches the opposite.

**D3 — `merge` awaits the job implicitly, like every Storage-job method in `client/`.** No
method in `client/` exposes a `wait` flag: `create_dev_branch`, `delete_dev_branch` and the
table import/export methods all call `_wait_for_storage_job` internally and return the finished
result. `merge_requests.merge(merge_request_id)` behaves identically — it awaits via the
`StorageRequester` Protocol's `wait_for_storage_job` (backed by `client/_core.py:160`: 1 s poll,
`STORAGE_JOB_MAX_WAIT` = 60 s cap, `STORAGE_JOB_FAILED` on a failed job,
`STORAGE_JOB_TIMEOUT` on expiry) and returns the completed job dict, whose `results` carry the
MR including its change log (`MergeDevBranchJob` returns
`getCreateMRResponseWithChangeLogArray`). An earlier draft proposed `wait: bool = False`; that
imported a Layer 1 Queue-API idiom (`job run --wait`) into a layer whose idiom is the opposite,
and fire-and-forget would make a failed merge (rollback to `approved`) indistinguishable from
success at the call site — the worst default for scripts. Should a real non-waiting need ever
appear, the MR state is pollable via `merge_requests.get()`. The wait budget is
`MERGE_JOB_MAX_WAIT` (600 s, following the `IMPORT_JOB_MAX_WAIT` / `EXPORT_JOB_MAX_WAIT`
precedent, not a flag) — shipped in Part 1 rather than waiting for E2E evidence, because
merging a many-config branch plausibly outlives the default 60 s storage-job budget and a
mid-merge `STORAGE_JOB_TIMEOUT` (with `retryable=True`) is actively misleading; this also
fixed the `StorageRequester` Protocol shape (`wait_for_storage_job(job, max_wait=...)`) before
the Protocol acquired external consumers.

One caveat the docstring must state: a successful merge always also deletes the source branch,
but that runs as a **second job enqueued by the first, with no job handle returned** — so the
await covers the merge outcome (changes published, or rolled back to `approved`), deliberately
not the branch deletion. After a successful return the changes are in production and the branch
is doomed but may still briefly exist; callers must not assume either way (see the notes doc,
*A successful merge deletes the source branch*). Rebase is synchronous and needs none of this.

**D4 — `diff` / `rebase` live in `client/configs.py`, not in the new module.** They are
configuration endpoints and sit next to `get_config_detail` and `update_config`; `client/` is
split by URL family (#520), not by feature.

**D5 — Their `branch_id` is required, breaking the house idiom on purpose.** Every other config
method takes `branch_id: int | None = None` with production fallback. Both endpoints carry
`isAvailableWithoutBranch: false` and answer 400 on the default branch, so both take
`branch_id: int` with no default — production becomes unrepresentable rather than a runtime
error. The docstrings state why.

**D6 — Keep and delete rebases are two methods, not one method with a `delete` flag.**
`rebase_config` takes `name` and `rows` as **required** parameters (matching
`validateDiffName` / `validateDiffRows` exactly) and always sends a populated `diff`;
`rebase_config_delete` takes only the four addressing arguments and sends
`{"version": N, "diff": {}}`. Python's signature does the validation — no runtime guard, no
illegal combination expressible. The cost, two methods for one endpoint, is a deliberate
documented exception; the envelope makes the split cheap (the bodies differ by one key's
content), and these two methods are the only place in kbagent that knows the envelope exists.

**D7 — Payload *semantics* stay in Layer 2.** The client sends what it is given. Whether a
resolved configuration is a sensible three-way merge, whether `version` came from
`theirs.version`, and whether the config is in the MR's conflict set are service concerns; D6
removes the only validation the client could usefully do.

**D8 — `merge_requests.update` and `external_id` are included.** kbagent's audience includes CI
pipelines and the SDK facade (`lib.py`), where "set auto-merge on an existing MR" is a
reasonable one-liner and a 255-char external reference correlates an MR with a ticket.
*Decided (2026-08-18):* `update` ships in Part 1 — Part 2 will ship the corresponding
`merge-request update` command, so the method is not dead code.

**D9 — No feature-flag plumbing at Layer 3; the pre-flight check is Layer 2's job.** Per
*Feature gating*, the reads aren't gated and a missing feature on a write is a 403 identical to
a role denial — no Layer 3 error mapping can produce the right message; only a client-side
pre-flight can. kbagent already has `has_feature(feature: str)` (`client/tokens.py:200`),
cache populated on every `verify_token`. Part 1 contributes only the flag name as a constant in
`constants.py` (`branches-merge-requests`); Part 2's service calls `has_feature()` before
writes and words the error. Checking specifically for `branches-merge-requests` also doubles as
the **SOX fence** — server-side either feature passes the gate, so without it a SOX project
would sail into a flow whose approvals semantics this RFC does not cover.

**D10 — The MR family is a namespace (`client.merge_requests.*`), not flat methods.** Flat
naming hits a wall no verb choice fixes: two endpoints have verbs starting with "request"
(`request_merge_request_review`) and the merge action degenerates into `merge_merge_request` —
the official PHP client resolved the same collision three different ways within one family. A
namespace removes the cause: the noun lives in the attribute, the verbs stay wire-faithful.
The flat shape was preserved in the #520 split only to avoid rewriting call sites; a brand-new
family has none, so this is the one moment a namespace costs nothing. In-repo precedent for the
shape: the SDK facade's `.files` (`lib.py:105`).

**The namespace depends on a transport Protocol, not on the client** — the structure mature
Python SDKs converge on (PyGithub's `Requester`, stripe-python's `_APIRequestor`). kbagent's
transport lives as protected methods on `_CoreClient`; extracting it for real is the
client-split RFC's job (`docs/client-split-rfc.md`, branch `martinsifra/requestor`, draft PR
[#595](https://github.com/keboola/cli/pull/595)). This RFC ships **first**, on the seam below;
the client split then builds under it:

```python
class StorageRequester(Protocol):
    """The FUTURE transport interface — public method names, defined today."""

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response: ...
    def wait_for_storage_job(self, job: dict[str, Any]) -> dict[str, Any]: ...


class _ClientRequester:
    """Temporary Adapter: satisfies the Protocol by delegating to the client.

    Dies the day a real transport object exists.
    """

    def __init__(self, client: _CoreClient) -> None:
        self._client = client

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return self._client._request(method, path, **kwargs)

    def wait_for_storage_job(self, job: dict[str, Any]) -> dict[str, Any]:
        return self._client._wait_for_storage_job(job)


class MergeRequests:
    """Merge-request endpoints, exposed as ``client.merge_requests``."""

    def __init__(self, requester: StorageRequester) -> None:  # never sees the client
        self._requester = requester

    def list(self) -> list[dict[str, Any]]:
        return self._requester.request("GET", "/v2/storage/merge-request").json()

    # ... the remaining eight methods, same shape


class _MergeRequestsMixin(_CoreClient):
    @functools.cached_property
    def merge_requests(self) -> MergeRequests:
        return MergeRequests(_ClientRequester(self))
```

Consequences:

- Cross-object protected access shrinks to the adapter's two one-line methods (unremarkable
  here — `_core.py:116` already reaches into a foreign instance's protected member, and the
  SLF ruff rule family is not selected).
- **A future transport refactor touches one line**: the real transport implements the Protocol
  structurally, the mixin's property passes it instead of `_ClientRequester(self)`, the adapter
  is deleted — `MergeRequests` and its tests stay byte-for-byte identical.
- The namespace is unit-testable against a stub requester, no HTTP mocking.
- Keep the Protocol **minimal** — grow it only when a method needs another transport
  capability; every method added is a promise the future transport must keep.

**Normative intent: new endpoint families are added as namespaces depending on
`StorageRequester`; existing flat families stay flat unless deliberately migrated.** The
Protocol and adapter live in `client/merge_requests.py` while they have one consumer; their
shared home (and the real transport) is the client-split RFC's to define — until then a second
family would move them to something like `client/_requester.py`. The mixed style inside
`KeboolaClient` (`client.list_dev_branches()` next to `client.merge_requests.list()`) is the
accepted, temporary cost.

## Method inventory

Returns are raw parsed JSON, as everywhere in `client/`.

### Merge requests — new file `client/merge_requests.py`, exposed as `client.merge_requests`

Per D10, the nine methods live on a namespace object, not as flat methods on `KeboolaClient`:

| Method | Endpoint |
|---|---|
| `merge_requests.list() -> list[dict]` | `GET /v2/storage/merge-request` |
| `merge_requests.get(merge_request_id, include_activity_log=False) -> dict` | `GET …/merge-request/{id}[?include=activityLog]` |
| `merge_requests.conflicts(merge_request_id) -> list[dict]` | `GET …/merge-request/{id}/conflicts` |
| `merge_requests.create(branch_from_id, branch_into_id, title, description=None, reviewer_ids=None, auto_merge_strategy=None, auto_merge_at=None, external_id=None) -> dict` | `POST /v2/storage/merge-request` |
| `merge_requests.update(merge_request_id, title=None, description=None, reviewer_ids=None, auto_merge_strategy=None, auto_merge_at=None, external_id=None) -> dict` | `PUT …/merge-request/{id}` (D8) |
| `merge_requests.request_review(merge_request_id) -> dict` | `PUT …/merge-request/{id}/request-review` |
| `merge_requests.approve(merge_request_id) -> dict` | `PUT …/merge-request/{id}/approve` |
| `merge_requests.request_changes(merge_request_id, reason=None) -> dict` | `PUT …/merge-request/{id}/request-changes` |
| `merge_requests.merge(merge_request_id) -> dict` | `PUT …/merge-request/{id}/merge` + awaits the job (D3) |

`list()` takes no arguments — the endpoint has no query parameters, so `--state` filtering
happens in the service. `request_changes` is deliberately not called `reject`: "reject" reads
terminal, while the transition sends the MR back to `development` to be revised (Part 2 should
carry the same care into the command name).

`client/merge_requests.py` holds four pieces (D10): the `StorageRequester` Protocol, the
`_ClientRequester` adapter, the `MergeRequests` namespace class, and a minimal mixin exposing
the namespace as a **`functools.cached_property`** returning
`MergeRequests(_ClientRequester(self))`, composed into `KeboolaClient` (`client/_client.py:28`)
like `_BranchesMixin`. `cached_property` rather than the house attr-initialized-to-None lazy
pattern because it needs no `__init__` change (the client defines no `__slots__`). Neither the
namespace nor the adapter owns an HTTP client, base URL, or token; the client↔namespace
reference cycle is harmless because resources are released by the explicit `close()`.

### Conflict resolution — added to `client/configs.py`

Branch-scoped; `branch_id` required (D5).

| Method | Endpoint |
|---|---|
| `get_config_diff(component_id, config_id, branch_id) -> dict` | `GET …/branch/{branch_id}/components/{c}/configs/{cfg}/diff` |
| `rebase_config(component_id, config_id, branch_id, version, name, rows, configuration, is_disabled, description, change_description=None) -> dict` | `POST …/rebase` (keep) |
| `rebase_config_delete(component_id, config_id, branch_id, version) -> dict` | `POST …/rebase` (delete) |

`get_config_diff` returns the three-way diff (`base` = dev branch v1, `ours` = dev head,
`theirs` = default head); each side may be null when the config does not exist there.
Flattening the nested `diff` payload is Layer 2's job.

The Python signatures are flat; only the body construction knows about the envelope.
`rebase_config` sends `version` at the top level and puts the five required content fields
(`name`, `rows`, `configuration`, `is_disabled`, `description`) plus `change_description` when
set inside `diff`. `description=None` is required-but-nullable: it omits the key, which is how
"the resolved config has no description" is expressed.
`rebase_config_delete` sends exactly `{"version": N, "diff": {}}`. Component and configuration
ids are `quote()`d, as everywhere in `configs.py`.

## Testing

New `tests/test_merge_request_client.py`, using `pytest_httpx` like `tests/test_ai_client.py`.

- **Path construction** — the invariant most likely broken by copying the `branch_id or
  production` idiom: every MR method hits a bare `/v2/storage/merge-request…` path even when
  the project has an active branch; `diff` / `rebase` always hit `/v2/storage/branch/{id}/…`.
- **JSON encoding** — the second-most-likely mistake, since the surrounding file does the
  opposite: assert `Content-Type: application/json`, real nested JSON (not strings), JSON
  numbers for `version` / `branchFromId` / `branchIntoId`, `true`/`false` booleans.
- **Presence detection** — `create` / `update` omit unset optionals; `rebase_config` sends
  `is_disabled=False` but omits `is_disabled=None`; `rows=[]` is sent, not treated as absent.
- **The `diff` envelope** — `version` at the top level, every content field inside `diff`,
  nothing content-like at the top level. Pin explicitly: a regression to the flat pre-envelope
  body would not fail loudly in a round-trip test, it would just build the wrong request.
- **Delete resolution** — `rebase_config_delete` sends exactly `{"version": N, "diff": {}}`,
  `diff` serialised as an empty JSON **object**, not `null`, `""` or `[]`.
- **Merge waiting** — `merge()` polls to a terminal state like every Storage-job method (there
  is no immediate-return mode); a failing job surfaces `STORAGE_JOB_FAILED` from the existing
  helper; success returns the completed job dict whose `results` carry the MR.
- **`include=activityLog`** is present only when asked for.
- **The seam holds** — one test constructs `MergeRequests` directly with a stub
  `StorageRequester` (no HTTP mocking) and asserts a call goes through it; this pins the
  namespace-never-touches-the-client property that makes the future transport swap free. The
  adapter's two pass-through methods need no dedicated tests — every wire test exercises them.

An E2E test is mandatory for the commands (convention #16) and lands in Part 2; Layer 3 alone
has no command to exercise. Everything above is verifiable offline, so no
`branches-merge-requests` project is needed to write or review this part.

## Non-goals for Part 1

All Part 2 material — including the derived status object and its approvals problem, error-code
additions for the merge 409, `mcp_parity.py` entries and the parity-canary sequencing,
post-merge cleanup, the fate of `kbagent branch merge`, and the UX facts already verified
against the backend — is recorded in
[`merge-requests-layer2-notes.md`](merge-requests-layer2-notes.md). Also out of scope: whether
`merge-request create` takes the source branch from `--branch` or `active_branch_id` (Layer 3
receives it explicitly); the SOX flow; auto-merge scheduling UX beyond passing fields through.

## Checks

`client/` has a file-size budget of 1500 soft / 2000 hard **code** lines
(`scripts/check_file_size.py:71-73`); `client/configs.py` is at 292 today, so both the new
module and the two added config methods are far inside it. No new `BaseHttpClient` subclass is
introduced — the Protocol, adapter and namespace own no HTTP client or token — so
`make check-sentinel-guards` is unaffected. **No `SESSION_UNSUPPORTED_FEATURES` entry is added**
— verified in Connection code, not just assumed: bearer session auth on Storage routes is
route-agnostic. `BearerTokenAuthenticator` matches every path under `/v2/storage`
(`Auth/Security/BearerTokenAuthenticator.php:58-63`) and resolves the session to the user's
**real admin Storage token** for the `X-KBC-ProjectId` project
(`authenticateForStorageRoute`, `BearerTokenAuthenticator.php:431-472`,
`findAdminTokenForProject`), so by the time an MR action runs, the request is
byte-for-byte indistinguishable from a static-token call — the MR routes need, and get, no
per-endpoint session support.

`make check` must be green before the PR.
