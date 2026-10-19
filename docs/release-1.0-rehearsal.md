# Rehearsal of the 1.0.0 release mechanics

This checklist is for the maintainer. Walk it in a fork before T0, and record each result in the Linear ticket. It covers the release mechanics in [CONTRIBUTING.md > Releasing a new major version](../CONTRIBUTING.md#releasing-a-new-major-version) and the points of the 1.0.0 proposal that code and documentation could not confirm. Never run any step against `keboola/cli`: every step below creates, edits or publishes releases.

T0 is the `published_at` of the GitHub release `v1.0.0`; M is T0 + 28 days. In the fork, M is shortened to minutes (step 2).

## 1. Prepare the fork

1. Fork `keboola/cli` (or push a clone to a new scratch repository). Enable GitHub Actions in it.
2. Do not add any secret and do not create a `release` environment with secrets. Without secrets, nothing can reach PyPI, S3, Homebrew, Chocolatey, winget or ai-kit.
3. Install `gh` and `jq`, and set `REPO=<owner>/<fork>` in your shell. Every `gh` command below uses `-R "$REPO"`.

## 2. Add the rehearsal commit to the fork's `main`

One commit, only in the fork:

- `scripts/release_latest.py`: set `MIGRATION_WINDOW = timedelta(minutes=20)`.
- `.github/workflows/release-kbagent.yml`, to keep only the jobs under test:
  - `gate`: replace the steps with one step that writes `release-notes.md` with any text and uploads it as the `release-notes` artifact (the same `actions/upload-artifact` step as today). The real gate needs a changelog entry for every rehearsal version.
  - `github-release`: change `needs` to `[version, gate]`; in "Collect release assets", create `release/dummy.txt` instead of copying artifacts; delete the two wheel steps ("Attach the pip/uv wheel", "Confirm the wheel was attached").
  - `pypi`, `homebrew`, `ai-kit-marketplace`: keep `needs` and `if` as they are, replace the steps with `- run: echo "would publish $VERSION"`, and delete `environment: release`.
  - Delete the jobs `freeze`, `package-linux`, `publish-s3`, `chocolatey`, `winget` and `test-install`. Change `needs` of `homebrew` and `ai-kit-marketplace` to `[version]`.
- `pyproject.toml`: the version of each rehearsal tag must match (the version job does not check it, but keep it consistent). Bump it in a commit before each tag.

## 3. Walk the timeline

For each tag: push it with `git push origin <tag>`, wait for the `release-kbagent` run, then check the results. "Latest" means `gh api repos/$REPO/releases/latest --jq .tag_name`. "Redirect" means `curl -sI https://github.com/$REPO/releases/latest | grep -i '^location'`.

| # | Action | Expected result |
|---|---|---|
| 3.1 | Tag `v0.1.0` on `main`. | `version` job: `MAJOR_START=false`, `old_line=false`. `make_latest=true`. Latest and redirect: `v0.1.0`. `homebrew` and `ai-kit-marketplace` run (echo). |
| 3.2 | Create `release/0.1` from `v0.1.0`. Bump to 0.1.1, tag `v0.1.1` on that branch. | `old_line=false` (no `v1.0.0` yet). `make_latest=true`. Latest: `v0.1.1`. |
| 3.3 | On `main`, bump to 1.0.0 and tag `v1.0.0`. Record `published_at`: this is T0. | `MAJOR_START=true`. `make_latest=false`. `pypi` starts only after `github-release` succeeded (compare the job start times). Latest and redirect: still `v0.1.1`. The releases page does not mark `v1.0.0` "Latest". |
| 3.4 | Re-run the `github-release` job of `v1.0.0` ("Re-run jobs" in the run). | `make_latest=false`. `published_at` of `v1.0.0` unchanged. Latest still `v0.1.1`. |
| 3.5 | Run `gh workflow run promote-major.yml -R "$REPO"` before M. | `decide` prints `tag=`. `promote` is skipped. Latest unchanged. |
| 3.6 | On `main`, tag `v1.0.1`. | `make_latest=false`. Latest still `v0.1.1`. |
| 3.7 | On `release/0.1`, tag `v0.1.2` (before M). | `old_line=true`. `homebrew` and `ai-kit-marketplace` are skipped. `make_latest=true`. Latest: `v0.1.2`. |
| 3.8 | Wait until M (T0 + 20 minutes), then run `promote-major` by hand. | `decide` prints `tag=v1.0.1`. `promote` marks it. Latest and redirect: `v1.0.1`. |
| 3.9 | Run `promote-major` again. | `tag=` (already latest). `promote` is skipped. |
| 3.10 | On `release/0.1`, tag `v0.1.3` (after M). | `old_line=true`, `make_latest=false`. Latest still `v1.0.1`. |
| 3.11 | On `main`, tag `v1.1.0`. | `make_latest=true`. Latest: `v1.1.0`. |
| 3.12 | Re-run the `github-release` job of `v1.0.1`. | `make_latest=false` (a lower version never takes the flag back). Latest still `v1.1.0`. |
| 3.13 | Leave the fork for at least one hour. | A scheduled `promote-major` run exists and printed `tag=`. |

## 4. Points that the proposal could not confirm (appendix C)

Do these in the same fork, with `gh` by hand.

| # | Point | How to check | Expected |
|---|---|---|---|
| 4.1 | Editing a release keeps its `published_at`. | Record `gh api repos/$REPO/releases/tags/v1.0.0 --jq .published_at`. Run `gh release edit v1.0.0 -R "$REPO" --notes "edited"`, then upload an asset with `gh release upload`. Read `published_at` again. | Unchanged. |
| 4.2 | Publishing a draft again changes `published_at`. | `gh release create v0.0.9 -R "$REPO" --draft --notes x`, then `gh release edit v0.0.9 --draft=false`, record `published_at`; `gh release edit v0.0.9 --draft=true`, wait one minute, `gh release edit v0.0.9 --draft=false`, read it again. | Changed. This is why `v1.0.0` is never set back to draft. |
| 4.3 | The web redirect `releases/latest` follows `make_latest`. | Compare redirect and latest after each step of section 3. | Always the same tag. |
| 4.4 | An edit that sends no `make_latest` keeps the current latest. | With latest on `v1.1.0`, run `gh release edit v1.0.1 -R "$REPO" --notes "no flag"` (no `--latest`). | Latest still `v1.1.0`. (The workflow sends `--latest` anyway; this checks the GitHub default.) |
| 4.5 | Release tags are lightweight or annotated. | `gh api repos/keboola/cli/git/ref/tags/v0.99.1 --jq .object.type` (read-only, on the real repository). | `commit` = lightweight. Checked on 2026-10-19: `v0.99.1` and `v0.98.0` are lightweight. |
| 4.6 | A Claude Code marketplace with a relative `source` serves the plugin from the default branch. | In the fork, `/plugin marketplace add $REPO`, install `kbagent` from it. Change `plugins/kbagent/.claude-plugin/plugin.json` `version` on a non-default branch only, run `/plugin marketplace update`. Then change it on the default branch and update again. | Only the default-branch change is visible. This is why the shim is pinned before 1.x documents reach `main`. |
| 4.7 | A pinned shim works. | In the fork, pin `.claude-plugin/marketplace.json` as CONTRIBUTING describes (`git-subdir`, `ref: v0.1.1`). Run `/plugin marketplace update` and check the installed plugin version. | The plugin of `v0.1.1`, also after `main` moves on. |
| 4.8 | PyPI `info.version` names the highest stable version, also when a 0.x patch is uploaded after a 1.x release. | On TestPyPI, with a throwaway package name: upload 0.1.0, 1.0.0, then 0.1.1. Read `curl -s https://test.pypi.org/pypi/<name>/json \| jq -r .info.version`. | `1.0.0`. If it is `0.1.1`, `install.sh` and the README PowerShell block must stop using `info.version` and take the highest stable key of `releases` from the same JSON instead; this needs a code change before T0. |
| 4.9 | httpx reads the proxy environment variables by default. | `uv run python -c "import httpx; print(httpx.Client().trust_env)"`. | `True`. Checked on 2026-10-19 with httpx 0.28.1. |

## 5. Not part of this rehearsal

- The Storage API answer when a shared bucket is deleted (proposal decision D9): a Storage API check, owned by the ticket of that change.
- The moderation time of Chocolatey and the merge time of a winget pull request: read them from the history of earlier releases for the T0 announcement.
- How fast active installs pick up the 0.N.x patch: measure it after the patch is released.

## 6. After the rehearsal

Delete the fork (or its releases and tags), and the TestPyPI project. Record in the Linear ticket: the fork, the run links of section 3, and the result of each row of section 4.
