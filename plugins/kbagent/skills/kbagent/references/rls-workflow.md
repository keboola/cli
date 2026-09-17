# RLS Workflow -- Row-Level Security Policies

Row-level security in Keboola is authored as metastore `rls-policy` objects
-- one object per protected table, holding a list of per-principal condition
primitives (never a hand-written SQL predicate). `kbagent` only **authors**
these objects; enforcement (the actual SQL rewrite on a query) happens
inside `keboola-mcp-server`'s `query_data` tool, a different repo and a
different runtime. `kbagent rls` never executes a query and never decides
whether a real query gets filtered -- it only creates, reads, updates and
deletes the policy objects the enforcement engine reads.

**Authorship is org-admin-only, structurally.** Every write (`create`,
`update`, `setup`) is issued at `organization` scope (default) or
`targeted` scope (`--target-project`, repeatable) -- there is no `--scope
project` option anywhere in this command group, matching the backend's own
restriction that a project's own admin can never author policy for its own
tables. `--project` on every command still names the project whose tables
you're working with (and, for reads, which metastore instance to query) --
it does not grant that project's admin any extra authority over policy
authorship.

Same metastore requirements as [semantic-layer-workflow.md](semantic-layer-workflow.md):
a MASTER (project admin) Storage token (`kbagent --json project info
--project P` -> `is_master_token`), same `MISSING_MASTER_TOKEN` reclassification
on a non-master token. Pre-flight before anything else in this file:

```bash
kbagent --json project info --project P | jq '.is_master_token'
```

**The metastore backend does not register the `rls-policy` object type on
any deployed stack yet** (a companion `go-monorepo` change, tracked
separately from the kbagent work). Every command below will answer with a
classified `NOT_FOUND` (schema fetch failure) or similar error against a
real project until that lands -- see Workflow 7 for how to tell that apart
from an actual problem. All the examples here are still correct and worth
running once the backend ships; they are also fully covered by kbagent's own
test suite against a mocked metastore in the meantime.

