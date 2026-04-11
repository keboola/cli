"""Shared test fixtures for Keboola Agent CLI tests."""

from pathlib import Path

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.output import OutputFormatter


@pytest.fixture(autouse=True)
def _force_stdio_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force stdio transport in all tests to prevent spawning persistent server."""
    monkeypatch.setenv("KBAGENT_MCP_TRANSPORT", "stdio")


@pytest.fixture(autouse=True)
def _clear_updated_from(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent auto-update changelog leaking between tests."""
    monkeypatch.delenv("KBAGENT_UPDATED_FROM", raising=False)


@pytest.fixture
def tmp_config_dir(tmp_path: Path) -> Path:
    """Provide a temporary directory for configuration files."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    return config_dir


@pytest.fixture
def config_store(tmp_config_dir: Path) -> ConfigStore:
    """Provide a ConfigStore backed by a temporary directory."""
    return ConfigStore(config_dir=tmp_config_dir)


@pytest.fixture
def json_formatter() -> OutputFormatter:
    """Provide an OutputFormatter in JSON mode."""
    return OutputFormatter(json_mode=True, no_color=True)


@pytest.fixture
def human_formatter() -> OutputFormatter:
    """Provide an OutputFormatter in human (Rich) mode."""
    return OutputFormatter(json_mode=False, no_color=True)
