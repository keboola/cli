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


@pytest.fixture(autouse=True)
def _no_wheel_asset_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the prebuilt-wheel HEAD probe to "asset absent" (issue #353).

    ``resolve_kbagent_wheel_url`` makes a live HEAD request to GitHub. Without
    this guard every test exercising an update path would hit the network and
    its result would depend on which versions have wheel assets. Defaulting the
    probe to 404 keeps the ``git+`` behaviour existing tests assert; wheel-path
    tests override ``httpx.head`` (or patch the resolver) to simulate a present
    asset. ``version_service`` is the only ``httpx.head`` caller in ``src``, so
    this does not mask any other network use.
    """
    from types import SimpleNamespace

    from keboola_agent_cli.services import version_service

    monkeypatch.setattr(
        version_service.httpx,
        "head",
        lambda *args, **kwargs: SimpleNamespace(status_code=404),
    )


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
