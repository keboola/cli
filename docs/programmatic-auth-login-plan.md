# Implementation Plan: PKCE + Device Authorization Login (`kbagent auth`)

Status: **implemented in 0.77.0**
This plan has been implemented in full (see `kbagent auth login|status|logout`,
`src/keboola_agent_cli/auth/`, and the "0.77.0" entry in
`src/keboola_agent_cli/changelog.py`). The document below is kept as the design
record; where behavior evolved during implementation, the code and the
changelog are authoritative over this text.
Related: keboola/connection `docs/rfc/programmatic-auth/device-authorization-flow.md` (v8),
keboola/platform-architecture-and-concepts#12 (`auth/programmatic-auth.md`)

---

## 1. Context and goal

Keboola Connection ships **programmatic authentication** (feature-flagged per stack):
browser-based CLI login flows that issue a *programmatic session* — an access token
`kbc_at_*` (1 hour) plus a refresh token `kbc_rt_*` (30 days, rotated on every refresh,
30-second idempotent grace window, family revoke on replay). A programmatic session is
user-scoped: one credential covers **all** the user's projects, with project binding per
request via the `X-KBC-ProjectId` header.

kbagent today supports only static Storage API tokens (`X-StorageApi-Token`, plaintext in
`config.json`). This plan adds:

- `kbagent auth login` implementing **PKCE authorization-code** (default, same-machine
  desktop) and **device authorization** (fallback and `--device-code`, RFC 8628 polling
  semantics);
- session credentials stored as **plaintext in a `0600` file** (`auth.json`), consistent
  with how kbagent already stores static Storage tokens in `config.json` and with how tools
  like Claude Code and Codex store their tokens — see the deliberate deviation from the RFC
  note in §4.2;
- transparent use of the session against Storage and Manage APIs
  (`Authorization: Bearer kbc_at_*` + `X-KBC-ProjectId`), including automatic refresh;
- **full backward compatibility**: static-token auth keeps working byte-identically
  (existing `config.json` files, `project add --token`, `KBAGENT_PROJECT_FROM_ENV`
  / `__env__`, CI pipelines). Both auth modes coexist; static remains the default.

Confirmed scope decisions:

| Decision | Choice |
|---|---|
| Secret storage | plaintext in `auth.json` at `0600` — no keyring, no encryption (matches existing static-token storage; deliberate deviation from the RFC, see §4.2) |
| Command surface | new `kbagent auth` group: `login`, `logout`, `status` |
| Release scope | PKCE + device flow together |
| v1 integration | CLI command paths only; `kbagent serve`, SDK (`lib.py`), MCP subprocess stay static-token and fail fast with a clear error on session projects (follow-up issue) |

## 2. Server contract consumed by the CLI

| Endpoint | Purpose |
|---|---|
| `GET {stack}/admin/auth/pkce/authorize?responseType=code&clientId=keboola-cli&redirectUri=http://127.0.0.1:{port}/callback&codeChallenge={S256}&codeChallengeMethod=S256&state={state}` | Browser-side authorize; 302 back to loopback with `code` (`kbc_ac_*`, 5 min) + `state` |
| `POST /v1/auth/pkce/token` `{clientId, code, state, redirectUri, codeVerifier}` | Code exchange → `CliTokenResponse` |
| `POST /v1/auth/device` `{clientId, scope: {credentialType: "session"}}` | → `{deviceCode, userCode, verificationUri, verificationUriComplete, expiresIn: 900, interval: 5}` |
| `POST /v1/auth/device/token` `{clientId, deviceCode}` | Poll. Non-success = Keboola exception envelope, HTTP 400, RFC 8628 token in `params.error`: `authorization_pending`, `slow_down` (+ `params.interval`), `access_denied`, `expired_token`, `invalid_grant`, `incorrect_client_credentials`; HTTP 429 `rate_limit_exceeded` |
| `POST /v1/auth/token/refresh` | Rotates the pair; 30 s idempotent grace for the old refresh token; reuse after grace = family revoked |
| `GET /v1/auth/token/introspect` | Session metadata + accessible projects `{id, name, role}` (live membership) |
| `POST /v1/auth/token/revoke`, `GET/DELETE /v1/auth/sessions[/{id}]` | Logout / session management |

`CliTokenResponse` (shared by both token endpoints):
`{accessToken, refreshToken, tokenType: "Bearer", expiresIn: 3600, sessionId, user?: {id, email, name}}`.

Feature flags per stack: `programmatic-auth` (master), `pkce-authorization-flow`,
`device-authorization-flow`. Disabled → endpoints return **404 fail-closed**; the CLI must
detect this and explain rather than retry.

Mandatory CLI behavior (RFC "CLI Behavior" section):

