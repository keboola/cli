# Developer Portal workflow

> Audience: a Keboola component developer or a kbagent agent acting on their
> behalf. Goal: safely register, inspect, and update components in the
> Keboola Developer Portal (`apps-api.keboola.com`).

## Identity model

Developer Portal logins are email + password (with MFA on personal
accounts). kbagent stores identities per-alias in the same `config.json`
as KB project tokens, under 0600 protection:

```
kbagent dev-portal identity add --alias vendor-keboola --username service.keboola.xxxxx --password ... --vendor keboola
kbagent dev-portal identity add --alias vendor-kds     --username service.kds-team.xxxxx --password ... --vendor kds-team
kbagent dev-portal identity add --alias admin-keboola  --username admin@keboola.com --role-hint admin --password-stdin
kbagent dev-portal identity use vendor-keboola         # default for subsequent commands
```

Service accounts (`service.{vendor}.{id}`) skip MFA. Personal admin
accounts prompt for the MFA code on `/dev/tty` at login time
(`SOFTWARE_TOKEN_MFA`, i.e. a TOTP authenticator app like 1Password / Authy /
Google Authenticator).

`--password-stdin` works in both pipe mode (`echo $PASS | … --password-stdin`,
reads to EOF) and TTY mode (hidden line-based prompt, Enter to confirm).

### `role_hint` is load-bearing (since v0.51.1)

`--role-hint` is **not** a free-text label. It picks which apps-api
endpoint kbagent uses for `dev-portal patch`:

| Role | PATCH endpoint | Schema | Use for |
|------|----------------|--------|---------|
| `vendor` (default) | `/vendors/{vendor}/apps/{app}` | `clientAppSchema` (restricted) | Cookiecutter-backed properties, schemas, UI options, descriptions, icon |
| `admin` | `/admin/apps/{app}` | `adminAppSchema` (permissive) | The 9 fields forbidden on vendor: `complexity`, `categories`, `category`, `features`, `forwardToken`, `forwardTokenDetails`, `injectEnvironment`, `processTimeout`, `requiredMemory` |

`role_hint` is validated (`vendor` or `admin`, case-folded). kbagent does
not verify the server-side role of the credential -- if you set `admin`
but the account isn't actually a portal admin, the PATCH fails at the
apps-api with an unambiguous 403.

When a vendor-role identity tries to patch one of the 9 admin-only
fields, the service **fail-fasts** with a message that names the
offending fields, explains why the server's 422 ("must be one of: ...")
is misleading, and shows the exact command to add and use an admin
identity. No portal call is made.

## Safety contract (read this before issuing any write)

- Reads are free: `dev-portal list`, `dev-portal get`.
- Writes (`create`, `patch`, `upload-icon`, `publish`, `deprecate`) always:
  1. Print the exact pending request to stderr (full diff for `patch`).
  2. Require the user to type a random hex code into the TTY.
  3. Exit 6 on a non-TTY shell.
- There is no `--yes`. There is no env-var bypass. By design.
- `--dry-run` prints the same preview and exits 0 without prompting. This
  is the agent-safe path.
- Caveat: `patch --dry-run` and `publish --dry-run` still **log in and GET
  the current app** (to compute the diff / run the publish pre-flight), so
  they need portal connectivity. On a personal (MFA) identity they will
  prompt for an MFA code on `/dev/tty` and fail with `DP_MFA_REQUIRED` in a
  non-interactive shell. For a fully non-interactive preview, use a
  `service.{vendor}.{id}` identity (no MFA). `create`/`upload-icon`/
  `deprecate` dry-runs are purely local (no portal call).

## The loop

1. Identify the component (vendor + app id). For an existing repo, check
   `.github/workflows/*.yml` for `KBC_DEVELOPERPORTAL_VENDOR` and `KBC_DEVELOPERPORTAL_APP`.
2. `kbagent --json dev-portal list --vendor <V>` and/or
   `kbagent --json dev-portal get --app VENDOR.APP_ID` to inspect.
3. Build a payload file (a JSON file — never inline JSON, shell quoting
   is unsafe with portal property names that contain spaces).
4. `kbagent dev-portal patch --app VENDOR.APP_ID --data /tmp/p.json --dry-run`
   — print the diff, show it to the user.
5. The user runs the same command without `--dry-run` and types the code.

## Peer-config research

Designing a new component? Pull reference configurations from existing
peers:

```
# List candidates
kbagent --json dev-portal list --vendor keboola | jq '.[] | select(.type=="extractor") | .id'

# Pull two peers in full
kbagent --json dev-portal get --app keboola.ex-db-mysql > /tmp/peer-mysql.json
kbagent --json dev-portal get --app keboola.ex-db-pgsql > /tmp/peer-postgres.json
```

Compare them yourself — the agent has the reasoning ability to spot
patterns. No dedicated `peers` command needed.

## Boundaries (what this surface does NOT own)

- Image push to ECR — stays in component GitHub Actions.
- Bulk repo-file -> property sync on deploy — stays in
  `scripts/developer_portal/update_properties.sh` (Cookiecutter-backed files).
- Writes to `component_config/` — never. That directory is governed by the
  Cookiecutter template; portal-direct properties (`uiOptions`,
  `encryption`, `defaultBucket`, …) live only in the portal.
