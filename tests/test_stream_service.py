"""Tests for StreamService -- source list/create/detail/delete + secret masking."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.stream_service import StreamService

STACK_URL = "https://connection.keboola.com"
TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
ALIAS = "padak"

SECRET = "opMczZin8tCT4jARu5yKrE9pNPFZ"
FULL_URL = f"https://stream-in.keboola.com/otlp/10539/my-otlp/{SECRET}"
BASE_URL = "https://stream-in.keboola.com/otlp/10539/my-otlp"

SOURCE = {
    "sourceId": "my-otlp",
    "type": "otlp",
    "name": "my-otlp",
    "description": "",
    "otlp": {"url": FULL_URL, "baseUrl": BASE_URL, "secret": SECRET},
}

SINKS = {
    "sinks": [
        {
            "sinkId": "logs",
            "allowedSignals": ["logs"],
            "table": {"type": "keboola", "tableId": "in.c-otlp-my-otlp.logs"},
        },
        {
            "sinkId": "traces",
            "allowedSignals": ["traces"],
            "table": {"type": "keboola", "tableId": "in.c-otlp-my-otlp.traces"},
        },
    ]
}


@pytest.fixture
def store(tmp_config_dir: Path) -> ConfigStore:
    s = ConfigStore(config_dir=tmp_config_dir)
    s.add_project(
        ALIAS,
        ProjectConfig(stack_url=STACK_URL, token=TOKEN, project_name="Padak 2.0", project_id=10539),
    )
    return s


@pytest.fixture
def client_factory() -> tuple[MagicMock, MagicMock]:
    mock = MagicMock()
    mock.list_sinks.return_value = SINKS
    factory = MagicMock(return_value=mock)
    return factory, mock


def _svc(store: ConfigStore, factory: MagicMock) -> StreamService:
    return StreamService(store, stream_client_factory=factory)


class TestListSources:
    def test_list(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.list_sources.return_value = {"sources": [SOURCE]}
        result = _svc(store, factory).list_sources(alias=ALIAS)
        assert result["alias"] == ALIAS
        assert result["branch_id"] == "default"
        assert result["sources"][0]["source_id"] == "my-otlp"
        # list view exposes only the secret-free base endpoint
        assert result["sources"][0]["base_endpoint"] == BASE_URL
        factory.assert_called_once_with(STACK_URL, TOKEN)
        mock.close.assert_called_once()

    def test_unknown_alias_raises(self, store, client_factory) -> None:
        factory, _ = client_factory
        with pytest.raises(ConfigError):
            _svc(store, factory).list_sources(alias="nope")

    def test_branch_override(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.list_sources.return_value = {"sources": []}
        _svc(store, factory).list_sources(alias=ALIAS, branch_id="1234")
        mock.list_sources.assert_called_once_with("1234")


class TestDetail:
    def test_detail_masks_secret_by_default(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.get_source.return_value = SOURCE
        result = _svc(store, factory).get_source_detail(alias=ALIAS, source_id="my-otlp")
        assert SECRET not in result["endpoint"]
        assert result["endpoint"].endswith("/***")
        assert result["secret_revealed"] is False
        # per-signal endpoints derived and masked
        assert result["signal_endpoints"]["logs"].endswith("/***/v1/logs")
        assert SECRET not in result["signal_endpoints"]["traces"]
        # raw source echo is sanitised
        assert result["source"]["otlp"]["secret"] == "***"
        assert SECRET not in result["source"]["otlp"]["url"]

    def test_detail_reveal(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.get_source.return_value = SOURCE
        result = _svc(store, factory).get_source_detail(
            alias=ALIAS, source_id="my-otlp", reveal=True
        )
        assert result["endpoint"] == FULL_URL
        assert result["signal_endpoints"]["logs"] == f"{FULL_URL}/v1/logs"
        assert result["source"]["otlp"]["secret"] == SECRET

    def test_http_detail_masks_secret(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.get_source.return_value = {
            "sourceId": "http",
            "type": "http",
            "http": {"url": f"https://stream-in/http/{SECRET}", "secret": SECRET},
        }
        result = _svc(store, factory).get_source_detail(alias=ALIAS, source_id="http")
        assert SECRET not in str(result)
        assert result["source"]["http"]["secret"] == "***"

    def test_detail_destination_from_sinks(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.get_source.return_value = SOURCE
        result = _svc(store, factory).get_source_detail(alias=ALIAS, source_id="my-otlp")
        assert result["destination"]["tables"]["logs"] == "in.c-otlp-my-otlp.logs"
        assert result["destination"]["bucket"] == "in.c-otlp-my-otlp"
        assert result["protocol"] == "http/protobuf"

    def test_detail_by_name(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.list_sources.return_value = {"sources": [SOURCE]}
        result = _svc(store, factory).get_source_detail(alias=ALIAS, name="my-otlp")
        assert result["source_id"] == "my-otlp"
        mock.get_source.assert_not_called()

    def test_detail_name_not_found(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.list_sources.return_value = {"sources": []}
        with pytest.raises(KeboolaApiError) as exc:
            _svc(store, factory).get_source_detail(alias=ALIAS, name="ghost")
        assert exc.value.error_code == "NOT_FOUND"

    def test_detail_requires_id_or_name(self, store, client_factory) -> None:
        factory, _ = client_factory
        with pytest.raises(ConfigError):
            _svc(store, factory).get_source_detail(alias=ALIAS)


class TestCreate:
    def test_create_provisions_three_otlp_sinks(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.create_source.return_value = {"taskId": "t1", "isFinished": False}
        mock.wait_for_task.return_value = {
            "taskId": "t1",
            "isFinished": True,
            "status": "success",
            "outputs": {"sourceId": "my-otlp"},
        }
        mock.list_sinks.return_value = {"sinks": []}  # nothing yet -> all 3 created
        mock.create_sink.return_value = {"taskId": "s", "isFinished": False}
        mock.get_source.return_value = SOURCE
        result = _svc(store, factory).create_source(alias=ALIAS, name="my-otlp")
        assert result["status"] == "created"
        assert result["source_id"] == "my-otlp"
        # one sink per signal, into in.c-otlp-<sourceId>.<signal>
        created_tables = sorted(c.kwargs["table_id"] for c in mock.create_sink.call_args_list)
        assert created_tables == [
            "in.c-otlp-my-otlp.logs",
            "in.c-otlp-my-otlp.metrics",
            "in.c-otlp-my-otlp.traces",
        ]
        # wait_for_task: 1 source + 3 sinks
        assert mock.wait_for_task.call_count == 4
        mock.get_source.assert_called_once_with("default", "my-otlp")

    @pytest.mark.parametrize("source_type", ["otlp", "http"])
    def test_create_no_sinks_skips_provisioning(self, store, client_factory, source_type) -> None:
        factory, mock = client_factory
        mock.create_source.return_value = {"taskId": "t1", "isFinished": False}
        mock.wait_for_task.return_value = {"isFinished": True, "outputs": {"sourceId": "my-otlp"}}
        mock.get_source.return_value = SOURCE
        result = _svc(store, factory).create_source(
            alias=ALIAS, name="my-otlp", source_type=source_type, provision_sinks=False
        )
        assert result["status"] == "created"
        mock.create_sink.assert_not_called()

    def test_provisioning_is_idempotent(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.create_source.return_value = {"taskId": "t1", "isFinished": False}
        mock.wait_for_task.return_value = {"isFinished": True, "outputs": {"sourceId": "my-otlp"}}
        # logs + traces already exist -> only metrics is created
        mock.list_sinks.return_value = {
            "sinks": [
                {"allowedSignals": ["logs"], "table": {"tableId": "in.c-otlp-my-otlp.logs"}},
                {"allowedSignals": ["traces"], "table": {"tableId": "in.c-otlp-my-otlp.traces"}},
            ]
        }
        mock.create_sink.return_value = {"isFinished": False}
        mock.get_source.return_value = SOURCE
        _svc(store, factory).create_source(alias=ALIAS, name="my-otlp")
        assert mock.create_sink.call_count == 1
        assert mock.create_sink.call_args.kwargs["table_id"] == "in.c-otlp-my-otlp.metrics"

    @pytest.mark.parametrize(
        "columns", [None, [], [{"name": "user", "type": "path", "path": "user.id"}]]
    )
    @pytest.mark.parametrize("table_id", [None, "in.c-webhooks.events"])
    def test_http_source_provisions_sink(self, store, client_factory, columns, table_id) -> None:
        factory, mock = client_factory
        mock.create_source.return_value = {"taskId": "t1", "isFinished": False}
        mock.wait_for_task.return_value = {"isFinished": True, "outputs": {"sourceId": "my-http"}}
        mock.get_source.return_value = {"sourceId": "my-http", "type": "http", "http": {}}
        _svc(store, factory).create_source(
            alias=ALIAS, name="my-http", source_type="http", columns=columns, table_id=table_id
        )
        mock.create_sink.assert_called_once_with(
            "default",
            "my-http",
            name="my-http",
            table_id=table_id or "in.c-data-stream-my-http.events",
            columns=columns
            or [
                {"name": "id", "type": "uuid"},
                {"name": "datetime", "type": "datetime"},
                {"name": "ip", "type": "ip"},
                {"name": "body", "type": "body"},
                {"name": "headers", "type": "headers"},
            ],
        )
        assert mock.wait_for_task.call_count == 2

    @pytest.mark.parametrize(
        "sinks", [[], [{"table": {"tableId": "in.c-data-stream-my-http.events"}}]]
    )
    def test_http_if_not_exists_reconciles(self, store, client_factory, sinks) -> None:
        factory, mock = client_factory
        mock.list_sources.return_value = {"sources": [{"sourceId": "my-http", "type": "http"}]}
        mock.list_sinks.return_value = {"sinks": sinks}
        result = _svc(store, factory).create_source(
            alias=ALIAS, name="my-http", source_type="http", if_not_exists=True
        )
        assert result["status"] == "skipped"
        assert mock.create_sink.call_count == (0 if sinks else 1)
        mock.create_source.assert_not_called()

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"columns": {}},
            {"columns": [1]},
            {"columns": [{"name": "x", "type": "bad"}]},
            {"columns": [{"type": "body"}]},
            {"table_id": " "},
            {"table_id": "in.c-x.events", "provision_sinks": False},
            {"source_type": "otlp", "columns": []},
        ],
    )
    def test_http_invalid_options_before_writes(
        self, store, client_factory, kwargs: dict[str, Any]
    ) -> None:
        factory, mock = client_factory
        options: dict[str, Any] = {"source_type": "http", **kwargs}
        with pytest.raises(ConfigError):
            _svc(store, factory).create_source(alias=ALIAS, name="s", **options)
        mock.create_source.assert_not_called()
        factory.assert_not_called()

    def test_http_sink_failure_propagates_and_closes(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.wait_for_task.return_value = {"outputs": {"sourceId": "s"}}
        mock.get_source.return_value = {"sourceId": "s", "type": "http"}
        mock.create_sink.side_effect = KeboolaApiError(message="Sink failed")
        with pytest.raises(KeboolaApiError, match="Sink failed"):
            _svc(store, factory).create_source(alias=ALIAS, name="s", source_type="http")
        mock.close.assert_called_once()

    def test_if_not_exists_skips(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.list_sources.return_value = {"sources": [SOURCE]}
        result = _svc(store, factory).create_source(alias=ALIAS, name="my-otlp", if_not_exists=True)
        assert result["status"] == "skipped"
        mock.create_source.assert_not_called()


class TestDelete:
    def test_dry_run(self, store, client_factory) -> None:
        factory, mock = client_factory
        result = _svc(store, factory).delete_source(alias=ALIAS, source_id="my-otlp", dry_run=True)
        assert result["status"] == "dry_run"
        mock.delete_source.assert_not_called()

    def test_real_delete_polls(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.delete_source.return_value = {"taskId": "t2", "isFinished": False}
        mock.wait_for_task.return_value = {"isFinished": True, "status": "success"}
        result = _svc(store, factory).delete_source(alias=ALIAS, source_id="my-otlp")
        assert result["status"] == "deleted"
        mock.delete_source.assert_called_once_with("default", "my-otlp")
        mock.wait_for_task.assert_called_once()
