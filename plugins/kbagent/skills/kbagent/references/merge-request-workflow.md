# Merge Request Workflow -- merging a dev branch into production with review

*(since vNEXT, DMD-1900)*

`kbagent merge-request` (alias `mr`) is the non-SOX "Branches 2.0" lifecycle: open a merge
request from a development branch, optionally get it reviewed, inspect and resolve conflicts,
merge. Requires the project feature `branches-merge-requests`. Read this before automating any
of it -- **two things in this group merge production without a human saying so** if you let them.

## The short path (0 required approvals, the non-SOX default)

```bash
kbagent branch create --project P --name "fix-x"          # auto-activates the branch
# ... edit configs on the branch (config update / sync push --branch) ...
kbagent mr create --project P --title "Fix X"              # from the active branch
kbagent mr detail --project P                              # readiness, blockers, conflicts
kbagent mr merge --project P                               # prompt -> merge -> branch deleted
```

Every command except `list` / `create` finds its merge request **from the active branch** when
`--merge-request-id` (or `--id`) is omitted -- a branch has at most one merge request, ever.
`--branch B` names another branch; `--merge-request-id N` names the MR directly; both at once
is exit 2. `--project` is single-project (the `project use` pin works).

`merge` works **straight from `development`** on a 0-approval project: the backend skips the
review itself. `request-review` is optional there (and lands directly in `approved`);
`approve` answers **422 in every state** because `in_review` is never reached. Both commands
exist for projects that require approvals.

## From a script or an agent (`--json`)

```bash
MR=$(kbagent --json mr create --project P --title "Fix X" --branch 123 | jq .data.id)
kbagent --json mr detail --project P --merge-request-id "$MR" | jq '.data | {mergeable, merge_blockers}'
kbagent --json mr merge  --project P --merge-request-id "$MR"     # explicit target REQUIRED
```

**Under `--json`, `merge` requires an explicit target** (`--merge-request-id` or `--branch`).
Every destructive kbagent command either prompts or is told its target; `--json` has no
prompt, so a bare `--json mr merge` would be the one command where nothing on the command line
says what gets destroyed. The same rule applies to every invocation that *escalates* to
destructive (next section). Every `--json` result carries `merge_request_id`,
`branch_from_id` and `resolved_from_branch` so you can assert on what was operated upon.

## Auto-merge is a production merge -- treat it as one

`--auto-merge-strategy immediately|scheduled` (on `create` or `update`) is not metadata. A
backend scheduler runs every `approved` merge request armed with it through the **same merge
processor** the `merge` command uses -- on its own, retrying every tick until it lands, with
`merge` never called and nothing in the arming call's response saying so. Consequences kbagent
enforces:

| operation | class | why |
|---|---|---|
| `merge` | destructive | deletes the source branch, rewrites production |
| `create` / `update` with `--auto-merge-strategy immediately\|scheduled` | destructive | arming IS a delayed merge |
| `request-review` / `approve` / `resolve` on an **already-armed** MR | destructive | they move it into `approved`, which is what the scheduler waits for (`resolve` unblocks a merge stuck on a conflict) |
| `--auto-merge-strategy none` | write | the disarm -- never escalates, so `--deny-destructive` can always disarm |

So an agent run with `--deny-destructive` can open, review, inspect and resolve, and **cannot
complete a merge by any route** -- direct or armed. Human mode prompts at the two decision
points (`merge`, arming); the armed transitions print a warning saying the merge is now
imminent. The escalation is deliberately conservative: on a 2-approval project `request-review`
lands in `in_review` and merges nothing, but the required count is unreadable with a Storage
token (DMD-1969), so every armed operation escalates.

## Conflicts

A conflict = a configuration changed in the branch **and** in production since the branch was
created. The backend computes them live on every `conflicts` call and every `merge` attempt;
rebasing each listed configuration is sufficient (no re-validate step).