- Stack URL must be known before login (login is not stack discovery).
- Prefer PKCE; **auto-fallback to device flow only on setup/callback failures before token
  exchange** (loopback bind failure, no browser handler, callback timeout, remote/container
  heuristics). Never fall back after a successful callback.
- `--device-code` forces device flow.
- `codeVerifier`: 43–128 chars, ≥256-bit CSPRNG; `state`: ≥128-bit; S256 only; the CLI must
  verify the returned `state` (constant time) *before* the token exchange.
- Loopback listener on a random free port on `127.0.0.1` or `[::1]`; shut down after
  success, timeout, or terminal error.
- Device flow: always print `verificationUri` + `userCode`; best-effort open
  `verificationUriComplete`; poll per `interval`; `slow_down` uses the returned interval.
- `clientId` is `keboola-cli` (must not contain `:`).
- The RFC recommends OS-keychain / encrypted credential storage. We deliberately store
  tokens as plaintext in a `0600` file instead (§4.2) — the security posture is identical
  to the static Storage tokens kbagent already persists that way. This means no keyring
  dependency and no per-invocation passphrase prompt.

## 3. Verified codebase facts that shape the design

- `config_store.py:276` — a config `version` greater than `CURRENT_CONFIG_VERSION` makes
  **every older CLI hard-fail** reading the file. A version bump is therefore off the table.
- `AppConfig` / `ProjectConfig` use pydantic default `extra="ignore"` — an old CLI *loads*
  a config with unknown fields fine but **drops them on save**. Session state cannot live in
  new `config.json` fields.
- `ClientFactory = Callable[[str, str], KeboolaClient]` (`services/base.py:21`); ~150 call
  sites pass `(project.stack_url, project.token)`. `cli.py:317-349` constructs services with
  only `config_store` — the factory default is resolved inside `BaseService.__init__`
  (`services/base.py:97`). Changing the factory *default*, not the signature, reaches
  everything with minimal churn.
- `BaseHttpClient` (`http_base.py:55-89`) builds a single `httpx.Client` with write-once
  headers and has **no auth-mutation or 401-refresh hook**; 401 → `ErrorCode.INVALID_TOKEN`
  (`http_base.py:285-291`), exit 3. httpx's native `auth=` parameter (an `httpx.Auth`
  instance, invoked per request, generator-based so it can retry once after a 401) is the
  zero-churn injection seam.
- Sub-clients (queue/query/encrypt/sync-actions) copy the main client's headers
  (`client/_core.py:93-149`) — bearer mode must propagate `auth=` there too.
- `ConfigStore` holds an `fcntl` flock on `config.json.lock` for the whole
  `transaction()` (`config_store.py:179-230`) — a token refresh must NOT run inside it
  (network I/O under the config lock would block every concurrent kbagent process).
- Parallel multi-project commands use `ThreadPoolExecutor`
  (`services/base.py:148-208`) — refresh must be thread-safe *and* cross-process-safe.
- `config.json` is already written `0600` with static Storage tokens in cleartext
  (`config_store.py`), so storing session tokens plaintext in a `0600` sibling file adds no
  new exposure class. **No new runtime dependency** is required.
- Loopback + browser precedent: `commands/lineage.py:1402-1460` (stdlib `http.server`
  on `127.0.0.1`, `webbrowser.open` on a thread).
- Precedent for "persist long-lived credential, keep short-lived bearer out of config":
  `DeveloperPortalIdentity` (`models.py:94-138`).

## 4. Design

### 4.1 Data model — sibling `auth.json` + sentinel token (no config.json schema change)

**`auth.json`** (new file next to `config.json`; `0600`, atomic tmp+rename, its own
`auth.json.lock` sidecar flock; mirrors `ConfigStore` patterns) holds session metadata
**and the tokens themselves** (plaintext), keyed by normalized stack URL (one PA login per
stack per OS user):

```python
class StackSession(BaseModel):              # persisted in auth.json (0600)
    stack_url: str                          # normalized https://host
    session_id: str
    user_email: str = ""
    user_name: str = ""
    access_token: str                       # kbc_at_* — plaintext, 0600 file
    refresh_token: str                      # kbc_rt_* — plaintext, 0600 file
    access_expires_at: datetime | None = None
    refresh_expires_at: datetime | None = None
    created_at: datetime
    model_config = {"extra": "allow"}       # forward compat

class AuthState(BaseModel):                 # auth.json root
    version: int = 1
    sessions: dict[str, StackSession] = Field(default_factory=dict)
```

**Project linkage — sentinel token.** A session-registered project is a normal
`ProjectConfig` whose `token` field holds:

```
kbc-session://{project_id}          # e.g. "kbc-session://10105"
```

Why this works:

- **New CLI**: the client factory detects the `kbc-session://` prefix, parses the project
  id (the one datum the 2-arg factory signature lacks), and builds a bearer client.
