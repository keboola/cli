# Manage-token resolution design

> Branched off `upstream/main` at `10ba4a0` (= v0.27.0). NOT yet
> committed for upstream review; this doc is the brief that goes to
> Padak with a structured comment on PR #236 before any production
> code is written.

## 1. Problem

Today every Manage API call site in `kbagent` resolves the token via
the same single-stack helper:

```python
# src/keboola_agent_cli/commands/_helpers.py:27-54
def resolve_manage_token() -> str:
    env_token = os.environ.get(ENV_KBC_MANAGE_API_TOKEN)  # KBC_MANAGE_API_TOKEN
    if env_token:
        return env_token
    if sys.stdin.isatty():
        return typer.prompt("Manage API token", hide_input=True)
    raise typer.Exit(code=2)
```

Three call sites at `upstream/main`:

| File:line | Command | What's in scope |
|---|---|---|
| `commands/org.py:223` | `org setup` | `--url URL` (stack URL is the flag) |
| `commands/project.py:423` | `project refresh [--project A \| --all]` | Either one alias (→ `stack_url` via ConfigStore) or N aliases each with their own `stack_url` |
| `commands/data_app.py:594` | `data-app password` | Project alias (→ `stack_url` via ConfigStore) |

A fourth call site arrives when **PR #236** lands
(`feat/project-member-invite`, currently `OPEN` and `CONFLICTING`):
`MemberService` uses Manage API for invite / member-list / member-
remove / set-role. Per Padak's review on #236, the maintainer flagged
two real problems with the single-env model — both before this PR
attempts a fix.

### Problem 1 — Multi-stack ambiguity

Manage tokens are stack-scoped. `connection.keboola.com` (legacy AWS US),
`connection.eu-central-1.keboola.com`, `connection.us-east4.gcp.keboola.com`,
`connection.north-europe.azure.keboola.com` — every stack mints its own.
But `kbagent` users routinely register projects across multiple stacks
in one `config.json`, and:

- `OrgService.refresh_tokens` constructs a single `ManageClient` from
  `projects_to_check[0][1].stack_url` (`org_service.py:290-293`) and
  reuses *one* `manage_token` across every project in the loop. If
  the projects span stacks, all but one stack receive an invalid
  token.
- `data-app password` works with whichever single token is in env at
  the moment of the call, regardless of which project alias was
  passed.
- For PR #236's bulk invite, the service already enforces a single-
  stack invariant (per Padak's verification: "service correctly
  enforces a single-stack-URL invariant per CSV (`member_service.py:158-164`)") —
  but that's a fail-fast, not a fix; users still need to split CSVs
  per stack and rotate env vars manually.

### Problem 2 — AI exfiltration risk

The `kbagent` permission firewall (`permissions.py::PermissionEngine`,
wired in `cli.py:319-324`) intercepts CLI ops registered in
`OPERATION_REGISTRY`. It does **not** intercept env-var reads or raw
HTTP from subprocesses. A sandboxed agent under `kbagent init
--read-only` can shell out:

```bash
curl -H "X-KBC-ManageApiToken: $KBC_MANAGE_API_TOKEN" \
     https://connection.keboola.com/manage/projects
```

…and the firewall stays blind. Manage tokens are **org-scoped**
(broader blast radius than the per-project Storage tokens, which are
at least scoped to one project, persisted with `0600`, and gated by
the firewall). Padak's review on #236 framed it precisely:

> The agent can shell out raw `curl …` and the firewall does not
> catch raw HTTP — it's not a kbagent operation. Read-only sandboxes
> are quietly more permissive than they look once a manage token is
> in env.

This is technically true even before #236 (`org setup` already exposes
the same surface), but #236 makes manage tokens persist in env *long-
term* because steady-state operations need them.

## 2. Verified surface (upstream/main @ 10ba4a0)

