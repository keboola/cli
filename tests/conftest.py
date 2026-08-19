"""Shared test fixtures for Keboola Agent CLI tests."""

from pathlib import Path

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.output import OutputFormatter


@pytest.fixture(autouse=True)
def _deterministic_console_width(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the width Rich renders at, so assertions do not depend on the host.

    Rich truncates table cells to fit the console and infers that width from
    the environment. A test asserting a value appears in a rendered table
    therefore passes or fails according to the machine it runs on -- which is
    how `organization-project` came to be silently cut in half on Windows CI
    while passing locally. Wide enough that nothing under test is elided.
    """
    monkeypatch.setenv("COLUMNS", "200")


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


@pytest.fixture(autouse=True)
def _redirect_global_state_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep auto-update bookkeeping out of the developer's real config directory.

    Both writers below resolve through ``platformdirs.user_config_dir``, so
    neither honours ``--config-dir``: they always address the machine's own
    files. That is correct for the shipped CLI -- the version cache and a
    pending update are per-machine, not per-project -- and exactly why the
    test suite has to redirect them rather than the production code.

    ``auto_update._get_cache_path`` backs ``version_cache.json``. Any test
    reaching the real ``_write_cache`` -- ``TestMaybeAutoUpdate`` has two that
    mock ``_read_cache`` but not the write -- stamps the canonical fake latest
    version ``1.0.0`` into it. The CLI then believes for a full
    ``AUTO_UPDATE_CHECK_INTERVAL`` (1 hour) that a release tag which does not
    exist is available, prints an update banner on every command, and fails
    the reinstall: running ``make test`` broke auto-update for whoever ran it.

    ``update_runner.state_dir`` backs the deferred-update marker / exit / log
    files. No test reaches it today (the deferral tests mock
    ``request_deferred_update``, and ``should_defer()`` is False off Windows),
    but it is the same latent bug: on Windows ``should_defer()`` defaults to
    True, and ``report_finished_deferred_update`` -- which runs ahead of every
    skip gate -- consumes and unlinks a genuine pending report while reading
    it, so a developer's own update outcome would vanish into a test run.

    Redirecting the module attributes (not the top-level import in
    ``test_auto_update.py``, which ``TestGetCachePath`` still exercises
    against the real resolver) covers tests written later that forget to mock
    the writer. ``test_suite_never_resolves_to_the_real_user_cache`` fails if
    this fixture is removed.
    """
    from keboola_agent_cli import auto_update, update_runner

    cache_file = tmp_path / "version_cache.json"
    monkeypatch.setattr(auto_update, "_get_cache_path", lambda: cache_file)
    monkeypatch.setattr(update_runner, "state_dir", lambda: tmp_path)


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
