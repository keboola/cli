# Review of #535 — Implementation plan for PKCE and device authorization login

PR: https://github.com/keboola/cli/pull/535  
Reviewed head: `622818288f136ff983e8026d67abcde4b782b4d1`

## Summary

The PKCE and device authorization flows are feasible, and the proposed separation between
`config.json` and `auth.json` is a reasonable compatibility mechanism. The plan should not
be implemented as written yet because session replacement, token revocation, Manage client
wiring, and cross-platform refresh locking have unresolved correctness problems.

Plaintext storage of the session credentials is an explicitly accepted project risk and is
not a finding in this review. The implementation must still enforce the documented
guardrails: owner-only file permissions where supported, atomic writes, local Git exclusion,
and no token exposure through logs, JSON output, command arguments, or subprocess
environments.

The review also assumes that
[DMD-1593](https://linear.app/keboola/issue/DMD-1593/support-pat-tokens-in-all-public-facing-services)
will provide programmatic bearer support in nearly all public-facing services. Since the
issue is described in terms of PATs, the implementation must still verify that each service
accepts session access tokens (`kbc_at_*`) as well as PATs (`kbc_pat_*`).

## Verdict

- **Verdict:** REQUEST CHANGES
- **Blocking findings:** 4
- **Non-blocking findings:** 5
- **Nits:** 0

## Blocking findings

### `[B-1]` `docs/programmatic-auth-login-plan.md:333` — Re-login leaves the previous server session active

The plan says that logging in again to the same stack "rotates credentials" while leaving
sentinel projects untouched. PKCE and device login actually create a new programmatic
session with a new session ID; overwriting the stack entry in `auth.json` only discards the
local handle to the previous session. The old access and refresh tokens remain valid until
expiry or explicit revocation, and a later `auth logout` can revoke only the newest session.

Define an explicit replacement algorithm: retain the old credentials until the new login is
fully persisted, revoke the old session, and specify recovery behavior if revocation fails.
Add a test proving that two consecutive logins followed by logout leave no locally replaced
session active.

References:

- [Plan lines 329-334](https://github.com/keboola/cli/blob/622818288f136ff983e8026d67abcde4b782b4d1/docs/programmatic-auth-login-plan.md#L329-L334)
- [Connection creates a new session ID for every login](https://github.com/keboola/connection/blob/044054e2428fd9d5614144739a70b08e132d5e98/connection/src/Auth/Session/ProgrammaticSessionService.php#L234-L261)

### `[B-2]` `docs/programmatic-auth-login-plan.md:221` — Logout uses the wrong token revocation contract

The proposed `revoke(access_token)` method is described as setting the bearer token on the
request. `POST /v1/auth/token/revoke` is a public endpoint that requires a JSON body
containing `token` and an optional `tokenTypeHint`; an Authorization header is not the
revocation input. Implemented literally, logout would receive a 400 response and then remove
the only local credentials, leaving the server session active.

Model the request body explicitly and revoke the refresh token with
`tokenTypeHint: "refreshToken"` before deleting local state. A failed or uncertain revoke
must be reported distinctly from a confirmed remote revoke, even if local cleanup remains
available as an explicit recovery action.

References:

- [Plan lines 217-223](https://github.com/keboola/cli/blob/622818288f136ff983e8026d67abcde4b782b4d1/docs/programmatic-auth-login-plan.md#L217-L223)
- [TokenRevokeAction request contract](https://github.com/keboola/connection/blob/044054e2428fd9d5614144739a70b08e132d5e98/connection/src/Controller/Auth/TokenRevokeAction.php#L41-L75)
- [TokenRevokeRequest body fields](https://github.com/keboola/connection/blob/044054e2428fd9d5614144739a70b08e132d5e98/connection/src/Auth/Token/TokenRevokeRequest.php#L11-L45)

### `[B-3]` `docs/programmatic-auth-login-plan.md:267` — The Manage credential path is incompatible with existing call sites

`resolve_manage_token()` currently returns a string, has no `ConfigStore`, stack, project,
or operation context, and its callers pass the returned value into service-level
`ManageClientFactory` functions. The plan changes its behavior to return a bearer-mode
`ManageClient`, which is incompatible with both its return type and the existing service
interfaces.

Introduce an explicit Manage credential/client abstraction and update the affected command
and service boundaries intentionally. The design must also distinguish session-compatible
Manage operations from endpoints requiring admin or super-admin tokens; for example, feature
management must continue to request the stronger credential instead of silently trying a PA
session and failing with 403.

References:

- [Plan lines 257-269](https://github.com/keboola/cli/blob/622818288f136ff983e8026d67abcde4b782b4d1/docs/programmatic-auth-login-plan.md#L257-L269)
- [`resolve_manage_token()` currently returns `str`](src/keboola_agent_cli/commands/_helpers.py#L27-L72)
- [PA sessions do not gain admin/super Manage privileges](https://github.com/keboola/platform-architecture-and-concepts/blob/a0685c3598c906e6ca84144da1a6385be7c25f5a/auth/programmatic-auth.md#L27-L33)

### `[B-4]` `docs/programmatic-auth-login-plan.md:285` — The refresh algorithm is not cross-process safe on Windows

The plan claims thread- and cross-process-safe refresh rotation by mirroring the existing
`fcntl` sidecar lock. The current locking helper is intentionally a no-op on Windows, while
the project builds and distributes a Windows wheel. Concurrent or delayed writers can
therefore overwrite a newer rotated pair with an older pair; once the server grace window
passes, reuse can revoke the whole refresh-token family.

Specify a real Windows locking implementation or a fail-closed platform strategy. Tests
must use separate processes rather than two provider instances in one process, and should
cover a delayed stale writer as well as the normal simultaneous-refresh case.

References:

- [Plan lines 278-299](https://github.com/keboola/cli/blob/622818288f136ff983e8026d67abcde4b782b4d1/docs/programmatic-auth-login-plan.md#L278-L299)
- [Current locking is skipped on Windows](src/keboola_agent_cli/config_store.py#L55-L75)

## Non-blocking findings

### `[NB-1]` `docs/programmatic-auth-login-plan.md:253` — Verify `kbc_at_*` support, not only PAT support, per service

DMD-1593 materially reduces the risk that Queue, Query, Encryption, and Sync Actions reject
programmatic bearers. However, the issue is framed around PAT support while this design sends
a programmatic session access token. Before enabling a command for sentinel projects, record
a capability matrix confirming both `kbc_at_*` and `kbc_pat_*` behavior for every directly
called service and endpoint family.

The E2E suite should exercise at least one real call through each sub-client, including a
401/refresh path. Unsupported services should fail before sending a request and name the
required static-token fallback.

References:

- [Plan lines 249-276](https://github.com/keboola/cli/blob/622818288f136ff983e8026d67abcde4b782b4d1/docs/programmatic-auth-login-plan.md#L249-L276)
- [DMD-1593](https://linear.app/keboola/issue/DMD-1593/support-pat-tokens-in-all-public-facing-services)

### `[NB-2]` `docs/programmatic-auth-login-plan.md:341` — `auth status` must refresh an expired access token

The plan calls `introspect(access_token)` directly and describes status as live whenever the
session is valid. A normal session can have an expired one-hour access token and a valid
30-day refresh token. Status would receive 401 and could incorrectly report a dead session
unless it obtains the access token through `SessionTokenProvider` first.

Use the same provider path as normal API calls, then introspect the resulting live access
token. Test expired-access/live-refresh, offline, revoked, and fully expired states
separately.

### `[NB-3]` `docs/programmatic-auth-login-plan.md:400` — Bearer E2E coverage arrives one PR too late

PR 5 changes the hot authentication path, but real service E2E coverage is deferred to PR 6.
The existing static-token suite cannot prove that programmatic bearer routing works across
the deployed services. Move the applicable E2E tests and capability verification into PR 5,
or combine PRs 5 and 6 so no merge advertises an unverified authentication path.

References:

- [PR phasing](https://github.com/keboola/cli/blob/622818288f136ff983e8026d67abcde4b782b4d1/docs/programmatic-auth-login-plan.md#L392-L401)
- [Current E2E proposal](https://github.com/keboola/cli/blob/622818288f136ff983e8026d67abcde4b782b4d1/docs/programmatic-auth-login-plan.md#L431-L433)

### `[NB-4]` `docs/programmatic-auth-login-plan.md:151` — Older CLIs do not always degrade to a clean 401

The compatibility argument assumes every old command sends `ProjectConfig.token` as
`X-StorageApi-Token`. Some existing paths use the configured token as data or pass it to
another client, including semantic-layer token encryption, Kai initialization, MCP
environment construction, and sharing fallback. Those commands may return validation errors,
encrypt the sentinel literal, or fail outside the normal INVALID_TOKEN mapping.

The sentinel remains a workable compatibility trade-off, but the plan should narrow the
claim, list known old-version failure modes, and add new-version fail-fast guards for every
direct `project.token` consumer rather than only client factories.

Reference:

- [Plan lines 147-155](https://github.com/keboola/cli/blob/622818288f136ff983e8026d67abcde4b782b4d1/docs/programmatic-auth-login-plan.md#L147-L155)

### `[NB-5]` `docs/programmatic-auth-login-plan.md:363` — Align the PKCE callback timeout with the backend contract

The plan defines a 300-second CLI callback timeout while the source RFC documents an initial
120-second backend callback timeout. Waiting longer than the server-side authorization
window creates a period where the CLI still appears to be waiting for a callback that can no
longer complete successfully.

Use one shared documented value or ensure the CLI timeout is shorter than the server-side
authorization lifetime. Add a boundary test covering callback arrival immediately before
and after expiry.

References:

- [Plan constants](https://github.com/keboola/cli/blob/622818288f136ff983e8026d67abcde4b782b4d1/docs/programmatic-auth-login-plan.md#L361-L366)
- [Backend RFC timeout](https://github.com/keboola/connection/blob/044054e2428fd9d5614144739a70b08e132d5e98/docs/rfc/programmatic-auth/device-authorization-flow.md#L1486-L1497)

## Nits

(none)

## Feasibility assessment

The feature is feasible after the blocking design gaps are resolved. The backend already
provides the required PKCE, device authorization, refresh, introspection, revocation, and
project-listing behavior. DMD-1593 also makes broad service compatibility plausible.

The highest-risk delivery step remains bearer wiring, not the browser flow itself. A safer
delivery sequence is:

1. Add the state store, protocol client, and login runners without registering public
   commands.
2. Add session lifecycle semantics, including replacement, refresh, status, and remote
   revoke, with cross-platform locking.
3. Activate Storage and Manage command paths only after their capability matrix and E2E
   tests pass.
4. Add remaining services incrementally, with explicit fail-fast behavior for any unsupported
   endpoint family.

## Verification

- PR is a draft and changes one documentation file.
- GitHub CI `check`, Python 3.12, Python 3.13, and Windows wheel build are green.
- `git diff --check origin/main...origin/pr-535` passes.
- No implementation or live feature-stack test was performed because this PR is a design
  document only.
