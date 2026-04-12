"""Tests for the --hint code generation system."""

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.hints import HintRegistry, render_hint
from keboola_agent_cli.hints.models import (
    ClientCall,
    CommandHint,
    HintMode,
    HintStep,
    ServiceCall,
)
from keboola_agent_cli.hints.renderer import ClientRenderer, ServiceRenderer

runner = CliRunner()

STACK_URL = "https://connection.eu-central-1.keboola.com"
CONFIG_DIR = Path("/tmp/test-config")


# ── Renderer unit tests ────────────────────────────────────────────


class TestClientRenderer:
    """Tests for ClientRenderer code generation."""

    def test_simple_command_produces_valid_python(self) -> None:
        """Generated code must be syntactically valid Python."""
        hint = CommandHint(
            cli_command="config.list",
            description="List configurations",
            steps=[
                HintStep(
                    comment="List components",
                    client=ClientCall(
                        method="list_components",
                        args={"component_type": "{component_type}"},
                        result_var="components",
                    ),
                ),
            ],
        )
        code = ClientRenderer.render(
            hint,
            params={"component_type": "extractor", "project": "myproj"},
            stack_url=STACK_URL,
            branch_id=None,
        )
        # Must compile without errors
        compile(code, "<hint>", "exec")

    def test_includes_stack_url(self) -> None:
        """Generated code includes the resolved stack URL."""
        hint = _simple_hint()
        code = ClientRenderer.render(hint, {}, stack_url=STACK_URL, branch_id=None)
        assert STACK_URL in code

    def test_uses_placeholder_when_no_stack_url(self) -> None:
        """Falls back to placeholder URL when project not resolved."""
        hint = _simple_hint()
        code = ClientRenderer.render(hint, {}, stack_url=None, branch_id=None)
        assert "https://connection.keboola.com" in code

    def test_never_contains_real_token(self) -> None:
        """Token must always be an env var reference, never a real value."""
        hint = _simple_hint()
        code = ClientRenderer.render(hint, {}, stack_url=STACK_URL, branch_id=None)
        assert "KBC_STORAGE_TOKEN" in code
        assert 'os.environ["KBC_STORAGE_TOKEN"]' in code
        # No hardcoded token patterns
        assert "901-" not in code

    def test_branch_id_injected(self) -> None:
        """Branch ID is passed to client methods when provided."""
        hint = CommandHint(
            cli_command="config.list",
            description="List configs",
            steps=[
                HintStep(
                    comment="List components",
                    client=ClientCall(
                        method="list_components",
                        args={"branch_id": "{branch}"},
                        result_var="components",
                    ),
                ),
            ],
        )
        code = ClientRenderer.render(
            hint, params={"branch": 789}, stack_url=STACK_URL, branch_id=789
        )
        assert "branch_id=789" in code

    def test_none_params_omitted(self) -> None:
        """Parameters with None values are not included in method calls."""
        hint = CommandHint(
            cli_command="config.list",
            description="List configs",
            steps=[
                HintStep(
                    comment="List components",
                    client=ClientCall(
                        method="list_components",
                        args={
                            "component_type": "{component_type}",
                            "branch_id": "{branch}",
                        },
                        result_var="components",
                    ),
                ),
            ],
        )
        code = ClientRenderer.render(
            hint,
            params={"component_type": None, "branch": None},
            stack_url=STACK_URL,
            branch_id=None,
        )
        assert "component_type" not in code
        assert "branch_id" not in code

    def test_original_command_in_docstring(self) -> None:
        """Docstring includes the reconstructed original CLI command."""
        hint = _simple_hint()
        code = ClientRenderer.render(
            hint,
            params={"project": "myproj"},
            stack_url=STACK_URL,
            branch_id=None,
        )
        assert "kbagent config test --project myproj" in code

    def test_manage_client_import(self) -> None:
        """Manage API commands use ManageClient with correct token env var."""
        hint = CommandHint(
            cli_command="org.setup",
            description="Setup org",
            steps=[
                HintStep(
                    comment="List projects",
                    client=ClientCall(
                        method="list_organization_projects",
                        args={"org_id": "123"},
                        client_type="manage",
                        result_var="projects",
                    ),
                ),
            ],
        )
        code = ClientRenderer.render(hint, {}, stack_url=STACK_URL, branch_id=None)
        assert "ManageClient" in code
        assert "KBC_MANAGE_API_TOKEN" in code
        assert "KeboolaClient" not in code

    def test_poll_loop_rendering(self) -> None:
        """Poll loop steps generate a while loop with sleep."""
        hint = CommandHint(
            cli_command="job.run",
            description="Run a job and wait",
            steps=[
                HintStep(
                    comment="Create job",
                    client=ClientCall(
                        method="create_job",
                        args={"component_id": '"keboola.ex-http"', "config_id": '"123"'},
                        result_var="job",
                    ),
                ),
                HintStep(
                    comment="Poll until job completes",
                    client=ClientCall(
                        method="get_job_detail",
                        args={"job_id": 'str(job["id"])'},
                        result_var="job",
                    ),
                    kind="poll_loop",
                    poll_interval=5.0,
                    poll_condition='not job.get("isFinished")',
                ),
            ],
        )
        code = ClientRenderer.render(hint, {}, stack_url=STACK_URL, branch_id=None)
        assert "while not job.get" in code
        assert "time.sleep(5.0)" in code
        assert "import time" in code
        compile(code, "<hint>", "exec")

    def test_notes_appended(self) -> None:
        """Notes are appended as comments at the end."""
        hint = CommandHint(
            cli_command="test.cmd",
            description="Test",
            steps=[_simple_step()],
            notes=["This is a test note."],
        )
        code = ClientRenderer.render(hint, {}, stack_url=STACK_URL, branch_id=None)
        assert "# NOTE: This is a test note." in code


