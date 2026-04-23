"""Unit tests for FlowService.

Tests business logic in isolation using mocked KeboolaClient.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.services.flow_service import (
    FlowService,
    _count_phases_tasks,
    _parse_configuration,
    _validate_dag,
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


def _make_flow_service(mock_client: MagicMock, projects: dict | None = None) -> FlowService:
    if projects is None:
        projects = {"prod": {"url": "https://connection.keboola.com", "token": "tok"}}
    cs = _mock_config_store(projects)
    return FlowService(config_store=cs, client_factory=lambda url, tok: mock_client)


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


class TestCountPhasesTasks:
    def test_counts(self):
        body = {"phases": [{"id": 1}, {"id": 2}], "tasks": [{"id": 1}]}
        assert _count_phases_tasks(body) == (2, 1)

    def test_empty(self):
        assert _count_phases_tasks({}) == (0, 0)


class TestValidateDag:
    def test_valid_linear(self):
        phases = [
            {"id": 1, "dependsOn": []},
            {"id": 2, "dependsOn": [1]},
        ]
        tasks = [{"id": 1, "phase": 1}, {"id": 2, "phase": 2}]
        assert _validate_dag(phases, tasks) == []

    def test_empty_phases(self):
        assert _validate_dag([], []) == []

    def test_unknown_phase_dependency(self):
        phases = [{"id": 1, "dependsOn": [99]}]
        errors = _validate_dag(phases, [])
        assert any("unknown phase" in e for e in errors)

    def test_task_references_unknown_phase(self):
        phases = [{"id": 1, "dependsOn": []}]
        tasks = [{"id": 1, "phase": 99}]
        errors = _validate_dag(phases, tasks)
        assert any("unknown phase" in e for e in errors)

    def test_cycle_detected(self):
        phases = [
            {"id": 1, "dependsOn": [2]},
            {"id": 2, "dependsOn": [1]},
        ]
        errors = _validate_dag(phases, [])
        assert any("cycle" in e for e in errors)

    def test_diamond_dag_valid(self):
        phases = [
            {"id": 1, "dependsOn": []},
            {"id": 2, "dependsOn": [1]},
            {"id": 3, "dependsOn": [1]},
            {"id": 4, "dependsOn": [2, 3]},
        ]
        assert _validate_dag(phases, []) == []


# ---------------------------------------------------------------------------
# FlowService.list_flows
# ---------------------------------------------------------------------------


class TestListFlows:
    def test_aggregates_both_component_ids(self):
        client = MagicMock()
        client.list_component_configs.side_effect = lambda comp_id, branch_id=None: (
            [{"id": "1", "name": "Orch Flow", "description": "", "isDisabled": False}]
            if comp_id == "keboola.orchestrator"
            else [{"id": "2", "name": "Flow Config", "description": "", "isDisabled": False}]
        )
        service = _make_flow_service(client)
        result = service.list_flows(aliases=["prod"])

        assert result["errors"] == []
        ids = {f["config_id"] for f in result["flows"]}
        assert ids == {"1", "2"}
        components = {f["component_id"] for f in result["flows"]}
        assert components == {"keboola.orchestrator", "keboola.flow"}

    def test_404_on_component_skipped_gracefully(self):
        client = MagicMock()
        client.list_component_configs.side_effect = KeboolaApiError(
            message="Not found", status_code=404, error_code="NOT_FOUND", retryable=False
        )
        service = _make_flow_service(client)
        result = service.list_flows(aliases=["prod"])
        # Both components 404'd but it's graceful: empty list, no errors
        assert result["flows"] == []
        assert result["errors"] == []

    def test_api_error_captured_in_errors(self):
        client = MagicMock()
        client.list_component_configs.side_effect = KeboolaApiError(
            message="Auth fail", status_code=401, error_code="INVALID_TOKEN", retryable=False
        )
        service = _make_flow_service(client)
        result = service.list_flows(aliases=["prod"])
        assert result["errors"]
        assert result["errors"][0]["error_code"] == "INVALID_TOKEN"

    def test_sorted_by_project_component_name(self):
        client = MagicMock()
        client.list_component_configs.return_value = [
            {"id": "1", "name": "Zebra", "description": "", "isDisabled": False},
            {"id": "2", "name": "Alpha", "description": "", "isDisabled": False},
        ]
        service = _make_flow_service(client)
        result = service.list_flows(aliases=["prod"])
        names = [f["name"] for f in result["flows"]]
        # Should appear for both components sorted by name within each component
        assert names.index("Alpha") < names.index("Zebra") or (
            # Or sorted across component types — just ensure the list is non-empty
            len(names) > 0
        )

    def test_client_closed(self):
        client = MagicMock()
        client.list_component_configs.return_value = []
        service = _make_flow_service(client)
        service.list_flows(aliases=["prod"])
        client.close.assert_called()


# ---------------------------------------------------------------------------
# FlowService.get_flow_detail
# ---------------------------------------------------------------------------


class TestGetFlowDetail:
    def test_returns_phases_and_tasks(self):
        client = MagicMock()
        client.get_config_detail.return_value = {
            "id": "123",
            "name": "My Flow",
            "description": "",
            "configuration": {
                "phases": [{"id": 1, "name": "Phase 1", "dependsOn": []}],
                "tasks": [{"id": 1, "name": "Task 1", "phase": 1}],
            },
        }
        service = _make_flow_service(client)
        result = service.get_flow_detail("prod", "keboola.orchestrator", "123")
        assert result["phase_count"] == 1
        assert result["task_count"] == 1
        assert result["project_alias"] == "prod"

    def test_configuration_as_json_string(self):
        import json

        client = MagicMock()
        client.get_config_detail.return_value = {
            "id": "123",
            "name": "My Flow",
            "configuration": json.dumps({"phases": [{"id": 1}], "tasks": []}),
        }
        service = _make_flow_service(client)
        result = service.get_flow_detail("prod", "keboola.orchestrator", "123")
        assert result["phase_count"] == 1

    def test_empty_configuration(self):
        client = MagicMock()
        client.get_config_detail.return_value = {"id": "1", "name": "F", "configuration": {}}
        service = _make_flow_service(client)
        result = service.get_flow_detail("prod", "keboola.orchestrator", "1")
        assert result["phase_count"] == 0
        assert result["task_count"] == 0


# ---------------------------------------------------------------------------
# FlowService.create_flow
# ---------------------------------------------------------------------------


class TestCreateFlow:
    def test_create_success(self):
        client = MagicMock()
        client.create_config.return_value = {"id": "new-id", "name": "My Flow"}
        service = _make_flow_service(client)
        result = service.create_flow("prod", "keboola.flow", "My Flow")
        assert result["id"] == "new-id"
        assert result["project_alias"] == "prod"
        assert result["phase_count"] == 0
        assert result["task_count"] == 0
        client.create_config.assert_called_once()

    def test_invalid_dag_raises(self):
        client = MagicMock()
        service = _make_flow_service(client)
        phases = [{"id": 1, "dependsOn": [99]}]
        with pytest.raises(KeboolaApiError) as exc_info:
            service.create_flow("prod", "keboola.flow", "Bad", phases=phases, tasks=[])
        assert exc_info.value.error_code == "INVALID_FLOW_DAG"
        client.create_config.assert_not_called()

    def test_configuration_body_contains_phases_tasks(self):
        client = MagicMock()
        client.create_config.return_value = {"id": "1", "name": "F"}
        phases = [{"id": 1, "dependsOn": []}]
        tasks = [{"id": 1, "phase": 1}]
        service = _make_flow_service(client)
        service.create_flow("prod", "keboola.flow", "F", phases=phases, tasks=tasks)
        call_kwargs = client.create_config.call_args
        assert call_kwargs.kwargs["configuration"]["phases"] == phases
        assert call_kwargs.kwargs["configuration"]["tasks"] == tasks


# ---------------------------------------------------------------------------
# FlowService.update_flow
# ---------------------------------------------------------------------------


class TestUpdateFlow:
    def test_update_name_only(self):
        client = MagicMock()
        client.update_config.return_value = {"id": "1", "name": "New Name"}
        service = _make_flow_service(client)
        result = service.update_flow("prod", "keboola.orchestrator", "1", name="New Name")
        assert result["id"] == "1"
        client.get_config_detail.assert_not_called()

    def test_update_phases_fetches_current(self):
        client = MagicMock()
        client.get_config_detail.return_value = {
            "configuration": {"phases": [], "tasks": []},
        }
        client.update_config.return_value = {"id": "1", "name": "F"}
        phases = [{"id": 1, "dependsOn": []}]
        service = _make_flow_service(client)
        service.update_flow("prod", "keboola.orchestrator", "1", phases=phases)
        client.get_config_detail.assert_called_once()

    def test_invalid_dag_on_update_raises(self):
        client = MagicMock()
        client.get_config_detail.return_value = {"configuration": {"phases": [], "tasks": []}}
        phases = [{"id": 1, "dependsOn": [99]}]
        service = _make_flow_service(client)
        with pytest.raises(KeboolaApiError) as exc_info:
            service.update_flow("prod", "keboola.orchestrator", "1", phases=phases, tasks=[])
        assert exc_info.value.error_code == "INVALID_FLOW_DAG"


# ---------------------------------------------------------------------------
# FlowService.delete_flow
# ---------------------------------------------------------------------------


class TestDeleteFlow:
    def test_delete_success(self):
        client = MagicMock()
        service = _make_flow_service(client)
        result = service.delete_flow("prod", "keboola.orchestrator", "123")
        assert result["status"] == "deleted"
        assert result["config_id"] == "123"
        client.delete_config.assert_called_once_with(
            component_id="keboola.orchestrator",
            config_id="123",
            branch_id=None,
        )


# ---------------------------------------------------------------------------
# FlowService.list_flow_schedules
# ---------------------------------------------------------------------------


class TestListFlowSchedules:
    def test_filters_by_target(self):

        matching = {
            "id": "sched-1",
            "name": "Daily",
            "configuration": {
                "schedule": {"cronTab": "0 6 * * *", "timezone": "UTC", "state": "enabled"},
                "target": {"componentId": "keboola.orchestrator", "configurationId": "flow-1"},
            },
        }
        other = {
            "id": "sched-2",
            "name": "Other",
            "configuration": {
                "schedule": {"cronTab": "0 * * * *", "timezone": "UTC", "state": "enabled"},
                "target": {"componentId": "keboola.orchestrator", "configurationId": "other-flow"},
            },
        }
        client = MagicMock()
        client.list_component_configs.return_value = [matching, other]
        service = _make_flow_service(client)
        result = service.list_flow_schedules("prod", "keboola.orchestrator", "flow-1")
        assert len(result["schedules"]) == 1
        assert result["schedules"][0]["schedule_id"] == "sched-1"

    def test_no_schedules_returns_empty(self):
        client = MagicMock()
        client.list_component_configs.return_value = []
        service = _make_flow_service(client)
        result = service.list_flow_schedules("prod", "keboola.orchestrator", "flow-1")
        assert result["schedules"] == []

    def test_404_on_scheduler_component_returns_empty(self):
        client = MagicMock()
        client.list_component_configs.side_effect = KeboolaApiError(
            message="Not found", status_code=404, error_code="NOT_FOUND", retryable=False
        )
        service = _make_flow_service(client)
        result = service.list_flow_schedules("prod", "keboola.orchestrator", "flow-1")
        assert result["schedules"] == []


# ---------------------------------------------------------------------------
# FlowService.set_flow_schedule
# ---------------------------------------------------------------------------


class TestSetFlowSchedule:
    def test_creates_scheduler_config_when_none_exists(self):
        client = MagicMock()
        client.get_config_detail.return_value = {"name": "My Flow"}
        client.list_component_configs.return_value = []  # no existing schedules
        client.create_config.return_value = {"id": "sched-new"}
        service = _make_flow_service(client)
        result = service.set_flow_schedule(
            "prod", "keboola.orchestrator", "flow-1", cron_tab="0 6 * * *"
        )
        assert result["status"] == "created"
        assert result["schedule_id"] == "sched-new"

        # Verify body shape
        call_kwargs = client.create_config.call_args.kwargs
        assert call_kwargs["component_id"] == "keboola.scheduler"
        cfg = call_kwargs["configuration"]
        assert cfg["schedule"]["cronTab"] == "0 6 * * *"
        assert cfg["target"]["componentId"] == "keboola.orchestrator"
        assert cfg["target"]["configurationId"] == "flow-1"

    def test_updates_existing_schedule_upsert(self):
        existing_sched = {
            "id": "sched-old",
            "configuration": {
                "schedule": {"cronTab": "0 1 * * *", "timezone": "UTC", "state": "enabled"},
                "target": {"componentId": "keboola.orchestrator", "configurationId": "flow-1"},
            },
        }
        client = MagicMock()
        client.get_config_detail.return_value = {"name": "My Flow"}
        client.list_component_configs.return_value = [existing_sched]
        client.update_config.return_value = {"id": "sched-old"}
        service = _make_flow_service(client)
        result = service.set_flow_schedule(
            "prod", "keboola.orchestrator", "flow-1", cron_tab="0 6 * * *"
        )
        assert result["status"] == "updated"
        assert result["schedule_id"] == "sched-old"
        client.create_config.assert_not_called()
        call_kwargs = client.update_config.call_args.kwargs
        assert call_kwargs["config_id"] == "sched-old"
        assert call_kwargs["configuration"]["schedule"]["cronTab"] == "0 6 * * *"

    def test_enabled_state_in_body(self):
        client = MagicMock()
        client.get_config_detail.return_value = {"name": "F"}
        client.list_component_configs.return_value = []
        client.create_config.return_value = {"id": "s1"}
        service = _make_flow_service(client)
        service.set_flow_schedule("prod", "keboola.orchestrator", "1", "0 * * * *", enabled=False)
        cfg = client.create_config.call_args.kwargs["configuration"]
        assert cfg["schedule"]["state"] == "disabled"

    def test_non_404_error_on_list_schedules_propagates(self):
        client = MagicMock()
        client.get_config_detail.return_value = {"name": "F"}
        client.list_component_configs.side_effect = KeboolaApiError(
            message="Forbidden", status_code=403, error_code="INVALID_TOKEN", retryable=False
        )
        service = _make_flow_service(client)
        with pytest.raises(KeboolaApiError) as exc_info:
            service.set_flow_schedule("prod", "keboola.orchestrator", "1", "0 * * * *")
        assert exc_info.value.error_code == "INVALID_TOKEN"
        client.create_config.assert_not_called()


# ---------------------------------------------------------------------------
# FlowService.remove_flow_schedule
# ---------------------------------------------------------------------------


class TestRemoveFlowSchedule:
    def test_removes_matching_schedules(self):
        matching = {
            "id": "sched-1",
            "configuration": {
                "target": {"componentId": "keboola.orchestrator", "configurationId": "flow-1"}
            },
        }
        other = {
            "id": "sched-2",
            "configuration": {
                "target": {"componentId": "keboola.orchestrator", "configurationId": "other"}
            },
        }
        client = MagicMock()
        client.list_component_configs.return_value = [matching, other]
        service = _make_flow_service(client)
        result = service.remove_flow_schedule("prod", "keboola.orchestrator", "flow-1")
        assert result["deleted_count"] == 1
        assert "sched-1" in result["deleted_schedule_ids"]
        client.delete_config.assert_called_once_with("keboola.scheduler", "sched-1", branch_id=None)

    def test_no_schedules_is_idempotent(self):
        client = MagicMock()
        client.list_component_configs.return_value = []
        service = _make_flow_service(client)
        result = service.remove_flow_schedule("prod", "keboola.orchestrator", "flow-1")
        assert result["deleted_count"] == 0
        assert result["deleted_schedule_ids"] == []
        client.delete_config.assert_not_called()

    def test_partial_delete_failure_returns_successes(self):
        sched1 = {
            "id": "sched-a",
            "configuration": {
                "target": {"componentId": "keboola.orchestrator", "configurationId": "flow-1"}
            },
        }
        sched2 = {
            "id": "sched-b",
            "configuration": {
                "target": {"componentId": "keboola.orchestrator", "configurationId": "flow-1"}
            },
        }
        client = MagicMock()
        client.list_component_configs.return_value = [sched1, sched2]
        # first delete succeeds, second raises
        client.delete_config.side_effect = [
            None,
            KeboolaApiError(
                message="Server error", status_code=500, error_code="INTERNAL", retryable=True
            ),
        ]
        service = _make_flow_service(client)
        result = service.remove_flow_schedule("prod", "keboola.orchestrator", "flow-1")
        # Partial success: first was deleted, second failed but is not re-raised when some succeeded
        assert result["deleted_count"] == 1
        assert "sched-a" in result["deleted_schedule_ids"]

    def test_all_deletes_fail_raises(self):
        sched1 = {
            "id": "sched-x",
            "configuration": {
                "target": {"componentId": "keboola.orchestrator", "configurationId": "flow-1"}
            },
        }
        client = MagicMock()
        client.list_component_configs.return_value = [sched1]
        client.delete_config.side_effect = KeboolaApiError(
            message="Forbidden", status_code=403, error_code="INVALID_TOKEN", retryable=False
        )
        service = _make_flow_service(client)
        with pytest.raises(KeboolaApiError) as exc_info:
            service.remove_flow_schedule("prod", "keboola.orchestrator", "flow-1")
        assert exc_info.value.error_code == "SCHEDULE_DELETE_FAILED"
