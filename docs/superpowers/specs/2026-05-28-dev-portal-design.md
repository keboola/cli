# `kbagent dev-portal` — Developer Portal support

**Status:** Draft, pending implementation
**Date:** 2026-05-28
**Author:** Matyáš Jirát

## Summary

Add a `kbagent dev-portal` command group that wraps the Keboola Developer
Portal API (`https://apps-api.keboola.com`) for component vendors and portal
admins. Every change made through this surface is production-affecting on
every Keboola stack at once, so the safety model is intentionally stricter
than any other kbagent surface: an AI agent can research and *prepare* a
write, but cannot execute one without a human typing a random hex code on a
real terminal. No env-var bypass, no `--yes` flag.

This work replaces the local-only `component-dev-portal` skill (`dp.py`) by
folding its logic into kbagent so the same primitive is available from the
CLI, the REST `serve` API, the kbagent plugin, and the MCP surface — with
the kbagent firewall, hint-mode codegen, audit-friendly identity model, and
3-layer test discipline applied.

## Motivation

The current state of Developer Portal automation in the team:

- **`dp.py` skill** — useful but local-only, single-user, no integration
  with kbagent's permission engine, no JSON output, no REST surface.
- **`scripts/developer_portal/update_properties.sh`** — pushes a fixed list
  of Cookiecutter-backed properties from `component_config/*` files on
  deploy. Doesn't help with one-off changes, doesn't manage app
  registration, doesn't manage `uiOptions`, `encryption`, `network`,
  `defaultBucket`, icons, etc.
- **Portal UI** — manual, slow, error-prone for property-level work; no
  audit trail of "which identity did what".

The gaps:

1. The historically-manual *register a new app* step.
2. Ad-hoc property updates for fields the deploy scripts don't manage.
3. Multi-identity workflow (Keboola Vendor account, KDS Vendor account,
   portal admin) — `dp.py` has one set of env vars at a time.
4. Programmatic *read* access for agents that need to see "what does
   component X currently look like in the portal?" while designing a peer.
5. A unified, production-safe write path that doesn't depend on each
   engineer remembering to use `--yes` carefully.

## Non-goals

- **ECR image push.** Stays in component GitHub Actions.
- **Bulk repo-file → property sync on deploy.** Stays in
  `scripts/developer_portal/update_properties.sh`.
- **Writes to `component_config/`.** kbagent never materialises portal
  state into the repo; that path belongs to the Cookiecutter template.
- **Persistent token caching.** Each kbagent invocation logs in fresh.

## Design decisions (locked during brainstorming)

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | **Random-code TTY confirm** is the only write path. No `--yes`, no env override. | Direct-production blast radius. Agent cannot generate the random code. Same primitive as `kbagent permissions set`. |
| 2 | Identities stored as `{username, password, role_hint, vendor, portal_url}` in `AppConfig.dev_portal_identities`, same `config.json`, 0600, same `_warning` header. | Mirrors KB project token storage. One store, one lock, one place to look. |
| 3 | v1 scope = `list`, `get`, `create`, `patch`, `upload-icon`, `publish`, `deprecate`. | Matches `dp.py` plus full lifecycle. `list` + `get` are enough primitives for an agent to compose peer-config research itself — no dedicated helper. |
| 4 | `--identity <alias>` flag per command + persisted default via `dev-portal identity use ALIAS`. | Mirrors `kbagent project` UX exactly. |
| 5 | App addressed as `--app VENDOR.APP_ID` (single arg), parsed to `vendor` + `app_id` internally. | Harder to mis-pair than two separate flags. `create` is the exception — no app id yet. |
| 6 | Uniform safety bar across `create`/`patch`/`upload-icon`/`publish`/`deprecate`. | Permission-engine categories (`write`/`admin`/`destructive`) already give graduated firewall control if persistent policy needs it. |

## Architecture

Standard kbagent 3-layer.

```
commands/dev_portal.py
    │  CLI parsing, formatter, identity resolution,
    │  random-code confirm gating, --dry-run, --json
    ▼
services/dev_portal_service.py
    │  Identity CRUD, login orchestration,
    │  prepare_*/apply pattern, diff computation,
    │  publish pre-flight validation
    ▼
dev_portal_client.py  (inherits BaseHttpClient)
    │  Auth (login + MFA), HTTP verbs against apps-api.keboola.com,
    │  icon two-hop (presigned S3 PUT)
    ▼
HTTPS to apps-api.keboola.com
```

### Data model

New Pydantic model:

```python
class DeveloperPortalIdentity(BaseModel):
    username: str
    password: str
    role_hint: str = "vendor"        # free-text label for `identity list`
    vendor: str | None = None        # default vendor for this identity
    portal_url: str = "https://apps-api.keboola.com"

    @field_validator("portal_url")
    @classmethod
    def validate_portal_url(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("Portal URL must use https://")
        return v
```

`AppConfig` gains two fields:

```python
dev_portal_identities: dict[str, DeveloperPortalIdentity] = {}
default_dev_portal_identity: str = ""
```

`ConfigStore` gains mirror methods of the project methods:
`add_dev_portal_identity`, `remove_dev_portal_identity`,
`edit_dev_portal_identity`, `rename_dev_portal_identity`,
`set_default_dev_portal_identity`. The existing `_warning` field at the top
of `config.json` is extended to mention DP credentials.

### Client layer (`dev_portal_client.py`)

`DeveloperPortalClient(BaseHttpClient)` — gets retry/backoff/timeout for
free. State held only in process memory:

```python
class DeveloperPortalClient(BaseHttpClient):
    def __init__(self, identity: DeveloperPortalIdentity) -> None: ...

    # Auth (lazy on first authenticated call)
    def _ensure_authenticated(self) -> None: ...
    def _prompt_mfa(self, session: str) -> str: ...   # /dev/tty, never stdin

    # Read
    def list_apps(self, vendor: str) -> list[dict]: ...
    def get_app(self, vendor: str, app_id: str) -> dict: ...

    # Write (dumb — confirm + dry-run belong to the layers above)
    def create_app(self, vendor: str, payload: dict) -> dict: ...
    def patch_app(self, vendor: str, app_id: str, payload: dict) -> dict: ...
    def upload_icon(self, vendor: str, app_id: str, png_bytes: bytes) -> None: ...
    def publish_app(self, vendor: str, app_id: str) -> dict: ...
    def deprecate_app(self, vendor: str, app_id: str) -> dict: ...
```

Auth flow:

1. `POST /auth/login {email, password}`.
2. `200 {"token": ...}` → bearer cached on the client instance.
3. `200 {"session": ...}` → MFA. If `sys.stdin.isatty()` then prompt via
   `/dev/tty` and re-login with `{email, session, code}`. Otherwise raise
   `DP_MFA_REQUIRED` with an actionable message naming service accounts.
4. Non-200 → raise `KeboolaApiError("DP_LOGIN_FAILED", …)`.

Bearer never written to disk, never logged, exists only on the client
instance. Next invocation = fresh login.

Icon upload is a two-hop: `POST /vendors/{v}/apps/{a}/icon` returns a
presigned URL, then `PUT` bytes to S3 via raw `urllib` (S3 doesn't use our
auth, retry, or timeout). Isolated in one method.

### Service layer (`services/dev_portal_service.py`)

Owns business logic. Commands stay thin; client stays dumb.

```python
class DeveloperPortalService:
    def __init__(
        self,
        config_store: ConfigStore,
        client_factory: Callable[[DeveloperPortalIdentity], DeveloperPortalClient],
    ) -> None: ...

    # Identity management
    def add_identity(self, alias, identity) -> None: ...
    def list_identities(self) -> dict[str, DeveloperPortalIdentity]: ...
    def remove_identity(self, alias) -> None: ...
    def edit_identity(self, alias, **fields) -> None: ...
    def use_identity(self, alias) -> None: ...
    def current_identity(self) -> str: ...
    def verify_identity(self, alias) -> dict: ...  # fresh login probe

    # Portal reads
    def list_apps(self, alias, vendor) -> list[dict]: ...
    def get_app(self, alias, vendor, app_id) -> dict: ...

    # Portal writes — prepare returns a PendingX dataclass with full diff;
    # apply executes only after the command layer has run confirm.
    def prepare_create(self, alias, vendor, payload) -> PendingCreate: ...
    def prepare_patch(self, alias, vendor, app_id, payload) -> PendingPatch: ...
    def prepare_upload_icon(self, alias, vendor, app_id, path) -> PendingIconUpload: ...
    def prepare_publish(self, alias, vendor, app_id) -> PendingPublish: ...
    def prepare_deprecate(self, alias, vendor, app_id) -> PendingWrite: ...

    def apply(self, pending: PendingWrite) -> dict: ...
```

`PendingWrite` and friends are small frozen dataclasses. `PendingPatch`
carries the fetched current state plus a list of `FieldDiff(key, current,
new)` for top-level keys that change — diff per top-level key is enough for
the preview and keeps it readable for nested JSON properties.

Validation pre-flight (no portal call):