class TestServiceRenderer:
    """Tests for ServiceRenderer code generation."""

    def test_simple_command_produces_valid_python(self) -> None:
        """Generated service-layer code must be syntactically valid Python."""
        hint = CommandHint(
            cli_command="config.list",
            description="List configurations",
            steps=[
                HintStep(
                    comment="List configs",
                    client=ClientCall(
                        method="list_components",
                        result_var="result",
                    ),
                    service=ServiceCall(
                        service_class="ConfigService",
                        service_module="config_service",
                        method="list_configs",
                        args={"aliases": "{project}"},
                    ),
                ),
            ],
        )
        code = ServiceRenderer.render(
            hint,
            params={"project": ["myproj"]},
            stack_url=STACK_URL,
            config_dir=CONFIG_DIR,
            branch_id=None,
        )
        compile(code, "<hint>", "exec")

    def test_includes_config_dir(self) -> None:
        """Generated code includes the explicit config_dir path."""
        hint = _service_hint()
        code = ServiceRenderer.render(
            hint,
            params={"project": ["myproj"]},
            stack_url=STACK_URL,
            config_dir=CONFIG_DIR,
            branch_id=None,
        )
        assert str(CONFIG_DIR) in code
        assert "ConfigStore" in code
        assert "config_dir=Path" in code

    def test_falls_back_to_client_when_no_service(self) -> None:
        """Commands without service equivalent show client code with a note."""
        hint = CommandHint(
            cli_command="branch.create",
            description="Create dev branch",
            steps=[
                HintStep(
                    comment="Create branch",
                    client=ClientCall(
                        method="create_dev_branch",
                        args={"name": '"my-branch"'},
                        result_var="branch",
                    ),
                    service=None,
                ),
            ],
        )
        code = ServiceRenderer.render(
            hint, {}, stack_url=STACK_URL, config_dir=CONFIG_DIR, branch_id=None
        )
        assert "No service-layer equivalent" in code
        assert "KeboolaClient" in code

    def test_never_contains_real_token(self) -> None:
        """Service layer code should not contain any token references."""
        hint = _service_hint()
        code = ServiceRenderer.render(
            hint,
            params={"project": ["myproj"]},
            stack_url=STACK_URL,
            config_dir=CONFIG_DIR,
            branch_id=None,
        )
        assert "901-" not in code


# ── HintRegistry tests ─────────────────────────────────────────────