- **Old CLI**: loads the config fine (plain string in a known field). For the common path
  — commands that send `ProjectConfig.token` as `X-StorageApi-Token` — this is a clean
  per-project 401 / exit 3; all static-token projects keep working, and the sentinel
  survives old-CLI saves.
- **No `CURRENT_CONFIG_VERSION` bump** (regression-tested), no new `ProjectConfig` field
  (which old CLIs would silently drop — a second source of truth that rots).

**Caveat (review NB-4): the "clean 401" claim is narrowed.** Not every command sends the
token as an `X-StorageApi-Token` header. Some consume `project.token` as *data* and would
fail differently on an old CLI: `semantic_layer_service.py:1662` encrypts it as
`#metastore_token` (would encrypt the sentinel literal), `kai_service.py:91` passes it as
`storage_api_token`, `sharing_service.py:65` returns it as a value, and
`mcp_service.py:238/452` injects it into the MCP subprocess env/header. The sentinel is
still a workable compatibility trade-off, but:

- the plan does **not** claim a uniform clean 401 on old CLIs — these paths may surface a
  validation error or an encrypted-literal instead; documented as a known old-version
  limitation.
- the **new** CLI must add a fail-fast sentinel guard at **every direct `project.token`
  consumer**, not only the client factories (enumerated above) — each must raise
  `AUTH_NOT_SUPPORTED_ON_STACK` before using the value.

New `AuthStateStore` (`auth/state_store.py`) mirrors `ConfigStore` (flock sidecar, atomic
write, `transaction()`), constructed from `config_store.config_dir` so `--config-dir` and
local `.kbagent/` resolution carry over. It is the single read/write path for both the
metadata and the tokens.

### 4.2 Token storage: plaintext `auth.json` (deliberate RFC deviation)

The RFC recommends an OS keychain or a passphrase-encrypted file and explicitly forbids a
plaintext escape hatch. **This plan deliberately does not follow that recommendation.**
Instead the access and refresh tokens are stored in cleartext inside `auth.json`, written
`0600` (owner read/write only), exactly like `ConfigStore` already persists static Storage
tokens in `config.json`.

Rationale for the deviation (project decision):

- **Identical posture to today.** A static Storage token in `config.json` is a
  long-lived, full-power, plaintext credential at `0600`. A `kbc_rt_*` refresh token at
  `0600` is *no worse* — and it is short-lived and server-revocable (`auth logout`,
  password change, admin cascade), which a static token is not. Adding a keychain would
  protect the new credential while leaving the strictly more dangerous existing one in
  cleartext — inconsistent for little real gain.
- **Precedent.** Claude Code, Codex, `gh`, and similar developer CLIs persist their tokens
  as plaintext files under the user's home; this is the accepted norm for developer tooling
  running under a single OS account.
- **Simplicity / no new dependency.** No `keyring`, no encryption code, no per-invocation
  passphrase prompt, no cross-platform keychain-availability matrix — all of which were the
  largest source of environment-specific failure modes in the original design.

Guardrails kept:

- `auth.json` is created and rewritten `0600` via the same atomic tmp+rename the config
  store uses; a permission-tightening check runs on load.
- Tokens never appear in logs, `--verbose` output, or `--json` command output; only
  `mask_token()` forms are ever printed.
- Tokens are never passed on the command line or exported into subprocess environments.

This decision is reversible later: because all token access goes through `AuthStateStore`,
a future encrypted/keychain backend can be slotted behind the same interface without
touching the token provider or the flows.

### 4.3 Auth HTTP client (Layer 3, `auth/auth_client.py`)

`AuthClient(BaseHttpClient)` with no auth header. Wire models in `auth/models.py`
(pydantic, camelCase aliases, `populate_by_name`, never persisted): `CliTokenResponse`,
`DeviceAuthorization`, `AuthUser`, `IntrospectResponse`.

- `start_device_authorization() -> DeviceAuthorization` — via `_do_request` (404
  intercepted → `AUTH_NOT_SUPPORTED_ON_STACK`).
- `poll_device_token(device_code) -> DevicePollResult` — calls `self._client.request`
  directly (bypasses the retry loop: polling 400s are protocol states, not errors):

  ```python
  @dataclass(frozen=True)
  class DevicePollResult:
      status: Literal["ok", "pending", "slow_down", "denied", "expired", "error"]
      tokens: CliTokenResponse | None = None
      interval: int | None = None      # params.interval on slow_down
  ```

  429 → treated as slow_down with backoff; unknown 400 → typed error.
- `exchange_pkce_code(code, state, redirect_uri, code_verifier) -> CliTokenResponse`.
- `refresh(refresh_token) -> CliTokenResponse` — network/5xx retry is safe (30 s grace
  makes it idempotent); `invalid_grant`/replay mapped to `SESSION_EXPIRED` before the
  generic error path.
