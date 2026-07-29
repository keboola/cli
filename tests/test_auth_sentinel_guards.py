"""Tests for the sentinel-aware client factories and fail-fast guards (contract
section 12, programmatic auth).

Covers three things:

1. Every direct ``project.token`` consumer enumerated in the contract's
   guard table raises `SessionAuthUnsupportedError` (via `require_static_token`)
   when handed a `kbc-session://` sentinel, before the token is ever used as
   a real credential.
2. `services.base.make_client_factory` builds a bearer-session client (no
   `X-StorageApi-Token` header) for a sentinel token and an ordinary static
   client otherwise.
3. Compat regression: a sentinel-token project round-trips through
   `ConfigStore` load/save unchanged, and `CURRENT_CONFIG_VERSION` is still 1
   (no config.json schema change shipped with this feature).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from keboola_agent_cli.auth.sentinel import make_session_token
from keboola_agent_cli.config_store import CURRENT_CONFIG_VERSION, ConfigStore
from keboola_agent_cli.data_science_client import DataScienceClient
from keboola_agent_cli.errors import ErrorCode, SessionAuthUnsupportedError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.base import default_client_factory, make_client_factory

STACK_URL = "https://connection.keboola.com"
SENTINEL_PROJECT_ID = 10105
STATIC_TOKEN = "12345-67890-abcdefghijklmnop"


def _sentinel_token() -> str:
    return make_session_token(SENTINEL_PROJECT_ID)


def _sentinel_project() -> ProjectConfig:
    return ProjectConfig(
        stack_url=STACK_URL,
        token=_sentinel_token(),
        project_name="Sentinel Project",
        project_id=SENTINEL_PROJECT_ID,
    )


def _config_store_with_sentinel_project(tmp_path: Path, alias: str = "sentinel") -> ConfigStore:
    store = ConfigStore(config_dir=tmp_path)
    store.add_project(alias, _sentinel_project())
    return store


def _static_project() -> ProjectConfig:
    return ProjectConfig(
        stack_url=STACK_URL,
        token=STATIC_TOKEN,
        project_name="Static Project",
        project_id=5725,
    )


def _mixed_config_store(tmp_path: Path) -> ConfigStore:
    """A config.json holding one session and one static project side by side --
    the shape ``auth register-projects`` produces on a stack where only some
    projects were migrated to browser login."""
    store = ConfigStore(config_dir=tmp_path)
    store.add_project("session", _sentinel_project())
    store.add_project("static", _static_project())
    return store


# ----------------------------------------------------------------------------
# services.base: default_client_factory / make_client_factory
# ----------------------------------------------------------------------------


class TestRemedyText:
    """The remedy must name a command that actually works for the reader.

    Every caller of `require_static_token` generates this message, so it has to
    be true for all of them -- the earlier text told the user to
    `project add --project <alias>`, which `add_project` rejects outright once
    that alias exists (and it always does: the guard fires on a *registered*
    session project).
    """

    def test_default_remedy_leads_with_project_edit(self) -> None:
        exc = SessionAuthUnsupportedError("The MCP server subprocess")

        assert "project edit --project <alias> --token <token>" in exc.message
        # `project add` is still mentioned, but scoped to the case it works for.
        assert "project add --project <new-alias>" in exc.message
        assert "rejects one that already exists" in exc.message

    def test_default_remedy_does_not_suggest_add_for_the_existing_alias(self) -> None:
        """The exact dead-end the previous wording produced."""
        exc = SessionAuthUnsupportedError("The importable SDK Client")

        assert "project add --project <alias>" not in exc.message

    def test_caller_supplied_remedy_replaces_the_default(self) -> None:
        """A credential-type swap keeps the project registered, so the generic
        "register it with a static token" advice would contradict the specific
        remedy the caller already supplies."""
        exc = SessionAuthUnsupportedError(
            "Replacing the stored credential of project 'prod' with a static Storage token",
            remedy="Run `kbagent project edit --project prod --token <token>` deliberately.",
        )

        assert exc.message.endswith(
            "Run `kbagent project edit --project prod --token <token>` deliberately."
        )
        assert "Point the project at a static Storage token instead" not in exc.message
        assert "<new-alias>" not in exc.message

    def test_feature_name_and_code_are_carried_either_way(self) -> None:
        with_remedy = SessionAuthUnsupportedError("kbagent kai", remedy="Do the other thing.")
        without = SessionAuthUnsupportedError("kbagent kai")

        for exc in (with_remedy, without):
            assert exc.feature == "kbagent kai"
            assert exc.error_code == ErrorCode.AUTH_NOT_SUPPORTED_ON_STACK
            assert exc.message.startswith(
                "kbagent kai does not support browser-login (session) projects yet."
            )


class TestDefaultClientFactory:
    def test_fails_fast_on_sentinel(self) -> None:
        with pytest.raises(SessionAuthUnsupportedError) as exc_info:
            default_client_factory(STACK_URL, _sentinel_token())
        assert exc_info.value.error_code == ErrorCode.AUTH_NOT_SUPPORTED_ON_STACK

    def test_static_token_unaffected(self) -> None:
        client = default_client_factory(STACK_URL, STATIC_TOKEN)
        try:
            assert client._client.headers.get("x-storageapi-token") == STATIC_TOKEN
        finally:
            client.close()


class TestMakeClientFactory:
    def test_static_token_gets_ordinary_header_client(self, tmp_path: Path) -> None:
        config_store = ConfigStore(config_dir=tmp_path)
        factory = make_client_factory(config_store)
        client = factory(STACK_URL, STATIC_TOKEN)
        try:
            assert client._client.headers.get("x-storageapi-token") == STATIC_TOKEN
            assert client._http_auth is None
        finally:
            client.close()

    def test_sentinel_token_gets_bearer_client_no_storage_header(self, tmp_path: Path) -> None:
        config_store = ConfigStore(config_dir=tmp_path)
        factory = make_client_factory(config_store)
        client = factory(STACK_URL, _sentinel_token())
        try:
            assert "x-storageapi-token" not in client._client.headers
            assert client._http_auth is not None
        finally:
            client.close()

    def test_malformed_sentinel_raises_config_error(self, tmp_path: Path) -> None:
        from keboola_agent_cli.errors import ConfigError

        config_store = ConfigStore(config_dir=tmp_path)
        factory = make_client_factory(config_store)
        with pytest.raises(ConfigError):
            factory(STACK_URL, "kbc-session://not-a-number")


# ----------------------------------------------------------------------------
# Guarded consumers -- each raises SessionAuthUnsupportedError on a sentinel
# ----------------------------------------------------------------------------


class TestMcpServiceGuards:
    @patch("keboola_agent_cli.services.mcp_service.shutil.which")
    def test_build_server_params_raises(self, mock_which: MagicMock) -> None:
        from keboola_agent_cli.services.mcp_service import _build_server_params

        mock_which.side_effect = lambda cmd: "/usr/local/bin/uvx" if cmd == "uvx" else None
        with pytest.raises(SessionAuthUnsupportedError) as exc_info:
            _build_server_params(_sentinel_project())
        assert exc_info.value.error_code == ErrorCode.AUTH_NOT_SUPPORTED_ON_STACK
        assert exc_info.value.feature == "The MCP server subprocess"

    def test_build_http_headers_raises(self) -> None:
        from keboola_agent_cli.services.mcp_service import _build_http_headers

        with pytest.raises(SessionAuthUnsupportedError) as exc_info:
            _build_http_headers(_sentinel_project())
        assert exc_info.value.feature == "The MCP HTTP transport"


class TestSemanticLayerGuards:
    def test_default_metastore_client_factory_raises(self) -> None:
        from keboola_agent_cli.services.semantic_layer_service import (
            default_metastore_client_factory,
        )

        with pytest.raises(SessionAuthUnsupportedError) as exc_info:
            default_metastore_client_factory(STACK_URL, _sentinel_token())
        assert exc_info.value.feature == "The Metastore Service (semantic layer)"

    def test_encrypt_token_raises(self, tmp_path: Path) -> None:
        from keboola_agent_cli.services.semantic_layer_service import SemanticLayerService

        config_store = _config_store_with_sentinel_project(tmp_path)
        service = SemanticLayerService(config_store=config_store)
        with pytest.raises(SessionAuthUnsupportedError) as exc_info:
            service.encrypt_token("sentinel", "keboola.some-component")
        assert exc_info.value.feature == "semantic-layer token --encrypt"


class TestKaiServiceGuard:
    def test_create_kai_client_raises(self, tmp_path: Path) -> None:
        from keboola_agent_cli.services.kai_service import KaiService

        config_store = _config_store_with_sentinel_project(tmp_path)
        service = KaiService(config_store=config_store)
        with pytest.raises(SessionAuthUnsupportedError) as exc_info:
            asyncio.run(service._create_kai_client("sentinel"))
        assert exc_info.value.feature == "kbagent kai"


class TestSharingServiceGuard:
    def test_resolve_master_token_fallback_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from keboola_agent_cli.services.sharing_service import SharingService

        monkeypatch.delenv("KBC_MASTER_TOKEN", raising=False)
        monkeypatch.delenv("KBC_MASTER_TOKEN_SENTINEL", raising=False)

        config_store = ConfigStore(config_dir=tmp_path)
        service = SharingService(config_store=config_store)
        project = _sentinel_project()
        with pytest.raises(SessionAuthUnsupportedError) as exc_info:
            service.resolve_master_token("sentinel", project)
        assert exc_info.value.feature == "kbagent sharing (master-token path)"

    def test_explicit_master_token_env_wins_no_guard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When a real master token env var is set, the sentinel is never
        touched -- the guard must not fire on the happy path."""
        from keboola_agent_cli.services.sharing_service import SharingService

        monkeypatch.setenv("KBC_MASTER_TOKEN", "901-master-token-value")
        config_store = ConfigStore(config_dir=tmp_path)
        service = SharingService(config_store=config_store)
        project = _sentinel_project()
        assert service.resolve_master_token("sentinel", project) == "901-master-token-value"