class TestHintRegistry:
    """Tests for the hint registry."""

    def test_config_hints_registered(self) -> None:
        """Config commands should be registered after importing definitions."""
        from keboola_agent_cli.hints import definitions as _  # noqa: F401

        assert HintRegistry.get("config.list") is not None
        assert HintRegistry.get("config.detail") is not None
        assert HintRegistry.get("config.search") is not None

    def test_get_nonexistent_returns_none(self) -> None:
        """Querying an unregistered command returns None."""
        assert HintRegistry.get("nonexistent.command") is None

    def test_all_commands_returns_sorted(self) -> None:
        """all_commands() returns a sorted list."""
        commands = HintRegistry.all_commands()
        assert commands == sorted(commands)
        assert len(commands) >= 3  # At least config.list/detail/search


# ── render_hint integration tests ──────────────────────────────────


class TestRenderHint:
    """Tests for the public render_hint() function."""

    def test_client_mode(self) -> None:
        """render_hint with CLIENT mode produces KeboolaClient code."""
        code = render_hint(
            "config.list",
            HintMode.CLIENT,
            params={"project": ["myproj"]},
            stack_url=STACK_URL,
            config_dir=CONFIG_DIR,
            branch_id=None,
        )
        assert "KeboolaClient" in code
        compile(code, "<hint>", "exec")

    def test_service_mode(self) -> None:
        """render_hint with SERVICE mode produces ConfigService code."""
        code = render_hint(
            "config.list",
            HintMode.SERVICE,
            params={"project": ["myproj"]},
            stack_url=STACK_URL,
            config_dir=CONFIG_DIR,
            branch_id=None,
        )
        assert "ConfigService" in code
        compile(code, "<hint>", "exec")

    def test_unknown_command_raises(self) -> None:
        """render_hint raises ValueError for unknown commands."""
        with pytest.raises(ValueError, match="No hint available"):
            render_hint(
                "nonexistent.cmd",
                HintMode.CLIENT,
                params={},
                stack_url=None,
                config_dir=None,
                branch_id=None,
            )


# ── CLI integration tests ──────────────────────────────────────────


class TestHintCLI:
    """End-to-end tests via CliRunner."""

    def test_hint_client_config_list(self, tmp_path: Path) -> None:
        """kbagent --hint client config list produces Python code on stdout."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        result = runner.invoke(
            app,
            ["--config-dir", str(config_dir), "--hint", "client", "config", "list"],
        )
        assert result.exit_code == 0
        assert "KeboolaClient" in result.stdout
        assert "list_components" in result.stdout
        compile(result.stdout, "<hint>", "exec")

    def test_hint_service_config_list(self, tmp_path: Path) -> None:
        """kbagent --hint service config list produces service layer code."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        result = runner.invoke(
            app,
            ["--config-dir", str(config_dir), "--hint", "service", "config", "list"],
        )
        assert result.exit_code == 0
        assert "ConfigService" in result.stdout
        assert "ConfigStore" in result.stdout

    def test_hint_invalid_value(self, tmp_path: Path) -> None:
        """kbagent --hint invalid exits with code 2."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        result = runner.invoke(
            app,
            ["--config-dir", str(config_dir), "--hint", "invalid", "config", "list"],
        )
        assert result.exit_code == 2

    def test_hint_config_detail_with_params(self, tmp_path: Path) -> None:
        """Parameters are correctly substituted in hint output."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        result = runner.invoke(
            app,
            [
                "--config-dir",
                str(config_dir),
                "--hint",
                "client",
                "config",
                "detail",
                "--project",
                "myproj",
                "--component-id",
                "keboola.ex-db-snowflake",
                "--config-id",
                "42",
            ],
        )
        assert result.exit_code == 0
        assert "keboola.ex-db-snowflake" in result.stdout
        assert '"42"' in result.stdout
        assert "get_config_detail" in result.stdout

    def test_hint_does_not_call_api(self, tmp_path: Path) -> None:
        """Hint mode must not make any HTTP calls."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.client.KeboolaClient") as MockClient:
            result = runner.invoke(
                app,
                [
                    "--config-dir",
                    str(config_dir),
                    "--hint",
                    "client",
                    "config",
                    "list",
                ],
            )
            # No client should have been instantiated
            MockClient.assert_not_called()

        assert result.exit_code == 0

    def test_hint_skips_auto_update(self, tmp_path: Path) -> None:
        """Hint mode should not trigger auto-update checks."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.auto_update.maybe_auto_update") as mock_update:
            result = runner.invoke(
                app,
                [
                    "--config-dir",
                    str(config_dir),
                    "--hint",
                    "client",
                    "config",
                    "list",
                ],
            )
            mock_update.assert_not_called()

        assert result.exit_code == 0


