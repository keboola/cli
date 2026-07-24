# Implementation Plan: PKCE + Device Authorization Login (`kbagent auth`)

Status: **draft / proposed**
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
- session credentials stored in an **OS keychain (`keyring`) or a passphrase-encrypted
  file** — never plaintext (hard RFC requirement, no escape hatch);
- transparent use of the session against Storage and Manage APIs
  (`Authorization: Bearer kbc_at_*` + `X-KBC-ProjectId`), including automatic refresh;
- **full backward compatibility**: static-token auth keeps working byte-identically
  (existing `config.json` files, `project add --token`, `KBAGENT_PROJECT_FROM_ENV`
  / `__env__`, CI pipelines). Both auth modes coexist; static remains the default.

Confirmed scope decisions:

| Decision | Choice |
|---|---|
| Secret storage | `keyring` primary, passphrase-encrypted file (AES-256-GCM + scrypt) fallback |
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
- Secure credential storage only; if no backend is available, login fails with an
  actionable message (CI/automation should keep using static tokens / PATs).

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
- `cryptography` is already a dependency; only `keyring` is new.
- Loopback + browser precedent: `commands/lineage.py:1402-1460` (stdlib `http.server`
  on `127.0.0.1`, `webbrowser.open` on a thread).
- Precedent for "persist long-lived credential, keep short-lived bearer out of config":
  `DeveloperPortalIdentity` (`models.py:94-138`).

## 4. Design

### 4.1 Data model — sibling `auth.json` + sentinel token (no config.json schema change)

**`auth.json`** (new file next to `config.json`; 0600, atomic tmp+rename, its own
`auth.json.lock` sidecar flock; mirrors `ConfigStore` patterns) holds **metadata only,
zero secrets**, keyed by normalized stack URL (one PA login per stack per OS user):

```python
class StackSession(BaseModel):              # persisted in auth.json
    stack_url: str                          # normalized https://host
    session_id: str
    user_email: str = ""
    user_name: str = ""
    credential_backend: Literal["keyring", "encrypted-file"]
    access_expires_at: datetime | None = None    # advisory; source of truth = credential store
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
- **Old CLI**: loads the config fine (plain string in a known field), sends the sentinel as
  `X-StorageApi-Token` → clean per-project 401 / exit 3; all static-token projects keep
  working. The sentinel survives old-CLI saves. `mask_token` output contains no secret.
- **No `CURRENT_CONFIG_VERSION` bump** (regression-tested), no new `ProjectConfig` field
  (which old CLIs would silently drop — a second source of truth that rots).

Secrets (access + refresh tokens) live **only** in the credential store. The access token
is cached there alongside the refresh token: CLI processes are short-lived, and a
memory-only access token would force a rotation-consuming refresh on every invocation,
multiplying race exposure and server load. (The RFC mandates protection for refresh
tokens; we apply the same bar to access tokens.)

New `AuthStateStore` (`auth/state_store.py`) mirrors `ConfigStore` (flock sidecar, atomic
write, `transaction()`), constructed from `config_store.config_dir` so `--config-dir` and
local `.kbagent/` resolution carry over.

### 4.2 Credential store (`auth/credential_store.py`)

```python
class CredentialBackend(Protocol):
    name: str                               # "keyring" | "encrypted-file"
    def available(self) -> bool: ...
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...
    def delete(self, key: str) -> None: ...

class CredentialStore:                      # facade, detection order: keyring -> encrypted file
    def store_tokens(self, stack_url, access, refresh) -> str   # returns backend name
    def load_tokens(self, stack_url) -> StoredTokens | None     # dataclass(access, refresh)
    def delete_tokens(self, stack_url) -> None
```

- **KeyringBackend** (primary): service name `keboola-cli`; **two entries per stack**
  (`{host}#access`, `{host}#refresh`) to stay under the Windows Credential Manager
  ~2.5 KB blob limit. `available()` = real set/get/delete canary probe (headless Secret
  Service typically fails only on use, not on import).
- **EncryptedFileBackend** (fallback): `credentials.enc` in the config dir, 0600, JSON
  envelope `{version, kdf: {name: "scrypt", n, r, p, salt}, entries: {key: {nonce,
  ciphertext}}}`, AES-256-GCM via `cryptography`. Passphrase via hidden TTY prompt only —
  no env-var escape hatch (matches the RFC and the repo's manage-token default-deny
  precedent). Documented consequence: file-backend users get a passphrase prompt per
  invocation; keyring is the recommended path.
- Neither available → `ErrorCode.AUTH_NO_SECURE_STORAGE`, actionable message
  ("install/enable an OS keychain, or use a static token; CI should use static tokens").
