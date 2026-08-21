"""Context command - provides compact usage reference for AI agents.

Outputs a curated text block that any AI agent (Claude, Codex, Gemini, etc.)
can consume to understand how to use kbagent effectively.
For detailed workflows, install the kbagent Claude Code plugin or use
`kbagent <command> --help`.
"""

import typer

from .. import __version__
from ._helpers import get_formatter

AGENT_CONTEXT = f"""\
# kbagent - Keboola Agent CLI v{__version__}

## What is kbagent?

AI-friendly CLI for managing Keboola projects. Connect to multiple projects
across stacks, browse configs/jobs/lineage, sync configs as local files,
create workspaces for SQL debugging, and manage dev branches -- all with
structured JSON output for programmatic consumption.

## IMPORTANT: Set Conversation ID

Before running any kbagent commands, set KBAGENT_CONVERSATION_ID to a unique
identifier for the current conversation/session. This is REQUIRED for platform
observability -- all API requests will include the X-Conversation-ID header.

  export KBAGENT_CONVERSATION_ID="<unique-conversation-id>"

## Quick Start

  # Add a single project
  kbagent --json project add --project my-project --url https://connection.keboola.com --token YOUR_TOKEN

  # Or bulk-onboard all projects from an organization
  KBC_MANAGE_API_TOKEN=xxx kbagent --allow-env-manage-token --json org setup --org-id 123 --url https://connection.keboola.com --yes

  # Explore
  kbagent --json project list
  kbagent --json config list

## Global Flags

  --json / -j       JSON output (always use for programmatic parsing)
  --verbose / -v    Verbose output
  --no-color        Disable colors (auto-disabled in non-TTY)
  --config-dir      Override config directory path
  --deny-writes     Session-only firewall: block the WIDE NET -- every write, destructive, AND admin op (project add/remove/edit, org setup, all storage mutations)
  --deny-destructive  Session-only firewall: NARROW -- block only data-destructive ops in Keboola (delete-table/bucket/column, terminate-job, branch delete). Admin ops (project remove, org setup) stay allowed -- use --deny-writes for those

## All Commands

Use `kbagent <command> --help` for full flag details and examples.

### Programmatic Auth (Browser Login) (since v0.80.0)

  Browser-based login (PKCE authorization-code by default; RFC 8628 device
  authorization on SSH/containers/WSL, or with --device-code) as an
  alternative to a long-lived static Storage API token.

  IMPORTANT FOR AI AGENTS: `auth login` (PKCE / device flow) REQUIRES A
  HUMAN AT A BROWSER. There is no unattended path for THAT command, and
  session tokens are deliberately not readable through the CLI -- do NOT
  attempt `auth login` from an unattended agent task. `auth login-password`
  (below) is the one deliberate, explicit exception: it is safe to run
  unattended given the credentials it needs, and is what CI/automation
  should use to get a full session (as opposed to a single-project static
  Storage token via `project add --token` / `KBAGENT_PROJECT_FROM_ENV`).

  kbagent auth login [--stack URL|alias] [--device-code] [--register-projects]
    Opens the Keboola login page in a browser (or falls back to the RFC 8628
    device flow: prints a short user_code + verification URL) and stores the
    resulting "programmatic session" (kbc_at_* access token + kbc_rt_*
    refresh token) in auth.json (0600), a sibling of config.json. A session
    is USER-scoped: one login covers every project the signed-in user can
    access; the target project for a given command is still selected the
    usual way (--project / KBAGENT_PROJECT / pin) and bound to the request
    via X-KBC-ProjectId. --stack accepts a stack URL or an existing project
    alias (its stack is used). --device-code forces the device flow even on
    a desktop with a browser. --register-projects additionally writes every
    accessible project into config.json with the sentinel token
    `kbc-session://{{project_id}}` (config.json schema and
    CURRENT_CONFIG_VERSION are unchanged; an existing alias for the same
    project+stack is left alone. A suggested alias never collides -- it is
    computed against every existing config.json key and suffixes
    `-{{project_id}}` past any clash -- so a "skipped" status only arises for
    an alias you force via --alias, or a second alias for an already-registered
    project; an existing entry is never overwritten). Without --register-projects, and on a TTY
    with human output, login now offers the same picker interactively right
    after reporting success -- see `auth register-projects` below for the
    full picker/--all/--project-id/--alias contract, which this shares.
    PKCE auto-falls-back to the device flow ONLY on a pre-exchange
    failure (no loopback browser, callback timeout, SSH/container/WSL
    detected) -- once the browser callback succeeds there is no fallback.
    A 404 from any auth endpoint means browser login is not enabled on that
    stack yet (per-stack feature flag); use a static token instead.

  kbagent auth login-password --email EMAIL (--password PASSWORD | --password-stdin) [--totp-secret SECRET] [--stack URL|alias] [--register-projects]
    Sign in via a password grant -- no browser, safe to run unattended from
    a CI secret-backed workflow. Prefer --password-stdin (or
    KBC_LOGIN_PASSWORD) over --password: a value on the command line lands
    in shell history and process listings. --password and --password-stdin
    are mutually exclusive (ConfigError if both are given). --email/
    --password/--totp-secret also read from
    KBC_LOGIN_EMAIL/KBC_LOGIN_PASSWORD/KBC_LOGIN_TOTP_SECRET env vars
    (same convention as KBC_TOKEN), so a workflow sets them once in a
    step's env: block instead of passing flags. --totp-secret is the
    account's base32 TOTP SEED (from its authenticator enrollment), NOT a
    6-digit code -- kbagent computes the current code itself
    (auth/totp.py, stdlib RFC 6238) so no human ever types a live code.
    Only resolves TOTP-based MFA this way; a WebAuthn/passkey-only account
    gets AUTH_MFA_INVALID and must use `auth login` instead (that ceremony
    needs a real browser). Stores the resulting session in auth.json
    exactly like `auth login` does -- same downstream command support,
    same `project list` "session" auth-mode column, same
    --register-projects contract. AN AI AGENT MAY run this command when
    given real credentials for this purpose (unlike `auth login`), but
    must never invent, guess, or reuse credentials from another context.
    Storing an account's password (and TOTP seed) as CI secrets is a
    bigger blast radius than one scoped project token -- use a dedicated,
    least-privileged service account, never a real human's own login.

  kbagent auth status [--stack URL|alias]
    Show the current session's state (live/refreshed/degraded/expired/missing),
    signed-in user, accessible projects, and token expiry. Proactively
    refreshes the access token if it is stale -- a healthy session routinely
    has an expired 1h access token alongside a valid 30-day refresh token.

  kbagent auth logout [--stack URL|alias] [--remove-projects] [--yes]
    Revoke the refresh token server-side and delete the local session from
    auth.json. --remove-projects also removes config.json project aliases
    pointing at this session (sentinel-token projects only -- a static-token
    project sharing the same stack is never touched).

  kbagent auth register-projects [--stack URL|alias] [--all] [--project-id ID ...] [--alias ID=ALIAS ...] [--yes]
    Register accessible projects as local config.json aliases from an
    EXISTING session, without re-running login. Fixes the usability trap
    where `login` prints an accessible-project table but nothing gets
    registered unless --register-projects was passed, and where the
    suggested alias is slugified from the project NAME -- the numeric
    project id (e.g. 9840) is never a valid alias on its own.
    --all registers every accessible project. --project-id ID (repeatable)
    registers specific ones (an id the session cannot access raises a
    ConfigError naming it). Omitting both starts an interactive arrow-key +
    spacebar checkbox picker (every not-yet-registered project preselected;
    up/down or j/k move, space toggles, 'a' selects/deselects all, enter
    accepts, q/esc/ctrl-c cancels), then a single "Edit aliases?" confirm
    (default no) that only opens the old per-project alias prompt if you opt
    in -- each row already shows its suggested alias. On a piped stdin or a
    terminal without real interactive capabilities, this falls back to the
    original typed prompt (numbers / ranges like 1-3 / 'all' / 'none'). This
    needs a real TTY and non-JSON output; in a non-interactive or --json
    context with neither --all nor --project-id, the command fails fast and
    tells you to pass one of them instead of hanging on a prompt. --alias
    ID=ALIAS (repeatable) overrides the suggested alias for a given project
    id in every mode, including as the picker's prefilled default. --yes
    skips only the picker's final "register these N projects?" confirmation.
    Never overwrites an existing config.json entry: a project already
    registered under an alias for this project+stack reports status
    "exists" (no-op, even if you request a different alias -- rename via
    `project edit --new-alias` instead); an alias already claimed by a
    DIFFERENT project or a static-token project reports status "skipped"
    with a note, never clobbered. `auth login` (without --register-projects)
    now also offers this same picker right after a successful login, when
    stdout is a TTY and --json was not passed; otherwise it just prints the
    hint to run this command later. A failure in this optional follow-up
    never changes login's own (already-successful) exit code.

  Storage posture: session tokens live in PLAINTEXT in auth.json (0600), a
  sibling of config.json -- the same posture as the static Storage tokens
  already kept there (deliberate RFC 8628 deviation; see
  docs/programmatic-auth-login-plan.md section 4.2).

  v1 scope: the Storage + Manage paths. `kbagent serve` reaches them too --
  it delegates to the same already-guarded services -- so a session project
  is usable over the REST API and web UI. Two consequences: whoever holds
  KBAGENT_SERVE_TOKEN acts as the signed-in USER (a session is a user
  credential, not a project one), and a session that expires while the
  server runs answers HTTP 401 with error_code SESSION_EXPIRED, which only a
  human on the host can fix by re-running `auth login`.

  These surfaces fail fast on a sentinel-token (`kbc-session://...`) project
  with AUTH_NOT_SUPPORTED_ON_STACK, naming the static-token fallback -- they
  do not (yet) understand bearer sessions. SESSION_UNSUPPORTED_FEATURES in
  services/_auth_registration.py is the in-code copy of this list. `auth login`
  and `auth register-projects` print it, and both ship it in --json as the
  additive key session_unsupported_features (`auth status` does NOT carry it):
    - kbagent kai
    - kbagent semantic-layer (Metastore Service)
    - kbagent data-app (Data Science Service)
    - kbagent stream (Data Streams Service)
    - kbagent sharing, unless a master token is set in the environment
    - AI Service paths: docs query, config examples, config new,
      component detail/search, flow new/update/validate
    - Scheduler Service paths: flow schedule, flow schedule-remove
    - the importable SDK (keboola_agent_cli.Client)

  `dev-portal` is NOT on that list: it authenticates with its own Developer
  Portal identity (`dev-portal identity add`), never a project token, so a
  session project changes nothing there. `flow` splits -- `flow list` /
  `flow detail` are plain Storage calls and work.

  An older kbagent build has no
  sentinel-token support at all: it gets an opaque 401 on a plain
  `X-StorageApi-Token` call, or a different failure on a token-as-data path
  (e.g. semantic-layer `token --encrypt`, kai, sharing's master-token
  fallback) -- upgrade first.

  New error codes: AUTH_NOT_SUPPORTED_ON_STACK, AUTH_FLOW_TIMEOUT,
  AUTH_FLOW_DENIED, AUTH_FLOW_EXPIRED, AUTH_BROWSER_UNAVAILABLE,
  AUTH_STATE_MISMATCH, SESSION_EXPIRED, SESSION_NOT_FOUND. Exit codes:
  SESSION_EXPIRED / SESSION_NOT_FOUND / AUTH_FLOW_DENIED -> 3 (auth error);
  AUTH_FLOW_TIMEOUT -> 4 (network error); others -> 1. A session refresh that
  times out or cannot reach the auth service reports TIMEOUT /
  CONNECTION_ERROR -> 4, never a session/config error: a slow auth service is
  a network problem, so do NOT treat it as "log in again". The refresh is a
  single attempt under a short budget by design (a retry would re-present the
  same refresh token); just run the command again.

  In multi-project commands (`data-app list`, `flow list`, `storage tables`)
  a per-project failure keeps its real error_code in the --json `errors[]`
  array instead of being relabelled UNEXPECTED_ERROR.
  A session project on an unsupported surface therefore reports
  error_code AUTH_NOT_SUPPORTED_ON_STACK per project -- branch on that code
  to auto-remediate (register a static-token alias) rather than parsing the
  message text. Only an exception carrying no code at all gets the fallback,
  and its message is truncated because its content is unknown.

### Project Management

  kbagent project add --project NAME --url URL --token TOKEN
    Add a new project connection. Token verified against API.

  kbagent project list
    List all connected projects (tokens always masked). An `Auth` column
    (before `Token`) shows how each project authenticates, and every --json
    entry carries `auth_mode`: exactly "session" (browser login) or "static"
    (Storage token). The field is always present and never empty, including on
    a row synthesized for a project whose connection failed, so a consumer can
    depend on it instead of testing for absence. This is the answer to "which
    mode is this project in":
      kbagent project list --json | jq '.data[].auth_mode'
    In the human table a session project's `Token` cell is a dash: the stored
    sentinel is not a credential, and masked (`kbc-...9840`) it reads like a
    truncated real token. The --json `token` value is unchanged -- still
    mask_token(...) -- because that is a stable contract; the sentinel's body
    is the project id, which has its own column.

  kbagent project remove --project NAME
    Remove a project connection.

  kbagent project edit --project NAME [--url URL] [--token TOKEN] [--new-alias NEW]
    Edit project connection. Re-verifies token if changed. --new-alias renames
    the alias and cascades the rename through config.json and the nested sync
    directory at <cwd>/<old-alias>/. Lineage cache embeds the alias in FQNs
    and is NOT auto-updated; rebuild via `kbagent lineage build` after rename.
    Passing --token for a browser-login (session) project is allowed and
    converts it to a static-token project, but it emits a warning: once
    converted, `auth logout --remove-projects` no longer cleans that alias up
    (use `project remove`). In --json mode the warning is carried in an
    additive top-level "warnings" array; --dry-run previews the same text.

  kbagent project status [--project NAME]
    Test connectivity. Shows OK/ERROR with response time. Carries the same
    `auth_mode` field (and an `Auth` column right after `Status`) as
    `project list`, including on an ERROR row.

  kbagent project refresh --project ALIAS [--dry-run] [--force] [--yes] [--token-description ...] [--token-expires-in N]
  kbagent project refresh --all [--dry-run] [--force] [--yes] [--token-description ...] [--token-expires-in N]
    Refresh project tokens via Manage API. --all refreshes all projects. --force replaces non-expiring tokens.
    Browser-login (session) projects are reported under "skipped" with the
    reason that there is no static token to replace -- their access token
    rotates on its own from auth.json. --force does NOT convert them either;
    use `project edit --token` for a deliberate one-project conversion.

  kbagent project description-get --project NAME
    Read the Keboola dashboard project description (markdown). Backed by
    the KBC.projectDescription metadata key on the default branch. Returns
    an empty string if no description has been set.

  kbagent project description-set --project NAME [--text STR | --file PATH | --stdin]
    Set the dashboard project description. Pass exactly one of --text,
    --file, or --stdin. Writes KBC.projectDescription to the default branch.

  kbagent project use ALIAS
    Pin ALIAS as the default project. Persists to config.json.
    Env var KBAGENT_PROJECT=ALIAS overrides the pin for a single shell/session;
    an explicit --project flag overrides both.

  kbagent project current
    Print the effective default project and its source (env / pin / none).
    Resolution order for single-project operations: --project > KBAGENT_PROJECT > pin.

  kbagent project info --project NAME
    Return full project details: ID, name, stack URL, default backend, enabled features,
    quota limits, and usage metrics. Useful for auditing project capabilities.
    Carries `auth_mode` too, rendered as an `Auth` row above the Token rows --
    on a session project those describe the rotating access token.
    `project current`, `project add` and `project edit` do NOT carry
    `auth_mode`: `current` answers which alias is effective (and reports a
    KBAGENT_PROJECT override that may name an alias absent from the config),
    and the other two are write confirmations.

### Project Members & Invitations (since v0.29.0)

  Requires KBC_MANAGE_API_TOKEN (Manage API auth). Allowed roles: admin, guest, readOnly, share.

  kbagent project invite --project ALIAS --email EMAIL --role ROLE [--reason TEXT] [--dry-run]
    Send an invitation email. Re-inviting an existing invitee or member is a no-op
    (HTTP 400 from the Manage API; the service returns status="noop" with note
    "already_invited" / "already_member").

  kbagent project invite --from-csv FILE [--default-role ROLE] [--workers N] [--dry-run]
    Bulk invite. CSV must have a header row with columns: email, project (alias or
    numeric ID), role (optional if --default-role is given), reason (optional).
    Parallelised with ThreadPoolExecutor (default 8 workers). Per-row results in
    `rows[]` with status=ok|noop|failed; `failed_rows` ordering is not deterministic.

  kbagent project member-list --project ALIAS [--include-pending]
    List active project members. --include-pending also fetches pending invitations.

  kbagent project invitation-list --project ALIAS
    List pending (unaccepted) invitations only.

  kbagent project invitation-cancel --project ALIAS --email EMAIL [--invitation-id ID] [--yes]
    Cancel a pending invitation. Without --invitation-id, the service resolves the
    ID by listing pending invitations and matching --email (case-insensitive).

  kbagent project member-remove --project ALIAS --email EMAIL [--yes]
    Remove an active member (destructive). Service resolves --email to user_id.

  kbagent project member-set-role --project ALIAS --email EMAIL --role ROLE
    Change an existing member's role via PATCH /manage/projects/{{id}}/users/{{userId}}.

### Component Discovery

  kbagent component list [--project NAME] [--type TYPE] [--query "search"]
    List or AI-search available components. --type: extractor, writer, transformation, application.

  kbagent component detail --component-id ID [--project NAME]
    Show component docs, config schema, and examples count.

  kbagent component sync-action ACTION_NAME --component-id ID --project ALIAS (--config-id ID [--row-id ID] | --config-data JSON|@file|-) [--branch ID] [--timeout N]
    (since 0.73.0) Run a synchronous component action (testConnection, getTables,
    ...) on the dedicated sync-actions service. ACTION_NAME is freeform --
    valid names are component-defined (see component detail synchronous_actions).
    --row-id shallow-merges the row over the root config at TOP level only
    (row parameters/storage keys replace root wholesale -- NOT a deep merge;
    MCP run_sync_action parity). --config-data sends explicit configData
    verbatim and skips the config fetch. Response shape is action-specific
    (opaque pass-through).

### Configuration Browsing

  kbagent config list [--project NAME] [--component-type TYPE] [--component-id ID] [--branch ID] [--include-rows]
    List configs from one/many/all projects. --project repeatable. Branch-aware.
    --include-rows extends each row with full configuration + rows bodies
    (significantly larger payload -- use only when bodies are needed).

  kbagent config detail --project NAME [--project NAME ...] --component-id ID [--config-id ID] [--branch ID] [--with-state]
    Full config detail. TWO MODES:
      - Single (--config-id given): returns the full config dict (shape unchanged).
      - Bulk (--config-id omitted): returns {{"configs": [...], "errors": [...]}}
        with every config of --component-id. --project repeatable for multi-project
        fan-out (one API call per project, not per config).
    --with-state attaches the runtime state dict. Single: dedicated get_config_state
    call. Bulk: include=state ridesalong (no N+1, still one call per project).
    --branch requires exactly one --project (branch IDs are per-project).
    Sandbox annotation (since v0.42.0, single-config mode only):
    --component-id keboola.sandboxes adds a sandbox_annotation block with
    sandbox_service_id (the misleading parameters.id) and storage_workspace_id
    (the actual Storage workspace ID -- resolved via workspace list lookup).
    Use storage_workspace_id with `kbagent workspace detail --workspace-id ID`,
    NOT parameters.id (which 404s).

  kbagent config update --project NAME --component-id ID --config-id ID [--name N] [--description D] [--configuration JSON|@file|-] [--configuration-file PATH] [--set PATH=VALUE ...] [--merge] [--change-description TEXT] [--dry-run] [--branch ID] [--allow-plaintext-on-encrypt-failure]
    Update config metadata and/or configuration content. --set targets a
    nested key (e.g. parameters.db.host=new-host). --merge deep-merges into
    existing config (preserves sibling keys). --dry-run previews changes.
    Paths are always relative to the configuration root. #-prefixed secrets
    auto-encrypt via the Encryption API before write (fail-closed; since
    0.54.0); --allow-plaintext-on-encrypt-failure overrides. --dry-run keeps
    plaintext in the diff. Note --set '#k=v' sets a top-level key; for a
    nested secret use --set 'parameters.#k=v'.
    Auto-normalize (0.28.0+; #245): parameters.blocks[].codes[].script
    strings are silently rewritten to arrays before pushing to Storage --
    SQL transformations split on statement boundaries (state machine
    respects 'string' / "ident" / $$..$$ / -- / # / // / /* */); Python /
    R / kds-team.app-custom-python wrap as [script]. The result envelope
    gains a normalizations: [{{path, action, before_type, after_type,
    after_length}}] field listing every change (empty when input was
    already valid). Closes the runtime "Expected array, got string" trap
    that the lax Storage API silently lets through.

  kbagent config set-default-bucket --project NAME --component-id ID --config-id ID (--bucket BUCKET_ID | --clear) [--dry-run] [--branch ID]
    Set or clear configuration.storage.output.default_bucket on a config without
    raw-mode JSON edits. --bucket/--clear are mutually exclusive. Read-modify-write
    that preserves sibling keys under storage.output and the rest of the config.
    No-op (with changed=false) when the value is already what you'd be setting.

  kbagent config rename --project NAME --component-id ID --config-id ID --name "New Name" [--branch ID] [--directory DIR]
    Rename a configuration. Updates name via API. If a local sync directory
    exists (.keboola/manifest.json), renames the directory and updates the
    manifest path. Uses git mv when inside a git repo for cleaner history.

  kbagent config delete --project NAME --component-id ID --config-id ID [--branch ID]
    Delete a configuration. Branch-aware.

  kbagent config new --component-id ID [--name NAME] [--project NAME] [--output-dir DIR]
                     [--push --no-files --description D --configuration JSON|@file|- --configuration-file PATH --no-validate --branch ID --dry-run --allow-plaintext-on-encrypt-failure]
    Default: generate boilerplate config from component schema (scaffold to --output-dir or stdout).
    With --push (0.33.0+): also create the config remotely via Storage API in one shot.
    --push requires --project AND a non-empty --name. --no-files skips the filesystem step
    entirely for FIIA-style one-shot creates. Schema validation runs by default when an explicit
    --configuration body is given (fail-closed; --no-validate opts out). Default body is {{}}
    (empty shell, validation auto-skipped). Works for ALL component types including
    keboola.snowflake-transformation.

  kbagent config clone --project P --component-id ID --config-id ID --name NAME
                       [--target-project P2] [--description D] [--set PATH=VALUE ...]
                       [--secret PATH=VALUE ...] [--branch ID] [--target-branch ID]
                       [--dry-run] [--allow-plaintext-on-encrypt-failure]
    (since 0.84.2) Duplicate a configuration WHOLE. Use this instead of reading config detail
    and rebuilding a body by hand -- that drops siblings of parameters (runtime, storage,
    authorization) silently, and a lost runtime.parallelism means Keboola falls back to
    parallelism 1 (issue #587).
    Same project (default): server-side copy; rows and KBC:: encrypted values come along.
    Cross project (--target-project): reassembled client-side. Encrypted values CANNOT travel
    (ciphertext is project-scoped), so the clone is REFUSED until each path is re-supplied via
    --secret PATH=VALUE; they are encrypted in the TARGET project on write. Run --dry-run first
    to list exactly which paths need one. Storage bucket/table IDs are copied verbatim, NOT
    remapped -- use sync clone for that.

  kbagent config search --query PATTERN [--project NAME] [--component-type TYPE] [-i] [-r] [--branch ID]
    Search config bodies for string/regex. Reports match location in JSON tree. Branch-aware.

  kbagent config examples --component-id ID [--project NAME] [--row]
    (since 0.73.0) Sample root/row configurations for a component, straight from
    the AI-service component detail (same data the UI shows). --row limits to
    row examples. --json emits {{component_id, root_examples, row_examples}} --
    structured dicts, ideal as a starting point before config new / row-create.

  kbagent config variables-set --project NAME --component-id ID --config-id ID --var KEY=VALUE [--var ...] [--replace] [--variables-id ID] [--values-id ID] [--branch ID] [--dry-run]
    Assign variables to any config. Auto-creates the backing keboola.variables + default row
    on first call and links the parent; subsequent calls update the same row (merge by default;
    --replace for full overwrite). Prefix KEY with # to auto-encrypt as a secret.

  kbagent config variables-get --project NAME --component-id ID --config-id ID [--branch ID]
    Read variable values attached to a config. Returns linked, variables_id, values_id, values.

  kbagent config variables-clear --project NAME --component-id ID --config-id ID [--branch ID] [--yes]
    Unlink variables from a config. Does NOT delete the underlying keboola.variables config
    (it may be shared). Delete it explicitly via `kbagent config delete` if needed.

### Config Metadata (folder organisation + arbitrary key/value)

  kbagent config metadata-list --project NAME --component-id ID --config-id ID [--branch ID]
    List all metadata entries on a configuration. Each entry: id, key, value, provider, timestamp.

  kbagent config get-metadata --project NAME --component-id ID --config-id ID --key KEY [--branch ID]
    Read a single metadata value by key. Exits 1 (NOT_FOUND) if absent.

  kbagent config set-metadata --project NAME --component-id ID --config-id ID --key KEY --value VALUE [--branch ID]
    Set (upsert) a metadata key/value on a configuration.

  kbagent config delete-metadata --project NAME --component-id ID --config-id ID --metadata-id ID [--branch ID] [--yes]
    Delete a configuration metadata entry by numeric ID (from metadata-list).

  kbagent config set-folder --project NAME --component-id ID --config-id ID --name "FolderName" [--branch ID]
    Sugar: writes KBC.configuration.folderName metadata. Groups the config in the Keboola UI.
    Pass --name "" to remove the folder assignment.

  kbagent config row-create --project NAME --component-id ID --config-id ID --name ROW_NAME [--description D] [--configuration JSON|@file|-] [--is-disabled] [--branch ID] [--allow-plaintext-on-encrypt-failure]
    Create a new configuration row. Returns the new row ID. Optional --configuration accepts JSON inline, @file, or stdin. #-prefixed secrets auto-encrypt before write (fail-closed; since 0.54.0).

  kbagent config row-update --project NAME --component-id ID --config-id ID --row-id ID [--name N] [--description D] [--configuration JSON|@file|-] [--change-description TEXT] [--is-disabled | --is-enabled] [--branch ID] [--allow-plaintext-on-encrypt-failure]
    Update an existing configuration row. Pass only the fields you want to change. #-prefixed secrets auto-encrypt before write (fail-closed; since 0.54.0).

  kbagent config row-delete --project NAME --component-id ID --config-id ID --row-id ID [--branch ID] [--yes]
    Delete a configuration row. Destructive; --yes to skip confirmation prompt.

  kbagent config oauth-url --project NAME --component-id ID --config-id ID [--redirect-url URL]
    Return the OAuth authorization URL for a component that uses OAuth authentication.
    Open the URL in a browser to complete the OAuth flow.

  kbagent config state-get --project NAME --component-id ID --config-id ID [--row-id ID] [--branch ID]
    Read a configuration's runtime state (the same dict --with-state attaches
    to config detail). Without --row-id returns the root config's state; with
    --row-id returns that row's state. For row-based components the root
    state is unused -- read/write the row state instead.

  kbagent config state-set --project NAME --component-id ID --config-id ID [--row-id ID] --state JSON|@file|- [--branch ID] [--dry-run] [--yes]
    Write a configuration's runtime state via the dedicated PUT .../state
    endpoint (branch-scoped). --state must be a JSON object under 4 MB.
    --row-id targets a row's state instead of the root. --dry-run previews
    the current-vs-new diff without writing; no-op (changed=false) when the
    new state equals the current one. Guarded write: prompts for
    confirmation unless --yes or --json. Use this to seed/reset/backfill
    state (e.g. seeding a dev branch's lastImportId before testing
    changed_since: adaptive) -- `config update --set 'state...'` does NOT
    reach this endpoint (see gotchas).

### Cross-Project Search

  kbagent search QUERY [--project NAME] [--type table|bucket|config|flow|data-app|transformation] [--search-type textual|config-based] [--regex] [--limit N]
    Search for items across one or more projects. Textual mode (default) searches item names
    via the Storage API global-search endpoint. Config-based mode scans full configuration JSON bodies.
    --type is repeatable. --limit applies per project in textual mode (1-100, default 50).
    --regex (0.67.0+): opt-in regex mode. Case-insensitive whole-term match on ENTITY NAMES only
    ('report' != 'monthly_report'; use '.*report.*'). Textual only (error with --search-type config-based);
    regex does NOT match columns, so matched_columns stays empty under --regex. In textual mode, tables
    matched via a column name carry matched_columns (JSON) / a "Matched columns" column.
    BOTH modes match case-insensitively; use `kbagent config search --query` for a case-sensitive
    body scan (it has its own --ignore-case).

### Job History

  kbagent job list [--project NAME] [--component-id ID] [--config-id ID] [--status STATUS] [--limit N]
    List jobs from Queue API. --status: processing, terminated, cancelled, success, error.

  kbagent job detail --project NAME --job-id ID
    Full job detail including result message and timing.

  kbagent job run --project NAME --component-id ID --config-id ID [--row-id ID ...] [--wait] [--timeout N] [--branch ID] [--mode run|debug] [--variable-values-id ID] [--no-variables] [--poll-strategy exponential|fixed] [--log-tail-lines N] [--idempotency-key KEY] [--force-rerun]
    Run a Queue API job. --row-id selects specific config rows (repeatable; omit to run entire config).
    --wait polls until job finishes. --timeout sets max wait in seconds (default 300). Branch-aware.
    When the config has linked variables (configuration.variables_id), kbagent auto-resolves
    a variableValuesId so the job binds to the deployed values row. --variable-values-id
    overrides; --no-variables skips resolution. Error code NO_VARIABLE_ROWS when the linked
    variables config has zero rows (run `kbagent config variables-set` to create one).
    Polling under --wait uses an exponential curve by default (2s x 30 -> 5s x 48 -> 15s);
    --poll-strategy fixed keeps a constant 1s interval. On FAILED/WARNING/TERMINATED, the last
    --log-tail-lines events (default 200, 0 disables -- recommended for automation pipelines) are
    surfaced as `logTail` in --json output.
    --mode run (default) writes to mapped output tables. --mode debug runs the component but
    redirects the output to a Storage File tagged `debug-<jobId>` instead of into destination
    buckets -- safe for dry-runs and for reproducing a failing run on a production configuration
    without touching production data. Invalid values exit 2 via Click choice gate (since v0.43.6).
    --json response shapes by exit code:
      - exit 0 (success): {{status:"ok", data:{{..., logTail?:[...]}}}}
      - exit 1 (QUEUE_JOB_FAILED, remote job status=error):
        {{status:"error", error:{{code:"QUEUE_JOB_FAILED", details:{{logTail:[...]}}}}}}
      - exit 4 (QUEUE_JOB_TIMEOUT, local timeout + remote kill also failed):
        {{status:"error", error:{{code:"QUEUE_JOB_TIMEOUT", retryable:true, details:{{logTail:[...]}}}}}}
      - exit 7 (JOB_TIMEOUT_TERMINATED, local timeout + remote kill succeeded):
        {{status:"error", error:{{code:"JOB_TIMEOUT_TERMINATED", details:{{job:{{...}}, logTail:[...]}}}}}}
    jq pattern: `.error.details.logTail? // .data.logTail? // []` picks up the tail regardless of exit.

  kbagent job terminate --project NAME (--job-id ID [--job-id ID ...] | --status any|created|waiting|processing [--component-id ID] [--config-id ID] [--branch ID] [--limit N]) [--dry-run] [--yes]
    Kill running jobs via Queue API (POST /jobs/{{id}}/kill). Use to stop runaway loops or pile-ups.
    Two modes: single/batch by --job-id, or bulk by --status. --status any covers all killable states
    (created+waiting+processing). Response partitions into killed / already_finished / not_found / failed.
    Idempotent: re-running on terminal jobs reports them as already_finished rather than failing.

### Storage

Note on branches: storage READ commands (buckets, bucket-detail, tables,
table-detail, files, file-detail) use the production endpoint by default,
even when a dev branch is active via `branch use`. The Storage API
branch-scoped endpoint returns only resources that were locally modified
in the dev branch, so a freshly-created branch lists nothing. Pass
`--branch ID` explicitly to query dev-branch-local tables/buckets.
Storage WRITE commands (create-*, upload-*, delete-*, file-upload, etc.)
remain branch-aware because modifying a dev branch is the expected intent.

  kbagent storage buckets [--project NAME] [--branch ID]
    List buckets with sharing/linked info. Shows source project for linked buckets.
    Uses production by default; pass --branch to query a dev branch explicitly.

  kbagent storage bucket-detail --project NAME --bucket-id BUCKET_ID [--branch ID]
    Bucket detail with backend-native direct-access paths. Resolves linked bucket source DB/dataset.
    Output adapts to the bucket's backend:
      - Snowflake -> snowflake_database / snowflake_schema / per-table snowflake_path quoted with "..."
      - BigQuery  -> bigquery_dataset (and bigquery_project when surfaced via API databaseName) /
                     per-table bigquery_path quoted with backticks `dataset`.`table` (or
                     `project`.`dataset`.`table` when project is known).
    Always-present backend-agnostic keys: sql_dialect ("snowflake" | "bigquery") and per-table sql_path.
    Prefer sql_path/sql_dialect in agent code instead of branching on backend yourself.
    Uses production by default; pass --branch to query a dev branch explicitly.

  kbagent storage tables [--project NAME ...] [--bucket-id BUCKET_ID] [--branch ID]
    List storage tables from one or more projects (in parallel). Omit --project
    to query all connected projects. Repeat --project for a specific subset.
    Multi-project by default, matching `storage buckets`, `config list`, `job list`.
    Each row is tagged with project_alias; per-project errors accumulate in the
    response envelope. --branch is only valid with a single --project.
    --bucket-id is applied independently per project; missing buckets are
    reported as per-project errors, not fatal.
    Uses production by default; pass --branch to query a dev branch explicitly.

  kbagent storage table-detail --project NAME --table-id TABLE_ID [--branch ID]
    Show detailed table info: columns (with types if available), primary key, row count, size, last import date.
    Uses production by default; pass --branch to query a dev branch explicitly.
    Also surfaces the raw Storage API `definition` (0.88.0+, #621) -- for a BigQuery table
    that is the ONLY readable record of the registered timePartitioning / rangePartitioning /
    clustering layout, so it is how you VERIFY a `create-table --source-table-id` +
    `swap-tables` repartition actually landed (`create-table` only echoes what you REQUESTED).
    Human mode prints Time partitioning / Range partitioning / Clustering / Partition filter
    required / Partitions (a COUNT; --json carries the full partitions[] list, one entry per
    physical partition -- thousands on a long-lived daily table). `definition` is present on
    EVERY response, untyped tables included, so null means the stack omitted it, NOT "untyped".
    `storage tables` (the list endpoint) has no layout: the API offers no `definition` include.

  kbagent storage create-bucket --project NAME --stage STAGE --name BUCKET_NAME [--description D] [--backend B] [--branch ID]
    Create a new storage bucket. Stage must be "in" or "out". Branch-aware.
    On projects WITHOUT the `storage-branches` feature (legacy fake-branch), --branch
    writes succeed at the API level but the transformation runner ignores the bucket
    and creates a parallel `out.c-<branch_id>-*` bucket in the default branch at job
    time. Response includes `legacy_branch_storage: true` and human mode prints a
    warning when this applies. See storage-types-workflow.md.

  kbagent storage create-table --project NAME --bucket-id BUCKET_ID --name TABLE_NAME [--column col:TYPE[(length)] ...] [--primary-key COL] [--not-null COL ...] [--default NAME=VALUE ...] [--source-table-id ID] [--source-branch-id N] [--time-partitioning-type DAY|HOUR|MONTH|YEAR] [--time-partitioning-field COL] [--time-partitioning-expiration-ms MS] [--range-partitioning-field COL --range-partitioning-start S --range-partitioning-end E --range-partitioning-interval I] [--clustering-field COL ...] [--branch ID] [--if-not-exists]
    Create a typed table. --column repeatable.
    - --if-not-exists (since 0.47.0): opt-in idempotency. On a duplicate-display-name failure,
      probe get-table-detail at the expected id and, if the table really exists, return
      `action: "skipped", skip_reason: "table already exists"` instead of raising. A different
      table with the same display name still surfaces the original error. Safe for parallel workers.
      Since 0.47.1: the skipped envelope reports the EXISTING table's actual `columns`/`primary_key`/`name`
      (not the request); requested values are mirrored under `requested_columns`/`requested_primary_key`,
      and `schema_drift: true` flags when the existing table diverges from what was requested.
    - Base types: STRING, INTEGER, NUMERIC, FLOAT, BOOLEAN, DATE, TIMESTAMP. Type defaults to STRING if omitted.
    - Native backend types with length pass through to the Storage API: VARCHAR(40), NUMBER(18,2), CHAR(10), TIMESTAMP_TZ, TIMESTAMP_NTZ, VARIANT, OBJECT, ARRAY, etc.
      The API validates type/length per backend; e.g. INTEGER(10) is rejected with "'10' is not valid length for INTEGER".
    - --not-null COL marks a column NOT NULL (nullable=false). Must match a defined --column name.
    - --default NAME=VALUE sets a DEFAULT expression. Booleans must be lowercase (true/false).
    - --source-table-id (since 0.66.0, BigQuery only): create the table by COPYING an existing
      table's data into the requested partition/clustering layout instead of from --column specs.
      The schema is derived from the source, so --column (and --not-null/--default) must NOT be used;
      the two are mutually exclusive. --source-branch-id resolves the source in another branch.
      This is the supported way to repartition a populated BigQuery table -- then promote it with
      `storage swap-tables`. Aliases and linked-bucket tables are valid sources.
    - Partition/clustering layout (since 0.66.0, BigQuery only; works in BOTH --column and
      --source-table-id mode): --time-partitioning-type (DAY/HOUR/MONTH/YEAR; required when any
      --time-partitioning-* is set) + optional --time-partitioning-field/-expiration-ms; OR
      --range-partitioning-field/-start/-end/-interval (all four required together; range bounds
      are strings). Time and range partitioning are mutually exclusive. --clustering-field repeatable.
    - BigQuery pre-flight guard: when any source/partition/clustering flag is used, create-table
      verifies the project backend first and fails fast (exit 2) on a non-BigQuery project before
      issuing the create. Plain --column creates are unaffected.
    - In a dev branch, the bucket is auto-materialized if it has not yet been written to in the branch
      (response includes auto_created_bucket=true). Mirrors the official Keboola Go CLI's EnsureBucketExists.
    - Auto-materialized buckets get KBC.createdBy.branch.id system metadata stamped on them,
      so transformation runners on branched-storage projects accept them as output destinations.
      A 403/5xx on the metadata write is logged and the create-table call still proceeds.
    - On legacy fake-branch projects (no `storage-branches` feature), response carries
      `legacy_branch_storage: true` and human mode prints a warning. The bucket and
      metadata stamp still happen but the runner will not use them -- it creates a
      parallel `out.c-<branch_id>-*` bucket in the default branch at job time.
    Branch-aware. Examples:
      --column pk:VARCHAR(40) --column amount:NUMERIC(18,2) --not-null pk --default amount=0
      --column ts:TIMESTAMP_TZ --column meta:VARIANT
      # BigQuery repartition: copy a populated table into a new layout, then swap it in place
      --name events_repart --source-table-id in.c-main.events --time-partitioning-type DAY \\
        --time-partitioning-field created_at --clustering-field tenant_id --primary-key id
      then: kbagent storage swap-tables --table-id in.c-main.events --target-table-id in.c-main.events_repart --branch ID

  kbagent storage upload-table --project NAME --table-id TABLE_ID --file PATH [--incremental] [--delimiter D] [--enclosure E] [--no-auto-create] [--branch ID]
    Upload CSV into a table. Auto-creates bucket and table if missing (columns inferred as STRING from CSV header).
    Use --no-auto-create to require the table to already exist.
    Full load by default; --incremental to append rows. Supports files up to 5 GB via async file-first upload flow. Branch-aware.

  kbagent storage download-table --project NAME --table-id TABLE_ID [--output FILE] [--columns COL ...] [--limit N] [--where-column COL --where-value VAL ... [--where-operator eq|neq]] [--changed-since WHEN] [--changed-until WHEN] [--branch ID]
    Export table data to a local CSV file. Async export with streaming download.
    --where-column + --where-value (repeatable) + --where-operator eq|neq filter rows; --changed-since/--changed-until (unix ts or strtotime) filter by import time.
    Default filename: TABLE_NAME.csv. Use --columns to select columns (see table-detail for names).
    Use --limit to cap row count. Handles sliced files and gzip decompression transparently. Branch-aware.
  kbagent storage add-column --project NAME --table-id ID --column COL:TYPE[(length)] [--not-null] [--default VALUE] [--branch ID]
    Add a single column to an existing table (synchronous). Same name:TYPE(length) grammar as create-table --column.

  kbagent storage delete-table --project NAME --table-id ID [--table-id ...] [--force] [--dry-run] [--yes] [--branch ID]
    Delete one or more tables. Batch: repeat --table-id. --force to cascade-delete aliased tables. --dry-run to preview. Branch-aware.

  kbagent storage truncate-table --project NAME --table-id ID [--table-id ...] [--dry-run] [--yes] [--branch ID]
    Truncate one or more tables (delete all rows; preserve schema, primary key, descriptions, sharing edges, and dependents).
    Batch: repeat --table-id. Endpoint is async-via-job on every branch (the client polls to completion before returning;
    do not pass async=true -- the API rejects it). Idempotent (truncating an empty table is a no-op). Use this when re-seeding
    a table without losing the schema contract.

  kbagent storage delete-column --project NAME --table-id ID --column COL [--column ...] [--force] [--dry-run] [--yes] [--branch ID]
    Delete one or more columns from a table. Batch: repeat --column. --force when column is referenced by aliases. --dry-run to preview. Branch-aware.

  kbagent storage delete-bucket --project NAME --bucket-id ID [--bucket-id ...] [--force] [--dry-run] [--yes] [--branch ID]
    Delete one or more buckets. --force cascade-deletes tables. Linked/shared buckets protected. Branch-aware.

  kbagent storage swap-tables --project NAME --table-id ID --target-table-id ID --branch ID [--dry-run] [--yes]
    Swap two storage tables in any branch, including the default/production branch (POST /tables/{{id}}/swap). Both tables exchange physical positions;
    aliases are NOT transferred (they keep pointing at the same physical position and therefore expose the
    OTHER table's data after the swap). Use to promote a typed rebuild back into the original name without
    touching downstream config references. branch_id is mandatory (--branch or active branch via 'kbagent
    branch use'); service guards before any HTTP call when none is set. Any branch works, INCLUDING the
    default/production branch -- a default-branch swap is how a typed rebuild reaches prod (dev-branch merge
    does not carry storage schema).

  kbagent storage clone-table --project NAME --table-id ID --branch ID [--dry-run]
    Clone (pull) a production table into a dev branch (POST /tables/{{id}}/pull). On storage-branches projects a
    dev branch reads prod tables transparently until first write, so mutating a table's schema in the branch
    (swap-tables, dropping columns) first needs a branch-local copy. This materializes that copy (one-way:
    default -> branch). Branch is mandatory; service guards before any HTTP call when no branch is set.

### Table Snapshots (point-in-time backup + restore-as-new-table)

  kbagent storage snapshot-create --project NAME --table-id ID [--description D] [--branch ID]
    Create a snapshot of a table: data + columns + primary key at a point in time (async job; polls to
    completion). The receipt carries the new snapshot_id -- keep it, restores are addressed by it.

  kbagent storage snapshots --project NAME --table-id ID [--limit N] [--branch ID]
    List snapshots of a table (id, createdTime, description, creator). Production endpoint by default.

  kbagent storage snapshot-detail --project NAME --snapshot-id ID
    One snapshot's detail. Snapshot IDs are global (not table-scoped); the detail includes the source
    table object (id, columns, primaryKey), so this is how a bare snapshot ID is traced back to its table.

  kbagent storage table-from-snapshot --project NAME --snapshot-id ID --bucket-id ID --name NAME [--branch ID] [--dry-run]
    Create a NEW table from an existing snapshot (snapshot restore; async job). Restores the snapshot's
    data, columns, and primary key into --bucket-id under --name. --name is REQUIRED (the API rejects an
    omitted/empty name). The destination bucket must exist; a table with the same name must not (no
    overwrite semantics -- restore under a new name, verify, then swap or delete the old table yourself).
    Goes through the classic tables-async endpoint, NOT tables-definition -- so it is a separate command,
    not a flag on create-table.

  kbagent storage snapshot-delete --project NAME --snapshot-id ID [--snapshot-id ...] [--dry-run] [--yes]
    Delete one or more snapshots (destructive: forecloses restores; source tables untouched).
    Batch-tolerant: one failure does not abort the rest; exit 1 if any ID failed.

### Storage Descriptions

  kbagent storage describe-bucket --project NAME --bucket-id ID [--text STR | --file PATH | --stdin] [--branch ID]
    Set the KBC.description metadata on a bucket (upsert). Visible in bucket-detail.

  kbagent storage describe-table --project NAME --table-id ID [--text STR | --file PATH | --stdin] [--branch ID]
    Set the KBC.description metadata on a table (upsert). Readable via table-detail --json .data.description.

  kbagent storage describe-column --project NAME --table-id ID --column NAME=DESC [--column ...] [--branch ID]
    Set per-column descriptions stored as KBC.column.{{name}}.description in table metadata (upsert).
    Readable via table-detail --json .data.column_details[].description.

  kbagent storage describe-batch --project NAME --from-file YAML [--branch ID]
    Apply bucket/table/column descriptions from a YAML file. Sections: buckets, tables, columns (all optional).
    Failures collected; one error does not abort remaining items.

### Storage Files

  kbagent storage files --project NAME [--tag TAG ...] [--limit N] [--offset N] [--query Q] [--branch ID]
    List Storage Files. --tag filters by tags (AND logic, repeat for multiple). --query for full-text search on name.
    Uses production by default; pass --branch to query a dev branch explicitly.

  kbagent storage file-upload --project NAME --file PATH [--name NAME] [--tag TAG ...] [--permanent] [--branch ID]
    Upload any file to Storage Files. --tag assigns tags (repeatable). --permanent prevents auto-deletion after 15 days.
    --name overrides the filename (default: local filename). Branch-aware.

  kbagent storage file-download --project NAME [--file-id ID | --tag TAG ...] [--output FILE]
    Download a Storage File. Either --file-id (by ID) or --tag (latest file matching all tags).
    --output sets local path (default: original filename). Handles sliced and gzipped files transparently.

  kbagent storage file-detail --project NAME --file-id ID
    Show file metadata: name, size, tags, sliced/permanent status, creator token. Does not download.

  kbagent storage file-delete --project NAME --file-id ID [--file-id ...] [--dry-run] [--yes]
    Delete one or more Storage Files. Batch: repeat --file-id. --dry-run to preview.

  kbagent storage file-tag --project NAME --file-id ID [--add TAG ...] [--remove TAG ...]
    Add and/or remove tags on a file in a single operation. Both --add and --remove are repeatable.

  kbagent storage load-file --project NAME --file-id ID --table-id TABLE_ID [--incremental] [--delimiter D] [--enclosure E] [--branch ID]
    Import an already-uploaded Storage File into a table. Useful for files uploaded by components or file-upload.
    --incremental to append rows. Branch-aware.

  kbagent storage unload-table --project NAME --table-id TABLE_ID [--columns COL ...] [--limit N] [--tag TAG ...] [--download] [--output FILE|DIR] [--file-type csv|parquet] [--branch ID]
    Export a table to a Storage File. The file stays in Keboola for other components to use.
    --tag assigns tags to the exported file. --download also saves it locally. Branch-aware.
    --file-type parquet produces a sliced Parquet file (CSV default). With --download, --output
    is a directory that will hold one .parquet file per slice plus _manifest.json.
    Default parquet directory: ./{{project}}/{{table_id}}.parquet/ (mirrors Keboola addressing).

### Data Streams (OpenTelemetry / OTLP)

  kbagent stream list --project NAME [--branch ID]
    List Data Streams sources (id, name, type, secret-free base endpoint).
  kbagent stream create-source --project NAME --name NAME [--type otlp|http] [--branch ID] [--if-not-exists] [--no-sinks] [--reveal]
    Create an OTLP (default) or HTTP source; polls the async task and returns the endpoint.
    For OTLP, auto-provisions the logs/metrics/traces sinks (bucket in.c-otlp-<source>) so data
    lands in Storage (idempotent; --no-sinks for a bare source).
    --if-not-exists returns an existing same-named source as status=skipped.
  kbagent stream detail [SOURCE_ID | --name NAME] --project NAME [--branch ID] [--reveal]
    Show base + per-signal endpoints (/v1/logs|/v1/traces|/v1/metrics), protocol http/protobuf,
    and destination bucket/tables (from sinks). The secret embedded in the OTLP URL is MASKED
    by default; pass --reveal to print it (e.g. to wire OTEL_EXPORTER_OTLP_ENDPOINT).
  kbagent stream delete SOURCE_ID --project NAME [--branch ID] [--dry-run] [--yes|--force]
    Delete a source (destructive; async task polled to completion).
  Notes: uses the per-project Storage token (no manage token). Control plane = stream.<region>
  derived from connection.<region>. The OTLP ingest host is stream-in.<region>, returned in
  source.otlp.url -- never derived. The raw Stream API does not auto-create sinks, so kbagent
  provisions the 3 OTLP sinks itself on create-source --type otlp (--no-sinks to opt out). Send
  OTLP/HTTP to <endpoint>/v1/logs|/v1/traces|/v1/metrics; data lands in in.c-otlp-<source>.* tables.

### Scoped Storage Tokens

  kbagent token list --project NAME
    List the project's Storage API tokens (id, description, created, expires, master flag,
    creating token). Secrets are never listed -- `token create` is the only reveal. This is where
    the --token-id for delete/refresh comes from. Acting token needs canManageTokens.
  kbagent token create --project NAME --description DESC [--bucket-write BUCKET ...] [--bucket-read BUCKET ...] [--component-access ID ...] [--can-read-all-file-uploads] [--expires-in N]
    Create a scoped Storage API token (Keboola single-bucket-write pattern). --bucket-write /
    --bucket-read (repeatable) grant per-bucket write/read; write wins when a bucket is on both.
    --component-access (repeatable) restricts to named components. The token secret is printed ONCE
    in a Rich Panel -- store it now, it is never retrievable again. Acting token needs canManageTokens.
  kbagent token delete --project NAME --token-id ID [--yes]
    Revoke a token by its numeric id (destructive; confirms unless --yes / --json).
  kbagent token refresh --project NAME --token-id ID [--yes]
    Rotate a token's secret (new secret printed ONCE; confirms unless --yes / --json).
  Notes: uses the per-project Storage token (no manage token); the acting token must have the
  canManageTokens privilege. The importable SDK (Client(url,token)) mirrors these as
  create_scoped_token / delete_token / refresh_token (dicts on .raw, typed ScopedTokenResult on the facade).

### Sharing (Cross-Project)

  kbagent sharing list [--project NAME]
    List shared buckets available for linking. Multi-project, uses regular token.

  kbagent sharing share --project ALIAS --bucket-id ID --type TYPE [--target-project-ids IDs] [--target-users EMAILS]
    Enable sharing on a bucket. Requires master token (KBC_MASTER_TOKEN_{{ALIAS}} or KBC_MASTER_TOKEN).
    Types: organization, organization-project, selected-projects, selected-users.

  kbagent sharing unshare --project ALIAS --bucket-id ID
    Disable sharing. Fails if linked buckets exist. Requires master token.

  kbagent sharing link --project ALIAS --source-project-id ID --bucket-id ID [--name NAME]
    Link a shared bucket into a project (read-only). Uses regular token.

  kbagent sharing unlink --project ALIAS --bucket-id ID
    Remove a linked bucket from a project. Uses regular token.

  kbagent sharing edges [--project NAME]
    Show cross-project data flow edges via bucket sharing. --project repeatable.

### Data Lineage

  kbagent lineage build --directory PATH --output PATH [--ai] [--refresh]
    Build column-level lineage graph from sync'd data. Scans all sync'd projects,
    detects dependencies via config mappings and SQL parsing, saves to cache file.
    Auto-detects both sync layouts: flat (./.keboola/manifest.json from
    sync pull --project X) and nested (./<alias>/.keboola/manifest.json from
    sync pull --all-projects). Emits a warning in response data if no projects
    are found. --refresh runs sync pull first. --ai generates
    .lineage_ai_tasks.json with AI analysis tasks for an AI agent to process
    (2-step flow).

  kbagent lineage show --load PATH [--upstream NODE] [--downstream NODE]
      [--column COL] [--columns] [--project ALIAS] [--depth N]
      [--format text|mermaid|html|er]
    Query upstream/downstream from cached lineage graph (from lineage build).
    Node identifiers: full FQN project-alias:bucket_id.table_name or just table_id.
    --columns shows column-level mapping. -c COL traces one column.

  kbagent lineage info --load PATH
    Show what's in a cached lineage graph: projects, tables, most connected nodes.

  kbagent lineage server --load PATH [--port N] [--host HOST]
    Start interactive lineage browser in the web browser. Sidebar-based node
    picker with mermaid/ER diagram rendering and export.

### Organization Management

  kbagent org setup --org-id ID --url URL [--dry-run] [--yes] [--token-description PREFIX] [--refresh]
    Bulk-onboard all org projects. Requires org-admin manage token. Idempotent.
    --refresh also refreshes tokens for already-registered projects with invalid tokens.
    --refresh skips browser-login (session) projects with an explicit reason
    instead of minting a static token over their sentinel.

  kbagent org setup --project-ids 901,9621,10539 --url URL [--dry-run] [--yes] [--refresh]
    Non-admin mode: onboard specific projects by ID. Works with Personal Access Token (PAT).
    Use --org-id OR --project-ids (at least one required).
    Token via interactive hidden prompt by default; pass top-level
    --allow-env-manage-token to read KBC_MANAGE_API_TOKEN from env (CI/CD).
    Default-deny since 0.29.0 -- closes the AI-exfiltration risk where
    subprocesses inherit the manage token via env.

### Billing / PAYG Credits (since v0.84.2)

  kbagent billing credits [--project ALIAS ...]
    Read-only PAYG (pay-as-you-go) credit balance. Fans out across all
    registered projects in parallel by default; --project (repeatable)
    narrows to specific aliases. Per-project failures degrade individually
    and never abort the run -- check the "errors" array in --json output.
    A project without the `pay-as-you-go` owner.features flag never touches
    the billing host (it can be NXDOMAIN on non-PAYG stacks) -- it gets a
    per-project error entry with error_code PAYG_NOT_AVAILABLE instead of a
    generic network error.
    Units: the API speaks credits; rows also carry derived minutes
    (1 credit = 60 minutes, matching what the Keboola UI displays) -- never
    hand-convert, use the minutes fields already in the row.
    This command gives the CURRENT BALANCE only. Purchase history / Stripe
    invoice IDs are NOT available -- that data lives on connection.{{stack}}
    /pay-as-you-go/billing/*, which does not accept a Storage token
    (issue #594, still open). Do not imply invoices are retrievable.

### Feature Flags (since v0.48.0)

  Requires a SUPER-ADMIN Manage API token (same kind as `org setup`). Same
  default-deny token policy: interactive hidden prompt by default; pass
  top-level --allow-env-manage-token to read KBC_MANAGE_API_TOKEN from env.
  --project resolves the stack URL (and, for project ops, the numeric
  project_id) from config -- the alias is the only handle you pass.

  kbagent feature list --project ALIAS
    Stack-wide feature catalogue (GET /manage/features).
  kbagent feature project-show --project ALIAS
    Features assigned to a project.
  kbagent feature project-add --project ALIAS --feature NAME [--dry-run] [--yes]
  kbagent feature project-remove --project ALIAS --feature NAME [--dry-run] [--yes]
    Enable / disable a feature on a project. add=admin, remove=destructive.
  kbagent feature user-show --project ALIAS --email EMAIL
  kbagent feature user-add --project ALIAS --email EMAIL --feature NAME [--dry-run] [--yes]
  kbagent feature user-remove --project ALIAS --email EMAIL --feature NAME [--dry-run] [--yes]
    Per-user features (GET/POST/DELETE /manage/users/{{email}}/features).

### Flows (Conditional Flows -- keboola.flow only; orchestrator dropped in 0.57.0)

  kbagent flow list [--project NAME] [--branch ID] [--with-schedules]
    List conditional flows (keboola.flow) across projects. Legacy keboola.orchestrator
    flows are NOT listed; their count is surfaced as legacy_orchestrator_count + a warning.
    --with-schedules enriches each row with {{schedule_id, cron, timezone, enabled}}
    entries from keboola.scheduler (one extra API call per project, NOT per flow).

  kbagent flow detail --project NAME --flow-id ID [--branch ID]
    Show phases, transitions (next[].goto + conditions), typed tasks, and full configuration.

  kbagent flow schema [--full [--project NAME]]
    Plain: print the offline conditional-flow YAML template. --full with --project
    fetches the live JSON Schema from the stack (source=live); --full WITHOUT
    --project serves the bundled authoritative snapshot (source=bundled,
    since 0.73.0 -- previously an error).

  kbagent flow examples [--component-id keboola.flow|keboola.orchestrator]
    (since 0.73.0) Bundled example flow configurations (vendored from
    keboola-mcp-server), fully offline. Default keboola.flow (conditional);
    keboola.orchestrator serves legacy examples with an informational-only
    warning (kbagent cannot create or edit orchestrator flows). --json emits
    the bare list of example configs.

  kbagent flow validate --file YAML|@file|- [--project NAME]
    With --project: fetch the live schema from the stack -> full structural + semantic
    validation (fetch failure degrades to semantic-only + a note). Without --project:
    semantic-only validation + a note that structural validation was skipped (no schema
    source). Exit 0 valid, exit 2 on errors. --json adds {{valid, errors, warnings, notes}}.

  kbagent flow new --project NAME --name "Name" [--description D] [--file YAML|@file|-] [--branch ID]
    Create a new conditional flow. --file accepts YAML with 'phases' and 'tasks' keys.
    Validated against the LIVE conditional-flow schema fetched from the stack
    (INVALID_FLOW_DEFINITION on failure). A schema-fetch failure does NOT block the write:
    structural check skipped, semantic checks still run, a warning is surfaced.
    IDs are STRINGS; phases use next[].goto (a phase id or null); tasks are typed
    (job/notification/variable). Execute with: job run --component-id keboola.flow --config-id ID.

  kbagent flow update --project NAME --flow-id ID [--name N] [--description D] [--file YAML] [--branch ID]
    Update a flow's name, description, or phases/tasks. --file replaces both phases and tasks.
    Omitting --file leaves the flow body unchanged. Validated against the live conditional-flow
    schema on write (merge-aware; INVALID_FLOW_DEFINITION on failure; schema-fetch failure ->
    semantic-only + warning).

  kbagent flow delete --project NAME --flow-id ID [--branch ID] [--yes]
    Delete a flow. Does NOT remove associated keboola.scheduler configs.
    Run 'flow schedule-remove' first if you want to clean up schedules.

  kbagent flow schedule --project NAME --flow-id ID --cron "0 6 * * *" [--timezone TZ] [--enabled/--disabled] [--name NAME] [--branch ID]
    Upsert a cron schedule: updates the existing keboola.scheduler config if one exists, creates one
    otherwise. Calling twice with a new cron replaces the old schedule — no duplicates created.
    The config is then activated on the Scheduler Service so the cron trigger fires; an activation
    failure (e.g. token cannot manage schedules) keeps the config written, sets activated=false, and
    surfaces a warning (exit stays 0). Re-run with a capable token to activate.

  kbagent flow schedule-remove --project NAME --flow-id ID [--branch ID] [--yes]
    Remove all schedules bound to this flow: each schedule is deregistered from the Scheduler
    Service, then its keboola.scheduler config is deleted. Idempotent: safe to run when no
    schedules exist.

### Schedule Discovery & Audit (Fleet-Wide)

  kbagent schedule list [--project NAME ...] [--enabled-only] [--branch ID]
    Fleet-wide list of every keboola.scheduler config across one, many, or all
    projects (multi-project fan-out; no --project means all). Each row shows
    project_alias, schedule_id, schedule_name, parent_component_id, parent_config_id,
    parent_name, cron, timezone, and enabled. Use --enabled-only to hide disabled
    schedules. Answers issue #195: "which flows are on cron triggers across N projects?".

  kbagent schedule detail --project NAME --schedule-id ID [--branch ID]
    Full detail for a single schedule: cron, timezone, enabled state, plus the
    parent config's component_id/config_id/name. Orphaned schedules (parent
    deleted) still return with parent_name="" -- never hard-fails.

  kbagent schedule find [--cron-window START-END] [--not-run-since DAYS] [--project NAME ...] [--branch ID]
    Audit filters, combinable with AND:
    * --cron-window "02:00-04:00" matches schedules whose cron's hour field
      is entirely inside the window. Hour-level approximation -- see gotchas.md.
    * --not-run-since N matches schedules whose parent config's latest job is
      older than N days (or never ran). Pass N=0 to force last_run_at lookup
      without applying a staleness filter.
    Columns last_run_at and matches_cron_window are ALWAYS present in the
    output but populated only when the corresponding filter is active --
    they stay None otherwise so LLM consumers do not treat unevaluated
    cells as positive match signals. Queue API is not branch-aware:
    --branch + --not-run-since still compares against production jobs.

### Notification Subscriptions (Flow Notifications Tab)

  kbagent notification list [--project NAME ...] [--event NAME] [--component-id ID] [--config-id ID]
    Fleet-wide list of Notification Service subscriptions -- the recipients
    behind the Flow Builder's Notifications tab (bell icon: Success / Error /
    Processing-delay / Warning cards). These live in a SEPARATE platform
    service, not in the flow's configuration JSON, so flow detail and
    config detail never show them. The in-flow task of type "notification"
    IS in the configuration and stays visible there -- different mechanism.
    Columns: project_alias, subscription_id, event, component_id, config_id,
    config_name (resolved), branch_id, phase_id, channel, address, expires_at,
    scope, filters.
    Event names are kebab-case: job-failed, job-succeeded,
    job-succeeded-with-warning, job-processing-long, and the phase-job-*
    variants. --event is NOT validated against that list (the API declares
    EventName as an open string).
    ALL filtering is CLIENT-SIDE, --event included: the service accepts
    ?event= and then IGNORES it, answering 200 with the project's full
    subscription list (verified live). kbagent still sends the parameter and
    narrows the rows itself, so --event is correct from the CLI -- anything
    calling the service directly must narrow too, or it gets a superset.
    --component-id / --config-id match the subscription's own
    job.component.id / job.configuration.id filter values.
    A subscription with NO filters is project-wide (scope="project-wide") and
    fires for every job -- those are excluded by --component-id/--config-id
    and reported as project_wide_excluded so the "who gets paged for this
    flow" answer is never silently incomplete.
    branch_id is populated on EVERY row, production included: the Flow
    Builder always writes a branch.id filter and uses the DEFAULT branch's
    numeric id for production. A filled Branch column does NOT mean
    "dev-branch only" -- cross-check `branch list` to tell them apart.

  kbagent notification detail --project NAME --subscription-id ID
    One subscription with every filter printed verbatim, including threshold
    filters like durationOvertimePercentage that have no dedicated column.

  Read-only in this release; creating/deleting subscriptions is not exposed.

### Development Branches

  kbagent branch list [--project NAME]
    List dev branches. --project repeatable.

  kbagent branch create --project ALIAS --name "name" [--description "..."]
    Create dev branch and auto-activate it. Async, CLI waits for completion.

  kbagent branch use --project ALIAS --branch ID
    Set existing branch as active for subsequent commands.

  kbagent branch reset --project ALIAS
    Reset to main/production branch.

  kbagent branch delete --project ALIAS --branch ID
    Delete branch (async). Auto-resets to main if it was active.

  kbagent branch merge --project ALIAS [--branch ID]
    Get KBC UI merge URL (does NOT merge via API). Resets active branch.

  kbagent branch metadata-list --project NAME [--branch ID|default]
    List all metadata entries on a branch (id, key, value, provider, timestamp).

  kbagent branch metadata-get --project NAME --key KEY [--branch ID|default]
    Read a single metadata value by key. Exits with NOT_FOUND if absent.

  kbagent branch metadata-set --project NAME --key KEY [--text STR | --file PATH | --stdin] [--branch ID|default]
    Set a metadata key/value. Useful for KBC.projectDescription and similar
    dashboard-visible fields. --branch defaults to "default" (main branch).

  kbagent branch metadata-delete --project NAME --metadata-id ID [--branch ID|default]
    Delete a metadata entry by its numeric ID (from metadata-list).

### Workspaces (SQL Debugging)

  kbagent workspace create --project ALIAS [--name NAME] [--backend TYPE] [--ui] [--read-only/--no-read-only]
    Create workspace. Backend auto-detected from project (or override with --backend). Default: headless (~1s). --ui: visible in KBC UI (~15s).
    Since 0.47.1, Snowflake headless creates return private_key and an empty password field; use key-pair auth.

  kbagent workspace list [--project NAME] [--orphaned] [--branch ID] [--qs-compatible]
    List workspaces. Read command: ignores active dev branch (production endpoint) with an Info banner;
    pass --branch to opt in. Each entry carries login_type, read_only, qs_compatible so callers can pick a
    Query-Service-compatible workspace without firing a probe query. --qs-compatible filters to RO +
    confirmed-whitelist loginType (canonical data-app shape). --orphaned shows orphaned workspaces.

  kbagent workspace detail --project ALIAS --workspace-id ID [--branch ID]
    Workspace connection details (no password). Includes login_type, read_only, qs_compatible.
    Read command: ignores active dev branch with an Info banner; pass --branch to opt in.

  kbagent workspace delete --project ALIAS --workspace-id ID
    Delete workspace. They also expire automatically.

  kbagent workspace password --project ALIAS --workspace-id ID
    Reset and return new workspace password.

  kbagent workspace load --project ALIAS --workspace-id ID --tables TABLE_ID [...] [--preserve]
    Load storage tables into workspace. --preserve keeps existing tables.

  kbagent workspace query --project ALIAS --workspace-id ID --sql "SQL" [--file F] [--transactional] [--full] [--limit N]
    Execute SQL via Query Service. No Snowflake credentials needed.
    Default reads results inline (fast JSON columns+rows), capped at --limit (default 500).
    --full uses the complete CSV export instead (slower, uncapped).

  kbagent workspace from-transformation --project ALIAS --component-id ID --config-id ID [--row-id ID]
    Create workspace from transformation config. Loads input tables automatically.

  kbagent workspace gc [--project NAME] [--dry-run] [--yes]
    Garbage-collect orphaned workspaces (keboola.sandboxes config missing). Use --dry-run to preview.

### Data Apps (Streamlit / Flask / Node deployments)

Lifecycle for `keboola.data-apps`. Combines the Storage API (config body --
git block, slug, runtime size, encrypted secrets) with the Data Science API
(/apps -- deployment record, state, URL, configVersion). Encapsulates the
§9 redeploy contract so callers cannot pin to the empty-shell v2.

  kbagent data-app list [--project NAME ...] [--branch ID]
    List data apps across one or many projects. Merges Data Science /apps
    index with Storage config names. Multi-project parallel.

  kbagent data-app detail --project NAME --app-id ID [--branch ID]
    Full merged view: state, desiredState, url, deployed configVersion, slug,
    runtime size, git settings (PAT redacted as <encrypted>).

  kbagent data-app create --project ALIAS --name NAME --slug SLUG
    (--git-repo URL | --use-managed-git-repo)
    [--description STR | --description-file PATH] [--git-branch main]
    [--git-public/--no-git-public] [--git-username USER]
    [--git-pat-env VAR | --git-pat-file PATH | --git-pat-encrypted KBC::Project...]
    [--auth password|public] [--size tiny|small|medium|large] [--auto-suspend SECONDS]
    [--type python-js|python|streamlit|r|...] [--workspace/--no-workspace] [--branch ID]
    [--no-deploy] [--wait] [--timeout SECONDS] [--keep-on-failure] [--dry-run]
    Create + configure + deploy in one call. Default `--auth password` mints
    a 20-char hex simpleAuth password (retrievable via `data-app password`).
    PAT input (private repo): env var (recommended) > file > pre-encrypted.
    Pre-encrypted PATs MUST start with KBC::Project (project-scoped KMS).
    Cleanup-in-finally if PUT or initial deploy fails (orphan shell deleted
    by default; --keep-on-failure preserves it for forensics).
    --use-managed-git-repo (0.65.0+) provisions an EMPTY Keboola-hosted repo
    instead of cloning an external one; writes no git block, forces --no-deploy,
    mutually exclusive with --git-repo and all --git-*/PAT flags. Managed deploy
    works via: create --use-managed-git-repo -> git-credentials-create
    --type http_token --permissions readWrite + push code to the managed repo
    URL -> deploy. The platform injects the clone credentials at deploy time,
    so no credential wiring is needed.
    --workspace (0.87.0+, DEFAULT ON) writes runtime.workspace.enabled=true --
    the single switch that makes the platform provision the ephemeral workspace
    and inject WORKSPACE_ID / QUERY_SERVICE_URL / KBC_WORKSPACE_MANIFEST_PATH.
    Any app that reads Storage needs it. Before 0.87.0 kbagent never wrote it,
    so apps deployed, reported state=running, passed the health probe, and then
    served no data, with NO platform-side diagnostic: verify by reading
    `config detail` -> `configuration.runtime`, not by grepping `data-app logs`
    (a `Missing env vars: WORKSPACE_ID` line comes from the app's own code, so
    its absence rules nothing out). Pass --no-workspace only for an app that
    never touches Storage. Retrofit an existing app with:
      kbagent config update --project P --component-id keboola.data-apps
        --config-id ID --merge --set 'runtime.workspace.enabled=true'
    then redeploy (deploy pins the LATEST version, so the change takes effect).

  kbagent data-app deploy --project NAME --app-id ID [--config-version N]
    [--wait] [--timeout SECONDS] [--branch ID]
    The §9 redeploy contract. Default reads the latest Storage config version
    and pins to it; --config-version pins an older version (rollback).
    Always sends {{desiredState=running, configVersion, restartIfRunning=true}}
    together -- HTTP 422 otherwise.

  kbagent data-app start --project NAME --app-id ID [--wait] [--timeout SECONDS]
    Wake an auto-suspended data app at its currently-pinned configVersion.
    Distinct from deploy: does NOT bump the version.

  kbagent data-app stop --project NAME --app-id ID [--wait] [--timeout SECONDS]
    Stop a running data app. Preserves URL and Storage config; container is
    torn down.

  kbagent data-app delete --project NAME --app-id ID [--yes]
    Delete the deployment AND the Storage config (cascade, irreversible).
    URL is permanently retired. Confirmation prompt unless --yes.

  kbagent data-app password --project NAME --app-id ID
    Retrieve the simpleAuth password. Requires the Manage API token in
    addition to the project's Storage token. Token is read from interactive
    hidden prompt by default; pass top-level --allow-env-manage-token to
    use KBC_MANAGE_API_TOKEN from env (default-deny since 0.29.0). Never
    persisted, never logged. Password is auto-generated at create time
    and CANNOT be rotated -- delete and recreate the app to mint a new one.

  kbagent data-app logs --project NAME --app-id ID [--lines N] [--since ISO8601]
    Tail the container log buffer (Data Science /apps/{{id}}/logs/tail).
    Plain-text body covering the full spin-up trace ([TIMING] git_clone,
    Cloning into /app, uv install, supervisord boot, runtime stack traces).
    Default --lines 500; pass --lines 0 to fetch the full current buffer
    (no server-side cap). --lines and --since are mutually exclusive;
    --since requires a timezone (Z or +00:00). App must be running or
    recently-stopped -- never-started apps return 400 "App X is not
    running"; recover with 'kbagent data-app start' or 'data-app deploy'.
    Closes the gap where the upstream keboola-mcp-server's get_data_apps
    tool hardcodes a 20-line cap on log output (structurally too small to
    capture a healthy spin-up). The log buffer can echo runtime secrets
    the app printed to stdout/stderr -- consider hygiene before piping
    --json output into AI agent context.

  kbagent data-app runs --project NAME --app-id ID [--limit N]
    List deployment attempts (runs) newest-first with failure_reason +
    startup_logs, including setup-phase failures (e.g. git-clone errors)
    that produce no container logs. Works on never-started/failed apps
    where 'data-app logs' returns HTTP 400 -- the canonical way to find
    WHY a deploy reverted to stopped. Project storage token only.

  kbagent data-app secrets-set --project ALIAS --app-id ID --secret '#KEY=VALUE'
        [--secret '#KEY2=VALUE2' ...] [--secrets-file PATH] [--branch ID]
        [--allow-plaintext-on-encrypt-failure] [--dry-run] [--no-hint-next]
    Encrypt and write '#'-prefixed secrets into parameters.dataApp.secrets.
    Per-project KMS via the Encryption API; ciphertext does not cross
    projects (writeup §8). Read-modify-write at the service layer to
    preserve sibling keys; never use Storage merge=True for nested edits.
    The runtime exposes each key as an env var with '#' stripped, '-'
    replaced with '_', uppercased ('#my-api-key' -> 'MY_API_KEY').
    Adding a secret bumps the Storage version; the running container
    keeps the OLD config until the next 'kbagent data-app deploy'.

  kbagent data-app secrets-list --project ALIAS --app-id ID [--branch ID]
        [--show-fingerprint]
    List the keys in parameters.dataApp.secrets with derived runtime
    env-var names. Never echoes encrypted ciphertext in full and never
    decrypts. --show-fingerprint includes a short fingerprint per key.

  kbagent data-app secrets-get --project ALIAS --app-id ID --key 'KEY'
        [--branch ID]
    Show ONE key from parameters.dataApp.secrets. Leading '#' is OPTIONAL
    -- the block holds both encrypted secrets (#) and plain env-var
    values, both enumerated by secrets-list. ENCRYPTED secret -> metadata
    only (encrypted: true, value: null); the decrypted plaintext is NEVER
    echoed (Encryption API has no decrypt endpoint). PLAIN value -> the
    literal value (encrypted: false), already visible via config detail.
    NOT_FOUND on absent key (exact match); never enumerates siblings.

  kbagent data-app secrets-remove --project ALIAS --app-id ID --key 'KEY'
        [--key 'KEY2' ...] [--branch ID] [--yes] [--dry-run]
    Remove one or more keys (encrypted secrets OR plain env vars; leading
    '#' optional). Idempotent (missing keys -> exit 0, removed: 0).
    Destructive: a removal can break the running app at next deploy if it
    depends on the value. Confirmation prompt unless --yes or --json.

  kbagent data-app validate-repo --git-repo URL [--git-branch BRANCH]
        [--git-public/--no-git-public] [--git-pat-env VAR | --git-pat-file PATH]
        [--type python-js] [--strict]
    Pre-flight check that a git repo follows the Keboola data-app
    Golden Rule (https://help.keboola.com/data-apps/python-js/). Walks
    the repo via GitHub Contents + Trees API (<=5 calls -- 1 tree +
    up to 4 contents -- regardless of
    repo size); each check emits BLOCKING / WARN / OK with a help-doc
    citation. --type currently restricted to python-js; streamlit /
    pure-Python / R / Node-only follow-up. --strict treats WARNs as
    failures (exit 1).

  kbagent data-app git-repo --project NAME --app-id ID
    Show the clone URLs (ssh_url / https_url) of the app's configured git
    repository plus is_managed_git_repo (sandboxes-service
    GET /apps/{{id}}/git-repo). Read-only; project storage token only.
    GOTCHA: returns 409 "no Git repository configured" until the app has
    been DEPLOYED at least once -- the git block is synced from the Storage
    config into the Data Science app record at deploy time, so a fresh
    --no-deploy app has no git repo from the service's point of view.

  kbagent data-app git-credentials --project NAME --app-id ID
    List the credentials of the app's MANAGED git repository (id, type,
    permissions, name, owner_admin_id, created_at). The secret is NEVER
    returned here. Needs an admin storage token. External repos (the kind
    `data-app create --git-repo` produces) have no managed credentials.

  kbagent data-app git-credentials-create --project NAME --app-id ID
        --type ssh_key|http_token --permissions readOnly|readWrite
        [--public-key KEY | --public-key-file PATH] [--name LABEL] [--yes]
    Mint a git credential for the app's MANAGED git repository. ssh_key
    requires a public key; http_token returns a ONE-TIME secret printed
    once and never retrievable again (mirrors data-app password). Requires
    an admin storage token. Apps created via `data-app create --git-repo`
    are EXTERNAL (not managed) -> 409 "no managed Git repository".
    Confirmation prompt unless --yes or --json.

### Project Sync

  kbagent sync init --project ALIAS [--directory DIR] [--git-branching] [--adopt-existing]
    Initialize sync working directory. --git-branching enables git-to-Keboola branch mapping.

  kbagent sync pull --project ALIAS [--all-projects] [--force] [--theirs] [--dry-run] [--with-samples] [--no-storage] [--no-jobs] [--job-limit N] [--branch ID]
    Download configs as local files. Idempotent, protects local modifications.
    --force (semantics corrected since 0.53.0): re-pull over locally-modified configs.
    A config edited locally whose remote is UNCHANGED is PRESERVED (its pending delta stays
    pushable -- NOT discarded, NOT silently re-stamped). A true merge conflict (the config
    changed BOTH locally and on the remote since the last pull) ABORTS the pull (exit 1,
    SYNC_CONFLICT) listing each conflict; resolve via sync diff then push-or-discard, then pull.
    --theirs (since 0.72.0): remote wins everywhere -- overwrites locally-modified configs
    and rows, restores deleted/missing files, resolves conflicts by taking remote (no abort).
    The supported way to reconcile a drifted tree with production (no manifest surgery).
    Since 0.72.0 plain pull also re-materializes a tracked config whose local dir is missing
    (manifest<->disk invariant), so delete-dir-then-pull refetches. Applies to rows too.
    Config-level isDisabled round-trips (since 0.72.0) as sparse `is_disabled: true` in
    _config.yml -- absent key means enabled; pull writes it, diff surfaces drift, push sends it.
    --job-limit controls max recent jobs per config (default 5). For large projects,
    automatically falls back to per-config job fetching to ensure all configs get job history.
    Auto-detects renamed configs and renames local directories to match (uses git mv in git repos).
    --branch (since 0.47.0): per-invocation dev-branch override. Same semantics as sync push/diff.

  kbagent sync status [--directory DIR]
    Show local changes since last pull (SHA256-based). LOCAL check only --
    it never contacts the API, so it cannot see remote drift; use sync diff
    for a local-vs-remote audit (human output says so since 0.72.0).
    Also returns plaintext_secret_warnings (since 0.55.0): in-sync
    configs/rows whose #-secrets are still plaintext on the remote
    (pre-0.54.0 leak; #378). Fix = re-push on >=0.54.0 + rotate (version
    history keeps the plaintext).

  kbagent sync diff --project ALIAS [--all-projects] [--directory DIR] [--branch ID]
    3-way diff: local vs pull-time snapshot vs remote. Detects conflicts.
    --branch (since 0.47.0): per-invocation dev-branch override. Wins over
    manifest.branches[0] / 'branch use' active branch / git-branching mapping.
    Requires exactly one --project.

  kbagent sync push --project ALIAS [--all-projects] [--dry-run] [--force] [--allow-plaintext-on-encrypt-failure] [--branch ID] [--no-name-drift-warnings]
    Push local changes. Auto-encrypts secrets. Skips conflicts (pull first).
    Fails if encryption fails (plaintext secrets never pushed). Use escape hatch flag only if you know what you are doing.
    Fresh-CREATE behavior (since 0.47.0): if the manifest contains a placeholder entry at
    (component_id, path), the create path updates it in place (no manifest duplication)
    and propagates any KBC.configuration.* metadata via set_config_metadata. Re-pushes
    against the now-real config id are naturally idempotent.
    Fresh-CREATE variable binding (since 0.47.2): when a keboola.variables config + its
    values row are created alongside a transformation in the same push, the transformation's
    variables_id / variables_values_id are rebound to the assigned ULIDs (not placeholder
    dirnames), the row's values are hoisted even when the scaffold row file has no _keboola
    block, and the row's placeholder parent is remapped before POST. job run then succeeds
    without a post-push config variables-set step.
    --branch (since 0.47.0): per-invocation dev-branch override. Same semantics as sync diff.
    When no <branch_name>/ subtree exists on disk (since 0.47.2), the local default tree
    (main/) is read as the source and promoted to the target branch; API writes still target
    the branch id.
    --no-name-drift-warnings (since 0.47.0): suppress the cosmetic name_drift_warnings
    array from the result envelope.
    Never-fetched guard (since 0.72.0): a manifest entry with an empty pull_hash and no
    local files (pre-0.72 name-collision phantom) is NEVER planned as a remote DELETE;
    diff/push exclude it and report it under never_fetched with a warning -- run sync pull
    to materialize it. Local deletion of a properly-pulled config still deletes on push.
    Adopted-by-id writeback (since 0.72.0): pushing an untracked local file whose
    _keboola.config_id resolves on the branch (adopt-update, #482) now also writes the
    manifest entry, so follow-up diffs are stable and a later local delete is detected.

  kbagent sync clone --source DIR --target ALIAS --target-dir DIR [--bucket-map FILE] [--variable-values FILE] [--instance-rename FILE] [--dry-run] [--branch ID]
    Clone a reference synced tree into a fresh target project + parameterize it
    (bucket_map / variable_values / instance_rename overrides), then push so every
    config CREATEs fresh. keboola.flow task configIds + variable links remap
    reference->ULID. Idempotent (re-run -> no_changes); needs a fresh target.
    Note: --dry-run still creates --target-dir on disk (copy + overrides + manifest)
    but does not push.

  kbagent sync branch-link --project ALIAS [--branch-id ID] [--branch-name NAME]
    Link git branch to Keboola dev branch. Auto-creates if needed.

  kbagent sync branch-unlink [--directory DIR]
    Remove git-to-Keboola branch mapping.

  kbagent sync branch-status [--directory DIR]
    Show current branch mapping status.

### Encryption

  kbagent encrypt values --project ALIAS --component-id ID --input JSON|@file|-  [--output-file PATH]
    Encrypt #-prefixed secret values via Keboola Encryption API (one-way, no decrypt).
    Scope: project-scoped ProjectSecure cipher, bound to this project + component.
    The prefix is cloud-specific: KBC::ProjectSecure:: (AWS), ::ProjectSecureGKMS::
    (GCP), ::ProjectSecureKV:: (Azure) -- match the family, never one literal.
    Use when ciphertext must exist before a `config update` / `config new` /
    `config clone` write.
    --input accepts: inline JSON, @file.json (from file), or - (from stdin).
    Already-encrypted values (KBC:: prefix) pass through unchanged.

### Semantic Layer (Metastore) (since v0.41.0)

Manage Keboola metastore models: datasets, metrics, relationships, constraints,
glossary terms. Metastore URL derived from stack URL by replacing `connection.`
with `metastore.`. Auth: same `X-StorageApi-Token` as Storage. Alias:
`kbagent sl ...` (hidden) is equivalent to `kbagent semantic-layer ...`.

  kbagent semantic-layer model list --project P
    List all semantic-layer models in a project.

  kbagent semantic-layer model create --project P --name N [--description D] [--sql-dialect Snowflake]
    Create a new model (default sql-dialect: Snowflake).

  kbagent semantic-layer model delete --project P --model M [--yes]
    Delete a model. Fails if the model still has child entities.

  kbagent semantic-layer show --project P [--model M] [--type T]
    Show a model's entities. --type filter: dataset|metric|relationship|constraint|glossary.
    Without --type prints a per-type count summary.

  kbagent semantic-layer schema --project P (--type model|dataset|metric|relationship|constraint|glossary[,TYPE...] | --all)
    (since 0.73.0) Live JSON Schema per semantic object type, fetched from the
    deployed metastore (never bundled -- cannot drift). Exactly one of
    --type/--all. --json emits {{project, schemas: [{{type, schema}}]}}.

  kbagent semantic-layer search-context --project P [--pattern G ...] [--type model|dataset|metric|relationship|constraint|glossary|all] [--limit N]
    (since 0.47.0) Project-wide glob search across semantic-layer entity names.
    Mirrors the upstream keboola-mcp-server search_semantic_context tool so a
    downstream caller can verify the model is populated without an MCP dependency.
    Patterns are case-sensitive fnmatch, repeatable (union). Default pattern is "*".
    Default --type is "all" (every CHILD type; "model" searches semantic models).
    Returns {{project, contexts: [{{id, type, name, description, attributes}}], total_count}}.

  kbagent semantic-layer get-context --project P --context-id ID
    (since 0.47.0) Single-entry fetch by id, irrespective of type. Probes model first,
    then datasets/metrics/relationships/constraints/glossary in order; raises NOT_FOUND
    if no type matches (exit 1).

  kbagent semantic-layer validate --project P [--model M] [--deep]
    Basic structural checks (duplicates, dangling refs, sum-on-pct,
    constraint orphans, severity-suffix). --deep adds parallel Snowflake
    column-existence checks for phantom fields, phantom column refs, and
    AGG-on-STRING via in-process StorageService.

  kbagent semantic-layer export --project P [--model M] [--output PATH]
    Snapshot the model to a self-describing JSON file. Default path:
    ./sl_export_{{model_name}}_{{YYYYMMDD_HHMMSS}}.json.

  kbagent semantic-layer diff (--project-a A | --file-a P) (--project-b B | --file-b P) [--model-a M] [--model-b M]
    Three-way diff: project<->project, project<->file, file<->file. Output
    groups changes per entity type: added, removed, changed (with diff_keys).

  kbagent semantic-layer reference-data list|get|set|delete ... (since 0.55.0)
    Dimension-member records (semantic-reference-data): one record per
    dimension holding the full member list in a members[] array (e.g. a
    Chart of Accounts). Deliberately OUTSIDE build/export/diff/cascade.
    list --project P [--model M] -> dimension summaries (id, dimension,
    member_count). get --project P (--id ID | --dimension D) ->
    one record + all members (dimension is project-unique, so no model
    needed). set --project P [--model M] --dimension D
    --members-file PATH ('-' = stdin) [--dataset-id T] [--description X] ->
    create-or-replace, idempotent on dimension (project-wide lookup): an
    existing record is replaced in place via PUT (revision++), else POST.
    delete --project P --id ID [--yes]. Member keys mirror the DIM_COA
    columns (account_code, account_name, parent_code, is_leaf, ...).

  kbagent semantic-layer add metric|dataset|relationship|constraint|glossary ...
    Add one entity. Dataset auto-derives `fqn` from --table-id; --deep-fields
    fetches the storage schema and synthesises role-classified fields
    (PK_/FK_->key, *_DATE/*_DT->timestamp, numeric amount/value/rate->measure,
    else dimension). Constraint name regex `^[a-z][a-z0-9_]*$`, severity is
    error|warning|info (the 4-band health convention lives in the NAME suffix
    `_critical/_warning/_healthy/_review`, not the API severity). `--rule` is
    a STRING expression (e.g. "value >= 0"), NEVER an object.

  kbagent semantic-layer edit metric|dataset|constraint|relationship|glossary ...
    DELETE+POST (no PATCH on metastore). Metric rename cascades through every
    constraint referencing the old name (DELETE old + POST new with updated
    metrics[]); CODE_METRIC warning shown
    (re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")). On POST failure,
    rollback re-POSTs original_attrs and reports success/failure explicitly.
    --yes skips the confirm prompt. `edit relationship` accepts --new-from /
    --new-to / --new-on / --new-type (left|inner). `edit glossary` accepts
    --new-term (destructive cascade; requires --yes in non-TTY) / --new-definition.
    Partial-state envelope (since v0.41.10): when metric rename succeeds but
    one or more dependent constraints fail to repoint, the response sets
    `partial_state: true` at the top level + `recovery_hint: "<text>"`
    pointing at `semantic-layer validate` + manual `edit constraint
    --new-metrics ...`. Human-mode CLI prints a red `PARTIAL STATE` banner
    above the per-entry list. `edit_simple` (no-cascade variants) carries
    `partial_state: false, recovery_hint: null` for envelope uniformity.

  kbagent semantic-layer remove metric|dataset|constraint|relationship|glossary ...
    Destructive. `remove metric` pre-scans constraints whose metrics[] includes
    the target; warns about dangling DIM_METRIC_THRESHOLD refs. --yes skips the
    prompt but the orphan warning is always printed. Non-TTY without --yes
    refuses with exit 2. `remove relationship` and `remove glossary` are leaf
    removes -- no orphan-check (those entities aren't referenced by others).
    `remove glossary` identifies the entity by --term, not --name.

  kbagent semantic-layer import --project P --file PATH [--model M] [--types T,T,...] [--dry-run] [--yes] [--overwrite]
    Replay a snapshot. Default: skip on conflict. --overwrite opts into
    DELETE+POST. Dependency-ordered push (datasets -> metrics -> relationships
    -> glossary -> constraints).

  kbagent semantic-layer promote --from-project A --to-project B [--from-model M] [--to-model M] [--types ...] [--dry-run] [--yes]
    Cross-project copy with modelUUID rewrite. Classifies items NEW / IDENTICAL
    / CHANGED (deep-equality after stripping modelUUID + timestamps).
    Additive + overwrite only -- NEVER deletes target items absent from source.

  kbagent semantic-layer build --project P [--model M] --tables T,T,... [--dry-run] [--keep-on-failure] [--output PATH]
    Non-interactive heuristic builder. AI caveat: the ai_client has no
    arbitrary-JSON endpoint, so `build` falls back to a deterministic
    heuristic (one dataset + one COUNT(*) metric + one glossary entry per
    table; FQN derived; fields[] role-classified). Response carries
    `fallback_used: "heuristic"`. Push loop iterates all 5 child types in
    dependency order (fixes the long-standing sl-build skill bug where
    semantic-constraint was silently dropped). On push failure rolls back
    every successfully-POSTed child in reverse order + deletes the model
    if we created it (since v0.41.10); pass --keep-on-failure to preserve
    the partial state for forensic inspection (mirrors data-app create).

  kbagent semantic-layer token --encrypt --project P --component-id C
    Encrypt the project's storage token for transformation `user_properties`.
    Builds {{"#metastore_token": <token>}} and delegates to EncryptService.
    --encrypt is currently required; other modes refused with USAGE_ERROR.


### Self-call HTTP (inside `kbagent serve` subprocesses)

  kbagent http get PATH [--timeout SECONDS]
  kbagent http post PATH [--body JSON|@file|-] [--timeout SECONDS]
  kbagent http patch PATH [--body JSON|@file|-] [--timeout SECONDS]
  kbagent http delete PATH [--timeout SECONDS]
    Raw HTTP client against the running `kbagent serve`. Reads KBAGENT_SERVE_URL +
    KBAGENT_SERVE_TOKEN env vars (auto-injected into AI-agent / cli_command
    subprocesses by the scheduler). Example:
      kbagent http get /openapi.json     # browse server's OpenAPI schema
      kbagent http get /projects         # list projects via HTTP
      kbagent http post /agents/test --body @task.json
    Prefer this over forking `kbagent` CLI inside scheduled-agent tasks --
    `kbagent http` calls the serve directly so you always see the same config
    the operator configured (not the global ~/.config one). Outside a serve
    subprocess context the command refuses to run.

### Agent Tasks (CLI parity with the `/agents` REST surface)

  Reads/writes <config_dir>/agents.json -- the same on-disk format the
  cron loop inside `kbagent serve` consumes. CLI CRUD + ad-hoc `run`
  work offline; cron firing still requires the live server.

  Every subcommand below that takes TASK_ID / RUN_ID accepts it either
  positionally (`agent show TASK_ID`) or via flag (`--id` / `--task-id`,
  plus `--run-id` for run-detail / run-events) -- the flag form matches
  the rest of the CLI (`--job-id`, `--config-id`, ...).

  kbagent agent list
    List all registered tasks (id, name, cron, type, state, last/next run).

  kbagent agent show TASK_ID
    Full task detail including the action payload.

  kbagent agent create --name N [--description D] [--cron CRON] [--manual]
                       [--enabled/--disabled]
                       (--type ai_agent --cli claude|codex|gemini --prompt P
                                        [--extra-arg ARG ...] [--timeout SECONDS]
                       |--type cli_command --argv ARG [--argv ARG ...]
                                           [--timeout SECONDS]
                       |--from-file PATH|@path|-)
                       [--trigger-task-id ID --trigger-on success|error|always]
    Persist a new scheduled task. --manual skips the cron loop. Use
    --from-file for the full {{"type":..., "params":...}} JSON envelope
    when prompts/args grow large. --extra-arg on an ai_agent task is
    honored only when the kbagent process (serve, or this `agent` run)
    has a truthy KBAGENT_ALLOW_AI_EXTRA_ARGS env (since 0.60.2);
    otherwise the args are dropped with a warning.

  kbagent agent update TASK_ID [--name N] [--description D] [--cron C]
                                [--enabled/--disabled] [--manual/--auto]
                                [--clear-trigger]
                                [--trigger-task-id ID --trigger-on ...]
    Patch one or more fields. Omitted flags leave the field unchanged.
    --manual nulls next_run_at; --auto recomputes it from the cron expr.

  kbagent agent delete TASK_ID [--yes]
    Permanent removal. Run history on disk is preserved.

  kbagent agent run TASK_ID [--stream]
                            [--runtime-prompt TEXT | --runtime-input JSON|@file|-]
    Trigger immediately. --stream emits live events (one line per event;
    NDJSON in --json mode). --runtime-prompt appends ad-hoc text to an
    ai_agent's persisted prompt for this run only; --runtime-input merges
    arbitrary JSON into the action params.

  kbagent agent runs TASK_ID [--limit N]
    Run history (newest first).

  kbagent agent run-detail TASK_ID RUN_ID
    Single AgentRun record (status, summary, output, error).

  kbagent agent run-events TASK_ID RUN_ID
    Replay the persisted ai_agent event timeline.

  kbagent agent test [--type ... | --from-file PATH] [--stream] [--name N]
                     [common action flags from `create`]
    Execute an action ad-hoc -- nothing is persisted. Useful for
    sanity-checking a prompt or argv before saving.

  kbagent agent cron-preview --cron "0 6 * * 1" [--count N]
    Validate a cron expression and show the next N firings (UTC, max 20).

  kbagent agent prompt-improve --goal "..." [--draft "..."]
                                [--cli claude|codex|gemini] [--project ALIAS]
                                [--extra-arg X ...] [--stream/--no-stream]
    AI-polished single-shot prompt for an unattended agent task. Spawns
    the chosen AI CLI with a meta-prompt; the final `done` event's
    `data.prompt` carries the cleaned body ready to paste into
    `agent create --prompt ...`. --extra-arg follows the same
    KBAGENT_ALLOW_AI_EXTRA_ARGS opt-in as `agent create` (since 0.60.2).

  See agent-tasks-cli-workflow.md skill reference for full walkthroughs.

### The `tool` group is GONE (removed in v0.85.0)

  `kbagent tool list` / `kbagent tool call` and `agent --type mcp_tool` no
  longer exist (epic #390 phase 3). Every tool the old catalog exposed has a
  native command -- if you know a historical tool name, look it up in
  docs/mcp-migration.md, which maps each one to its replacement. Never
  suggest `tool call` or `--type mcp_tool` to a user: those commands fail.
  Existing `mcp_tool` agent tasks are kept as inert tombstones that never
  run; `kbagent doctor` reports them as FAIL and `agent list` flags them.
  Recreate each as `--type cli_command`.

### Kai -- Keboola AI Assistant (BETA)

  Requires the project to be added with its MASTER Storage API token (the
  auto-generated 'owner' token, not a custom one) and the 'AI Agent Chat'
  feature flag enabled on the project. Custom Storage API tokens cannot
  access Kai -- all `kbagent kai *` calls will fail with KAI_NOT_ENABLED.

  kbagent kai ping [--project NAME]
    Check Kai server health and MCP connection status.
    Fails with KAI_NOT_ENABLED if the project lacks the 'agent-chat' feature
    or was added with a non-master token.

  kbagent kai preflight [--project NAME]
    Inspect the configured token's Kai readiness WITHOUT raising. Returns
    {{ok, is_master_token, has_agent_chat_feature, token_description, error}}.
    Use this when you need to render a warning instead of failing — UIs and
    automation pre-flight checks should use this instead of `ping`.

  kbagent kai chat-detail --chat-id ID [--project NAME]
    Fetch the full message history of a single Kai chat. Returns a flat list
    of {{role, content, created_at}} records. Use to restore / continue a
    conversation with `kai chat --chat-id ID` or to export a transcript.

  kbagent kai ask --message "question" [--project NAME]
    One-shot question to Kai. Collects full response. Use --json for structured output.
    Kai has MCP access to project data -- use for Keboola-specific questions
    (e.g. "What tables do I have?", "Is it safe to drop bucket X?").

  kbagent kai chat --message "msg" [--chat-id ID] [--project NAME]
    Send message in a chat session. Use --chat-id to continue a conversation.
    Without --chat-id starts a new chat. Returns chat_id for continuation.

  kbagent kai history [--project NAME] [--limit N]
    List recent Kai chat sessions. Default limit: 10.

### SQL Transformations (since v0.73.0)

  kbagent transformation create --project NAME --name NAME (--sql 'SELECT ...' | --sql-file PATH) [--created-table NAME ...] [--component-id ID] [--description D] [--branch ID] [--dry-run]
    Create a SQL transformation. Component id derived from the project
    default_backend (snowflake -> keboola.snowflake-transformation,
    bigquery -> keboola.google-bigquery-transformation; other backends
    require --component-id). SQL is split one statement per script element;
    a single block "Blocks" with one code "Code" is created (UI/MCP parity).
    Each --created-table T adds output mapping T -> out.c-<cleaned-name>.<T>.

  kbagent transformation show --project NAME --config-id ID [--component-id ID] [--branch ID]
    Print the block/code tree with synthetic positional ids b{{i}} / b{{i}}.c{{j}}
    plus storage mappings. Without --component-id every known SQL
    transformation component is probed (404s skipped). ALWAYS run show
    before edit -- ids renumber after every structural change.

  kbagent transformation edit --project NAME --config-id ID --change-description TEXT (--op JSON ... | --op-file ops.json) [--storage JSON|@file|-] [--component-id ID] [--branch ID] [--dry-run]
    Apply structured ops to blocks/codes: add_block, remove_block,
    rename_block, add_code, remove_code, rename_code, set_code, add_script,
    str_replace. Ops in one batch apply sequentially against BATCH-START ids
    (mid-batch structural changes do not renumber within the batch).
    --storage REPLACES configuration.storage wholesale -- include every
    mapping you want to keep. --dry-run previews the resulting tree + op
    summary without writing.

### Documentation Q&A (since v0.73.0)

  kbagent docs query "QUESTION" [--project NAME]
    Answer a natural-language question from the Keboola documentation via the
    AI Service (server-side RAG; no local corpus). Returns the answer text
    plus source URLs. --json emits {{query, text, source_urls}}. Unlike
    `kai ask` this does NOT see project data -- it is documentation-only,
    works with any token, and is the right tool for "how do I ..." questions.

### Developer Portal (since v0.49.0)

  The `dev-portal` command group talks to `apps-api.keboola.com` (the Keboola
  Developer Portal) and lets component developers register and update components
  without leaving the terminal.

  **Safety contract**: reads are unrestricted. Writes (`create`, `patch`,
  `upload-icon`, `publish`, `deprecate`) always print the full pending request
  and then require the user to type a random hex code on a real TTY. There is
  no `--yes` flag and no env-var bypass; non-TTY shells exit 6. Use `--dry-run`
  to get a clean exit-0 preview (the agent-safe path).

  **Identity management** -- portal logins are stored per-alias in `config.json`:

    kbagent dev-portal identity add --alias vendor-keboola \\
      --username service.keboola.xxxxx --password ... --vendor keboola \\
      --role-hint vendor    # default; restricts PATCH to vendor endpoint
    kbagent dev-portal identity add --alias admin-keboola \\
      --username admin@keboola.com --role-hint admin --password-stdin
    kbagent dev-portal identity use vendor-keboola

  **`role_hint` is load-bearing (since v0.51.1)**: `vendor` (default) routes
  `dev-portal patch` to `PATCH /vendors/{{vendor}}/apps/{{app}}` (restricted
  schema); `admin` routes it to `PATCH /admin/apps/{{app}}` (permissive
  schema). The admin endpoint is the **only** way to set the 9 fields
  apps-api `.forbidden()`s on vendor: `complexity`, `categories`, `category`,
  `features`, `forwardToken`, `forwardTokenDetails`, `injectEnvironment`,
  `processTimeout`, `requiredMemory`. Sending any of those with a `vendor`
  identity fails fast at preflight with the exact command to switch
  identity (server-side it would have returned a misleading 422 saying
  "must be one of: easy, medium, hard"; that message is a known apps-api
  bug -- the field is actually `forbidden()`, not enum-validated).

  **`--password-stdin` (since v0.51.1)** works on TTY (hidden line-based
  prompt, Enter to confirm) AND on a pipe (`echo $PASS | … --password-stdin`,
  reads to EOF). Pre-0.51.1 the flag hung interactively because it always
  waited for EOF.

  **Read commands** (unrestricted; good for peer-config research):

    kbagent --json dev-portal list --vendor keboola
      List all apps for a vendor. Use for peer research: compare how existing
      extractors configure uiOptions, encryption, defaultBucket, etc.

    kbagent --json dev-portal get --app keboola.ex-db-mysql
      Full portal entry for one component. Pull two peers and compare.

  **Write commands** (require random-code TTY confirm; use --dry-run first):

    kbagent dev-portal create --vendor V --data FILE [--dry-run]
    kbagent dev-portal patch --app VENDOR.APP_ID (--data FILE | --property KEY ...) [--dry-run]
    kbagent dev-portal upload-icon --app VENDOR.APP_ID --file PATH [--dry-run]
    kbagent dev-portal publish --app VENDOR.APP_ID [--dry-run]
    kbagent dev-portal deprecate --app VENDOR.APP_ID [--dry-run]

  **Identity lifecycle**:

    kbagent dev-portal identity add / list / remove / edit / use / current / verify

  **Identity selection**: pass `--identity <alias>` on any command, or set the
  default with `dev-portal identity use <alias>`.

### Utility Commands

  kbagent init [--from-global] [--project ALIAS ...]
    Create local .kbagent/ workspace. --from-global copies existing projects;
    --project ALIAS (repeatable) copies only the named project(s) and implies
    --from-global.

  kbagent context
    Show this reference text.

  kbagent serve [--host HOST] [--port PORT] [--ui] [--ui-dist PATH] [--reload]
                [--log-level LVL] [--cors-origin ORIGIN] [--config-dir DIR]
    Launch the FastAPI HTTP server backing the web UI. Two modes:

    - `--ui` (single-process, recommended): bundles the built React SPA from
      `--ui-dist PATH` (default: shipped `web/frontend/dist`) and mounts it at
      `/`. The bearer token is injected via an HttpOnly `kbagent_session`
      cookie on the SPA bootstrap, so the browser is already authenticated
      and no token leaves the terminal. EventSource SSE connections use the
      same cookie; nothing leaks into URLs or proxy logs.
    - No `--ui`: API-only mode; the SPA must be served separately (the
      legacy three-process dev setup with web/backend + web/frontend).

    Prints the bearer token to stdout on startup (use it for `kbagent http`
    subprocesses). Requires the optional 'server' extra:
    `uv pip install -e ".[server]"`.

  kbagent doctor
    Health checks (no --fix since 0.85.0 -- it only installed the MCP server).
    Inside a sync working tree, the sync_secrets check (since 0.55.0) warns about
    in-sync configs that still hold plaintext #-secrets (#378); skipped outside a
    sync tree. The mcp_tool_tasks check FAILs on agent tasks still using the
    removed `--type mcp_tool`; recreate them as `--type cli_command`.

  kbagent version [--beta]
    Version info for kbagent. Reports both the locally installed version and
    the latest available; flags any staleness. Since 0.85.0 it reports kbagent
    only -- keboola-mcp-server is a separate distribution kbagent no longer
    tracks, so there is no `dependencies` key in --json.
    --beta (since 0.42.0) reports the latest pre-release (beta / rc) instead
    of the latest stable. Same env override: KBAGENT_INCLUDE_PRERELEASE=1.

  kbagent update [--beta]
    Upgrade kbagent. Since 0.85.0 this is the ONLY thing `update` touches --
    keboola-mcp-server is neither installed nor refreshed by kbagent; keep it
    fresh yourself with `uv tool install --upgrade --prerelease=allow
    keboola-mcp-server` (see docs/mcp-migration.md). The same flow runs
    automatically on every kbagent startup -- the explicit `update` command
    forces a fresh check.
    Since 0.79.0 a STANDALONE (PyInstaller) binary -- Chocolatey / WinGet /
    Homebrew / apt / dnf / signed zip -- refuses the kbagent stage and reports
    that channel's own upgrade command instead; a uv/pip reinstall there
    installs a SECOND, unrelated kbagent rather than upgrading the packaged
    one. `version --json` then carries kbagent.install_channel and
    kbagent.upgrade_hint; upgrade_command is empty for a hand-unpacked
    archive.
    --beta (since 0.42.0) opts into pre-release versions (PEP 440 betas/rc,
    e.g. 0.43.0b1). Without --beta the auto-update path uses GitHub's
    /releases/latest endpoint, which excludes prereleases server-side --
    stable users never silently land on a beta. Set
    KBAGENT_INCLUDE_PRERELEASE=1 in env to make every update in the session
    treat betas as installable without re-typing --beta.

  kbagent changelog [--limit N] [--full]
    Show recent changelog (what changed in each version). Default: last 5
    versions, one-line summary each; --full (-v) expands every note.

  kbagent permissions list [--category read|write|destructive|admin]
    List all operations with risk categories and current allowed/denied status.

  kbagent permissions show
    Show current active permission policy.

  kbagent permissions set --mode allow|deny [--allow PATTERN ...] [--deny PATTERN ...]
    Set firewall-style permission policy. Patterns: exact (branch.delete),
    glob (sync.*), category (cli:read, cli:write, cli:destructive).
    NOTE: `tool:*` patterns are INERT since 0.85.0 -- the MCP passthrough
    they matched is gone. A persisted policy still loads with them, but they
    match nothing, so a mode=deny policy whose only allowance was `tool:read`
    now denies everything. Rewrite such a policy with `cli:read`.

  kbagent permissions reset
    Remove all restrictions.

  kbagent permissions check OPERATION
    Check if operation is allowed. Exit 0=allowed, 6=denied. Reflects the
    EFFECTIVE policy: persisted policy MERGED with --deny-writes /
    --deny-destructive session flags (since 0.30.5; pre-0.30.5 consulted
    only the persisted policy and could mislead self-introspection).

## Tips for AI Agents

1. ALWAYS use --json flag for reliable, parseable output:
     kbagent --json project list

2. JSON response format:
     Success: {{"status": "ok", "data": ...}}
     Error:   {{"status": "error", "error": {{"code": "...", "message": "...", "retryable": true/false}}}}
   Check "retryable" -- if true, retry the operation.

3. Multi-project: most read commands accept repeatable --project flag.
   Omit --project to query ALL connected projects in parallel.

4. Tokens are always masked in output (e.g. 901-...XXXX) -- expected behavior.

5. Common workflow -- explore a project:
     kbagent --json project list
     kbagent --json config list --project prod
     kbagent --json config detail --project prod --component-id ID --config-id ID
     kbagent --json job list --project prod --status error --limit 10

6. Health check and setup:
     kbagent --json doctor           # full health check
     kbagent --json project status   # test all connections

7. Environment variables:
     KBAGENT_CONVERSATION_ID  Conversation/session ID (REQUIRED -- sent as X-Conversation-ID header)
     KBC_TOKEN                Storage API token (fallback for --token)
     KBC_STORAGE_API_URL      Default stack URL (fallback for --url)
     KBC_MANAGE_API_TOKEN     Manage API token (org setup, project refresh, data-app password).
                              Default-DENY since 0.29.0: pass --allow-env-manage-token
                              to opt in, otherwise this var is ignored and a TTY prompt
                              is required. Closes AI-exfiltration via subprocess env.
     KBC_MASTER_TOKEN         Master token for sharing ops (global fallback)
     KBC_MASTER_TOKEN_*       Per-project master token (e.g. KBC_MASTER_TOKEN_PROD)
     KBAGENT_CONFIG_DIR       Override config directory
     KBAGENT_PROJECT          Override the pinned default project for this shell/session (beats pin, loses to --project)
     KBAGENT_PROJECT_FROM_ENV Set to "1" (or true/yes/on) to synthesize an in-memory project under the
                              reserved alias __env__ from KBC_TOKEN + KBC_STORAGE_API_URL (since 0.50.0).
                              Headless / token-only mode: no `project add`, no config.json on disk. Use
                              `--project __env__` (or rely on it as the sole/default project). The token
                              lives in memory only -- it is NEVER persisted, even if a write op runs.
                              Works for both the CLI and `kbagent serve`. Fails fast if the flag is set
                              but KBC_TOKEN / KBC_STORAGE_API_URL are missing.
     KBAGENT_MAX_PARALLEL_WORKERS  Max concurrent threads for multi-project ops (default 10, max 100)
     KBAGENT_AUTO_UPDATE      Set to "false" to disable automatic update on startup
     KBAGENT_UPDATE_TIMEOUT   Integer seconds; overrides the 300s self-update subprocess timeout
                              (raise for slow WSL git+ source builds). Since 0.60.0 install/update
                              prefer a prebuilt wheel Release asset, so timeouts are rare.
     KBAGENT_UPDATED_FROM     Set to an older version to trigger "What's new" display on next run
     KBAGENT_INCLUDE_PRERELEASE  Set to "1" (or "true"/"yes"/"on") to opt into pre-release versions for
                              `kbagent update` / `kbagent version` in this shell (equivalent to --beta flag,
                              since 0.43.3). NEVER affects the startup auto-update hook -- that path is
                              stable-channel-only by design so betas stay an explicit per-invocation choice.

8. Config resolution order:
     --config-dir flag > KBAGENT_CONFIG_DIR env > .kbagent/ in CWD/parents > ~/.config/keboola-agent-cli/

9. Historical MCP tool names: the `tool` group was removed in v0.85.0. If a
   user or an old script names a tool (get_configs, query_data, ...), map it to
   its native command with docs/mcp-migration.md -- do not try `tool call`.

10. Parquet export (typed analytics data, no CSV round-trip):
     # Export + download as Parquet dataset (default layout mirrors Keboola addressing)
     kbagent storage unload-table --project prod \\
       --table-id in.c-my-bucket.my-table \\
       --file-type parquet --download
     # -> ./prod/in.c-my-bucket.my-table.parquet/
     #      ├── <slice>.parquet
     #      └── _manifest.json   (underscore -> skipped by pyarrow/Spark/DuckDB)

     # Read the whole dataset in one line -- directory is a valid Parquet dataset:
     # python: pyarrow.parquet.read_table("./prod/in.c-my-bucket.my-table.parquet/")

     # Export only (stays in Keboola Storage Files, no local copy):
     kbagent --json storage unload-table --project prod \\
       --table-id in.c-bucket.t --file-type parquet --tag daily
     # -> {{"file_id": 123, "file_type": "parquet", "is_sliced": true, ...}}

     # Download an existing sliced .parquet Storage File (auto-detected):
     kbagent storage file-download --project prod --file-id 123 --output ./dir/

     Notes:
     - Parquet output is ALWAYS sliced -> directory, never a single file.
     - Default download path: ./{{project_alias}}/{{table_id}}.parquet/ -- override with --output.
     - CSV concat logic is never used for parquet; slices have their own footers.

## Exit Codes

  0  Success
  1  General error
  2  Usage error (invalid arguments)
  3  Authentication error (invalid or expired token)
  4  Network error (timeout, unreachable server)
  5  Configuration error (corrupt config, missing alias)
  6  Permission denied (operation blocked by policy)

When you receive a non-zero exit code, use --json to get structured error details.

## Claude Code Plugin

If you are using Claude Code, install the kbagent plugin for richer guidance:

  /plugin marketplace add keboola/cli
  /plugin install kbagent@keboola-agent-cli

The plugin provides a skill with detailed workflow references including:
- SQL transformation migration (input mapping removal, Snowflake paths)
- Workspace SQL debugging
- Development branch lifecycle
- Configuration scaffolding and sync (GitOps)
- Common Snowflake gotchas (MULTI_STATEMENT_COUNT, quoting, etc.)

The skill triggers automatically when you mention Keboola-related tasks.
Without the plugin, this `kbagent context` output is your standalone reference.
"""


def context_command(ctx: typer.Context) -> None:
    """Show usage instructions for AI agents interacting with Keboola."""
    formatter = get_formatter(ctx)

    if formatter.json_mode:
        # In JSON mode, output the context text as structured data
        data = {
            "version": __version__,
            "context": AGENT_CONTEXT,
        }
        formatter.output(data)
    else:
        formatter.console.print(AGENT_CONTEXT)