# ── Security tests ─────────────────────────────────────────────────


class TestHintSecurity:
    """Security-focused tests for the hint system (CWE-94 prevention)."""

    def test_string_param_with_quotes_is_escaped(self) -> None:
        """Quotes in parameter values must be escaped to prevent code injection."""
        hint = CommandHint(
            cli_command="test.cmd",
            description="Test",
            steps=[
                HintStep(
                    comment="Do something",
                    client=ClientCall(
                        method="some_method",
                        args={"param": "{evil}"},
                        result_var="result",
                    ),
                ),
            ],
        )
        # Attempt injection via double-quote breakout
        code = ClientRenderer.render(
            hint,
            params={"evil": 'value", x=__import__("os").system("id") #'},
            stack_url=STACK_URL,
            branch_id=None,
        )
        # The quotes must be escaped — generated code must be safe to compile
        compile(code, "<hint>", "exec")
        # Injected code must NOT appear as executable
        assert "__import__" not in code or '\\"' in code

    def test_list_param_with_quotes_is_escaped(self) -> None:
        """Quotes in list parameter values must be escaped."""
        hint = CommandHint(
            cli_command="test.cmd",
            description="Test",
            steps=[
                HintStep(
                    comment="Do something",
                    client=ClientCall(
                        method="some_method",
                        args={"items": "{evil_list}"},
                        result_var="result",
                    ),
                ),
            ],
        )
        code = ClientRenderer.render(
            hint,
            params={"evil_list": ["normal", 'evil"]; import os #']},
            stack_url=STACK_URL,
            branch_id=None,
        )
        compile(code, "<hint>", "exec")
        assert "import os" not in code or '\\"' in code

    def test_docstring_injection_prevented(self) -> None:
        """Triple quotes in params must not break the docstring."""
        hint = CommandHint(
            cli_command="test.cmd",
            description="Test",
            steps=[
                HintStep(
                    comment="Do something",
                    client=ClientCall(method="some_method", result_var="result"),
                ),
            ],
        )
        code = ClientRenderer.render(
            hint,
            params={"project": '"""\nimport os\nos.system("id")\n"""'},
            stack_url=STACK_URL,
            branch_id=None,
        )
        compile(code, "<hint>", "exec")

    def test_newlines_in_params_escaped(self) -> None:
        """Newline characters in params must be escaped."""
        hint = CommandHint(
            cli_command="test.cmd",
            description="Test",
            steps=[
                HintStep(
                    comment="Do something",
                    client=ClientCall(
                        method="some_method",
                        args={"name": "{name}"},
                        result_var="result",
                    ),
                ),
            ],
        )
        code = ClientRenderer.render(
            hint,
            params={"name": "line1\nline2\rline3"},
            stack_url=STACK_URL,
            branch_id=None,
        )
        compile(code, "<hint>", "exec")
        # Raw newlines must not appear in the string literal
        assert "line1\nline2" not in code


# ── Test helpers ───────────────────────────────────────────────────


def _simple_step() -> HintStep:
    return HintStep(
        comment="Do something",
        client=ClientCall(method="some_method", result_var="result"),
    )


def _simple_hint() -> CommandHint:
    return CommandHint(
        cli_command="config.test",
        description="Test command",
        steps=[_simple_step()],
    )


def _service_hint() -> CommandHint:
    return CommandHint(
        cli_command="config.svc",
        description="Test service command",
        steps=[
            HintStep(
                comment="Do something",
                client=ClientCall(method="some_method", result_var="result"),
                service=ServiceCall(
                    service_class="ConfigService",
                    service_module="config_service",
                    method="list_configs",
                    args={"aliases": "{project}"},
                ),
            ),
        ],
    )
