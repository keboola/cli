# GitOps / CI/CD Deploy Pipeline Workflow

How to run kbagent unattended in a deploy pipeline (GitHub Actions, GitLab CI,
any container): configs live in a git repository, every run deploys them into a
Keboola dev branch or production with `sync`. The pitfalls at the end were all
hit by a customer running exactly this flow on 0.100.4; each one names the
versions it affects and the workaround.

Related: [sync-workflow.md](sync-workflow.md) (the sync engine),
[sync-rows-workflow.md](sync-rows-workflow.md) (rows),
[branch-workflow.md](branch-workflow.md),
[safe-write-workflow.md](safe-write-workflow.md),
[gotchas.md](gotchas.md). For a ready-made GitHub Actions pipeline between two
projects see the sibling skill
[kbagent-promotion-pipeline](../../kbagent-promotion-pipeline/SKILL.md); for
moving a `kbc` (keboola-as-code) pipeline see
[kbagent-cicd-migration](../../kbagent-cicd-migration/SKILL.md).

## 1. Install a pinned version

A pipeline must not change kbagent under its own feet.

- **Pin the version**: `uv tool install 'keboola-cli==0.100.4'` (or
  `pip install 'keboola-cli==0.100.4'`). To stay on the 0.x line without an exact
  pin, use `'keboola-cli<1'`.
- **Turn off the self-update**: set `KBAGENT_AUTO_UPDATE=false` in the job
  environment. Without it, kbagent checks for a newer release at startup and can
  update itself in the middle of a pipeline -- after the 1.0.0 migration date also
  to the next major version.
- **Pre-releases are not on PyPI.** A `1.0.0rcN` build is published only as a
  GitHub Release asset. Install it by URL:
  `uv tool install https://github.com/keboola/cli/releases/download/v1.0.0rc4/keboola_cli-1.0.0rc4-py3-none-any.whl`.
  `keboola-cli<1` / `==X.Y.Z` from PyPI never picks one up.
- **Check what runs** with `kbagent --json version` and read
  `data.kbagent.version` (also `latest_version`, `up_to_date`,
  `upgrade_command`). `kbagent --json --version` prints plain text
  (`kbagent vX.Y.Z`), not JSON -- the `--version` flag ignores `--json`.

## 2. Headless project: no config file, no login

```bash
export KBAGENT_PROJECT_FROM_ENV=1
export KBC_TOKEN=...                 # from the CI secret store
export KBC_STORAGE_API_URL=https://connection.keboola.com
export KBAGENT_AUTO_UPDATE=false
```

kbagent then has an in-memory project named `__env__` (since 0.50.0). The token
is never written to disk. Pass `--project __env__` on every command.

- **Pass `--branch <id>` on every command that should act in a dev branch.** The
  `__env__` project has no stored active branch: `branch use` and the activation
  that `branch create` does cannot be saved for it (see pitfall 2.6 below).
- **Put `--json` before the subcommand**: `kbagent --json sync push ...`. 0.x
  refuses root flags after the subcommand.
- **Never run `sync`, `branch` or a write without `--project __env__`.** On 1.x an
  omitted `--project` uses the active project.

## 3. The deploy run

Use an empty folder per run. The repository holds the config files; the
manifest and the baseline come from a fresh pull of the target branch.

```bash
WORK=$(mktemp -d)
BR=12345                                          # the dev branch id

kbagent --json sync init --project __env__ --directory "$WORK"
kbagent --json sync pull --project __env__ --branch "$BR" --directory "$WORK"
# sync pull also initializes an empty folder by itself; init is optional.

# Copy the repository's config files over the pulled tree, e.g.
#   rsync -a repo/keboola/ "$WORK/<branch folder>/"
# A dev-branch pull writes into that branch's own folder, not main/.

# Validate every _config.yml before kbagent reads it (pitfall 2.2).

kbagent --json sync push --project __env__ --branch "$BR" --directory "$WORK" --dry-run > plan.json
# Review plan.json: data.changes[], data.skipped_deletions, data.errors.

kbagent --json sync push --project __env__ --branch "$BR" --directory "$WORK" [--force] > result.json
```