- `introspect(access_token)` — bearer set per request.
- `revoke(token, token_type_hint)` (review B-2) — `POST /v1/auth/token/revoke` is a
  **public** endpoint whose input is a JSON **body** `{token, tokenTypeHint?}`, not an
  `Authorization` header (a header-only call would 400). Model the body explicitly and
  revoke the **refresh** token with `tokenTypeHint: "refreshToken"`. Returns a typed result
  distinguishing *confirmed revoked* from *failed/uncertain* so callers can report the
  difference (see logout).
- Shared `_map_auth_error(response)` recognizing the 404 feature-flag case on every
  auth endpoint.

### 4.4 Bearer auth in existing clients — zero churn at call sites

New `auth/token_provider.py`:

```python
class TokenProvider(Protocol):
    def get_access_token(self) -> str: ...
    def force_refresh(self, rejected_token: str) -> str: ...

class SessionTokenProvider:
    """One instance per stack per process (module-level registry + lock);
    thread-safe; reads and persists rotations via AuthStateStore (auth.json)."""

class BearerAuth(httpx.Auth):
    def auth_flow(self, request):
        request.headers["Authorization"] = f"Bearer {provider.get_access_token()}"
        if self._project_id is not None:
            request.headers["X-KBC-ProjectId"] = str(self._project_id)
        response = yield request
        if response.status_code == 401:          # exactly once
            request.headers["Authorization"] = f"Bearer {provider.force_refresh(...)}"
            yield request
```

Plumbing (additive, default `None` = byte-identical behavior):

- `BaseHttpClient.__init__(..., http_auth: httpx.Auth | None = None)` → `httpx.Client(auth=...)`.
- `_CoreClient.__init__(stack_url, token, *, http_auth=None)` — bearer mode omits
  `X-StorageApi-Token`; `_get_or_create_sub_client` also passes `auth=` so
  queue/query/encrypt/sync-actions inherit it.
- `ManageClient.__init__(..., http_auth=None)` — bearer mode omits `X-KBC-ManageApiToken`.

Factory wiring: change the *fallback* in `BaseService.__init__` (`services/base.py:97`)
from `client_factory or default_client_factory` to
`client_factory or make_default_client_factory(config_store)`. The new factory keeps the
`(stack_url, token)` signature: sentinel token → parse project id, fetch the per-stack
`SessionTokenProvider` from the process-global registry, return
`KeboolaClient(stack_url, token="", http_auth=BearerAuth(provider, project_id))`;
otherwise → exactly the current behavior. Sweep `services/*.py` for locally defined
factory fallbacks and direct `default_client_factory` imports; same treatment for the
manage-client factory. Explicit `client_factory=` injection keeps working (tests rely on it).

Manage API (review B-3): the plan does **not** repurpose `resolve_manage_token`
(`commands/_helpers.py:27-72`) — it currently returns a `str`, has no ConfigStore / stack /
project / operation context, and its return value is fed into service-level
`ManageClientFactory` functions, so making it return a `ManageClient` would break both its
type and every caller. Instead introduce an explicit **Manage credential abstraction**
(e.g. `resolve_manage_credential(ctx, project) -> ManageCredential`) carrying either a
static token or a session-bearer marker, and update the manage command/service boundaries
to consume it deliberately. Crucially, PA sessions **do not** gain admin/super Manage
privileges — session-compatible Manage operations must be separated from those requiring an
admin/super token: `feature` and other privileged commands must keep requesting the
stronger credential (interactive prompt) rather than silently trying a PA session and
failing with 403.

v1 exclusions — fail fast with a clear `ConfigError` when a project token is a sentinel:
`kbagent serve` project resolution, `lib.Client`, `services/mcp_service.py`
`_build_server_params` (never export the sentinel into a subprocess env), and the
AI / data-science / metastore / dev-portal / stream client factories (the RFC guarantees
bearer semantics only for Storage + Manage; verify the rest against a flagged stack as a
follow-up matrix).

**Cross-process lock (review B-4).** The existing `config_store.py` lock helper is a
deliberate **no-op on Windows** (`_HAS_FCNTL = False` → `_try_flock` returns early,
`config_store.py:55-75`), yet the project builds and ships a **Windows wheel**. Mirroring
that helper would leave refresh rotation unserialized on Windows: a delayed or concurrent
writer can overwrite a newer rotated pair with an older one, and once the 30 s grace passes,
reusing the stale refresh token triggers server-side **family revocation** — a hard logout.
Therefore the auth lock must be a **real cross-platform advisory lock**: use the portable
[`filelock`](https://pypi.org/project/filelock/) library (POSIX `fcntl` + Windows
`msvcrt` under one API) for `auth.json.lock`. `config.json.lock` is left unchanged. New
dependency: `filelock` in `[project.dependencies]`.

**Refresh algorithm (thread + process safe):**

