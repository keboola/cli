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
    its result would depend on which versions have wheel assets. The patched
    ``head`` returns 404 ONLY for the kbagent release-asset URL (so the resolver
    yields None -> the ``git+`` path existing tests assert). Any OTHER
    ``httpx.head`` call raises loudly rather than silently returning 404, so a
    future caller that leans on this fixture by accident fails visibly instead of
    getting a surprise 404. Wheel-path tests override ``httpx.head`` (or patch
    the resolver) themselves to simulate a present asset.
    """
    from types import SimpleNamespace

    from keboola_agent_cli.services import version_service

    def _head(url: str, *args: object, **kwargs: object) -> SimpleNamespace:
        if "keboola/cli/releases/download" in str(url):
            return SimpleNamespace(status_code=404)
        raise RuntimeError(
            f"unexpected httpx.head({url!r}) in tests -- add an explicit mock; "
            "the _no_wheel_asset_probe fixture only stubs the kbagent wheel-asset probe"
        )

    monkeypatch.setattr(version_service.httpx, "head", _head)


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