class TestLibClientGuard:
    def test_client_init_raises(self) -> None:
        from keboola_agent_cli.lib import Client

        with pytest.raises(SessionAuthUnsupportedError) as exc_info:
            Client(url=STACK_URL, token=_sentinel_token())
        assert exc_info.value.feature == "The importable SDK Client"


class TestAiServiceFactoryGuards:
    """default AI-service factories in component_service / config_service / flow_service."""

    def test_component_service_default_factory_raises(self) -> None:
        from keboola_agent_cli.services.component_service import default_ai_client_factory

        with pytest.raises(SessionAuthUnsupportedError) as exc_info:
            default_ai_client_factory(STACK_URL, _sentinel_token())
        assert exc_info.value.feature == "The Keboola AI Service"

    def test_config_service_default_factory_raises(self) -> None:
        from keboola_agent_cli.services.config_service import _default_ai_client_factory

        with pytest.raises(SessionAuthUnsupportedError) as exc_info:
            _default_ai_client_factory(STACK_URL, _sentinel_token())
        assert exc_info.value.feature == "The Keboola AI Service"

    def test_flow_service_ai_factory_raises(self) -> None:
        from keboola_agent_cli.services.flow_service import default_ai_client_factory

        with pytest.raises(SessionAuthUnsupportedError) as exc_info:
            default_ai_client_factory(STACK_URL, _sentinel_token())
        assert exc_info.value.feature == "The Keboola AI Service"

    def test_flow_service_scheduler_factory_raises(self) -> None:
        from keboola_agent_cli.services.flow_service import default_scheduler_client_factory

        with pytest.raises(SessionAuthUnsupportedError) as exc_info:
            default_scheduler_client_factory(STACK_URL, _sentinel_token())
        assert exc_info.value.feature == "The Scheduler Service"