- `prepare_create`: payload must have `id`, `name`, `type`; `name` must not
  contain "extractor" or "writer".
- `prepare_publish`: fetch current state; raise
  `DP_PUBLISH_REQUIREMENTS_MISSING` listing missing fields (`icon`, `name`,
  `type`, `repository`, `shortDescription`, `longDescription`,
  `licenseUrl`, `documentationUrl`).
- `prepare_upload_icon`: file exists; reads bytes; soft warning if not 128
  ×128 PNG.

Peer-config research (e.g. "show me how MySQL and Postgres extractors
configure themselves so I can model a new DB connector after them") is
done by the agent calling `list --vendor V` followed by `get --app
VENDOR.APP` for the specific ids of interest, then comparing the
returned JSON in its own context. No dedicated helper — the agent has
the brains, and `list` + `get` already expose everything it needs.

### Command layer (`commands/dev_portal.py`)

```
kbagent dev-portal identity add --alias A --username U
                                [--password P | --password-stdin]
                                [--role-hint vendor|admin] [--vendor V]
                                [--portal-url URL]
kbagent dev-portal identity list
kbagent dev-portal identity remove --alias A
kbagent dev-portal identity edit --alias A [--username U]
                                 [--password P | --password-stdin]
                                 [--role-hint H] [--vendor V] [--new-alias N]
kbagent dev-portal identity use ALIAS
kbagent dev-portal identity current
kbagent dev-portal identity verify [--identity A]

kbagent dev-portal list --vendor V [--identity A]
kbagent dev-portal get --app VENDOR.APP [--identity A]

kbagent dev-portal create --vendor V (--data FILE|@FILE|-)
                          [--identity A] [--dry-run]
kbagent dev-portal patch  --app VENDOR.APP
                          (--data FILE | --property KEY (--value V | --value-file F))
                          [--identity A] [--dry-run]
kbagent dev-portal upload-icon --app VENDOR.APP --file PATH
                               [--identity A] [--dry-run]
kbagent dev-portal publish --app VENDOR.APP [--identity A] [--dry-run]
kbagent dev-portal deprecate --app VENDOR.APP [--identity A] [--dry-run]
```

Write flow (uniform across `create` / `patch` / `upload-icon` / `publish` /
`deprecate`):

```python
def cmd_patch(ctx, app, data, property_, value, value_file, identity, dry_run):
    svc = get_service(ctx, "dev_portal_service")
    formatter = get_formatter(ctx)
    alias = resolve_identity_alias(ctx, identity)
    require_permission(ctx, "dev-portal.patch")

    pending = svc.prepare_patch(alias, vendor, app_id, payload)
    render_pending(formatter, pending)            # stderr; never stdout

    if dry_run:
        formatter.output({"status": "dry-run", "diff": pending.diff_as_json()})
        return

    require_random_code_confirmation(action=f"patch {app}")
    result = svc.apply(pending)
    formatter.output({"status": "ok", "app": app,
                      "patched_keys": [d.key for d in pending.diff]})
```

`require_random_code_confirmation()` lives in `commands/_helpers.py`,
extracted from the current implementation in `commands/permissions.py`.
Single primitive used by `permissions set`, `permissions reset`, and every
DP write. Non-TTY = exit 6 with message:

> This is a production-affecting Developer Portal write. Run from a real
> terminal — there is no `--yes` bypass by design.

`--dry-run` exits 0 without prompting. This is the safe path agents call
freely.

### CLI wiring & permission registry

`cli.py` registers `dev_portal_app` Typer instance under name `dev-portal`.

`OPERATION_REGISTRY` gains:

```
dev-portal.identity-add        admin
dev-portal.identity-list       read
dev-portal.identity-edit       admin
dev-portal.identity-remove     admin
dev-portal.identity-use        write
dev-portal.identity-verify     read
dev-portal.list                read
dev-portal.get                 read
dev-portal.create              write
dev-portal.patch               write
dev-portal.upload-icon         write
dev-portal.publish             admin
dev-portal.deprecate           destructive
```

`--deny-writes` automatically blocks all writes by category. Persistent
policy can pin individual operations the usual way.

`commands/_helpers.py` gains `resolve_identity_alias(ctx, explicit)` and a
`get_dev_portal_service(ctx)` factory.

## Security

- **Bearer never persisted.** In-memory only on the client instance, scoped
  to the invocation.
- **Password persisted on disk** with the same protections as KB Storage
  tokens: 0600 file, locked dir, `_warning` header, auto-`.gitignore`.
- **No env-var bypass** for the random-code confirm. The
  `--allow-env-manage-token` precedent is intentionally *not* mirrored — DP
  credentials are persisted (no env needed for creds) and the safety bar is
  the confirm, not env-deny. An env-var override of the confirm would
  defeat the entire load-bearing safety claim.
- **Subprocess inheritance** already handled by
  `mcp_transport._build_minimal_env()` allow-list (strips `KBC_*` and
  anything not on the allow-list, so `KBC_DEVELOPERPORTAL_*` env vars
  would be stripped by default).
- **`mask_token()`** applied to any error path that might surface the
  bearer.
- **MFA prompt** opens `/dev/tty` explicitly so a redirected stdin doesn't
  cause it to hang or quietly auto-fail.

## Testing

| File | Coverage |
|------|----------|
| `tests/test_dev_portal_client.py` (new) | Mocked HTTP for login (token + MFA-session + bad creds), list/get, create/patch, icon two-hop, publish/deprecate. |
| `tests/test_dev_portal_service.py` (new) | Identity CRUD; diff correctness; publish pre-flight detection; verify-on-add. |
| `tests/test_dev_portal_cli.py` (new) | Identity lifecycle; every write refuses on non-TTY with exit 6; every write succeeds with correct random code; `--dry-run` exits 0 without prompt; `--json` shapes stable. |
| `tests/test_config_store.py` (extend) | Adding/removing/editing identities; default bookkeeping; rename; default fall-through on removal. |
| `tests/test_permissions.py` (extend) | New ops in registry; `--deny-writes` blocks them; `dev-portal.deprecate` is destructive. |
| `tests/test_helpers.py` (extend) | `require_random_code_confirmation()` extracted helper: TTY/non-TTY, correct/wrong code, EOF. |
| `tests/test_e2e.py` (extend) | Smoke `dev-portal identity list`; guarded `dev-portal list --vendor` against the test portal if `E2E_DP_USERNAME`/`E2E_DP_PASSWORD` env vars are set; skip otherwise. |

## Documentation sync (rule #17 silent-drift surfaces)

Every PR shipping this work must update:

- `src/keboola_agent_cli/commands/context.py` (`AGENT_CONTEXT`).
- `CLAUDE.md` `## All CLI Commands` section.
- `plugins/kbagent/agents/keboola-expert.md` — Rule 6 version gate,
  tool-selection matrix, inline gotchas.
- `plugins/kbagent/skills/kbagent/SKILL.md` — decision-table row for
  "manage portal property / register app".
- `plugins/kbagent/skills/kbagent/references/commands-reference.md`.
- `plugins/kbagent/skills/kbagent/references/dev-portal-workflow.md`
  (new file, mirrors the structure of `workspace-workflow.md`).
- `plugins/kbagent/skills/kbagent/references/gotchas.md` — entry tagged
  `(since v<next>)` explaining: writes always require a TTY; the agent
  must not attempt to invoke a DP write itself; agent's job ends at
  `--dry-run` and presenting the preview.
- `src/keboola_agent_cli/changelog.py` — release entry on the version
  bump that ships this.

## Out of scope for v1

- **`dev-portal sync`** that mirrors `update_properties.sh` from a
  workstation. Possible v2 if the deploy script gap becomes painful.
- **Portal admin operations** beyond `publish`/`deprecate` (token
  management, vendor management, etc.).
- **Cross-vendor moves** of existing apps. Not supported by the API in a
  clean way; out of scope.
- **Audit log** of which identity wrote what. Possible v2 by tailing
  writes into a local SQLite at `${KBAGENT_CONFIG_DIR}/dev-portal-audit.db`
  — flagged but not committed to.

## Migration / rollout

- No migration required for existing kbagent installs — new fields on
  `AppConfig` default to empty.
- The local `component-dev-portal` skill in
  `cf-claude-code-kit/plugins/component-developer/` stays in place during
  v1 rollout. After kbagent v0.44.x lands, the skill's `SKILL.md` should
  be updated to *prefer* the kbagent commands and only fall back to
  `dp.py` for behaviour kbagent doesn't yet wrap. Once kbagent parity is
  proven in real use, deprecate `dp.py` outright.

## Open questions to resolve during implementation

- **PNG dimension check.** Adding `Pillow` as a dependency just for 128×128
  validation is heavy. Soft warning via stdlib `struct` reading the PNG
  IHDR chunk is cheaper and dep-free; prefer that. Confirmed at impl time.
- **Identity `vendor` field.** Currently optional, used as a default. Worth
  considering whether an identity that has no `vendor` should refuse to
  run any operation that needs one (vs. erroring later with a clear
  message). Lean toward the latter — explicit per-command `--vendor` wins.
