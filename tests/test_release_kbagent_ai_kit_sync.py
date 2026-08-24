"""Tests for the ``ai-kit-marketplace`` release job's marketplace rewrite.

``ai-kit-marketplace`` in ``.github/workflows/release-kbagent.yml`` is the only
job in this repo that WRITES to another repository: on every stable tag it
jq-rewrites the ``kbagent`` entry in ``keboola/ai-kit``'s
``.claude-plugin/marketplace.json`` (``version`` + ``source.ref``) and opens a
PR there. Merging that PR is what actually ships a release to
``keboola-claude-kit`` plugin users, so a silent failure here is a release that
looks green and moves nobody.

Nothing in this repo can see ai-kit's catalogue, and the entry's shape is owned
by ai-kit -- so the failure mode worth pinning is a shape change on the far
side: an entry whose ``source`` is a plain string (the shape THIS repo's own
deprecated ``.claude-plugin/marketplace.json`` still uses) cannot take a
``.source.ref`` assignment at all, and an over-eager no-op guard would skip a
real bump.

Rather than restate the shell, these tests extract the step's real ``run:``
block from the workflow and execute it with ``bash`` against a throwaway git
repo -- so editing the workflow is what changes the behaviour under test. Same
"load the real artifact by path, then exercise it" approach as
``tests/test_sync_version_script.py`` and ``tests/test_gen_release_notes.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-kbagent.yml"

JOB_ID = "ai-kit-marketplace"
STEP_ID = "bump"
MARKETPLACE_PATH = ".claude-plugin/marketplace.json"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None,
    reason="the job's rewrite is implemented in jq; skip where jq is unavailable",
)


def _bump_job() -> dict:
    """The ai-kit-marketplace job as the workflow actually declares it."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert JOB_ID in jobs, f"{WORKFLOW.name} no longer defines a '{JOB_ID}' job"
    return jobs[JOB_ID]


def _bump_script() -> str:
    """The `run:` body of the rewrite step, verbatim from the workflow."""
    steps = [s for s in _bump_job()["steps"] if s.get("id") == STEP_ID]
    assert len(steps) == 1, f"expected exactly one step with id '{STEP_ID}', found {len(steps)}"
    script = steps[0]["run"]
    # Guard the harness itself: if the rewrite stops going through jq, these
    # tests are no longer exercising what they claim to.
    assert "jq" in script, "the rewrite step no longer shells out to jq"
    return script


@pytest.fixture
def ai_kit(tmp_path: Path) -> Path:
    """A throwaway git repo standing in for a checkout of keboola/ai-kit.

    The step's own safety net is `git diff --quiet` on the rewritten file, so the
    file has to be committed for the harness to mean anything.
    """
    repo = tmp_path / "ai-kit"
    (repo / ".claude-plugin").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    return repo


def marketplace(entry: dict | None, *, extra_plugins: bool = False) -> dict:
    """An ai-kit-shaped marketplace document, optionally without a kbagent entry."""
    plugins: list[dict] = []
    if extra_plugins:
        plugins.append(
            {
                "name": "some-other-plugin",
                "version": "3.2.1",
                "source": {"source": "github", "repo": "keboola/other", "ref": "v3.2.1"},
            }
        )
    if entry is not None:
        plugins.append(entry)
    return {
        "name": "keboola-claude-kit",
        "version": "1.0.0",
        "owner": {"name": "Keboola", "email": "support@keboola.com"},
        "plugins": plugins,
    }


def kbagent_entry(version: str, ref: str | None = "same") -> dict:
    """A git-subdir kbagent entry, as ai-kit's catalogue carries it.

    ``ref=None`` drops ``source.ref`` entirely (registered but never pinned);
    the default keeps it in step with ``version``.
    """
    source: dict = {
        "source": "github",
        "repo": "keboola/cli",
        "path": "plugins/kbagent",
    }
    if ref is not None:
        source["ref"] = f"v{version}" if ref == "same" else ref
    return {
        "name": "kbagent",
        "version": version,
        "source": source,
        "description": "AI-friendly interface to Keboola Connection projects",
        "category": "development",
    }


def write_marketplace(repo: Path, document: dict) -> str:
    """Commit ``document`` as the repo's marketplace file; return its exact bytes."""
    target = repo / MARKETPLACE_PATH
    text = json.dumps(document, indent=2) + "\n"
    target.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", MARKETPLACE_PATH], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)
    return text