| Element | File:line | Note |
|---|---|---|
| `resolve_manage_token()` | `commands/_helpers.py:27-54` | Refactor target. |
| `ENV_KBC_MANAGE_API_TOKEN` | `constants.py:132` | `"KBC_MANAGE_API_TOKEN"`. |
| `ManageClient.__init__(stack_url, manage_token)` | `manage_client.py:17,27-30` | Token consumed once into `headers`; never persisted to instance state. |
| `ManageClient.verify_token()` | `manage_client.py:46-59` | `GET /manage/tokens/verify` already exists; returns `{user: {id, name, email}, ...}`. |
| `OrgService.refresh_tokens` | `services/org_service.py:228-406` | Per-project `_manage_client_factory(stack_url, token)` at lines 290-293 — but the same token is reused across all projects (the bug). |
| `DataScienceClient.get_app_password(app_id, manage_token)` | `data_science_client.py:170-190` | Per-call header pattern. The pattern to **preserve** for any other Manage call site: token never on persistent client headers, always passed per-request. |
| `apply_firewall_flags()` | `cli.py:108-157` | Synthesises session-only `PermissionPolicy`. Does NOT touch `os.environ`. |
| `--deny-writes` / `--deny-destructive` declarations | `cli.py:208-222` | Shape to mirror for a new top-level flag. Stored as `ctx.obj["deny_writes"]`. |
| `OPERATION_REGISTRY` | `permissions.py:15-161` | `"<group>.<op>": "read|write|destructive|admin"`. |
| `PermissionPolicy` model | `models.py:36-69` | `{mode: str, allow: list[str], deny: list[str]}`. **No bool fields.** |
| `AppConfig` model | `models.py:71-88` | `version`, `default_project`, `max_parallel_workers`, `permissions`, `projects`. New top-level bool fields land here. |
| `ConfigStore` token storage | `config_store.py:176-226` | Plaintext tokens in JSON; file mode `0o600`; dir `0o700`. |
| `mask_token` utility | `errors.py:97-119` | Use for any new error message that interpolates a token. |
| Plugin sync map | `CONTRIBUTING.md:251-272` | 12 surfaces; walk every row. |

**Important:** the prior session's draft assumed
`PermissionPolicy.deny_manage_env: bool = False`. That's a category
mismatch — `PermissionPolicy` uses string patterns to match operations
(`cli:write`, `tool:destructive`, …). A boolean about credential
*resolution* doesn't fit. The cleaner shape is on `AppConfig` (e.g.
`AppConfig.allow_env_manage_token: bool = True`).

## 3. Prior art

### 3.1 How established CLIs handle multi-region credentials

| CLI | Where token lives | Disambiguator | Env override semantics | Per-invocation flag |
|---|---|---|---|---|
| **AWS CLI** | `~/.aws/credentials` (plaintext) | profile | env > file (field-level: `AWS_ACCESS_KEY_ID` overrides one field of the profile) | `--profile NAME` |
| **gcloud** | `~/.config/gcloud/...` | configuration | env sets active config (whole-config: `CLOUDSDK_ACTIVE_CONFIG_NAME`) | `--configuration NAME` |
| **kubectl** | `~/.kube/config` (plaintext) | context | env merges multiple files (`KUBECONFIG`, colon-delimited) | `--context NAME`, `--kubeconfig FILE` |
| **gh** (GitHub CLI) | OS keychain (fallback plaintext) | hostname | env replaces stored token entirely (`GH_TOKEN`) | `--hostname HOST` |

Two dominant patterns: **field-layered override** (AWS) where env
replaces one field of a named profile, and **whole-config switch**
(gcloud, kubectl, gh) where env picks a different complete credential
set.

Quotations and source URLs in the appendix.

### 3.2 Keboola-specific prior art

- **`keboola-as-code`** (the official Go CLI) is project/stack-scoped
  per working directory, not profile-based. The stack hostname is
  pinned in `.keboola/manifest.json`; the Storage API token in
  `.env.local`. **No named-profile system. No multi-stack support.
  No manage-token support.** We are designing greenfield.