- Token values never appear in logs or output; diagnostics log backend name + masked forms.
- New dependency: `keyring>=25` in `[project.dependencies]`.

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
- `introspect(access_token)`, `revoke(access_token)` — bearer set per request.
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
    thread-safe; persists rotations via CredentialStore + AuthStateStore."""

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

Manage API: `resolve_manage_token` (`commands/_helpers.py:27-72`) gains a session path —
when the resolved project is a sentinel project with a live `StackSession`, return a
bearer-mode `ManageClient` instead of prompting for a manage token.

v1 exclusions — fail fast with a clear `ConfigError` when a project token is a sentinel:
`kbagent serve` project resolution, `lib.Client`, `services/mcp_service.py`
`_build_server_params` (never export the sentinel into a subprocess env), and the
AI / data-science / metastore / dev-portal / stream client factories (the RFC guarantees
bearer semantics only for Storage + Manage; verify the rest against a flagged stack as a
follow-up matrix).

**Refresh algorithm (thread + process safe):**

```
get_access_token():
  1. memory cache valid (now < expires_at - AUTH_REFRESH_MARGIN 120 s) -> return
  2. acquire threading.Lock (per-stack provider is a process singleton)
  3. re-check memory cache -> return if another thread refreshed
  4. acquire flock on auth.json.lock        # never config.json.lock across network I/O
  5. re-read credential store; if the stored access token is fresh
     (another PROCESS rotated) -> adopt it, release, return
  6. POST /v1/auth/token/refresh
     - success: persist new pair to credential store FIRST, then update
       auth.json expiries, then update memory cache
     - invalid_grant / family revoked: purge stack credentials, raise
       SESSION_EXPIRED ("Your login expired or was revoked. Run `kbagent auth login`.")
  7. release flock, release thread lock
```

The 30 s server-side grace window covers the residual races (crash between refresh and
persist; two processes reading the old refresh token before either locked). A refresh
token past `refresh_expires_at` short-circuits locally to `SESSION_EXPIRED` without a
network call.

### 4.5 Login flows (Layer 2 `services/auth_service.py` + Layer 1 `commands/auth.py`)

Helpers: `auth/pkce.py`, `auth/device.py`, `auth/environment.py`. Commands registered via
`app.add_typer(auth_app, name="auth")`.

**`auth login [--stack URL|alias] [--device-code] [--register-projects] [--json]`**

1. Resolve stack: explicit URL (normalized) → existing alias → default project's stack →
   error (login is not stack discovery).
2. **Probe credential backend availability before starting any flow** (fail early with
   `AUTH_NO_SECURE_STORAGE`).
3. Method selection: `--device-code` forces device; otherwise PKCE unless
   `auth/environment.py` heuristics indicate remote/containerized: `SSH_CONNECTION` or
   `SSH_TTY` set, `/.dockerenv` exists, `$container` set, `WSL_INTEROP` set without a
   working `wslview` (`wslview --version` exit 0 + on PATH), or no browser handler among
   `xdg-open`/`wslview`/`open`/`start`.
4. **PKCE runner** (`auth/pkce.py`): `code_verifier = secrets.token_urlsafe(48)` (64
   chars, 384-bit), `state = secrets.token_urlsafe(32)`; challenge =
   base64url-nopad(SHA-256(verifier)); stdlib `http.server` on `("127.0.0.1", 0)`
   (fallback `[::1]`) on a thread + `threading.Event` (lineage.py precedent); open the
   authorize URL via `webbrowser`; wait ≤ `AUTH_CALLBACK_TIMEOUT` (300 s). Callback
   handler: constant-time state check (`hmac.compare_digest`) **before** exchange; minimal
   "you can close this tab" page (error page on mismatch). Setup/callback failures →
   automatic device fallback; post-callback exchange failures → error, **no fallback**.
   Listener always shut down (success, timeout, terminal error).
5. **Device runner** (`auth/device.py`): create device authorization; always print
   `verificationUri` + `userCode` (Rich panel; stderr in `--json` mode); best-effort open
   `verificationUriComplete`; poll per `interval`, honor `slow_down` returned interval
   (cap `AUTH_DEVICE_MAX_INTERVAL` 60 s), 429 backoff, deadline = server `expiresIn`.
   Terminal errors → `AUTH_FLOW_DENIED` / `AUTH_FLOW_EXPIRED`.
6. Success: persist pair via `CredentialStore` → upsert `StackSession` → `introspect` →
   print user + accessible projects table. `--register-projects` (or interactive TTY
   picker): for each selected project, `config_store.add_project(alias,
   ProjectConfig(stack_url, token=f"kbc-session://{id}", project_name, project_id))` —
   aliases slugified from project names, conflicts warned and skipped. Re-login to the
   same stack rotates credentials; sentinel projects untouched.
7. 404 handling: one clear message ("browser login is not enabled on this stack yet");
   if PKCE is 404 but device flow may be enabled, suggest `--device-code`. No retry loop.
8. `--json` output: `{status, method: "pkce"|"device", stack_url, session_id, user_email,
   expires_at, backend, registered_projects: [...]}` — output models structurally contain
   **no token fields** (by construction, not post-hoc filtering).

**`auth status [--stack] [--json]`** — per-stack session: backend, user, access/refresh
expiry from `auth.json`; live `introspect` when reachable (projects + validity), degraded
offline output otherwise. Exit 0 with a live session; exit 3 when the queried session is
expired/missing.

**`auth logout [--stack] [--remove-projects]`** — best-effort `revoke` (warn, do not fail,
on network error), delete credential entries + `StackSession`. Sentinel projects are left
by default (subsequent use → `SESSION_NOT_FOUND` with the remedy command);
`--remove-projects` deletes the matching sentinel aliases too.

### 4.6 Error codes and exit mapping

`errors.py` additions: `AUTH_NO_SECURE_STORAGE`, `AUTH_NOT_SUPPORTED_ON_STACK`,
`AUTH_FLOW_TIMEOUT`, `AUTH_FLOW_DENIED`, `AUTH_FLOW_EXPIRED`, `AUTH_BROWSER_UNAVAILABLE`,
`AUTH_STATE_MISMATCH`, `SESSION_EXPIRED`, `SESSION_NOT_FOUND`.
`map_error_code_to_type`: all → `"authentication"` except `AUTH_NO_SECURE_STORAGE` and
`AUTH_NOT_SUPPORTED_ON_STACK` → `"configuration"`.
`map_error_to_exit_code` (`commands/_helpers.py:85-108`): `SESSION_EXPIRED`,
`SESSION_NOT_FOUND`, `AUTH_FLOW_DENIED` → 3; `AUTH_FLOW_TIMEOUT` → 4; others → 1.

### 4.7 Constants (`constants.py`, new section — zero hardcoded values)

`AUTH_CLIENT_ID = "keboola-cli"`, auth endpoint paths, `AUTH_CALLBACK_TIMEOUT = 300.0`,
`AUTH_DEVICE_DEFAULT_INTERVAL = 5`, `AUTH_DEVICE_MAX_INTERVAL = 60`,
`AUTH_REFRESH_MARGIN = 120`, `AUTH_LOCK_TIMEOUT = 30.0`,
`KEYRING_SERVICE = "keboola-cli"`, `SESSION_TOKEN_PREFIX = "kbc-session://"`,
`AUTH_STATE_FILENAME = "auth.json"`, `ENCRYPTED_CREDENTIALS_FILENAME = "credentials.enc"`,
scrypt N/r/p, `AES_GCM_NONCE_BYTES = 12`.

### 4.8 Guardrails

- Permission engine: `auth login`/`auth logout` classified `cli:write` (mutate local
  config/credentials, same class as `project add`); `auth status` = `cli:read`.
- Tokens never printed: JSON output models contain no token fields; provider logs masked
  values only; `--verbose` must not dump auth headers.
- `commands/context.py` AGENT_CONTEXT: note that `auth login` requires a human at a
  browser (AI agents must not attempt it headlessly) and that session tokens are not
  readable via the CLI by design.

## 5. Files

**New** (`src/keboola_agent_cli/auth/` package): `__init__.py`, `models.py`,
`state_store.py`, `credential_store.py`, `auth_client.py`, `token_provider.py`, `pkce.py`,
`device.py`, `environment.py`; plus `services/auth_service.py`, `commands/auth.py`.

**Modified**: `http_base.py` (http_auth param), `client/_core.py` + `client/_client.py`
(pass-through + sub-client auth propagation), `manage_client.py`, `services/base.py`
(sentinel-aware default factory), `commands/_helpers.py` (exit codes, manage session
path), `errors.py`, `constants.py`, `cli.py` (register `auth` group), `models.py`
(docstring note on the token sentinel only), `pyproject.toml` (`keyring`), fail-fast
guards in `server/` dependencies, `lib.py`, `services/mcp_service.py`, and the
non-Storage client factories.

## 6. PR phasing (each PR leaves main shippable; static path untouched)

| PR | Content | Risk to existing users |
|----|---------|------------------------|
| 1 | `auth/models.py`, `credential_store.py`, `state_store.py`, constants, ErrorCodes, `keyring` dep, unit tests. Nothing imported by `cli.py`. | none |
| 2 | `auth/auth_client.py` + wire models + endpoint/polling tests. | none |
| 3 | `services/auth_service.py` + `commands/auth.py` with **device flow** login/status/logout, permissions, exit mapping, docs-sync, CLI tests. Sessions storable, not yet consumable. | none |
| 4 | `auth/pkce.py`, `auth/environment.py`, fallback logic; PKCE becomes the default. | none |
| 5 | Bearer wiring: `http_auth` plumbing, `token_provider.py` + registry, sentinel-aware default factory, `--register-projects`, manage session path, fail-fast guards (serve / lib / MCP / other factories). | only PR touching hot paths; existing suite is the regression net |
| 6 | E2E, gotchas, changelog entry, version bump + `make version-sync`. | none |

## 7. Test plan

- `test_credential_store.py` — backend detection/fallback order; encrypted-file roundtrip
  (wrong passphrase = GCM auth failure, tampered ciphertext, 0600 perms); keyring canary
  probe (fake backend); no-backend error message.
- `test_auth_client.py` — device poll matrix (`pending` → `slow_down` with new interval →
  success; `denied`; `expired`; 429; malformed envelope), PKCE exchange, refresh
  `invalid_grant` → `SESSION_EXPIRED`, 404 → `AUTH_NOT_SUPPORTED_ON_STACK`,
  introspect/revoke. (httpx_mock, pattern of `test_client_device_enrollment.py`.)
- `test_auth_pkce.py` — real loopback server on an ephemeral port driven by an in-test
  HTTP request; state mismatch rejected before exchange (assert token endpoint never
  called); timeout → fallback signal; RFC 7636 S256 test vector.
- `test_auth_environment.py` — heuristics matrix via monkeypatched env/paths.
- `test_token_provider.py` — proactive margin; reactive 401-once through a real
  `httpx.Client` + `BearerAuth` against httpx_mock; **race test**: 10 ThreadPoolExecutor
  workers with an expired cache → exactly one refresh call; cross-process simulation: a
  second provider instance sharing the config dir adopts the persisted pair without
  refreshing; family-revoked → purge + `SESSION_EXPIRED`.
- `test_auth_state_store.py` — flock/transaction semantics (mirror
  `test_file_locking.py`), atomic write, unknown-field passthrough.
- Compat regression — sentinel project round-trips through load/save;
  `CURRENT_CONFIG_VERSION` unchanged; `__env__` injection and `project add --token`
  byte-identical (existing `TestAddProject`/`TestVersionCheck` untouched and green).
- `test_http_base.py` extension — `http_auth` present/absent; bearer mode sends no
  `X-StorageApi-Token`; sub-client auth propagation.
- `test_cli_auth.py` (CliRunner) — device happy path with mocked service; `--json` output
  contains no token substrings anywhere; 404 UX; sentinel-project fail-fast messages for
  serve/MCP paths.
- E2E (`tests/test_e2e.py` additions, env-gated for a feature-flagged stack) — bearer
  Storage `verify_token` + one Manage call with a pre-provisioned session; device flow as
  a documented semi-manual scenario.

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
  `kbc-session://…`; older CLI versions get 401 on them; keyring/secure storage required;
  CI should keep static tokens"
- new `references/auth-workflow.md`
- `src/keboola_agent_cli/changelog.py` entry; version bump + `make version-sync`

## 10. Top risks and mitigations

1. **Old-CLI writes erase session metadata** — all session state lives in `auth.json` +
   credential store (files old CLIs never touch); the only `config.json` footprint is the
   sentinel in a known field that round-trips. Residual: old-CLI `project edit --token` on
   a sentinel project overwrites it — acceptable (explicit user action).
2. **Non-Storage/Manage services rejecting the bearer** (queue/query/encrypt/sync-actions
   inherit it; AI/stream/DS/dev-portal gated off in v1) — verify each against a flagged
   stack during PR5; unverified paths fail with a clear error, not a confusing 401.
3. **Refresh rotation races** — per-stack singleton provider, thread lock + dedicated
   `auth.json.lock` flock + re-read-before-refresh + persist-before-use ordering + 30 s
   server grace; family-revoked → deterministic purge + re-login message. Never hold
   `config.json.lock` across network I/O.
4. **Keyring environment variance** (headless Linux without Secret Service, Windows blob
   cap, macOS codesign prompts) — canary probe at login before any flow starts, two-entry
   storage, encrypted-file fallback, actionable failure message, backend shown in
   `auth status`.
5. **PKCE loopback/browser edge cases** (broken `wslview`, firewalled loopback,
   IPv6-only) — fallback strictly limited to pre-exchange failures, `--device-code` always
   available, both bind families attempted, bounded callback timeout, heuristics
   unit-tested as a matrix.
