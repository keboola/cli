# kbagent command-line interface specification

This document is binding for every change to the kbagent command-line interface and to the REST API of `kbagent serve`. A pull request that adds or changes a command, an option, a JSON key, an exit code or a route follows these rules. `make cli-spec-check` checks the rules that a machine can check; the reviewer checks the others.

The rules come from the proposal in [#791](https://github.com/keboola/cli/issues/791). The proposal keeps the research and the reasons in full; this document keeps the rules.

## Contents

- [How to read this document](#how-to-read-this-document)
- [Rule index](#rule-index)
- [1. Terms and invariants](#1-terms-and-invariants)
- [2. Naming](#2-naming)
- [3. Arguments and options](#3-arguments-and-options)
- [4. Input](#4-input)
- [5. Target selection](#5-target-selection)
- [6. Machine output](#6-machine-output)
- [7. Errors and exit codes](#7-errors-and-exit-codes)
- [8. Safety](#8-safety)
- [9. Repeatability](#9-repeatability)
- [10. Lists](#10-lists)
- [11. Help and the command manifest](#11-help-and-the-command-manifest)
- [12. The serve REST API](#12-the-serve-rest-api)
- [13. Change and deprecation policy](#13-change-and-deprecation-policy)
- [14. Human output](#14-human-output)
- [15. Secrets](#15-secrets)
- [16. Agent-facing documents](#16-agent-facing-documents)
- [17. Long-running operations](#17-long-running-operations)
- [18. Environment variables and global options](#18-environment-variables-and-global-options)
- [19. Versions and updates](#19-versions-and-updates)

## How to read this document

Each rule has an ID (`A1`, `T5`, …), a level, the version from which it applies, and how it is checked.

- **MUST**: a change that breaks the rule is not merged. **MUST for new commands** (or new options) binds new code; existing code that breaks the rule is listed in the baseline. **SHOULD**: the default; a deviation needs a reason in the pull request.
- **Applies** says from which version the rule holds:
  - **now**: the code on `main` follows the rule, and new code must follow it.
  - **now for new commands** (or new options, new routes, new text): new code must follow the rule now; existing code changes in the version that the rule names.
  - **0.x**: the change ships in a 0.x release and is not on `main` yet.
  - **0.100.0**: the change ships in kbagent 0.100.0, the 0.x release with the major gate, at least 14 days before T0.
  - **before T0**: the change ships before the GitHub release `v1.0.0` is published.
  - **1.0.0**: the change ships in 1.0.0.

  When a rule describes behavior that is not on `main` yet, the rule also says what happens until then.
- **Checked by**: a rule id of `make cli-spec-check` (for example `output.no-direct-print`), a named test, or **review**. A rule id is **report only** when the check prints its count but never fails. A rule id is **deferred** when the check does not run yet; `python scripts/check_cli_spec.py --list-rules` prints each rule id, its kind and what checks it.
- **Anchors**: every spec ID and every rule id of the check has an anchor, so `docs/cli-interface-spec.md#A1` and `docs/cli-interface-spec.md#output.no-direct-print` both work. The failure lines of the check link to these anchors.

The baseline `scripts/cli_spec_baseline.json` lists the existing violations of each rule id and a `blocking` flag per rule id. A new violation of a blocking rule fails CI, whatever the level of the spec rule behind it: a check that serves a SHOULD rule fails the same way as one that serves a MUST rule. A fix removes its line from the baseline (`make cli-spec-baseline`). On a pull request the number of entries per blocking rule can only shrink against the base branch; a rename keeps the count. A reviewed deviation goes into `exceptions` with a reason, where the diff shows it; CI accepts a new exception only when a reviewer has added the label `cli-spec-exception` to the pull request.

A rule changes only through a pull request that changes this document. When a new rule id lands in the check, its spec rule names it in the same pull request.

## Rule index

Every rule with its level, the version from which it applies, and how it is checked. The rule text is in its section; `tests/test_check_cli_spec.py` keeps this table equal to the rule headings.

| ID | Level | Applies | Checked by |
|---|---|---|---|
| [N1](#N1) | MUST | now | `naming.kebab-case` |
| [N2](#N2) | MUST for new commands | now | `naming.verb-vocabulary`, review |
| [N3](#N3) | SHOULD for new compound leaves | now | review |
| [N4](#N4) | MUST | now | `naming.alias-parity`, `naming.hidden-leaf-has-canonical` |
| [N5](#N5) | MUST | now for new text; 1.0.0 for existing text | review |
| [A1](#A1) | MUST | now | `args.global-option-one-shape`, `args.long-option-one-shape`, review |
| [A2](#A2) | MUST | now | `args.short-flag-one-meaning`, `args.no-global-option-shadow` |
| [A3](#A3) | MUST for new commands | now | `args.no-positional-arguments`, `args.no-bare-id-option` |
| [A4](#A4) | MUST | now for new options; 1.0.0 for existing ones | `args.list-option-repeatable` |
| [A5](#A5) | MUST | now | `args.no-dead-parameter` |
| [A6](#A6) | MUST | 1.0.0 | `args.repeated-single-value` |
| [A7](#A7) | SHOULD for new options | now | review |
| [I1](#I1) | MUST | now | `input.single-parser` |
| [I2](#I2) | MUST | now | `input.file-option-vocabulary` |
| [I3](#I3) | MUST | 1.0.0 | `input.content-option-refuses-at-file` |
| [I4](#I4) | MUST | 1.0.0 | `output.one-json-document`, review |
| [I5](#I5) | SHOULD for new options | now | review |
| [I6](#I6) | SHOULD for new options | now | review |
| [T1](#T1) | MUST | now | review, unit tests |
| [T2](#T2) | MUST | 1.0.0 | `target.project-help-states-default` |
| [T3](#T3) | MUST | now | `target.no-write-fanout` |
| [T4](#T4) | MUST | 0.x | review |
| [T5](#T5) | MUST | now | `tests/test_effective_branch.py` |
| [T6](#T6) | MUST | now; `project_source` from 1.0.0 | `tests/test_effective_branch.py`, `target.targets-reported` |
| [T7](#T7) | withdrawn | – | – |
| [T8](#T8) | MUST | now | review, unit tests |
| [T9](#T9) | MUST | now | review, unit tests |
| [T10](#T10) | MUST | now | review, unit tests |
| [T11](#T11) | SHOULD | now | `target.project-help-states-default`, review |
| [T12](#T12) | MUST | 1.0.0 | `target.targets-reported` |
| [T13](#T13) | SHOULD | now | review |
| [T14](#T14) | MUST | now | review, unit tests |
| [T15](#T15) | MUST | now | review |
| [T16](#T16) | MUST | now | `tests/test_project_ref.py`, review, unit tests |
| [O1](#O1) | MUST | 1.0.0 | `output.one-json-document`, `output.bare-group-exits-2` |
| [O2](#O2) | MUST | `warnings` 0.x; `exit_code` and `schema_version` 1.0.0 | `output.envelope-schema` |
| [O3](#O3) | MUST | 0.x | `output.envelope-schema`, `output.no-warning-after-output` |
| [O4](#O4) | MUST | 1.0.0 | `output.ok-then-exit-declares-exit-code` |
| [O5](#O5) | MUST | now | review |
| [O6](#O6) | MUST | now | `safety.confirm-via-helper`, `output.no-direct-print`, review |
| [O7](#O7) | SHOULD | 1.0.0 | `output.root-flags-anywhere` |
| [O8](#O8) | MUST | now | review |
| [E1](#E1) | MUST | 1.0.0 | `errors.code-has-type-and-exit` |
| [E2](#E2) | MUST | now; the single list: not yet | review |
| [E3](#E3) | MUST | now | `errors.forward-retryable`, `errors.config-error-keeps-code`, `tests/test_error_forwarding.py` |
| [E4](#E4) | MUST | now for writes; 1.0.0 for fan-out reads | `errors.fanout-exit`, unit tests |
| [E5](#E5) | SHOULD | now | review |
| [E6](#E6) | MUST | now | review |
| [E7](#E7) | MUST | now | review |
| [S1](#S1) | MUST | now | `safety.destructive-verb-category`, review |
| [S2](#S2) | MUST | now | `safety.destructive-has-dry-run`, `safety.destructive-has-yes` |
| [S3](#S3) | MUST | now; the flag changes in 1.0.0 | `safety.force-meaning-registered`, `safety.yes-has-short-y` |
| [S4](#S4) | MUST | now; the no-terminal refusal from 1.0.0 | `safety.confirm-via-helper`, `safety.no-tty-confirmation` |
| [S5](#S5) | MUST | now | `tests/test_effective_branch.py`, review |
| [S6](#S6) | MUST | now | `enforcement.runtime-permission-guard`, `enforcement.permission-literal-matches-mount`, `serve.route-has-permission-key` |
| [S7](#S7) | MUST | now | review |
| [S8](#S8) | MUST | now | review |
| [R1](#R1) | MUST | now | review |
| [R2](#R2) | MUST | now | review, unit tests |
| [R3](#R3) | MUST | now | review, unit tests |
| [R4](#R4) | SHOULD | now | review |
| [R5](#R5) | MUST | now | `tests/test_job_idempotency_store.py`, review, unit tests |
| [L1](#L1) | note | – | – |
| [L2](#L2) | MUST | now for new commands; existing lists after 1.0.0 | review |
| [L3](#L3) | MUST | now for new commands; existing commands after 1.0.0 | review, unit tests |
| [L4](#L4) | SHOULD for new commands | now | review |
| [L5](#L5) | SHOULD | now | `lists.list-has-limit` |
| [L6](#L6) | SHOULD for new options | now | review |
| [H1](#H1) | MUST | now | `help.command-has-help`, `help.param-has-help` |
| [H2](#H2) | MUST | now | `help.claude-md-flags-exist` |
| [H3](#H3) | MUST | now | `help.doc-examples-parse`, `tests/test_doc_examples.py` |
| [H4](#H4) | SHOULD | now | review |
| [H5](#H5) | MUST | 1.0.0 | `help.dash-h` |
| [H6](#H6) | MUST | now | `help.manifest-fresh` |
| [H7](#H7) | SHOULD for new commands | now | review |
| [V1](#V1) | MUST | now | `serve.route-has-permission-key`, `serve.route-permission`, `tests/test_cli_contract.py` |
| [V2](#V2) | MUST | 1.0.0 | `serve.targets-reported` |
| [V3](#V3) | MUST | 1.0.0 | `serve.targets-reported` |
| [V4](#V4) | MUST | 1.0.0 | review |
| [V5](#V5) | MUST | now | `serve.dry-run-parity`, `serve.undeclared-dry-run-rejected`, `tests/test_server_dry_run_guard.py` |
| [V6](#V6) | MUST | now | review |
| [V7](#V7) | SHOULD | now | `serve.command-has-route`, `serve.map-names-live-command` |
| [V8](#V8) | MUST | 1.0.0 | review |
| [V9](#V9) | MUST | now for new routes; 1.0.0 for existing ones | `serve.partial-update-uses-patch` |
| [C1](#C1) | MUST | now | review |
| [C2](#C2) | MUST | now | `change.breaking-bullet-on-removal` |
| [C3](#C3) | MUST | now | `change.expired-deprecation` |
| [C4](#C4) | MUST | now | review |
| [C5](#C5) | MUST | 1.0.0 | review |
| [C6](#C6) | MUST | now | review |
| [C7](#C7) | MUST | now | `tests/test_check_cli_spec.py` |
| [C8](#C8) | MUST | now | review |
| [C9](#C9) | MUST | 0.100.0 | updater contract tests |
| [HO1](#HO1) | MUST | now | `output.no-direct-print` |
| [HO2](#HO2) | MUST | now for new commands; 1.0.0 for existing ones | `output.multi-item-headline`, review |
| [HO3](#HO3) | MUST | now | review |
| [HO4](#HO4) | SHOULD | now | `output.no-direct-print` |
| [HO5](#HO5) | MUST | now for `config oauth-url`; `auth login` when `--no-open` is added | review |
| [HO6](#HO6) | MUST | now | `output.no-direct-print` |
| [HO7](#HO7) | MUST | now | `output.human-snapshots`, `tests/test_cli_human_snapshots.py` |
| [SEC1](#SEC1) | MUST | now | review |
| [SEC2](#SEC2) | MUST | now | review |
| [SEC3](#SEC3) | MUST | now | review |
| [SEC4](#SEC4) | MUST | now | unit tests |
| [SEC5](#SEC5) | MUST | now | review |
| [AG1](#AG1) | MUST | 1.0.0 | `help.context-from-manifest` |
| [AG2](#AG2) | MUST | now | `help.doc-examples-parse` |
| [AG3](#AG3) | MUST | now | `scripts/check_version_gates.py` |
| [LR1](#LR1) | MUST | now | `args.global-option-one-shape`, review |
| [LR2](#LR2) | MUST | now | review |
| [LR3](#LR3) | SHOULD | now | review |
| [EV1](#EV1) | MUST | now | `env.registry-complete`, review |
| [EV2](#EV2) | MUST | now | review |
| [EV3](#EV3) | MUST | now | review |
| [EV4](#EV4) | SHOULD | 1.0.0 | `output.root-flags-anywhere` |
| [UP1](#UP1) | MUST | 0.100.0 | updater contract tests |
| [UP2](#UP2) | MUST | 0.100.0 | updater contract tests |
| [UP3](#UP3) | MUST | 0.100.0 | updater contract tests |
| [UP4](#UP4) | MUST | 0.100.0 | unit tests |
| [UP5](#UP5) | MUST | before T0 | review |

## 1. Terms and invariants

Every rule serves at least one of these invariants.

- <a id="INV-1"></a>**INV-1 No silent target choice.** Every result names the project and the branch it acted on, and where each came from. From 1.0.0, no command widens to several projects without `--all-projects`.
- <a id="INV-2"></a>**INV-2 No silent consent.** Consent to a destructive effect is `--yes`, `--json` (a documented consent), or a human answer at a terminal. From 1.0.0, a pipe never answers a prompt. A flag that deletes or drops data raises the command to `destructive`.
- <a id="INV-3"></a>**INV-3 No hidden failure.** A command exits 1 when anything that it did failed, reads included. With `--json`, stdout holds one JSON document for every exit code. Warnings reach `warnings[]`. Until 1.0.0, fan-out reads exit 0 when a project failed (E4), and some errors under `--json` print no JSON (O1).
- <a id="INV-4"></a>**INV-4 One meaning per option.** One long name has one meaning and one shape. `--yes` only skips a confirmation. `--force` only overrides a guard that its help names. A repeated single-value option is reported (from 1.0.0).

Terms:

- **Active project** and **active branch**: the choice that `project use` and `branch use` save. JSON values use `active_project` and `active_branch`. The `config.json` fields `default_project` and `active_branch_id` keep their names, because older kbagent versions read the same file.
- **Source values** of a target: `explicit` (a flag), `env` (`KBAGENT_PROJECT`), `active_project` or `active_branch` (the saved choice), `sole` (the only registered project), `all` (fan-out), `production`, `git_mapping`, `manifest`, `merge_request`, and `none` (the command chose no branch). `project_source`, `branch_source` and `warnings[].code` are open enums: an agent treats an unknown source like `active_project`.
- **Categories**: `read`, `write`, `destructive` and `admin`, from `OPERATION_REGISTRY` in `permissions.py`. A category is a static property of the command, so a rule that depends on it can run before any network call. An unknown operation is `write` (fail-closed).
- **Fan-out reads**: the 18 reads that can read several projects: `search`, `billing credits`, `component list`, `config list`, `config search`, `data-app list`, `job list`, `storage buckets`, `storage tables`, `sharing list`, `sharing edges`, `flow list`, `schedule list`, `schedule find`, `notification list`, `branch list`, `workspace list` and `project status`.
- **T0** is the time when the GitHub release `v1.0.0` is published. **M**, the migration instant, is its `published_at` plus 28 days (28 × 86,400 seconds), in UTC.

What stays as it is:

- The resolvers: `BaseService.resolve_pinned_alias` for single-project commands, `BaseService.resolve_projects` for explicit and fan-out lists, and `effective_branch.resolve_branch` for the branch.
- `OPERATION_REGISTRY`, `FLAG_ESCALATIONS`, and the fail-closed default `write`.
- The envelope `{status, data}` and `{status, error}` on stdout under `--json`, plus `targets`. A new key is added; no key is removed or renamed.
- `project use` and `branch use`. Registering the first project sets the active project and says so.
- `--yes` or `--json` means consent. The merge-request rule that requires an explicit target under `--json` stays.
- A command with a required `--project` keeps it required.
- The exit numbers 0–7 and 130.

## 2. Naming

<a id="N1"></a><a id="naming.kebab-case"></a>
**N1 · MUST · now.** Command segments and long options are lowercase kebab-case.

- Checked by: `naming.kebab-case`.

<a id="N2"></a><a id="naming.verb-vocabulary"></a>
**N2 · MUST for new commands · now.** Use these verbs: `detail` reads one object; `get` and `set` read or write one attribute; `list` lists; `create` and `delete` create and destroy a server object; `update` changes one; `add` and `remove` register something locally, or attach and detach a relationship; `new` generates local files. `describe` never names a write. Existing outliers (the five `storage describe-*` writes, `flow new`, `config get-metadata`) keep their names.

- Checked by: `naming.verb-vocabulary` (deferred: the verb vocabulary file is not reviewed yet).

<a id="N3"></a>
**N3 · SHOULD for new compound leaves · now.** A compound leaf puts the noun first and the verb second (`metadata-get`), the order that most commands use.

- Checked by: review.

<a id="N4"></a><a id="naming.alias-parity"></a><a id="naming.hidden-leaf-has-canonical"></a>
**N4 · MUST · now.** An alias is hidden. It is listed in one `COMMAND_ALIASES` map (alias path → canonical path), and the permission check uses the canonical key. A hidden alias group (`mr`, `sl`) has the same leaves and options as its visible group. Until the first hidden leaf alias lands: no hidden leaf exists, the map is empty, and it lives only in `scripts/check_cli_spec.py`; `permissions.py` and `check_cli_permission` do not read it. Before the first hidden leaf alias lands, the map moves into `permissions.py` and the permission check reads it.

- Why: a hidden leaf without a map entry falls back to category `write` and passes `--deny-destructive`.
- Checked by: `naming.alias-parity`, `naming.hidden-leaf-has-canonical`.

<a id="N5"></a>
**N5 · MUST · now for new text; 1.0.0 for existing text.** Help texts, messages, `--json` keys and REST summaries call the saved choice of project and branch "active" ("active project", "active branch"). New text uses "active" now. The existing texts that say "pin" or "default project" (for example the help of `project use`) change in 1.0.0. The `project current` keys `pinned` and `source: "pin"` and the `config.json` field names stay.

- Checked by: review.

## 3. Arguments and options

<a id="A1"></a><a id="args.global-option-one-shape"></a><a id="args.long-option-one-shape"></a>
**A1 · MUST · now.** A global option name has one meaning and one shape in the whole tree. The global names are listed in the reviewed registry `GLOBAL_OPTIONS` (`--project`, `--branch`, `--all-projects`, `--yes`, `--force`, `--dry-run`, `--limit`, `--wait`, `--timeout`, `--json`, …). Every other long name has one meaning and one shape inside its command group. The shape is the multiplicity and the value kind (text, identifier, integer, JSON, path, flag). A `click.Choice` is text with fixed values, so it fits a text option of the same name. The multiplicity of `--project` follows T2, not this rule. `--scope`, `--target-project`, `--type` and `--job-id` keep their names and their meanings per group.

- Checked by: `args.global-option-one-shape` (deferred: the `GLOBAL_OPTIONS` registry is not reviewed yet); `args.long-option-one-shape` (report only).

<a id="A2"></a><a id="args.short-flag-one-meaning"></a><a id="args.no-global-option-shadow"></a>
**A2 · MUST · now.** One short flag maps to one long option. The map is the `SHORT_FLAGS` registry in `scripts/check_cli_spec.py`, and a new short flag needs an entry there. `-p` means `--project` only. A command option does not reuse the name of a global option. `-p` is not added to the existing `--project` options that lack it; new commands get it through a shared option object in `commands/_options.py`. Until `commands/_options.py` exists, a new command declares `-p` itself. The short flags `-c`, `-n` and `-r` are reserved: their existing uses stay (each is an `exceptions` entry in the baseline), and no new use is added with either meaning.

- Checked by: `args.short-flag-one-meaning`, `args.no-global-option-shadow`.

| Short flag | Long option | Status |
|---|---|---|
| `-V` | `--version` | global |
| `-j` | `--json` | global |
| `-v` | `--verbose` | global |
| `-a` | `--allow` | fixed |
| `-c` | `--category` or `--column` | reserved |
| `-d` | `--directory` | fixed |
| `-e` | `--email` | fixed |
| `-f` | `--feature` | fixed |
| `-i` | `--ignore-case` | fixed |
| `-l` | `--load` | fixed |
| `-m` | `--message` | fixed |
| `-n` | `--limit` or `--name` | reserved |
| `-o` | `--output` | fixed |
| `-p` | `--project` | fixed |
| `-q` | `--query` | fixed |
| `-r` | `--regex` or `--role` | reserved |
| `-t` | `--type` | fixed |
| `-y` | `--yes` | fixed |

"Fixed" entries follow the majority use when the check was introduced (`SHORT_FLAGS`); the other uses of a fixed flag are in the baseline. "Reserved" entries (`RESERVED_SHORT_FLAGS`) have tied uses: `-c` is `--category` once and `--column` once; `-n` is `--limit` twice and `--name` twice; `-r` is `--regex` twice and `--role` twice. A new use of a reserved flag fails the check, with either meaning.

<a id="A3"></a><a id="args.no-positional-arguments"></a><a id="args.no-bare-id-option"></a>
**A3 · MUST for new commands · now.** An identifier is an option `--<noun>-id`, with one name per identifier: `--config-id` for any configuration, `--component-id`, `--project`, `--identity`. Existing positional identifiers stay accepted.

- Checked by: `args.no-positional-arguments`, `args.no-bare-id-option` (both report only).

<a id="A4"></a><a id="args.list-option-repeatable"></a>
**A4 · MUST · now for new options; 1.0.0 for existing ones.** A list-valued option is repeatable. An option whose values are IDs or fixed values (project IDs, table IDs, types) also accepts a comma list in one occurrence. An option with free-form values (JSON, `KEY=VALUE`, `PATH=VALUE`, SQL, text; for example `transformation edit --op`, `config update --set`, `config variables-set --var`) is repeat-only, because a comma can be part of the value. Until 1.0.0, the nine existing comma-only options (`org setup --project-ids`, `sharing share --target-project-ids` and `--target-users`, and six `semantic-layer` options) take one comma string.

- Checked by: `args.list-option-repeatable`.

<a id="A5"></a><a id="args.no-dead-parameter"></a>
**A5 · MUST · now.** A command body reads every parameter it declares.

- Checked by: `args.no-dead-parameter`.

<a id="A6"></a><a id="args.repeated-single-value"></a>
**A6 · MUST · 1.0.0.** A single-value option given twice is reported: a usage error (exit 2) on `write` and `destructive` commands, and a warning on the other commands. Until 1.0.0, the last value wins without a message.

- Why: on a write, the dropped value can be the intended target (`--project x … --project test` acts on `test`).
- Checked by: `args.repeated-single-value` (deferred: lands with the 1.0.0 command class).

<a id="A7"></a>
**A7 · SHOULD for new options · now.** Durations are seconds and data sizes are bytes, both integers without a unit suffix. Points in time are ISO 8601 with a time zone. Booleans are `--x/--no-x`. A fixed set of values, named sizes included, is a `click.Choice`. A fixed threshold in code (such as the 1 GB guard of `workspace load`) names its unit in the help and in the message.

- Checked by: review.

## 4. Input

<a id="I1"></a><a id="input.single-parser"></a>
**I1 · MUST · now.** A structured value (JSON or YAML) accepts inline text, `@path`, and `-` for stdin, with the behavior of `parse_json_arg` in `commands/_helpers.py`. New code calls it. The other copies of this parsing move to it when their command is changed.

- Checked by: `input.single-parser`.

<a id="I2"></a><a id="input.file-option-vocabulary"></a>
**I2 · MUST · now.** An option named `--file`, `--*-file` or `--from-file` that carries text or data takes a path or `-`, and never treats a mistyped path as content. Listed legacy: `flow new/update/validate --file` and `agent create/test --from-file` also accept inline content. A new file option is named `--<field>-file`, or `--file` when the file is the main input of the command. `--from-file` is not used for new options.

- Why: an inline fallback would store or upload a mistyped path as content.
- Checked by: `input.file-option-vocabulary` (report only).

<a id="I3"></a><a id="input.content-option-refuses-at-file"></a>
**I3 · MUST · 1.0.0.** A free-text content option refuses a value that is `@` followed by the path of an existing file, with exit 2 and a hint that names the file option. In 1.0.0 this covers the four commands that use `resolve_text_input` and `transformation create/edit --sql`; the other free-text options follow after 1.0.0. Until then, such a value is stored as text.

- Why: project and branch metadata keep no old value, so a wrong guess cannot be undone.
- Checked by: `input.content-option-refuses-at-file` (deferred: lands in 1.0.0).

<a id="I4"></a>
**I4 · MUST · 1.0.0.** A bad input value is a usage error (exit 2, `INVALID_ARGUMENT`) that names the option, never a traceback. Until 1.0.0, some bad values end in a traceback with exit 1.

- Checked by: `output.one-json-document` (crash case, deferred) and review.

<a id="I5"></a>
**I5 · SHOULD for new options · now.** A secret comes from a prompt, a `--*-stdin` flag, a file or an environment variable, never from an inline value.

- Checked by: review.

<a id="I6"></a>
**I6 · SHOULD for new options · now.** Standard input is `-` as the value of a file option (I2) or of a structured option (I1). A new command adds no `--stdin` flag. The four existing `--stdin` flags (`project description-set`, `storage describe-bucket`, `storage describe-table`, `branch metadata-set`) stay. `--*-stdin` for secrets (I5) stays.

- Checked by: review.

## 5. Target selection

<a id="T1"></a>
**T1 · MUST · now.** A single-project command resolves its project in this order: `--project` > `KBAGENT_PROJECT` > active project > sole registered project > `CONFIG_ERROR` (exit 5). It uses `resolve_pinned_alias`. A command with a required `--project` does not use this chain. The value of `--project` and `KBAGENT_PROJECT` is an alias or a project ID (T16).

- Checked by: review and unit tests.

<a id="T2"></a>
**T2 · MUST · 1.0.0.** The 18 fan-out reads use the T1 chain when no `--project` is given, like every other command. `--all-projects` reads all registered projects. Until 1.0.0, an omitted `--project` on these reads reads all registered projects.

- Why: with E4, an implicit read of all projects would fail because of one expired token in another project.
- Checked by: `target.project-help-states-default`, which checks only that the help states the class, not the behavior.

<a id="T3"></a><a id="target.no-write-fanout"></a>
**T3 · MUST · now.** A `write` or `destructive` command never reads or changes several projects implicitly. `workspace gc` requires `--project` or `--all-projects`, also in 1.0.0, and does not use the T1 chain.

- Checked by: `target.no-write-fanout`.

<a id="T4"></a>
**T4 · MUST · 0.x.** "All projects" is spelled `--all-projects`, and it conflicts with `--project`. The 18 fan-out reads get `--all-projects` in 0.x as an additive option, so one script works on 0.x and on 1.0; until 1.0.0 it behaves as an omitted `--project`. Today only `workspace gc` and `sync pull/diff/push` have it. `project refresh --all` is the one legacy spelling of "all projects"; it gets a hidden `--all-projects` alias. `auth register-projects --all` (every accessible project on the stack) and `semantic-layer schema --all` (every semantic type) keep their meanings.

- Checked by: review.

<a id="T5"></a>
**T5 · MUST · now.** A command that uses a branch resolves `--branch` > active branch > production through `effective_branch.resolve_branch`. `--branch 0` means production. Declared deviations that always use production and report the unused active branch: six storage reads (`buckets`, `bucket-detail`, `tables`, `table-detail`, `files`, `snapshots`), eight `data-app` commands, four `stream` commands and four `branch metadata-*` commands. `notification create` builds its job filter only from an explicit `--branch`, and its help says so. `sync` in git-branching mode resolves git mapping > active branch > manifest.

- Checked by: `tests/test_effective_branch.py`.

<a id="T6"></a><a id="target.targets-reported"></a>
**T6 · MUST · now; `project_source` from 1.0.0.** Every command that uses a project or a branch reports it in `targets`: in the success envelope, in the error envelope and in `--dry-run`, and in one `Target:` line on stderr in human mode. Each record carries `branch_source`. From 1.0.0 each record also carries `project_source`, and a command that chose no branch has `branch_source: "none"`.

- Why: an agent's `export` does not persist between its calls, so the saved active project and branch are the only target state that persists, and neither is on the command line that a human approves.
- Checked by: `tests/test_effective_branch.py`; `target.targets-reported` (deferred: lands with `project_source` in 1.0.0).

<a id="T7"></a>
**T7.** Withdrawn: from 1.0.0 an omitted `--project` no longer reads all projects (T2), so no fan-out warning is needed.

<a id="T8"></a>
**T8 · MUST · now.** kbagent sets the active project only in `project use` and when the first project is registered (`project add`, `org setup`, `auth` registration). It clears the active project when that project is removed, and does not move it to the next alias. A command that sets or clears it says so (`active_project_set`, `active_project_cleared`). `default_project_updated` of `project rename` stays as a frozen key.

- Checked by: review and unit tests.

<a id="T9"></a>
**T9 · MUST · now.** `auth` commands resolve the stack in this order: `--stack` alias > `--stack` URL > `KBAGENT_PROJECT` > active project > sole project > `CONFIG_ERROR`. With `--stack` given, `KBAGENT_PROJECT` is not read. A `KBAGENT_PROJECT` that names an unregistered alias is skipped with a warning in `auth login`, `login-password` and `register-projects`, and is `CONFIG_ERROR` in `auth logout` and `auth status`.

- Checked by: review and unit tests.

<a id="T10"></a>
**T10 · MUST · now.** A sync directory belongs to one project. `sync pull/diff/push` refuse a manifest whose project ID or `apiHost` differs from the project of `--project`. When the registered project has no `project_id`, the command compares `apiHost` and reads the project ID once from token verification. If that fails, it refuses with `CONFIG_ERROR` and a hint to replace the token with `kbagent project edit --token-stdin`.

- Checked by: review and unit tests.

<a id="T11"></a><a id="target.project-help-states-default"></a>
**T11 · SHOULD · now.** The help of `--project` and `--branch` states the class: required, the T1 chain, all projects, or production only. A command whose class depends on another option states both classes. Example: until 1.0.0 `component list` reads all projects without `--query` and uses the T1 chain with it; `config new` requires `--project` with `--push`.

- Checked by: `target.project-help-states-default` for `--project` only; review for `--branch`.

<a id="T12"></a>
**T12 · MUST · 1.0.0.** A `destructive` command under `--json` whose project comes from the active project, or whose branch comes from the active branch, adds a `warnings[]` entry `TARGET_FROM_ACTIVE_CHOICE` with a hint that names the explicit flag. The command still runs, because `--json` stays consent.

- Checked by: `target.targets-reported` (deferred).

<a id="T13"></a>
**T13 · SHOULD · now.** `project use` warns when `KBAGENT_PROJECT` overrides the choice being saved. `branch use` warns when `KBAGENT_PROJECT` names another project than `--project`, because commands on the T1 chain in that shell then use the active branch of the other project. The warning also goes into `warnings[]` once the envelope has it. Today `project use` prints the note in human mode and returns `env_override`.

- Checked by: review.

<a id="T14"></a>
**T14 · MUST · now.** The configuration directory resolves `--config-dir` > `KBAGENT_CONFIG_DIR` > the nearest `.kbagent/config.json` found by walking up from the working directory (the walk stops at the home directory) > the global directory. With `KBAGENT_PROJECT_FROM_ENV` set, `KBC_TOKEN` and `KBC_STORAGE_API_URL` add the in-memory project `__env__`. It is the active project only when no active project is saved on disk, and it is never written to disk. T1 then applies unchanged.

- Checked by: review and unit tests.

<a id="T15"></a>
**T15 · MUST · now.** A branch ID belongs to one project. An explicit `--branch` on a read that targets several projects (several `--project` values, `--all-projects`, or, until 1.0.0, an omitted `--project`) is `INVALID_ARGUMENT` (exit 2). Without `--branch`, each project uses its own branch by T5, and its record in `targets` names it.

- Checked by: review.

<a id="T16"></a>
**T16 · MUST · now.** An option that names a registered project takes its alias or its project ID: `--project` on every command, `KBAGENT_PROJECT`, `project use`, `sync clone --target`, `config clone --target-project`, `--stack` on the `auth` commands, the project options of `semantic-layer promote` and `semantic-layer diff`, and the project parameters in `kbagent serve` paths and queries. An alias wins; kbagent looks up the ID only when no alias matches. An alias that is also the ID of another registered project gives a warning on stderr. An ID that matches several registered projects is `CONFIG_ERROR` with the matching aliases, except when all matches are on one stack and exactly one of them has a session token. kbagent translates the ID to the alias once, before the command runs, and `targets` names the alias. An option whose value is a new alias (`project add --project`) takes no ID. A new option that names a target project that does not have to be registered takes a registered alias or a project ID, repeatable or as a comma list (A4). REST request bodies are not covered yet.

- Checked by: review and unit tests (`tests/test_project_ref.py`).

## 6. Machine output

<a id="O1"></a><a id="output.one-json-document"></a><a id="output.bare-group-exits-2"></a>
**O1 · MUST · 1.0.0.** With `--json`, stdout holds exactly one JSON document for every exit code, parse errors, bare groups and crashes included. `--stream` commands write NDJSON. Everything else goes to stderr. An explicit `--help` (and `-h`, H5) prints help on stdout with exit 0 in both modes. A bare group, and `--json` without a command, exit 2 with a `USAGE_ERROR` envelope. Until 1.0.0, a parse error, a crash or a bare group under `--json` gives an empty stdout.

- Checked by: `output.one-json-document`, `output.bare-group-exits-2` (both deferred: land with the 1.0.0 root command class).

<a id="O2"></a><a id="output.envelope-schema"></a>
**O2 · MUST · `warnings` 0.x; `exit_code` and `schema_version` 1.0.0.** The success envelope has `status: "ok"`, `data`, `exit_code`, `schema_version`, and `targets` and `warnings` when they are not empty. The error envelope has `status: "error"`, `error`, `schema_version`, and `targets` and `warnings` when they are not empty. `schema_version` is `"<major>.<minor>"`: an added key raises the minor; a removed or renamed key raises the major and follows C3. Today the envelope is `{status, data}` or `{status, error}`, plus `targets`.

- Checked by: `output.envelope-schema` (deferred: lands with the envelope).

<a id="O3"></a><a id="output.no-warning-after-output"></a>
**O3 · MUST · 0.x.** In JSON mode a warning is an entry `{code, message, hint?}` in `warnings[]`, emitted before the output. A `warning()` call after `output()` has written the document does nothing in JSON mode, so O1 holds. A deprecation uses `code: "DEPRECATED"` with `replacement` and `remove_after` (a date). Until `warnings[]` lands, `OutputFormatter.warning()` does nothing in JSON mode, and a command puts its warnings into `data`.

- Checked by: `output.envelope-schema`, `output.no-warning-after-output` (both deferred).

<a id="O4"></a><a id="output.ok-then-exit-declares-exit-code"></a>
**O4 · MUST · 1.0.0.** `exit_code` in the success envelope equals the process exit code. `status: "ok"` with a non-zero `exit_code` means a negative answer (`flow validate`, `auth status`, `permissions check`, `data-app validate-repo`), or a failed item or project of a command that processes several of them, reads included (E4). The error envelope has no `exit_code`; the process exit code and the exit-code table give it. Until 1.0.0, the envelope has no `exit_code`, and the process exit code is the only signal.

- Checked by: `output.ok-then-exit-declares-exit-code` (deferred: lands with the 1.0.0 envelope).

<a id="O5"></a>
**O5 · MUST · now.** Keys that kbagent adds are snake_case. Raw API objects pass through unchanged. The existing camelCase derived keys (`logTail`, `lastUsed*`) and the three project keys in `data` (`project_alias`, `alias`, `project`) are frozen; new payloads use `project_alias`.

- Checked by: review.

<a id="O6"></a>
**O6 · MUST · now.** In human mode, results go to stdout. Warnings, notices, hints, progress and prompts go to stderr.

- Checked by: `safety.confirm-via-helper` (prompts), `output.no-direct-print`, and review.

<a id="O7"></a><a id="output.root-flags-anywhere"></a>
**O7 · SHOULD · 1.0.0.** The root flags `--json`/`-j`, `--deny-writes` and `--deny-destructive` are also accepted after the subcommand. argv is not rewritten, because a token equal to `--json` can be the value of another option. Until 1.0.0, the root flags go before the subcommand.

- Checked by: `output.root-flags-anywhere` (deferred: lands with the 1.0.0 command class).

<a id="O8"></a>
**O8 · MUST · now.** A command that reads an existing secret that the user can also see in another place (a data app password, the secret in a stream endpoint) does not print it by default, in human mode or under `--json`. It shows a masked value, or it says where the secret went and where the user can see it (`data-app password`: `password_delivered_to` and `ui_url`). `--reveal` prints the secret. `--copy` puts it into the clipboard; `--copy` with `--reveal` is `INVALID_ARGUMENT`. A REST route returns the secret only with `reveal=true`. Reading the secret SHOULD be an operation of its own in `OPERATION_REGISTRY` (as `data-app.password`), so that a deny policy refuses it before any API call. The rule does not cover a command whose result is a new credential.

- Why: when an AI agent runs kbagent, the output goes into the model context and the chat history.
- Checked by: review.

Shape of the success envelope in 1.0.0 (`storage delete-table --project prod`, active branch 456):

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

## 7. Errors and exit codes

<a id="exit-codes"></a>
| Exit | Meaning |
|---|---|
| 0 | Success. Also a prompt declined at a terminal (E6). |
| 1 | General failure, or a partial failure: at least one item or project failed (E4). |
| 2 | Usage or validation error, or a refused confirmation (`CONFIRMATION_REQUIRED`, from 1.0.0). |
| 3 | Authentication error. |
| 4 | Network error, or a local wait that timed out while the server job keeps running. |
| 5 | Configuration error, for example no project resolves (`CONFIG_ERROR`). |
| 6 | Denied by a permission policy, or a refused random-code confirmation. |
| 7 | The job was terminated after its timeout (`JOB_TIMEOUT_TERMINATED`). |
| 130 | Interrupted (Ctrl-C). |

<a id="E1"></a><a id="errors.code-has-type-and-exit"></a>
**E1 · MUST · 1.0.0.** One table `ErrorCode → (error_type, exit_code)` in `errors.py` gives every code an explicit entry, and `map_error_to_exit_code` reads it. Until 1.0.0, `map_error_to_exit_code` in `commands/_helpers.py` maps codes with defaults, and four paths use an exit code that differs from the table above (`transformation` `VALIDATION_ERROR` 1, `lineage` `FILE_NOT_FOUND` 1, `flow` `NOT_FOUND` 4, `http` `CONFIG_ERROR` 2); they follow the table in 1.0.0.

- Checked by: `errors.code-has-type-and-exit` (deferred: lands with the table).

<a id="E2"></a>
**E2 · MUST · now; the single list: not yet.** Exit codes follow the table above, and a new error code gets its exit code from the table. Not yet: `docs/error-codes.md` gets `Exit` and `Type` columns and becomes the one list of codes, exit codes and types that other documents link to. Until then, this table is the list of exit codes.

- Checked by: review.

<a id="E3"></a><a id="errors.forward-retryable"></a><a id="errors.config-error-keeps-code"></a>
**E3 · MUST · now.** An error handler forwards `retryable` and `details` (use `emit_api_error` in `commands/_helpers.py`) and keeps a typed code: a `ConfigError` handler passes `getattr(exc, "error_code", ErrorCode.CONFIG_ERROR)`, not a fixed `CONFIG_ERROR`. `kai` and `semantic-layer token` get typed codes in 1.0.0.

- Why: `kbagent context` tells agents to retry when `retryable` is true.
- Checked by: `errors.forward-retryable`, `errors.config-error-keeps-code`, `tests/test_error_forwarding.py`.

<a id="E4"></a><a id="errors.fanout-exit"></a>
**E4 · MUST · now for writes; 1.0.0 for fan-out reads.** A command that processes several items or projects exits 1 when at least one of them failed, reads included. It prints the full payload before it exits, and the items that succeeded stay written. A `--dry-run` that reports a failed item exits 1 like the real run. A run with no failed item exits 0. A fan-out read with no registered project exits 5. An answer that is not a failure (`PAYG_NOT_AVAILABLE` in `billing credits`) does not count as failed. Use `item_failure_exit_code()` in `commands/_helpers.py`. Until 1.0.0, a fan-out read exits 0 when a project failed and lists the failure in `data.errors`.

- Checked by: `errors.fanout-exit` (deferred: lands with T2 in 1.0.0) and unit tests.

<a id="E5"></a>
**E5 · SHOULD · now.** A per-item failure entry keeps its existing keys (`error`, `message`, `reason`) and adds `error_code`, `retryable`, and `message` where it is missing.

- Checked by: review.

<a id="E6"></a>
**E6 · MUST · now.** A prompt declined at a terminal prints `Aborted.` and exits 0. Without a terminal, S4 applies.

- Checked by: review.

<a id="E7"></a>
**E7 · MUST · now.** An error message names the object, the reason and, when one exists, the next step as a command or a flag. Example: "Bucket in.c-main not found in project prod; run kbagent storage buckets --project prod", not "404".

- Checked by: review.

## 8. Safety

<a id="S1"></a><a id="safety.destructive-verb-category"></a>
**S1 · MUST · now.** Categories describe the effect on Keboola.

- `read` changes nothing.
- `write` creates or changes an object or a value.
- `destructive` deletes, drops or invalidates a remote object, value or credential (a soft delete into the trash, a password reset and a token refresh included), or makes a change that the CLI cannot undo (`--scope organization`).
- `admin` changes who can access a project or an organization and deletes nothing. A command with a destructive effect is `destructive` also when it needs admin rights.

A flag that raises the class is in `FLAG_ESCALATIONS` and is checked where the flag is read (`sync.push --force`, the `--scope organization` entries, `config.variables-set --replace`). `token.refresh` and `workspace.password` are `destructive`. Declared deviation: the full load of `storage upload-table` and `storage load-file` replaces data and stays `write`; the help and the `--dry-run` output say that it replaces data. Categories and deny policies describe effects on Keboola, not on local files: `sync pull --theirs` stays `read`, its help names the effect, and `--dry-run` shows it.

- Checked by: review; `safety.destructive-verb-category` (report only).

<a id="S2"></a><a id="safety.destructive-has-dry-run"></a><a id="safety.destructive-has-yes"></a><a id="safety.write-has-dry-run"></a>
**S2 · MUST · now.** A `destructive` command has `--dry-run` and `--yes`. Recorded exceptions, each with its reason in the baseline: `config delete` (soft delete; `config restore` undoes it), `dev-portal deprecate` (a random code on a terminal), `http delete` (raw client; the `http` permission check applies), and the merge-request transitions (an explicit target under `--json`). `branch delete` and `workspace delete` get `--yes`, a read-only `--dry-run` and a confirmation in 1.0.0. `safety.write-has-dry-run` (report only) counts `write` commands without `--dry-run`; it measures, and no rule requires it.

- Checked by: `safety.destructive-has-dry-run`, `safety.destructive-has-yes`.

<a id="S3"></a><a id="safety.force-meaning-registered"></a><a id="safety.yes-has-short-y"></a>
**S3 · MUST · now; the flag changes in 1.0.0.** `--yes` only skips a confirmation, and it has `-y`. `--force` only overrides a named guard, and its help names every guard that it overrides. Every `--force` has a `FORCE_MEANINGS` entry in `scripts/check_cli_spec.py` that names its guards, and a new `--force` needs one. A guard may ask on a terminal (the size guard of `workspace load`); `--force` then answers that guard only. `storage delete-bucket --force` overrides only the guard for a non-empty bucket and never deletes a shared bucket. From 1.0.0, `stream delete --force` is a hidden alias of `--yes` (in 0.x it skips the general confirmation), and `semantic-layer import --yes` is hidden (it has no effect in any version). Both keep working with a `DEPRECATED` warning until at least M + 28 days (C3). `sync pull --force` (pull again) and `project refresh --force` (refresh valid tokens too) stay as registered legacy meanings.

- Checked by: `safety.force-meaning-registered`, `safety.yes-has-short-y`.

<a id="S4"></a><a id="safety.confirm-via-helper"></a><a id="safety.no-tty-confirmation"></a>
**S4 · MUST · now; the no-terminal refusal from 1.0.0.** With `--yes` or `--json` there is no prompt. In human mode on a terminal, the prompt goes to stderr. A safety prompt goes through the shared confirmation helper `_confirm_or_abort` in `commands/_helpers.py` (which `--yes` and `--json` skip), not through a direct `typer.confirm`. In human mode without a terminal (stdin or stderr is not a terminal), the command refuses with `CONFIRMATION_REQUIRED` (exit 2). The message names `--yes` and adds "AI agents: ask the user before you retry with `--yes`". 0.x releases read the answer from stdin instead. The preview-then-confirm flows of `org setup` and `project refresh` show their preview and then call the helper. The `exceptions` in the baseline are the two non-safety questions (`init` copies the global projects, `auth login` registers projects) and the size guard of `workspace load` (S3), which asks on a terminal only and refuses without one. Two guards in the service layer still ask through a `typer.confirm` that the command passes in: `semantic-layer add metric` (a dataset that is not in the model) and `semantic-layer edit metric` (a rename that changes constraints). They refuse without a terminal with `VALIDATION_ERROR` (exit 2), and on a terminal they ask also under `--json`. The check does not find them, because the command does not call `typer.confirm` itself.

- Checked by: `safety.confirm-via-helper` (only `_confirm_or_abort` in `commands/_helpers.py` calls `typer.confirm`) and `safety.no-tty-confirmation` (contract case in `tests/test_cli_contract.py`).

<a id="S5"></a>
**S5 · MUST · now.** `--dry-run` makes no mutating call, returns `dry_run: true` and reports the target in `targets`. It SHOULD read the affected resources when one GET is enough. The eight existing echo-only destructive previews stay as they are, because a read would turn the preview of a missing object into a failure.

- Checked by: `tests/test_effective_branch.py` (targets in `--dry-run`) and review.

<a id="S6"></a><a id="enforcement.runtime-permission-guard"></a><a id="enforcement.permission-literal-matches-mount"></a>
**S6 · MUST · now.** The permission check runs for every leaf command and every REST route. A group callback checks the key of its own mount path. The `permissions.*` commands are always allowed, so that a policy cannot lock the user out.

- Checked by: `enforcement.runtime-permission-guard`, `enforcement.permission-literal-matches-mount`, `serve.route-has-permission-key`.

<a id="S7"></a>
**S7 · MUST · now.** A session under a deny policy cannot preview a denied command with `--dry-run`: the check sees only the command name, before the options are parsed.

- Checked by: review.

<a id="S8"></a>
**S8 · MUST · now.** An option that widens the effect of a command (scope, all projects, cascade, recursion) defaults to the narrowest value. A wider value is explicit, and when it cannot be undone it raises the class (S1).

- Why: `rls create` without a scope created an organization-wide policy in review.
- Checked by: review.

## 9. Repeatability

<a id="R1"></a>
**R1 · MUST · now.** Each mutating command is classified as `converges` (a repeat changes nothing), `guarded` (`--if-not-exists`, locate-first, an idempotency key) or `duplicates`. The class data does not exist yet. Until it does, the pull request description states the class of each new or changed mutating command.

- Checked by: review.

<a id="R2"></a>
**R2 · MUST · now.** A delete of an object that goes to the trash locates the object first. A repeat answers `already_in_trash` with exit 0 and sends no second DELETE (`config delete`, `flow delete`). A delete of an object without a trash answers `NOT_FOUND` on a repeat.

- Checked by: review and unit tests.

<a id="R3"></a>
**R3 · MUST · now.** POST and PATCH are not repeated after a 5xx or a read timeout. PUT and DELETE are repeated, unless the endpoint is a state transition or changes meaning after success (`retry_safe=False`). When `merge-request merge` gets `MR_NOT_READY_TO_MERGE`, it reads the merge request: if it is merged, the command runs the local cleanup and returns `already_merged` with exit 0.

- Checked by: review and unit tests.

<a id="R4"></a>
**R4 · SHOULD · now.** A create guard is named `--if-not-exists`. It is added only where the API reports a name conflict (not `workspace create`, whose names are not unique).

- Checked by: review.

<a id="R5"></a>
**R5 · MUST · now.** `--idempotency-key` records a pending entry before the create. A definite failure of the create (a 4xx, a validation or permission error, a refused connection or a connect timeout) removes the entry. An unknown outcome (a read timeout, a 5xx, exhausted retries) keeps it; `errors.request_outcome_unknown` classifies the outcome. A replay that finds a pending entry refuses instead of creating again, with a hint that names `kbagent job list` and `--force-rerun`. `POST /jobs/{project}/run` accepts `idempotency_key`.

- Checked by: review and unit tests (`tests/test_job_idempotency_store.py`).

## 10. Lists

<a id="L1"></a>
**L1 · note.** A Typer `min`/`max` bound on `--limit` is acceptable. From 1.0.0 a rejected value gives a `USAGE_ERROR` envelope with exit 2 (O1). No new bound is added to a limit that is unbounded today.

<a id="L2"></a>
**L2 · MUST · now for new commands; existing lists after 1.0.0.** A limited list reports `limit`, `offset` when it has one, and `has_more`.

- Checked by: review.

<a id="L3"></a>
**L3 · MUST · now for new commands; existing commands after 1.0.0.** A command that selects items for a write or a delete reports `selection_limit_reached` when the limit cut the selection (for example `job terminate --status`).

- Checked by: review and unit tests.

<a id="L4"></a>
**L4 · SHOULD for new commands · now.** `--limit` is a total. `job list` and `search` keep their per-project limit, and their help says so.

- Checked by: review.

<a id="L5"></a><a id="lists.list-has-limit"></a>
**L5 · SHOULD · now.** Sort options are `--sort-by` and `--sort-order`. A list command backed by an API has `--limit` with a documented default.

- Checked by: `lists.list-has-limit` (report only).

<a id="L6"></a>
**L6 · SHOULD for new options · now.** A filter option is named after the field it matches (`--component-id`, `--config-id`, `--status`, `--type`). A free-text match is `--query`, with a `--regex` flag when a pattern is allowed. Several filters combine with AND. A filter takes one value, unless the API accepts several; then it follows A4.

- Checked by: review.

## 11. Help and the command manifest

<a id="H1"></a><a id="help.command-has-help"></a><a id="help.param-has-help"></a>
**H1 · MUST · now.** Every command and every visible option has help.

- Checked by: `help.command-has-help`, `help.param-has-help`.

<a id="H2"></a><a id="help.claude-md-flags-exist"></a>
**H2 · MUST · now.** Every flag in a command signature in `CLAUDE.md` exists on that command.

- Checked by: `help.claude-md-flags-exist`.

<a id="H3"></a><a id="help.doc-examples-parse"></a>
**H3 · MUST · now.** Every `kbagent …` example in the agent-facing documents parses against the command tree: `CLAUDE.md`, the output of `kbagent context`, and the Markdown files under `plugins/kbagent/`. An example of a command that does not exist on purpose is listed in the allow-list of the test.

- Checked by: `help.doc-examples-parse` (`tests/test_doc_examples.py`).

<a id="H4"></a>
**H4 · SHOULD · now.** `--help` shows no raw reStructuredText double backticks and no internal issue references.

- Checked by: review.

<a id="H5"></a><a id="help.dash-h"></a>
**H5 · MUST · 1.0.0.** `-h` works as `--help`. Until 1.0.0, only `--help` works.

- Checked by: `help.dash-h` (deferred: lands with the 1.0.0 root command class).

<a id="H6"></a><a id="help.manifest-fresh"></a>
**H6 · MUST · now.** The command manifest `docs/cli-manifest.json` (commands, options, kinds, help texts) is generated from the Typer tree (`make cli-manifest`) and committed, so that every option or help change shows in the pull-request diff. From 1.0.0, the command reference and the command part of `kbagent context` are generated from it (AG1).

- Checked by: `help.manifest-fresh`.

<a id="H7"></a>
**H7 · SHOULD for new commands · now.** The first help line says what the command does. The help of a `destructive` command says what it deletes and whether the trash or `config restore` can undo it. The help of an option states its default when omitting the option changes the target or the effect (T11 covers `--project` and `--branch`). A command with structured input shows one example.

- Checked by: review.

## 12. The serve REST API

<a id="V1"></a><a id="serve.route-has-permission-key"></a><a id="serve.route-permission"></a>
**V1 · MUST · now.** Each route resolves to one `OPERATION_REGISTRY` key in `server/route_permissions.py`, a central fail-closed table. The permission policy is checked on every route except `/health/*`, `/ui-config` and the documentation routes.

- Checked by: `serve.route-has-permission-key`; `serve.route-permission` (`tests/test_cli_contract.py`).

<a id="V2"></a>
**V2 · MUST · 1.0.0.** A route chooses the branch with the CLI rule T5: when `branch_id` is omitted, the service applies the active branch through `effective_branch.resolve_branch`. Until 1.0.0, the 26 routes that this changes do not apply the active branch. From 0.x the web UI sends `branch_id: 0` in its production view, so that the 1.0.0 change does not change what the UI labels as production.

- Checked by: `serve.targets-reported` (deferred).

<a id="V3"></a><a id="serve.targets-reported"></a>
**V3 · MUST · 1.0.0.** A success response carries the `targets` record in a `Kbagent-Targets` response header and the warnings in a `Kbagent-Warnings` header, both JSON, because REST success bodies are bare service dicts. The error body carries `targets` and `warnings` as keys.

- Checked by: `serve.targets-reported` (deferred: lands with the headers).

<a id="V4"></a>
**V4 · MUST · 1.0.0.** The REST error body has the same fields as the CLI error object (`error_type`, `project`, `retryable`).

- Checked by: review.

<a id="V5"></a><a id="serve.dry-run-parity"></a><a id="serve.undeclared-dry-run-rejected"></a>
**V5 · MUST · now.** A route offers the same `dry_run`, guard and `idempotency_key` parameters as its command. A mutating route rejects an undeclared `dry_run`, in the query or in the body, with HTTP 422. A new route rejects every undeclared parameter (`extra="forbid"` on its body model).

- Checked by: `serve.dry-run-parity`; `serve.undeclared-dry-run-rejected` (`tests/test_server_dry_run_guard.py`).

<a id="V6"></a>
**V6 · MUST · now.** REST has no confirmation: the request is the consent. A command whose confirmation needs a human at a terminal has no route.

- Checked by: review.

<a id="V7"></a><a id="serve.command-has-route"></a><a id="serve.map-names-live-command"></a>
**V7 · SHOULD · now.** Every command has a route, or a baseline exception with a reason (terminal-only commands, commands that a generic route serves). Every value in the serve command map names a live command.

- Checked by: `serve.command-has-route`, `serve.map-names-live-command`.

<a id="V8"></a>
**V8 · MUST · 1.0.0.** The routes that change the saved choice on disk (`POST /projects/use/{alias}`, `POST /branches/{project}/use`, `POST /branches/{project}/reset`) change it for every CLI call and every REST client that uses the same configuration directory. They stay, because the web UI uses them. Their summaries say that the change is shared, their responses carry `active_project_set` or `active_branch_set` (T8), and they add a `Kbagent-Warnings` entry that names the configuration directory.

- Checked by: review.

<a id="V9"></a><a id="serve.partial-update-uses-patch"></a>
**V9 · MUST · now for new routes; 1.0.0 for existing ones.** A route that changes only the fields in the request uses `PATCH`. A route that replaces the whole resource at its URL, or sets one value, uses `PUT`. The method that kbagent sends to a Keboola API is outside this rule. In 1.0.0 four existing routes move to `PATCH`: `PUT /semantic-layer/items/{kind}/{name}`, `PUT /merge-requests/{project}/{merge_request_id}`, `PUT /data-apps/{project}/{app_id}/secrets`, and the merge of `PUT /configs/{project}/{component_id}/{config_id}/variables`. That last `PUT` keeps only the full replace (`replace: true`); a `PUT` without it keeps merging with a `Kbagent-Warnings` entry until M + 28 days and is then refused with 405. It never replaces silently. The old `PUT` of the other three routes works the same way until M + 28 days.

- Checked by: `serve.partial-update-uses-patch`.

## 13. Change and deprecation policy

The 0.x line, until T0, ships only bug, security and data-loss fixes (C4), the major gate of the updater in 0.100.0 (section 19), and additive forms that let one script work on 0.x and on 1.0 (`warnings[]`, `--all-projects`). Every other interface change waits for 1.0.0. After M, `release/0.N` ships no more fixes.

<a id="C1"></a>
**C1 · MUST · now.** A change is **breaking** when it removes or renames a command, an option, a short flag, a positional form or an environment variable; adds a required option; changes a default; changes an exit code, an error code or an `error_type`; removes or renames a JSON key or an enum value; adds a refusal or a prompt; adds a denial under a permission policy; or changes a target (project or branch). A change is **additive** when it adds a command, an option or a JSON key, a value of an open enum (`project_source`, `branch_source`, `warnings[].code`), or a warning.

- Checked by: review.

<a id="C2"></a><a id="change.breaking-bullet-on-removal"></a>
**C2 · MUST · now.** Every breaking change has a `BREAKING:` changelog bullet with the migration.

- Checked by: `change.breaking-bullet-on-removal` (deferred: a release-PR check that compares the baseline and the manifest with the last tag).

<a id="C3"></a><a id="change.expired-deprecation"></a>
**C3 · MUST · now.** Inside a major version, a deprecated form keeps working for at least 28 days. It prints one stderr line in human mode and adds a `DEPRECATED` entry with `replacement` and `remove_after` (a date) to `warnings[]`. After that date, a minor release removes it. Until `warnings[]` is on `main`, the stderr line in human mode and a note in `data` are enough. A removed form stays in the `DEPRECATIONS` registry, so that its error names the replacement. For a behavior change, the final state is the new behavior or a refusal with a hint. The window is a date, not a number of releases.

- Checked by: `change.expired-deprecation` (deferred: the `DEPRECATIONS` registry in `commands/_helpers.py` has no `remove_after` date yet, because the window of the 1.0.0 hidden aliases starts at M).

<a id="C4"></a>
**C4 · MUST · now.** A change ships at once, with a `BREAKING:` bullet and no window, when it restores a documented contract (help, docstring, `CLAUDE.md`, `kbagent context`), fails closed on a severity-5 path ("a silent write or delete on an unexpected target, or data loss", as the proposal of #791 rates it), makes a deny policy hold, changes only a result that is already wrong, or removes a secret from the default output.

- Checked by: review.

<a id="C5"></a>
**C5 · MUST · 1.0.0.** After an update, the "What's new" banner on stderr lists the `BREAKING:` bullets of every version that the update crossed. Under `--json`, the first command after the update adds a `warnings[]` entry `UPDATED_ACROSS_BREAKING`. Until 1.0.0, the banner shows only the target version.

- Checked by: review.

<a id="C6"></a>
**C6 · MUST · now.** A renamed command or option follows C3; the old reviewer rule "alias kept for one cycle" is replaced by C3.

- Checked by: review.

<a id="C7"></a>
**C7 · MUST · now.** `CLAUDE.md` (Coding Conventions), `CONTRIBUTING.md` ("Checklist: Adding a New CLI Command") and the `kbagent-pr-reviewer` playbook name this document as binding for every interface change.

- Checked by: `tests/test_check_cli_spec.py` (every rule id of the check has an anchor here, and the three files link this document).

<a id="C8"></a>
**C8 · MUST · now.** The SDK (`keboola_agent_cli.__all__`) follows semantic versioning. A CLI change that changes a type in `result_models.py` or `lib.py` is reviewed as an SDK change. For the CLI and REST interfaces, a major version marks an interface generation. Between majors, a breaking change ships in a minor release after the C3 window, and a C4 fix ships at once. The "Versioning" table of `docs/error-codes.md` points to this rule.

- Checked by: review.

<a id="C9"></a>
**C9 · MUST · 0.100.0.** An installed client of major N moves to major N+1 by itself 28 days after the `published_at` of the GitHub release `vN+1.0.0`. The client reads the date from GitHub and never hardcodes it. New installs get the newest major at once. Tags keep the `v` prefix. Before 0.100.0, the updater installs any newer version.

- Checked by: updater contract tests.

## 14. Human output

<a id="HO1"></a><a id="output.no-direct-print"></a>
**HO1 · MUST · now.** Results go to stdout. Headlines, notices, hints, progress, prompts and panels go to stderr, also in human mode (O6). A command prints only through `OutputFormatter` and the shared renderers in `output.py`, not through `print`, `typer.echo`, `click.echo` or a new `rich` console. `auth login` prints its notices to stdout in human mode until 1.0.0.

- Checked by: `output.no-direct-print`.

<a id="HO2"></a><a id="output.multi-item-headline"></a>
**HO2 · MUST · now for new commands; 1.0.0 for existing ones.** A command that processes several items ends its human output with one headline: `Success:` when nothing failed, `Failed: N of M` when an item failed.

- Checked by: review today; `output.multi-item-headline` (deferred: lands with the headline renderer).

<a id="HO3"></a>
**HO3 · MUST · now.** No ANSI color when the stream is not a terminal, with `--no-color`, or with `NO_COLOR` set.

- Checked by: review.

<a id="HO4"></a>
**HO4 · SHOULD · now.** Panels and tables come from shared renderers in `output.py`. A new layout is added there, not in a command.

- Checked by: `output.no-direct-print`.

<a id="HO5"></a>
**HO5 · MUST · now for `config oauth-url`; `auth login` when `--no-open` is added.** A command whose purpose is a browser step (`auth login`, `config oauth-url`) opens the browser on a terminal, unless `--no-open` is given. Every other command opens a link only with `--open` (for example `data-app password --open`). No command opens a browser with `--json`, without a terminal, over SSH or in a container. Until `auth login` gets `--no-open`, it opens the browser where the environment allows, and `--device-code` avoids the browser.

- Checked by: review.

<a id="HO6"></a>
**HO6 · MUST · now.** Progress (`--progress`) and spinners write to stderr only.

- Checked by: `output.no-direct-print`.

<a id="HO7"></a><a id="output.human-snapshots"></a>
**HO7 · MUST · now.** The human output of one command per shared renderer (table, panel, headline, `Target:` line, notice) has a snapshot test. A change to a renderer shows as a snapshot diff in the pull request (`KBAGENT_UPDATE_SNAPSHOTS=1` records it), and a new renderer lands with its snapshot.

- Checked by: `output.human-snapshots` (`tests/test_cli_human_snapshots.py`).

## 15. Secrets

<a id="SEC1"></a>
**SEC1 · MUST · now.** Secret input follows I5: a prompt, `--*-stdin`, `--*-file` or `--*-env`, never inline. Existing inline secret options stay accepted.

- Checked by: review.

<a id="SEC2"></a>
**SEC2 · MUST · now.** Secret output follows O8.

- Checked by: review.

<a id="SEC3"></a>
**SEC3 · MUST · now.** `--dry-run` previews, diffs and `targets` never print a secret value. Encrypted `#` keys and known secret fields are masked.

- Checked by: review (a contract case with a secret in a dry-run fixture is planned).

<a id="SEC4"></a>
**SEC4 · MUST · now.** Local credential files (`config.json`, `auth.json`) keep file mode 0600.

- Checked by: unit tests.

<a id="SEC5"></a>
**SEC5 · MUST · now.** A secret never goes to logs, telemetry or error messages. Tokens are masked with `mask_token()`.

- Checked by: review.

## 16. Agent-facing documents

<a id="AG1"></a><a id="help.context-from-manifest"></a>
**AG1 · MUST · 1.0.0.** The command part of `kbagent context` is generated from the command manifest (H6); the hand-written part keeps only workflow guidance. `kbagent context <group>` prints one group. Until 1.0.0, `AGENT_CONTEXT` in `commands/context.py` is written by hand.

- Checked by: `help.context-from-manifest` (deferred).

<a id="AG2"></a>
**AG2 · MUST · now.** Every `kbagent …` example in `CLAUDE.md`, `kbagent context` and the plugin agents, skills and references parses against the command tree (H3).

- Checked by: `help.doc-examples-parse`.

<a id="AG3"></a>
**AG3 · MUST · now.** `(since vX.Y.Z)` notes and the version gate of the plugin name versions that exist. From 1.0.0, the plugin documents state the minimum version.

- Checked by: `scripts/check_version_gates.py`.

## 17. Long-running operations

<a id="LR1"></a>
**LR1 · MUST · now.** `--wait`/`--no-wait` and `--timeout` (seconds, A7) have one meaning everywhere. The help of each command states whether it waits by default and its default timeout. The defaults differ between commands today; a change of a default is breaking (C1) and waits for 1.0.0.

- Checked by: `args.global-option-one-shape` (deferred) and review.

<a id="LR2"></a>
**LR2 · MUST · now.** A local wait timeout does not stop the server job. The error says that the job keeps running, names the job and the command that follows it, has `retryable: false` and exits 4.

- Checked by: review.

<a id="LR3"></a>
**LR3 · SHOULD · now.** A command that starts a server job returns the job ID under `--json`, also when it waits.

- Checked by: review.

## 18. Environment variables and global options

<a id="EV1"></a><a id="env.registry-complete"></a>
**EV1 · MUST · now.** Every environment variable that kbagent reads is in one registry (name, purpose, secret or not, added in) and in the generated reference. An option that a variable sets declares it with `envvar=`, so that `--help` shows it. The registry does not exist yet; until it lands, a new variable is named in the pull request and documented in `CLAUDE.md`.

- Checked by: `env.registry-complete` (deferred: lands with the registry) and review.

<a id="EV2"></a>
**EV2 · MUST · now.** A new variable uses the prefix `KBAGENT_`. `KBC_` stays for values that other Keboola tools share (`KBC_TOKEN`, `KBC_STORAGE_API_URL`).

- Checked by: review.

<a id="EV3"></a>
**EV3 · MUST · now.** Precedence is flag > environment variable > saved choice > default (T1, T14).

- Checked by: review.

<a id="EV4"></a>
**EV4 · SHOULD · 1.0.0.** Global options are accepted before and after the subcommand (O7).

- Checked by: `output.root-flags-anywhere` (deferred).

## 19. Versions and updates

<a id="UP1"></a>
**UP1 · MUST · 0.100.0.** The updater stays on the running major. It crosses to a higher major only at M (C9) or with `kbagent update --major`, which 0.100.0 adds. Before 0.100.0, the updater installs any newer version.

- Checked by: updater contract tests.

<a id="UP2"></a>
**UP2 · MUST · 0.100.0.** A crossing to a higher major is installed for the next run. No command line runs again under a different major.

- Checked by: updater contract tests.

<a id="UP3"></a>
**UP3 · MUST · 0.100.0.** Update and deprecation notices go to stderr in human mode and to `warnings[]` under `--json`.

- Checked by: updater contract tests.

<a id="UP4"></a>
**UP4 · MUST · 0.100.0.** `kbagent version --beta` and `kbagent update --beta` never write the startup cache.

- Checked by: unit tests.

<a id="UP5"></a>
**UP5 · MUST · before T0.** `install.sh` and the PowerShell block of `README.md` install the highest stable version on PyPI (`info.version` of `https://pypi.org/pypi/keboola-cli/json`), not the release that GitHub marks as latest and not the GitHub release list.

- Checked by: review.