```
get_access_token():
  1. memory cache valid (now < expires_at - AUTH_REFRESH_MARGIN 120 s) -> return
  2. acquire threading.Lock (per-stack provider is a process singleton)
  3. re-check memory cache -> return if another thread refreshed
  4. acquire filelock on auth.json.lock     # cross-platform; never config.json.lock across network I/O
  5. re-read auth.json; if the stored access token is fresh
     (another PROCESS rotated) -> adopt it, release, return
  6. POST /v1/auth/token/refresh
     - success: write the new pair + expiries to auth.json FIRST,
       then update the in-memory cache
     - invalid_grant / family revoked: delete the StackSession from auth.json,
       raise SESSION_EXPIRED ("Your login expired or was revoked. Run `kbagent auth login`.")
  7. release filelock, release thread lock
```

The 30 s server-side grace window covers the residual races (crash between refresh and
persist). A refresh token past `refresh_expires_at` short-circuits locally to
`SESSION_EXPIRED` without a network call.

### 4.5 Login flows (Layer 2 `services/auth_service.py` + Layer 1 `commands/auth.py`)

Helpers: `auth/pkce.py`, `auth/device.py`, `auth/environment.py`. Commands registered via
`app.add_typer(auth_app, name="auth")`.

**`auth login [--stack URL|alias] [--device-code] [--register-projects] [--json]`**

1. Resolve stack: explicit URL (normalized) → existing alias → default project's stack →
   error (login is not stack discovery).
2. Method selection: `--device-code` forces device; otherwise PKCE unless
   `auth/environment.py` heuristics indicate remote/containerized: `SSH_CONNECTION` or
   `SSH_TTY` set, `/.dockerenv` exists, `$container` set, `WSL_INTEROP` set without a
   working `wslview` (`wslview --version` exit 0 + on PATH), or no browser handler among
   `xdg-open`/`wslview`/`open`/`start`.
