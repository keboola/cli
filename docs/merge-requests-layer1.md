# Merge requests — Layer 1 (commands), RFC

Linear: [DMD-1900](https://linear.app/keboola/issue/DMD-1900). The `kbagent merge-request`
command group over `MergeRequestService` (Layer 2, DMD-1899). Backend facts with citations live
in [`merge-requests-notes.md`](merge-requests-notes.md) — **it is the authority on wire shapes;
this document never restates a field list**. The service contract is
[`merge-requests-layer2.md`](merge-requests-layer2.md), the HTTP client
[`merge-requests-layer3.md`](merge-requests-layer3.md). Scope stays **non-SOX**.

Layer 2 settles most of the surface: twelve service methods map to eleven commands with no
invention. What this RFC decides is what Layer 2 left to the caller — target resolution,
rendering, error presentation, and above all **what may happen without a human saying so**.

## Shape

- Group **`merge-request`**, mounted in `cli.py` under the `_DEV` help panel immediately after
  `branch`; hidden alias **`mr`** (precedent: `sl` for `semantic-layer`, `cli.py:155`; hidden
  subtrees are skipped by `check_command_sync.py:84`, so the alias trips no doc gate).
- Two modules: `commands/merge_request.py` (Typer commands) and
  `commands/_merge_request_render.py` (Rich renderers). The reason is **not** a budget wall —
  `output.py` is at 679 code lines against a 1000 soft ceiling (`make loc-report`; budgets are
  code lines, not raw lines, `CONTRIBUTING.md:185`) and has ample room. The reason is
  qualitative: four non-trivial renderers (list table, detail panel, conflicts table, three-way
  diff) are a coherent unit that no other group's output shares, and eleven commands at this
  repo's real ~75-code-lines-per-command average land near the 800 soft ceiling on their own.
  Precedent for a renderer-only private sibling: `_auth_picker.py`.
- Group callback `check_cli_permission(ctx, "merge-request")`.
- Service registered in `cli.py` as `ctx.obj["merge_request_service"]`.

## Command surface

Eleven commands over twelve service methods. `--project` resolves via `resolve_project_alias`
and is **single-project throughout** — every service method takes one `alias: str` and there is
no multi-project entry point, so this group never fans out the way `branch list` or `job list`
do.

| Command | Service method |
|---|---|
| `list [--state V]` | `list_merge_requests` |
| `detail [--activity-log]` | `get_merge_request` |
| `create --title T [--description D] [--reviewer-id ID ...] [--auto-merge-strategy S] [--auto-merge-at TS] [--external-id X]` | `create_merge_request` |
| `update [--title] [--description] [--reviewer-id ...] [--auto-merge-strategy] [--auto-merge-at] [--external-id]` | `update_merge_request` |
| `request-review` | `request_review` |
| `approve` | `approve` |
| `request-changes [--reason TEXT]` | `request_changes` |
| `merge` | `merge` |
| `conflicts` | `list_conflicts` |
| `diff --component-id C --config-id I [--format short\|full] [--output PATH]` | `get_config_diff` |
| `resolve --component-id C --config-id I (--take ours\|theirs\|delete \| --resolved JSON\|@file\|-) [--change-description TEXT]` | `resolve_conflict` |

Every command above additionally accepts `--project A`, and every one except `list` and
`create` accepts the target pair `[--merge-request-id N | --id N] [--branch B]` (see *Target
resolution*). `create` takes `--branch` only — its target is the source branch.

`find_merge_request_for_branch` gets no command of its own: it is the resolver behind an
omitted `--merge-request-id`, and what it resolved is reported in every command's output.

### Flag naming

The repo's convention, measured across `commands/*.py`: a **bare noun** is the context you work
*in* — `--project` (206 uses), `--branch` (94), `--model` (23) — while **`--<noun>-id`** is the
object you act *on*: `--component-id` (40), `--config-id` (30), `--table-id` (17), `--app-id`
(15), and ~16 more. A merge request is an object, not a context, so the flag is
**`--merge-request-id`**, with **`--id`** as a short alias on the same parameter (precedent:
the `agent` group accepts `--id` beside `--task-id`).

### Flag details that are not free choices

- **`--reviewer-id` must never be declared as `typer.Option([], ...)`.** On this stack
  (typer 0.26.7 / click 8.4.1) `list[int] | None = typer.Option(None, ...)` yields `None` when
  omitted, which is correct — but the `typer.Option([], ...)` style, live in this repo at
  `commands/agent.py:862`, yields `[]`, and `_optional_mr_fields` sends `reviewerIds` whenever
  it is not `None` (`client/merge_requests.py:113-114`). An empty list therefore **replaces the
  reviewer set with nothing** on any `update` that never mentioned reviewers.
- Passing `--reviewer-id` at all **replaces** the set; it never appends. There is consequently
  no way to clear reviewers from the CLI (the wire accepts `[]`, the flag cannot express it).
  Deliberately not solved in v1 — a `--no-reviewers` sentinel is the shape if it is ever wanted.
- **`update` semantics:** `None` = leave unchanged; an **empty string clears** `description` /
  `externalId`. `PUT {}` is a server-side no-op, so `update` with no field flags must be
  refused at exit 2 rather than reporting success having changed nothing.
- **`--auto-merge-at` is required by the backend only with `--auto-merge-strategy scheduled`**
  and meaningless otherwise; validate the pairing in Layer 1. Keep it a `str` — a `datetime`
  annotation makes Typer coerce and reformat the caller's value.
- `--external-id` is capped at 255 characters server-side.
- **`--state` and `--take` pre-validate in Layer 1 to exit 2** (`INVALID_ARGUMENT`), importing
  the now-public `STATE_FILTER_VOCABULARY` and `TAKE_MODES` from the service (precedent:
  `commands/notification.py:33` importing `VALID_CHANNELS`). Without the pre-check a typo
  reaches the service and exits 5 (`CONFIG_ERROR`), where every comparable bad-enum flag in the
  repo exits 2. Never copy the vocabularies into a local `Enum` — that is the drift convention
  #17 exists to prevent.
- **`resolve --take delete --change-description "…"` is refused at exit 2.**
  `rebase_config_delete` has no `change_description` parameter (`client/configs.py:766-772`;
  the wire body is exactly `{"version": N, "diff": {}}`), so the text would be silently
  dropped. The same collapse happens when `--take ours|theirs` picks a side whose `isDeleted`
  is true, so the check is on the *resolved* mode, not on the flag alone.

### `request-review` and `approve` on a default project

Both ship because the state machine has them, but on a non-SOX project with the default **0
required approvals** neither has a happy path: `request_review` is auto-finished by the backend
(`skip_review`), so the MR jumps straight to `approved` and `in_review` is never reached — and
`approve`, whose only `from` place is `in_review`, answers **422 in every state**. `merge` works
directly from `development` anyway. Their `--help` states this plainly rather than letting a
user discover it as an unexplained 422.

There is also **no `close` command**, for a related reason: creator-request-changes is the UI's
cancel, but it leaves `state=development`, and the `closed` derivation depends on
`reviewers[].status`, which a 0-approval project never populates. A `close` command would look
like a no-op. Documented in `request-changes --help` and `gotchas.md` instead.

## Target resolution

`--merge-request-id` is **optional**, resolved in three steps:

1. `--merge-request-id` / `--id` given → use it.
2. Otherwise `resolve_branch()` — the idiom of 12 command modules: explicit `--branch`, else
   `active_branch_id` from `config.json`.
3. On that branch, `find_merge_request_for_branch()` → the MR id.

With neither, the house error: *pass `--branch` or run `branch use`*. The chain cannot be
ambiguous — a branch has **at most one MR, ever**, so step 3 finds one or none. Nothing is
cached: `active_branch_id` is persisted because it is a *user decision*, while branch→MR is
*derivable server state* and caching it would be a cache with no invalidation. Persist
decisions, derive facts.

Every implicit resolution is reported, on stderr in human mode and in the payload always:

```
Info: Using active branch (ID: 123) for project 'acme'
Info: Resolved merge request #7 from branch 123
```

`--json` results carry `merge_request_id`, `branch_from_id` and `resolved_from_branch`
regardless of how the target was reached, so a machine caller can always assert on what was
actually operated upon.

### The one exception, and the rule behind it

Surveying every destructive command in the repo produces a rule that holds without exception:

> **Implicit branch resolution selects the *scope*. The *target* is always named on the command
> line.**

`storage delete-table --table-id X` takes the active branch (`commands/storage.py:1017`) but
still requires the table. `config delete` requires `--config-id`. `branch delete` requires
`--branch` — there the branch *is* the target, which is why that one command has no fallback
even though 12 modules use `resolve_branch`. The only branch command with an active-branch
fallback is `branch merge`, which merges nothing (it prints a URL).

Layered on that, the repo's destructive commands take one of two shapes: **prompt**
(12 sites of `if not yes and not formatter.json_mode` + `typer.confirm`) or **mandatory target**
(`branch delete`). Not one of them relies on the prompt for machine safety — `--json` implies
consent everywhere, in all 48 commands carrying `--yes`.

A bare `kbagent --json merge-request merge --project acme` would satisfy **neither** shape: it
does not prompt and it names nothing. It would be the first command in kbagent where nothing on
the command line identifies what gets destroyed. Hence:

> **When an invocation resolves to the `destructive` class, `--json` requires an explicit
> target — `--merge-request-id` or `--branch`.**

One rule, mechanically checkable, covering `merge` and every auto-merge-escalated path below.
A human at a terminal keeps the full fallback and gets the prompt instead; a script, which
received the id in the JSON payload of its previous call, names it. The ergonomic objection to
ids is an objection about humans, and humans never pass one.

`--yes` keeps its house meaning everywhere (skip the prompt; `--json` implies it). Inverting it
for one command was considered and **rejected**: it has zero precedent across 48 commands, and
a familiar flag with reversed semantics is worse than a rule that names the target.

## Auto-merge is a destructive act

`autoMergeStrategy` is not metadata. A background scheduler
(`AutoMergeScheduleProvider.php:24-26`, every `AUTO_MERGE_INTERVAL`) selects every `approved` MR
whose strategy is `immediately` (or `scheduled` and due) and runs it through the **same
`MergeProcessor`** the merge endpoint uses, under a system token
(`AutoMergeCandidateRepository.php:44-47`, `AutoMergeTickHandler.php:86`). It is polling, not a
hook on approve: the approve path has no auto-merge trigger, and `applyAutoMerge`
(`MergeRequestService.php:226-236`) only persists the strategy. A blocked tick **retries every
tick indefinitely** — an auto-merge cannot be left to fail; it stops only when the strategy is
set back to `none`.

Two consequences kbagent must honour:

- **Order does not matter.** The strategy can be armed before the MR is approved; it fires when
  the state arrives. So `create --auto-merge-strategy immediately` followed by `request-review`
  ends in a production merge with `merge` never called.
- **Nothing kbagent returns reports it.** The arming call answers 200 and the merge happens
  later, invisibly: no job handle to await.

Classifying only `merge` as destructive would therefore be theatre. The rules:

| Operation | Class | Confirmation |
|---|---|---|
| `merge` | destructive | prompt (human); explicit target (`--json`) |
| `create` / `update` with `--auto-merge-strategy` ≠ `none` | destructive via `FLAG_ESCALATIONS` | prompt worded as *arming an automatic production merge*; explicit target (`--json`) |
| `request-review` / `approve` / `resolve` on an **armed** MR | destructive via escalation | **none** — consent was given when it was armed |
| the same on an unarmed MR | write | none |

**Confirmation belongs where a human chooses an irreversible outcome; escalation belongs
wherever one is caused.** No redundancy, no gap.

```python
FLAG_ESCALATIONS = {
    "auth.logout --remove-projects":                    "admin",
    "merge-request.create --auto-merge-strategy":       "destructive",
    "merge-request.update --auto-merge-strategy":       "destructive",
    "merge-request.request-review --auto-merge-armed":  "destructive",
    "merge-request.approve --auto-merge-armed":         "destructive",
    "merge-request.resolve --auto-merge-armed":         "destructive",
}
```

The mechanism is generic — `permissions.py:557` resolves the category as
`FLAG_ESCALATIONS.get(operation) or OPERATION_REGISTRY.get(operation, "write")`, so a
state-derived operation string works exactly like a flag-derived one, and `permissions list`
displays all of them (`list_operations`, `:620`). The dict's name no longer describes its
contents; either rename it or document that a key is an operation string, not necessarily a
flag.

Three details that are easy to get wrong:

- **`--auto-merge-strategy none` must NOT escalate.** It is the disarm. Escalating it would let
  `--deny-destructive` lock a dangerous setting in place — the safety flag inverting into a
  hazard. Guard on the value, not on the flag's presence.
- **Detecting "armed" is usually free.** When the target was resolved implicitly,
  `find_merge_request_for_branch` already returned the enriched row, which carries
  `autoMergeStrategy`. Only an explicit `--merge-request-id` costs one GET.
- **The escalation is deliberately conservative.** On a project requiring 2 approvals,
  `request-review` lands in `in_review` and merges nothing — but distinguishing that needs the
  required-approvals count, which is **unreadable with a Storage token** (project metadata on
  the Manage API; [DMD-1969](https://linear.app/keboola/issue/DMD-1969)). So any operation on an
  armed MR escalates, even where it would not yet merge. Fail-closed by choice, not by neglect.

`request-review` / `approve` / `resolve` on an armed MR take no extra confirmation but **must
say so in their output**:

```
Success: Review requested for merge request #7.
Warning: Auto-merge is armed (immediately) -- this MR is now approved and will be
         merged into production by the backend shortly.
```

## Human rendering

Wire shapes come from `merge-requests-notes.md`; this section decides presentation only.

**`list`** — a Rich table, **in server order** (the endpoint returns `createdAt DESC` and the
renderer must not re-sort): `ID`, `Status`, `Title`, `Author`, `Branch`, `Reviewers`. `Status`
is `derived_state`, never the raw state — the point of the derivation is that the CLI agrees
with the web UI. `Branch` renders `—` when `branchFromId` is null (published/canceled MRs have
no branch). `External ID`, `Created` and `Merged by` appear only when some row carries a value,
so the common table stays narrow. When the result is empty, use the service's `feature_enabled`
(present only on an empty *unfiltered* result) to print *"merge requests are not enabled on this
project"* instead of *"No merge requests"* — the read endpoints are ungated, so the two are
otherwise the same HTTP 200.

**`detail`** — a panel, then sections: mergeable / `merge_blockers` spelled out; `viewer` (*you
created this MR*, *you have approved* — a `None` flag renders as nothing, never as "no");
`allowed_actions` rendered as the commands that produce them; auto-merge state when armed;
branches, reviewers with status, approvals; the **change log**, which is legitimately empty
while the MR sits in `development` (the backend writes it at review time) and must say so
rather than show a bare empty table; conflicts for open MRs; `activityLog` with
`--activity-log`.

**Every wire-sourced string goes through `rich.markup.escape()`** before entering a `Table` or
`Panel` — titles, descriptions, names, conflict messages, reasons. Rich interprets markup by
default, so an MR titled `Fix [bold] parsing` mangles the table and an unbalanced `[/]` raises
`MarkupError`. Ten command modules already import `escape` for exactly this.

**Hint-next.** In human mode every command ends with a one-line next step. Rich-only: since
`_enrich_row` now applies `allowed_actions` to every return, `--json` consumers have the same
information as data, and Layer 1 never manufactures a payload the service did not produce. No
`--no-hint-next` flag in v1.

## Conflicts, diff, resolve

**`conflicts`** — `componentId` / `configurationId` / `isDeleted` / `message`. The entry's
`isDeleted` is the **dev-branch** side's flag, not production's.

**`diff`** — three sections from the service's per-path `changed_by` classification: **Both
changed** (the hotspots; rows flagged `agreed` are demoted to the bottom and marked as
agreement), **Only you changed**, **Only production changed**. Long values elide by default,
`--format full` prints them whole. Deletions are not paths — they are the top-level
`ours_deleted` / `theirs_deleted` flags, rendered as a line above the table. The branch is
derived from the merge request by the service (`get_config_diff(alias, merge_request_id,
component_id, config_id)`), so `diff` never takes a branch of its own.

`--output PATH` writes the ours-prefilled resolution candidate — the flat body `--resolved`
expects. Edit, then `resolve --resolved @file`: the git-mergetool loop with a file as the third
pane. When the ours side is absent or deleted there is nothing to prefill; refuse with a
pointer to `--take delete` rather than writing a misleading skeleton.

**`resolve` has no `--all`.** Rebase *replaces*, so a bulk `--all --take theirs` is a bulk
irreversible overwrite of the dev branch behind one keystroke — and conflicts exist to be walked,
not waved away. (Layer 2 also left it out of v1, on the different grounds that it is a trivial
Layer 1 loop; the decision here stands on the blast radius.) A caller who wants the loop can
write it over `conflicts --json`.

## Merge

- **No `--wait` / `--timeout` in v1.** Layer 3 always awaits the Storage job with
  `MERGE_JOB_MAX_WAIT` (600 s, `constants.py:162`) and exposes no parameter; threading one
  through would mean re-reviewing two already-reviewed layers, and `--no-wait` would need
  polling that does not exist. The help says the command can block for up to 10 minutes. A
  timeout reports `STORAGE_JOB_TIMEOUT` → exit 4, which scripts can tell apart from a failure.
- **Confirmation** in human mode unless `--yes`, naming the MR and the source branch that will
  be deleted. Under `--json`, no prompt (house semantics) and an explicit target instead.
- **Wording:** the source branch **"is being deleted"**, never "is deleted" — a second async job
  with no handle. The service already words this; the renderer must not upgrade it to a fact.
- The 409 arrives pre-mapped as `MR_MERGE_CONFLICT` or `MR_NOT_READY_TO_MERGE`, both carrying
  the next step. Layer 1 prints them as-is.

## Errors and exit codes

- **`FeatureNotEnabledError` must not be flattened.** It is a `ConfigError` subclass carrying
  `error_code = FEATURE_NOT_ENABLED` (`errors.py:219-233`). The common `except ConfigError`
  idiom — and especially the shared `_handle_config_service_error` (`commands/config.py:585`,
  which hardcodes `ErrorCode.CONFIG_ERROR` at `:593`) — would discard it, leaving a `--json`
  consumer unable to tell "merge requests are not enabled on this project" from a bad alias.
  Use `getattr(exc, "error_code", ErrorCode.CONFIG_ERROR)`, as `server/app.py:760` does. Exit 5.
- **Plain `ConfigError` → exit 5** (`CONFIG_ERROR`). The service raises it for the default-branch
  source, the `--take`/`--resolved` mutual exclusion, and an incomplete resolved body. The
  vocabulary errors are pre-empted in Layer 1 at exit 2 (above), so they never reach this path.
- `KeboolaApiError` → `map_error_to_exit_code` unchanged. **`MR_NOT_READY_TO_MERGE` is
  deliberately not added to the exit-4 set**: 4 means transport-level retryable, and conflating a
  backend "another MR is processing" with a connection failure would make exit 4 useless. The
  envelope already carries `retryable: true` and the code — that is what a script branches on.
- A scoped Storage token 403s on everything but `list` (`MergeRequestVoter`, recorded in the
  notes doc). Surfaced as `ACCESS_DENIED`; documented in `gotchas.md`, not special-cased.
- No new error codes: `MR_MERGE_CONFLICT` / `MR_NOT_READY_TO_MERGE` shipped with Layer 2 and are
  in `docs/error-codes.md`.

## Permissions

```
merge-request.list             read
merge-request.detail           read
merge-request.conflicts        read
merge-request.diff             read
merge-request.create           write      (destructive when arming auto-merge)
merge-request.update           write      (destructive when arming auto-merge)
merge-request.request-review   write      (destructive on an armed MR)
merge-request.approve          write      (destructive on an armed MR)
merge-request.resolve          write      (destructive on an armed MR)
merge-request.request-changes  write
merge-request.merge            destructive
merge-request.by-branch        read       # serve-only
```

`merge` is destructive because it irreversibly deletes the source branch and rewrites
production — the class `branch.delete` already occupies. `resolve` is write in the unarmed case
despite replacing configuration content: Keboola keeps configuration versions, so a rebase adds
one rather than destroying the previous.

The useful consequence: an agent under `--deny-destructive` can run the whole flow — create,
review, inspect conflicts, resolve them — and cannot complete a merge by any route, direct or
armed.

## `kbagent serve`

A full `server/routers/merge_requests.py` ships with the commands, prefix `/merge-requests`,
paths `/{project}/…` per the `branches.py` convention. CONTRIBUTING requires the 1:1 mirror,
the service returns plain dicts, and `check_command_sync.py` deliberately does **not** gate
routers (`:34-37`) — a missing route reaches users as an HTTP 404 with nothing red in CI.

- **Every route declares `Depends(require_permission("merge-request.<op>"))`.** Today
  `require_permission` appears only in `server/routers/auth.py`; the other ~30 routers do not
  check the engine. Without it the destructive classification above means nothing over HTTP,
  which would make the whole auto-merge analysis decorative for `serve` callers. The
  state-derived escalations must be evaluated in the route body, where the request body is known.
- **`GET /merge-requests/{project}/by-branch/{branch_id}`** exposes
  `find_merge_request_for_branch` — over HTTP there is no active-branch idiom to hide it behind.
  Register `merge-request.by-branch` in `OPERATION_REGISTRY` and add it to
  `SERVE_ONLY_OPERATIONS` so the dead-key check passes (precedent: `auth.projects`). **Declare
  it before** any `GET /merge-requests/{project}/{merge_request_id}`, or FastAPI matches
  `by-branch` as an id.
- **`POST …/merge` can block for 600 s** — no proxy or HTTP client tolerates that by default.
  Document it on the route.
- Skipped: **`diff --output PATH`**, which writes to the host's disk. `GET …/diff` returns the
  same payload and the caller writes its own file. Note the skip in the PR description.
- Wiring, all required and none of it gated: a `merge_request` field + `__post_init__` line in
  `server/dependencies.py`; `app.include_router(...)` in `server/app.py`; an `OPENAPI_TAGS`
  entry (without it `make endpoints-gen` emits an `(untagged)` section); cases in
  `tests/test_server_router_calls.py`, whose purpose is catching router→service kwarg drift.
  Then `make endpoints-gen`.

## `kbagent branch merge`

Deprecate-with-pointer, same release. **Not a 1:1 replacement**: it only builds a UI URL and
works on **any** project, including one without `branches-merge-requests`. The notice is
therefore conditional — *if this project has merge requests enabled, use `kbagent merge-request
create` + `merge`* — and the command keeps working unchanged; removal is a later decision. It
also **unconditionally resets `active_branch_id`** (`services/branch_service.py:348-349`, which
`merge-requests-layer2.md` calls "the worse of the two precedents"), so describing it as a
harmless URL builder would mislead. Its `write` classification does not change.

## Bookkeeping

### Tests

- `tests/test_merge_request_cli.py` — **mandatory** (`CONTRIBUTING.md:392-394`: service-layer,
  CLI-layer and E2E are three separate requirements). Layers 2 and 3 already shipped
  `test_merge_request_service.py` / `test_merge_request_client.py`; the CLI file is the missing
  third. Given that E2E is unresolved below, **this is the only automated coverage this work
  will actually have** — it is not optional and not implicit.
- `tests/test_server_router_calls.py` additions, per the serve section.

### E2E (convention #16)

**Still open — this RFC does not settle it.** What follows is analysis and a proposed path, not
a decision.

No E2E project carries `branches-merge-requests`, and kbagent cannot provision one:
`ManageClient` has `get_project` / `list_organization_projects` but no project create, and
Connection's own suite creates its projects itself. A new project is not needed, though — the
feature is additive and kbagent ships the command to enable it:

```
kbagent feature project-add --project kbagent-e2e --feature branches-merge-requests
```

(super-admin manage token required). Until then the tests gate the way `conditional_flows` does
— `pytest.skip` on a `FEATURE_NOT_ENABLED` pre-flight — so the suite stays green and starts
covering the group the moment the flag lands. Two properties of the scenario are not obvious:

- **The happy path merges into production.** There is no dry-run merge, so the test creates a
  throwaway config in a dev branch, merges it, and deletes it from production afterwards. That
  is inside the blast radius the flow/config E2E tests already have, but it must be an explicit
  teardown.
- **`merge` takes a project-wide lock** and refuses while another MR in the project is
  processing, so concurrent runs collide with `MR_NOT_READY_TO_MERGE`. Serialise the MR test or
  accept it as a known flake source.

`approve` and `request-review` have no happy path to assert (see above); the E2E asserts the
422 refusal, not a success.

### Documentation

Convention #17 silent-drift surfaces, all mandatory: `commands/context.py` `AGENT_CONTEXT`, the
CLAUDE.md `## All CLI Commands` section, `keboola-expert.md`, `SKILL.md` triggers,
`commands-reference.md`, `gotchas.md`, and a new `merge-request-workflow.md`. Plus
`make skill-gen` (CI-gated via `skill-check`) and `make endpoints-gen`.

`gotchas.md` entries, each tagged `(since vNEXT)`: auto-merge arming as a destructive act and
its invisibility; the `--json` explicit-target rule for destructive invocations; the scoped-token
403 on everything but `list`; the empty change log in `development`; `--reviewer-id` replacing
rather than appending; empty-string-clears; the source branch deleted asynchronously; `approve`
answering 422 on a 0-approval project.

No `mcp_parity.py` work — that map was deleted with the MCP passthrough in 0.85.0.

## Known gaps, documented rather than handled

- **`derived_state` never reports `rejected` / `closed` on a default non-SOX project** — those
  overrides read `reviewers[].status`, which only a review round anchored by a real
  `request_review` event populates, and `skip_review` writes none. The UI badge has the
  identical blind spot, so the CLI and the web still agree; the fix is server-side
  ([DMD-1988](https://linear.app/keboola/issue/DMD-1988)).
- **"What will this MR merge" is unavailable while it is in `development`** — the change log is
  written at review time and the UI computes its preview client-side with no endpoint behind it.
- **Required-approvals count is unreadable with a Storage token** (DMD-1969), which is why the
  auto-merge escalation is conservative rather than precise.