`--force` is needed only to delete remote configs or rows whose folders are gone
(since 0.96.1 a plain push deletes nothing and lists them under
`skipped_deletions`). Never add `--force` without a reviewed `--dry-run` -- see
pitfall 2.2.

## 4. Read results safely

1. **The process exit code first.** 0 = done; 1 = at least one item failed (the
   items that succeeded stay written, since 0.98.0); 2 = usage; 3 = auth;
   4 = network; 5 = config; 6 = permission denied.
2. **`status: ok` does not mean every item succeeded.** On 1.x the envelope
   carries `exit_code`: `status: "ok"` with a non-zero `exit_code` is a partial
   failure. On 0.x the envelope has no `exit_code`; use the process exit code.
3. **Per-item failures** are in `data.errors[]`. Read `message` (and
   `error_code` when present). On 1.x every failed item in `failed[]` /
   `errors[]` carries `message` with `error_code` and `retryable`; 0.x commands
   sometimes used `error` or `reason` -- fall back to them (see
   [gotchas.md](gotchas.md) "Keys removed from `--json` output in 1.0.0").
4. **Branch on `data.status` of `sync push`**: `pushed`, `dry_run` or
   `no_changes`. The shapes differ -- see pitfall 5.2.
5. **Default every optional list to empty.** Many keys appear only when not
   empty -- see pitfall 5.3.

## 5. Pitfalls

Version notes: "<= 0.100.4" is the stable line the customer used. 1.0.0rc4 is
the pre-release cut from `main`. *(since vNEXT)* marks a fix that ships in the
next release; each entry says whether it is already in 1.0.0rc4.

### 1. A transformation job is `success` but did not run the SQL (platform)

- **Symptom:** in a dev branch, `keboola.snowflake-transformation` 1.6.1 jobs ended
  `success` without running the user's statements. The job events show only the
  setup queries, no "Running query" event for the statement.
- **Affects:** the component, not kbagent. Reported to the component team.
- **Workaround:** a deploy smoke test must not trust `success` alone. Read the
  job's log: `kbagent --json job detail --project __env__ --job-id ID --log-tail-lines 200`
  and check that each statement ran (or check the output table).

### 2.1 New config with rows (no ids) in one push

- **Symptom (<= 0.100.4):** a new config folder with a `rows/` subfolder: one
  `sync push` created only the config; a second push created the rows.
- **Status:** fixed *(since vNEXT)* by #931 -- already in 1.0.0rc4: the rows are
  created in the same push, and each new row's `_config.yml` gets its
  `_keboola.row_id` (not on a promote push `--branch <dev>` from `main/`).
