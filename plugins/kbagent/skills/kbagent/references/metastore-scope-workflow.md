# Metastore Scope Workflow -- Sharing & Organization-Wide Objects (PSGO-140)

Every metastore item (semantic-layer model, dataset, metric, relationship,
constraint, glossary term) carries a visibility **scope**:

- `project` (default) -- visible only to the owning project. Unchanged
  behavior; every command that doesn't mention `--scope` still creates
  project-scoped items exactly as before this feature.
- `targeted` -- owner project + an explicit **target-project grant list**
  (repeatable project alias). Any normal project token can create at this
  scope; no elevated role needed.
- `organization` -- visible to **every** project in the organization.
  Creating directly at this scope, or *elevating* an existing item to it,
  requires the caller to hold the **organization-admin** role -- a normal
  project token gets `ACCESS_DENIED` (403).

For one-line command reference, see
[commands-reference.md](commands-reference.md#scope--target-project-grants--elevation-scope-sub-app-since-vnext-psgo-140).
For the schema-version / replace-vs-merge / 403-vs-404 surprises, see
[gotchas.md](gotchas.md).

**Hard rule for AI agents**: never pass `--scope organization|targeted`, and
never run `scope elevate`/`scope request-elevation`, without the user having
explicitly named which project(s) should gain visibility. Widening an
object's visibility is a security-relevant decision, and organization-scope
elevation has **no downgrade endpoint** -- it cannot be undone via the API.
When in doubt, create/leave the item at the default `project` scope and ask.

## When to use what

| Goal | Command |
|------|---------|
| Check an item's current scope/grants/pending elevation | `semantic-layer scope status --type T --id ID` |
| Create a new item visible to a couple of named projects | `model create` / `add <kind>` with `--scope targeted --target-project ALIAS ...` |
| Add/remove target projects on an existing targeted item | `scope grant --target-project ALIAS ...` / `--remove-target-project ALIAS ...` |
| Replace the whole target-project list in one call | `scope grant --replace --target-project ALIAS ...` |
| Revoke every grant (item becomes owner-only again) | `scope grant --clear` |
| Ask an org-admin to make an item org-wide | `scope request-elevation` (owner-only) |
| Cancel a pending elevation request | `scope withdraw-elevation` (owner-only) |
| Actually make an item org-wide (org-admin token) | `scope elevate --yes` |
| Find items awaiting an elevation decision | `scope pending --type T` (org-admin token) |

---

## Workflow 1 -- Create an item shared with specific projects

```bash
# Ask the user which project(s) should see this metric BEFORE running this.
kbagent --json semantic-layer add metric \
  --project prod --model core_model \
  --name gross_margin --sql "SUM(revenue) - SUM(cogs)" --dataset out.c-fin.fact_pnl \
  --scope targeted --target-project analytics-prod --target-project finance-prod
```

`--target-project` is repeatable and takes project **aliases** (resolved to
numeric Storage project IDs internally). Omitting `--target-project` with
`--scope targeted`:

- On a real terminal: launches an interactive checkbox picker over every
  *other* registered project.
- In `--json` / non-interactive mode: fails fast with `INVALID_ARGUMENT`
  (exit 2) instead of guessing. There is always an explicit target list --
  never a silent default.

## Workflow 2 -- Adjust grants on an existing targeted item

```bash
# See what's granted today
kbagent --json semantic-layer scope status --project prod --type metric --id <uuid>
# -> {"scope": "targeted", "target_project_ids": [1234, 5678], ...}

# Add one more project (merge -- read-modify-write, NOT atomic against a
# concurrent grant change on the same item)
kbagent semantic-layer scope grant --project prod --type metric --id <uuid> \
  --target-project new-team-prod

# Replace the whole set in one round trip (matches the API's native semantics)
kbagent semantic-layer scope grant --project prod --type metric --id <uuid> \
  --replace --target-project analytics-prod

# Revoke every grant -- item becomes owner-only again
kbagent semantic-layer scope grant --project prod --type metric --id <uuid> --clear
```

`scope grant` only works on an item already created with `scope="targeted"`
-- it 400s against a project-scoped or organization-scoped item (targeting a
project-scoped item makes no sense; an organization-scoped item is already
visible everywhere).

## Workflow 3 -- Make an item organization-wide

Two-step, deliberately: the owner requests, an organization-admin decides.

```bash
# 1. Owner project flags the item (idempotent -- re-running just refreshes
#    the timestamp)
kbagent semantic-layer scope request-elevation --project prod --type dataset --id <uuid>

# 2. An organization-admin discovers the queue...
kbagent --json semantic-layer scope pending --project prod --type dataset
# -> [{"id": "<uuid>", "name": "fact_pnl", "scope_elevation_requested_at": "..."}]

# 3. ...and elevates. IRREVERSIBLE -- confirmation prompt unless --yes/--json.
kbagent semantic-layer scope elevate --project prod --type dataset --id <uuid> --yes
```

A non-admin token can also call `scope elevate` directly (skipping step 1) if
the caller already holds the organization-admin role -- `request-elevation`
exists for the common case where the owner and the admin are different
people/tokens. `scope withdraw-elevation` cancels a pending request before
an admin acts on it.

There is **no bulk-elevate endpoint**. "Elevate an existing project's
objects" as a migration means one `request-elevation` + `elevate` call per
item, run deliberately for objects the user has named -- never loop this
over every object in a project speculatively.

## Workflow 4 -- Editing a scoped item

`semantic-layer edit <kind>` is DELETE+POST (the metastore has no PATCH for
data changes). The edit path automatically reads the item's current
scope/target-project grants before deleting and re-applies them on the POST
half -- editing an `organization`/`targeted`-scope item never silently
resets it back to `project` scope. Nothing to do here; this is just so you
don't have to re-grant after every rename.
