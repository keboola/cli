"""Tests for the permission seam on the ``kbagent serve`` REST surface.

The CLI has always had a session firewall (`--deny-writes` /
`--deny-destructive` plus the persisted `config.permissions` policy). Until
now it stopped at the terminal: every route on the same process was wide
open. These tests pin the seam that closes that gap:

* ``app.state.permission_engine`` -- built by ``create_app`` from the persisted
  policy of the config dir it SERVES, plus the ``--deny-writes`` /
  ``--deny-destructive`` flags ``kbagent serve`` forwards.
* ``require_permission(operation)`` -- the FastAPI dependency routes declare.
* ``PermissionDeniedError`` -> HTTP 403 with ``error_code: PERMISSION_DENIED``,
  the same code the CLI prints for the same denial.

Task 2 builds the ``/auth/*`` router on top of this; the probe routes below
exercise the seam without depending on any particular route existing.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

if importlib.util.find_spec("fastapi") is None:  # pragma: no cover
    pytest.skip(
        "FastAPI not installed; run `uv pip install -e '.[server]'`", allow_module_level=True
    )

from fastapi import Depends
from fastapi.testclient import TestClient

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.constants import EXIT_PERMISSION_DENIED
from keboola_agent_cli.models import PermissionPolicy
from keboola_agent_cli.permissions import (
    OPERATION_REGISTRY,
    SERVE_ONLY_OPERATIONS,
    PermissionEngine,
)
from keboola_agent_cli.server import create_app
from keboola_agent_cli.server.dependencies import (
    ServiceRegistry,
    get_registry,
    require_permission,
)
from keboola_agent_cli.services.auth_service import AuthService

TOKEN = "perm-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

# One read and one destructive probe, so a policy targeting a category can be
# observed to block one while letting the other through.
READ_PROBE = "/_probe/read"
DESTRUCTIVE_PROBE = "/_probe/destructive"


def _install_probes(app: Any) -> None:
    """Register two test-only routes guarded by ``require_permission``."""

    @app.get(READ_PROBE, dependencies=[Depends(require_permission("config.list"))])
    def _read_probe() -> dict[str, str]:
        return {"status": "ok", "operation": "config.list"}

    @app.get(DESTRUCTIVE_PROBE, dependencies=[Depends(require_permission("config.delete"))])
    def _destructive_probe() -> dict[str, str]:
        return {"status": "ok", "operation": "config.delete"}


def _persist_policy(config_dir: Path, policy: PermissionPolicy) -> None:
    """Write ``policy`` into config.json of ``config_dir`` the way the CLI does."""
    store = ConfigStore(config_dir=config_dir)
    config = store.load()
    config.permissions = policy
    store.save(config)


def _client(tmp_path: Path, **create_app_kwargs: Any) -> TestClient:
    app = create_app(config_dir=str(tmp_path), auth_token=TOKEN, **create_app_kwargs)
    _install_probes(app)
    return TestClient(app)


class TestDefaultEngineFromPersistedPolicy:
    """``create_app`` with no engine reads ``config.permissions`` from disk."""

    def test_denied_operation_answers_403(self, tmp_path: Path) -> None:
        _persist_policy(tmp_path, PermissionPolicy(mode="allow", deny=["cli:destructive"]))
        resp = _client(tmp_path).get(DESTRUCTIVE_PROBE, headers=AUTH)
        assert resp.status_code == 403, resp.text

    def test_allowed_operation_passes_through(self, tmp_path: Path) -> None:
        _persist_policy(tmp_path, PermissionPolicy(mode="allow", deny=["cli:destructive"]))
        resp = _client(tmp_path).get(READ_PROBE, headers=AUTH)
        assert resp.status_code == 200, resp.text
        assert resp.json()["operation"] == "config.list"

    def test_no_config_file_means_no_policy(self, tmp_path: Path) -> None:
        # Nothing persisted at all -- every operation passes, exactly like
        # `PermissionEngine(None)` on the CLI side.
        client = _client(tmp_path / "empty")
        assert client.get(READ_PROBE, headers=AUTH).status_code == 200
        assert client.get(DESTRUCTIVE_PROBE, headers=AUTH).status_code == 200

    def test_corrupted_config_degrades_to_no_policy(self, tmp_path: Path) -> None:
        # A broken config file must not take the server down; it degrades to
        # "no policy" the same way the CLI bootstrap does.
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "config.json").write_text("{ not json", encoding="utf-8")
        assert _client(tmp_path).get(DESTRUCTIVE_PROBE, headers=AUTH).status_code == 200


class TestExplicitEngineWins:
    """An engine passed to ``create_app`` replaces the persisted-policy default.

    This is how ``kbagent serve`` carries the process-global ``--deny-writes``
    / ``--deny-destructive`` flags into the REST surface.
    """

    def _session_engine(self) -> PermissionEngine:
        from keboola_agent_cli.cli import apply_firewall_flags

        policy = apply_firewall_flags(None, deny_writes=True, deny_destructive=False)
        return PermissionEngine(policy)

    def test_session_deny_writes_blocks_destructive_probe(self, tmp_path: Path) -> None:
        # Persisted policy would allow everything; the session engine does not.
        _persist_policy(tmp_path, PermissionPolicy(mode="allow", deny=[]))
        client = _client(tmp_path, permission_engine=self._session_engine())
        assert client.get(DESTRUCTIVE_PROBE, headers=AUTH).status_code == 403

    def test_persisted_policy_is_not_consulted(self, tmp_path: Path) -> None:
        # Persisted policy denies the READ probe; the explicit engine (which
        # only denies writes) wins, so the read passes.
        _persist_policy(tmp_path, PermissionPolicy(mode="allow", deny=["config.list"]))
        client = _client(tmp_path, permission_engine=self._session_engine())
        assert client.get(READ_PROBE, headers=AUTH).status_code == 200


class TestErrorEnvelope:
    """A denial renders the kbagent error envelope, not a FastAPI detail body."""

    def test_403_body_carries_permission_denied_code(self, tmp_path: Path) -> None:
        _persist_policy(tmp_path, PermissionPolicy(mode="allow", deny=["cli:destructive"]))
        resp = _client(tmp_path).get(DESTRUCTIVE_PROBE, headers=AUTH)
        assert resp.status_code == 403
        body = resp.json()
        assert body["status"] == "error"
        assert body["error"]["code"] == "PERMISSION_DENIED"
        # The message names the operation, so a caller knows what to re-run
        # with a different policy.
        assert "config.delete" in body["error"]["message"]

    def test_denial_is_403_not_401(self, tmp_path: Path) -> None:
        # The bearer token was accepted; only the operation is blocked. A 401
        # would send callers chasing their credentials instead of the policy.
        _persist_policy(tmp_path, PermissionPolicy(mode="allow", deny=["cli:destructive"]))
        client = _client(tmp_path)
        assert client.get(DESTRUCTIVE_PROBE).status_code == 401  # no bearer at all
        assert client.get(DESTRUCTIVE_PROBE, headers=AUTH).status_code == 403


class TestRequirePermissionDependency:
    """Behaviour of the dependency itself, independent of ``create_app``."""

    def test_registry_override_cannot_disable_enforcement(self, tmp_path: Path) -> None:
        # Server tests routinely override `get_registry` with a registry built
        # via `__new__` (no `__init__`). The engine lives on app.state, not on
        # the registry, precisely so such an override cannot silently switch
        # the firewall off.
        _persist_policy(tmp_path, PermissionPolicy(mode="allow", deny=["cli:destructive"]))
        app = create_app(config_dir=str(tmp_path), auth_token=TOKEN)
        _install_probes(app)
        bare = ServiceRegistry.__new__(ServiceRegistry)
        app.dependency_overrides[get_registry] = lambda: bare
        client = TestClient(app)
        assert client.get(DESTRUCTIVE_PROBE, headers=AUTH).status_code == 403
        assert client.get(READ_PROBE, headers=AUTH).status_code == 200

    def test_app_without_an_engine_fails_closed(self, tmp_path: Path) -> None:
        # An app assembled some other way than create_app has no engine, so it
        # cannot say whether an operation is permitted -- it must refuse rather
        # than treat the missing attribute as "no policy".
        from fastapi import FastAPI

        from keboola_agent_cli.server.auth import AuthSettings, install_auth

        app = FastAPI()
        install_auth(app, AuthSettings(token=TOKEN))
        _install_probes(app)
        resp = TestClient(app, raise_server_exceptions=False).get(READ_PROBE, headers=AUTH)
        assert resp.status_code != 200


class TestRegistryWiring:
    """The registry exposes AuthService; the engine lives on app.state."""

    def test_auth_service_is_registered(self, tmp_path: Path) -> None:
        app = create_app(config_dir=str(tmp_path), auth_token=TOKEN)
        assert isinstance(app.state.registry.auth, AuthService)

    def test_explicit_engine_is_stored_on_app_state(self, tmp_path: Path) -> None:
        engine = PermissionEngine(PermissionPolicy(mode="allow", deny=["cli:destructive"]))
        app = create_app(config_dir=str(tmp_path), auth_token=TOKEN, permission_engine=engine)
        assert app.state.permission_engine is engine
        # Not on the registry: one source of truth, and a registry override in
        # a test must not be able to drop it.
        assert not hasattr(app.state.registry, "permission_engine")


class TestServedConfigDirPolicyWins:
    """`create_app` reads the policy of the dir it SERVES, not the caller's.

    `kbagent --config-dir A serve --config-dir B` serves B. Before this, the
    CLI callback's pre-built engine (policy of A) was handed to `create_app`
    while every service read B -- so B's persisted deny policy was silently
    ignored whenever the two dirs diverged.
    """

    def test_served_dir_policy_applies_when_dirs_diverge(self, tmp_path: Path) -> None:
        caller_dir = tmp_path / "caller"
        served_dir = tmp_path / "served"
        # The caller's own dir allows everything; only the SERVED dir denies.
        _persist_policy(caller_dir, PermissionPolicy(mode="allow", deny=[]))
        _persist_policy(served_dir, PermissionPolicy(mode="allow", deny=["cli:destructive"]))

        client = _client(served_dir)
        assert client.get(DESTRUCTIVE_PROBE, headers=AUTH).status_code == 403
        assert client.get(READ_PROBE, headers=AUTH).status_code == 200

    def test_session_flags_merge_onto_the_served_dir_policy(self, tmp_path: Path) -> None:
        # The flags are a property of the invocation, so they travel; the
        # persisted policy is the served dir's. Both must end up in force.
        _persist_policy(tmp_path, PermissionPolicy(mode="allow", deny=["config.list"]))
        client = _client(tmp_path, deny_destructive=True)
        assert client.get(READ_PROBE, headers=AUTH).status_code == 403  # persisted
        assert client.get(DESTRUCTIVE_PROBE, headers=AUTH).status_code == 403  # flag


class TestServeCommandCarriesTheFlags:
    """`kbagent --deny-destructive serve` must reach the REST surface."""

    def _invoke_serve(
        self,
        monkeypatch: pytest.MonkeyPatch,
        argv: list[str],
    ) -> tuple[Any, dict[str, Any]]:
        import uvicorn
        from typer.testing import CliRunner

        from keboola_agent_cli import server as server_pkg
        from keboola_agent_cli.cli import app as cli_app

        monkeypatch.setenv("KBAGENT_AUTO_UPDATE", "false")
        captured: dict[str, Any] = {}

        def _fake_create_app(**kwargs: Any) -> object:
            captured.update(kwargs)
            return object()

        def _fake_run(*_args: Any, **_kwargs: Any) -> None:
            return None

        monkeypatch.setattr(server_pkg, "create_app", _fake_create_app)
        monkeypatch.setattr(uvicorn, "run", _fake_run)
        return CliRunner().invoke(cli_app, argv), captured

    def test_deny_destructive_flag_is_forwarded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, captured = self._invoke_serve(
            monkeypatch, ["--config-dir", str(tmp_path), "--deny-destructive", "serve"]
        )
        assert result.exit_code == 0, result.output
        assert captured["deny_destructive"] is True
        assert captured["deny_writes"] is False
        # The CLI must NOT hand over a pre-built engine any more -- that engine
        # carried the caller's config dir, not the served one.
        assert captured.get("permission_engine") is None

    def test_no_flags_forwards_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        result, captured = self._invoke_serve(monkeypatch, ["--config-dir", str(tmp_path), "serve"])
        assert result.exit_code == 0, result.output
        assert captured["deny_writes"] is False
        assert captured["deny_destructive"] is False


class TestDenyWritesBlocksTheServeCommandItself:
    """`kbagent --deny-writes serve` never starts the server.

    `serve` is classified `admin` in OPERATION_REGISTRY and `--deny-writes`
    appends `cli:write`, which spans write+destructive+admin -- so the CLI
    callback denies the `serve` command before uvicorn is ever reached. Every
    doc surface that recommends enforcement over REST must therefore recommend
    a persisted policy (or `--deny-destructive`), never `--deny-writes`.
    """

    def test_deny_writes_serve_exits_permission_denied(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, captured = TestServeCommandCarriesTheFlags()._invoke_serve(
            monkeypatch, ["--config-dir", str(tmp_path), "--deny-writes", "serve"]
        )
        assert result.exit_code == EXIT_PERMISSION_DENIED
        # create_app was never called: the command itself was blocked.
        assert captured == {}


class TestOperationRegistryEntry:
    """`auth.projects` is the serve-only read endpoint Task 2 will expose."""

    def test_auth_projects_is_a_read_operation(self) -> None:
        assert OPERATION_REGISTRY["auth.projects"] == "read"

    def test_auth_projects_is_exempt_from_the_command_sync_gate(self) -> None:
        # It has no CLI leaf command, so the dead-key check in
        # scripts/check_command_sync.py must skip it.
        assert "auth.projects" in SERVE_ONLY_OPERATIONS

    def test_serve_only_operations_are_all_registered(self) -> None:
        assert set(OPERATION_REGISTRY) >= SERVE_ONLY_OPERATIONS
