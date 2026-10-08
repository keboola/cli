# RLS / CLS Workflow -- Row- and Column-Level Security Policies

Row-level security in Keboola is authored as metastore `rls-policy` objects
-- one object per protected table, holding a list of per-principal condition
primitives (never a hand-written SQL predicate). `kbagent` only **authors**
these objects; enforcement (the actual SQL rewrite on a query) happens
inside `keboola-mcp-server`'s `query_data` tool, a different repo and a
different runtime. `kbagent rls` never executes a query and never decides
whether a real query gets filtered -- it only creates, reads, updates and
deletes the policy objects the enforcement engine reads.

**Scope: `targeted` by default, `organization` only when asked for, never `project`.**

- `targeted` (the default, and the metastore schema's own default) governs the
  table in the owning project (`--project`) plus every project granted with
  `--target-project` (alias or project ID, repeatable or comma-separated).
- `--scope organization` governs the table in **every project of the
  organization** that has the `row-level-security` project feature. The
  enforcement applies every policy the metastore lists for a project -- it does
  not re-check the owning project -- so an organization policy is never "only
  for my project". It is gated as `destructive` (`rls.create --scope
  organization` in `FLAG_ESCALATIONS`), so `--deny-destructive` blocks it.
- There is no `--scope project`: the schema does not support it for policies.

**Who may write** (the metastore schema ACL, not kbagent):

| Operation | Project admin (master token, admin role) | Organization admin |
|---|---|---|
| `create` / `update` / `delete` a `targeted` policy of its own project, no grants | yes | yes |
| `--target-project` (grants) on `create` or `update`, `--clear-target-projects` | no (403) | yes |
| `--scope organization` | no (403) | yes |
| Read (`list` / `detail`) | only the policies its project owns | everything visible to the project |

**Enforcement prerequisite.** Policies do nothing until the consuming project has
the `row-level-security` project feature (it gates CLS too). Without it, queries
are unfiltered -- not an error.

**How rules combine (policy schema 1.1.0, the metastore default).** A rule
selects identities by `principal`, `principals` or IdP `groups`. Every rule
that matches the reader applies: RLS conditions combine with OR, CLS
`visible_columns` are united. An RLS policy may set a `default` condition for
readers no rule matches; without one, their reads are refused. 1.1.0 is a
superset of 1.0.0 -- every older policy keeps its meaning.

**Dialect.** A policy whose `dialect` differs from the reading workspace makes
the enforcement refuse every read of that table. `--dialect` defaults to the
project backend, and kbagent refuses any other value (`INVALID_RLS_POLICY`).

An `organization` policy is loaded by every project in the organization,
whatever their backend -- in an organization with both Snowflake and BigQuery
projects, an organization policy blocks the queries of every project on the
other backend. Prefer `targeted` there.

Same metastore requirements as [semantic-layer-workflow.md](semantic-layer-workflow.md):
a MASTER (project admin) Storage token (`kbagent --json project info
--project P` -> `is_master_token`), same `MISSING_MASTER_TOKEN` reclassification
on a non-master token. A master token is NOT an organization admin: grants and
organization scope also need the organization-admin role, which the metastore
checks (403 otherwise).

**Backend availability.** The `rls-policy` and `cls-policy` object types are registered by
the metastore's `2026-09-29` schema migrations. A stack whose metastore predates the `rls-policy`/`cls-policy` schema migrations answers `rls schema`/`cls schema` with a classified `NOT_FOUND` (schema-fetch failure) -- expected there, not a kbagent bug. See Workflow 7.

Full per-command flag reference: [commands-reference.md](commands-reference.md#row-level-security-rls).
Non-obvious behaviors and version gates: [gotchas.md](gotchas.md).

## When to use what

| Goal | Command |
|------|---------|
| See what RLS policies already protect a project | `rls list --project P` |
| Inspect one policy's full rule set | `rls detail --project P --policy-id ID` |
| Check the live condition/rules shape before hand-writing JSON | `rls schema --project P` |
| Walk an admin through authoring a new policy interactively | `rls setup --project P` |
| Script or automate policy creation (CI, batch onboarding) | `rls create --project P --table-id T --rules '[...]'` |
| Change one field on an existing policy without touching the rest | `rls update --project P --policy-id ID --table-id/--dialect/--rules ...` |
| Share one policy with sibling projects (organization admin) | `rls create`/`rls update ... --target-project ALIAS_OR_ID` |
| Govern the table in every project of the organization (organization admin) | `rls create ... --scope organization` |
| Preview the compiled condition before writing anything | any write command + `--dry-run` |
| Remove a policy (un-protect a table) | `rls delete --project P --policy-id ID` (`--dry-run` shows it first) |

---

## Workflow 1 -- Guided setup (the default path for a human admin)

`rls setup` is interactive-terminal-only -- it needs a real TTY for the
checkbox table picker and the condition-builder prompts. Under `--json` or
without an interactive terminal (stdin or stdout piped) it exits 2 with an
`INVALID_ARGUMENT` error (a JSON envelope under `--json`) pointing at `rls create`.

```bash
# 1. Run it directly in an interactive terminal -- NOT through kbagent http,
#    NOT with --json, NOT from a scheduled agent task.
kbagent rls setup --project prod

# 2. It shows a checkbox picker over `storage tables --project prod`'s
#    result: arrows/j-k to move, space to toggle, 'a' to select all,
#    enter to confirm, 'q'/esc/ctrl-c to cancel.

# 3. For the rules, either:
#    (a) build interactively -- it prompts for principal(s), then a small
#        menu: comparison / IN-NOT_IN / IS NULL-IS NOT NULL / always-true,
#        with an "add another rule?" loop. A typed value is read as JSON when
#        it is one (42, 4.5, true); quote it ("42") to keep a string. Or
#    (b) skip the builder entirely with a prepared --rules file for
#        anything beyond a single simple comparison per principal:
kbagent rls setup --project prod --rules @rls_rules.json

# 4. It prints a compiled-condition preview per selected table, then a
#    single confirm (skip with --yes) before creating one `rls-policy`
#    object per table -- via the exact same write path `rls create` uses.
#    The dialect is the project backend unless --dialect says otherwise.
```

## Workflow 2 -- Scripted / non-interactive policy creation

The path for CI, batch onboarding, or any AI-agent-driven invocation.
`--rules` always takes the same `JSON|@file|-` form the rest of kbagent uses
for structured input (see e.g. `config update --configuration`).

```bash
# 1. Write the rules array to a file (or build it inline for something
#    this short) -- ALWAYS a declarative condition, never a predicate string.
cat > rls_rules.json <<'EOF'
[
  {
    "principal": "eu-analyst@example.com",
    "condition": {"column": "region", "op": "eq", "value": "EU"}
  },
  {
    "principals": ["us-analyst@example.com", "us-lead@example.com"],
    "condition": {"column": "region", "op": "eq", "value": "US"}
  }
]
EOF

# 2. ALWAYS preview first -- --dry-run runs every validation (dialect vs
#    backend, rule and default shape, live schema check when
#    available) and compiles a condition preview, but writes nothing.
kbagent --json rls create \
  --project prod \
  --table-id in.c-crm.invoices \
  --rules @rls_rules.json \
  --dry-run
# -> {"table": "in.c-crm.invoices", "dialect": "snowflake",
#     "scope": "targeted", "target_project_ids": [], "preview": [
#       {"principal": "eu-analyst@example.com", "condition": "\"region\" = 'EU'"},
#       {"principals": ["us-analyst@example.com", "us-lead@example.com"],
#        "condition": "\"region\" = 'US'"}
#     ], "dry_run": true}

# 3. Then the real write (still --json for a clean, single-document
#    response -- no confirmation prompt in --json mode):
kbagent --json rls create --project prod --table-id in.c-crm.invoices --rules @rls_rules.json
```

A caller with no rule on a governed table is refused, not silently returned
empty -- fail-closed is the enforcement engine's whole design point (see the
RFC in `keboola-mcp-server`). Before protecting a table, make sure every
principal who legitimately needs access has a matching rule, including
yourself if you plan to verify the result via `query_data`. A principal is the
user's email, matched case-insensitively.

## Workflow 3 -- Inspect existing policies before making a change

```bash
# 1. What's visible to this project?
kbagent --json rls list --project prod
# -> {"project": "prod", "policies": [
#      {"id": "...", "table": "in.c-crm.invoices", "dialect": "snowflake",
#       "rule_count": 2, "scope": "targeted", "owner_project_id": 12345,
#       "source_project_id": null, "target_project_ids": [22222]}
#    ]}

# 2. Full rule set for one policy (needed before a partial `rls update`):
kbagent --json rls detail --project prod --policy-id <id-from-list>
```

Every policy in the listing **applies** to the project: the metastore lists
what the project owns, what is granted to it (`targeted`) and every
`organization` policy, and the enforcement applies all of them.
`owner_project_id` is the project that owns (and alone may update or delete)
a `targeted` policy; it is `null` for an `organization` policy.
`target_project_ids` is shown to the owning project only.

## Workflow 4 -- Condition primitive cookbook

Every `condition` is one of six shapes -- there is no free-text predicate
field anywhere in this schema, by design (closes the injection surface a
hand-written-SQL model would have). `rls schema --project P` returns the
live, authoritative JSON Schema; this is the quick-reference version.

```jsonc
// Comparison: eq | ne | gt | gte | lt | lte   (value: string, number or boolean)
{"column": "status", "op": "eq", "value": "active"}

// Membership: in | not_in
{"column": "region", "op": "in", "values": ["EU", "US"]}

// Nullness: is_null | is_not_null  -- the ONLY way to match null
{"column": "deleted_at", "op": "is_null"}

// Boolean composition (2+ nested conditions each; there is no "not")
{"and": [
  {"column": "region", "op": "eq", "value": "EU"},
  {"column": "status", "op": "ne", "value": "draft"}
]}
{"or": [
  {"column": "region", "op": "eq", "value": "EU"},
  {"column": "region", "op": "eq", "value": "US"}
]}

// Always applies (no filtering for this principal -- use sparingly, this
// is the escape hatch for "this admin sees everything")
{"true": true}
```

- `"value": null` (or `null` inside `values`) is refused: the enforcement would
  render `col = NULL`, which matches no row. Use `is_null` / `is_not_null`.
- Column names are matched **case-exactly**: the enforcement quotes them
  (`"region"` on Snowflake, `` `region` `` on BigQuery), and so does the preview.

`rls create --dry-run` (or `rls setup`'s preview step) renders a condition
as a `WHERE`-clause-shaped string. It follows the enforcement's rendering
(quoted columns, `TRUE`/`FALSE`), but it is kbagent's own renderer -- the
engine that decides what a query returns lives in `keboola-mcp-server`'s
`rls.py` (sqlglot-based). Treat the preview as "does this look like what I
meant," not as proof of what will be enforced.

## Workflow 4b -- Groups, a default and identity placeholders (schema 1.1.0)

One policy for many readers instead of one rule per person. Groups are the IdP
`groups` claim strings exactly as delivered (Entra may deliver object ids);
Keboola never manages membership.

```bash
cat > orders_rules.json <<'EOF'
[
  {"groups": ["sales-eu"],
   "condition": {"column": "region", "op": "in", "values": ["EU"]}},
  {"groups": ["sales-reps"],
   "condition": {"column": "owner_email", "op": "eq", "value": {"$identity": "email"}}},
  {"principals": ["auditor@example.com"],
   "condition": {"true": true}}
]
EOF
kbagent --json rls create --project prod --table-id in.c-sales.orders \
  --rules @orders_rules.json --default '{"false": true}' --dry-run
```

A reader in `sales-eu` and `sales-reps` sees
`"region" IN ('EU') OR "owner_email" = <their email>`; the auditor sees every
row; anyone else sees no rows (the `default`), instead of an error.

- `{"$identity": "email"}` (as `value`) and `{"$identity": "groups"}` (as
  `values`) are resolved by the enforcement per reader, as bound literals.
- `{"false": true}` matches no row -- useful as a `default` or a rule.
- A default cannot be removed by `rls update` (the metastore's partial update
  cannot delete a key); recreate the policy instead.
- MCP OAuth users have an email but no IdP groups, so `groups` rules only
  match readers whose identity carries a groups claim.

## Workflow 5 -- Share a policy with sibling projects (targeted grants)

`--target-project` (on `create`/`setup`/`update`) takes a registered alias or a
project ID, repeatable or comma-separated, de-duplicated. On `create` the grants
travel in the create request itself (one transaction). On `update` the list
REPLACES the grants of a `targeted` policy, and `--clear-target-projects` revokes
them all. Granting needs the organization-admin role; on `update` the grants
change before the rules, so a refused grant writes nothing.

An `organization` policy has no grants and cannot be narrowed -- delete it and
create a `targeted` one instead.

```bash
kbagent --json rls create \
  --project prod \
  --table-id in.c-crm.invoices \
  --rules @rls_rules.json \
  --target-project customer-a,33333 \
  --dry-run
# -> {"scope": "targeted", "target_project_ids": [22222, 33333], ...}
```

Omitting `--target-project` on a later `rls update` leaves the grants
untouched -- pass the FULL desired list to change them, not just an addition.

## Workflow 6 -- Update or revoke a policy safely

`rls update` changes only the flags you pass. It reads the policy, validates the
MERGED result (the metastore's partial update validates only the keys it
receives), then sends a partial update (`PATCH`) with just the changed keys --
keys kbagent does not know and concurrent changes to other keys survive. The
result is read back after the write.

```bash
# Change only the rules, leave table/dialect/scope/grants exactly as they are:
kbagent --json rls update \
  --project prod \
  --policy-id <id> \
  --rules @updated_rls_rules.json \
  --dry-run   # preview first, same as create

kbagent --json rls update --project prod --policy-id <id> --rules @updated_rls_rules.json

# Revoke entirely (un-protects the table). Show it first, then delete:
kbagent --json rls delete --project prod --policy-id <id> --dry-run
kbagent rls delete --project prod --policy-id <id>
# --yes to skip the confirmation prompt in a script; --json also skips it.
# `delete` is destructive-class: `--deny-destructive` blocks it.
```

## Workflow 7 -- Troubleshooting errors

On a stack whose metastore predates the `rls-policy`/`cls-policy` schemas,
EVERY command in this group fails with a classified `NOT_FOUND` -- expected,
not a kbagent bug. Tell it apart from a real problem:

```bash
kbagent --json rls schema --project prod
```

- **`NOT_FOUND` / "Could not fetch the rls-policy schema: ..."** -- the
  object type isn't registered on this stack's metastore (same for
  `cls schema`). Nothing here is broken; use a stack with a newer metastore.
  `detail`/`update`/`delete` also answer `NOT_FOUND` for a policy this token
  may not read (owned by another project).
- **`MISSING_MASTER_TOKEN`** -- the registered token for this project isn't a
  master token. `kbagent project info --project P` -> `is_master_token`, then
  `project edit --project P --token ...` with a master token.
- **`ACCESS_DENIED` (403)** on `--target-project`, `--clear-target-projects` or
  `--scope organization` -- the token's user is not an organization admin.
- **`INVALID_RLS_POLICY`** -- the policy failed validation: a dialect other than
  the project backend, a malformed rule or default, a null comparison, or the
  live JSON-Schema check. The message lists every
  violation; nothing was written.
- **`ALREADY_EXISTS`** -- the project already has a policy for this table
  (policies are named by table). `rls update` or `rls delete` it.
- **`INVALID_ARGUMENT` / exit 2** -- a bad option value: an unknown
  `--target-project`, `--target-project` with `--scope organization`, or an
  update with nothing to change.
- **`CONFIG_ERROR` / exit 5** -- the `--project` alias isn't registered
  locally. `kbagent project list`.

---

## Reference: RLS contract gotchas

- [gotchas.md](gotchas.md) -- search "rls" for the full list, version-tagged.
- One `rls-policy` object per protected table, never one blob per project.
- Every policy a project can list applies to it -- an `organization` policy
  governs the table in every project of the organization.
- `--scope project` does not exist anywhere in this command group. This is
  intentional, not a missing feature -- see the RFC in
  `keboola-mcp-server`'s `feature_spec/rls_query_tool/RFC.md`.
- `rls setup` has no REST route (`kbagent serve`) -- it is genuinely
  terminal-only, same carve-out as `auth register-projects`'s picker.

---

## Column-Level Security (CLS)

`kbagent cls` authors `cls-policy` objects: one per protected table, each rule naming a
principal and the `visible_columns` that principal may read (an allowlist -- unlisted
columns are omitted from the result; masking is not supported). It mirrors `rls`
(`list`, `detail`, `schema`, `create`, `update`, `delete`) with the same scope rule and
default, the same write permissions, dialect check, duplicate-principal check,
`--dry-run`/`--yes` flags and partial `update`; there is no `cls setup` wizard.
Enforcement and RLS+CLS composition happen in `keboola-mcp-server`'s `query_data`,
gated by the same `row-level-security` feature.

```bash
kbagent --json cls schema --project prod            # live shape
kbagent --json cls list --project prod              # expect [] on a fresh project

# Dry-run first: prints each principal's projection, writes nothing.
kbagent cls create --project prod --table-id in.c-crm.customers \
  --rules '[{"principal":"analyst@example.com","visible_columns":["id","region","amount"]},
            {"principals":["a@example.com","b@example.com"],"visible_columns":["id"]}]' --dry-run

kbagent cls create --project prod --table-id in.c-crm.customers --rules @cls_rules.json --yes
kbagent --json cls update --project prod --policy-id <id> --rules @cls_rules_v2.json
kbagent cls delete --project prod --policy-id <id> --yes
```

- A table can carry both an `rls-policy` and a `cls-policy`; author them separately.
- `INVALID_CLS_POLICY` -- the policy failed validation (exactly one of `principal`/`principals`/
  `groups` per rule, non-empty `visible_columns` of `[A-Za-z0-9_]+` names, the dialect check,
  no `default`, plus the live JSON Schema). Nothing was written.
- An identity several CLS rules match sees the union of their `visible_columns`.
