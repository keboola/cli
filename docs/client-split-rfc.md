# RFC: Split `KeboolaClient` into a transport and resource namespaces

**Status: DRAFT — problem statement & requirements only.** The design and investigation
sections follow once this scope is approved. No Linear issue exists; the deliverable of this
work is the PR carrying this RFC.

Related: `docs/merge-requests-rfc.md` (Part 1 of merge-request support). Its D10 introduced the
first resource namespace (`client.merge_requests.*`) and is the immediate trigger for this
document; per the sequencing requirement below, that RFC ships first and gains an adapter seam
so it migrates onto this architecture at near-zero cost.

## Problem

`KeboolaClient` is a God class. The `client/` package is 13 files and ~4,300 lines composing
**109 public methods into one flat namespace** via ten mixins — `list_dev_branches` sits next
to `upload_table`, `verify_token`, and `encrypt_values`, with nothing but naming prefixes to
group them.

That flatness has two compounding costs:

- **Orientation.** Finding the right method means scanning a 109-entry surface. Every method
  must carry its resource noun (`list_tables`, not `tables.list`), so discoverability lives in
  prefixes and grep instead of structure.
- **Naming degeneracy.** The noun-in-every-verb convention produces increasingly awkward names
  as families grow. The merge-request work made this acute: a flat surface forces
  `merge_merge_request`, and the two endpoints whose verbs start with "request" collide with
  the noun outright (`request_merge_request_review`). The official Keboola PHP client hit the
  same wall and resolved it three inconsistent ways within one family
  (`mergeRequestRequestReview`, `requestMergeRequestChanges`, `mergeRequestApprove`).

There is a structural cost underneath the cosmetic one: the endpoint families are welded to the
transport by **inheritance**. Every mixin extends `_CoreClient` → `BaseHttpClient`, so retry,
backoff, error mapping, token masking, the bearer-auth hook, and the management of five
sibling-host sub-clients (storage, queue, query, encryption, sync-actions) are all reachable —
and reached — as `self._*` from 109 methods. There is no seam at which the transport could be
tested, replaced, or reasoned about independently, and no interface a new endpoint family could
depend on other than "the whole client".

The first resource namespace (`client.merge_requests.*`, merge-requests RFC D10) had to work
around this: as a composed object rather than a mixin, it can only reach the transport through
another object's protected members.

## Target model

