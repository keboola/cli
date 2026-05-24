"""Unit tests for config-dir credential hydration used by the E2E harness.

These run as part of ``make test`` -- they carry no ``e2e`` marker and make no
real API calls. They verify that ``KBAGENT_E2E_CONFIG_DIR`` +
``KBAGENT_E2E_ALIAS`` promote a stored project's token/URL into the
``E2E_API_TOKEN`` / ``E2E_URL`` env vars, so the E2E suite can run against a
project already registered in a local ``config.json`` without the token ever
being typed on the command line.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.models import ProjectConfig
from test_e2e import (
    ENV_ALIAS,
    ENV_CONFIG_DIR,
    ENV_TOKEN,
    ENV_URL,
    _hydrate_credentials_from_config_dir,
)

# Canonical fake test token (see tests memory: 901-<role>-<value> convention).
_FAKE_URL = "https://connection.keboola.com"
_FAKE_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"


def _seed_config(config_dir: Path, alias: str) -> None:
    """Write a config.json holding a single project under *alias*."""
    ConfigStore(config_dir=config_dir).add_project(
        alias,
        ProjectConfig(stack_url=_FAKE_URL, token=_FAKE_TOKEN),
    )


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start each test with the four env vars unset.

    The function under test writes ``os.environ`` directly (not via
    monkeypatch), so monkeypatch's auto-restore would not undo it -- we clear
    up front to guarantee a clean slate regardless of how the previous test or
    the ambient shell left things.
    """
    for var in (ENV_TOKEN, ENV_URL, ENV_CONFIG_DIR, ENV_ALIAS):
        monkeypatch.delenv(var, raising=False)


def test_hydrate_populates_token_and_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Config-dir mode reads the stored project and fills both env vars."""
    _seed_config(tmp_path, "kbagent-e2e")
    monkeypatch.setenv(ENV_CONFIG_DIR, str(tmp_path))
    monkeypatch.setenv(ENV_ALIAS, "kbagent-e2e")

    _hydrate_credentials_from_config_dir()

    assert os.environ[ENV_TOKEN] == _FAKE_TOKEN
    assert os.environ[ENV_URL] == _FAKE_URL


def test_explicit_token_takes_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pre-set E2E_API_TOKEN is never overwritten by config-dir mode."""
    _seed_config(tmp_path, "kbagent-e2e")
    monkeypatch.setenv(ENV_TOKEN, "explicit-token")
    monkeypatch.setenv(ENV_CONFIG_DIR, str(tmp_path))
    monkeypatch.setenv(ENV_ALIAS, "kbagent-e2e")

    _hydrate_credentials_from_config_dir()

    assert os.environ[ENV_TOKEN] == "explicit-token"
    # URL must not be touched either when the explicit token short-circuits.
    assert ENV_URL not in os.environ


def test_noop_when_mode_not_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    """With neither env var set, the token stays unset (no accidental fill)."""
    _hydrate_credentials_from_config_dir()

    assert ENV_TOKEN not in os.environ
    assert ENV_URL not in os.environ


def test_partial_request_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CONFIG_DIR without ALIAS (or vice versa) is treated as 'not requested'."""
    _seed_config(tmp_path, "kbagent-e2e")
    monkeypatch.setenv(ENV_CONFIG_DIR, str(tmp_path))  # ALIAS deliberately omitted

    _hydrate_credentials_from_config_dir()

    assert ENV_TOKEN not in os.environ


def test_missing_alias_fails_fast(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo'd alias raises with the available aliases, never silently skips."""
    _seed_config(tmp_path, "real-alias")
    monkeypatch.setenv(ENV_CONFIG_DIR, str(tmp_path))
    monkeypatch.setenv(ENV_ALIAS, "typo-alias")

    with pytest.raises(RuntimeError, match=r"typo-alias.*not found.*real-alias"):
        _hydrate_credentials_from_config_dir()

    # Failure must not leave a half-populated environment behind.
    assert ENV_TOKEN not in os.environ
