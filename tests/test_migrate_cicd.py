"""Tests for the kbagent-cicd-migration skill's generator script.

Imported by path since the script lives under plugins/, not src/.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SCRIPT = (
    Path(__file__).parent.parent
    / "plugins/kbagent/skills/kbagent-cicd-migration/scripts/migrate_cicd.py"
)
_spec = importlib.util.spec_from_file_location("migrate_cicd", _SCRIPT)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _mod
_spec.loader.exec_module(_mod)

Project = _mod.Project
discover_projects = _mod.discover_projects
detect_legacy_ci = _mod.detect_legacy_ci
_alias_from_dir = _mod._alias_from_dir
_project_step = _mod._project_step


def _project(directory: str = "L0") -> Project:
    return Project(
        alias=_alias_from_dir(directory),
        directory=directory,
        project_id="9996",
        api_host="connection.keboola.com",
    )


def _write_manifest(repo: Path, directory: str, project_id: int = 9996) -> None:
    manifest_dir = repo / directory / ".keboola" if directory != "." else repo / ".keboola"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / "manifest.json").write_text(
        json.dumps({"project": {"id": project_id, "apiHost": "connection.keboola.com"}}),
        encoding="utf-8",
    )


class TestAliasFromDir:
    def test_root_project_gets_project_alias(self) -> None:
        assert _alias_from_dir(".") == "PROJECT"
        assert _alias_from_dir("") == "PROJECT"

    def test_nested_path_uses_whole_path_not_just_last_segment(self) -> None:
        assert _alias_from_dir("env/prod") != _alias_from_dir("other/prod")

    def test_sanitizes_non_alnum_to_underscore(self) -> None:
        assert _alias_from_dir("L0") == "L0"
        assert _alias_from_dir("a/b-c") == "A_B_C"


class TestDiscoverProjects:
    def test_finds_multi_project_layout(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, "L0", 9996)
        _write_manifest(tmp_path, "L1", 9997)
        projects = discover_projects(tmp_path)
        assert {p.directory for p in projects} == {"L0", "L1"}
        assert {p.project_id for p in projects} == {"9996", "9997"}

    def test_skips_manifest_missing_project_id(self, tmp_path: Path) -> None:
        manifest_dir = tmp_path / "L0" / ".keboola"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "manifest.json").write_text(json.dumps({"project": {}}), encoding="utf-8")
        assert discover_projects(tmp_path) == []

    def test_skips_invalid_json(self, tmp_path: Path) -> None:
        manifest_dir = tmp_path / "L0" / ".keboola"
        manifest_dir.mkdir(parents=True)
        (manifest_dir / "manifest.json").write_text("{not valid", encoding="utf-8")
        assert discover_projects(tmp_path) == []

    def test_rejects_unsafe_directory_name(self, tmp_path: Path) -> None:
        """Regression test for the shell-injection finding: a directory name
        containing shell metacharacters must never reach a generated workflow."""
        _write_manifest(tmp_path, "foo'; touch pwned; echo '")
        assert discover_projects(tmp_path) == []


class TestDetectLegacyCi:
    def test_detects_kbc_command_usage(self, tmp_path: Path) -> None:
        workflows = tmp_path / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text("run: kbc pull --force\n", encoding="utf-8")
        assert detect_legacy_ci(tmp_path) == [".github/workflows/ci.yml"]

    def test_detects_legacy_env_var(self, tmp_path: Path) -> None:
        workflows = tmp_path / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text("env:\n  KBC_STORAGE_API_TOKEN: x\n", encoding="utf-8")
        assert detect_legacy_ci(tmp_path) == [".github/workflows/ci.yml"]

    def test_no_github_dir_returns_empty(self, tmp_path: Path) -> None:
        assert detect_legacy_ci(tmp_path) == []

    def test_clean_kbagent_workflow_not_flagged(self, tmp_path: Path) -> None:
        workflows = tmp_path / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text("run: kbagent sync pull --force\n", encoding="utf-8")
        assert detect_legacy_ci(tmp_path) == []


class TestProjectStepShellSafety:
    """Regression coverage for the shell-injection finding: a directory name
    containing shell metacharacters must render as a single safely-quoted
    argument, never break out of the --directory value."""

    def test_directory_with_single_quote_is_shell_safe(self) -> None:
        import shlex

        malicious = "foo'; touch pwned; echo '"
        p = _project(malicious)
        step = _project_step(p, "pull --force", "Pull foo")
        # The raw unescaped payload must never appear bare after --directory --
        # only inside shlex.quote()'s properly '\''-escaped form.
        assert f"--directory {malicious}\n" not in step
        assert f"--directory {shlex.quote(malicious)}\n" in step

    def test_plain_directory_is_rendered_unquoted_but_safely(self) -> None:
        p = _project("L0")
        step = _project_step(p, "pull --force", "Pull L0")
        assert "--directory L0" in step
