"""Wiring of the release workflows for a new major version (CONTRIBUTING.md >
"Releasing a new major version").

The decision logic lives in scripts/release_latest.py (tests/test_release_latest.py).
These tests pin how the workflows use it, because a wrong `if:` or a missing input
fails silently: the release stays green and the "latest" flag, PyPI or Homebrew move
at the wrong time. Same "load the real workflow by path" approach as
tests/test_release_kbagent_ai_kit_sync.py; the one shell step with logic of its own
(the version job) is executed with bash.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
RELEASE = WORKFLOWS / "release-kbagent.yml"
WHEEL = WORKFLOWS / "release.yml"
PROMOTE = WORKFLOWS / "promote-major.yml"


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def jobs(path: Path) -> dict:
    return load(path)["jobs"]


def step(job: dict, *, step_id: str | None = None, uses: str | None = None) -> dict:
    found = [
        s
        for s in job["steps"]
        if (step_id is not None and s.get("id") == step_id)
        or (uses is not None and str(s.get("uses", "")).startswith(uses))
    ]
    assert len(found) == 1, f"expected one step (id={step_id}, uses={uses}), found {len(found)}"
    return found[0]


def all_run_blocks(path: Path) -> list[str]:
    return [str(s.get("run", "")) for job in jobs(path).values() for s in job.get("steps", [])]


class TestLatestFlag:
    def test_github_release_passes_the_decided_flag_to_softprops(self) -> None:
        job = jobs(RELEASE)["github-release"]
        publish = step(job, uses="softprops/action-gh-release")
        assert publish["with"]["make_latest"] == "${{ steps.latest.outputs.make_latest }}"

    def test_the_flag_is_decided_right_before_publishing(self) -> None:
        steps = jobs(RELEASE)["github-release"]["steps"]
        ids = [s.get("id") or s.get("uses") or s.get("name") for s in steps]
        decide = ids.index("latest")
        publish = next(i for i, s in enumerate(steps) if "action-gh-release" in str(s.get("uses")))
        assert decide < publish
        assert "scripts/release_latest.py" in steps[decide]["run"]
        assert "classify" in steps[decide]["run"]

    @pytest.mark.parametrize("path", [RELEASE, WHEEL], ids=lambda p: p.name)
    def test_every_release_edit_or_create_sets_the_flag_explicitly(self, path: Path) -> None:
        """No step may leave the latest flag to the GitHub default."""
        for run in all_run_blocks(path):
            for line in run.splitlines():
                if "gh release edit" in line or "gh release create" in line:
                    assert "--latest" in line, f"{path.name}: {line.strip()}"

    def test_fill_notes_repeats_the_published_flag(self) -> None:
        job = jobs(RELEASE)["github-release"]
        fill = next(s for s in job["steps"] if s.get("name", "").startswith("Fill release notes"))
        assert fill["env"]["MAKE_LATEST"] == "${{ steps.latest.outputs.make_latest }}"
        assert '--latest="$MAKE_LATEST"' in fill["run"]


class TestScriptSteps:
    def test_steps_that_pipe_the_script_into_tee_use_pipefail(self) -> None:
        """The default `bash -e` has no pipefail: a failed script would leave an empty
        output, and an empty make_latest makes GitHub mark the release latest."""
        steps = [
            step(jobs(RELEASE)["version"], step_id="line"),
            step(jobs(RELEASE)["github-release"], step_id="latest"),
            step(jobs(PROMOTE)["decide"], step_id="promote"),
        ]
        for s in steps:
            assert "| tee" in s["run"]
            assert s["shell"] == "bash", s["id"]


class TestMajorStartAndOldLine:
    def test_pypi_waits_for_github_release_and_needs_it_on_a_major_start(self) -> None:
        pypi = jobs(RELEASE)["pypi"]
        assert "github-release" in pypi["needs"]
        condition = " ".join(pypi["if"].split())
        assert condition.startswith("!cancelled()")
        assert "needs.gate.result == 'success'" in condition
        assert (
            "(needs.version.outputs.MAJOR_START == 'false' "
            "|| needs.github-release.result == 'success')"
        ) in condition

    @pytest.mark.parametrize("job_id", ["homebrew", "ai-kit-marketplace"])
    def test_old_line_tags_skip_single_version_channels(self, job_id: str) -> None:
        assert "needs.version.outputs.OLD_LINE == 'false'" in jobs(RELEASE)[job_id]["if"]

    @pytest.mark.parametrize("job_id", ["chocolatey", "winget", "publish-s3"])
    def test_multi_version_channels_still_get_old_line_tags(self, job_id: str) -> None:
        assert "OLD_LINE" not in str(jobs(RELEASE)[job_id].get("if", ""))

    def test_version_job_exposes_old_line_from_the_script(self) -> None:
        version = jobs(RELEASE)["version"]
        assert version["outputs"]["OLD_LINE"] == "${{ steps.line.outputs.old_line }}"
        assert "scripts/release_latest.py" in step(version, step_id="line")["run"]

    @pytest.mark.skipif(
        sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash"
    )
    @pytest.mark.parametrize(
        ("ref", "expected"),
        [
            ("v1.0.0", "true"),
            ("v2.0.0", "true"),
            ("v10.0.0", "true"),
            ("v0.0.0", "false"),
            ("v1.0.1", "false"),
            ("v1.1.0", "false"),
            ("v0.105.1", "false"),
            ("v1.0.0rc1", "false"),
        ],
    )
    def test_major_start_output(self, tmp_path: Path, ref: str, expected: str) -> None:
        script = step(jobs(RELEASE)["version"], step_id="v")["run"]
        output = tmp_path / "out"
        proc = subprocess.run(
            ["bash", "-e", "-c", script],
            capture_output=True,
            text=True,
            check=False,
            env={
                "PATH": os.environ.get("PATH", ""),
                "REF": ref,
                "INPUT": "",
                "S3_PREFIX": "keboola-cli2",
                "GITHUB_OUTPUT": str(output),
            },
        )
        assert proc.returncode == 0, proc.stderr
        lines = dict(line.split("=", 1) for line in output.read_text().splitlines())
        assert lines["MAJOR_START"] == expected


class TestInstallTest:
    def test_apt_and_dnf_install_the_exact_version_on_a_stable_tag(self) -> None:
        job = jobs(RELEASE)["test-install"]
        assert job["env"]["IS_PRERELEASE"] == "${{ needs.version.outputs.IS_PRERELEASE }}"
        runs = {s["name"]: s["run"] for s in job["steps"]}
        assert 'pkg="keboola-cli2=${VERSION}"' in runs["apt (Ubuntu)"]
        assert 'apt-get install -y "$pkg"' in runs["apt (Ubuntu)"]
        assert 'pkg="keboola-cli2-${VERSION}"' in runs["rpm (Fedora)"]
        assert 'dnf install -y "$pkg"' in runs["rpm (Fedora)"]
        for name in ("apt (Ubuntu)", "rpm (Fedora)"):
            assert "-e IS_PRERELEASE" in runs[name]

    @pytest.mark.skipif(
        sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash"
    )
    @pytest.mark.parametrize(
        ("name", "version", "prerelease", "expected"),
        [
            ("apt (Ubuntu)", "0.105.2", "false", "keboola-cli2=0.105.2"),
            ("apt (Ubuntu)", "0.105.2.post1", "false", "keboola-cli2"),
            ("apt (Ubuntu)", "0.0.1-dev.1", "true", "keboola-cli2"),
            ("rpm (Fedora)", "1.0.0", "false", "keboola-cli2-1.0.0"),
            ("rpm (Fedora)", "1.0.0.post2", "false", "keboola-cli2"),
            ("rpm (Fedora)", "1.1.0rc1", "true", "keboola-cli2"),
        ],
    )
    def test_package_selection(
        self, name: str, version: str, prerelease: str, expected: str
    ) -> None:
        """Runs the real selection lines; a .postN tag stays unpinned (nfpm may rewrite it)."""
        run = {s["name"]: s["run"] for s in jobs(RELEASE)["test-install"]["steps"]}[name]
        lines = [line.strip() for line in run.splitlines()]
        selection = [line for line in lines if line.startswith(("pkg=", "case "))]
        assert len(selection) == 2, selection
        proc = subprocess.run(
            ["bash", "-c", "\n".join([*selection, 'printf %s "$pkg"'])],
            capture_output=True,
            text=True,
            check=True,
            env={"VERSION": version, "IS_PRERELEASE": prerelease, "PATH": os.environ["PATH"]},
        )
        assert proc.stdout == expected


class TestCheckoutCredentials:
    def test_new_sparse_checkouts_do_not_persist_the_token(self) -> None:
        checkouts = [
            step(jobs(RELEASE)["version"], uses="actions/checkout"),
            step(jobs(RELEASE)["github-release"], uses="actions/checkout"),
            step(jobs(PROMOTE)["decide"], uses="actions/checkout"),
        ]
        for checkout in checkouts:
            assert checkout["with"]["persist-credentials"] is False


class TestPromoteMajor:
    def test_runs_hourly_and_on_demand(self) -> None:
        triggers = load(PROMOTE)[True]  # PyYAML reads the `on:` key as True
        assert triggers["schedule"] == [{"cron": "17 * * * *"}]
        assert "workflow_dispatch" in triggers

    def test_minimal_permissions(self) -> None:
        workflow = load(PROMOTE)
        assert workflow["permissions"] == {"contents": "read"}
        assert "permissions" not in workflow["jobs"]["decide"]
        assert workflow["jobs"]["promote"]["permissions"] == {"contents": "write"}

    def test_writes_only_when_the_decision_names_a_tag(self) -> None:
        workflow = load(PROMOTE)
        assert workflow["jobs"]["decide"]["outputs"]["TAG"] == "${{ steps.promote.outputs.tag }}"
        assert workflow["jobs"]["promote"]["if"] == "needs.decide.outputs.TAG != ''"
        assert 'gh release edit "$TAG" --latest' in all_run_blocks(PROMOTE)[-1]

    def test_no_expression_inside_run_blocks(self) -> None:
        """Values reach the shell through env, never through `${{ }}` in the script."""
        for run in all_run_blocks(PROMOTE):
            assert "${{" not in run

    def test_new_release_steps_have_no_expression_inside_run_blocks(self) -> None:
        release = jobs(RELEASE)
        new_steps = [
            step(release["version"], step_id="line"),
            step(release["github-release"], step_id="latest"),
            *(
                s
                for s in release["github-release"]["steps"]
                if s.get("name", "").startswith("Fill release notes")
            ),
            *release["test-install"]["steps"],
        ]
        for s in new_steps:
            assert "${{" not in str(s.get("run", "")), s.get("name") or s.get("id")
