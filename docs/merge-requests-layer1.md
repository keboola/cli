# Merge requests — Layer 1 (commands), RFC

Linear: [DMD-1900](https://linear.app/keboola/issue/DMD-1900). The `kbagent merge-request`
command group over `MergeRequestService` (Layer 2, DMD-1899). Backend facts with citations
live in [`merge-requests-notes.md`](merge-requests-notes.md); the service contract this RFC
consumes is [`merge-requests-layer2.md`](merge-requests-layer2.md); the HTTP client is
[`merge-requests-layer3.md`](merge-requests-layer3.md). Scope stays **non-SOX**.

Layer 2 settles almost the whole surface: eleven service methods map to eleven commands with
no invention. What this RFC decides is the parts Layer 2 deliberately left to the caller —
target resolution, rendering, error presentation, risk classification — plus the surfaces
convention #17 requires.

## Shape

- Group name **`merge-request`**, mounted in `cli.py` under the `_DEV` help panel immediately
  after `branch`; hidden alias **`mr`** (precedent: `sl` for `semantic-layer`).
- Two modules: `commands/merge_request.py` (Typer commands) and
  `commands/_merge_request_render.py` (Rich renderers). **The split is decided up front, not
  deferred.** Eleven commands at this repo's ~50-code-lines-per-command average already sit
  near the `commands/*.py` soft ceiling of 800, and four renderers are non-trivial (list
  table, detail panel, conflicts table, three-way diff). `output.py` is not an option: it is
  at 1013 lines against a 1000 soft ceiling, so a fifth `format_*` family there would push it
  over. Precedent for the private sibling: `_storage_describe.py`, `_auth_picker.py`.
- Group callback `check_cli_permission(ctx, "merge-request")`, as every group has.
- Service registered in `cli.py` as `ctx.obj["merge_request_service"]`.

## Command surface

Eleven commands, one per service method. `--project` follows the house rule everywhere
(`resolve_project_alias` on writes).

| Command | Service method | Notes |
|---|---|---|
| `list --project A [--state V]` | `list_merge_requests` | `--state` filters client-side |
| `detail [--mr-id N] [--branch B] [--activity-log]` | `get_merge_request` | adds live conflicts for open MRs |
| `create --title T [--branch B] [--description D] [--reviewer-id ID ...] [--auto-merge-strategy S] [--auto-merge-at TS] [--external-id X]` | `create_merge_request` | source branch via `resolve_branch()` |
| `update [--mr-id N] [--title] [--description] [--reviewer-id ...] [--auto-merge-strategy] [--auto-merge-at] [--external-id]` | `update_merge_request` | |
| `request-review [--mr-id N]` | `request_review` | lands in `approved` directly (see below) |
| `approve [--mr-id N]` | `approve` | 422 on a 0-approval project (see below) |
| `request-changes [--mr-id N] [--reason TEXT]` | `request_changes` | `reason` capped at 1000 chars server-side |
| `merge [--mr-id N] [--yes]` | `merge` | confirmation unless `--yes` / `--json` |
| `conflicts [--mr-id N]` | `list_conflicts` | |
| `diff --component-id C --config-id I [--mr-id N] [--format short\|full] [--output PATH]` | `get_config_diff` | branch derived from the MR |
| `resolve --component-id C --config-id I (--take ours\|theirs\|delete \| --resolved JSON\|@file\|-) [--mr-id N] [--change-description TEXT]` | `resolve_conflict` | exactly one of `--take` / `--resolved` |

`find_merge_request_for_branch` gets **no command of its own** — it is the resolver behind
`--mr-id` (below), and its result is visible in every command's output line.

### `request-review` and `approve` on a default project

Both commands exist because the state machine has them, but on a non-SOX project with the
default **0 required approvals** neither has a happy path: `request_review` is auto-finished by
the backend (`skip_review`), so the MR jumps straight to `approved` and `in_review` is never
reached — and `approve`, whose only `from` place is `in_review`, therefore answers **422 in
every state**. `merge` works directly from `development` anyway. The commands ship (a project
that raises the required count needs them), but their `--help` states the default-project
reality plainly rather than letting the user discover it as an unexplained 422.

### Flag details that are not free choices

- **`--reviewer-id` must normalise empty to `None`.** `_optional_mr_fields`
  (`client/merge_requests.py:113`) includes `reviewerIds` whenever it is not `None`, so an
  empty list is sent as `reviewerIds: []` and the server **replaces the reviewer set with
  nothing**. A Typer repeatable option that yields `()` instead of `None` would therefore
  silently clear all reviewers on any `update` that did not mention them. Normalise with an
  explicit `or None` at the call site.
- **`update` semantics:** `None` = leave unchanged; an **empty string clears**
  `description` / `externalId` (server-side `?? null` mapping). There is no clear-to-null for
  anything else. Passing `--reviewer-id` at all **replaces** the set — it never appends.
- `--auto-merge-strategy` accepts exactly `immediately` | `scheduled` | `none`.
- `--external-id` is capped at 255 characters server-side.
- **`--state` stays a plain `str`, not a Typer enum.** The vocabulary lives in the service
  (`_STATE_FILTER_VOCABULARY`) and an unknown value already fails with the accepted list. A
  second copy as an `Enum` in Layer 1 is exactly the drift convention #17 exists to prevent.
  The help string enumerates the values for discoverability.

## Which merge request am I working on (decided 2026-08-27)

`--mr-id` is **optional on every command that takes one**, resolved in three steps:

1. `--mr-id` given → use it.
2. Otherwise `resolve_branch()` — the standard idiom of 11 command modules: explicit
   `--branch`, else `active_branch_id` from `config.json`.
3. On that branch, `find_merge_request_for_branch()` → the MR id.

With neither an id nor a branch, the error is the house wording: *pass `--branch` or run
`branch use`*. The chain is unambiguous because a branch has **at most one MR, ever** (the
backend's existence check has no state filter), so step 3 can only find one or none.

This is why `find_merge_request_for_branch` was added to Layer 2 at all. It calls
`GET /v2/storage/merge-request` **on every invocation** — nothing is cached. `active_branch_id`
is persisted because it is a *user decision* that cannot be derived; branch→MR is *derivable
server state*, and persisting it would be a cache with no invalidation (someone merges from
the UI, and the CLI operates on a stale id). Persist decisions, derive facts.

`merge` is **not** exempt from the fallback. It is the same principle as everywhere else, and
the risk is covered by the confirmation prompt plus the resolution line in the output.

**Every command that resolved its target implicitly says so**, on stderr in human mode:

```
Info: Using active branch (ID: 123) for project 'acme'
Info: Resolved merge request #7 from branch 123
```

## What the wire actually carries

Verified against `MergeRequestResponseProvider.php` (Opus wire review, 2026-08-27). Two facts
change what the renderers can show:

- **List and detail have a byte-identical item shape.** Detail adds `changeLog`, and
  `activityLog` only with `?include=activityLog`. So anything the detail panel renders, the
  list could too — the limit is width, not data.
- **There are no timestamps.** The serializer emits `id`, `state`, `title`, `description`,
  `externalId`, `mergerName`, `creator{id,name}`, `approvals[]{approverId,approverName}`,
  `reviewers[]{id,name,status}`, `branches{branchFromId,branchIntoId}` — and nothing else.
  A `Created` / `Updated` column is **impossible**, not merely omitted. The list arrives
  server-side `createdAt DESC`, so **the renderer must preserve the server's order** (newest
  first) — it is the only chronological signal that survives.

Type traps already handled in Layer 2, restated so renderers do not re-introduce them:
`creator.id` and `reviewers[].id` are ints, `approvals[].approverId` is a **string**;
`branches.branchFromId` is nullable once the source branch row is deleted.

**Access:** every `/merge-request/{id}` endpoint — detail and conflicts included — goes through
`MergeRequestVoter`, which denies a token with no admin. **A scoped Storage token gets 403 on
`detail`, `conflicts`, `diff` and `resolve`, but `list` works** (the list action has no voter).
Layer 1 does not special-case this in v1; it surfaces as `ACCESS_DENIED` and is documented in
`gotchas.md`. See *Known gaps* below.

## Human rendering

**`list`** — a Rich table in server order:

| ID | Status | Title | Author | Branch | Reviewers |

`Status` is `derived_state`, not raw `state` — the whole point of the derivation is that the
CLI shows what the web shows. `Branch` renders `branches.branchFromId`, or `—` when null
(published/canceled MRs have no branch). `Reviewers` shows names with their status; an MR with
none renders empty rather than a placeholder. `External ID` and `Merged by` are shown only
when at least one row carries a value, so the common table stays narrow.

**`detail`** — a panel, then sections:

- header `#7 <title>` + `derived_state`
- **Mergeable / blocked** — `mergeable` plus `merge_blockers` spelled out (`conflicts (3)`,
  `approvals`, `state`). Worded as information, never as a guarantee: the merge 409 is the
  authority.
- **You** — from `viewer`: *you created this MR* / *you have approved*. `None` flags render as
  nothing, never as "no".
- **Next** — `allowed_actions` rendered as the commands that produce them.
- branches from → into, reviewers with status, approvals
- **Change log** — the configurations the merge will apply. **Legitimately empty while the MR
  sits in `development`**: the backend writes it at `request_review` / `skip_review`, not at
  create. The renderer says so instead of showing a bare empty table.
- **Conflicts** — count + list, for open MRs only
- **Activity log** — only with `--activity-log`

**Hint-next.** In human mode every command ends with a one-line next step (`create` → *when
you are done, `merge-request merge`*; a merge conflict → *run `merge-request conflicts`*). This
is Rich-only: `--json` already carries `allowed_actions`, and inventing a `next_steps` payload
in Layer 1 would mean the command layer manufacturing data the service never produced. No
`--no-hint-next` flag in v1 (one line is not noise).

## Conflicts, diff, resolve

**`conflicts`** — a table of `componentId` / `configurationId` / `isDeleted` / `message`. Note
the entry's `isDeleted` is the **dev-branch** side's flag, not production's.

**`diff`** — three sections, from the service's per-path `changed_by` classification:

- **Both changed** — the actual conflict hotspots. Rows carrying `agreed: true` are demoted
  to the bottom of this section and marked as agreement (both sides moved, nothing to decide).
- **Only you changed**
- **Only production changed**

Long values are elided by default; `--format full` prints them untruncated. Deletions never
appear as paths — they are the top-level `ours_deleted` / `theirs_deleted` flags, rendered as a
line above the table.

`--output PATH` writes the **ours-prefilled resolution candidate**: the flat body
`--resolved` expects (`name`, `rows`, `configuration`, and optionally `description`,
`isDisabled`). This is the git-mergetool loop with a file as the third pane — edit, then
`resolve --resolved @file`. When the ours side is absent or deleted there is nothing to
prefill: refuse with a pointer to `--take delete` rather than writing a misleading skeleton.

**`resolve` has no `--all` (decided 2026-08-27).** Rebase *replaces*; a bulk
`--all --take theirs` is a bulk irreversible overwrite of the dev branch behind one keystroke.
Conflicts are meant to be walked, not waved away. Layer 2 excluded it from v1 for the same
reason. A caller who genuinely wants the loop can write it in three lines of shell over
`conflicts --json`.

## Merge

- **No `--wait` / `--timeout` in v1 (decided 2026-08-27).** Layer 3 always awaits the Storage
  job with `MERGE_JOB_MAX_WAIT` (600 s) and exposes no parameter; a `--timeout` would have to
  be threaded through both already-reviewed layers, and `--no-wait` would need polling that
  does not exist. The help says the command can block for up to 10 minutes. A timeout already
  reports `STORAGE_JOB_TIMEOUT` → exit 4, which scripts can tell apart from a failure.
- **Confirmation** unless `--yes` or `--json`, the house pattern (`notification delete`,
  `branch metadata-delete`). The prompt names the MR and the source branch that will be
  deleted.
- **Wording:** the source branch **"is being deleted"**, never "is deleted" — the deletion is
  a second async job with no handle. The service already words this; the renderer must not
  rephrase it into a completed fact.
- The 409 arrives pre-mapped as `MR_MERGE_CONFLICT` or `MR_NOT_READY_TO_MERGE`, both already
  carrying the next step in the message. Layer 1 prints them as-is.

## Errors and exit codes

- **`FeatureNotEnabledError` must not be flattened.** It is a `ConfigError` subclass carrying
  `error_code = FEATURE_NOT_ENABLED`. The common `except ConfigError` idiom — and especially
  the shared `_handle_config_service_error` (`commands/config.py:585`) — hardcodes
  `ErrorCode.CONFIG_ERROR` and would throw that code away, leaving a `--json` consumer unable
  to tell "merge requests are not enabled on this project" from a typo in the alias. Use
  `getattr(exc, "error_code", ErrorCode.CONFIG_ERROR)`, the way `server/app.py:760` does.
  Exit 5 either way.
- `KeboolaApiError` → `map_error_to_exit_code` unchanged. **`MR_NOT_READY_TO_MERGE` is
  deliberately not added to the exit-4 set**: 4 means network/retryable-transport, and
  conflating a backend "another MR is processing" with a connection failure would make exit 4
  useless. The error envelope already carries `retryable: true` and the code — that is what a
  script branches on.
- No new error codes. `MR_MERGE_CONFLICT` / `MR_NOT_READY_TO_MERGE` shipped with Layer 2 and
  are documented in `docs/error-codes.md`.

## Permissions

`merge` is **destructive** (decided 2026-08-27): it irreversibly deletes the source branch and
rewrites production — the same class as `branch.delete`. The useful consequence is the split
it creates: an agent under `--deny-destructive` can run the entire flow (create, review,
inspect conflicts, resolve them) and **must hand the last step to a human**.

`resolve` stays **write** despite replacing configuration content: Keboola keeps configuration
versions, so a rebase adds a version rather than destroying the previous one.

```
merge-request.list             read
merge-request.detail           read
merge-request.conflicts        read
merge-request.diff             read
merge-request.create           write
merge-request.update           write
merge-request.request-review   write
merge-request.approve          write
merge-request.request-changes  write
merge-request.resolve          write
merge-request.merge            destructive
merge-request.by-branch        read      # serve-only, see below
```

No `FLAG_ESCALATIONS` entries — no flag on any of these crosses into a higher class.

## `kbagent serve`

A full `server/routers/merge_requests.py` ships **in this PR** (decided 2026-08-27), prefix
`/merge-requests`, paths `/{project}/…` per the `branches.py` / `notifications.py` convention.
CONTRIBUTING requires the 1:1 mirror, the service returns plain dicts so it is cheap, and
`scripts/check_command_sync.py` deliberately does **not** gate routers — a missing route would
therefore reach users as an HTTP 404 with nothing red in CI.

- Every command gets a route except **`diff --output PATH`**, which writes to the host's disk
  and has no meaning over HTTP; `GET …/diff` returns the same payload and the caller writes its
  own file. Document the skip in the PR description, as CONTRIBUTING asks.
- One route with **no CLI leaf command**: `GET /merge-requests/{project}/by-branch/{branch_id}`,
  exposing `find_merge_request_for_branch` — over HTTP there is no "active branch" idiom to
  hide it behind. Register `merge-request.by-branch` in `OPERATION_REGISTRY` and add it to
  `SERVE_ONLY_OPERATIONS` so the dead-key check passes (precedent: `auth.projects`).
- Run `make endpoints-gen` (gated by `make endpoints-check`).

## `kbagent branch merge`

Deprecate-with-pointer, in the same release. **It is not a 1:1 replacement**: today's command
only builds a UI URL and works on **any** project, including one without the
`branches-merge-requests` feature. So the deprecation notice must be conditional — *if this
project has merge requests enabled, use `kbagent merge-request create` + `merge`* — and the
command keeps working unchanged. Removal is a later release's decision.

## Bookkeeping

### E2E (convention #16)

**Still open — the E2E story is not settled by this RFC.** What follows is the analysis and
the proposed path, not a decision: how the group actually gets live coverage is a separate
conversation before implementation lands.

No E2E project carries `branches-merge-requests` today, and **kbagent cannot provision one** —
`ManageClient` has `get_project` / `list_organization_projects` but no project create, and
Connection's own E2E suite creates its projects itself. A new project is not needed, though:
the feature is additive and kbagent already ships the command to turn it on, so the plan is a
one-time enablement of the existing E2E project with a super-admin manage token:

```
kbagent feature project-add --project kbagent-e2e --feature branches-merge-requests
```

Until it is enabled, the tests gate the way `conditional_flows` does — `pytest.skip` on a
`FEATURE_NOT_ENABLED` pre-flight — so the suite stays green and starts covering the group the
moment the flag lands. Two properties of the scenario are not obvious:

- **The happy path merges into production.** There is no dry-run merge, so the test necessarily
  creates a throwaway config in a dev branch, merges it, and then deletes it from production.
  That is inside the blast radius the flow/config E2E tests already have, but it must be
  written as an explicit teardown, not left to the next run.
- **`merge` takes a project-wide lock** and refuses while another MR in the project is
  processing, so two concurrent E2E runs against the same project will collide with
  `MR_NOT_READY_TO_MERGE`. Serialise the MR test or accept it as a known flake source.

**`approve` and `request-review` have no happy path to assert.** On a non-SOX project with the
default 0 required approvals, `request_review` is auto-finished by the backend and the MR lands
directly in `approved` (`in_review` is unreachable), and `approve` — whose only `from` place is
`in_review` — answers **422 in every state**. The E2E for `approve` therefore asserts the
refusal, not a success, and the command's `--help` says so. This is not a kbagent limitation:
it is what a 0-approval project means.
- Convention #17 silent-drift surfaces, all mandatory: `commands/context.py` `AGENT_CONTEXT`,
  the CLAUDE.md `## All CLI Commands` section, `keboola-expert.md` (tool-selection matrix +
  version gate), `SKILL.md` triggers, `commands-reference.md`, `gotchas.md`, and a new
  `merge-request-workflow.md` reference doc.
- `gotchas.md` entries, each tagged `(since vNEXT)`: the no-timestamps list shape; the
  scoped-token 403 on everything but `list`; the empty change log in `development`;
  `--reviewer-id` replacing rather than appending; the empty-string-clears rule; the source
  branch being deleted asynchronously.
- `docs/web-server-endpoints.md` via `make endpoints-gen`.
- No `mcp_parity.py` work — that map was deleted with the MCP passthrough in 0.85.0.

## Known gaps, documented rather than handled

- **A scoped Storage token gets 403 on everything but `list`.** Wording it properly is Layer 2
  work (only the service knows which endpoint answered), and Layer 2 is already reviewed in
  PR #703. v1 surfaces the raw `ACCESS_DENIED` and documents it; fix it the next time Layer 2
  is opened.
- **"What will this MR merge" is unavailable while the MR is in `development`.** The change log
  is written at review time; the UI computes the preview client-side with no endpoint behind
  it. Out of scope, as agreed for Layer 2.
- **`derived_state` never reports `rejected` / `closed` on a default non-SOX project.** Those
  overrides read `reviewers[].status`, which is only populated inside a review round anchored
  by a real `request_review` event — and with 0 required approvals every request-review takes
  the `skip_review` path, which writes none. **The UI badge has the identical blind spot**, so
  the CLI and the web still agree; the fix is server-side (DMD-1988). The practical consequence
  for Layer 1: **there is no `close` command.** Creator-request-changes *is* the UI's cancel,
  but it leaves `state=development`, so a `close` command would look like a no-op. It is
  documented in `request-changes --help` and in `gotchas.md` instead.