class BumpResult:
    """Outcome of one run of the rewrite step."""

    def __init__(self, proc: subprocess.CompletedProcess[str], repo: Path, outputs: dict[str, str]):
        self.proc = proc
        self.repo = repo
        self.outputs = outputs

    @property
    def ok(self) -> bool:
        return self.proc.returncode == 0

    @property
    def log(self) -> str:
        return self.proc.stdout + self.proc.stderr

    @property
    def changed(self) -> str | None:
        """The step's `changed` output -- gates whether a PR gets opened at all."""
        return self.outputs.get("changed")

    @property
    def document(self) -> dict:
        return json.loads((self.repo / MARKETPLACE_PATH).read_text(encoding="utf-8"))

    @property
    def raw(self) -> str:
        return (self.repo / MARKETPLACE_PATH).read_text(encoding="utf-8")

    def entry(self, name: str = "kbagent") -> dict:
        matches = [p for p in self.document["plugins"] if p["name"] == name]
        assert len(matches) == 1, f"expected one '{name}' entry, found {len(matches)}"
        return matches[0]


def run_bump(repo: Path, version: str) -> BumpResult:
    """Execute the workflow step's real shell against ``repo``."""
    job_env = _bump_job()["env"]
    # Mirror the workflow's own job-level env instead of hardcoding values here,
    # so renaming PLUGIN_NAME/AI_KIT_REPO in the workflow surfaces as a failure.
    assert job_env["PLUGIN_NAME"] == "kbagent"
    github_output = repo / "github_output"
    github_output.write_text("", encoding="utf-8")

    proc = subprocess.run(
        ["bash", "-c", _bump_script()],
        cwd=repo,
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(repo),
            "VERSION": version,
            "PLUGIN_NAME": job_env["PLUGIN_NAME"],
            "AI_KIT_REPO": job_env["AI_KIT_REPO"],
            "GITHUB_OUTPUT": str(github_output),
        },
    )
    outputs: dict[str, str] = {}
    for line in github_output.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            outputs[key] = value
    return BumpResult(proc, repo, outputs)


class TestAiKitMarketplaceJobWiring:
    """The job's declared wiring, which the shell tests below depend on."""

    def test_job_declares_the_env_the_rewrite_reads(self) -> None:
        """The rewrite reads $PLUGIN_NAME / $AI_KIT_REPO / $VERSION from job env."""
        job_env = _bump_job()["env"]
        assert job_env["PLUGIN_NAME"] == "kbagent"
        assert job_env["AI_KIT_REPO"] == "keboola/ai-kit"
        assert "VERSION" in job_env

    def test_pr_step_is_gated_on_the_rewrite_having_changed_something(self) -> None:
        """No-op safety is only real if the PR step honours `changed`."""
        steps = _bump_job()["steps"]
        pr_steps = [s for s in steps if "gh pr create" in str(s.get("run", ""))]
        assert len(pr_steps) == 1
        assert pr_steps[0]["if"] == f"steps.{STEP_ID}.outputs.changed == 'true'"