3. **PKCE runner** (`auth/pkce.py`): `code_verifier = secrets.token_urlsafe(48)` (64
   chars, 384-bit), `state = secrets.token_urlsafe(32)`; challenge =
   base64url-nopad(SHA-256(verifier)); stdlib `http.server` on `("127.0.0.1", 0)`
   (fallback `[::1]`) on a thread + `threading.Event` (lineage.py precedent); open the
   authorize URL via `webbrowser`; wait ≤ `AUTH_CALLBACK_TIMEOUT` (115 s, under the
   backend's 120 s callback window — see §4.7). Callback
   handler: constant-time state check (`hmac.compare_digest`) **before** exchange; minimal
   "you can close this tab" page (error page on mismatch). Setup/callback failures →
   automatic device fallback; post-callback exchange failures → error, **no fallback**.
   Listener always shut down (success, timeout, terminal error).
4. **Device runner** (`auth/device.py`): create device authorization; always print
   `verificationUri` + `userCode` (Rich panel; stderr in `--json` mode); best-effort open
   `verificationUriComplete`; poll per `interval`, honor `slow_down` returned interval
   (cap `AUTH_DEVICE_MAX_INTERVAL` 60 s), 429 backoff, deadline = server `expiresIn`.
   Terminal errors → `AUTH_FLOW_DENIED` / `AUTH_FLOW_EXPIRED`.
5. Success: write the pair + `StackSession` to `auth.json` (via `AuthStateStore`) →
   `introspect` → print user + accessible projects table. `--register-projects` (or
   interactive TTY picker): for each selected project, `config_store.add_project(alias,
   ProjectConfig(stack_url, token=f"kbc-session://{id}", project_name, project_id))` —
   aliases slugified from project names, conflicts warned and skipped. Sentinel projects
   are untouched on re-login.
   **Session replacement on re-login (review B-1).** Each PKCE/device login creates a
   *new* server session with a new `sessionId`; simply overwriting the `auth.json` entry
   would orphan the old server session (its tokens stay valid until expiry, and only the
   newest session is later revocable). Explicit replacement algorithm: (a) capture the
   existing `StackSession` (if any) before writing; (b) persist the new pair; (c) revoke
   the **old** session's refresh token (`revoke(..., "refreshToken")`); (d) if revocation
   fails, keep the new session but warn and record the orphaned old `sessionId` so
   `auth status`/`auth logout` can surface/retry it. Never delete the old credentials
   before the new ones are durably persisted.
6. 404 handling: one clear message ("browser login is not enabled on this stack yet");
   if PKCE is 404 but device flow may be enabled, suggest `--device-code`. No retry loop.
7. `--json` output: `{status, method: "pkce"|"device", stack_url, session_id, user_email,
   expires_at, registered_projects: [...]}` — output models structurally contain
   **no token fields** (by construction, not post-hoc filtering).

**`auth status [--stack] [--json]`** — per-stack session: user, access/refresh
expiry from `auth.json`. For the live check (review NB-2) it must **not** call
`introspect` on the stored access token directly — a normal session routinely has an
expired 1 h access token but a valid 30 d refresh token, which would 401 and be
misreported as dead. Instead obtain a live token through `SessionTokenProvider`
(triggering a refresh if needed) and introspect *that*. Distinct outcomes:
expired-access/live-refresh (refresh + live), offline (degraded, from `auth.json`),
revoked / fully expired (dead). Exit 0 with a live session; exit 3 when the queried
session is expired/missing.

**`auth logout [--stack] [--remove-projects]`** — call `revoke(refresh_token,
"refreshToken")` (body-based contract, review B-2) and then delete the `StackSession`
(and its tokens) from `auth.json`; also attempt revoke for any orphaned old `sessionId`
recorded by a prior re-login (B-1). A failed or uncertain revoke is reported **distinctly**
from a confirmed remote revoke (the server session may still be live) — local cleanup still
proceeds as an explicit recovery step, with a warning naming the remaining server session.
Sentinel projects are left by default (subsequent use → `SESSION_NOT_FOUND` with the remedy
command); `--remove-projects` deletes the matching sentinel aliases too.

### 4.6 Error codes and exit mapping

`errors.py` additions: `AUTH_NOT_SUPPORTED_ON_STACK`,
`AUTH_FLOW_TIMEOUT`, `AUTH_FLOW_DENIED`, `AUTH_FLOW_EXPIRED`, `AUTH_BROWSER_UNAVAILABLE`,
`AUTH_STATE_MISMATCH`, `SESSION_EXPIRED`, `SESSION_NOT_FOUND`.
`map_error_code_to_type`: all → `"authentication"` except
`AUTH_NOT_SUPPORTED_ON_STACK` → `"configuration"`.
`map_error_to_exit_code` (`commands/_helpers.py:85-108`): `SESSION_EXPIRED`,
`SESSION_NOT_FOUND`, `AUTH_FLOW_DENIED` → 3; `AUTH_FLOW_TIMEOUT` → 4; others → 1.

### 4.7 Constants (`constants.py`, new section — zero hardcoded values)

`AUTH_CLIENT_ID = "keboola-cli"`, auth endpoint paths, `AUTH_DEVICE_DEFAULT_INTERVAL = 5`,
`AUTH_DEVICE_MAX_INTERVAL = 60`, `AUTH_REFRESH_MARGIN = 120`, `AUTH_LOCK_TIMEOUT = 30.0`,
`SESSION_TOKEN_PREFIX = "kbc-session://"`, `AUTH_STATE_FILENAME = "auth.json"`.

`AUTH_CALLBACK_TIMEOUT` (review NB-5): the backend documents
`AUTH_PKCE_CALLBACK_TIMEOUT_SECONDS = 120` (and a 300 s authorization-code lifetime). The
CLI callback wait must be **shorter than the server-side window**, otherwise the CLI keeps
waiting for a callback that can no longer complete — set `AUTH_CALLBACK_TIMEOUT = 115.0`
(just under the backend's 120 s), documented as tracking the backend value. A boundary
test must cover callback arrival immediately before and after expiry.

### 4.8 Guardrails

- Permission engine: `auth login`/`auth logout` classified `cli:write` (mutate local
  config/token state, same class as `project add`); `auth status` = `cli:read`.
- Tokens never printed: JSON output models contain no token fields; provider logs masked
  values only; `--verbose` must not dump auth headers.
- `commands/context.py` AGENT_CONTEXT: note that `auth login` requires a human at a
  browser (AI agents must not attempt it headlessly) and that session tokens are not
  readable via the CLI by design.

## 5. Files

**New** (`src/keboola_agent_cli/auth/` package): `__init__.py`, `models.py`,
`state_store.py`, `auth_client.py`, `token_provider.py`, `pkce.py`,
`device.py`, `environment.py`; plus `services/auth_service.py`, `commands/auth.py`.

**Modified**: `http_base.py` (http_auth param), `client/_core.py` + `client/_client.py`
(pass-through + sub-client auth propagation), `manage_client.py`, `services/base.py`
(sentinel-aware default factory), `commands/_helpers.py` (exit codes, manage session
path), `errors.py`, `constants.py`, `cli.py` (register `auth` group), `models.py`
(docstring note on the token sentinel only), `pyproject.toml` (`filelock` dependency for
cross-platform auth-lock, review B-4), and fail-fast sentinel guards at **every direct
`project.token` consumer** (review NB-4): `server/` dependencies, `lib.py`,
`services/mcp_service.py` (`:238`, `:452`), `services/semantic_layer_service.py` (`:1662`
`#metastore_token`), `services/kai_service.py` (`:91`), `services/sharing_service.py`
(`:65`), and the non-Storage client factories.

## 6. PR phasing (each PR leaves main shippable; static path untouched)

| PR | Content | Risk to existing users |
|----|---------|------------------------|
| 1 | `auth/models.py`, `state_store.py`, constants, ErrorCodes, unit tests. Nothing imported by `cli.py`. | none |
| 2 | `auth/auth_client.py` + wire models + endpoint/polling tests. | none |
| 3 | `services/auth_service.py` + `commands/auth.py` with **device flow** login/status/logout, permissions, exit mapping, docs-sync, CLI tests. Sessions storable, not yet consumable. | none |
| 4 | `auth/pkce.py`, `auth/environment.py`, fallback logic; PKCE becomes the default. | none |
| 5 | Bearer wiring: `http_auth` plumbing, `token_provider.py` + registry + cross-platform `filelock`, session replacement + remote revoke (B-1/B-2), sentinel-aware default factory, `--register-projects`, Manage credential abstraction (B-3), fail-fast guards at every direct `project.token` consumer (NB-4). **Includes the Storage + Manage capability matrix and bearer E2E tests** (review NB-3): no merge here advertises an unverified auth path. | only PR touching hot paths; existing suite + new bearer E2E are the regression net |
| 6 | Remaining services incrementally (each gated behind fail-fast until verified), gotchas, changelog entry, version bump + `make version-sync`. | none |

## 7. Test plan

- `test_auth_state_store.py` (below) also covers that tokens are written to `auth.json` at
  `0600` and never leak into `config.json`.
- `test_auth_client.py` — device poll matrix (`pending` → `slow_down` with new interval →
  success; `denied`; `expired`; 429; malformed envelope), PKCE exchange, refresh
  `invalid_grant` → `SESSION_EXPIRED`, 404 → `AUTH_NOT_SUPPORTED_ON_STACK`, introspect.
  `revoke` (B-2): asserts a JSON **body** `{token, tokenTypeHint:"refreshToken"}` is sent
  (not an Authorization header) and that a failed revoke returns the *uncertain* result.
  (httpx_mock, pattern of `test_client_device_enrollment.py`.)
- `test_auth_pkce.py` — real loopback server on an ephemeral port driven by an in-test
  HTTP request; state mismatch rejected before exchange (assert token endpoint never
  called); timeout → fallback signal; RFC 7636 S256 test vector; **callback boundary**
  (NB-5): arrival just before vs just after `AUTH_CALLBACK_TIMEOUT`.
- `test_auth_environment.py` — heuristics matrix via monkeypatched env/paths.
- `test_token_provider.py` — proactive margin; reactive 401-once through a real
  `httpx.Client` + `BearerAuth` against httpx_mock; **in-process race**: 10
  ThreadPoolExecutor workers with an expired cache → exactly one refresh call;
  **cross-process race (B-4)**: real separate processes (not two in-process instances)
  contending on `auth.json` via `filelock`, including a *delayed stale writer* that must
  not clobber a newer pair; runs on both POSIX and Windows CI; family-revoked → purge +
  `SESSION_EXPIRED`.
- `test_auth_service.py` — **re-login replacement (B-1)**: two consecutive logins then
  logout leave no locally replaced session active (old refresh token revoked; orphan
  recorded if revoke fails). **`auth status` (NB-2)**: expired-access/live-refresh,
  offline, revoked, fully-expired reported distinctly (status goes through the provider,
  not a raw introspect).
- `test_auth_state_store.py` — flock/transaction semantics (mirror
  `test_file_locking.py`), atomic write, unknown-field passthrough, `0600` perms on the
  written file, tokens absent from `config.json`.
- Compat regression — sentinel project round-trips through load/save;
  `CURRENT_CONFIG_VERSION` unchanged; `__env__` injection and `project add --token`
  byte-identical (existing `TestAddProject`/`TestVersionCheck` untouched and green).
- `test_http_base.py` extension — `http_auth` present/absent; bearer mode sends no
  `X-StorageApi-Token`; sub-client auth propagation.
- `test_cli_auth.py` (CliRunner) — device happy path with mocked service; `--json` output
  contains no token substrings anywhere; 404 UX; sentinel-project fail-fast messages for
  serve/MCP paths.
- E2E (`tests/test_e2e.py` additions, env-gated for a feature-flagged stack, **landing in
  PR5** per NB-3) — one real call through **each** enabled sub-client (Storage, Queue,
  Query, Encryption, Sync Actions) including a 401→refresh path, plus a Manage call with a
  pre-provisioned session; device flow as a documented semi-manual scenario.
- **Capability matrix (NB-1)** — a recorded matrix confirming each directly called service
  accepts a session access token (`kbc_at_*`), *not only* the PATs (`kbc_pat_*`) that
  DMD-1593 targets, before its command is enabled for sentinel projects; unsupported
  services must fail before sending a request and name the static-token fallback.

## 8. Verification (definition of done per PR)

1. `make check` green (ruff, format, changelog-check, pytest).
2. Manual on a feature-flagged stack: `kbagent auth login` (PKCE, desktop); `kbagent auth
   login --device-code` over SSH; `auth status`; a sentinel project running `kbagent
   storage buckets --project X`; access-token expiry → transparent refresh (verify single
   refresh under `job list` across parallel projects); `auth logout` → next use shows the
   `SESSION_EXPIRED`/`SESSION_NOT_FOUND` remedy.
3. Backward compat: existing static-token `config.json` through the new build →
   byte-identical behavior; new-style config opened by a pre-change build → only sentinel
   projects fail (401/exit 3), everything else works.

## 9. Docs / plugin-sync checklist (CLAUDE.md convention #17 — part of "done")

- `src/keboola_agent_cli/commands/context.py` (`AGENT_CONTEXT`)
- `CLAUDE.md` `## All CLI Commands`
- `plugins/kbagent/agents/keboola-expert.md` (tool matrix + version gate)
- `plugins/kbagent/skills/kbagent/SKILL.md` + `references/commands-reference.md`
- `references/gotchas.md` — "(since vX.Y.Z) session-login projects show token
  `kbc-session://…`; older CLI versions get 401 on them; session tokens live plaintext in
  `auth.json` (0600); CI should keep static tokens"
- new `references/auth-workflow.md`
- `src/keboola_agent_cli/changelog.py` entry; version bump + `make version-sync`

## 10. Top risks and mitigations

1. **Old-CLI writes erase session state** — all session state (metadata + tokens) lives in
   `auth.json`, a file old CLIs never touch; the only `config.json` footprint is the
   sentinel in a known field that round-trips. Residual: old-CLI `project edit --token` on
   a sentinel project overwrites it — acceptable (explicit user action).
2. **Non-Storage/Manage services rejecting the bearer** (queue/query/encrypt/sync-actions
   inherit it; AI/stream/DS/dev-portal gated off in v1) — verify each against a flagged
   stack during PR5; unverified paths fail with a clear error, not a confusing 401.
3. **Refresh rotation races, incl. Windows** — per-stack singleton provider, thread lock +
   dedicated **cross-platform `filelock`** on `auth.json.lock` (the existing `fcntl` helper
   is a Windows no-op, review B-4) + re-read-before-refresh + persist-before-use ordering +
   30 s server grace; family-revoked → deterministic purge + re-login message. Never hold
   `config.json.lock` across network I/O. Cross-process tests run on POSIX and Windows CI.
4. **Plaintext tokens on disk** (accepted risk, deliberate §4.2 decision) — refresh tokens
   sit in `auth.json` at `0600`, the same posture as the static Storage tokens already in
   `config.json`. Mitigations: `0600` enforced on write and re-checked on load; tokens
   never logged, printed, put on the command line, or exported to subprocess environments;
   short TTL + server-side revocation (`auth logout`, password/MFA change cascade) mean a
   leaked refresh token is bounded and killable in a way a leaked static token is not.
5. **PKCE loopback/browser edge cases** (broken `wslview`, firewalled loopback,
   IPv6-only) — fallback strictly limited to pre-exchange failures, `--device-code` always
   available, both bind families attempted, bounded callback timeout, heuristics
   unit-tested as a matrix.
6. **Orphaned server sessions on re-login** (review B-1) — every login mints a new
   `sessionId`; the replacement algorithm revokes the old session's refresh token and
   records an orphan handle if revoke fails so `auth status`/`logout` can retry it.
7. **Wrong revoke contract** (review B-2) — `revoke` sends the documented JSON body, not a
   bearer header, and reports confirmed-vs-uncertain so logout never silently leaves a live
   server session while clearing local state.
8. **Old-CLI non-header token consumers** (review NB-4) — the "clean 401" guarantee is
   narrowed to header consumers; token-as-data paths (semantic-layer encrypt, Kai, sharing,
   MCP) are documented old-version limitations and get new-version fail-fast guards.

## 11. Review resolution (PR #535)

This revision incorporates the `review-pr-535.md` findings. Blocking: **B-1** re-login
replacement + old-session revoke (§4.5), **B-2** body-based revoke contract (§4.3, logout),
**B-3** Manage credential abstraction instead of repurposing `resolve_manage_token`, with
admin/super endpoints kept on the stronger token (§4.4), **B-4** cross-platform `filelock`
because the existing lock is a Windows no-op (§4.4, §7, risk 3). Non-blocking: **NB-1**
`kbc_at_*` capability matrix (§7), **NB-2** `auth status` refreshes via the provider (§4.5),
**NB-3** bearer E2E moved into PR5 (§6), **NB-4** narrowed compat claim + per-consumer
fail-fast guards (§4.1, §5), **NB-5** callback timeout aligned under the backend's 120 s
(§4.7). The plaintext-storage decision (§4.2) was explicitly not a review finding.