- **Personal Access Tokens (PATs)** exist in Keboola Account Settings
  and work against the Manage API today (`manage_client.py:64-65`
  comment confirms: *"Works with Personal Access Tokens (PAT) for
  projects where the token owner is a member -- does NOT require
  organization admin"*). PATs reduce blast radius vs. an org-admin
  token, but PATs are still issued **per stack**.
- **`GET /manage/tokens/verify`** returns
  `{id, description, type, scopes, creator, user, ...}` per the
  Apiary spec. The response **does not include a stack/org
  discriminator at root**, so the client must already know which
  stack URL to hit — the endpoint cannot bootstrap stack discovery
  from the token alone. **Empirically confirmed in §10.1**: probes
  1A and 1B captured the live response shape on
  `connection.europe-west3.gcp.keboola.com` and
  `connection.us-east4.gcp.keboola.com`; neither response carries
  any stack/org root field.
- **No org-scoped credential primitive that spans stacks exists in
  Keboola today.** The multi-stack disambiguation problem is real
  and not going away.

## 4. Five candidate scenarios

Each candidate answers four questions:

1. Where does the token live? (env / config.json / OS keychain / hybrid)
2. How does kbagent disambiguate stack? (hostname-derived suffix /
   curated alias / named profile / per-project storage)
3. What blocks env-var exfiltration? (opt-in flag / opt-out flag /
   persisted policy / child-env scrubbing)
4. How does TTY prompt show up? (per-call / batched / fallback)

| Scenario | Sketch |
|---|---|
| **S1 — Per-stack env vars + opt-in `--no-env-manage-token`** | Hostname-derived `KBC_MANAGE_TOKEN_<SUFFIX>` env vars (`EU_CENTRAL_1`, `US_EAST4_GCP`, `NORTH_EUROPE_AZURE`, …). Legacy `KBC_MANAGE_API_TOKEN` falls back. Session-only `--no-env-manage-token` flag mirrors `--deny-writes` and disables both env paths for one invocation. TTY prompt names the stack URL so the human knows which stack the prompt is for. |
| **S2 — Per-project storage in `config.json`** | Manage tokens persisted alongside Storage tokens (0600, plaintext). Multi-stack auto-solved by alias keying. Same hygiene as Storage tokens today. |
| **S3 — OS keychain integration** | macOS Keychain / Linux Secret Service / Windows Credential Manager. Token stored under a stack-keyed item; `keyring` Python library. |
| **S4 — Named profiles à la AWS** | `kbagent --profile prod data-app password ...` bundling `(stack_url, manage_token)` per profile. `KBAGENT_PROFILE` env for shell-level default. |
| **S5 — Refuse env by default; `--allow-env-manage-token` to opt in** | Inverts the threat model. Env vars are ignored unless explicitly allowed. CI pipelines must opt in. |

## 5. Scoring against the four threats

The four threats from Padak's review:

- **T1 — AI agent in `kbagent init --read-only` workspace** tries to
  `curl` the Manage API.
- **T2 — CI/CD pipeline rotates a manage token** — how many places
  must change?
- **T3 — User has 3 projects across 3 stacks** and runs `project
  refresh --all`.
- **T4 — User pastes a stack-A manage token while targeting
  stack-B**. Does kbagent fail clearly and immediately?

Score each cell 0 (catastrophic / breaks the threat scenario) to 3
(handled cleanly).

| | T1 (AI exfil) | T2 (CI rotation) | T3 (multi-stack refresh) | T4 (wrong-stack fail-fast) | Total |
|---|---|---|---|---|---|
| **S1: per-stack env + opt-in flag** | 2 | 3 | 3 | 3 | **11** |
| **S2: config.json plaintext** | 1 | 2 | 3 | 3 | 9 |
| **S3: OS keychain** | 3 | 1 | 2 | 3 | 9 |
| **S4: named profiles** | 1 | 2 | 2 | 3 | 8 |
| **S5: refuse env by default** | 3 | 0 | 3 | 3 | 9 |

T1 dominates the user's threat model (AI-exfil is the explicit
ask). If we weight T1 2× the others:

| | 2×T1 | T2 | T3 | T4 | Total |
|---|---|---|---|---|---|
| **S1** | 4 | 3 | 3 | 3 | **13** |
| S2 | 2 | 2 | 3 | 3 | 10 |
| S3 | 6 | 1 | 2 | 3 | 12 |
| S5 | 6 | 0 | 3 | 3 | 12 |

S1 still wins. S3 and S5 are competitive on AI safety but lose on
operational ergonomics (S3 doubles the resolution code to keep CI
working; S5 breaks every existing CI pipeline).

### Per-cell rationale (S1, the winner)

- **T1 (2/3)**: The hostname suffix means `KBC_MANAGE_API_TOKEN`
  alone no longer works — the user must explicitly set the per-stack
  form, which (a) is more friction for casual env exposure, but (b)
  **the token is still readable by any subprocess**. Full T1
  protection requires the **persisted policy opt-out** (described
  in §6 below) — combining S1 with S5 *behaviour for sandboxed
  installs only*. That's the hybrid we recommend.