class TestAiKitMarketplaceRewrite:
    """The jq rewrite and its value-comparison no-op guard."""

    def test_no_op_when_entry_is_already_at_the_target_version(self, ai_kit: Path) -> None:
        """Entry already at this version: exit clean, changed=false, file untouched.

        jq re-emits the whole document with its own indentation, so the guard has
        to compare VALUES -- a `git diff` check alone would see pure reformatting
        and open an empty PR against ai-kit on every re-run.
        """
        before = write_marketplace(ai_kit, marketplace(kbagent_entry("0.90.0")))

        result = run_bump(ai_kit, "0.90.0")

        assert result.ok, result.log
        assert result.changed == "false"
        # Byte-identical: not merely value-equal. This is what keeps a re-tag from
        # opening a reformat-only PR.
        assert result.raw == before
        assert "already published" in result.log

    def test_genuine_bump_rewrites_both_version_and_source_ref(self, ai_kit: Path) -> None:
        """A real release rewrites `version` AND `source.ref` -- both, or the pin lies."""
        write_marketplace(ai_kit, marketplace(kbagent_entry("0.89.0"), extra_plugins=True))

        result = run_bump(ai_kit, "0.90.0")

        assert result.ok, result.log
        assert result.changed == "true"
        entry = result.entry()
        assert entry["version"] == "0.90.0"
        assert entry["source"]["ref"] == "v0.90.0"

    def test_genuine_bump_leaves_every_other_field_and_plugin_alone(self, ai_kit: Path) -> None:
        """The rewrite is surgical: only the kbagent entry's two fields move."""
        write_marketplace(ai_kit, marketplace(kbagent_entry("0.89.0"), extra_plugins=True))

        result = run_bump(ai_kit, "0.90.0")

        assert result.ok, result.log
        entry = result.entry()
        # The git-subdir coordinates must survive -- rewriting `repo` or `path`
        # would point plugin users at the wrong source tree.
        assert entry["source"]["source"] == "github"
        assert entry["source"]["repo"] == "keboola/cli"
        assert entry["source"]["path"] == "plugins/kbagent"
        assert entry["category"] == "development"
        assert entry["description"] == "AI-friendly interface to Keboola Connection projects"
        other = result.entry("some-other-plugin")
        assert other["version"] == "3.2.1"
        assert other["source"]["ref"] == "v3.2.1"
        assert result.document["name"] == "keboola-claude-kit"

    def test_stale_source_ref_alone_still_counts_as_a_bump(self, ai_kit: Path) -> None:
        """`version` current but `ref` stale must NOT be treated as a no-op.

        A guard that compares only `version` would leave the tag pin pointing at
        the previous release -- plugin users would install old code from a
        catalogue that claims the new version.
        """
        write_marketplace(ai_kit, marketplace(kbagent_entry("0.90.0", ref="v0.89.0")))

        result = run_bump(ai_kit, "0.90.0")

        assert result.ok, result.log
        assert result.changed == "true"
        assert result.entry()["source"]["ref"] == "v0.90.0"

    def test_stale_version_alone_still_counts_as_a_bump(self, ai_kit: Path) -> None:
        """`ref` current but `version` stale must NOT be treated as a no-op."""
        write_marketplace(ai_kit, marketplace(kbagent_entry("0.89.0", ref="v0.90.0")))

        result = run_bump(ai_kit, "0.90.0")

        assert result.ok, result.log
        assert result.changed == "true"
        assert result.entry()["version"] == "0.90.0"

    def test_entry_with_no_source_ref_gets_one_added(self, ai_kit: Path) -> None:
        """An entry registered without a tag pin gets pinned, not skipped."""
        write_marketplace(ai_kit, marketplace(kbagent_entry("0.90.0", ref=None)))

        result = run_bump(ai_kit, "0.90.0")

        assert result.ok, result.log
        assert result.changed == "true"
        assert result.entry()["source"]["ref"] == "v0.90.0"

    def test_fails_loudly_when_there_is_no_kbagent_entry(self, ai_kit: Path) -> None:
        """A missing entry means ai-kit was never registered -- fail, don't no-op."""
        write_marketplace(ai_kit, marketplace(None, extra_plugins=True))

        result = run_bump(ai_kit, "0.90.0")

        assert not result.ok, result.log
        assert "no plugins[] entry named 'kbagent'" in result.log
        # A failure must not tell the next step there is a PR to open.
        assert result.changed != "true"

    def test_fails_loudly_when_source_is_a_string_not_an_object(self, ai_kit: Path) -> None:
        """`source` as a plain string cannot take `.source.ref` -- fail, don't half-write.

        This is the shape THIS repo's deprecated marketplace.json still uses
        (`"source": "./plugins/kbagent"`), so it is exactly what a copy-paste into
        ai-kit would produce. jq cannot index a string, so the rewrite must abort
        with the file untouched rather than publish a version with no tag pin.
        """
        entry = kbagent_entry("0.89.0")
        entry["source"] = "./plugins/kbagent"
        before = write_marketplace(ai_kit, marketplace(entry))

        result = run_bump(ai_kit, "0.90.0")

        assert not result.ok, result.log
        assert result.changed != "true"
        # No partial write: the rewrite goes through a .tmp file and only `mv`s on
        # success, so a failed jq must leave the committed file byte-identical.
        assert result.raw == before

    def test_fails_loudly_when_the_marketplace_file_is_absent(self, ai_kit: Path) -> None:
        """No marketplace.json at all: fail rather than silently publish nothing."""
        subprocess.run(["git", "commit", "-qm", "empty", "--allow-empty"], cwd=ai_kit, check=True)

        result = run_bump(ai_kit, "0.90.0")

        assert not result.ok, result.log
        assert "has no .claude-plugin/marketplace.json" in result.log
        assert result.changed != "true"
