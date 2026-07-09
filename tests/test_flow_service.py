"""Unit tests for FlowService (conditional flows only)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.services.flow_service import (
    FLOW_COMPONENT_ID,
    FlowService,
    _parse_configuration,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_config_store(projects: dict) -> MagicMock:
    cs = MagicMock()
    config = MagicMock()
    config.projects = {
        alias: MagicMock(stack_url=v["url"], token=v["token"], active_branch_id=None)
        for alias, v in projects.items()
    }
    config.max_parallel_workers = 10
    cs.load.return_value = config
    cs.get_project.side_effect = lambda alias: config.projects.get(alias)
    return cs


# A minimal keboola.flow configurationSchema for tests -- enough to exercise the
# structural-validation path without touching the network.
_FLOW_SCHEMA: dict = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["phases", "tasks"],
    "properties": {
        "phases": {"type": "array"},
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["job", "notification", "variable"]}
                        },
                    }
                },
            },
        },
    },
}


def _make_ai_client(schema: dict | None = _FLOW_SCHEMA, raise_exc: Exception | None = None):
    """Build a mock AiServiceClient returning a keboola.flow component detail."""
    ai = MagicMock()
    if raise_exc is not None:
        ai.get_component_detail.side_effect = raise_exc
    else:
        ai.get_component_detail.return_value = {
            "componentId": FLOW_COMPONENT_ID,
            "componentName": "Conditional Flow",
            "componentType": "other",
            "configurationSchema": schema or {},
        }
    return ai


def _make_scheduler_client() -> MagicMock:
    """Build a mock SchedulerClient usable as a context manager."""
    scheduler = MagicMock()
    scheduler.__enter__.return_value = scheduler
    scheduler.activate_schedule.return_value = {"id": "77"}
    return scheduler


def _make_flow_service(
    mock_client: MagicMock,
    projects: dict | None = None,
    ai_client: MagicMock | None = None,
    scheduler_client: MagicMock | None = None,
) -> FlowService:
    if projects is None:
        projects = {"prod": {"url": "https://connection.keboola.com", "token": "tok"}}
    cs = _mock_config_store(projects)
    ai = ai_client if ai_client is not None else _make_ai_client()
    scheduler = scheduler_client if scheduler_client is not None else _make_scheduler_client()
    return FlowService(
        config_store=cs,
        client_factory=lambda url, token: mock_client,
        ai_client_factory=lambda url, token: ai,
        scheduler_client_factory=lambda url, token: scheduler,
    )


def _valid_body():
    phases = [
        {"id": "p1", "name": "P1", "next": [{"id": "n", "goto": None}]},
    ]
    tasks = [
        {
            "id": "t1",
            "name": "T1",
            "phase": "p1",
            "task": {"type": "job", "componentId": "c", "configId": "1", "mode": "run"},
        },
    ]
    return phases, tasks


# ---------------------------------------------------------------------------
# Helpers unit tests
# ---------------------------------------------------------------------------


class TestParseConfiguration:
    def test_dict_passthrough(self):
        body = {"phases": [1, 2], "tasks": [3]}
        assert _parse_configuration(body) == body

    def test_json_string_parsed(self):
        assert _parse_configuration('{"phases": []}') == {"phases": []}

    def test_invalid_json_returns_empty(self):
        assert _parse_configuration("not-json") == {}

    def test_none_returns_empty(self):
        assert _parse_configuration(None) == {}


# ---------------------------------------------------------------------------
# Component constant
# ---------------------------------------------------------------------------


def test_component_id_constant():
    assert FLOW_COMPONENT_ID == "keboola.flow"


# ---------------------------------------------------------------------------
# create_flow
# ---------------------------------------------------------------------------


def test_create_flow_rejects_invalid_definition():
    client = MagicMock()
    svc = _make_flow_service(client)
    # task references a phase that does not exist -> semantic error
    phases = [{"id": "p1", "name": "P1", "next": [{"id": "n", "goto": None}]}]
    tasks = [
        {
            "id": "t1",
            "name": "T1",
            "phase": "ghost",
            "task": {"type": "job", "componentId": "c", "configId": "1", "mode": "run"},
        }
    ]
    with pytest.raises(KeboolaApiError) as exc:
        svc.create_flow(alias="prod", name="F", phases=phases, tasks=tasks)
    assert exc.value.error_code == ErrorCode.INVALID_FLOW_DEFINITION


def test_create_flow_uses_keboola_flow_component():
    client = MagicMock()
    client.create_config.return_value = {"id": "999", "name": "F"}
    svc = _make_flow_service(client)
    phases, tasks = _valid_body()
    result = svc.create_flow(alias="prod", name="F", phases=phases, tasks=tasks)
    assert client.create_config.call_args.kwargs["component_id"] == "keboola.flow"
    assert result["id"] == "999"


def test_create_flow_attaches_unreachable_warnings():
    client = MagicMock()
    client.create_config.return_value = {"id": "999", "name": "F"}
    svc = _make_flow_service(client)
    phases = [
        {"id": "p1", "name": "P1", "next": [{"id": "n", "goto": None}]},
        {"id": "island", "name": "Island"},  # unreachable
    ]
    tasks = [
        {
            "id": "t1",
            "name": "T1",
            "phase": "p1",
            "task": {"type": "job", "componentId": "c", "configId": "1", "mode": "run"},
        },
        {
            "id": "t2",
            "name": "T2",
            "phase": "island",
            "task": {"type": "job", "componentId": "c", "configId": "2", "mode": "run"},
        },
    ]
    result = svc.create_flow(alias="prod", name="F", phases=phases, tasks=tasks)
    assert any("island" in w for w in result["warnings"])


def test_create_flow_full_validation_rejects_bad_structure():
    # Live schema present -> structural validation catches the bad task type.
    client = MagicMock()
    client.create_config.return_value = {"id": "999", "name": "F"}
    svc = _make_flow_service(client)
    phases = [{"id": "p1", "name": "P1", "next": [{"id": "n", "goto": None}]}]
    tasks = [
        {
            "id": "t1",
            "name": "T1",
            "phase": "p1",
            "task": {"type": "nonsense", "componentId": "c", "configId": "1", "mode": "run"},
        }
    ]
    with pytest.raises(KeboolaApiError) as exc:
        svc.create_flow(alias="prod", name="F", phases=phases, tasks=tasks)
    assert exc.value.error_code == ErrorCode.INVALID_FLOW_DEFINITION
    client.create_config.assert_not_called()


def test_create_flow_schema_fetch_failure_degrades_to_semantic_only():
    # AI Service raises -> structural validation skipped, semantic checks run,
    # write proceeds with a warning. The bad task type slips through (no schema).
    client = MagicMock()
    client.create_config.return_value = {"id": "999", "name": "F"}
    ai = _make_ai_client(
        raise_exc=KeboolaApiError("boom", status_code=500, error_code="NETWORK_ERROR")
    )
    svc = _make_flow_service(client, ai_client=ai)
    phases = [{"id": "p1", "name": "P1", "next": [{"id": "n", "goto": None}]}]
    tasks = [
        {
            "id": "t1",
            "name": "T1",
            "phase": "p1",
            "task": {"type": "nonsense", "componentId": "c", "configId": "1", "mode": "run"},
        }
    ]
    result = svc.create_flow(alias="prod", name="F", phases=phases, tasks=tasks)
    assert result["id"] == "999"
    assert any("structural schema validation skipped" in w for w in result["warnings"])


def test_create_flow_empty_schema_degrades_to_semantic_only():
    client = MagicMock()
    client.create_config.return_value = {"id": "999", "name": "F"}
    ai = _make_ai_client(schema={})  # empty configurationSchema
    svc = _make_flow_service(client, ai_client=ai)
    phases, tasks = _valid_body()
    result = svc.create_flow(alias="prod", name="F", phases=phases, tasks=tasks)
    assert result["id"] == "999"
    assert any("structural schema validation skipped" in w for w in result["warnings"])


def test_create_flow_fetch_failure_still_rejects_semantic_errors():
    # Even without a schema, a semantic error must still reject the write.
    client = MagicMock()
    ai = _make_ai_client(raise_exc=RuntimeError("network down"))
    svc = _make_flow_service(client, ai_client=ai)
    phases = [{"id": "p1", "name": "P1", "next": [{"id": "n", "goto": None}]}]
    tasks = [
        {
            "id": "t1",
            "name": "T1",
            "phase": "ghost",  # semantic error
            "task": {"type": "job", "componentId": "c", "configId": "1", "mode": "run"},
        }
    ]
    with pytest.raises(KeboolaApiError) as exc:
        svc.create_flow(alias="prod", name="F", phases=phases, tasks=tasks)
    assert exc.value.error_code == ErrorCode.INVALID_FLOW_DEFINITION


def test_fetch_flow_schema_success():
    svc = _make_flow_service(MagicMock())
    fetch = svc.fetch_flow_schema("prod")
    assert fetch.reason is None
    assert fetch.schema and fetch.schema["required"] == ["phases", "tasks"]


def test_fetch_flow_schema_empty_returns_reason():
    svc = _make_flow_service(MagicMock(), ai_client=_make_ai_client(schema={}))
    fetch = svc.fetch_flow_schema("prod")
    assert fetch.schema is None
    assert fetch.reason and "configurationSchema" in fetch.reason


def test_fetch_flow_schema_error_returns_reason():
    ai = _make_ai_client(raise_exc=KeboolaApiError("nope", status_code=404, error_code="NOT_FOUND"))
    svc = _make_flow_service(MagicMock(), ai_client=ai)
    fetch = svc.fetch_flow_schema("prod")
    assert fetch.schema is None
    assert fetch.reason == "nope"


# ---------------------------------------------------------------------------
# update_flow (merge-aware validation)
# ---------------------------------------------------------------------------


def test_update_flow_validates_merged_body():
    client = MagicMock()
    # Current remote body has valid phases; update supplies only tasks that break it.
    client.get_config_detail.return_value = {
        "configuration": {
            "phases": [{"id": "p1", "name": "P1", "next": [{"id": "n", "goto": None}]}],
            "tasks": [],
        }
    }
    svc = _make_flow_service(client)
    bad_tasks = [
        {
            "id": "t1",
            "name": "T1",
            "phase": "ghost",
            "task": {"type": "job", "componentId": "c", "configId": "1", "mode": "run"},
        }
    ]
    with pytest.raises(KeboolaApiError) as exc:
        svc.update_flow(alias="prod", config_id="5", tasks=bad_tasks)
    assert exc.value.error_code == ErrorCode.INVALID_FLOW_DEFINITION


def test_update_flow_uses_keboola_flow_component():
    client = MagicMock()
    client.update_config.return_value = {"id": "5", "name": "renamed"}
    svc = _make_flow_service(client)
    result = svc.update_flow(alias="prod", config_id="5", name="renamed")
    assert client.update_config.call_args.kwargs["component_id"] == "keboola.flow"
    assert result["id"] == "5"


# ---------------------------------------------------------------------------
# list_flows
# ---------------------------------------------------------------------------


def test_list_flows_reports_legacy_orchestrator_count():
    client = MagicMock()

    def list_configs(component_id, branch_id=None):
        if component_id == "keboola.flow":
            return [{"id": "1", "name": "CF"}]
        if component_id == "keboola.orchestrator":
            return [{"id": "9", "name": "Old"}, {"id": "10", "name": "Old2"}]
        return []

    client.list_component_configs.side_effect = list_configs
    svc = _make_flow_service(client)
    result = svc.list_flows(aliases=["prod"])
    assert result["legacy_orchestrator_count"] == 2
    assert all(f["component_id"] == "keboola.flow" for f in result["flows"])


def test_list_flows_legacy_count_zero_when_orchestrator_404():
    client = MagicMock()

    def list_configs(component_id, branch_id=None):
        if component_id == "keboola.flow":
            return [{"id": "1", "name": "CF"}]
        raise KeboolaApiError(message="nope", status_code=404, error_code="NOT_FOUND")

    client.list_component_configs.side_effect = list_configs
    svc = _make_flow_service(client)
    result = svc.list_flows(aliases=["prod"])
    assert result["legacy_orchestrator_count"] == 0
    assert len(result["flows"]) == 1


# ---------------------------------------------------------------------------
# delete_flow / detail
# ---------------------------------------------------------------------------


def test_delete_flow_uses_keboola_flow_component():
    client = MagicMock()
    svc = _make_flow_service(client)
    result = svc.delete_flow(alias="prod", config_id="5")
    assert client.delete_config.call_args.kwargs["component_id"] == "keboola.flow"
    assert result["component_id"] == "keboola.flow"


def test_get_flow_detail_uses_keboola_flow_component():
    client = MagicMock()
    client.get_config_detail.return_value = {
        "id": "5",
        "name": "CF",
        "configuration": {"phases": [{"id": "p1"}], "tasks": []},
    }
    svc = _make_flow_service(client)
    result = svc.get_flow_detail(alias="prod", config_id="5")
    assert client.get_config_detail.call_args[0][0] == "keboola.flow"
    assert result["component_id"] == "keboola.flow"
    assert result["phase_count"] == 1


# ---------------------------------------------------------------------------
# schedules
# ---------------------------------------------------------------------------


def test_set_flow_schedule_targets_keboola_flow():
    client = MagicMock()
    client.get_config_detail.return_value = {"name": "CF"}
    client.list_component_configs.return_value = []
    client.create_config.return_value = {"id": "77"}
    svc = _make_flow_service(client)
    result = svc.set_flow_schedule(alias="prod", config_id="5", cron_tab="0 6 * * *")
    # scheduler config created with target.componentId == keboola.flow
    cfg = client.create_config.call_args.kwargs["configuration"]
    assert cfg["target"]["componentId"] == "keboola.flow"
    assert result["component_id"] == "keboola.flow"


def test_set_flow_schedule_activates_created_config():
    client = MagicMock()
    client.get_config_detail.return_value = {"name": "CF"}
    client.list_component_configs.return_value = []
    client.create_config.return_value = {"id": "77"}
    scheduler = _make_scheduler_client()
    svc = _make_flow_service(client, scheduler_client=scheduler)
    result = svc.set_flow_schedule(alias="prod", config_id="5", cron_tab="0 6 * * *")
    # activation targets the scheduler config id, not the flow id
    scheduler.activate_schedule.assert_called_once_with("77")
    assert result["activated"] is True
    assert result["warnings"] == []


def test_set_flow_schedule_activates_updated_config():
    client = MagicMock()
    client.get_config_detail.return_value = {"name": "CF"}
    client.list_component_configs.return_value = [
        {
            "id": "88",
            "configuration": {
                "target": {"componentId": "keboola.flow", "configurationId": "5"},
            },
        }
    ]
    client.update_config.return_value = {"id": "88"}
    scheduler = _make_scheduler_client()
    svc = _make_flow_service(client, scheduler_client=scheduler)
    result = svc.set_flow_schedule(alias="prod", config_id="5", cron_tab="0 6 * * *")
    assert result["status"] == "updated"
    scheduler.activate_schedule.assert_called_once_with("88")
    assert result["activated"] is True


def test_set_flow_schedule_disabled_still_calls_scheduler_service():
    client = MagicMock()
    client.get_config_detail.return_value = {"name": "CF"}
    client.list_component_configs.return_value = []
    client.create_config.return_value = {"id": "77"}
    scheduler = _make_scheduler_client()
    svc = _make_flow_service(client, scheduler_client=scheduler)
    svc.set_flow_schedule(alias="prod", config_id="5", cron_tab="0 6 * * *", enabled=False)
    scheduler.activate_schedule.assert_called_once_with("77")


def test_set_flow_schedule_activation_failure_warns_not_raises():
    client = MagicMock()
    client.get_config_detail.return_value = {"name": "CF"}
    client.list_component_configs.return_value = []
    client.create_config.return_value = {"id": "77"}
    scheduler = _make_scheduler_client()
    scheduler.activate_schedule.side_effect = KeboolaApiError(
        message="Access denied",
        status_code=403,
        error_code=ErrorCode.ACCESS_DENIED,
        retryable=False,
    )
    svc = _make_flow_service(client, scheduler_client=scheduler)
    result = svc.set_flow_schedule(alias="prod", config_id="5", cron_tab="0 6 * * *")
    assert result["status"] == "created"
    assert result["activated"] is False
    assert len(result["warnings"]) == 1
    assert "NOT fire" in result["warnings"][0]


def test_remove_flow_schedule_filters_keboola_flow():
    client = MagicMock()
    client.list_component_configs.return_value = [
        {
            "id": "77",
            "configuration": {
                "target": {"componentId": "keboola.flow", "configurationId": "5"},
            },
        }
    ]
    svc = _make_flow_service(client)
    result = svc.remove_flow_schedule(alias="prod", config_id="5")
    assert result["deleted_count"] == 1
    assert result["component_id"] == "keboola.flow"


def test_remove_flow_schedule_deregisters_from_scheduler_service():
    client = MagicMock()
    client.list_component_configs.return_value = [
        {
            "id": "77",
            "configuration": {
                "target": {"componentId": "keboola.flow", "configurationId": "5"},
            },
        }
    ]
    scheduler = _make_scheduler_client()
    svc = _make_flow_service(client, scheduler_client=scheduler)
    result = svc.remove_flow_schedule(alias="prod", config_id="5")
    scheduler.remove_schedule.assert_called_once_with("77")
    assert result["deleted_count"] == 1
    assert result["warnings"] == []


def test_remove_flow_schedule_tolerates_missing_service_registration():
    client = MagicMock()
    client.list_component_configs.return_value = [
        {
            "id": "77",
            "configuration": {
                "target": {"componentId": "keboola.flow", "configurationId": "5"},
            },
        }
    ]
    scheduler = _make_scheduler_client()
    scheduler.remove_schedule.side_effect = KeboolaApiError(
        message="Schedule not found",
        status_code=404,
        error_code=ErrorCode.NOT_FOUND,
        retryable=False,
    )
    svc = _make_flow_service(client, scheduler_client=scheduler)
    result = svc.remove_flow_schedule(alias="prod", config_id="5")
    assert result["deleted_count"] == 1
    assert result["warnings"] == []


def test_remove_flow_schedule_service_failure_warns_but_deletes_config():
    client = MagicMock()
    client.list_component_configs.return_value = [
        {
            "id": "77",
            "configuration": {
                "target": {"componentId": "keboola.flow", "configurationId": "5"},
            },
        }
    ]
    scheduler = _make_scheduler_client()
    scheduler.remove_schedule.side_effect = KeboolaApiError(
        message="Access denied",
        status_code=403,
        error_code=ErrorCode.ACCESS_DENIED,
        retryable=False,
    )
    svc = _make_flow_service(client, scheduler_client=scheduler)
    result = svc.remove_flow_schedule(alias="prod", config_id="5")
    assert result["deleted_count"] == 1
    assert len(result["warnings"]) == 1
    client.delete_config.assert_called_once()