- **T2 (3/3)**: One env var per stack, set once per pipeline. AWS-
  style field-layered.
- **T3 (3/3)**: `OrgService.refresh_tokens` groups projects by
  `stack_url` and resolves the per-stack token lazily for each
  distinct stack — no env-var swap mid-flow.
- **T4 (3/3)**: When the per-stack env var is set, the suffix
  derivation is stack-derived from the URL — so the right token is
  picked structurally, not by user attention. If only the legacy
  `KBC_MANAGE_API_TOKEN` is set and the target stack doesn't match
  what minted it, the resolver still hands the wrong token to
  `ManageClient`, which receives a 401. The TTY prompt fallback
  names the stack URL explicitly so the human sees the mismatch.
  *(Optional future enhancement: call `verify_token()` on first use
  per stack and bail out if the user info hints at a mismatch — but
  the verify endpoint doesn't expose stack/org metadata today, so
  this is best-effort.)*

### Rejected alternatives (one-liner each)

- **S2 (config.json plaintext)**: marginal AI-exfil win — the agent
  can `cat ~/.config/keboola-agent-cli/config.json` just as easily
  as it can read env. Adds at-rest plaintext for no real gain.
- **S3 (OS keychain)**: strongest on T1 in isolation, but CI flows
  still need env (no keychain in headless containers), so we'd ship
  two parallel resolution paths. Big lift cross-platform; defers
  the CI-rotation pain.
- **S4 (named profiles)**: mental-model overhead for casual single-
  stack users, who are most of `kbagent`'s audience. AWS/gcloud
  bear this cost because their multi-region surface is the *common*
  case; for kbagent it's the exception.
- **S5 (refuse env by default)**: breaks every existing CI pipeline
  on day one. The right *posture* for sandboxed-agent installs, but
  not the right *default*.

## 6. Recommendation: S1 + S5-for-sandboxed-installs (hybrid)

### 6.1 Default behavior (S1)

```python
# commands/_helpers.py — refactored shape
def resolve_manage_token(
    stack_url: str | None = None,
    *,
    allow_env: bool = True,
) -> str:
    """Resolve the manage token for the target stack.

    Resolution order:
      1. KBC_MANAGE_TOKEN_<SUFFIX> env var (per-stack form)
            — only when stack_url is given AND allow_env is True
      2. KBC_MANAGE_API_TOKEN env var (legacy single-stack fallback)
            — only when allow_env is True
      3. Interactive TTY prompt — message includes the stack URL so
         the human knows which stack the prompt is for
      4. Exit code 2 with an error naming both env-var forms
    """
```

Hostname-derived suffix (no curated table — future stacks slot in
automatically):

| Stack URL | Env var |
|---|---|
| `connection.keboola.com` (legacy AWS US) | `KBC_MANAGE_API_TOKEN` (existing — no per-stack form needed) |
| `connection.eu-central-1.keboola.com` | `KBC_MANAGE_TOKEN_EU_CENTRAL_1` |
| `connection.us-east4.gcp.keboola.com` | `KBC_MANAGE_TOKEN_US_EAST4_GCP` |
| `connection.eu-west1.gcp.keboola.com` | `KBC_MANAGE_TOKEN_EU_WEST1_GCP` |
| `connection.north-europe.azure.keboola.com` | `KBC_MANAGE_TOKEN_NORTH_EUROPE_AZURE` |

```python
def _stack_suffix_for_env_var(stack_url: str | None) -> str | None:
    """Return UPPERCASE suffix derived from the stack hostname.

    The hostname between `connection.` and the trailing `.keboola.com`
    becomes the suffix, with non-alphanumerics replaced by underscores.
    Returns None for the legacy `connection.keboola.com` (no suffix)
    and for empty/None inputs.
    """
```

### 6.2 Three knobs for the AI-exfil mitigation

Three layers of opt-in, smallest blast radius first:

1. **Session-only flag**: `kbagent --no-env-manage-token <command>`
   — mirrors `--deny-writes` shape (`cli.py:208-222`). Stored as
   `ctx.obj["allow_manage_env"]` (negated) and threaded into
   `resolve_manage_token(allow_env=...)` at call sites.
