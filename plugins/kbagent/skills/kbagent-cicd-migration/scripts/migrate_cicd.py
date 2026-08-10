#!/usr/bin/env python3
"""Migrate a kbc (keboola-as-code) GitHub CI/CD repo to kbagent (keboola-cli).

This is the engine the ``kbagent-cicd-migration`` skill drives. It:

  1. Discovers every Keboola project in the repo by locating ``.keboola/manifest.json``
     files (supports the multi-project layout, e.g. ``L0/``, ``L1/``).
  2. Reads each manifest's ``project.id`` / ``project.apiHost`` /
     ``allowedBranches`` / ``ignoredComponents`` so generated workflows are
     project-accurate and the "subset of a project" lever is surfaced.
  3. Detects the legacy kbc CI/CD it will replace (``.github/workflows`` +
     ``.github/actions`` referencing ``kbc``).
  4. Emits **clean kbagent-native** GitHub Actions workflows
     (validate / pull / push) that use ``uv tool install`` + ``kbagent sync``
     with the env-injection auth model — no per-CLI composite actions, no
     committed tokens.
  5. Prints the exact GitHub **secrets + variables + environments** the new
     workflows need, with copy-paste ``gh`` CLI commands.

Stdlib only. Dry-run by default; pass ``--write`` to write files.

Usage:
    python migrate_cicd.py <repo_dir> [--write] \\
        [--version X.Y.Z | --git-ref vX.Y.Z] \\
        [--main-branch main] [--schedule "0 * * * *"]

Examples:
    # Inspect what would change (no writes):
    python migrate_cicd.py ../CLI-based-sync-demo

    # Generate workflows pinned to a PyPI version:
    python migrate_cicd.py ../CLI-based-sync-demo --write --version X.Y.Z

    # Pin to a git tag instead:
    python migrate_cicd.py ../CLI-based-sync-demo --write --git-ref vX.Y.Z
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# Project discovery
# --------------------------------------------------------------------------- #


@dataclass
class Project:
    """A single Keboola project discovered via its manifest."""

    alias: str  # derived from the directory name, uppercased for secret naming
    directory: str  # path relative to repo root (".", "L0", "L1", ...)
    project_id: str
    api_host: str
    allowed_branches: list[str] = field(default_factory=list)
    ignored_components: list[str] = field(default_factory=list)

    @property
    def token_secret(self) -> str:
        return f"KBC_TOKEN_{self.alias}"

    @property
    def stack_url(self) -> str:
        # apiHost in the manifest is bare ("connection.keboola.com"); kbagent's
        # KBC_STORAGE_API_URL wants a full URL.
        host = self.api_host.strip()
        if host.startswith(("http://", "https://")):
            return host
        return f"https://{host}"


def _alias_from_dir(directory: str) -> str:
    """Derive a secret-safe alias from the project's full relative path.

    Uses the whole path (not just the last segment) so nested multi-project
    layouts (``env/prod``, ``other/prod``) don't collide on a shared
    ``KBC_TOKEN_<ALIAS>`` secret name.
    """
    if directory in ("", "."):
        return "PROJECT"
    return re.sub(r"[^A-Za-z0-9]+", "_", directory).strip("_").upper() or "PROJECT"


def discover_projects(repo: Path) -> list[Project]:
    """Find every ``.keboola/manifest.json`` and parse it into a Project."""
    projects: list[Project] = []
    for manifest_path in sorted(repo.glob("**/.keboola/manifest.json")):
        project_dir = manifest_path.parent.parent
        rel = project_dir.relative_to(repo).as_posix()
        rel = "." if rel == "" else rel
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  ! skipping {manifest_path}: {exc}", file=sys.stderr)
            continue
        proj = data.get("project", {})
        project_id = proj.get("id")
        api_host = proj.get("apiHost")
        if not project_id or not api_host:
            print(
                f"  ! skipping {manifest_path}: missing project.id or project.apiHost",
                file=sys.stderr,
            )
            continue
        projects.append(
            Project(
                alias=_alias_from_dir(rel),
                directory=rel,
                project_id=str(project_id),
                api_host=str(api_host),
                allowed_branches=[str(b) for b in data.get("allowedBranches", [])],
                ignored_components=[str(c) for c in data.get("ignoredComponents", [])],
            )
        )
    return projects


def detect_legacy_ci(repo: Path) -> list[str]:
    """Return a list of legacy kbc CI/CD files that the migration supersedes."""
    found: list[str] = []
    gh = repo / ".github"
    if not gh.exists():
        return found
    for path in sorted(gh.glob("**/*")):
        if not path.is_file() or path.suffix not in {".yml", ".yaml"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # A legacy file is one that invokes the kbc binary or its env vars.
        if re.search(r"\bkbc\s+(pull|push|init|persist|diff|validate)\b", text) or (
            "KBC_STORAGE_API_TOKEN" in text
        ):
            found.append(path.relative_to(repo).as_posix())
    return found


# --------------------------------------------------------------------------- #
# Workflow generation (clean kbagent-native)
# --------------------------------------------------------------------------- #


def _install_steps(version: str | None, git_ref: str | None) -> str:
    if git_ref:
        spec = f"git+https://github.com/keboola/cli@{git_ref}"
    elif version:
        spec = f"keboola-cli=={version}"
    else:
        # Unpinned: only acceptable for non-production lanes. The skill warns.
        spec = "keboola-cli"
    return (
        "      - name: Install uv\n"
        "        uses: astral-sh/setup-uv@v5\n"
        "      - name: Install kbagent\n"
        f"        run: uv tool install '{spec}'\n"
        "      - name: Show version\n"
        "        run: kbagent version\n"
    )


def _project_step(p: Project, command: str, step_name: str, json_output: bool = False) -> str:
    """Render one per-project step using the env-injection auth model.

    kbagent sync is an orchestrator over registered project aliases, NOT a
    cwd-per-folder tool like kbc. In CI we synthesize an ephemeral project from
    the env (KBAGENT_PROJECT_FROM_ENV=1 -> reserved alias ``__env__``) and pass
    ``--project __env__`` explicitly. The committed ``.keboola/manifest.json``
    (written by the one-time conversion, Step 3b) is already checked out by
    ``actions/checkout`` -- no ``sync init`` step is needed or run here; a
    fresh, un-converted project has no CI step to init in the first place.
    ``--json`` is a global option, so it goes before ``sync``, not after the
    subcommand.
    """
    prefix = "kbagent --json " if json_output else "kbagent "
    return (
        f"      - name: {step_name} ({p.alias})\n"
        "        env:\n"
        '          KBAGENT_PROJECT_FROM_ENV: "1"\n'
        f"          KBC_TOKEN: ${{{{ secrets.{p.token_secret} }}}}\n"
        f"          KBC_STORAGE_API_URL: {p.stack_url}\n"
        "        run: |\n"
        f"          {prefix}sync {command} --project __env__ --directory '{p.directory}'\n"
    )


def gen_validate(projects: list[Project]) -> str:
    diff_steps = "".join(
        _project_step(p, "diff", f"Diff {p.directory}", json_output=True) for p in projects
    )
    dry_run_steps = "".join(
        _project_step(p, "push --dry-run", f"Push dry-run {p.directory}") for p in projects
    )
    return (
        "# Generated by kbagent-cicd-migration. Clean kbagent-native CI.\n"
        "name: kbagent validate\n"
        "on:\n"
        "  pull_request:\n"
        "  workflow_dispatch:\n"
        "permissions:\n"
        "  contents: read\n"
        "jobs:\n"
        "  validate:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        f"{_INSTALL_TOKEN}"
        "      # Show drift between the committed config files and each remote\n"
        "      # project. `sync diff` is read-only. The push dry-run below also\n"
        "      # surfaces secret-encryption problems before a real push.\n"
        f"{diff_steps}"
        f"{dry_run_steps}"
    )


def gen_pull(projects: list[Project], schedule: str | None) -> str:
    on_block = "  workflow_dispatch:\n"
    if schedule:
        on_block += f"  schedule:\n    - cron: '{schedule}'\n"
    steps = "".join(_project_step(p, "pull --force", f"Pull {p.directory}") for p in projects)
    return (
        "# Generated by kbagent-cicd-migration. Pulls remote state into git.\n"
        "name: kbagent pull\n"
        "on:\n"
        f"{on_block}"
        "permissions:\n"
        "  contents: write\n"
        "jobs:\n"
        "  pull:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        f"{_INSTALL_TOKEN}"
        f"{steps}"
        "      - name: Commit pulled state\n"
        "        run: |\n"
        "          git config user.name 'Keboola kbagent'\n"
        "          git config user.email 'kbagent@users.noreply.github.com'\n"
        "          git add -A\n"
        "          git commit -m \"Automatic kbagent pull $(date -u +%Y-%m-%dT%H:%M:%SZ)\" || echo 'no changes'\n"
        "          git push\n"
    )


def gen_push(projects: list[Project], main_branch: str) -> str:
    # GitHub Environments gate production approvals; the prod environment maps to
    # the main branch, mirroring the legacy `github.ref_name == 'main'` logic.
    env_expr = f"${{{{ github.ref_name == '{main_branch}' && 'prod' || 'dev' }}}}"
    # The workflow_dispatch boolean arrives as the string 'true'/'false'; the GH
    # expression maps it to the --force flag (opt-in deletion of remote configs
    # that were removed locally). kbagent's actual flag is --force, NOT
    # --allow-delete -- there is no --allow-delete option in the CLI.
    delete_expr = "${{ github.event.inputs.allow_delete == 'true' && '--force' || '' }}"
    steps = "".join(
        _project_step(p, f"push {delete_expr}", f"Push {p.directory}") for p in projects
    )
    return (
        "# Generated by kbagent-cicd-migration. Pushes git state to Keboola.\n"
        "# Protected by a GitHub Environment so prod pushes require approval.\n"
        "name: kbagent push\n"
        "on:\n"
        "  workflow_dispatch:\n"
        "    inputs:\n"
        "      allow_delete:\n"
        "        description: 'Delete remote configs removed locally'\n"
        "        type: boolean\n"
        "        default: false\n"
        "permissions:\n"
        "  contents: read\n"
        "jobs:\n"
        "  push:\n"
        f"    environment: {env_expr}\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        f"{_INSTALL_TOKEN}"
        "      # `sync push` encrypts #-secrets fail-closed by default. Do NOT add\n"
        "      # --allow-plaintext-on-encrypt-failure in CI.\n"
        f"{steps}"
    )


# Install steps are injected after generation so the version/ref is applied once.
_INSTALL_TOKEN = "@@INSTALL@@\n"


# --------------------------------------------------------------------------- #
# Secrets / variables checklist
# --------------------------------------------------------------------------- #


def secrets_report(projects: list[Project], repo_slug: str) -> str:
    lines: list[str] = []
    lines.append("Required GitHub secrets (per project Storage API token):")
    for p in projects:
        lines.append(
            f"  gh secret set {p.token_secret} "
            f"--repo {repo_slug}   # project {p.project_id} ({p.directory})"
        )
    lines.append("")
    lines.append("Required GitHub Environments (for `kbagent push` approval gating):")
    lines.append(f"  gh api -X PUT repos/{repo_slug}/environments/prod")
    lines.append(f"  gh api -X PUT repos/{repo_slug}/environments/dev")
    lines.append(
        "  # Then scope each KBC_TOKEN_* secret to its environment and add "
        "required reviewers to 'prod' in the GitHub UI."
    )
    lines.append("")
    lines.append(
        "No KBC_STORAGE_API_URL secret needed: it is baked from each manifest's "
        "apiHost. Override per project by editing the generated env: block."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


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


def run(args: argparse.Namespace) -> int:
    repo = Path(args.repo_dir).resolve()
    if not repo.is_dir():
        print(f"error: {repo} is not a directory", file=sys.stderr)
        return 2

    projects = discover_projects(repo)
    if not projects:
        print(
            f"error: no .keboola/manifest.json found under {repo}. "
            "Is this a kbc project-as-code repo?",
            file=sys.stderr,
        )
        return 2

    print(f"Discovered {len(projects)} project(s) in {repo}:")
    for p in projects:
        subset = ""
        if p.ignored_components:
            subset = f"  [subset: {len(p.ignored_components)} ignored component(s)]"
        print(
            f"  - {p.directory:<8} id={p.project_id:<8} host={p.api_host}"
            f"  token=secrets.{p.token_secret}{subset}"
        )

    legacy = detect_legacy_ci(repo)
    print(f"\nLegacy kbc CI/CD files detected ({len(legacy)}):")
    for f in legacy:
        print(f"  - {f}")
    if legacy:
        print(
            "  NOTE: these are NOT deleted. Review the new workflows, then remove "
            "the legacy ones in the same PR."
        )

    install = _install_steps(args.version, args.git_ref)
    files = {
        ".github/workflows/kbagent-validate.yml": gen_validate(projects),
        ".github/workflows/kbagent-pull.yml": gen_pull(projects, args.schedule),
        ".github/workflows/kbagent-push.yml": gen_push(projects, args.main_branch),
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
    print(secrets_report(projects, _guess_repo_slug(repo)))
    print("=" * 70)

    if not args.write:
        print("\nDry-run only. Re-run with --write to create the files above.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("repo_dir", help="Path to the kbc project-as-code repo to migrate")
    ap.add_argument(
        "--write", action="store_true", help="Write the generated workflows (default: dry-run)"
    )
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--version", help="Pin kbagent to this PyPI version, e.g. X.Y.Z")
    grp.add_argument("--git-ref", help="Pin kbagent to a git tag/ref, e.g. vX.Y.Z")
    ap.add_argument(
        "--main-branch",
        default="main",
        help="Branch that maps to the prod environment (default: main)",
    )
    ap.add_argument(
        "--schedule", default=None, help="Cron for scheduled pull, e.g. '0 * * * *' (default: none)"
    )
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