Full per-command flag reference: [commands-reference.md](commands-reference.md#row-level-security-rls).
Non-obvious behaviors and version gates: [gotchas.md](gotchas.md).

## When to use what

| Goal | Command |
|------|---------|
| See what RLS policies already protect a project | `rls list --project P` |
| Inspect one policy's full rule set | `rls detail --project P --policy-id ID` |
| Check the live condition/rules shape before hand-writing JSON | `rls schema --project P` |
| Walk an org admin through authoring a new policy interactively | `rls setup --project P` |
| Script or automate policy creation (CI, batch onboarding) | `rls create --project P --table T --dialect D --rules '[...]'` |
| Change one field on an existing policy without touching the rest | `rls update --project P --policy-id ID --table/--dialect/--rules ...` |
| Share one policy across sibling projects | `rls create`/`rls update ... --target-project ID` |
| Preview the compiled condition before writing anything | any write command + `--dry-run` |
| Remove a policy (un-protect a table) | `rls delete --project P --policy-id ID` |

---

## Workflow 1 -- Guided setup (the default path for a human org admin)

`rls setup` is interactive-terminal-only -- it needs a real TTY for the
checkbox table picker and the condition-builder prompts. It refuses cleanly
under `--json` or a piped/non-TTY stdout, printing a one-line hint instead
of hanging or producing malformed JSON output.

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
#        with an "add another rule?" loop, or
#    (b) skip the builder entirely with a prepared --rules file for
#        anything beyond a single simple comparison per principal:
kbagent rls setup --project prod --rules @rls_rules.json

# 4. It prints a compiled-condition preview per selected table, then a
#    single confirm (skip with --yes) before creating one `rls-policy`
#    object per table -- via the exact same write path `rls create` uses,
#    never a second, parallel one.
```

If `rls setup` is not usable (no TTY -- a CI job, a scheduled agent task, a
piped invocation), it tells you so and points at `rls create` -- go to
Workflow 2.

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

# 2. ALWAYS preview first -- --dry-run runs every validation (dialect,
#    rule shape, live schema check when available) and compiles a
#    human-readable condition preview, but writes nothing.
kbagent --json rls create \
  --project prod \
  --table in.c-crm.invoices \
  --dialect snowflake \
  --rules @rls_rules.json \
  --dry-run
# -> {"table": "in.c-crm.invoices", "dialect": "snowflake",
#     "scope": "organization", "preview": [
#       {"principal": "eu-analyst@example.com", "condition": "region = 'EU'"},
#       {"principal": "us-analyst@example.com, us-lead@example.com",
#        "condition": "region = 'US'"}
#     ], "dry_run": true}

# 3. Then the real write (still --json for a clean, single-document
#    response -- no confirmation prompt in --json mode):
kbagent --json rls create \
  --project prod \
  --table in.c-crm.invoices \
  --dialect snowflake \
  --rules @rls_rules.json
```

A table with no rows matching ANY rule for a given caller means the query
is refused, not silently returned empty -- fail-closed is the enforcement
engine's whole design point (see the RFC in `keboola-mcp-server`). Before
protecting a table, make sure every principal who legitimately needs access
has a matching rule, including yourself if you plan to verify the result
via `query_data`.

## Workflow 3 -- Inspect existing policies before making a change

Always read before you write -- `rls update` merges onto the CURRENT stored
state, so knowing what's there first avoids surprises.

```bash
# 1. What's protected in this project already?
kbagent --json rls list --project prod
# -> {"project": "prod", "policies": [
#      {"id": "...", "table": "in.c-crm.invoices", "dialect": "snowflake",
#       "rule_count": 2, "scope": "organization",
#       "source_project_id": "12345", "target_project_ids": []}
#    ]}

# 2. Full rule set for one policy (needed before a partial `rls update`):
kbagent --json rls detail --project prod --policy-id <id-from-list>
```

`scope`/`source_project_id`/`target_project_ids` tell you how a policy
became visible here: `organization` scope is visible to every project in
the org, but only APPLIES where `source_project_id` matches the querying
project (or that project is in `target_project_ids` for a `targeted`
policy) -- a policy authored for a different project, with the same table
name, never applies here. Don't assume a listed policy protects the current
project just because it's in the listing; check `source_project_id`.

## Workflow 4 -- Condition primitive cookbook

Every `condition` is one of six shapes -- there is no free-text predicate
field anywhere in this schema, by design (closes the injection surface a
hand-written-SQL model would have). `rls schema --project P` returns the
live, authoritative JSON Schema; this is the quick-reference version.

```jsonc
// Comparison: eq | ne | gt | gte | lt | lte
{"column": "status", "op": "eq", "value": "active"}

// Membership: in | not_in
{"column": "region", "op": "in", "values": ["EU", "US"]}

// Nullness: is_null | is_not_null
{"column": "deleted_at", "op": "is_null"}

// Boolean composition (2+ nested conditions each)
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

`rls create --dry-run` (or `rls setup`'s preview step) renders any of these
as a `WHERE`-clause-shaped string for human sanity-checking. **That preview
is kbagent's own renderer, not the enforcement engine** -- the engine that
actually decides what a query returns lives in `keboola-mcp-server`'s
`rls.py` (sqlglot-based). Treat the preview as "does this look like what I
meant," never as proof of what will be enforced.

## Workflow 5 -- Share a policy across sibling projects (targeted scope)

`--target-project` (repeatable, on `create`/`update`/`setup`) shifts scope
from `organization` to `targeted` and registers each listed project as a
grant. Use this when one org-authored policy should apply identically
across specific sibling customer projects, not the whole org.

```bash
kbagent --json rls create \
  --project prod \
  --table in.c-crm.invoices \
  --dialect snowflake \
  --rules @rls_rules.json \
  --target-project 22222 \
  --target-project 33333 \
  --dry-run
# -> {"scope": "targeted", "target_project_ids": ["22222", "33333"], ...}
```

Omitting `--target-project` on a later `rls update` call leaves the current
target list untouched (fetch-then-merge, see Workflow 6) -- pass the FULL
desired list to change it, not just an addition.

## Workflow 6 -- Update or revoke a policy safely

`rls update` is fetch-then-merge: only the flags you pass change, every
omitted flag keeps its current stored value -- a partial update can never
silently blank `table`/`dialect`/`rules`/target projects you didn't mean to
touch.

```bash
# Change only the rules, leave table/dialect/scope exactly as they are:
kbagent --json rls update \
  --project prod \
  --policy-id <id> \
  --rules @updated_rls_rules.json \
  --dry-run   # preview first, same as create

kbagent --json rls update --project prod --policy-id <id> --rules @updated_rls_rules.json

# Revoke entirely (un-protects the table -- confirm this is what you want,
# a project without the feature flag or without an applicable policy is
# simply unfiltered, not an error state on the enforcement side):
kbagent rls delete --project prod --policy-id <id>
# --yes to skip the confirmation prompt in a script; --json also skips it.
```

## Workflow 7 -- Troubleshooting "backend not available" errors

Until the `go-monorepo` companion work registers the `rls-policy` object
type, EVERY command in this group will fail against a real, live project --
this is expected, not a kbagent bug. Tell it apart from a real problem:

```bash
kbagent --json rls schema --project prod
```

- **`NOT_FOUND` / "Could not fetch the rls-policy schema: ..."** -- the
  expected shape while the backend isn't registered yet. The commands are
  fully implemented and unit-tested against a mocked metastore client
  (`tests/test_rls_service.py`); nothing here is broken, the object type
  just doesn't exist on the stack yet.
- **`MISSING_MASTER_TOKEN`** -- an actual, fixable problem: the registered
  token for this project isn't a master token. `kbagent project info
  --project P` -> `is_master_token`, then `project edit --project P
  --token ...` with a master token.
- **`INVALID_RLS_POLICY`** -- your `--rules`/`--dialect`/`--table` failed
  validation (either the schema-independent checks kbagent always runs, or
  the live JSON-Schema check when the backend IS reachable). The error
  message lists every violation found; nothing was written.
- **`CONFIG_ERROR` / exit 5** -- the `--project` alias isn't registered
  locally. `kbagent project list`.

---

## Reference: RLS contract gotchas

- [gotchas.md](gotchas.md) -- search "rls" for the full list, version-tagged.
- One `rls-policy` object per protected table, never one blob per project.
- `organization` scope is visible org-wide but only APPLIES where
  `source_project_id` (or `target_project_ids` for `targeted` scope)
  matches the querying project -- never by table-name text alone.
- `--scope project` does not exist anywhere in this command group. This is
  intentional, not a missing feature -- see the RFC in
  `keboola-mcp-server`'s `feature_spec/rls_query_tool/RFC.md`.
- `rls setup` has no REST route (`kbagent serve`) -- it is genuinely
  terminal-only, same carve-out as `auth register-projects`'s picker.
- kbagent's `--dry-run` preview and the real enforcement engine
  (`keboola-mcp-server`'s `rls.py`) are two separate implementations by
  design -- the preview is a convenience for human review, never the
  source of truth for what a query actually returns.
