# Migrating to kbagent 1.0

> **Draft.** Most 1.0.0 changes are not implemented yet. This page grows as each 1.0.0 change merges to `main`; until then, a row describes the planned behavior.

kbagent 1.0.0 makes the command-line interface consistent: one way to select the target project, one JSON document on stdout, one exit-code table, one confirmation rule. Some of these changes break scripts, agents and REST clients that depend on the 0.x behavior. This page tells you when the change reaches you, how to move early or stay on 0.x, and what to change. The plan and the reasons are in [#791](https://github.com/keboola/cli/issues/791).

## What happens and when

Two points in time control the migration:

- **T0** is the moment the GitHub release `v1.0.0` is published.
- **M**, the migration time, is T0 plus 28 days: the `published_at` value of the GitHub release `v1.0.0` plus 28 × 86,400 seconds, in UTC.

The dates are not known yet. When `v1.0.0` is published, the real dates appear in two places:

- the release page: <https://github.com/keboola/cli/releases/tag/v1.0.0> (its publication time is T0);
- the deprecation notice of kbagent 0.x (see [The notice](#the-notice)), which prints T0 and M in UTC.

kbagent reads M from GitHub and never from a hardcoded date. It takes `published_at` from `GET /repos/keboola/cli/releases/tags/v1.0.0`, not `created_at` and not the tag date. "Now" is the `Date` header of the GitHub response, so a wrong local clock does not move M. If the release is deleted after T0, kbagent holds the migration and the notice says that the date is unknown. If the release is published again with a later `published_at`, the 28 days start again.

### Who gets which version

| Install | Before T0 | From T0 to M | From M |
|---|---|---|---|
| New install on any channel (`install.sh`, `/kbagent:setup`, PyPI, Homebrew, apt/rpm, Chocolatey, winget) | 0.x | 1.x | 1.x |
| Manual upgrade (`uv tool upgrade`, `brew upgrade`, `apt upgrade`, `choco upgrade`, `winget upgrade`) | 0.x | 1.x | 1.x |
| CI pipeline that installs `keboola-cli` without a version | 0.x | 1.x (a new install on every run) | 1.x |
| Existing install that updates through kbagent (startup auto-update, `kbagent update`) | 0.x | 0.x, with the deprecation notice; 1.x only with `kbagent update --major` | 1.x |
| Existing install with `KBAGENT_AUTO_UPDATE=false`, or a native package held by its package manager (`brew pin`, `apt-mark hold`, ...) | 0.x | 0.x; `kbagent version` and `kbagent doctor` show the notice | 0.x until you upgrade |
| Existing native package (Homebrew, apt/rpm, Chocolatey, winget, zip binary) | 0.x | 1.x on the next upgrade by the package manager; the notice names its command | 1.x on the next upgrade |

`kbagent update --major` exists from kbagent 0.100.0 on (see [The notice](#the-notice)); older 0.x releases do not have the option.

An unpinned CI pipeline installs kbagent again on every run, so it never shows the notice. It gets 1.x at T0. If the pipeline must stay on 0.x, pin `keboola-cli<1` for the install and set `KBAGENT_AUTO_UPDATE=false` for the run (see [Stay on 0.x](#stay-on-0x)).

### The notice

kbagent 0.100.0 adds the notice and the major-version gate to the updater. It is published at least 14 days before T0. Older 0.x releases install any newer version, also a new major version, so update to 0.100.0 or later before T0.

From the moment kbagent 0.100.0 or later sees a published, stable `v1.0.0`:

- In human mode it prints one line on stderr once per 24 hours, and on every run in the last 72 hours before M. `kbagent version`, `kbagent update`, `kbagent doctor` and `kbagent context` always show it.
- Under `--json` it adds an entry to the top-level `warnings[]` of the success and the error envelope, on every run:

```json
{
  "code": "DEPRECATED_VERSION",
  "message": "kbagent 0.N is deprecated. ...",
  "hint": "kbagent update --major",
  "current_version": "0.N.x",
  "next_version": "1.0.0",
  "released_at": "<T0 in UTC>",
  "migration_at": "<M in UTC>",
  "url": "https://github.com/keboola/cli/blob/main/docs/migrate-to-1.0.md"
}
```

The texts below are examples; the final wording can change. The notice has three forms:

| Install | Notice |
|---|---|
| Auto-update on (the default) | "This installation updates itself to 1.x on M." |
| `KBAGENT_AUTO_UPDATE=false` | "This installation stays on 0.x; run kbagent update --major." Only in `kbagent version` and `kbagent doctor`, because the startup check is off. |
| Native package (frozen binary) | "Your package manager installs 1.x now:" followed by the upgrade command of that channel. |

When the check for a new version fails (offline, proxy error, HTTP 403 or 429), kbagent installs nothing. The notice then uses the cached date if there is one; otherwise there is no notice and no migration.

### At M

- The updater of kbagent 0.100.0 and later installs 1.x for the next run. The current command finishes on 0.x; no 0.x command line runs again under 1.x. A running `kbagent serve` holds the update until it stops; from M the notice tells you to restart it.
- A client older than 0.100.0 follows the release that GitHub marks as latest. That release stays on 0.x until M, so these clients also move at M, within one hour, but without a notice.
- After the automatic update from 0.x, 1.x refuses the first write or destructive command once, with the error `UPDATED_ACROSS_MAJOR` (exit 6) that links this page. Until that command, every read runs with a `warnings[]` entry `UPDATED_ACROSS_MAJOR`, also in later runs. The protection has no time limit. After the refused command, every command runs normally. An update that you start with `kbagent update --major` does not refuse. Admin commands (for example `project add`, `init`, `serve`, `update`) are not refused, and the REST routes of `kbagent serve` are not covered.
- The post-update banner lists the `BREAKING:` bullets of every version that the update crossed. Under `--json`, the first command after the update adds a `warnings[]` entry `UPDATED_ACROSS_BREAKING`.

### Pre-releases

No 1.x pre-release is published until kbagent 0.100.0 has been out for 14 days. A client older than 0.100.0 that runs `kbagent version --beta` or `kbagent update --beta` can still install a 1.x pre-release. Update to 0.100.0 or later first; it never crosses a major version with `--beta` alone.

## Move to 1.0 early

From T0, on an install that updates through kbagent (`install.sh`, `/kbagent:setup`, `uv tool`):

```bash
kbagent update --major   # kbagent 0.100.0 and later
```

`kbagent update` without `--major` stays on 0.x and prints the notice. On a native package, use the upgrade command of the package manager (see [Install channels](#install-channels)).

Before you move, check the [changes in 1.0.0](#changes-in-100) against your scripts. Two forms work on 0.x and on 1.0, so you can change a script before you move:

- Give `--project` explicitly, or give `--all-projects` when you want all projects. `--all-projects` on the 18 fan-out reads is added in a 0.x release before T0; this page names the version when it ships.
- Read `warnings[]` in the envelope when it is present. The key is added in a 0.x release before T0.

## Stay on 0.x

Pick the method that matches how kbagent gets to the machine:

- **Install that updates itself** (`install.sh`, `/kbagent:setup`, `uv tool`). Turn off the startup auto-update. Set it in your shell profile, or in the CI job environment, because an `export` lasts only for one shell:

  ```bash
  export KBAGENT_AUTO_UPDATE=false
  ```

  kbagent then stays on the installed version, also after M. `kbagent version` and `kbagent doctor` still show the notice.

  A version pin alone does not keep this install on 0.x. The self-update replaces the `uv tool` install with the exact wheel of the new version and keeps no pin, so a pinned install that updates itself moves to 1.x at M.
- **CI pipeline.** Do both: pin the version for the install and turn off the auto-update for the run.

  ```bash
  uv tool install --force 'keboola-cli[server]<1'
  export KBAGENT_AUTO_UPDATE=false
  ```

  In a `requirements.txt` or `pyproject.toml`, use `keboola-cli<1`. The pin controls only what the install puts on the machine; `KBAGENT_AUTO_UPDATE=false` stops the self-update. The pipeline generators of the plugin accept a version.
- **Native package** (Homebrew, apt/rpm, Chocolatey, winget, zip binary). A native package does not update itself, so a hold in the package manager keeps it on 0.x (see [Install channels](#install-channels)).

0.x gets security and data-loss fixes only until M, from the `release/0.N` branch. After M, 0.x gets no fixes.

## Go back to 0.x

If 1.x breaks something that you cannot change at once, install the last 0.x release again and stop the auto-update:

```bash
uv tool install --force 'keboola-cli[server]<1'
export KBAGENT_AUTO_UPDATE=false
```

Set `KBAGENT_AUTO_UPDATE=false` in your shell profile or in the CI job environment. At or after M, a 0.x install with auto-update on moves to 1.x again on its next run. For native packages, see [Install channels](#install-channels).

The `config.json` fields keep their names in 1.0.0. 1.x keeps its update check in its own cache file (`version_cache_v1.json`), so a 0.x client does not read a 1.x version from it.

## Changes in 1.0.0

Every change below has a `BREAKING:` bullet in the 1.0.0 changelog. The number is the row number in the [#791](https://github.com/keboola/cli/issues/791) proposal.

"Who is affected": **scripts** are shell scripts and CI steps that read the human output or the exit code; **agents** are callers that use `--json`; **REST** means clients of `kbagent serve`.

| # | Old form (0.x) | New form (1.0.0) | How you see it | Who is affected |
|---|---|---|---|---|
| 27 | An omitted `--project` on the 18 fan-out reads reads all registered projects | An omitted `--project` uses the default-project chain (`KBAGENT_PROJECT` > active project > sole project); all projects need `--all-projects` | `targets[].project_source`; `CONFIG_ERROR` (exit 5) when no project resolves | scripts, agents |
| 15 | A fan-out read exits 0 when one project failed; the failure is only in `data.errors` | Exits 1 when a project failed; exits 5 when no project is registered | exit code; `exit_code` in the envelope | scripts, agents |
| 12 | A bare group (`kbagent storage`) exits 0 and prints nothing; `kbagent --json` without a command prints help on stdout and exits 0 | Exit 2, help on stderr | `USAGE_ERROR` envelope | scripts, agents |
| 25 | A bad input value can end in a Python traceback with exit 1 | Usage error with exit 2 that names the option | `INVALID_ARGUMENT` | scripts, agents |
| 19 | A single-value option given twice: the last value wins, with no message | Usage error (exit 2) on write and destructive commands; a warning on the other commands | `USAGE_ERROR` naming the option | scripts, agents |
| 23 | 9 list options take only one comma-separated value (`org setup --project-ids`, `sharing share --target-project-ids`, `sharing share --target-users`, 6 `semantic-layer` options); a repeated option keeps only the last value | The options are repeatable and still accept a comma list | the target list in `data` | scripts, agents |
| 24 | A free-text option stores a literal `@<existing file>` (`--text` of `project description-set`, `storage describe-bucket`, `storage describe-table`, `branch metadata-set`; `transformation create --sql`) | Refused with exit 2 and a hint that names the file option | `INVALID_ARGUMENT` | scripts, agents |
| 16 | Human mode without a terminal: a confirmation reads the answer from stdin (`echo y \| kbagent ...`) | Refused unless `--yes`; no change under `--json` | `CONFIRMATION_REQUIRED` (exit 2) naming `--yes` | scripts |
| 17 | `branch delete` and `workspace delete` delete without a confirmation | Both ask on a terminal and take `--yes` (1.0.0 form: `kbagent branch delete --branch ID --yes`) | the prompt, or the row 16 refusal | scripts |
| 20 | `stream delete --force` skips the confirmation; `semantic-layer import --yes` has no effect | `stream delete --yes`; `semantic-layer import` without `--yes`. The old flags keep working as hidden aliases until at least M + 28 days; the first minor release after that date can remove them | `DEPRECATED` entry in `warnings[]` with `replacement` and `remove_after` | scripts, agents |
| 13 | `kai` and `semantic-layer token` report `CONFIG_ERROR` for other errors; `INVALID_ARGUMENT`, `USAGE_ERROR`, `MISSING_PARAMETER` and `FILE_NOT_FOUND` have no explicit `error_type` | Typed error codes; an explicit `error_type` for the 4 codes | `error.code`, `error.error_type` | agents |
| 21 | 4 paths use an exit code that does not match their error code: `transformation` `VALIDATION_ERROR` (1), `lineage` `FILE_NOT_FOUND` (1), `flow` `NOT_FOUND` (4), `http` `CONFIG_ERROR` (2) | The exit code from the table in [error-codes.md](error-codes.md) (its exit-code column is added in 1.0.0) | exit code | scripts, agents |
| 30 | `auth login` prints its human-mode notices on stdout | Notices on stderr; results stay on stdout | notices on stderr | scripts |
| 28 | – | After an automatic update from 0.x, the first write or destructive command is refused once. Reads before it run with a warning. Then every command runs. Admin commands, REST routes of `kbagent serve` and an update started with `kbagent update --major` are not refused | `UPDATED_ACROSS_MAJOR` (exit 6) that links this page. On a read, a `warnings[]` entry with the same code | scripts, agents (unattended jobs that run at M) |
| 18 | REST: 26 routes use production when `branch_id` is omitted | The routes use the active branch, as the CLI does; send `branch_id: 0` for production | `Kbagent-Targets` header; `targets` in REST errors | REST |
| 26 | REST: 4 partial-update routes use `PUT` (see [REST API](#rest-api)) | `PATCH`; the `PUT` form keeps working until at least M + 28 days; the first minor release after that date can remove it | `Kbagent-Warnings` header; after the removal, HTTP 405 naming `PATCH` | REST |

1.0.0 renames no command and no option. The only new hidden aliases are those of row 20.

The 18 fan-out reads of row 27 and row 15: `search`, `billing credits`, `component list`, `config list`, `config search`, `data-app list`, `job list`, `storage buckets`, `storage tables`, `sharing list`, `sharing edges`, `flow list`, `schedule list`, `schedule find`, `notification list`, `branch list`, `workspace list` and `project status`.

### Already changed in 0.x

These changes fixed a bug, a security gap or a data-loss path, so they shipped in 0.x at once:

- 0.96.1: `sync push` deletes remote configurations and rows only with `--force` (row 1); `sync push --force` is destructive under a deny policy (row 2).
- 0.98.0: a command that processes several items exits 1 after a failed item, also in `--dry-run` (row 3).
- 0.99.0: the permission policy applies to every REST route (row 2).
- 0.100.0:
  - The deny policies hold for `http *`, `search`, `token refresh`, `workspace password` and `config variables-set --replace` (row 2).
  - `workspace gc` needs `--project` or `--all-projects`, in the CLI and over REST, and deletes nothing in a project whose configuration listing failed (row 4).
  - `sync pull`, `sync diff` and `sync push` refuse a sync directory of another project with `CONFIG_ERROR` (row 5).
  - Removing the active project, and `init --project` that leaves it out, clear the active project (row 7).
  - `auth` commands take the stack from `KBAGENT_PROJECT` before the active project, with a sole-project fallback (row 8).
  - `storage delete-bucket` and `storage file-delete` read `--yes` and ask on a terminal without it (row 9); `storage delete-bucket --force` does not delete a shared bucket (row 10).
  - A mutating REST route rejects an undeclared `dry_run` with HTTP 422 (row 11).
  - A second `flow delete` of a flow in the trash answers `already_in_trash` and sends no DELETE; `job run --idempotency-key` refuses a replay that finds a pending entry; `merge-request merge` after a lost response returns `already_merged` (row 14).

### Changes in 0.100.0 for the move to 1.0

- `kbagent update` stays on the running major version unless `--major` is given (row 29).
- The deprecation notice, and the `DEPRECATED_VERSION` entry in `warnings[]` under `--json`.
- `kbagent version --beta` no longer writes the startup cache.

## Envelope and exit codes

Under `--json`, 1.0.0 keeps every key of the 0.x envelope and adds keys. No key is removed or renamed.

- stdout holds exactly one JSON document for every exit code, also for a parse error, a bare group and a crash. An explicit `--help` or `-h` still prints help on stdout with exit 0.
- The success envelope adds `exit_code` (the process exit code) and `schema_version` (`"1.0"`; an added key raises the minor number). The error envelope adds `schema_version`; it has no `exit_code`.
- `warnings[]` holds `{code, message, hint}` entries. It is present only when it is not empty. Treat `code` as an open list: new codes can appear in a minor release.
- Each record in `targets` adds `project_source` next to `branch_source`. Values: `explicit`, `env`, `active_project`, `sole`, `all`. Treat an unknown value like `active_project`.
- A destructive command whose project or branch comes from the active choice (`project use`, `branch use`) adds the warning `TARGET_FROM_ACTIVE_CHOICE` with the explicit flag as the hint.
- Planned, not settled yet: `--json`, `--deny-writes` and `--deny-destructive` are also accepted after the subcommand. Planned 1.0.0 form: `kbagent config list --project prod --json`. Until this page confirms it, put these flags before the subcommand.
- `status: "ok"` with a non-zero `exit_code` means a negative answer (`flow validate`, `auth status`, `permissions check`, `data-app validate-repo`) or a failed item or project.

Example: `storage delete-table` under `--json` in 1.0.0, with `--project prod` and active branch 456:

```json
{
  "status": "ok",
  "schema_version": "1.0",
  "exit_code": 0,
  "targets": [
    {
      "role": "target",
      "project_alias": "prod",
      "project_source": "explicit",
      "branch_id": 456,
      "branch_name": "feature-x",
      "branch_source": "active_branch",
      "active_branch": {"branch_id": 456, "branch_name": "feature-x"}
    }
  ],
  "warnings": [
    {
      "code": "TARGET_FROM_ACTIVE_CHOICE",
      "message": "Branch 456 comes from 'kbagent branch use', not from the command line.",
      "hint": "--branch 456"
    }
  ],
  "data": {"deleted": ["in.c-main.orders"], "failed": [], "dry_run": false, "project_alias": "prod"}
}
```

Exit codes in 1.0.0: 0 success; 1 general or partial failure; 2 usage, validation or refused confirmation; 3 authentication; 4 network or "still running"; 5 configuration; 6 policy denial or refused random-code confirmation; 7 job terminated after timeout; 130 interrupted. From 1.0.0, [error-codes.md](error-codes.md) is the one list of error codes with their exit codes; today it lists the codes without them. The changed exit codes are rows 12, 15, 16, 19, 21, 24 and 25 above.

## Changed defaults

### Target selection

- On the 18 fan-out reads, an omitted `--project` no longer means all projects (row 27). The command uses the same chain as every other command: `--project` > `KBAGENT_PROJECT` > active project > sole registered project > `CONFIG_ERROR`. For all projects, give `--all-projects`. 1.0.0 form: `kbagent config list --all-projects`.
- A fan-out read exits 1 when one project failed (row 15). One expired token in a registered project no longer fails a read that does not use that project.
- A write or destructive command never reads or writes several projects without `--all-projects`.

### Consent

- `--yes` and `--json` still mean consent; a destructive command under `--json` does not ask.
- `branch delete` and `workspace delete` ask before they delete, and take `--yes` (row 17).
- `stream delete --force` becomes `stream delete --yes` (row 20).

### Terminal

- In human mode without a terminal, a confirmation refuses with `CONFIRMATION_REQUIRED` unless `--yes` is given (row 16). A pipe no longer answers a prompt. The hint tells an AI agent to ask the user before it retries with `--yes`.
- `auth login` prints its notices on stderr (row 30).

## REST API

For clients of `kbagent serve`:

- **Active branch (row 18).** 26 routes use the active branch of the configuration directory when `branch_id` is omitted, as the CLI does. Send `branch_id: 0` for production. The web UI sends `branch_id: 0` in its production view.
- **`PATCH` for partial updates (row 26).** These routes move from `PUT` to `PATCH`:
  - `PUT /semantic-layer/items/{kind}/{name}`
  - `PUT /merge-requests/{project}/{merge_request_id}`
  - `PUT /data-apps/{project}/{app_id}/secrets`
  - the merge of `PUT /configs/{project}/{component_id}/{config_id}/variables`. This `PUT` keeps the full replace with `replace: true`. A `PUT` without `replace: true` keeps merging with a warning until at least M + 28 days; the first minor release after that date can refuse it. It never replaces silently.

  The old `PUT` form keeps working with a `Kbagent-Warnings` entry until at least M + 28 days. The first minor release after that date can remove it; from then on it answers HTTP 405 that names `PATCH`.
- **Response headers.** A success response carries the `targets` record in a `Kbagent-Targets` header and the warnings in a `Kbagent-Warnings` header, both as JSON. The error body carries `targets` and `warnings` as keys, and the same fields as the CLI error object (`error_type`, `project`, `retryable`).
- Already in 0.x: the permission policy on every route (0.99.0), the 422 for an undeclared `dry_run` and `workspace gc` that needs a project (see [Already changed in 0.x](#already-changed-in-0x)).

## SDK

The in-process SDK (`from keboola_agent_cli import Client`, see [sdk.md](sdk.md)) follows semantic versioning: everything in `keboola_agent_cli.__all__` is public API. The SDK does not read `config.json` and takes no `--project`, so the target-selection and consent changes do not apply to it.

The SDK ships in the same `keboola-cli` package, so 1.0.0 is also a new major version of the SDK. This section lists every SDK change in 1.0.0; none is known yet. Until this section confirms it, keep `keboola-cli<1` in your dependencies.

## Plugin

For the Claude Code plugin from `keboola/ai-kit` and the agents that use it:

- From T0 to M, the plugin documents use only forms that work on 0.x and on 1.x: an explicit `--project` or `--all-projects`, and flags that 1.x keeps as hidden aliases.
- At M, the plugin documents change to the 1.x forms.
- The version gate of the plugin tells the agent: when `kbagent --version` reports 0.x, ask the user before it runs `kbagent update --major` (0.100.0 and later).
- The deprecated marketplace entry in this repository (`.claude-plugin/marketplace.json`) is removed or pinned to the last 0.x tag before the 1.x documents reach `main`.

## Install channels

| Channel | 1.0 arrives | Stay on 0.x | Go back to 0.x |
|---|---|---|---|
| `install.sh`, `/kbagent:setup`, `uv tool` | new install: at T0; existing install: at M, or at once with `kbagent update --major` (0.100.0 and later) | `KBAGENT_AUTO_UPDATE=false` (a version pin alone does not stop the self-update) | `uv tool install --force 'keboola-cli[server]<1'` and `KBAGENT_AUTO_UPDATE=false` |
| PyPI (`pip`, `uv pip`, CI) | at T0 for an unpinned install | `keboola-cli<1` for the install and `KBAGENT_AUTO_UPDATE=false` for the run | install `keboola-cli<1` and set `KBAGENT_AUTO_UPDATE=false` |
| Homebrew (`keboola/keboola-cli2` tap) | on the next `brew upgrade` from T0 | `brew pin keboola-cli2` | the tap holds only the newest formula: use the zip binary of the last 0.x release, or the `uv tool` install |
| apt (`keboola-cli2`) | on the next `apt upgrade` from T0 | `sudo apt-mark hold keboola-cli2` | `sudo apt-get install keboola-cli2=<version>` when the repository still lists that version, then hold it |
| dnf/yum (`keboola-cli2`) | on the next upgrade from T0 | `sudo dnf versionlock add keboola-cli2` (needs the versionlock plugin) | `sudo dnf downgrade keboola-cli2-<version>` when the repository still lists that version, then lock it |
| Chocolatey (`keboola-cli2`) | after the package passes moderation | `choco pin add --name=keboola-cli2` | `choco upgrade keboola-cli2 --version <version> --allow-downgrade`, then pin it |
| winget (`Keboola.KeboolaCLI2`) | after the winget-pkgs pull request merges | `winget pin add --id Keboola.KeboolaCLI2` | uninstall first (`winget uninstall --id Keboola.KeboolaCLI2`), then `winget install --id Keboola.KeboolaCLI2 --version <version>`, then pin it |
| zip binary | when you download a new one | keep the binary you have | download the zip of the last 0.x release from the [releases page](https://github.com/keboola/cli/releases) |

Native packages and the zip binary do not update themselves, so a hold in the package manager keeps them on 0.x. Their notice names the upgrade command of the channel.
