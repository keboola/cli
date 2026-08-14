#!/usr/bin/env python3
"""Generate a kbagent-native source-project -> destination-project promotion pipeline.

This is a from-scratch generator (no existing repo to migrate) for the "one GitHub
repo covers the whole org" pattern: one or more named pipelines, each syncing a
directory between a SOURCE Keboola project (e.g. dev) and a DESTINATION project
(e.g. prod). It emits three GitHub Actions workflows:

  1. kbagent-promote-pull.yml   (workflow_dispatch + optional schedule)
     Pulls every pipeline's directory from its SOURCE project and opens/updates
     one PR against the main branch with the combined diff.
  2. kbagent-promote-validate.yml (pull_request against main)
     For every pipeline, runs `sync push --dry-run` against the DESTINATION
     project -- this is the cross-project diff: "if this PR merges, here is
     exactly what changes in the destination project."
  3. kbagent-promote-push.yml   (push to main, environment-gated)
     Pushes every pipeline's directory to its DESTINATION project once the PR
     has merged.

Each pipeline needs two Storage API token secrets (`KBC_TOKEN_<NAME>_SOURCE` /
`KBC_TOKEN_<NAME>_DEST`) and uses kbagent's `KBAGENT_PROJECT_FROM_ENV=1` /
`__env__` env-injection model -- no token is ever committed to the repo.

Stdlib only. Dry-run by default; pass ``--write`` to write files.

Usage:
    # Single pipeline via flags:
    python generate_promotion_pipeline.py --write \\
        --name SALESFORCE --directory salesforce \\
        --source-stack-url https://connection.keboola.com \\
        --dest-stack-url https://connection.keboola.com \\
        --version X.Y.Z

    # Multiple pipelines (whole-org repo) via a JSON config:
    python generate_promotion_pipeline.py --write --config pipelines.json --version X.Y.Z

pipelines.json shape:
    [
      {"name": "SALESFORCE", "directory": "salesforce",
       "source_stack_url": "https://connection.keboola.com",
       "dest_stack_url": "https://connection.keboola.com"},
      {"name": "GA4", "directory": "ga4",
       "source_stack_url": "https://connection.keboola.com",
       "dest_stack_url": "https://connection.keboola.com"}
    ]
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------- #
# Pipeline definition
# --------------------------------------------------------------------------- #


@dataclass
class Pipeline:
    """One source-project -> destination-project promotion pipeline."""

    name: str
    directory: str
    source_stack_url: str
    dest_stack_url: str

    @property
    def label(self) -> str:
        return re.sub(r"[^A-Za-z0-9]+", "_", self.name).strip("_").upper() or "PIPELINE"

    @property
    def source_token_secret(self) -> str:
        return f"KBC_TOKEN_{self.label}_SOURCE"

    @property
    def dest_token_secret(self) -> str:
        return f"KBC_TOKEN_{self.label}_DEST"


_SAFE_DIRECTORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]*$")
_SAFE_URL_RE = re.compile(r"^https?://[A-Za-z0-9.-]+/?$")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
_SAFE_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
# Exactly five space-separated cron fields, each restricted to the POSIX
# charset -- also rejects newlines, which `str.split()` alone would swallow.
_SAFE_CRON_RE = re.compile(r"^[0-9*/,-]+(?:[ ]+[0-9*/,-]+){4}$")


def _validate_pipelines(pipelines: list[Pipeline]) -> None:
    """Reject pipeline fields that would break out of generated YAML/shell quoting.

    ``name``/``directory``/``*_stack_url`` are admin-authored (a JSON config
    file or CLI flags), not attacker-controlled at runtime, but the generator
    still embeds them into YAML string fields and (for ``directory``) a
    shell command -- a value containing a quote, colon, newline, or `..`
    would corrupt the generated workflow. Fail fast at generation time
    instead (CWE-78-adjacent). Also rejects two pipelines that would collide
    on the same tracked directory (guaranteed-wrong: two different tokens
    pulling/pushing into one folder) or the same secret-name label.
    """
    labels_seen: dict[str, str] = {}
    directories_seen: dict[str, str] = {}
    for p in pipelines:
        if not _SAFE_NAME_RE.match(p.name):
            raise ValueError(f"pipeline name {p.name!r} contains unsafe characters")
        if ".." in p.directory.split("/") or not _SAFE_DIRECTORY_RE.match(p.directory):
            raise ValueError(f"pipeline {p.name!r}: unsafe directory {p.directory!r}")
        for field, url in (
            ("source_stack_url", p.source_stack_url),
            ("dest_stack_url", p.dest_stack_url),
        ):
            if not _SAFE_URL_RE.match(url):
                raise ValueError(f"pipeline {p.name!r}: unsafe {field} {url!r}")
        if p.directory in directories_seen:
            raise ValueError(
                f"pipelines {directories_seen[p.directory]!r} and {p.name!r} both use "
                f"directory {p.directory!r} -- each pipeline needs its own directory"
            )
        directories_seen[p.directory] = p.name
        if p.label in labels_seen:
            raise ValueError(
                f"pipelines {labels_seen[p.label]!r} and {p.name!r} both sanitize to the "
                f"secret-name label {p.label!r} -- rename one"
            )
        labels_seen[p.label] = p.name


def _validate_workflow_options(schedule: str | None, main_branch: str) -> None:
    """Reject ``--schedule`` / ``--main-branch`` values that would corrupt the YAML.

    Same threat model as :func:`_validate_pipelines`, one layer up: both are
    admin-authored CLI flags, and both are spliced **unquoted** into the
    generated workflows -- ``- cron: '{schedule}'`` in the pull workflow,
    ``base: {main_branch}`` in its PR step, and ``branches: [{main_branch}]``
    in the push trigger. A stray quote, colon, bracket or newline silently
    produces a workflow file GitHub cannot parse, so fail fast here instead.
    """
    if ".." in main_branch or not _SAFE_BRANCH_RE.match(main_branch):
        raise ValueError(f"unsafe --main-branch {main_branch!r}")
    if schedule is not None and not _SAFE_CRON_RE.match(schedule):
        raise ValueError(
            f"--schedule {schedule!r} is not a 5-field cron expression, e.g. '0 6 * * 1'"
        )


def _pipeline_from_dict(p: dict) -> Pipeline:
    try:
        return Pipeline(
            name=str(p["name"]),
            directory=str(p["directory"]),
            source_stack_url=_normalize_url(str(p["source_stack_url"])),
            dest_stack_url=_normalize_url(str(p["dest_stack_url"])),
        )
    except KeyError as exc:
        raise ValueError(f"pipeline entry missing required key {exc}") from exc


def _load_pipelines(args: argparse.Namespace) -> list[Pipeline]:
    if args.config:
        try:
            raw = Path(args.config).read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, list):
                raise ValueError("--config must contain a JSON list of pipeline objects")
            pipelines = [_pipeline_from_dict(p) for p in data]
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"error: --config {args.config!r} is invalid: {exc}", file=sys.stderr)
            sys.exit(2)
    else:
        missing = [
            flag
            for flag, val in (
                ("--name", args.name),
                ("--directory", args.directory),
                ("--source-stack-url", args.source_stack_url),
                ("--dest-stack-url", args.dest_stack_url),
            )
            if not val
        ]
        if missing:
            print(
                f"error: --config or all of {', '.join(missing)} must be provided",
                file=sys.stderr,
            )
            sys.exit(2)
        pipelines = [
            Pipeline(
                name=args.name,
                directory=args.directory,
                source_stack_url=_normalize_url(args.source_stack_url),
                dest_stack_url=_normalize_url(args.dest_stack_url),
            )
        ]
    try:
        _validate_pipelines(pipelines)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
    return pipelines


def _normalize_url(host: str) -> str:
    # kbagent's own `KBC_STORAGE_API_URL` consumption normalizes a bare host
    # to `https://<host>` (see `models.normalize_stack_url`), but the
    # generator also uses this value in printed messages and YAML before
    # kbagent ever sees it, so normalize once here too.
    host = host.strip()
    if host.startswith(("http://", "https://")):
        return host
    return f"https://{host}"


# --------------------------------------------------------------------------- #
# Workflow generation
# --------------------------------------------------------------------------- #


def _install_steps(version: str | None, git_ref: str | None) -> str:
    if git_ref:
        spec = f"git+https://github.com/keboola/cli@{git_ref}"
    elif version:
        spec = f"keboola-cli=={version}"
    else:
        spec = "keboola-cli"
    return (
        "      - name: Install uv\n"
        "        uses: astral-sh/setup-uv@v5\n"
        "      - name: Install kbagent\n"
        f"        run: uv tool install {shlex.quote(spec)}\n"
        "      - name: Show version\n"
        "        run: kbagent version\n"
    )


def _pipeline_step(
    p: Pipeline,
    step_name: str,
    command: str,
    token_secret: str,
    stack_url: str,
    json_output: bool = False,
) -> str:
    prefix = "kbagent --json " if json_output else "kbagent "
    return (
        f"      - name: {step_name} ({p.name})\n"
        "        env:\n"
        '          KBAGENT_PROJECT_FROM_ENV: "1"\n'
        f"          KBC_TOKEN: ${{{{ secrets.{token_secret} }}}}\n"
        f"          KBC_STORAGE_API_URL: {stack_url}\n"
        "        run: |\n"
        f"          {prefix}sync {command} --project __env__ "
        f"--directory {shlex.quote(p.directory)}\n"
    )


def _pull_and_merge_step(p: Pipeline) -> str:
    """Pull SOURCE into a throwaway scratch dir, then merge its *content* --
    never its `.keboola/manifest.json` -- into the tracked directory.

    The tracked directory's manifest is bootstrapped once from the DEST
    project (Step 4) and must stay bound to DEST's config IDs forever after;
    overwriting it with a plain `sync pull --project __env__ --directory
    '{p.directory}'` against the SOURCE token (the original approach) replaces
    that manifest with SOURCE's IDs, so every subsequent `sync push` to DEST
    fails to match any existing config by ID and recreates duplicates on every
    promotion cycle instead of converging. Pulling into an ephemeral scratch
    directory and copying only the content over keeps DEST's ID mapping
    stable while still picking up SOURCE's additions/edits/deletions.
    """
    scratch = f"/tmp/promote-scratch/{p.directory}"
    return (
        f"      - name: Pull {p.name} from source (scratch)\n"
        "        env:\n"
        '          KBAGENT_PROJECT_FROM_ENV: "1"\n'
        f"          KBC_TOKEN: ${{{{ secrets.{p.source_token_secret} }}}}\n"
        f"          KBC_STORAGE_API_URL: {p.source_stack_url}\n"
        "        run: |\n"
        f"          kbagent sync pull --project __env__ --directory {shlex.quote(scratch)} "
        "--force\n"
        f"      - name: Merge {p.name} content into '{p.directory}' (manifest untouched)\n"
        "        run: |\n"
        "          python3 - <<'PYEOF'\n"
        "          import pathlib, shutil\n"
        f"          src = pathlib.Path({scratch!r})\n"
        f"          dst = pathlib.Path({p.directory!r})\n"
        "          dst.mkdir(parents=True, exist_ok=True)\n"
        "          for item in list(dst.iterdir()):\n"
        "              if item.name == '.keboola':\n"
        "                  continue\n"
        "              # is_dir() follows symlinks -- unlink the link itself rather\n"
        "              # than rmtree-ing through it (which could delete outside <dir>).\n"
        "              if item.is_symlink() or item.is_file():\n"
        "                  item.unlink()\n"
        "              else:\n"
        "                  shutil.rmtree(item)\n"
        "          for item in src.iterdir():\n"
        "              if item.name == '.keboola':\n"
        "                  continue\n"
        "              if item.is_symlink():\n"
        "                  # kbagent's own `sync pull` output is always plain files/\n"
        "                  # dirs, never symlinks -- refuse rather than guess at intent.\n"
        "                  raise SystemExit(f'refusing to copy symlink from pull: {item}')\n"
        "              target = dst / item.name\n"
        "              if item.is_dir():\n"
        "                  shutil.copytree(item, target)\n"
        "              else:\n"
        "                  shutil.copy2(item, target)\n"
        "          PYEOF\n"
    )


def gen_pull(pipelines: list[Pipeline], schedule: str | None, main_branch: str) -> str:
    on_block = "  workflow_dispatch:\n"
    if schedule:
        on_block += f"  schedule:\n    - cron: '{schedule}'\n"
    steps = "".join(_pull_and_merge_step(p) for p in pipelines)
    paths = ", ".join(p.directory for p in pipelines)
    return (
        "# Generated by kbagent-promotion-pipeline. Pulls every pipeline's SOURCE\n"
        "# project into a scratch dir, merges content (not the DEST-bound manifest)\n"
        "# into the tracked directory, and opens/updates one PR against main.\n"
        "name: kbagent promote - pull\n"
        "on:\n"
        f"{on_block}"
        "permissions:\n"
        "  contents: write\n"
        "  pull-requests: write\n"
        "jobs:\n"
        "  pull:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        f"{_INSTALL_TOKEN}"
        f"{steps}"
        "      - name: Open promotion PR\n"
        "        uses: peter-evans/create-pull-request@v7\n"
        "        with:\n"
        "          # The default GITHUB_TOKEN cannot be used here: GitHub suppresses\n"
        "          # `pull_request`-triggered workflow runs (kbagent-promote-validate)\n"
        "          # for PRs opened by the default token, so validate would silently\n"
        "          # never run. Use a PAT/GitHub App token instead -- see\n"
        "          # references/secrets-setup.md.\n"
        "          token: ${{ secrets.PROMOTION_PR_TOKEN }}\n"
        "          branch: promote/update\n"
        f"          base: {main_branch}\n"
        '          commit-message: "kbagent promote: pull latest config from source project(s)"\n'
        '          title: "Promote: pull latest config from source project(s)"\n'
        "          body: |\n"
        "            Automated pull from the source project(s) for:\n"
        f"            {paths}\n\n"
        "            Review the diff, then merge to push it to the destination\n"
        "            project(s) -- see the validate check on this PR for the exact\n"
        "            destination-side change preview.\n"
    )


def gen_validate(pipelines: list[Pipeline]) -> str:
    steps = "".join(
        _pipeline_step(
            p,
            "Destination dry-run",
            "push --dry-run",
            p.dest_token_secret,
            p.dest_stack_url,
            json_output=True,
        )
        for p in pipelines
    )
    paths = "\n".join(f"      - '{p.directory}/**'" for p in pipelines)
    return (
        "# Generated by kbagent-promotion-pipeline. Cross-project diff: shows\n"
        "# exactly what merging this PR would change in each DESTINATION project.\n"
        "name: kbagent promote - validate\n"
        "on:\n"
        "  pull_request:\n"
        "    paths:\n"
        f"{paths}\n"
        "permissions:\n"
        "  contents: read\n"
        "jobs:\n"
        "  validate:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        f"{_INSTALL_TOKEN}"
        f"{steps}"
    )


def _push_job(p: Pipeline) -> str:
    """One job per pipeline, each gated by its own `prod` environment run.

    GitHub's required-reviewer approval is per job *run*, not per environment
    name -- two jobs in the same workflow run that both reference `prod` each
    get their own separate approval prompt. The original design put every
    pipeline's push as a *step* inside one shared job, so a single approval
    click unlocked every pipeline at once, and one pipeline's failure could
    leave later pipelines silently un-pushed. Splitting into independent jobs
    fixes both: per-pipeline approval, and jobs run independently so one
    failing does not block the others.
    """
    job_id = f"push_{p.label.lower()}"
    return (
        f"  {job_id}:\n"
        "    environment: prod\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        f"{_INSTALL_TOKEN}"
        "      # `sync push` encrypts #-secrets fail-closed by default. Do NOT add\n"
        "      # --allow-plaintext-on-encrypt-failure in CI.\n"
        f"{_pipeline_step(p, 'Push', 'push', p.dest_token_secret, p.dest_stack_url)}"
    )


def gen_push(pipelines: list[Pipeline], main_branch: str) -> str:
    paths = "\n".join(f"      - '{p.directory}/**'" for p in pipelines)
    jobs = "".join(_push_job(p) for p in pipelines)
    return (
        "# Generated by kbagent-promotion-pipeline. Pushes every pipeline's\n"
        "# directory to its DESTINATION project once merged to main. Each\n"
        "# pipeline is its OWN job, each gated by the 'prod' GitHub Environment --\n"
        "# add required reviewers there. Every job run needs its own separate\n"
        "# approval; approving one pipeline's push never approves another's, and\n"
        "# one pipeline failing does not block the others.\n"
        "name: kbagent promote - push\n"
        "on:\n"
        "  push:\n"
        f"    branches: [{main_branch}]\n"
        "    paths:\n"
        f"{paths}\n"
        "permissions:\n"
        "  contents: read\n"
        "jobs:\n"
        f"{jobs}"
    )


_INSTALL_TOKEN = "@@INSTALL@@\n"


# --------------------------------------------------------------------------- #
# Secrets checklist
# --------------------------------------------------------------------------- #


def secrets_report(pipelines: list[Pipeline], repo_slug: str) -> str:
    lines: list[str] = []
    lines.append("Required GitHub secrets (one SOURCE + one DEST token per pipeline):")
    for p in pipelines:
        lines.append(
            f"  gh secret set {p.source_token_secret} --repo {repo_slug}   # {p.name} source project"
        )
        lines.append(
            f"  gh secret set {p.dest_token_secret} --repo {repo_slug}   # {p.name} destination project"
        )
    lines.append("")
    lines.append(
        "Required GitHub PAT (default GITHUB_TOKEN cannot trigger the validate\n"
        "check on the PR it opens -- see references/secrets-setup.md):"
    )
    lines.append(f"  gh secret set PROMOTION_PR_TOKEN --repo {repo_slug}")
    lines.append("")
    lines.append("Required GitHub Environment (for push approval gating):")
    lines.append(f"  gh api -X PUT repos/{repo_slug}/environments/prod")
    lines.append("  # Then add required reviewers to 'prod' in the GitHub UI.")
    lines.append("  # Also add a required status check for 'validate' on the main")
    lines.append("  # branch's protection rules, or a merge can bypass validate entirely.")
    return "\n".join(lines)


def _guess_repo_slug(repo: Path) -> str:
    config = repo / ".git" / "config"
    if config.exists():
        m = re.search(
            r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?\s*$",
            config.read_text(errors="ignore"),
            re.MULTILINE,
        )
        if m:
            return m.group(1)
    return "<owner>/<repo>"


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def run(args: argparse.Namespace) -> int:
    repo = Path(args.repo_dir).resolve()
    if not repo.is_dir():
        print(f"error: {repo} is not a directory", file=sys.stderr)
        return 2

    try:
        _validate_workflow_options(args.schedule, args.main_branch)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    pipelines = _load_pipelines(args)

    print(f"{len(pipelines)} promotion pipeline(s):")
    for p in pipelines:
        print(f"  - {p.name:<12} directory={p.directory}")
        print(f"      source: {p.source_stack_url}  (secret {p.source_token_secret})")
        print(f"      dest:   {p.dest_stack_url}  (secret {p.dest_token_secret})")

    install = _install_steps(args.version, args.git_ref)
    files = {
        ".github/workflows/kbagent-promote-pull.yml": gen_pull(
            pipelines, args.schedule, args.main_branch
        ),
        ".github/workflows/kbagent-promote-validate.yml": gen_validate(pipelines),
        ".github/workflows/kbagent-promote-push.yml": gen_push(pipelines, args.main_branch),
    }
    files = {k: v.replace(_INSTALL_TOKEN, install) for k, v in files.items()}

    print(f"\nGenerated workflows ({'WRITING' if args.write else 'dry-run, use --write'}):")
    for rel, content in files.items():
        target = repo / rel
        print(f"  - {rel}  ({len(content.splitlines())} lines)")
        if args.write:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    print("\n" + "=" * 70)
    print(secrets_report(pipelines, _guess_repo_slug(repo)))
    print("=" * 70)

    if not args.write:
        print("\nDry-run only. Re-run with --write to create the files above.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("repo_dir", help="Path to the git repo to generate workflows into")
    ap.add_argument(
        "--write", action="store_true", help="Write the generated workflows (default: dry-run)"
    )
    ap.add_argument("--config", help="JSON file with a list of pipeline definitions")
    ap.add_argument("--name", help="Pipeline name (single-pipeline mode)")
    ap.add_argument("--directory", help="Directory to sync (single-pipeline mode)")
    ap.add_argument(
        "--source-stack-url", help="Source project's stack URL/host (single-pipeline mode)"
    )
    ap.add_argument(
        "--dest-stack-url", help="Destination project's stack URL/host (single-pipeline mode)"
    )
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--version", help="Pin kbagent to this PyPI version, e.g. X.Y.Z")
    grp.add_argument("--git-ref", help="Pin kbagent to a git tag/ref, e.g. vX.Y.Z")
    ap.add_argument(
        "--main-branch",
        default="main",
        help="Branch promotion PRs merge into and that triggers the push workflow (default: main)",
    )
    ap.add_argument(
        "--schedule",
        default=None,
        help="Cron for scheduled pulls, e.g. '0 6 * * 1' (default: none)",
    )
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
