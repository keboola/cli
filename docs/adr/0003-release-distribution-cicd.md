# ADR 0003: Release & Distribution CI/CD

## Status

Accepted

## Date

2026-06-09

## Context

`kbagent` (`keboola-agent-cli`) ships today **only** via
`uv tool install git+https://github.com/keboola/cli` — no PyPI release, no native
packages, and a startup auto-update from mutable git `HEAD` (unsigned). The legacy
`kbc` CLI ships nine signed/notarized channels (Homebrew, apt/deb, rpm, apk, winget,
chocolatey, scoop, MSI, S3) from one goreleaser pipeline.

Current CI (`.github/workflows/`):

- **`ci.yml`** (push/PR → `main`): `check` (lint, ruff-format, `ty`, SKILL freshness,
  version consistency, command-sync, error-codes) · `test` (matrix py3.12/3.13,
  `pytest -m "not integration"`, coverage **informational**, no `--cov-fail-under`) ·
  `build-windows` (real `uv build --wheel` with Node 20, asserts the React SPA is
  bundled — issue #320 — plus a CLI-only `KBAGENT_SKIP_UI_BUILD=1` wheel).
- **`e2e.yml`** (cron 03:17 daily + dispatch): real-project suite that **self-skips
  green** when `E2E_API_TOKEN` is unset.

Key observation: `build-windows` already proves the **SPA-bundled wheel builds in CI**
(Node is present on the runner). The npm build hook is therefore **not** a CI problem —
it only bites **end users** installing on a Node-less host. The fix is small: publish
the CI-built **prebuilt wheel**, rather than re-running the hook on the user's machine.

We want native install/upgrade parity with `kbc`, using a `…2` naming
(`keboola-cli2` / `Keboola.KeboolaCLI2`) so both CLIs coexist during transition, while
retiring the unsigned git-HEAD auto-update.

## Decision

Add a **tag-triggered release pipeline** (`.github/workflows/release-kbagent.yml`) that:

1. **Gates** on the existing `check` + `test` against the tagged commit (plus
   `changelog-check`, which is intentionally excluded from per-PR CI).
2. **Builds the prebuilt wheel** (reusing the pinned uv (`astral-sh/setup-uv` at uv `0.11.16`) + `setup-node@20` +
   `uv build` recipe already proven in `build-windows`) and publishes to **PyPI via
   OIDC Trusted Publishing** (no stored token).
3. **Freezes** native `kbagent` binaries with **PyInstaller** (`--onefile`,
   `--collect-all keboola_agent_cli`, entry `build/package/entry.py`) across a matrix
   (linux amd64/arm64, macOS arm64, windows amd64 — macOS is Apple Silicon only;
   Intel Macs fall back to `uv tool install`), then **signs/notarizes**
   (Apple `notarytool`, Windows Authenticode via `jsign`). **Verified:** a frozen
   Linux binary runs in a stripped env — `env -i kbagent --version` → `kbagent v0.58.0`
   — with no Python/uv present. This is the **primary install path**; `uv`/`pipx` is
   only an additional convenience for Python users, never a requirement.
4. **Packages** via `nfpm` (deb/rpm/apk; no MSI — Chocolatey wraps the signed `.exe`) and **publishes** to S3
   (`cli-dist.keboola.com/keboola-cli2/`), a Homebrew tap PR (`keboola-cli2`),
   Chocolatey, and a winget PR. Scoop is **optional** (uv/uvx already covers that
   Windows audience). Packaging uses **`nfpm`** (standalone, language-agnostic — NOT
   goreleaser, which only builds Go) for deb/rpm/apk, a Homebrew formula template, and a
   Chocolatey nuspec wrapping the signed `.exe`. **All packaging config lives in this
   repo** under `build/package/` — **no dependency on the legacy keboola-as-code (`kbc`)
   repo, which is being deprecated**. The Homebrew tap is a new kbagent-owned repo
   (`keboola/homebrew-keboola-cli2`), not kbc's tap.

Jobs that need release credentials (`freeze` signing, `pypi`, `publish-s3`, the package
pushes) reference the GitHub **Environment `release`** purely to scope its secrets. The
environment has **no required reviewers** — releases are automatic on a version tag, so
the deliberate tag push *is* the approval. This matters: the `freeze` matrix is
environment-scoped (so signing secrets resolve), and because there are no reviewers it
is **never blocked**, which is what lets the `continue-on-error` signing steps actually
keep pre-release/dev runs non-blocking. Dev-vs-prod gating is the `IS_PRERELEASE` check,
not an approval prompt. (If approval is ever wanted, add it as a separate gated publish
job, not on `freeze`.) Native installs **defer upgrades to the package manager**; the
built-in self-update is pointed at **signed PyPI** (or disabled on managed installs),
retiring the git-HEAD risk.

### As-is → to-be delta

| Change | File | Type |
|---|---|---|
| New tag-triggered release pipeline | `.github/workflows/release-kbagent.yml` | ADD |
| Release gate re-runs `check`+`test`+`changelog-check` on the tag | release workflow | ADD |
| Protected `release` environment + scoped secrets | repo settings | ADD |
| Reusable wheel-build step shared by `build-windows` and release | `ci.yml` / composite | REFACTOR (optional) |
| e2e credentials guard **fails** (not warns) on scheduled/release runs | `e2e.yml` | CHANGE |
| Coverage floor asserted on release commit (PR stays informational) | release workflow | ADD (optional) |

### Required secrets (scoped to `release`)

| Name | Purpose | Source |
|---|---|---|
| *(PyPI OIDC — none)* | Publish to PyPI | Trusted Publisher on pypi.org for project `keboola-cli` (repo + workflow + env `release`) |
| `HOMEBREW_TAP_TOKEN` | Push formula to `keboola/homebrew-keboola-cli2` | fine-grained PAT, tap repo only |
| `CHOCOLATEY_KEY` | Push `.nupkg` | chocolatey.org API key (org account) |
| `WINGET_TOKEN` | Fork + PR `microsoft/winget-pkgs` | classic PAT (`public_repo`) on an org bot |
| `APPLE_DEVELOPER_CERTIFICATE_P12_BASE64` / `APPLE_DEVELOPER_CERTIFICATE_PASSWORD` | macOS code-sign | Developer ID Application cert (`.p12` base64) + its password |
| `APPLE_ACCOUNT_PASSWORD` | Notarization | App-specific password for the Apple ID (account/team are literals in the workflow) |
| `WINDOWS_SIGNING_TENANT_ID` / `WINDOWS_SIGNING_CLIENT_ID` / `WINDOWS_SIGNING_CLIENT_SECRET` | Authenticode via Azure Key Vault | service principal with Key Vault access |
| `AWS_ROLE_ARN` | Upload to `cli-dist.keboola.com` | IAM role trusting GitHub OIDC |
| `DEB_KEY_PRIVATE` (apt keyring exported inline from it by `index.sh`; no `DEB_KEY_PUBLIC`), `RPM_KEY_PRIVATE` / `RPM_KEY_PUBLIC` | Sign deb/rpm packages + repo metadata | GPG keypair (passphrase-less) |
| `APK_KEY_PRIVATE` / `APK_KEY_PUBLIC` | Sign the apk index | abuild RSA keypair |

Set with `gh secret set <NAME> --env release --repo keboola/cli` piping the value via
**stdin** (never argv); shred the temp file after.

#### Creating `WINGET_TOKEN` (step-by-step)

The `winget` release job (`.github/workflows/release-kbagent.yml`) needs a classic PAT
belonging to an **org-owned bot account** — never a personal token — because
`wingetcreate` forks `microsoft/winget-pkgs` and opens the PR as whoever owns the
token.

1. Get (or create) an org-owned GitHub bot account for this purpose — not a personal
   account. It only needs to be a regular GitHub user; it does **not** need to be a
   member of the `keboola` org or have any repo permissions here, since the token is
   only ever used against the external `microsoft/winget-pkgs` repo.
2. Sign in to GitHub as that bot account.
3. Go to **Settings → Developer settings → Personal access tokens → Tokens
   (classic)** (`https://github.com/settings/tokens`).
4. Click **Generate new token (classic)**.
5. Name it something identifiable, e.g. `wingetcreate-keboola-cli2`.
6. Set an expiration per org security policy (rotate before it lapses — an expired
   token fails the release job with an auth error, not a build error).
7. Under scopes, check **only** `public_repo`. Nothing else is needed.
8. Click **Generate token** and copy the value — GitHub shows it exactly once.
9. Store it as the repo secret, scoped to the `release` environment, piping the value
   via stdin so it never lands in shell history:
   ```
   gh secret set WINGET_TOKEN --env release --repo keboola/cli
   ```
   (paste the token, then Ctrl-D / Enter depending on your shell — see the `Set with`
   line above for the general pattern).
10. Shred any temp file or clipboard copy of the token once it's set.

This alone does not turn the `winget` job green: as of this writing that job is
disabled (`if: false`) because `Keboola.KeboolaCLI2` has never had its **first**
submission to `microsoft/winget-pkgs` — `wingetcreate update` only bumps an existing
manifest, it can't create one. Someone with the token above still needs to run
`wingetcreate submit` once by hand (see the NOTE in the `winget:` job for what that
first manifest must set, e.g. `License: Apache-2.0`) before the automated per-release
`update` job can be re-enabled.

### Rollout

1. **P0** — npm-hook prebuilt wheel + PyPI (OIDC). Instant `uv`/`uvx`/`pipx` parity.
2. **P1a** — PyInstaller freeze (Linux) → `nfpm` deb/rpm/apk + S3 repo index + Homebrew formula.
3. **P1b** — macOS sign/notarize, Windows sign → winget + choco.
4. **P2** — retire git-HEAD auto-update (managed-install detection); Scoop on demand.

## Consequences

- **Positive:** native install/upgrade parity with `kbc`; signed artifacts; OIDC for
  PyPI/AWS removes long-lived secrets; resolves the unsigned-auto-update risk
  (see ADR 0002 `docs/adr/0002-sec-09-config-privilege-separation.md` and the security review).
- **Cost:** Python cannot cross-compile, so the freeze matrix needs ~6 per-OS/arch
  runners (vs Go's single build); per-release wall-clock and maintenance grow.
- **Risk:** signing-cert and notarization setup is fiddly; mitigated by Keboola's
  existing org-level signing certs/keys (stored as secrets in this repo) and the
  `release` environment gate. Everything required to cut a release lives in this repo —
  no dependency on the deprecated `kbc` repository.

## Notes

This ADR is the **canonical, in-repo** decision record. The concrete artifacts all live
in this repository:

- `.github/workflows/release-kbagent.yml` — the pipeline (real publish jobs, gated by the `release` environment + the pre-release check; triggers on version tags and gated manual dispatch).
- `build/package/` — self-contained packaging: `entry.py` (freeze entry), `nfpm.yaml`
  (deb/rpm/apk), `homebrew/` (formula template + render), `chocolatey/` (nuspec + install),
  and `linux/`, `macos/`, `windows/` sign/index scripts.

No build- or release-time dependency on the legacy `keboola-as-code` (`kbc`) repository,
which is being deprecated.