class TestDataScienceFactoryGuards:
    def test_data_app_service_default_factory_raises(self) -> None:
        from keboola_agent_cli.services.data_app_service import _default_ds_client_factory

        with pytest.raises(SessionAuthUnsupportedError) as exc_info:
            _default_ds_client_factory(STACK_URL, _sentinel_token())
        assert exc_info.value.feature == "The Data Science Service (data apps)"

    def test_data_app_git_service_default_factory_raises(self) -> None:
        from keboola_agent_cli.services.data_app_git_service import _default_ds_client_factory

        with pytest.raises(SessionAuthUnsupportedError) as exc_info:
            _default_ds_client_factory(STACK_URL, _sentinel_token())
        assert exc_info.value.feature == "The Data Science Service (data apps)"


class TestStreamServiceGuard:
    def test_default_stream_client_factory_raises(self) -> None:
        from keboola_agent_cli.services.stream_service import default_stream_client_factory

        with pytest.raises(SessionAuthUnsupportedError) as exc_info:
            default_stream_client_factory(STACK_URL, _sentinel_token())
        assert exc_info.value.feature == "The Data Streams Service"


# ----------------------------------------------------------------------------
# Multi-project paths: the guard's AUTH_NOT_SUPPORTED_ON_STACK reaches the
# --json envelope per project, and the static project's result is unaffected.
# ----------------------------------------------------------------------------