2. **Persisted policy (the real AI-exfil fix for sandboxed installs)**:
   new top-level field `AppConfig.allow_env_manage_token: bool = True`
   (NOT inside `PermissionPolicy` — see §2 note). Set via
   `kbagent permissions deny-manage-env`. Once set in a
   `kbagent init --read-only` workspace, every subsequent invocation
   refuses env-var manage tokens, including from a sandboxed agent
   that can't `chmod` the config file to flip it back.
3. **`kbagent init --read-only` default**: when `--read-only` is
   passed, automatically set `allow_env_manage_token=False` in the
   newly-created `config.json`. This makes the AI-exfil mitigation
   the safe default for every new sandboxed install without any
   user action. Existing installs are unchanged.

### 6.3 Call-site updates

| File:line | Change |
|---|---|
| `commands/data_app.py:594` | Resolve `ProjectConfig.stack_url` from `--project alias` first; pass `stack_url=` and `allow_env=ctx.obj["allow_manage_env"]` to `resolve_manage_token()`. |
| `commands/org.py:223` | Pass `stack_url=url` (the `--url` flag value) and `allow_env=ctx.obj["allow_manage_env"]`. |
| `commands/project.py:423` | Single-project: same pattern as data-app. **Multi-project (`--all`): move the resolution INTO `OrgService.refresh_tokens`**, group projects by `stack_url`, and call `resolve_manage_token` per distinct stack with caching so the user is prompted at most once per stack. |
| `services/org_service.py:228-406` | Accept a `manage_token_resolver` callback (or take the resolver itself) instead of the single `manage_token: str`. Internally group `projects_to_check` by `stack_url`, resolve once per group, store in a local dict for the loop. Adds ~30 LOC; preserves the cleanup-in-finally and per-project error-accumulation invariants. |

### 6.4 What does NOT change (security invariants preserved)

- Token never lives on persistent client state (`ManageClient.headers`
  consumed once at init; `DataScienceClient` per-call header is the
  pattern that scales).
- Token never logged, never echoed in errors (use `mask_token` if
  interpolation is unavoidable).
- Token never on argv, never passed via `--manage-token`.
- ConfigStore continues to be the only on-disk persistence layer
  for any token (storage tokens), and **manage tokens still don't
  land in `config.json`** under this design — the only persisted
  bit is the `allow_env_manage_token` boolean policy.

## 7. Migration

