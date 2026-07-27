# Review of #535 — Browser login via PKCE + device authorization (implementation)

PR: https://github.com/keboola/cli/pull/535
Reviewed head: `a21fc92dc0b8c71c45ad58bdd5fe15a4b9b9c3a8` (local working tree)
Pushed head at review time: `8a531c054b3bb29bf608b4a4ba9b5e041b1c3050` (3 commits behind local)
Scope: 74 files, ~12 544 insertions, 78 deletions

This document reviews the **shipped implementation**. The earlier review of the
same PR while it was docs-only is preserved in `review-pr-535.md`; its findings
B-1..B-4 / NB-1..NB-5 are re-verified against code in
[Prior review regression check](#prior-review-regression-check) below.

## Verdict

- **Verdict:** REQUEST CHANGES
- **Blocking findings:** 7
- **Non-blocking findings:** 15
- **Nits:** 14

The `auth/` package itself is high-quality code and survived six independent
review passes with no blocking finding against it. Every blocking finding lies
either at the **edges** — pre-existing services that do not know about the
session sentinel — or in the **formal ceremony** the repo mandates.

### Mechanical gates (all passing)

| Gate | Result |
|------|--------|
| `make check` | exit 0 |
| `uv run pytest tests/` | exit 0 |
| `make command-sync-check` | OK, all 255 commands registered + documented |
| Version consistency | `0.77.0` in `pyproject.toml:3`, `plugin.json:3`, `marketplace.json:13`, `changelog.py:27` |
| Error codes | 8 new codes, enum ↔ `docs/error-codes.md` ↔ `_ERROR_CODE_TO_TYPE` all match (74 ↔ 74) |
| File-size budgets | all under soft ceilings (largest: `services/auth_service.py` 946 / soft 1000) |
| Commit hygiene | 11 commits, conventional prefixes, one logical change each, zero `Co-Authored-By` / AI footers |

---

## The structural insight that explains two of the blocking findings

The sentinel guard is opt-in **on the wrong axis**. There are two distinct
channels by which a `kbc-session://` sentinel can be mishandled, and only one is
guarded:

- **Channel A — the sentinel is sent as a credential.**
  Guarded 14× via `auth/sentinel.py:require_static_token`, plus the
  `make_client_factory` bearer branch. This is the channel the PR was designed
  around, and it is well covered.
- **Channel B — the sentinel is silently *replaced* by a static token, or a
  session project is treated as a static-token project.**
  **Completely unguarded.** `ConfigStore.edit_project` (`config_store.py:667`)
  and `add_project` (`:583`) accept any `token` string with no inspection.

Blocking findings 1 and 2 are both channel B, reached through different
entry points. Critically: **grepping for a missing `require_static_token` call
would never have found either of them.**

`org_service.py` does not misuse channel A at all — it correctly received the
bearer-aware `make_client_factory` in this PR (`:78`). It misuses channel B,
where there is nothing to forget.

### Recommended fix — one chokepoint instead of two patches

```python
# config_store.py -- every config write already funnels through here
def edit_project(
    self, alias: str, *, allow_credential_type_change: bool = False, **kwargs
) -> None:
    token = kwargs.get("token")
    if token is not None and not allow_credential_type_change:
        existing = self.get_project(alias)
        if existing and is_session_token(existing.token) and not is_session_token(token):
            raise SessionAuthUnsupportedError(
                f"Replacing the browser-login session credential of project '{alias}' "
                "with a static Storage token",
                remedy="Run `kbagent auth logout --remove-projects`, then "
                       "`kbagent project add`, if that is what you want.",
            )
    ...
```

`project edit --token` passes `allow_credential_type_change=True` (explicit user
intent) and prints a warning. `org_service` does not pass it, and additionally
skips session projects at the selection layer so the reported outcome is
truthful rather than an exception:

```python
# org_service.py, in the :341 loop, beside the existing project_id skip
if is_session_token(project.token):
    projects_skipped.append({
        "alias": alias, "project_name": project.project_name,
        "reason": "Browser-login (session) project -- nothing to refresh; "
                  "session access tokens rotate automatically.",
    })
    continue
```

Estimated ~10 LOC in `config_store.py`, ~8 in `org_service.py`, ~1 in
`project_service.py`, ~20 of tests → **~40 LOC total**.

### Plus a CI gate, because "the 17th service forgets" is a CI problem

The repo already has `make check-error-codes` as precedent. Add
`make check-sentinel-guards` (~30 LOC): grep every file that reads
`project.token` / `ProjectConfig.token` outside `auth/` and `services/base.py`
and require it to also reference `is_session_token` or `require_static_token`.
`org_service.py:356` reads `project.token` and mentions neither — this check
catches it mechanically.

---

## Blocking findings

### `[B1]` `services/org_service.py:78,340-392,477-500` — `project refresh` / `org setup --refresh` silently converts a session project to a static token

`org_service.py` has **zero** sentinel awareness (the only match for
`sentinel` in the file is a docstring at `:35`), yet takes the bearer-aware
`make_client_factory` at `:78`.

Trigger path. `refresh_tokens()` builds `projects_to_check` from all projects
(`:309`); the `project_id is None` skip at `:343` does not catch session
projects (they do have a `project_id`, written by `auth_service.py:665`), so
they reach `:356`.

**A healthy session is accidentally safe** — `verify_token()` succeeds over
bearer, so `token_valid = True` and `:380` skips it. The hole opens on two
realistic paths:

- **`--force`** (`refresh_tokens(force=True)`, `:269`) bypasses the
  `token_valid` short-circuit entirely, converting even healthy session
  projects.
- **An expired or unreachable session** raises `SESSION_EXPIRED`, which is not
  `INVALID_TOKEN`, so it hits `else: raise` (`:364`) and is swallowed by the
  outer `except KeboolaApiError: token_valid = False` (`:367-368`) — refresh
  then proceeds *without* `--force`.

Either way `_refresh_single_project` mints a real static Storage token via the
Manage API (`:479`) and `edit_project(alias, token=storage_token)` (`:497`)
permanently overwrites the `kbc-session://` sentinel.

Why this is worse than an error: the project silently changes credential
*type*. `auth logout --remove-projects` filters on
`is_session_token(project.token)` (`auth_service.py:838`), so the converted
project is no longer recognized as session-owned and **survives logout as a
long-lived static credential**. The user asked to refresh tokens, not to
convert a browser-login session into a permanent one. It requires a super-admin
Manage token, so this is not unprivileged escalation — the blast radius is "an
admin runs `project refresh --all --force` and silently converts every session
project on the stack".

Fix: see the channel-B chokepoint above.

### `[B2]` `services/project_service.py:249-262`, `server/routers/projects.py:75` — `project edit --token` silently overwrites the sentinel and breaks cleanup

Neither file contains a single occurrence of `is_session_token` or `sentinel`
(verified by grep). `edit_project` verifies the new static token and writes it
over the sentinel with no warning.

The consequence is worse than B1's: because `auth logout --remove-projects`
filters on `is_session_token(project.token)` (`auth_service.py:838`), once the
sentinel is overwritten that alias **can never be cleaned up by logout** —
leaving an orphaned session in `auth.json` and a static-token project in
`config.json`. Also reachable over HTTP via `server/routers/projects.py:75`.

Converting a session alias to static may be a legitimate user request, so this
one wants an explicit opt-in plus a warning (`allow_credential_type_change=True`),
not a hard failure.

### `[B3]` `Makefile:29-43` — `tests/test_e2e_auth.py` is in no Makefile target

`grep -c auth Makefile` returns **0**. The 404-line file
(`TestBearerCapabilityMatrix`, `TestBearerReactiveRefresh`,
`TestBearerManageApi`) runs nowhere: `make test` excludes it via
`@pytest.mark.e2e`, and `make test-e2e` selects only `test_e2e.py`,
`test_server_semantic_layer_routes_e2e.py` and three `-k` filters. The file's
own docstring admits this at `:44-47`.

So the bearer auth path — the hot path this PR introduces — has **zero
automated verification in any target a human or CI invokes**.
CONTRIBUTING:377 requires E2E coverage per command and names
`make test-e2e` as the verification command.

Fix, following the existing per-feature pattern at `Makefile:36-43`:

```make
test-e2e-auth: ## Run programmatic-auth E2E (E2E_URL + E2E_SESSION_REFRESH_TOKEN + E2E_SESSION_PROJECT_ID required)
	uv run pytest tests/test_e2e_auth.py -v -s --tb=long
```

plus an `e2e_auth` marker in `pyproject.toml:92-96` alongside `e2e_invite`.
The file's own skip guards (`:93,109`) make inclusion in the default
`test-e2e` list safe, and inclusion is what makes `make test-e2e` a real gate.

Note `register-projects` genuinely does have E2E coverage
(`tests/test_e2e.py:12501-12680`, gated at `:126-146`); `login` / `status` /
`logout` do not, and their only E2E artifact is a `@pytest.mark.skip`ped
manual runbook (`test_e2e_auth.py:361-396`) — which is honest, but not
coverage.

### `[B4]` `errors.py:239`, `services/base.py:236-253`, `data_app_service.py:380-428`, `mcp_service.py:894-932` — the sentinel guard loses `AUTH_NOT_SUPPORTED_ON_STACK` in multi-project paths

`SessionAuthUnsupportedError` subclasses `ConfigError` (`errors.py:239`), not
`KeboolaApiError`. Per-project workers narrow their catch to
`except KeboolaApiError`, so the guard's exception is never caught there and
propagates to the blanket `except Exception` in `_run_parallel`
(`base.py:239`), which relabels it `UNEXPECTED_ERROR`. `mcp_service.py:927`
hardcodes `MCP_ERROR`.

Two confirmed instances:

1. `data_app_service.py:380` calls the guarded factory **before** the `try:` at
   `:382`, whose only handler is `except KeboolaApiError` (`:420`).
2. `mcp_service.py:894-932` — `list_tools()` calls `_build_server_params`
   (`:267` → `require_static_token` at `:238`) inside a bare
   `except Exception as exc:` (`:923`) that hardcodes the error code (`:927`).

Consequence: a `--json` consumer — i.e. the AI agents this CLI exists for —
cannot branch on `error_code == "AUTH_NOT_SUPPORTED_ON_STACK"` to
auto-remediate, in exactly the scenario `auth register-projects` creates (a
config mixing static-token and session projects). The human-readable message
survives, so this is misclassification rather than a fully silent failure, but
it breaks the contract `CLAUDE.md` documents.

Not covered by tests: `tests/test_auth_sentinel_guards.py` exercises the guards
in isolation, never through `list_data_apps` / `list_tools` with a mixed
project set.

Fix: in every `_run_parallel`-based worker that constructs a guarded secondary
client, catch `(KeboolaApiError, ConfigError)`, preserve
`getattr(exc, "error_code", "UNEXPECTED_ERROR")` instead of hardcoding, and
move the guarded factory call inside the widened `try`. Audit
`semantic_layer_service.py` / `stream_service.py` / `flow_service.py` for the
same shape. (`component_service.py:635` is fine — outside `_run_parallel`.)

### `[B5]` `auth/token_provider.py:165-206`, `auth/state_store.py:89`, `constants.py:54-58,577` — the lock hold time can exceed the lock acquire timeout during normal operation

`_refresh_locked` deliberately holds the cross-process `auth.json.lock` flock
across the network refresh call. That call goes through
`BaseHttpClient._do_request`'s retry loop: worst case
`MAX_RETRIES=3` × `read=30.0` + backoff 1 s + 2 s ≈ **93 s**
(verified against `constants.py:54-58`). Meanwhile every other caller acquires
the same lock with `AUTH_LOCK_TIMEOUT = 30.0` (`constants.py:577`) — including a
read-only `get_session()` for `auth status`.

Result: during exactly the degraded-auth-service window where the retry loop
matters, every other concurrent kbagent process or thread touching `auth.json`
fails after 30 s with
`ConfigError("Could not acquire lock... Another kbagent process may be stuck
holding it.")` — which is false; the holder is merely slow. `ConfigError` maps
to **exit 5**, not the auth-specific 3 or network 4, so scripts branching on
exit codes get a misleading signal. This affects **any** command against a
session project (via `BearerAuth` / `make_client_factory`), not just `auth *`.

Preferred fix: give the refresh `AuthClient` a short, single-attempt timeout — a
90-second refresh is useless anyway — rather than raising `AUTH_LOCK_TIMEOUT`
above 93 s.

### `[B6]` `server/dependencies.py:62-68` vs `CLAUDE.md`, `docs/programmatic-auth-login-plan.md:46`, `errors.py:239` — the v1 scope of `kbagent serve` is self-contradictory

`src/keboola_agent_cli/server/` contains no sentinel guard at all (verified:
zero matches for `AUTH_NOT_SUPPORTED_ON_STACK` / `require_static_token` /
`is_session_token`). `dependencies.py:62-68` documents this as **deliberate** —
serve inherits bearer support by delegating to already-guarded services, so
session projects work through serve for Storage and Manage.

Three places say the opposite:

- `CLAUDE.md` auth block: "`serve` ... fail fast on a sentinel-token project
  (`AUTH_NOT_SUPPORTED_ON_STACK`) naming the static-token fallback"
- `docs/programmatic-auth-login-plan.md:46`
- the `SessionAuthUnsupportedError` docstring itself (`errors.py:239`), which
  lists `kbagent serve` among consumers that "must fail fast here"

**This needs a product decision, not a code fix chosen by a reviewer.** On the
merits: a long-running daemon performing refresh-token rotation on behalf of
remote REST callers is a materially different threat model from a CLI
invocation, and it was not part of what was designed or reviewed. Whichever way
it goes, the losing side must be corrected.

### `[B7]` PR metadata — the description describes a PR that no longer exists

The body still says *"This PR is docs-only (plan review)"* and
*"Document: `docs/programmatic-auth-login-plan.md`"* for what is now 74 files
and ~12.5k lines of shipped implementation. It also:

- omits `auth register-projects` from the command list entirely;
- does not document the `serve` router skip, which CONTRIBUTING:333 requires as
  an explicit one-line reason ("Document any skip in the PR description ... so
  reviewers don't flag it"). Skipping is defensible on the merits — `login` needs
  a loopback browser redirect and `register-projects`' default mode is a
  terminal picker — but `auth status` is arguably *not* terminal-only and would
  map cleanly to a `GET`; say whether it is deferred;
- does not list the plugin/doc files touched, which the release checklist
  step 11 requires. There are eight: `keboola-expert.md`, `SKILL.md`,
  `commands-reference.md`, `gotchas.md`, new `auth-workflow.md`, `context.py`,
  `CLAUDE.md`, `docs/error-codes.md`.

Additionally the PR is still a **draft**, and local HEAD is **3 commits ahead**
of the pushed head (`e2e0161`, `b49c357`, `a21fc92`) — so the green CI applies
to `8a531c0`, not to this content.

The PR **title** is accurate and needs no change.

---

## The static-token / session split

Both credential modes coexist per **project** — not per stack, and not as a
global mode. The credential type is a property of each `config.json` project
entry's `token` field:

- a real token string → the static path, `X-StorageApi-Token`, byte-identical to
  pre-0.77.0 behaviour;
- `kbc-session://{project_id}` → the bearer path; the live credential is read
  from `auth.json` keyed by stack URL and rotates over time.

`make_client_factory` (`services/base.py:90-130`) is the single branch point, so
one config can freely mix static and session projects and each command resolves
per project.

**There is no fallback from session to static, and one is not constructible.** A
session-registered project has no static token stored anywhere — the sentinel
*replaced* that field. `require_static_token` (`auth/sentinel.py:49-56`) is
therefore not "try bearer, else static"; it is an unconditional fail.

| Situation | Behaviour |
|---|---|
| Static project, no session at all | Exactly as before; none of the new code runs |
| Session project + Storage / Manage (and `serve`, under D1) | Bearer, including refresh rotation and the single 401 retry |
| Session project + kai, AI service, data-science, metastore, dev-portal, stream, MCP, importable SDK | Hard `AUTH_NOT_SUPPORTED_ON_STACK` — no fallback, no degraded mode |

For `kai` the guard fires before the client is constructed
(`kai_service.py:91`), so the failure is immediate and typed rather than an
opaque 401 from the service.

### Transparency assessment

The **moment of failure** is handled well: the error is immediate, names the
specific feature (`kbagent kai`), and proposes a remedy
(`errors.py:250-260`). Everything around that moment is weak:

- nothing labels a project as session-backed in any human or JSON output
  (`[N13]`);
- the remedy text names a command that fails (`[N14]`);
- the restriction is only ever discovered reactively, at first use (`[N15]`);
- and in multi-project commands even the machine-readable error code is lost
  (`[B4]`).

This section is the raw material for the `docs/auth.md` page that decision D8
requires.

## Non-blocking findings

### `[N1]` `commands/auth.py:108,120,139-144,150,168-169`, `commands/_auth_picker.py:139-144,224,254,272` — server-controlled strings rendered as unescaped Rich markup

`AuthProject.name` / `.role` (from `IntrospectResponse`, `extra="allow"`, no
content validation — `auth/models.py:121-128`), `AuthUser.name` / `.email`, and
server error text via `RevokeResult.message` / `warnings` are interpolated
directly into f-strings passed to `Console.print(...)` and `Table.add_row(...)`.
`OutputFormatter`'s Console is built with default `markup=True`
(`output.py:34-38`), so `[tag]` / `[link=...]` sequences are parsed and
rendered.

`project.name` is settable by anyone with rename rights on a shared project, so
a `[link=https://phish.example]...[/link]` project name renders in another
admin's terminal as a clickable, deceptively-labelled OSC-8 hyperlink during
their own `auth status` / `register-projects` run. Cross-user, not
self-inflicted.

This class is already solved in this codebase — `commands/config.py` imports
`rich.markup.escape` and wraps every server-supplied string
(`config.py:397,1521,2006-2007`). The new code does not follow that precedent.

`_checkbox_select.py` is **not** affected — `prompt_toolkit`'s
`FormattedTextControl` takes `(style, text)` tuples and does not parse embedded
markup.

Fix: `rich.markup.escape()` on `project["name"]` / `role` in both "Accessible
projects" tables, `candidate.project_name` in the picker table/prompts/label,
`result.user_name` / `user_email` in the login and status panels, and any string
originating from `AuthClient._extract_error_message`.

Explicitly **not** flagged: `OutputFormatter.error()` (`output.py:104`) has the
same unescaped-interpolation shape but is pre-existing and codebase-wide, not
introduced or touched here.

### `[N2]` `models.py:10-45` — `normalize_stack_url` does not lowercase the host

Returns `f"https://{parsed.netloc}"` with no `.lower()`. Hostnames are
case-insensitive, but `auth.json`'s `sessions` dict is keyed directly on this
string (`state_store.py:198-227`), as is the process-wide provider registry key
(`token_provider.py:289`). So `auth login --stack Connection.Keboola.Com`
followed by `auth status --stack connection.keboola.com` resolves to two
different keys: status reports "missing" for a live session, or a second login
mints a redundant session for the same physical stack. Fix: `.lower()` the
netloc.

### `[N3]` `commands/_helpers.py:86-169` — the new Manage credential abstraction is dead code

`ManageCredential`, `resolve_manage_credential`, and
`make_manage_client_factory` have **zero production callers and zero tests**.
All ~17 Manage call sites still use the unchanged `resolve_manage_token`
(`feature.py` ×7, `org.py:220`, `project.py:496,965,1018,1047,1090,1134,1174`,
`data_app.py:612`); grepping `tests/` for the three new names yields only a
docstring mention at `tests/test_e2e_auth.py:333`. The bearer branch
(`:155-164`) is therefore unverified.

The docstrings admit this ("No v1 command selects the bearer path yet"). So the
prior review's B-3 incompatibility never materialized — because nothing
changed — but new security-relevant code shipped uncalled and untested. Either
unit-test both branches of each function, or drop the seam until a non-admin
Manage command needs it.

Positive corollary: `kbagent feature *` is safe. A session-only user gets the
hidden manage-token prompt (`_helpers.py:75`) or, non-TTY, exit 2 with an
actionable message (`:77-83`) — **never a raw 403**, and no silent PA-session
substitution is possible.

### `[N4]` `services/base.py:14,100-104`, `auth/__init__.py:46` — the lazy-import docstring claim is false

`services/base.py:100-104` claims the lazy import inside the returned closure
keeps `filelock` and the httpx machinery off the static-token startup path. It
does not. The **module-level** `from ..auth.sentinel import ...` at `:14`
executes `auth/__init__.py`, whose `:46` imports `state_store` → `filelock`.

Verified empirically:

```
$ python -c "import keboola_agent_cli.services.base; ..."
filelock                               LOADED
keboola_agent_cli.auth.state_store     LOADED
keboola_agent_cli.auth.token_provider  lazy
```

Only `token_provider` stays lazy. Same pattern in `commands/_helpers.py:19` and
`lib.py:44`.

Fix: delete the re-export block in `auth/__init__.py` — **verified unused**; the
only imports from the auth package root are submodule imports in tests
(`from keboola_agent_cli.auth import pkce` / `environment`), and
`server/app.py:32`'s `from .auth import ...` is a different module
(`server/auth.py`). Alternatively relocate the three sentinel functions to
`errors.py`, next to the `SessionAuthUnsupportedError` they already depend on —
which also unblocks `[N5]`.

### `[N5]` Channel-A consolidation opportunity — `client/stream.py:30` is the standing evidence

`client/stream.py:30` builds `StreamClient(stack_url=..., token=self._token)`
with no guard **and no `http_auth` propagation**, in direct contrast to
`client/_core.py:114-119`, which propagates `auth=self._http_auth` to the
queue / query / encryption / sync-actions sub-clients with a comment saying
exactly why ("don't silently fall back to no auth when the main client is
running in session (bearer) mode"). Under a bearer `KeboolaClient`,
`self._token` is `""` → empty `X-StorageApi-Token` → opaque 401 instead of the
fail-fast message. Note `StreamClient.__init__` (`stream_client.py:95`) has no
`http_auth` parameter to forward.

Currently unreachable — the CLI `stream` group uses `stream_service`'s own
guarded factory, and `lib.py:252` is guarded — so this is latent, not live. But
it demonstrates that "14 places currently remember" is not the same as "it
cannot be forgotten".

Structural fix (~14 LOC, and it *shrinks* the diff by removing 8 guard calls
and ~40 LOC of duplicated docstrings), prerequisite `[N4]`:

```python
class BaseHttpClient:
    # None = this client supports bearer sessions (KeboolaClient / ManageClient /
    # AuthClient) and must NOT be guarded.
    SESSION_AUTH_FEATURE: str | None = None

    def __init__(self, base_url, token, headers, timeout=None, *, http_auth=None):
        if http_auth is None and self.SESSION_AUTH_FEATURE is not None:
            require_static_token(token, feature=self.SESSION_AUTH_FEATURE)
```

plus one line on each of `AiServiceClient`, `DataScienceClient`,
`MetastoreClient`, `SchedulerClient`, `StreamClient`,
`DeveloperPortalClient`.

### `[N6]` `commands/auth.py:458-517` — business logic leaked into the command layer

`auth_register_projects` holds ~55 lines of selection-mode orchestration: which
selector wins, **whether to introspect at all** (`--all` calls
`list_project_candidates`, `--project-id` deliberately does not, `:482-488`),
and assembling the `ProjectSelection` list from `alias_overrides`. The
mutual-exclusivity check (`:458`) and the TTY/`--json` check (`:494`) are
legitimately command-layer (usage error, terminal capability); the rest is
business logic.

Fix: `AuthService.register_projects(*, stack, select_all=False,
project_ids=None, alias_overrides=None, selections=None)`. The interactive
branch stays in the command — the picker needs the candidate list in hand.

`_run_post_login_hook` (`:300-348`) is acceptable despite being a flow: it is
confirm → fetch → picker → register → render, i.e. interactive I/O interleaved
with service calls, which cannot live in a service that must not import
`typer`. `_auth_picker.py` is compliant — terminal I/O only, with authoritative
collision checking correctly located in `AuthService._apply_selections:645` and
its own checks documented as "advisory UX only" (`:213-221`).

### `[N7]` `services/auth_service.py:862,881` — two new bare `tuple[...]` returns

`_try_get_live_access_token -> tuple[str | None, str]` returns
`(token, reason)`; `_retry_orphans -> tuple[list[str], list[str]]` returns
`(revoked, remaining)` — two same-typed lists distinguished only by position.
Both unpacked positionally at `:811` and `:813`.

CONTRIBUTING is explicit and calls this non-negotiable: multi-value returns use
a `@dataclass`, and "No new `tuple[...]` returns". Fix: two frozen dataclasses,
~12 LOC. (`commands/_helpers.py:342`'s tuple return is pre-existing and
untouched — grandfathered.)

Dataclass discipline is otherwise good throughout the new code:
`DevicePollResult`, `RevokeResult`, `DeviceFlowOutcome`, `PkceChallenge`,
`LoopbackCallback`, `BrowserEnvironment`, `ManageCredential`,
`ResolvedProjectCredentials`, and all six `auth_service` result types.

### `[N8]` `permissions.py:20` — `auth.logout` is `write`, but `--remove-projects` deletes config entries

`auth logout --remove-projects` removes project entries via
`remove_project(alias)` (`auth_service.py:835-842`) — the same observable effect
as `project remove`, registered `admin` (`permissions.py:29`).
`auth register-projects` writes project entries; `project.add` is `admin`
(`:27`). Both new operations are `write` (`:20,25`).

Consequence: a policy denying `cli:admin` to keep an agent out of the project
registry still lets it register and de-register projects through `auth`.

The authors reasoned about this at `:22-25` ("not the admin class `project add`
uses for a pasted static token") — fair for `register-projects`, since no
credential is pasted; weaker for `logout --remove-projects`, which is a
deletion. Also note the comment at `:20` says "same risk class as `project add`"
while assigning `write`, which contradicts itself.

### `[N9]` `plugins/kbagent/skills/kbagent/SKILL.md:13-18` — trigger keywords not updated

`:13` adds "browser login" to the *Covers* prose, but the `Triggers:` list has
no `auth`, `login`, `sign in`, or `browser login` token. CONTRIBUTING:366 asks
specifically for the *trigger* keywords when a new topic area lands, because
that is what drives description-matching auto-invocation.

### `[N10]` `plugins/kbagent/skills/kbagent/references/auth-workflow.md` — predates `auth register-projects`

Otherwise the strongest artifact in the PR: the human-only warning is the first
section (`:11-30`), the five `auth status` states are tabulated (`:91-99`), the
v1-scope fail-fast list is complete (`:101-124`). But it documents only the
`--register-projects` **flag** — step 2 of "The loop" (`:76-78`) tells the
reader to `jq` over `project list`, and the standalone
`kbagent auth register-projects` command and the arrow-key picker appear nowhere
in 162 lines. Commit `e2e0161` swept five hand-maintained surfaces for the
picker change and missed this sixth one.

### `[N11]` `README.md:167-169` — no user-facing documentation

Setup still opens with "Three ways to register projects, depending on what you
have" and lists static token / `org setup --project-ids` / `org setup --org-id`.
Browser login is a fourth and, for an interactive human, likely the first to
try. `docs/TUTORIAL.md` and `docs/guide.md` have zero `auth login` hits. The
repo pattern for a major surface is a dedicated user-facing page
(`docs/sdk.md`, `docs/web-server.md`); the only auth document shipped is the
internal 620-line design plan.

Minimum: a fourth Setup bullet with the human-required caveat. Better:
`docs/auth.md` linked from `README.md:258`.

### `[N12]` `services/base.py:150`, `snapshot_service.py:41`, `token_service.py:104`, `org_service.py:78` — the "single central seam" is four places

The zero-churn claim holds functionally — no bypassed call site was found, and
all ~150 factory call sites are genuinely untouched — but the seam is **four**
places. `SnapshotService`, `TokenService`, and `OrgService` do not inherit
`BaseService` and each separately writes
`client_factory or make_client_factory(config_store)`; all three had to be
edited in this PR. This is the same opt-in-per-service weakness that produced
`[B1]`.

All three already take exactly `(config_store, client_factory)` and already use
the extracted `resolve_project_credentials` (`base.py:39`), so making them
`BaseService` subclasses collapses 4 → 1 at low risk. Related:
`default_snapshot_client_factory` / `default_token_client_factory` /
`default_storage_client_factory` are now pure one-line pass-throughs to
`default_client_factory` (~30 LOC of docstring for zero behaviour) — keep only
if a test injects them by name.

### `[N13]` `commands/project.py:64,76,92-97`, `services/project_service.py:630-641` — nothing tells the user which projects are session-backed

`project list` has a `Token` column rendering `mask_token(project.token)`. The
two credential types are distinguishable only *by accident*, and cryptically:

```
mask_token('kbc-session://9840')       -> 'kbc-...9840'
mask_token('kbc_at_realtokenvalue...') -> '***'
```

`kbc-...9840` reads like a truncated real token. There is no `Auth` /
`Credential` column, and `project status` (`:92-97`: Alias, Status, Response
Time, Project Name, Stack URL, Branch) does not surface the credential type at
all.

`--json` is no better: `list_projects` (`project_service.py:630-641`) returns
`"token": mask_token(project.token)` and **no** explicit credential-type field,
so a programmatic consumer — including the AI agents this CLI targets — has to
infer it from a masked prefix, which masking makes unreliable.

Given that the whole point of the sentinel design is that the two modes coexist
in one config, "which mode is this project in" is a first-class question the CLI
currently cannot answer.

Fix: an `Auth` column (`session` / `static`) in `project list` and
`project status`, and an explicit field (e.g. `auth_mode`) in the `--json`
payload so nobody parses a masked token.

### `[N14]` `errors.py:250-256` — the remedy text names a command that fails

`SessionAuthUnsupportedError`'s message ends:

> Register the project with a static Storage token instead:
> `kbagent project add --project <alias> --url <stack> --token <token>.`

The natural reading is to substitute the alias the user was just working with —
but that alias is already taken by the session project, so `add_project`
(`config_store.py:595-598`) rejects it:

```
Project '<alias>' already exists. Use 'project edit' to modify it.
```

So the suggested recovery path dead-ends on the first try. Under decision D6 the
correct command is `kbagent project edit --project <alias> --token <token>`
(warn-and-allow). Fix the remedy text to name that, and mention `project add`
only for the genuinely-new-alias case.

### `[N15]` `commands/auth.py:300-348`, `services/auth_service.py` — the v1 restrictions are never disclosed up front

The set of command groups that do not work on session projects is documented in
the plugin skill (`references/auth-workflow.md:101-124`) and in
`docs/error-codes.md`, but the CLI itself never says it at the moment it
matters: `auth login` / `auth register-projects` register projects and print a
success table without mentioning that `kai`, the AI service, data-science,
metastore, dev-portal, stream, MCP and the importable SDK will refuse them.

The user therefore discovers each restriction reactively, one failed command at
a time, potentially days later. Fix: after a successful registration, print a
short "not available on session projects" list (human mode) and include the same
list as a field in `--json`. This is the single cheapest transparency
improvement in this group, because it front-loads what `[N13]` and `[N14]` only
help with after the fact.

---

## Nits

1. `plugins/kbagent/agents/keboola-expert.md` is **61 305 B** against the 60 KiB
   (61 440 B) hard cap — 135 B of headroom. The next contributor adding one
   matrix row breaks the budget. CONTRIBUTING:364 says trim stale content, not
   raise the cap. There is no CI enforcement of this budget.
2. `services/auth_service.py` is 946 lines vs a 1 000-line soft ceiling (94 %).
   Under budget, so not a finding — but the natural split for the *next* PR is
   the alias/candidate logic (`:410-676`, ~265 LOC) into
   `services/_auth_registration.py`. Worth mentioning in the description so the
   reviewer knows it was measured.
3. `commands/auth.py` login docstring ends "see `_run_post_login_hook`" — a
   private symbol that renders verbatim in `kbagent auth login --help`. Point at
   `kbagent auth register-projects --help` instead.
4. `tests/test_e2e.py:12513` cites `tests/test_auth_command.py`, which does not
   exist. The file is `tests/test_cli_auth.py`.
5. `tests/test_token_provider.py:409-427` — `test_ten_threads_on_an_expired_
   cache_trigger_exactly_one_refresh` claims to prove the per-provider
   `threading.Lock` is load-bearing. Empirically it is not: with
   `get_access_token` monkeypatched to skip `with self._lock:` entirely, the
   10-thread race still yields exactly one refresh in every trial, because the
   flock-based re-read-and-adopt in `_refresh_locked` serializes threads at the
   OS level too. A regression dropping `self._lock` would ship past this test.
   Not a production bug — the lock still usefully reduces flock contention —
   but the test does not isolate what its docstring claims.
6. `tests/test_token_provider.py:472,630` — `skipif` on
   `get_start_method() == "fork" and os.name == "nt"` can never be true.
   Harmless (tests always run) but vacuous; it would error rather than skip if
   spawn were genuinely unavailable.
7. `services/auth_service.py:240-241` — `notice = on_notice or (lambda _message: None)`
   and `prompt = on_device_prompt or (lambda _authorization: None)` are assigned
   lambdas, which the "Named functions over throwaway lambdas" rule covers by
   the letter. Two module-level `def _noop_*` stubs. (The lambda at
   `_checkbox_select.py:215` is a permitted single-expression throwaway.)
8. `auth/pkce.py:239-244` — `PkceCallbackServer.__init__` binds the socket
   (`:241`) then starts the thread (`:242-243`). If `Thread.start()` raises
   (`RuntimeError: can't start new thread`), `__exit__` never runs and the bound
   listener leaks. Wrap `:242-243` in `try/except: self._httpd.server_close(); raise`.
   (`close()` at `:291` is correctly idempotent, and `auth_service.py:390`
   correctly uses `with`.)
9. `services/auth_service.py:253,359,805,854` — `login()` / `logout()` use
   manual `try/finally: client.close()` while `AuthClient` defines
   `__enter__`/`__exit__` (`auth_client.py:123-127`) and
   `token_provider.py:146,210` uses `with`. Functionally correct, but the repo
   rule mandates `with` and the PR is inconsistent with itself. In `logout`,
   keep `reset_provider_registry()` in its own `try/finally` around the `with`.
10. `auth/environment.py:81,91,106` — `wslview --version` is spawned up to **3×**
    per login on WSL (`_is_wsl_without_working_opener`, `_detect_opener`, then
    `_has_no_opener` → `_detect_opener` again). At
    `_WSLVIEW_PROBE_TIMEOUT_SECONDS = 2.0` that is up to 6 s on the login hot
    path. `@functools.cache`, or compute `opener` once and pass it in.
11. `plugins/kbagent/.claude-plugin/CLAUDE.md:83-91` — "when NOT to delegate"
    lists three cases; "user asks to log in / set up auth" is a natural fourth,
    since the right behaviour is handing the command back to the human.
    `keboola-expert.md:162` already encodes it for the subagent, so this is
    defense-in-depth, not a hole. `plugins/kbagent/commands/keboola.md` needs no
    change — the `/keboola` UX genuinely did not change.
12. `lib.py:252` now raises `SessionAuthUnsupportedError`, which is not in
    `__all__`, so SDK consumers can only catch it as `ConfigError`. Worth one
    line in `docs/sdk.md` even though CONTRIBUTING does not force it.
    (`__init__.py` has a zero-line diff, so there is no semver act here.)
13. Untracked `.cache/` and `review-pr-535.md` are not covered by
    `.gitignore` (`git check-ignore` exits 1 for both) — risk of accidentally
    committing. This file adds a third.
14. `commands/project.py:76` passes `p["token"]` to `Table.add_row` without
    `escape()`, while every other field in the same call *is* escaped
    (`:73-75,77`). The only injection vector is the user's own `config.json`, so
    this is an inconsistency rather than a vulnerability — but the file is being
    touched anyway for `[N13]`, so fix it in the same pass.

Also cosmetic: the branch name `docs/programmatic-auth-login-plan` no longer
matches the content. CONTRIBUTING mandates nothing about branch naming, and
renaming is not worth losing the PR.

---

## Prior review regression check

Findings from `review-pr-535.md`, re-verified against the shipped code.

| Finding | Status | Evidence |
|---|---|---|
| B-1 re-login leaves the previous server session active | **RESOLVED** | `auth_service.py:304-323` reads `previous` before writing, `put_session` at `:307` (durable-first), then `revoke(previous.refresh_token, token_type_hint="refreshToken")` at `:313`; unconfirmed revoke records an orphan (`state_store.py:229-244`). `logout` (`:805-832`) obtains a live access token first, retries orphans via `AuthClient.delete_session` (`auth_client.py:365-401`), then revokes its own token. Tests `test_auth_service.py:458,477,494,842,872` |
| B-2 wrong token revocation contract | **RESOLVED** | `auth_client.py:335` — `revoke(token, *, token_type_hint="refreshToken")`, public endpoint, token in the JSON body; `RevokeResult` distinguishes confirmed from unconfirmed; local state always deleted with `remote_revoked=False` surfaced distinctly |
| B-3 Manage credential path incompatible | **PARTIAL** | `resolve_manage_token` is unchanged and still returns `str`; all 7 `feature.py` call sites still use it, so **never a raw 403**. But the new abstraction is dead code — see `[N3]` |
| B-4 refresh not cross-process safe on Windows | **RESOLVED** | `pyproject.toml:25` `filelock>=3.13`, `uv.lock` 3.29.4; `state_store.py:56,78-99`; `filelock._windows` uses `msvcrt.locking`. The cross-process tests **do spawn real OS processes** (`multiprocessing.get_context("spawn")`, `test_token_provider.py:495,523,544,662`), including the delayed-stale-writer case at `:658-707`. The `_PidTaggedAuthClient` subclassing in `df525ed` is a `ty` accommodation, not fakery |
| NB-1 verify `kbc_at_*` per service + capability matrix | **PARTIAL** | `test_e2e_auth.py:192-255,263-319,330-347` covers it, but the file runs in no target — see `[B3]`. No capability matrix is recorded anywhere; `docs/programmatic-auth-login-plan.md:570-573` still states it as a promise. The `kbc_pat_*` half is arguably moot — the CLI only mints `kbc_at_*` |
| NB-2 `auth status` must refresh an expired access token | **RESOLVED** | `SessionTokenProvider.introspect()` (`token_provider.py:136-147`) with exactly this rationale, and the `auth status` path calls it |
| NB-3 bearer E2E arrives one PR too late | **MOOT (formally)** | `test_e2e_auth.py` ships in this diff — but weakened to a formality by `[B3]` |
| NB-4 guards must cover every direct `project.token` consumer | **PARTIAL** | All four named consumers are guarded: `semantic_layer_service.py:239,1661`, `kai_service.py:91`, `mcp_service.py:238,454`, `sharing_service.py:66`. A full sweep of `\.token\b` reads in `src/` found the rest clean — except the two channel-B write paths in `[B1]` and `[B2]`, and the latent `[N5]` |
| NB-5 align the PKCE callback timeout with the backend | **RESOLVED** | `constants.py:579-582` — `AUTH_CALLBACK_TIMEOUT = 115.0`, with a comment stating the backend closes the window at 120 s |

---

## Verified clean

Recorded so it does not get lost among the findings — the core of this PR is
genuinely good, and these were checked by reading the code, not inferred from
names.

**PKCE** (`auth/pkce.py`). `secrets.token_urlsafe` with 48/32 bytes
(384/256-bit entropy, inside RFC 7636's 43-128 char range, sizes read from
`constants.py:584-587` rather than hardcoded); `code_challenge` =
unpadded base64url(SHA256(verifier)); `S256` hardcoded at `auth_client.py:145`.
State compared with `hmac.compare_digest` **before** any other branching in
`do_GET` (`:201`), so a forged `error=` callback cannot bypass it. Loopback
binds only `127.0.0.1` / `[::1]`, never `0.0.0.0`. Success/failure HTML are
static constants, never templated with request data. `log_message` silenced so
the code never reaches stderr via the stdlib access log. `open_browser` /
`_open_silently` (`environment.py:164-194`) never log the URL and catch every
exception so `threading.excepthook` cannot print it. The exception hierarchy
encodes fallback-eligibility as a security property: only pre-exchange failures
(`PkceSetupError`, `PkceCallbackTimeout`) may fall back to the device flow;
`PkceStateMismatch` and `PkceAuthorizationError` are terminal.

**Device flow** (`auth/device.py`). Honours `authorization.interval`, adopts a
server `slow_down` interval or increments by
`AUTH_DEVICE_SLOW_DOWN_INCREMENT` capped at `AUTH_DEVICE_MAX_INTERVAL`, checks
the `expires_in` deadline before every poll, and distinguishes `DENIED` from
`PENDING` / `ERROR`. The `_REFUSAL_KEYWORDS` prose match (`:33-47`) only
upgrades a generic ERROR to DENIED — both paths are terminal — and the
rationale is documented; the English-only keyword list is a theoretical
localization gap, not a finding.

**Token storage.** `state_store.py:save()` opens the temp file with
`os.open(..., 0o600)` **before** any byte is written, then `os.replace()` in the
same directory — atomic, no chmod-after-write race. Every result dataclass in
`auth_service.py` carries no token field at all, safe by construction. No
f-string, log call, or exception message anywhere in the auth package embeds a
full `access_token` / `refresh_token`. `AuthClient` is always constructed with
`token=""` (`auth_client.py:118`).

**Bearer wiring.** `BearerAuth` is passed as an `httpx.Auth` via the `auth=`
kwarg (`http_base.py:98`, propagated to sub-clients at `client/_core.py:114`),
so it is stamped per-request rather than baked into client defaults. None of
`KeboolaClient` / `ManageClient` / `AuthClient` sets `follow_redirects=True`,
and httpx 0.28.1 strips `Authorization` on cross-origin redirects anyway
(verified) — no redirect leak surface. The `http_auth` parameter added to
`BaseHttpClient` is additive and keyword-only, byte-identical for static-token
callers.

**Guard centralization.** `require_static_token` lives in one place
(`auth/sentinel.py`) and is called from 14 files — not 16 hand-written copies.
`default_client_factory` was retained but now fail-fasts, so the sentinel can
never be sent as `X-StorageApi-Token` even through the legacy factory.

**`auth/` dependency direction.** Nothing in `auth/*.py` imports from
`services/` or `commands/`. Every import is stdlib, third-party, a top-level
sibling (`constants` / `errors` / `http_base` / `models`), or intra-package; the
one `..config_store` reference is `TYPE_CHECKING`-only
(`state_store.py:26-30`). Internally layered `sentinel`/`models` →
`state_store` → `token_provider`/`auth_client` → `pkce`/`device`, with
`auth_client` imported lazily from `token_provider._build_client` to keep the
graph acyclic. A clean peer of `sync/`, not a fourth layer.

**`AuthClient`'s deviations from the client contract are all documented with a
stated reason.** `poll_device_token:181-193` bypasses `_do_request` because an
RFC 8628 polling 400 is a protocol state; `revoke:335` / `delete_session:365`
bypass it because logout must never raise; `refresh:283` *does* use it,
justified by the server's 30 s idempotent grace window. It overrides
`_raise_api_error` (`:436`) so the 404-means-feature-disabled mapping covers
every call at once. Better than the contract, not a deviation from it.

**Error handling.** Zero raw `error_code="..."` literals in any new file. All 8
new codes were added to `ErrorCode` *and* `_ERROR_CODE_TO_TYPE`
(`errors.py:212-220,260-267`). No `# ty: ignore`, `# type: ignore`, or `# noqa`
anywhere in the new code. Corrupt JSON, wrong-shape JSON, a version newer than
`AUTH_STATE_VERSION`, and unreadable/non-UTF8 `auth.json` all raise
`ConfigError` explicitly — never silently treated as "logged out" or silently
overwritten (`state_store.py:124-169`).

**Refresh classification.** `_is_rejected_grant` (`auth_client.py:66-91`)
treats HTTP 401 as an unconditional structural rejected-grant signal; the
residual prose match is deliberately scoped to ambiguous 400s only, and that
scoping is right — a malformed-request bug must not purge a live refresh token.
`token_provider.py` persists the rotated pair before updating the in-memory
cache and only purges on `SESSION_EXPIRED`, re-raising anything else, so a
transient failure cannot silently wipe the session. `force_refresh`'s
`rejected_token` parameter correctly prevents the step-5 adopt shortcut from
handing back a token the server just rejected.

**Expiry math.** A missing `refreshExpiresIn` yields `refresh_expires_at=None`,
correctly treated as "unknown, let the server be the authority" rather than
guessed in either direction; a missing `access_expires_at` is treated as stale
(forces refresh) rather than trusted (`auth/models.py:80-105,216-240`).

**Terminal picker.** Non-TTY / piped stdin detected before constructing the
`Application` (raises `CheckboxUnavailable`, falls back to the numeric prompt);
Ctrl-C bound as a key rather than left to raise mid-render, so raw-mode teardown
stays owned by `prompt_toolkit`. `_checkbox_select.py` sits at `commands/`
package level (not auth-scoped), splits a pure I/O-free `CheckboxState` from the
`Application` wiring, and takes `input`/`output` test seams. It duplicates
nothing — the repo had no multi-select before (`init.py` has only a
`typer.confirm`) and no `termios`/`tty.setraw` anywhere, new code included.
`kbagent init --project` is an obvious future consumer.

**Permission registry.** All four subcommands registered
(`permissions.py:19-25`), and `check_cli_permission(ctx, "auth")`
(`commands/auth.py:71`) composes `auth.{subcommand}` via `_helpers.py:270`,
identical to all 30 other groups. No silently-allowed operation.

**Formal surfaces mostly complete.** `AGENT_CONTEXT`
(`commands/context.py:69-96`), `CLAUDE.md:283-286` (flags byte-match live
`--help`), `commands-reference.md:13-32`, `gotchas.md:14` correctly tagged
`(since v0.77.0)` with pre-existing sections intact, `keboola-expert.md` all
three required updates (`:71-73` VERSION GATE, `:162` matrix row, `:328-348`
inline gotchas including the human-at-a-browser rule at `:331-333`),
`SKILL.md:406` workflow-table row and `:71-74` decision table, changelog
(`changelog.py:60-67`) covering both the human requirement and the v1 scope
limits.

**Dependencies.** One new: `filelock>=3.13` — standard, well-known,
cross-platform, and justified by the documented fcntl-is-a-no-op-on-Windows
rationale. No `verify=False` or TLS weakening, no `shell=True`, no
`subprocess` call with shell interpolation of untrusted data anywhere in the new
code.

---

## Decisions taken

Resolved by the repo owner after this review. These are settled inputs, not
open questions — the work plan below implements them.

| # | Question | Decision |
|---|---|---|
| D1 | `[B6]` — does `kbagent serve` support session projects? | **Yes, support them; correct the documentation.** Code stays as written |
| D2 | Scope of this PR vs follow-ups | **Everything in this PR**, including the `[N4]`/`[N5]` refactor and the `make check-sentinel-guards` CI gate |
| D3 | `[N3]` — the unused Manage abstraction | **Delete it** (~-84 LOC). Re-add with its first real caller and a real test |
| D4 | `[B3]` — E2E wiring | **New `test-e2e-auth` target *and* include in the default `make test-e2e`**; provisioning the rotating secret is a separate ops task |
| D5 | `serve` behaviour when a session expires at runtime | **HTTP 401 with `error_code: SESSION_EXPIRED`**, message naming `kbagent auth login` on the host |
| D6 | `project edit --token` on a session project | **Warn and allow** — `allow_credential_type_change=True` at the chokepoint |
| D7 | `[N8]` — `auth.logout` risk class | **Split**: `auth.logout` stays `write`, `--remove-projects` additionally requires an admin-class check |
| D8 | `[N11]` — user-facing docs | **README Setup bullet + a dedicated `docs/auth.md`**, linked from README |

### Consequences of D1 that must be written down, not left implicit

Supporting session projects in `serve` means a **USER-scoped** credential is
exercised on behalf of whoever holds `KBAGENT_SERVE_TOKEN`, which is not the
user's Keboola identity. Anyone with the serve token acts as the logged-in user
for the duration of the session. Separately, refresh-token rotation was designed
for short-lived CLI invocations; in a daemon running for weeks the residual
crash window (between `put_session` at `auth_service.py:307` and the revoke at
`:313`) is open far longer, and a crash there leaves a server session that no
later logout can revoke.

D1 is a deliberate trade for web-UI usability. The requirement it creates is
that this property be **documented as a consciously accepted risk** in
`docs/auth.md` and in the `serve` documentation — not left to be rediscovered.
It also raises the value of `[B3]`/NB-1: with `serve` in scope, the bearer path
carries more weight than the original v1 scope assumed.

### Documentation locations to correct under D1

The code (`server/dependencies.py:62-68`) becomes the authority. Three places
currently claim the opposite and must change:

- `CLAUDE.md`, auth block — the sentence listing `serve` among consumers that
  "fail fast on a sentinel-token project"
- `docs/programmatic-auth-login-plan.md:46`
- the `SessionAuthUnsupportedError` docstring (`errors.py:239`), which lists
  `kbagent serve` among consumers that "must fail fast here"

Keep the plan document as the historical design record, but fix those two stale
claims in it: the `serve` scope above, and the capability-matrix promise at
`:570-573`, which is still stated as future work.

### Note on `[B2]` under D6

The review overstated the "orphaned config entry" consequence for
`project edit --token`. After a deliberate conversion the project has its own
static token, so `auth logout --remove-projects` correctly leaving it alone is
right behaviour, not a leak — and the session itself is still revoked normally.
The genuine defect in `[B1]` was that the conversion is **unrequested**; in
`project edit` the user asks for it explicitly. That is what makes D6
(warn-and-allow) the right call rather than a hard block.

## Recommended order of work

1. **Channel-B guard in `ConfigStore.edit_project`** (~40 LOC) — covers `[B1]`
   and `[B2]` at one chokepoint instead of two patches. `org_service` does not
   pass `allow_credential_type_change` and additionally skips session projects
   at the selection layer; `project edit --token` passes it and warns (D6). Add
   the regression test with a mixed sentinel/static project set, which also
   covers `[B4]`.
2. **`make check-sentinel-guards`** (~30 LOC, D2) — every file reading
   `project.token` outside `auth/` and `services/base.py` must also reference
   `is_session_token` or `require_static_token`. This is what mechanically
   catches the next `org_service`.
3. **`[B5]`** — short single-attempt timeout for the refresh call, so the lock
   hold time cannot exceed `AUTH_LOCK_TIMEOUT`.
4. **`[B4]`** — preserve `error_code` in `_run_parallel` and the MCP loop; audit
   `semantic_layer_service.py` / `stream_service.py` / `flow_service.py` for the
   same shape.
5. **`[B6]` under D1** — correct the three documentation locations, document the
   accepted identity-scope risk, and implement D5 (401 + `SESSION_EXPIRED` from
   the REST surface).
6. **`[B3]` under D4** — `test-e2e-auth` target + `e2e_auth` marker + inclusion
   in the default `make test-e2e`.
7. **`[N4]` + `[N5]`** (D2) — delete the unused `auth/__init__.py` re-export
   block, move the sentinel helpers into `errors.py`, hoist the channel-A guard
   into `BaseHttpClient`. Net negative LOC, and it fixes `client/stream.py` in
   passing.
8. **`[N3]` under D3** — delete `ManageCredential`,
   `resolve_manage_credential`, `make_manage_client_factory`.
9. **`[N7]`** — the two bare tuple returns (~12 LOC).
10. **`[N8]` under D7** — split the logout permission check.
11. **Transparency group — `[N13]`, `[N14]`, `[N15]`** (see
    [The static-token / session split](#the-static-token--session-split)). Do
    these together, and after `[B4]`, since without the preserved error code the
    machine-readable half does not work:
    - `Auth` column (`session` / `static`) in `project list` and
      `project status`, plus an explicit `auth_mode` field in `--json`
      (`[N13]`, and nit 14 in the same pass);
    - fix the `SessionAuthUnsupportedError` remedy text to name
      `project edit --token` per D6 (`[N14]`);
    - print the "not available on session projects" list after a successful
      registration, in both human and `--json` output (`[N15]`).
12. **`[N1]`, `[N2]`, `[N6]`, `[N9]`, `[N10]`, `[N12]`** and the remaining nits —
    Rich markup escaping, `normalize_stack_url` lowercasing, moving the
    selection-mode orchestration into `AuthService`, `SKILL.md` triggers,
    `auth-workflow.md` register-projects section, the `docs/sdk.md` line.
13. **`[N11]` under D8** — README Setup bullet + `docs/auth.md`, using
    [The static-token / session split](#the-static-token--session-split) as the
    source material and documenting the D1 identity-scope trade-off explicitly.
14. **`[B7]`** — rewrite the PR description (drop "docs-only", add
    `register-projects`, document the `serve`-router skip, list the eight plugin
    and doc files touched), push the 3 local commits, mark ready for review.

`[N1]` (Rich markup escaping) is cheap and worth folding into whichever pass
touches `commands/auth.py`.

---

## Review methodology

Six independent parallel passes, each briefed with the same shared context and
constrained to read-only:

| Pass | Focus |
|---|---|
| security | PKCE/device-flow crypto, token handling, bearer wiring, revocation |
| silent-failure | swallowed exceptions, misclassified errors, fallbacks hiding bugs |
| devil's advocate | concurrency interleavings, expiry arithmetic, edge cases, test quality |
| formal compliance | every CONTRIBUTING / CLAUDE.md mandated surface |
| architecture | 3-layer boundaries, binding quality patterns, duplication, structural fixes |
| regression | the prior docs-only review's B-1..B-4 / NB-1..NB-5 against shipped code |

Every blocking finding was then re-verified firsthand rather than taken on
trust: the `org_service` skip logic and overwrite path were read directly;
`grep -c auth Makefile` = 0; `SessionAuthUnsupportedError`'s base class was read
at `errors.py:239`; the `[B5]` arithmetic was computed from `constants.py`
(`MAX_RETRIES=3`, `read=30.0`, backoff 1+2 s); the absence of any guard in
`server/` and of `is_session_token` in `project_service.py` /
`server/routers/projects.py` were confirmed by grep; `[N4]` was verified by an
empirical `sys.modules` check; httpx 0.28.1's cross-origin `Authorization`
stripping was verified against the installed package.

Two findings were **corrected** during consolidation rather than passed through:
the `[B1]` trigger (a healthy session is skipped by default; the hole needs
`--force` or an expired session), and the `[B4]` Windows-locking suspicion (the
cross-process tests do spawn real processes — the original concern does not
apply).

Nothing in the repository was modified, and nothing was posted to GitHub.