class TestListDataAppsMixedProjects:
    def _service(self, store: ConfigStore, ds_mock: MagicMock, storage_mock: MagicMock):
        from keboola_agent_cli.services.data_app_service import DataAppService

        # Stub only the HTTP call, so the real `DataScienceClient.__init__`
        # still runs and its `SESSION_AUTH_FEATURE` guard fires for the session
        # project. Replacing the class itself would construct a MagicMock and
        # silently skip the very guard under test.
        return patch.object(
            DataScienceClient,
            "list_apps",
            autospec=True,
            side_effect=lambda _self, *a, **kw: ds_mock.list_apps(*a, **kw),
        ), DataAppService(
            config_store=store,
            client_factory=lambda url, token: storage_mock,
            encrypt_service=MagicMock(),
        )

    def test_session_project_errors_with_auth_code_static_project_lists(
        self, tmp_path: Path
    ) -> None:
        store = _mixed_config_store(tmp_path)
        storage_mock = MagicMock()
        storage_mock.list_component_configs.return_value = [
            {"id": "cfg1", "name": "Sales dashboard"}
        ]
        ds_mock = MagicMock()
        ds_mock.list_apps.return_value = [
            {
                "id": "77",
                "componentId": "keboola.data-apps",
                "configId": "cfg1",
                "type": "python-js",
            }
        ]
        ds_patch, service = self._service(store, ds_mock, storage_mock)

        with ds_patch:
            result = service.list_data_apps(["session", "static"])

        assert [a["project_alias"] for a in result["apps"]] == ["static"]
        assert result["apps"][0]["name"] == "Sales dashboard"

        assert len(result["errors"]) == 1
        error = result["errors"][0]
        assert error["project_alias"] == "session"
        assert error["error_code"] == ErrorCode.AUTH_NOT_SUPPORTED_ON_STACK
        assert "static Storage token" in error["message"]

        # The --json envelope a consuming agent branches on.
        assert (
            json.loads(json.dumps(result["errors"]))[0]["error_code"]
            == "AUTH_NOT_SUPPORTED_ON_STACK"
        )

    def test_unexpected_failure_still_reports_unexpected_error(self, tmp_path: Path) -> None:
        """The typed code must not come at the cost of the generic fallback."""
        store = _mixed_config_store(tmp_path)
        ds_mock = MagicMock()
        ds_mock.list_apps.side_effect = RuntimeError("connection pool exhausted")
        ds_patch, service = self._service(store, ds_mock, MagicMock())

        with ds_patch:
            result = service.list_data_apps(["session", "static"])

        assert result["apps"] == []
        by_alias = {e["project_alias"]: e["error_code"] for e in result["errors"]}
        assert by_alias == {
            "session": ErrorCode.AUTH_NOT_SUPPORTED_ON_STACK,
            "static": "UNEXPECTED_ERROR",
        }