```bash
kbagent mr conflicts --project P                                     # what is in conflict
kbagent mr diff --project P --component-id C --config-id I            # per path: both / only you / only production
kbagent mr resolve --project P --component-id C --config-id I --take ours     # keep your content
kbagent mr resolve ... --take theirs                                   # adopt production's
kbagent mr resolve ... --take delete                                   # drop the configuration
```

For a hand-made three-way merge, the git-mergetool loop with a file as the third pane:

```bash
kbagent mr diff --project P --component-id C --config-id I --output resolved.json   # your content, prefilled
$EDITOR resolved.json
kbagent mr resolve --project P --component-id C --config-id I --resolved @resolved.json
```

- `diff` reports a side that deleted the configuration wholesale **as a sentence with the
  `--take` to pick**, not as a table: "Production deleted this configuration; your branch
  changed it. Resolve with `--take delete` or `--take ours`."
- The `--output` file carries **all five keys** -- `name`, `description` (an explicit `null`
  when empty), `isDisabled`, `configuration`, `rows`. Rebase REPLACES the whole configuration:
  a body missing any of them is refused, because a defaulted `isDisabled` would silently
  re-enable a disabled configuration and the merge would push that to production. Edit
  values, do not delete keys.
- There is no `--all`. Conflicts are meant to be walked; loop over `conflicts --json` yourself
  if you truly want to take one side everywhere.
- `--change-description` on a `--take delete` is dropped with a warning -- the delete
  tombstone has nowhere to carry it.

## What the outputs mean

- **Status** is the *derived* state the web UI shows: `in_development`, `in_review`,
  `approved`, `in_merge`, `merged`, `closed`, `rejected`. `closed` / `rejected` depend on
  reviewer status a 0-approval project never populates -- the same blind spot the UI has
  (DMD-1988). **There is no `close` command**: `request-changes` by the creator is the UI's
  cancel and leaves the MR in `development`.
- `detail.mergeable` / `merge_blockers` (`conflicts`, `approvals`, `state`) are
  informational; the merge itself is the authority (409 → `MR_MERGE_CONFLICT` or
  `MR_NOT_READY_TO_MERGE`, the latter retryable).
- The **change log is empty until the MR is sent for review** -- the backend writes it then.
  Not a gap; "what will this merge" is unavailable in `development`.
- `allowed_actions` (and the human hint-next line) are state-derived and **feature-blind**: on
  a project where the feature was later switched off they recommend writes that will fail
  `FEATURE_NOT_ENABLED`.
- Every result may carry `warnings[]` (a failed post-merge cleanup, a dropped
  change-description). Render or log it.
- A **truncated conflict list** inside `MR_MERGE_CONFLICT` carries
  `details.api_error_params_truncated: true` -- run `conflicts` for the full set.

## Errors you will meet

| error | cause | do |
|---|---|---|
| `FEATURE_NOT_ENABLED` (exit 5) | project lacks `branches-merge-requests` -- surfaces from ANY command whose target was resolved implicitly, reads included; second wording = SOX project (`protected-default-branch`), which kbagent does not support | enable the feature / use the UI on SOX |
| `ACCESS_DENIED` on everything but `list` | scoped Storage token; the detail/conflicts endpoints require an admin identity | use a master token |
| exit 2 `INVALID_ARGUMENT` | both `--merge-request-id` and `--branch`; unknown `--state`/`--take`; `--json` destructive without a target; `update` with no fields; `--auto-merge-at` without `scheduled` | fix the flags |
| `NOT_FOUND` from the resolver | the branch has no merge request | `mr create` |
| `MR_NOT_READY_TO_MERGE` (retryable) | merge lock, wrong state, another MR merging | retry |
| `STORAGE_JOB_TIMEOUT` (exit 4) | merge ran past 10 min; it continues server-side | poll `mr detail` |

## Requesting review emails people

With no `--reviewer-id` and no project-designated reviewers, the review-requested notification
goes to **every project member**. Pass reviewer ids (from `project member-list`) or, on a
0-approval project, just `merge`.

## `branch merge` is deprecated

It only builds a UI URL (and resets the active branch). It keeps working -- it also serves
projects without the feature -- but on a project with merge requests enabled use this group.
