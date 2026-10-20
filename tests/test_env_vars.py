"""The environment variable registry (spec rule EV1) and the options a variable sets.

`env.registry-complete` in scripts/check_cli_spec.py checks that the registry
and the reads in src/ agree; these cases check the registry itself, that
--help never shows a variable's value, and that `envvar=` on the root options
kept the precedence flag > variable > default.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest
import typer.main
from typer.testing import CliRunner

from keboola_agent_cli import cli as cli_module
from keboola_agent_cli.cli import app
from keboola_agent_cli.constants import ENV_AUTO_UPDATE, ENV_CONFIG_DIR, ENV_CONVERSATION_ID
from keboola_agent_cli.env_vars import ENV_VARS

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_auto_update(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_AUTO_UPDATE, "false")


def test_registry_entries_are_complete_and_unique() -> None:
    names = [var.name for var in ENV_VARS]
    assert len(names) == len(set(names))
    for var in ENV_VARS:
        assert var.purpose.endswith("."), var.name
        assert re.fullmatch(r"\d+\.\d+\.\d+", var.added_in), var.name


def _envvar_options() -> list[tuple[list[str], str]]:
    """(command path, envvar name) of every visible option that declares ``envvar=``."""
    found: list[tuple[list[str], str]] = []

    def walk(command: Any, path: list[str]) -> None:
        for param in command.params:
            envvar = getattr(param, "envvar", None)
            if isinstance(envvar, str) and not getattr(param, "hidden", False):
                found.append((path, envvar))
        for name, child in (getattr(command, "commands", None) or {}).items():
            if not getattr(child, "hidden", False):
                walk(child, [*path, name])

    walk(typer.main.get_command(app), [])
    return found


def test_every_envvar_option_is_registered() -> None:
    registered = {var.name for var in ENV_VARS}
    assert {envvar for _, envvar in _envvar_options()} <= registered


def test_help_never_shows_a_variable_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """--help names the variable of an option and never prints its value (EV1)."""
    for var in ENV_VARS:
        if "<" not in var.name:
            monkeypatch.setenv(var.name, f"value-of-{var.name.lower()}")
    monkeypatch.setenv(ENV_AUTO_UPDATE, "false")
    for path in sorted({tuple(p) for p, _ in _envvar_options()}):
        result = runner.invoke(app, [*path, "--help"], terminal_width=200)
        assert result.exit_code == 0, result.output
        assert "value-of-" not in result.output, path


@pytest.mark.parametrize(
    ("flag", "env", "expected"),
    [
        (None, "/from/env", None),  # the variable is left to resolve_config_dir
        ("/from/flag", "/from/env", "/from/flag"),
        ("/from/flag", None, "/from/flag"),
    ],
)
def test_config_dir_precedence_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str | None,
    env: str | None,
    expected: str | None,
) -> None:
    seen: list[str | None] = []
    real = cli_module.resolve_config_dir

    def _record(cli_config_dir: str | None = None) -> tuple[Path, str]:
        seen.append(cli_config_dir)
        return real(cli_config_dir=str(tmp_path))

    monkeypatch.setattr(cli_module, "resolve_config_dir", _record)
    if env is not None:
        monkeypatch.setenv(ENV_CONFIG_DIR, env)
    args = ["--config-dir", flag] if flag else []
    result = runner.invoke(app, [*args, "--json", "project", "list"])
    assert result.exit_code == 0, result.output
    assert seen == [expected]


def test_conversation_id_flag_wins_over_the_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_CONVERSATION_ID, "from-env")
    args = ["--config-dir", str(tmp_path), "--conversation-id", "from-flag"]
    result = runner.invoke(app, [*args, "--json", "project", "list"])
    assert result.exit_code == 0, result.output
    assert os.environ[ENV_CONVERSATION_ID] == "from-flag"