class TestListToolsMixedProjects:
    @staticmethod
    def _patch_stdio_transport(
        monkeypatch: pytest.MonkeyPatch, *, fail_static: bool = False
    ) -> None:
        """Stand in for the MCP subprocess while keeping the real guard.

        ``_build_server_params`` is called for real, so the sentinel project
        still fails through ``require_static_token``; only the stdio session
        below it is replaced.
        """
        from keboola_agent_cli.services import mcp_service as mcp_module
        from keboola_agent_cli.services.mcp_service import McpService

        async def fake_connect_and_list_tools(
            project: ProjectConfig, branch_id: str | None = None
        ) -> list[dict[str, object]]:
            mcp_module._build_server_params(project, branch_id=branch_id)
            if fail_static:
                raise RuntimeError("stdio session closed")
            return [{"name": "get_buckets", "description": "", "inputSchema": {}}]

        def fake_which(cmd: str) -> str | None:
            return "/usr/local/bin/uvx" if cmd == "uvx" else None

        monkeypatch.setattr(mcp_module, "_connect_and_list_tools", fake_connect_and_list_tools)
        monkeypatch.setattr(mcp_module.shutil, "which", fake_which)
        monkeypatch.setattr(McpService, "_get_server_url", lambda self: None)

    def test_session_project_errors_with_auth_code_static_project_lists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from keboola_agent_cli.services.mcp_service import McpService

        self._patch_stdio_transport(monkeypatch)
        service = McpService(config_store=_mixed_config_store(tmp_path))

        result = service.list_tools(aliases=["session", "static"])

        assert [t["name"] for t in result["tools"]] == ["get_buckets"]
        assert len(result["errors"]) == 1
        error = result["errors"][0]
        assert error["project_alias"] == "session"
        assert error["error_code"] == ErrorCode.AUTH_NOT_SUPPORTED_ON_STACK
        assert "Failed to list tools" in error["message"]

    def test_real_mcp_failure_still_reports_mcp_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from keboola_agent_cli.services.mcp_service import McpService

        self._patch_stdio_transport(monkeypatch, fail_static=True)
        service = McpService(config_store=_mixed_config_store(tmp_path))

        result = service.list_tools(aliases=["session", "static"])

        assert result["tools"] == []
        by_alias = {e["project_alias"]: e["error_code"] for e in result["errors"]}
        assert by_alias == {
            "session": ErrorCode.AUTH_NOT_SUPPORTED_ON_STACK,
            "static": "MCP_ERROR",
        }


class TestGatherResultsMixedProjects:
    """The parallel read-tool path (`_call_read_tool` -> `_gather_results`)."""

    @staticmethod
    def _gather(outcomes: dict[str, Any]) -> dict[str, Any]:
        from keboola_agent_cli.services.mcp_service import McpService

        async def _run() -> dict[str, Any]:
            async def _produce(outcome: Any) -> dict[str, Any]:
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome

            tasks = {
                alias: asyncio.create_task(_produce(outcome)) for alias, outcome in outcomes.items()
            }
            return await McpService._gather_results(tasks)

        return asyncio.run(_run())

    def test_session_project_error_keeps_auth_code(self) -> None:
        result = self._gather(
            {
                "session": SessionAuthUnsupportedError("The MCP server subprocess"),
                "static": {"data": "rows"},
            }
        )

        assert result["results"] == [{"data": "rows", "project_alias": "static"}]
        assert result["errors"][0]["project_alias"] == "session"
        assert result["errors"][0]["error_code"] == ErrorCode.AUTH_NOT_SUPPORTED_ON_STACK

    def test_transport_failure_still_reports_mcp_error(self) -> None:
        result = self._gather({"static": RuntimeError("stdio session closed")})

        assert result["errors"][0]["error_code"] == "MCP_ERROR"
        assert result["errors"][0]["message"] == "stdio session closed"


