# kbc ↔ kbagent command / flag / env mapping

Authoritative mapping used by the migration generator. Verify flags against your
installed `kbagent` version (`kbagent sync pull --help`); the new CLI evolves fast.

> **Verified against:** kbagent v0.80.0, live-verified 2026-08-06 (see
> `SKILL.md`'s "Verified against" note). Dated claims below are point-in-time
> repro notes, not version floors.

## Install

| kbc (old) | kbagent (new) |
|---|---|
| Download Go binary zip from `keboola/keboola-as-code` GitHub release, unzip to `/usr/local/bin/kbc` | `uv tool install keboola-cli==<ver>` (PyPI) or `uv tool install 'git+https://github.com/keboola/cli@<tag>'` |
| `kbc --version` | `kbagent version` |
| Custom `install` composite action | `astral-sh/setup-uv@v7` + one `uv tool install` line |

## Core sync commands

| kbc (old) | kbagent (new) | Notes |
|---|---|---|
| `kbc init -d DIR --allow-target-env` | `rm DIR/.keboola/manifest.json && kbagent sync init --project <alias> --directory DIR` | `--project` is required. For the one-time kbc→kbagent conversion use **plain `init`, not `--adopt-existing`** — adopting a kbc-written manifest inherits kbc's row/companion-config paths verbatim and leaves a permanently dirty `sync status` (root-caused; see SKILL.md and `migration-runbook.md`). kbc and kbagent both write to the same path (`.keboola/manifest.json`), so plain `init` errors "Manifest already exists" until you delete that one file (not the `config.json`/`meta.json` tree next to it) — confirmed live, 2026-08-06. `--adopt-existing` is still correct for re-registering an *already-converted* kbagent-native manifest in ephemeral CI (no kbc data involved at that point). |
| `kbc persist -d DIR` | *(folded into `sync pull`)* | No separate persist step; pull writes manifest + new objects |
| `kbc pull -d DIR --force` | `kbagent sync pull --project <alias> --directory DIR --force` | `--project` (or `--all-projects`) is required; `--force` overrides local-vs-remote conflicts (3-way diff) |
| `kbc push -d DIR` | `kbagent sync push --project <alias> --directory DIR` | Encrypts `#`-secrets fail-closed before write |
| `kbc push -d DIR --force` | `kbagent sync push --project <alias> --directory DIR --force` | Push's `--force` removes remote configs deleted locally (there is no `--allow-delete` flag — same flag name as pull's `--force`, but a different meaning per command) |
| `kbc push --dry-run` / push-dry action | `kbagent sync push --project <alias> --dry-run --directory DIR` | Shows planned changes without writing |
| `kbc diff -d DIR` | `kbagent [--json] sync diff --project <alias> --directory DIR` | `--json` is a **global** option (before `sync`, not after `diff`); gives structured drift for CI gating |
| `kbc status` | `kbagent sync status --directory DIR` | `sync status` reads the local manifest only, no `--project` needed |
| `kbc validate` (JSON-schema) | *(no direct equivalent — gap)* | Use `sync diff` for drift; schema validation is not ported |

## Auth / environment variables

| kbc (old) | kbagent (new) | Notes |
|---|---|---|
| `KBC_STORAGE_API_TOKEN` | `KBC_TOKEN` | Storage API token |
| `KBC_STORAGE_API_HOST` (bare host) | `KBC_STORAGE_API_URL` (full URL) | `connection.keboola.com` → `https://connection.keboola.com` |
| *(implicit)* | `KBAGENT_PROJECT_FROM_ENV=1` | **Required** opt-in so kbagent synthesizes an ephemeral project from the env in CI (no `config.json` on disk). See `constants.py`'s `ENV_PROJECT_FROM_ENV` and `ConfigStore._inject_env_project` |
| `KBC_PROJECT_ID`, `KBC_BRANCH_ID`, `KBC_BRANCHES` | *(from manifest + branch-mapping)* | Project id comes from `.keboola/manifest.json`; branch from `.keboola/branch-mapping.json` |

## Branching

| kbc (old) | kbagent (new) |
|---|---|
| Fixed `KBC_BRANCH_ID` per env; `allowedBranches` in manifest | `.keboola/branch-mapping.json` (git branch → Keboola branch id; `null` = production) managed by `kbagent sync branch-link / branch-unlink / branch-status` |
| Branch dir under repo (`main/`) | Same on-disk layout; mapping decides which Keboola branch a git branch targets |

## Subset of a project

Both CLIs honor manifest-level scoping — no command change needed:

- `allowedBranches: ["<id>"]` — restrict which branches sync.
- `ignoredComponents: ["keboola.foo", ...]` — exclude component types.

`kbagent` parses both (`sync/manifest.py:120`). Additionally, `sync pull` flags
`--no-storage` / `--no-jobs` / `--with-samples` control how much *metadata*
(beyond configs) is pulled — orthogonal to the config subset.

## What has NO clean port (call out to the user)
- `kbc validate` JSON-schema validation.
- `kbc ci workflows` generator itself (this skill replaces it).
- Templates / dbt / CI-scaffold subsystems (`kbc template`, `kbc dbt`) — keep `kbc`
  for those; they are out of scope for sync CI/CD.
