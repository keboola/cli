# kbc ↔ kbagent command / flag / env mapping

Authoritative mapping used by the migration generator. Verify flags against your
installed `kbagent` version (`kbagent sync pull --help`); the new CLI evolves fast.

## Install

| kbc (old) | kbagent (new) |
|---|---|
| Download Go binary zip from `keboola/keboola-as-code` GitHub release, unzip to `/usr/local/bin/kbc` | `uv tool install keboola-agent-cli==<ver>` (PyPI) or `uv tool install 'git+https://github.com/keboola/cli@<tag>'` |
| `kbc --version` | `kbagent version` |
| Custom `install` composite action | `astral-sh/setup-uv@v5` + one `uv tool install` line |

## Core sync commands

| kbc (old) | kbagent (new) | Notes |
|---|---|---|
| `kbc init -d DIR --allow-target-env` | `kbagent sync init --directory DIR [--adopt-manifest]` | `--adopt-manifest` reuses an existing `.keboola/manifest.json` written by `kbc` |
| `kbc persist -d DIR` | *(folded into `sync pull`)* | No separate persist step; pull writes manifest + new objects |
| `kbc pull -d DIR --force` | `kbagent sync pull --directory DIR --force` | `--force` overrides local-vs-remote conflicts (3-way diff) |
| `kbc push -d DIR` | `kbagent sync push --directory DIR` | Encrypts `#`-secrets fail-closed before write |
| `kbc push -d DIR --force` | `kbagent sync push --directory DIR --allow-delete` | `--allow-delete` removes remote configs deleted locally |
| `kbc push --dry-run` / push-dry action | `kbagent sync push --dry-run --directory DIR` | Shows planned changes without writing |
| `kbc diff -d DIR` | `kbagent sync diff --directory DIR [--json]` | `--json` gives structured drift for CI gating |
| `kbc status` | `kbagent sync status --directory DIR` | |
| `kbc validate` (JSON-schema) | *(no direct equivalent — gap)* | Use `sync diff` for drift; schema validation is not ported |

## Auth / environment variables

| kbc (old) | kbagent (new) | Notes |
|---|---|---|
| `KBC_STORAGE_API_TOKEN` | `KBC_TOKEN` | Storage API token |
| `KBC_STORAGE_API_HOST` (bare host) | `KBC_STORAGE_API_URL` (full URL) | `connection.keboola.com` → `https://connection.keboola.com` |
| *(implicit)* | `KBAGENT_PROJECT_FROM_ENV=1` | **Required** opt-in so kbagent synthesizes an ephemeral project from the env in CI (no `config.json` on disk). See `constants.py:163`, `config_store.py:193` |
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
`--skip-storage` / `--skip-jobs` / `--with-table-samples` control how much
*metadata* (beyond configs) is pulled — orthogonal to the config subset.

## What has NO clean port (call out to the user)
- `kbc validate` JSON-schema validation.
- `kbc ci workflows` generator itself (this skill replaces it).
- Templates / dbt / CI-scaffold subsystems (`kbc template`, `kbc dbt`) — keep `kbc`
  for those; they are out of scope for sync CI/CD.
