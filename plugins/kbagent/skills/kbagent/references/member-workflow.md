# Project Member & Invitation Workflow

Closes the long-standing Manage API gap that forced every Keboola-internal
automation (most recently the Cuesta-training orchestrator) to bypass kbagent
and POST raw HTTP at `/manage/projects/{id}/invitations`.

## Auth

All seven commands use the **Manage API**, not the Storage API. Provide the
manage token via `KBC_MANAGE_API_TOKEN` (env var or interactive prompt). The
manage token is *never* persisted to config.json, *never* accepted as a CLI
argument, *never* logged.

```bash
export KBC_MANAGE_API_TOKEN=<your-org-admin-or-PAT>
```

## Roles (whitelist)

The Manage API accepts exactly four role values:

| Role | Use case |
|------|----------|
| `admin` | Full project control (create/delete tokens, manage members, all data ops) |
| `share` | Read-only with sharing rights to other projects in the org |
| `readOnly` | Read-only |
| `guest` | Lowest blast radius; useful for temporary access (and for the e2e test) |

Both Typer (`click.Choice`) and `MemberService._validate_role()` enforce this
list. The whitelist is defined in `constants.PROJECT_ROLES`.

## Single invite

```bash
kbagent project invite --project prod --email a@b.com --role admin --reason "On-call rotation"
```

Returns:
```json
{
  "status": "ok",
  "invitation_id": 1741,
  "alias": "prod",
  "project_id": 5725,
  "email": "a@b.com",
  "role": "admin"
}
```

If the user is already invited or already a member, the API returns HTTP 400
and kbagent translates it to `{"status": "noop", "note": "already_invited" | "already_member"}` -- this is **not** an error and exit code stays 0.

## Bulk invite from CSV (the headline use case)

CSV header required. Recognised columns (case-insensitive): `email` (required),
`project` (alias) **or** `project_id` (numeric integer), `role`
(optional if `--default-role` is set), `reason` (optional). Extra columns are
ignored. Each row may pick a different project as long as **all rows resolve
to the same stack URL** (rows referencing multiple stacks raise upfront
before any HTTP call).

```csv
email,project,role,reason
ann@example.com,prod,admin,On-call
ben@example.com,staging,guest,Read-only access for QA
chen@example.com,5725,share,Shared bucket consumer
```

```bash
kbagent project invite --from-csv participants.csv --default-role guest --workers 8
```

Result schema:
```json
{
  "total": 3,
  "succeeded": 2,
  "noop": 1,
  "failed": 0,
  "rows": [
    {"email": "ann@example.com", "project": "prod", "role": "admin", "status": "ok", "invitation_id": 1741, ...},
    {"email": "ben@example.com", "project": "staging", "role": "guest", "status": "noop", "note": "already_invited", ...},
    {"email": "chen@example.com", "project": "5725", "project_id": 5725, "role": "share", "status": "ok", "invitation_id": 1742, ...}
  ],
  "dry_run": false
}
```

The `rows[]` array is in **completion order**, not CSV order (parallel
workers). Match by `email`, not by index. Partial-success exits 0 with
`failed > 0` reflected in the JSON; this mirrors `org setup`.

`--dry-run` resolves every row and reports what *would* happen without
sending invitations. Use it before any large CSV.

## Audit who is on a project

```bash
kbagent project member-list --project prod --include-pending
```

Returns active members + pending invitations in one shot:
```json
{
  "alias": "prod",
  "project_id": 5725,
  "members": [
    {"id": 216, "email": "max.ottomansky@keboola.com", "role": "admin", "status": "active", "mfa_enabled": true, ...},
    {"id": 4241, "email": "mfiser@cuestapartners.com", "role": "guest", "status": "active", "mfa_enabled": true, ...}
  ],
  "pending_invitations": [
    {"id": 1515, "user": {"email": "marcusscwong@gmail.com"}, "role": "admin", "reason": "", ...}
  ]
}
```

For the pending-only view: `kbagent project invitation-list --project prod`.

## Change a member's role

Uses HTTP **PATCH** under the hood (PUT returns 404 even on real members --
that's the Manage API's quirk, not a kbagent bug).

```bash
kbagent project member-set-role --project prod --email a@b.com --role guest
```

The service resolves `--email` to the numeric user_id by listing project
members and matching case-insensitively. The PATCH response includes the
updated user dict.

## Cancel a pending invitation

```bash
kbagent project invitation-cancel --project prod --email a@b.com --yes
```

Without `--invitation-id`, the service resolves the ID by listing pending
invitations and matching `--email`. With `--invitation-id ID`, it skips the
lookup. The DELETE returns 204 No Content on success; if the invitation has
already been deleted the API returns 404 with "Invitation not found".

## Remove an active member (destructive)

```bash
kbagent project member-remove --project prod --email a@b.com --yes
```

Resolves `--email` to user_id, then DELETEs `/manage/projects/{id}/users/{userId}`.
Permission category: `destructive` (re-adding requires sending a fresh invite).

## Idempotency cheat-sheet

| API response | kbagent translation | Exit code |
|--------------|--------------------|-----------|
| HTTP 201 invitation created | `status="ok"` | 0 |
| HTTP 400 "...already been invited..." | `status="noop"`, `note="already_invited"` | 0 |
| HTTP 400 "...already a member..." | `status="noop"`, `note="already_member"` | 0 |
| HTTP 400 "Role X is not valid..." | Re-raised; `--role` should be on the whitelist | 1 |
| HTTP 401 invalid manage token | `KeboolaApiError(INVALID_TOKEN)` | 3 |
| HTTP 403 manage token lacks org-admin | `KeboolaApiError(ACCESS_DENIED)` | 1 |
| HTTP 404 project / invitation not found | `KeboolaApiError(NOT_FOUND)` | 1 |

## When to use the Manage API direct-add (not in v0.29.0)

The Manage API also exposes `POST /manage/projects/{id}/users` with body
`{"email": "...", "role": "..."}`. This **directly creates a member without
sending an email** -- useful for org-internal automation, dangerous for
public-facing flows. v0.29.0 deliberately does NOT expose this path because
its semantics differ from `invite`. If you need it, talk to the maintainers
about a future `member-add-direct` command.
