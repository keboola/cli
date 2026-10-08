# Metastore Scope Workflow -- Sharing & Organization-Wide Objects (PSGO-140)

Every metastore item (semantic-layer model, dataset, metric, relationship,
constraint, glossary term) carries a visibility **scope**:

- `project` -- visible only to the owning project. A `model create` without
  `--scope` creates a project-scoped model.
- `targeted` -- owner project + an explicit **target-project grant list**
  (repeatable `--target-project`, alias or numeric project ID). Creating at
  this scope needs a **project-admin** token, like every metastore write.
- `organization` -- visible to **every** project in the organization.
  Creating directly at this scope, or *elevating* an existing item to it,
  requires the **organization-admin** role -- a normal project token gets
  `ACCESS_DENIED` (403).

`add <kind>` without `--scope` **inherits its model's scope** (and target
projects), so an org-level model gets org-level children. Pass `--scope` to
override (a project-admin token that is not an org admin must pass
`--scope project` under an org-level model, or it gets a 403).

For one-line command reference, see
[commands-reference.md](commands-reference.md#scope--target-project-grants--elevation-scope-sub-app-psgo-140).
For the schema-version / replace-vs-merge / 403-vs-404 surprises, see
[gotchas.md](gotchas.md).

**Hard rule for AI agents**: never pass `--scope organization|targeted`, and
never run `scope set --scope organization` / `scope request-create`, without
the user having explicitly named which project(s) should gain visibility.
Widening an object's visibility is a security-relevant decision, and
organization-scope elevation has **no downgrade endpoint** -- it cannot be
undone via the API. When in doubt, create/leave the item at `project` scope
and ask.

## When to use what

| Goal | Command |
|------|---------|
| Check an item's current scope/grants/pending elevation | `semantic-layer scope get --type T --context-id ID` |
| Create a new item visible to a couple of named projects | `model create` / `add <kind>` with `--scope targeted --target-project ALIAS\|ID ...` |
| Add / remove target projects on an existing targeted item | `scope add --target-project X` / `scope remove --target-project X` (from the owning project) |
| Replace the whole target-project list in one call | `scope set --target-project X --target-project Y` |
| Revoke every grant (item becomes owner-only again) | `scope set --clear` |
| Ask an org-admin to make an item org-wide | `scope request-create` (owner-only) |
| Cancel a pending elevation request | `scope request-delete` (owner-only) |
| Actually make an item org-wide (org-admin token) | `scope set --scope organization --yes` (preview with `--dry-run`) |
| Find items awaiting an elevation decision | `scope request-list --type T` (org-admin token) |

---

## Workflow 1 -- Create an item shared with specific projects

```bash
# Ask the user which project(s) should see this metric BEFORE running this.
kbagent --json semantic-layer add metric \
  --project prod --model core_model \
  --name gross_margin --sql "SUM(revenue) - SUM(cogs)" --dataset out.c-fin.fact_pnl \
  --scope targeted --target-project analytics-prod,5678
```

`--target-project` is repeatable or comma-separated and takes a registered
project **alias** or a numeric project **ID** (so the target need not be
registered). An alias must be on the owner project's stack. Omitting
`--target-project` with `--scope targeted`:

- On a real terminal: launches an interactive checkbox picker over every
  *other* registered project on the same stack.
- In `--json` / non-interactive mode: fails fast with `INVALID_ARGUMENT`
  (exit 2) instead of guessing. There is always an explicit target list --
  never a silent default.

`--target-project` without `--scope targeted`, or an unknown alias, also
exits 2.

## Workflow 2 -- Adjust grants on an existing targeted item

```bash
# See what's granted today
kbagent --json semantic-layer scope get --project prod --type metric --context-id <uuid>
# -> {"scope": "targeted", "target_project_ids": [1234, 5678], ...}

# Attach / detach one project (client-side merge -- NOT atomic against a
# concurrent grant change; only from the OWNING project)
kbagent semantic-layer scope add    --project prod --type metric --context-id <uuid> \
  --target-project new-team-prod
kbagent semantic-layer scope remove --project prod --type metric --context-id <uuid> \
  --target-project 5678

# Replace the whole set in one round trip (the API's native semantics)
kbagent semantic-layer scope set --project prod --type metric --context-id <uuid> \
  --target-project analytics-prod,5678

# Revoke every grant -- item becomes owner-only again
kbagent semantic-layer scope set --project prod --type metric --context-id <uuid> --clear
```

These only work on an item already created with `scope="targeted"` -- they
400 against a project- or organization-scoped item. `scope add|remove` from a
project that does not own the item (an org admin elsewhere) are refused
(exit 2): the server hides the current grants from a non-owner, so a merge
would overwrite them. Use `scope set --target-project` in that case.

## Workflow 3 -- Make an item organization-wide

Two-step, deliberately: the owner requests, an organization-admin decides.

```bash
# 1. Owner project flags the item (idempotent -- re-running just refreshes
#    the timestamp)
kbagent semantic-layer scope request-create --project prod --type dataset --context-id <uuid>

# 2. An organization-admin discovers the queue...
kbagent --json semantic-layer scope request-list --project prod --type dataset --limit 50
# -> {"items": [{"id": "<uuid>", "name": "fact_pnl", ...}], "limit": 50, "offset": 0, "has_more": false}

# 3. ...previews, then elevates. IRREVERSIBLE -- confirmation prompt unless --yes/--json.
kbagent semantic-layer scope set --project prod --type dataset --context-id <uuid> \
  --scope organization --dry-run
kbagent semantic-layer scope set --project prod --type dataset --context-id <uuid> \
  --scope organization --yes
```

A caller who already holds the organization-admin role can run step 3
directly (skipping step 1) -- `request-create` exists for the common case
where the owner and the admin are different people/tokens.
`scope request-delete` cancels a pending request before an admin acts on it.

`--scope organization` is gated as **destructive** by the permission engine
(`--deny-destructive` blocks it, here and on `model create` / `add <kind>`);
`--dry-run` is gated too (same as `sync push --force`). An `add <kind>` that
would INHERIT `organization` from its model is gated the same way.

There is **no bulk-elevate endpoint**. "Elevate an existing project's
objects" as a migration means one `request-create` + `set --scope
organization` call per item, run deliberately for objects the user has named
-- never loop this over every object in a project speculatively. Items
created by a kbagent older than this feature may refuse elevation (they were
pinned to schema `1.0.0`, which only supports `project`); if so, re-create
them.

## Workflow 4 -- Editing a scoped item

`semantic-layer edit <kind>`, `import --overwrite` and `promote` update the
item **in place** (PUT): it keeps its id, scope, target projects and any
pending elevation request, and a failed update changes nothing. Nothing to do
here; this is just so you don't have to re-grant after every rename.

## Over `kbagent serve`

`GET|PUT /semantic-layer/scope/{context_id}`, `POST|DELETE
.../target-projects`, `PUT|DELETE .../elevation-request` and `GET
/semantic-layer/scope/elevation-requests` mirror the commands above;
`POST /semantic-layer/models` and `/items/{kind}` accept `scope` +
`target_projects`. Like the other semantic-layer routes they do not consult the
`permissions` policy -- the serve bearer token is the gate.