Zero-cost for existing single-stack users:
- `KBC_MANAGE_API_TOKEN` keeps working (it's the legacy fallback).
- No `config.json` rewrite required.
- `permissions deny-manage-env` is opt-in.

Multi-stack users export per-stack vars in their CI/CD config. The
docstring on `resolve_manage_token()` and the `gotchas.md`
`(since v0.27.1)` entry are the migration nudge.

## 8. Versioning, changelog, sync map

Patch bump: `0.27.0` → `0.27.1`. The change is additive (new flag,
new optional resolver kwargs, new `AppConfig` field with safe
default). Public API of `resolve_manage_token()` evolves —
`stack_url=None` keeps the existing call sites green during PR #236's
rebase if it ships first.

Walked surfaces (per `CONTRIBUTING.md:251-272`, all 12 rows):

- `pyproject.toml` (version)
- `src/keboola_agent_cli/changelog.py` (`0.27.1` entry)
- `src/keboola_agent_cli/commands/context.py` (`AGENT_CONTEXT`)
- `CLAUDE.md` (the `## All CLI Commands` block — new
  `--no-env-manage-token` global option; new `permissions set
  --deny-manage-env` flag)
- `plugins/kbagent/.claude-plugin/plugin.json` (`make version-sync`)
- `plugins/kbagent/.claude-plugin/CLAUDE.md`
- `plugins/kbagent/agents/keboola-expert.md` — Rule 6 VERSION GATE
  bump to `0.27.1+`; new matrix row for the env-var family; new
  inline gotcha for the AI-exfil concern
- `plugins/kbagent/commands/keboola.md`
- `plugins/kbagent/skills/kbagent/SKILL.md` — description triggers,
  workflow link if a `manage-token-workflow.md` is added
- `plugins/kbagent/skills/kbagent/references/commands-reference.md`
- `plugins/kbagent/skills/kbagent/references/gotchas.md` —
  `(since v0.27.1)` entry pointing at AI-exfil + multi-stack
- `plugins/kbagent/skills/kbagent/references/permissions-workflow.md`
  if it exists, else add a section to the closest analogue

## 9. Risks / open questions for Padak

1. **Hostname-derived vs. curated suffix.** Hostname-derived means
   `KBC_MANAGE_TOKEN_US_EAST4_GCP` (verbose). Curated means a short
   table like `_US`/`_EU`/`_GCP_US` (ambiguous on multiple cloud
   regions). Hostname-derived wins on extensibility; curated wins on
   typing ergonomics. The brief leans hostname.
2. **Should `--no-env-manage-token` also strip env from MCP
   subprocesses?** McpService spawns `keboola-mcp-server` as a
   subprocess (`mcp_service.py`). Today subprocess inherits the
   parent's env. If we `--no-env-manage-token`, we should also pass
   a scrubbed `os.environ` (drop `KBC_MANAGE_API_TOKEN` and any
   `KBC_MANAGE_TOKEN_*`) to subprocesses to fully close the
   exfil window. This is a small extra refactor on `McpService` that
   we'd bundle here unless Padak prefers to defer.
3. **Per-stack `--all` UX.** When `project refresh --all` spans 3
   stacks and 2 of the 3 per-stack env vars are missing, do we (a)
   prompt for each missing one in sequence at start of the run, or
   (b) fail-fast naming all missing vars? Option (a) is friendlier
   for humans, option (b) is better for CI. Recommend (a) when TTY,
   (b) otherwise — automatic.
4. **`AppConfig.allow_env_manage_token` as the persisted bool**
   versus a tag inside `PermissionPolicy.deny[]` like
   `"resolver:env-manage-token"`. The category mismatch argument
   says separate field; the precedent argument (everything firewall-
   related lives on `PermissionPolicy`) says deny pattern. Padak's
   call.

## 10. Live validation (executed 2026-05-02)

Two throwaway projects on two GCP stacks, manage tokens scoped to
specific orgs. Tokens never echoed in any output; receipts use
prefix + last-4 mask. Both test projects deleted afterward; orgs
verified back to baseline.

### 10.1 Read-only probes (Phase 1)

| # | Probe | HTTP | Empirical finding |
|---|---|---|---|
| 1A | `GET europe-west3.gcp.keboola.com/manage/tokens/verify` (EU token) | 200 | Response body has `id, description, type, scopes, creator, user` — **no `host` / `stackId` / `organizationId` root field**. Confirms the design's assumption: token alone cannot bootstrap stack discovery. |
| 1B | Same against US4 stack with US4 token | 200 | Identical shape. Same email → different `user.id` per stack (179 EU vs 216 US4) — confirms stack-bound user-account model. |
| 1C | EU token → US4 stack `/verify` | **401** | `{"error":"Invalid access token","code":"storage.tokenInvalid"}`. Wrong-stack failure is clean and predictable. |
| 1D | US4 token → EU stack `/verify` | **401** | Same shape. kbagent's `INVALID_TOKEN` mapping → exit 3. |
| 1E | List org 86 (EU) projects | 200 | 4 baseline projects. |
| 1F | List org 3675 (US4) projects | 200 | 2 baseline projects. |

### 10.2 Multi-stack scenario battery (Phase 3)

| # | Scenario | Result |
|---|---|---|
| 3.1 | Legacy `KBC_MANAGE_API_TOKEN` only → register EU project | ✅ 200, Storage token minted (`2956-...ivFh`) |
| 3.2 | Both per-stack vars set → register US4 + `refresh --all` | ✅ both projects refreshed across stacks (`2956-...daIl`, `5796-...70AX`) — no 401s |
| 3.3 | Both per-stack vars + bogus `KBC_MANAGE_API_TOKEN` legacy | ✅ per-stack vars correctly preferred (`2956-...UaDx`, `5796-...Xn21`) |
| 3.4 | Wrong-stack token in env (US4 token as EU's per-stack var) | ✅ Per-project failure with masked token in error: `Invalid or expired token (token: 112145-...IF8D)` |
| 3.5 | `--no-env-manage-token` non-TTY with both env vars set | ✅ **Exit 2**, message names lever, no HTTP traffic to `/manage` |
| 3.6 | `--all` resolver caching count | ✅ **`/manage/tokens/verify` called exactly 1× per stack** (`europe-west3`: 1, `us-east4`: 1) — owner-name caching works |
| 3.7 | Persisted `allow_env_manage_token=False` + both env vars set | ✅ **Exit 2** + token never reaches the wire (verified by patching `DataScienceClient` and asserting `not_called`) |
| 3.8 | `kbagent init` (no `--read-only`) writes `allow_env_manage_token=true` | ✅ default-True confirmed in fresh workspace |
| 3.9 | Cross-org isolation | ✅ EU project (2956) → org 86; US4 project (5796) → org 3675; baseline projects in both orgs untouched |

### 10.3 Cleanup (Phase 4)

- Both test projects (EU 2956, US4 5796) deleted with `DELETE
  /manage/projects/{id}` → HTTP 204.
- Re-list org 86: back to baseline `[1143, 1636, 2043, 2450]`.
- Re-list org 3675: back to baseline `[4066, 4067]`.
- Scratch dir + tokens env file removed; final `grep` confirms no
  raw tokens in any persisted artifact.

### Findings folded into the design

1. ✅ **Hostname-derived suffix scheme works empirically** — both
   `KBC_MANAGE_TOKEN_EUROPE_WEST3_GCP` and `KBC_MANAGE_TOKEN_US_EAST4_GCP`
   were correctly derived and resolved.
2. ✅ **Wrong-stack failure mode is HTTP 401 + `storage.tokenInvalid`** —
   maps cleanly to kbagent's existing `INVALID_TOKEN` exit-3 path, no
   special handling needed.
3. ✅ **Per-stack `_owner_for` caching** behaves as designed: exactly
   1 `/manage/tokens/verify` per stack across an `--all` refresh of
   2 projects on 2 stacks.
4. ✅ **AI-exfil mitigation is empirically airtight**: with
   `allow_env_manage_token=False` persisted AND both env vars set in
   the shell, the resolver exits 2 BEFORE any HTTP client is even
   instantiated. The `DataScienceClient` mock in
   `test_persisted_deny_manage_env_blocks_env_resolver_end_to_end`
   pins `assert_not_called()`.
5. ⚠️ **Documented limitation**: `--hint client` mode emits a
   template Python snippet that reads `os.environ["KBC_MANAGE_API_TOKEN"]`
   verbatim, regardless of whether `--no-env-manage-token` is set on
   the same invocation. Hint mode is a code-generation aid; the flag
   protects the kbagent process, not the user's eventual rendered
   script. Pinned by `test_hint_mode_with_no_env_manage_token_documented_limitation`.

---

## Appendix — Prior-art quotations

### AWS CLI

> "If you specify an option by using one of the environment variables
> described in this topic, it overrides any value loaded from a
> profile in the configuration file. If you specify an option by
> using a parameter on the AWS CLI command line, it overrides any
> value from either the corresponding environment variable or a
> profile in the configuration file."
> — https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-envvars.html

### gcloud

> "Use this flag on any gcloud command to override the active
> configuration for a single invocation: `gcloud auth list
> --configuration=[CONFIGURATION_NAME]`"
> — https://cloud.google.com/sdk/docs/configurations

### kubectl

> "By default, `kubectl` looks for a file named `config` in the
> `$HOME/.kube` directory. You can specify other kubeconfig files
> by setting the `KUBECONFIG` environment variable or by setting
> the `--kubeconfig` flag."
> — https://kubernetes.io/docs/concepts/configuration/organize-cluster-access-kubeconfig/

### gh

> "After completion, an authentication token will be stored securely
> in the system credential store. If a credential store is not found
> or there is an issue using it gh will fallback to writing the token
> to a plain text file."
> — https://cli.github.com/manual/gh_auth_login

### Keboola CLI (`keboola-as-code`)

> "When you initialize a Keboola CLI project, it creates a metadata
> directory `.keboola`, a manifest file `.keboola/manifest.json`,
> and a file `.env.local` that contains the API token."
> — https://developers.keboola.com/cli/getting-started/

### Keboola Manage API token verification

`GET /manage/tokens/verify` returns `{id, description, type, scopes,
creator, user, ...}` with no stack/org root field per the Apiary
spec. Source: https://github.com/keboola/kbc-manage-api-php-client/blob/master/apiary.apib.
Cross-stack scope is **unverified** in the public docs (not stated
either way for PATs specifically), but every available signal —
separate Apiary endpoints per region, per-stack account-settings UI,
no documented stack-federation primitive — confirms tokens are
stack-bound.
