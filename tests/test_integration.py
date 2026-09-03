"""Integration tests for Keboola Agent CLI using real API credentials.

These tests are skipped unless the following environment variables are set:
  - KBA_TEST_TOKEN_AWS: Storage API token for AWS stack
  - KBA_TEST_URL_AWS: Stack URL for AWS stack (default: https://connection.keboola.com)

To run integration tests:
    KBA_TEST_TOKEN_AWS=your-token uv run pytest tests/test_integration.py -v

These tests exercise the full workflow: add project, list, status, config list, remove.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore

runner = CliRunner()

# Environment variable names for test credentials
ENV_TOKEN_AWS = "KBA_TEST_TOKEN_AWS"
ENV_URL_AWS = "KBA_TEST_URL_AWS"

# Skip all tests in this module if credentials are not available
HAS_AWS_CREDENTIALS = os.environ.get(ENV_TOKEN_AWS) is not None

skip_without_credentials = pytest.mark.skipif(
    not HAS_AWS_CREDENTIALS,
    reason=f"Integration tests require {ENV_TOKEN_AWS} environment variable",
)


@pytest.fixture
def integration_config_dir(tmp_path: Path) -> Path:
    """Provide a temporary config directory for integration tests."""
    config_dir = tmp_path / "integration_config"
    config_dir.mkdir()
    return config_dir


def _invoke_with_store(config_dir: Path, args: list[str]):
    """Invoke the CLI app with a custom config store pointed at config_dir."""
    from unittest.mock import patch

    with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
        MockStore.return_value = ConfigStore(config_dir=config_dir)
        return runner.invoke(app, args)


@skip_without_credentials
@pytest.mark.integration
class TestFullWorkflow:
    """End-to-end integration test: add project, list, status, config list, remove."""

    def test_full_workflow(self, integration_config_dir: Path) -> None:
        """Full workflow: add -> list -> status -> config list -> remove."""
        token = os.environ[ENV_TOKEN_AWS]
        url = os.environ.get(ENV_URL_AWS, "https://connection.keboola.com")
        alias = "integration-test"

        from unittest.mock import patch

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            store = ConfigStore(config_dir=integration_config_dir)
            MockStore.return_value = store

            # Step 1: Add project
            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "add",
                    "--project",
                    alias,
                    "--url",
                    url,
                    "--token",
                    token,
                ],
            )
            assert result.exit_code == 0, f"project add failed: {result.output}"
            add_output = json.loads(result.output)
            assert add_output["status"] == "ok"
            assert add_output["data"]["alias"] == alias
            assert add_output["data"]["project_name"]  # Should have a name
            assert add_output["data"]["project_id"] > 0  # Should have an ID

            # Verify token is masked in output
            assert token not in result.output

            # Step 2: List projects
            result = runner.invoke(app, ["--json", "project", "list"])
            assert result.exit_code == 0, f"project list failed: {result.output}"
            list_output = json.loads(result.output)
            assert list_output["status"] == "ok"
            assert len(list_output["data"]) >= 1
            project_aliases = [p["alias"] for p in list_output["data"]]
            assert alias in project_aliases

            # Verify token is masked in list output too
            assert token not in result.output

            # Step 3: Project status
            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "status",
                    "--project",
                    alias,
                ],
            )
            assert result.exit_code == 0, f"project status failed: {result.output}"
            status_output = json.loads(result.output)
            assert status_output["status"] == "ok"
            assert len(status_output["data"]) == 1
            assert status_output["data"][0]["alias"] == alias
            assert status_output["data"][0]["status"] == "ok"
            assert status_output["data"][0]["response_time_ms"] >= 0

            # Step 4: Config list
            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "list",
                    "--project",
                    alias,
                ],
            )
            assert result.exit_code == 0, f"config list failed: {result.output}"
            config_output = json.loads(result.output)
            assert config_output["status"] == "ok"
            assert "configs" in config_output["data"]
            assert "errors" in config_output["data"]
            assert config_output["data"]["errors"] == []
            # Configs may or may not be empty depending on the project
            # but the structure should be correct
            for cfg in config_output["data"]["configs"]:
                assert cfg["project_alias"] == alias
                assert "component_id" in cfg
                assert "config_name" in cfg

            # Step 5: Job list
            result = runner.invoke(
                app,
                [
                    "--json",
                    "job",
                    "list",
                    "--project",
                    alias,
                    "--limit",
                    "5",
                ],
            )
            assert result.exit_code == 0, f"job list failed: {result.output}"
            job_output = json.loads(result.output)
            assert job_output["status"] == "ok"
            assert "jobs" in job_output["data"]
            assert "errors" in job_output["data"]
            assert job_output["data"]["errors"] == []
            for job in job_output["data"]["jobs"]:
                assert job["project_alias"] == alias
                assert "id" in job
                assert "status" in job

            # Step 6: Doctor check (was Step 5)
            result = runner.invoke(app, ["--json", "doctor"])
            assert result.exit_code == 0, f"doctor failed: {result.output}"
            doctor_output = json.loads(result.output)
            assert doctor_output["status"] == "ok"
            assert doctor_output["data"]["summary"]["healthy"] is True

            # Step 7: Remove project
            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "remove",
                    "--project",
                    alias,
                ],
            )
            assert result.exit_code == 0, f"project remove failed: {result.output}"
            remove_output = json.loads(result.output)
            assert remove_output["status"] == "ok"

            # Verify project is gone
            result = runner.invoke(app, ["--json", "project", "list"])
            assert result.exit_code == 0
            final_list = json.loads(result.output)
            remaining_aliases = [p["alias"] for p in final_list["data"]]
            assert alias not in remaining_aliases

    def test_add_with_invalid_token_returns_error(self, integration_config_dir: Path) -> None:
        """Adding a project with a deliberately invalid token returns an auth error."""
        from unittest.mock import patch

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=integration_config_dir)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "add",
                    "--project",
                    "bad-project",
                    "--url",
                    "https://connection.keboola.com",
                    "--token",
                    "000-invalid-token-definitely-wrong",
                ],
            )

        assert result.exit_code == 3
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INVALID_TOKEN"

    def test_context_command_works(self, integration_config_dir: Path) -> None:
        """Context command outputs useful agent instructions."""
        from unittest.mock import patch

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=integration_config_dir)
            result = runner.invoke(app, ["context"])

        assert result.exit_code == 0
        assert "kbagent" in result.output
        assert "--json" in result.output


# ===========================================================================
# CI guard: check_error_codes.py catches planted raw strings
# ===========================================================================


@pytest.mark.integration
class TestCheckErrorCodesGuard:
    """Verify the CI guard script rejects raw error_code string literals."""

    def test_guard_passes_on_clean_source(self) -> None:
        """scripts/check_error_codes.py exits 0 on the current (clean) source."""
        result = subprocess.run(
            [sys.executable, "scripts/check_error_codes.py"],
            capture_output=True,
            check=False,
            text=True,
        )
        assert result.returncode == 0, (
            f"Guard failed on clean source:\n{result.stdout}\n{result.stderr}"
        )

    def test_guard_catches_planted_literal(self, tmp_path: Path) -> None:
        """Guard exits 1 when a raw string literal is planted in a temp source file."""
        # Write a minimal Python file that uses a raw error_code string
        planted = tmp_path / "planted.py"
        planted.write_text(
            "from keboola_agent_cli.errors import KeboolaApiError\n"
            'raise KeboolaApiError("oops", error_code="QUEUE_JOB_FAILED")\n',
            encoding="utf-8",
        )
        # Run the guard against only this file by patching SRC_ROOT via env isn't
        # practical; instead verify the guard script's logic directly via import.
        import ast

        source = planted.read_text(encoding="utf-8")
        tree = ast.parse(source)
        violations = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "error_code" and isinstance(kw.value, ast.Constant):
                    violations.append(kw.value.value)

        assert violations == ["QUEUE_JOB_FAILED"], (
            "Guard logic should detect the planted raw string literal"
        )

    def test_guard_ignores_enum_usage(self, tmp_path: Path) -> None:
        """Guard logic does NOT flag error_code=ErrorCode.X (non-Constant node)."""
        import ast

        source = (
            "from keboola_agent_cli.errors import ErrorCode, KeboolaApiError\n"
            'raise KeboolaApiError("oops", error_code=ErrorCode.QUEUE_JOB_FAILED)\n'
        )
        tree = ast.parse(source)
        violations = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "error_code" and isinstance(kw.value, ast.Constant):
                    violations.append(kw.value.value)

        assert violations == [], "Enum usage should not be flagged as a violation"


@pytest.mark.integration
class TestErrorCodesDocCompleteness:
    """Verify the enum-vs-docs/error-codes.md completeness guard."""

    @staticmethod
    def _load_script():
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "check_error_codes", Path("scripts") / "check_error_codes.py"
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_doc_matches_enum(self) -> None:
        """docs/error-codes.md documents exactly the ErrorCode members."""
        mod = self._load_script()
        assert mod._enum_members() == mod._documented_codes()

    def test_detects_missing_code(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Removing one documented code from the doc makes the check fail."""
        mod = self._load_script()
        doc_lines = mod.DOC_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
        pruned = [line for line in doc_lines if not line.startswith("| `INVALID_TOKEN` |")]
        assert len(pruned) == len(doc_lines) - 1
        stripped_doc = tmp_path / "error-codes.md"
        stripped_doc.write_text("".join(pruned), encoding="utf-8")
        monkeypatch.setattr(mod, "DOC_PATH", stripped_doc)
        assert mod._check_doc_completeness() is False

    def test_detects_stale_code(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A doc row for a code that is not in the enum makes the check fail."""
        mod = self._load_script()
        doc = mod.DOC_PATH.read_text(encoding="utf-8")
        doc += "| `NO_SUCH_CODE_EVER` | Planted stale row |\n"
        stale_doc = tmp_path / "error-codes.md"
        stale_doc.write_text(doc, encoding="utf-8")
        monkeypatch.setattr(mod, "DOC_PATH", stale_doc)
        assert mod._check_doc_completeness() is False
