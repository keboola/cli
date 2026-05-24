"""End-to-end integration test for sync git-branching workflow.

Tests the full lifecycle:
  init → pull → git branch → branch-link → edit → push → verify isolation
  → merge (if API available) → pull production → verify

Requires environment variables:
  - KBA_TEST_TOKEN_AWS: Storage API token
  - KBA_TEST_URL_AWS: Stack URL (default: https://connection.keboola.com)

Run with:
    KBA_TEST_TOKEN_AWS=your-token uv run pytest tests/test_sync_e2e.py -v -s
"""

import contextlib
import json
import os
import subprocess
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.client import KeboolaClient
from keboola_agent_cli.config_store import ConfigStore

runner = CliRunner()

ENV_TOKEN = "KBA_TEST_TOKEN_AWS"
ENV_URL = "KBA_TEST_URL_AWS"

HAS_CREDENTIALS = os.environ.get(ENV_TOKEN) is not None

skip_without_credentials = pytest.mark.skipif(
    not HAS_CREDENTIALS,
    reason=f"Requires {ENV_TOKEN} environment variable",
)


def _git(cwd: Path, *args: str) -> str:
    """Run a git command and return stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _invoke(config_dir: Path, args: list[str]) -> Any:
    """Invoke the CLI with a custom config store."""
    with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
        MockStore.return_value = ConfigStore(config_dir=config_dir)
        return runner.invoke(app, args, catch_exceptions=False)


@skip_without_credentials
@pytest.mark.integration
class TestSyncGitBranchingE2E:
    """Full git-branching sync lifecycle test."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> None:
        """Set up test directories and credentials."""
        self.token = os.environ[ENV_TOKEN]
        self.url = os.environ.get(ENV_URL, "https://connection.keboola.com")
        self.alias = "e2e-test"
        self.project_root = tmp_path / "project"
        self.project_root.mkdir()
        self.config_dir = tmp_path / "config"
        self.config_dir.mkdir()

        # Register the project in kbagent config
        result = _invoke(
            self.config_dir,
            [
                "--json",
                "project",
                "add",
                "--project",
                self.alias,
                "--url",
                self.url,
                "--token",
                self.token,
            ],
        )
        assert result.exit_code == 0, f"project add failed: {result.output}"

        # Track created Keboola branch IDs for cleanup
        self._created_branch_ids: list[int] = []

    @pytest.fixture(autouse=True)
    def cleanup_branches(self) -> Generator[None, None, None]:
        """Clean up any Keboola dev branches created during the test."""
        yield
        client = KeboolaClient(stack_url=self.url, token=self.token)
        with client:
            for branch_id in self._created_branch_ids:
                with contextlib.suppress(Exception):
                    client.delete_dev_branch(branch_id)

    def _invoke(self, args: list[str]) -> Any:
        return _invoke(self.config_dir, args)

    def _find_any_config(self) -> tuple[Path, dict]:
        """Find any _config.yml in the project tree and return (path, data)."""
        configs = list(self.project_root.rglob("_config.yml"))
        assert configs, "No _config.yml files found after pull"
        # Pick one that has a 'name' field
        for cfg_path in configs:
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
            if data and "name" in data:
                return cfg_path, data
        # Fallback to first one
        cfg_path = configs[0]
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        return cfg_path, data

    def _get_config_name_from_api(
        self, component_id: str, config_id: str, branch_id: int | None = None
    ) -> str:
        """Fetch a config name directly from Keboola API."""
        client = KeboolaClient(stack_url=self.url, token=self.token)
        with client:
            components = client.list_components_with_configs(branch_id=branch_id)
        for comp in components:
            if comp.get("id") == component_id:
                for cfg in comp.get("configurations", []):
                    if str(cfg.get("id")) == str(config_id):
                        return cfg.get("name", "")
        return ""

    def _step(self, num: int, title: str, detail: str = "") -> None:
        """Print a progress line for an E2E step."""
        suffix = f"  ({detail})" if detail else ""
        print(f"\n  [{num:>2}/10] {title}{suffix}")

    def test_full_git_branching_lifecycle(self) -> None:
        """Test: init -> pull -> branch -> link -> edit -> push -> verify -> cleanup."""
        print()  # blank line before steps

        # ----------------------------------------------------------
        self._step(1, "git init + sync init --git-branching")
        # ----------------------------------------------------------
        _git(self.project_root, "init")
        _git(self.project_root, "config", "user.email", "test@example.com")
        _git(self.project_root, "config", "user.name", "Test")

        result = self._invoke(
            [
                "sync",
                "init",
                "--project",
                self.alias,
                "--directory",
                str(self.project_root),
                "--git-branching",
            ]
        )
        assert result.exit_code == 0, f"sync init failed: {result.output}"

        manifest_path = self.project_root / ".keboola" / "manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        assert manifest["gitBranching"]["enabled"] is True

        mapping_path = self.project_root / ".keboola" / "branch-mapping.json"
        assert mapping_path.exists()
        mapping = json.loads(mapping_path.read_text())
        git_branch = _git(self.project_root, "branch", "--show-current")
        assert git_branch in mapping["mappings"]
        assert mapping["mappings"][git_branch]["id"] is None  # production
        print(f"         manifest OK, git branch '{git_branch}' -> production")

        # ----------------------------------------------------------
        self._step(2, "Pull production configs")
        # ----------------------------------------------------------
        result = self._invoke(
            [
                "sync",
                "pull",
                "--project",
                self.alias,
                "--directory",
                str(self.project_root),
            ]
        )
        assert result.exit_code == 0, f"sync pull failed: {result.output}"

        config_files = list(self.project_root.rglob("_config.yml"))
        assert len(config_files) > 0, "No configs pulled"
        print(f"         {len(config_files)} config files pulled")

        # ----------------------------------------------------------
        self._step(3, "Create git branch + link to Keboola dev branch")
        # ----------------------------------------------------------
        _git(self.project_root, "add", "-A")
        _git(self.project_root, "commit", "-m", "initial sync")
        _git(self.project_root, "checkout", "-b", "feature/e2e-test")

        result = self._invoke(
            [
                "sync",
                "branch-link",
                "--project",
                self.alias,
                "--directory",
                str(self.project_root),
            ]
        )
        assert result.exit_code == 0, f"branch-link failed: {result.output}"

        mapping = json.loads(mapping_path.read_text())
        entry = mapping["mappings"].get("feature/e2e-test", {})
        kbc_branch_id = entry.get("id")
        assert kbc_branch_id is not None, "Branch link did not create Keboola branch"
        self._created_branch_ids.append(int(kbc_branch_id))

        result = self._invoke(
            [
                "--json",
                "sync",
                "branch-status",
                "--directory",
                str(self.project_root),
            ]
        )
        assert result.exit_code == 0
        status = json.loads(result.output)
        assert status["data"]["linked"] is True
        assert status["data"]["git_branch"] == "feature/e2e-test"
        print(f"         feature/e2e-test -> Keboola branch {kbc_branch_id}")

        # ----------------------------------------------------------
        self._step(4, "Edit a config locally")
        # ----------------------------------------------------------
        cfg_path, cfg_data = self._find_any_config()
        original_name = cfg_data.get("name", "")
        test_name = f"{original_name} E2E-TEST-{int(time.time())}"
        cfg_data["name"] = test_name
        cfg_path.write_text(
            yaml.dump(cfg_data, default_flow_style=False, allow_unicode=True),
            encoding="utf-8",
        )

        rel_path = cfg_path.relative_to(self.project_root)
        parts = list(rel_path.parts)
        manifest_data = json.loads(manifest_path.read_text())
        config_component_id = None
        config_id = None
        for cfg_entry in manifest_data.get("configurations", []):
            cfg_dir = self.project_root / parts[0] / cfg_entry["path"]
            if cfg_dir == cfg_path.parent:
                config_component_id = cfg_entry["componentId"]
                config_id = cfg_entry["id"]
                break
        assert config_component_id, f"Could not find component for {cfg_path}"
        assert config_id, f"Could not find config ID for {cfg_path}"
        print(f"         '{original_name}' -> '{test_name}'")
        print(f"         component: {config_component_id}, config: {config_id}")

        # ----------------------------------------------------------
        self._step(5, "Diff shows local change")
        # ----------------------------------------------------------
        result = self._invoke(
            [
                "--json",
                "sync",
                "diff",
                "--project",
                self.alias,
                "--directory",
                str(self.project_root),
            ]
        )
        assert result.exit_code == 0, f"sync diff failed: {result.output}"
        diff_data = json.loads(result.output)
        n_modified = diff_data["data"]["summary"]["modified"]
        assert n_modified >= 1
        print(f"         {n_modified} modified config(s) detected")

        # ----------------------------------------------------------
        self._step(6, "Push to Keboola dev branch")
        # ----------------------------------------------------------
        result = self._invoke(
            [
                "sync",
                "push",
                "--project",
                self.alias,
                "--directory",
                str(self.project_root),
            ]
        )
        assert result.exit_code == 0, f"sync push failed: {result.output}"
        assert "updated" in result.output.lower() or "created" in result.output.lower()
        print(f"         {result.output.strip()}")

        # ----------------------------------------------------------
        self._step(7, "Verify: change in dev branch, NOT in production")
        # ----------------------------------------------------------
        dev_name = self._get_config_name_from_api(
            config_component_id, config_id, branch_id=int(kbc_branch_id)
        )
        assert dev_name == test_name, (
            f"Dev branch config name mismatch: expected '{test_name}', got '{dev_name}'"
        )
        print(f"         dev branch {kbc_branch_id}: name = '{dev_name}' -- OK")

        prod_name = self._get_config_name_from_api(config_component_id, config_id, branch_id=None)
        assert prod_name != test_name, (
            f"Production should NOT have the test name '{test_name}' but it does!"
        )
        assert prod_name == original_name, (
            f"Production name changed unexpectedly: expected '{original_name}', got '{prod_name}'"
        )
        print(f"         production: name = '{prod_name}' (unchanged) -- OK")

        # ----------------------------------------------------------
        self._step(8, "Diff after push is clean")
        # ----------------------------------------------------------
        result = self._invoke(
            [
                "--json",
                "sync",
                "diff",
                "--project",
                self.alias,
                "--directory",
                str(self.project_root),
            ]
        )
        assert result.exit_code == 0
        diff_data = json.loads(result.output)
        assert diff_data["data"]["summary"]["modified"] == 0, (
            f"Diff not clean after push: {diff_data['data']['summary']}"
        )
        print("         no differences -- OK")

        # ----------------------------------------------------------
        self._step(9, f"Switch to {git_branch}, pull production, verify isolation")
        # ----------------------------------------------------------
        _git(self.project_root, "checkout", git_branch)

        result = self._invoke(
            [
                "sync",
                "pull",
                "--project",
                self.alias,
                "--directory",
                str(self.project_root),
            ]
        )
        assert result.exit_code == 0

        cfg_data_after = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert cfg_data_after["name"] == original_name, (
            f"Main branch should still have '{original_name}', got '{cfg_data_after['name']}'"
        )
        print(f"         config name on {git_branch} = '{original_name}' (unchanged) -- OK")

        # ----------------------------------------------------------
        self._step(10, "Unlinked branch is blocked")
        # ----------------------------------------------------------
        _git(self.project_root, "checkout", "-b", "feature/unlinked-test")

        result = self._invoke(
            [
                "sync",
                "diff",
                "--project",
                self.alias,
                "--directory",
                str(self.project_root),
            ]
        )
        assert result.exit_code != 0, "Unlinked branch should fail"
        assert "not linked" in result.output.lower()
        print("         sync diff blocked with 'not linked' error -- OK")

        print("\n  All 10 steps passed!")

        # Go back to main for cleanup
        _git(self.project_root, "checkout", git_branch)
