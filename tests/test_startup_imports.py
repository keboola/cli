"""Startup imports (#801): a command imports its own command module, not the whole CLI."""

import subprocess
import sys

import pytest

from keboola_agent_cli.constants import ENV_AUTO_UPDATE

# Runs the CLI in a new interpreter and prints the imported modules on the last line.
_PROBE = """
import sys
from keboola_agent_cli.cli import app
try:
    app(sys.argv[1:])
except SystemExit:
    pass
print(" ".join(sys.modules), file=sys.stderr)
"""
_COMMANDS = "keboola_agent_cli.commands."


@pytest.mark.parametrize(
    ("argv", "command_modules"),
    [(["--version"], set()), (["job", "list", "--help"], {"job"})],
)
def test_a_command_imports_only_its_own_modules(
    argv: list[str], command_modules: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_AUTO_UPDATE, "false")
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, *argv], capture_output=True, text=True, check=True
    )
    modules = set(result.stderr.splitlines()[-1].split())
    names = {m.removeprefix(_COMMANDS) for m in modules if m.startswith(_COMMANDS)}
    assert {name for name in names if not name.startswith("_")} == command_modules
    assert "keboola_agent_cli.services.storage_service" not in modules
    assert "prompt_toolkit" not in modules