The architecture used by mature API SDKs — [PyGithub's
`Requester`](https://github.com/PyGithub/PyGithub/blob/main/github/Requester.py) and
[stripe-python's `_APIRequestor`](https://github.com/stripe/stripe-python/blob/master/stripe/_stripe_client.py):

1. **One transport object** ("requester") whose only job is HTTP: request execution with retry
   / backoff / error mapping / token masking / auth. Its methods are **public**; privacy is
   achieved by holding the object itself in a private attribute.
2. **Resource namespaces per endpoint family** (`client.tables.*`, `client.configs.*`,
   `client.merge_requests.*`, …) that receive the requester at construction and call its public
   interface. No inheritance from the transport, no protected-member access.
3. **`KeboolaClient` becomes a thin shell**: builds the requester, exposes the namespaces.

Illustrative sketch — shapes and names are settled in the design section, this only shows the
relationships (`Requester` plays the role of PyGithub's `Requester` / stripe's `_APIRequestor`;
`Tables` plays the role of a stripe service class):

```python
# client/_requester.py — the ONLY place HTTP lives
class Requester:
    """Executes requests with retry/backoff, KeboolaApiError mapping, token
    masking, and bearer/static auth — everything BaseHttpClient does today."""

    def __init__(self, base_url: str, token: str, *, http_auth: httpx.Auth | None = None): ...
    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response: ...
    def wait_for_storage_job(self, job: dict[str, Any]) -> dict[str, Any]: ...


# client/tables.py — one resource family, no inheritance
class Tables:
    def __init__(self, requester: Requester) -> None:
        self._requester = requester        # private *holding*, public *interface*

    def list(self, bucket_id: str | None = None, branch_id: int | None = None) -> list[dict]:
        prefix = f"/v2/storage/branch/{branch_id}" if branch_id else "/v2/storage"
        return self._requester.request("GET", f"{prefix}/tables").json()

    def detail(self, table_id: str, branch_id: int | None = None) -> dict: ...


# client/_client.py — the thin shell
class KeboolaClient:
    def __init__(self, stack_url: str, token: str, *, http_auth: httpx.Auth | None = None):
        self._requester = Requester(stack_url, token, http_auth=http_auth)

    @cached_property
    def tables(self) -> Tables:
        return Tables(self._requester)

    @cached_property
    def merge_requests(self) -> MergeRequests:     # the family born on the R4 seam
        return MergeRequests(self._requester)      # adapter deleted, nothing else changes
```

The R4 seam is the miniature of the same picture: until `Requester` exists, the merge-requests
RFC defines the *interface* of `request()` / `wait_for_storage_job()` as a `Protocol` and
satisfies it with a ~10-line adapter over today's client — so the last line above is the entire
migration for that family.

## Requirements (decided in the intake interview, 2026-08-17)

- **R1 — Both layers, one program.** The transport extraction is the foundation; the namespace
  API is the goal. Neither alone: namespaces without the transport just relocate the
  protected-member coupling; the transport without namespaces leaves the God-class surface.
- **R2 — Migration policy: internal now, aliases only where an external consumer exists.**
  Internal call sites — **544 calls to 154 distinct client methods** across `services/` and
  `sync/` — migrate to the namespace API as part of this work, family by family. Flat aliases
  are kept only on surfaces external consumers can reach: `Client.raw` is documented in
  `docs/sdk.md` §7 as an escape hatch (and, verbatim, a "less-stable surface"), but its
  examples teach `raw.list_buckets()` / `raw.list_tables()` and the storage TUI demo is built
  on four such calls, so those get a deprecation bridge rather than an overnight break. The
  removal milestone for the bridge is decided in the design section (precedent: the MCP tool
  group deprecation named `v0.85.0` and a date).
- **R3 — `KeboolaClient` only, for now.** The other six `BaseHttpClient` subclasses (manage,
  ai, data-science, metastore, dev-portal, stream) are out of scope: none is a God class, and
  extending the pattern across them deserves a team discussion once it has proven itself here.
- **R4 — Merge requests ship first, on a seam.** The merge-requests RFC is implementation-ready
  and does not wait for this refactor. Instead, its D10 is amended so the `MergeRequests`
  namespace depends on a small transport *interface* satisfied today by a ~10-line adapter over
  the existing client; when this refactor lands, the real requester implements the same
  interface, the adapter is deleted, and the namespace plus its tests are untouched. (Action
  item on `docs/merge-requests-rfc.md`, decided here.)
- **R5 — The typed SDK facade is untouched.** `from keboola_agent_cli import Client` — the
  semver-committed facade in `lib.py` (`query`, `run_job`, `.files`, typed result models) —
  keeps its exact surface. This refactor reorganizes the machinery underneath it; `.raw`
  remains the only place the change is visible to SDK users, governed by R2.
- **R6 — No wire-behavior change.** Retry/backoff parameters, error mapping to
  `KeboolaApiError` / `ErrorCode`, token masking, timeout handling, and the bearer-session auth
  hook behave identically before and after. This is a reorganization, not a behavior change;
  the test suite must be able to assert it.

## Non-goals

- The other six HTTP clients (R3) — future discussion, wider audience.
- Any change to the `lib.py` facade surface or the typed result models (R5).
- Any change to HTTP semantics, endpoints, or payloads (R6).
- New endpoint coverage of any kind — this RFC adds no capability, only structure.

## Open questions for the design phase

Deliberately not answered here; each needs investigation first.

1. **Family taxonomy.** Mirror the ten existing URL-family mixins 1:1 (`tables`, `files`,
   `configs`, `queue`/`jobs`, `tokens`, `branches`, `stream`, `query`, `workspaces`, `misc`) or
   redesign the grouping while we are at it? (`misc` in particular should not survive as a
   namespace name.)
2. **The requester's shape versus five hosts.** `KeboolaClient` today multiplexes storage,
   queue, query, encryption, and sync-actions hosts behind `_get_or_create_sub_client`. One
   requester with per-call host selection, or one per host?
3. **The transport interface.** What exactly do namespaces get — the full requester, or a
   narrow protocol (the merge-requests seam of R4 starts with `request()` +
   `wait_for_storage_job()`)? Grows-on-demand versus designed-up-front.
4. **CI coupling.** `scripts/check_sentinel_guards.py` inventories `BaseHttpClient` subclasses
   and their `SESSION_AUTH_FEATURE` declarations; the refactor moves that topology and the
   guard must learn the new one without losing its teeth.
5. **`.raw` bridge mechanics.** Delegating properties vs. generated alias methods vs.
   `__getattr__` forwarding; how deprecation warnings surface; the removal milestone.
6. **Migration order of families** and how to keep every intermediate commit green
   (`make check`, 154 distinct methods, ~30 service modules).
