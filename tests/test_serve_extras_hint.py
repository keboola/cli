"""The reinstall hint of ``kbagent serve`` when the [server] extras are missing.

The hint never names a git install (an untagged one builds ``main``, #791). It
pins the running version only when that version can be a PyPI release.
"""

from __future__ import annotations

import sys

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app as cli_app


@pytest.mark.parametrize(
    ("version", "spec"),
    [
        ("0.99.1", "'keboola-cli[server]==0.99.1'"),
        ("1.0.0", "'keboola-cli[server]==1.0.0'"),
        ("0.0.0-dev", "'keboola-cli[server]'"),
        ("0.99.1+g1234abc", "'keboola-cli[server]'"),
        ("not-a-version", "'keboola-cli[server]'"),
    ],
)
def test_missing_extras_hint(monkeypatch: pytest.MonkeyPatch, version: str, spec: str) -> None:
    monkeypatch.setitem(sys.modules, "uvicorn", None)  # the import raises ModuleNotFoundError
    monkeypatch.setattr("keboola_agent_cli.commands.serve.__version__", version)

    result = CliRunner().invoke(cli_app, ["serve"])

    assert result.exit_code == 1
    assert "requires the optional 'server' extras (missing: uvicorn)" in result.output
    assert f"uv tool install --force {spec}\n" in result.output
    assert "git+" not in result.output