# ----------------------------------------------------------------------------
# Constructor-level wiring: Doctor / Snapshot / Token / Org get the
# bearer-aware default (make_client_factory), same treatment as BaseService.
# ----------------------------------------------------------------------------


class TestConstructorLevelWiring:
    def test_doctor_service_default_is_bearer_aware(self, tmp_path: Path) -> None:
        from keboola_agent_cli.services.doctor_service import DoctorService

        config_store = ConfigStore(config_dir=tmp_path)
        service = DoctorService(config_store=config_store)
        client = service._client_factory(STACK_URL, _sentinel_token())
        try:
            assert "x-storageapi-token" not in client._client.headers
        finally:
            client.close()

    def test_snapshot_service_default_is_bearer_aware(self, tmp_path: Path) -> None:
        from keboola_agent_cli.services.snapshot_service import SnapshotService

        config_store = ConfigStore(config_dir=tmp_path)
        service = SnapshotService(config_store=config_store)
        client = service._client_factory(STACK_URL, _sentinel_token())
        try:
            assert "x-storageapi-token" not in client._client.headers
        finally:
            client.close()

    def test_token_service_default_is_bearer_aware(self, tmp_path: Path) -> None:
        from keboola_agent_cli.services.token_service import TokenService

        config_store = ConfigStore(config_dir=tmp_path)
        service = TokenService(config_store=config_store)
        client = service._client_factory(STACK_URL, _sentinel_token())
        try:
            assert "x-storageapi-token" not in client._client.headers
        finally:
            client.close()

    def test_org_service_storage_factory_is_bearer_aware(self, tmp_path: Path) -> None:
        from keboola_agent_cli.services.org_service import OrgService

        config_store = ConfigStore(config_dir=tmp_path)
        service = OrgService(config_store=config_store)
        client = service._storage_client_factory(STACK_URL, _sentinel_token())
        try:
            assert "x-storageapi-token" not in client._client.headers
        finally:
            client.close()

    def test_named_default_factories_still_fail_fast_when_injected_explicitly(self) -> None:
        """The module-level default_*_client_factory functions (kept for
        explicit injection / back-compat) still delegate to the static-token
        guard even though the constructor default now uses the bearer-aware
        factory instead."""
        from keboola_agent_cli.services.org_service import default_storage_client_factory
        from keboola_agent_cli.services.snapshot_service import default_snapshot_client_factory
        from keboola_agent_cli.services.token_service import default_token_client_factory

        for fn in (
            default_snapshot_client_factory,
            default_token_client_factory,
            default_storage_client_factory,
        ):
            with pytest.raises(SessionAuthUnsupportedError):
                fn(STACK_URL, _sentinel_token())


# ----------------------------------------------------------------------------
# Compat regression: a sentinel project round-trips through ConfigStore,
# and CURRENT_CONFIG_VERSION is unchanged.
# ----------------------------------------------------------------------------


class TestConfigStoreCompatRegression:
    def test_current_config_version_unchanged(self) -> None:
        assert CURRENT_CONFIG_VERSION == 1

    def test_sentinel_project_round_trips_unchanged(self, tmp_path: Path) -> None:
        store = ConfigStore(config_dir=tmp_path)
        project = _sentinel_project()
        store.add_project("sentinel", project)

        # Fresh ConfigStore instance -> forces a real load from disk.
        reloaded_store = ConfigStore(config_dir=tmp_path)
        reloaded = reloaded_store.get_project("sentinel")

        assert reloaded is not None
        assert reloaded.token == project.token
        assert reloaded.token.startswith("kbc-session://")
        assert reloaded.stack_url == STACK_URL
        assert reloaded.project_id == SENTINEL_PROJECT_ID

        raw_config = reloaded_store.load()
        assert raw_config.version == CURRENT_CONFIG_VERSION