- **Still true:** `pushed_details[].config_id` is `""` for a config the push
  created (the entry is the planned change, made before the id existed). Read the
  new id from the rewritten `_config.yml` (`_keboola.config_id`) or from
  `.keboola/manifest.json` (`configurations[]` entry with the folder's `path`).
  Workaround on <= 0.100.4: run `sync push` twice, then check `sync diff` is clean.

### 2.2 A `_config.yml` that does not parse is skipped -- or deleted on the remote

- **Symptom (<= 1.0.0rc4):** a `_config.yml` with a YAML syntax error is read as
  "no file". Push prints only a stderr `WARNING Failed to parse ...`, reports
  `status` ok and exits 0. For an untracked (new) config nothing is created. For
  a tracked config the folder counts as deleted: a plain push lists it under
  `skipped_deletions`, and **`sync push --force` deletes the remote config**
  (#827 item 9).
- **Workaround:** before any push, parse every `_config.yml` of the tree with a
  YAML parser (for example `python -c 'import sys,yaml; [yaml.safe_load(open(p)) for p in sys.argv[1:]]' $(find "$WORK" -name _config.yml)`)
  and fail the pipeline on an error or on a file that is empty or not a mapping.
  Never pass `--force` unless the `--dry-run` was reviewed and every `deleted`
  change / `skipped_deletions` entry is intended.
- **Status:** fix in #941 *(since vNEXT)*, not in 1.0.0rc4: such a file is an
  `errors[]` entry (`change_type: invalid_local_file`, `error_code:
  SYNC_LOCAL_FILE_INVALID`), its config and rows are never created, updated or
  deleted (also with `--force`), and the command exits 1.

### 2.3 Unknown top-level keys in `_config.yml` are dropped

- **Symptom (<= 1.0.0rc4):** push sends only `name`, `description`, `parameters`,
  `input` / `output` (as `configuration.storage`), `processors` and the keys
  under `_configuration_extra` (plus `is_disabled`, and the companion files such
  as `transform.sql`). Any other top-level key -- a flow's `phases:` / `tasks:`,
  `runtime:`, `storage:`, a typo -- is dropped without a message.
- **Workaround:** put every other configuration key under `_configuration_extra:`
  (that is where `sync pull` writes a flow's `phases` and `tasks` and a
  `runtime` block). Compare your file with a freshly pulled one.
- **Status:** fix in #941 *(since vNEXT)*, not in 1.0.0rc4: an unknown key is a
  `SYNC_LOCAL_FILE_INVALID` error naming the key, and the config is held.

### 2.4 One-line `dependencies = [...]` in `pyproject.toml` is ignored

- **Symptom (<= 0.100.4):** the package list is read only from the multi-line
  form (`dependencies = [` alone on a line, one package per line, `]` alone on a
  line). A one-line list is read as no packages, and push keeps the old packages.
- **Workaround (<= 0.100.4):** use the multi-line form.
- **Status:** fixed *(since vNEXT)* by #931 -- already in 1.0.0rc4: the file is
  parsed as TOML.

### 2.5 `flow schedule` exits 0 although the schedule is not active

- **Symptom (all versions since 0.66.1):** when the Scheduler Service refuses the
  activation (for example a token without the permission to manage schedules),
  `flow schedule` keeps the written schedule config, exits **0**, and returns
  `data.activated: false` with a message in `data.warnings[]`. The cron never
  fires.
- **Workaround:** always check `data.activated` is `true`; fail the pipeline
  otherwise and re-run with a token that can manage schedules. See
  [schedule-workflow.md](schedule-workflow.md).
- **Status:** not changed; by design a documented partial outcome.

### 2.6 / 3.7 `branch create` on `__env__` reports an error but creates the branch

- **Symptom (<= 1.0.0rc4):** `branch create --project __env__` creates the branch,
  then fails to store it as the active branch: `status: error`, exit 5. A retry
  creates a second branch. `branch reset --project __env__` and
  `branch use --project __env__` are refused with exit 5.
- **Workaround:** after a `branch create` error, look the branch up with
  `kbagent --json branch list --project __env__` (by name) before you retry. Never
  use `branch use` / `branch reset` on `__env__`; pass `--branch <id>` everywhere.
- **Status:** fix in #940 *(since vNEXT)*, not in 1.0.0rc4: `branch create`
  exits 0 with `activated: false` and an `ACTIVE_BRANCH_NOT_STORED` warning;
  `branch reset` is a no-op; `branch use` is refused before any API call.

### 3.1 The last config of a dev-branch tree cannot be deleted

- **Symptom (<= 1.0.0rc4):** after `sync pull --branch <dev>`, deleting the folder
  of the only config in that branch's tree and running
  `sync push --branch <dev> --force` returns `no_changes`, deletes nothing, exits
  0, and lists the config under `orphaned[]` with reason
  `tracked_on_other_branch`. With two or more configs it works.
- **Workaround:** delete it directly:
  `kbagent --json config delete --project __env__ --component-id C --config-id ID --branch <dev>`
  (a soft delete into the trash; `config restore` undoes it).
- **Status:** fix in #938 *(since vNEXT)*, not in 1.0.0rc4.

### 3.2 Deleting a config with rows reports row errors

- **Symptom (<= 0.100.4):** `sync push --force` deleted the config, then sent a
  DELETE for each of its rows, which answered 404: each row landed in
  `errors[]` and the push exited 1, although the delete worked.
- **Workaround (<= 0.100.4):** after such a push, confirm with
  `config detail` / `sync diff` that the config is gone and ignore the row errors.
- **Status:** fixed *(since vNEXT)* by #933 -- already in 1.0.0rc4: one DELETE
  for the config covers its rows (`covered_rows` on the change).

### 3.3 A plaintext secret in source control creates a new version on every push

- **Symptom (all versions):** a `#secret: plaintext` value differs from the
  remote `KBC::ProjectSecure...` ciphertext, so the config is `modified` on every
  run, push encrypts it again and the config gets a new version each time.
- **Workaround:** commit the ciphertext instead of the plaintext. Get it once
  with `kbagent --json encrypt values --project __env__ --component-id C --input '{"#password": "..."}'`
  (see [encrypt-workflow.md](encrypt-workflow.md)), or take it from the
  `_config.yml` that `sync push` rewrites after it encrypts. A value that starts
  with `KBC::` is sent unchanged, and the diff treats any two encrypted values as
  equal, so the config stays unchanged. The cipher is bound to the project and
  the component: it works in every branch of that project. Because encrypted
  values compare equal, a new ciphertext alone is not a change -- to rotate a
  secret, push the new plaintext once, then commit its ciphertext.
- **Status:** not changed.

### 3.4 Row descriptions and `_description.md`

- **Symptom (<= 1.0.0rc4):** a row `_config.yml` without `description:` diffs as
  "description removed" on every push. When a folder has `_description.md`, it
  silently wins over `description:` in `_config.yml`.
- **Workaround:** write `description: ''` in every row `_config.yml`, and keep
  the description in one place only (`_description.md` or inline).
- **Status:** fix in #941 *(since vNEXT)*, not in 1.0.0rc4: a missing description
  equals `''`, and a different inline description next to `_description.md`
  adds a `DESCRIPTION_SHADOWED` warning (the file still wins).

### 3.5 Flow schedules live in their own folder

- **Symptom (all versions):** `sync pull` writes a `keboola.scheduler` config to
  `<branch folder>/other/keboola.scheduler/<name>/`, not under the flow it
  triggers. The `schedulerConfig` naming template in the manifest is not used.
  `sync push` writes the schedule config to Storage only; nothing in `sync`
  calls the Scheduler Service activation that `flow schedule` makes.
- **Workaround:** keep schedule configs in that folder in the repository; after
  a push that adds or changes a schedule, run
  `kbagent --json flow schedule --project __env__ --flow-id ID --cron ... --branch <dev>`
  for it and check `activated` (pitfall 2.5).
- **Status:** not changed.

### 3.6 `config new` scaffolds a `storage:` key that push drops

- **Symptom (<= 1.0.0rc4):** `kbagent config new` writes the table mappings as a
  top-level `storage: {input, output}` block. Sync push drops it (pitfall 2.3),
  so the mappings never reach Keboola.
- **Workaround:** move `storage.input` / `storage.output` to top-level `input:` /
  `output:` before the push.
- **Status:** fix in #941 *(since vNEXT)*, not in 1.0.0rc4: the scaffold writes
  top-level `input:` / `output:`; an old scaffold fails with
  `SYNC_LOCAL_FILE_INVALID`.

### 4.1 `data-app create --branch` fails with 422

- **Symptom (<= 1.0.0rc4):** `data-app create --branch <dev>` fails with HTTP 422:
  kbagent sends `branchId` as a number.
- **Workaround:** create the app in the default branch.
- **Status:** fix pending in #937 (not merged yet).

### 4.2 `add-column` returns before the column exists

- **Symptom (<= 1.0.0rc4):** `storage add-column` returns as soon as Storage
  accepts the request, but the add is an asynchronous Storage job. A
  `storage table-detail` right after it can miss the new column, and
  `add-column --not-null` on a table with rows reported success although the job
  failed and the column was never created.
- **Workaround:** after `add-column`, re-read `storage table-detail` (same
  `--branch`) until the column is listed, and fail the pipeline if it never
  appears.
- **Status:** fix in #939 *(since vNEXT)*, not in 1.0.0rc4: `add-column` waits
  for the job; a failed job is `STORAGE_JOB_FAILED` (exit 1), a timeout
  `STORAGE_JOB_TIMEOUT` (exit 4, `retryable: false`, the job keeps running).

### 4.3 Sync never deploys, stops or deletes a data app

- **Symptom (all versions):** `sync push` of a new `keboola.data-apps` config
  creates the app and its configuration but never deploys it; a changed config is
  not redeployed. `sync push --force` and `config delete` delete only the
  Storage configuration; kbagent does not call the Data Science API there, so the
  app is not stopped or deleted by it.
- **Workaround:** manage the app lifecycle with `data-app create`,
  `data-app deploy` and `data-app delete`
  ([data-app-workflow.md](data-app-workflow.md)).
- **Status:** not changed.

### 5.1 Every push has the same version message

- **Symptom (all versions):** `sync push` writes the change description
  `Updated via kbagent sync push`; there is no option to set it.
- **Workaround:** for traceability, write the commit SHA to the config's
  metadata after the push:
  `kbagent --json config set-metadata --project __env__ --component-id C --config-id ID --key ci.commitSha --value "$GITHUB_SHA" --branch <dev>`
  (see [config-metadata-workflow.md](config-metadata-workflow.md); key rules in
  5.6).
- **Status:** not changed.

### 5.2 `sync push --dry-run` has two result shapes

- **Symptom (all versions):** with something to push, `--dry-run` returns
  `status: "dry_run"` with `changes[]`, `summary` and `errors[]`. With nothing to
  push it returns `status: "no_changes"` with the real push's shape (`created`,
  `updated`, `deleted`, `errors`; no `changes`, no `summary`). A real push
  returns `status: "pushed"` with `pushed_details[]`.
- **Workaround:** branch on `data.status` before reading other keys.

### 5.3 Optional keys appear only when not empty

- **Symptom (all versions):** in `sync push` results, `skipped` (a count),
  `skipped_reason`, `skipped_deletions`, `skipped_deletions_reason`,
  `refused_deletions`, `orphaned`, `never_fetched`, `warnings`,
  `companion_warnings`, `name_drift_warnings` and the move keys (`moves`,
  `ambiguous_moves`, `identity_conflicts`) are present only when they have
  content. `skipped_deletions[].path` (and the `path` of every `deleted` change)
  is always `""`.
- **Workaround:** default each list to `[]` and `skipped` to `0`. Identify a
  deletion by `component_id` + `config_id` (for a row: `config_id` is the row id,
  `parent_config_id` its config), and use `config_name` for display.

### 5.4 `kbagent --json --version` is not JSON

- Use `kbagent --json version` (section 1).

### 5.5 Error messages contain the masked token

- **Symptom (all versions):** HTTP errors (invalid token, access denied,
  connection failures) name the token in masked form:
  `(token: <prefix>-...<last4>)` -- the part before the first `-` (the project
  id of a Storage token) and the last 4 characters.
- **Workaround:** do not treat CI logs as token-free; keep them private. The full
  token is never printed.

### 5.6 Metadata key rules and branch metadata permissions

- **Config metadata keys** may contain only `a-z`, `A-Z`, `0-9`, `_`, `.` and
  `-`. A key with `#`, `[`, `]`, `/`, `:` or a space is rejected by Storage with
  HTTP 400 (kbagent passes the key through unchanged). Use keys like
  `ci.commitSha`.
- **`branch metadata-set`** needs a token that may change dev branches: an admin
  (master) token, or a token with the `canManageDevBranches` permission. Other
  tokens are refused.
