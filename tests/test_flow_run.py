"""Tests for partial flow runs -- `kbagent flow run` (issue #725).

Covers the three layers the feature crosses: the ``onlyFlowTaskIds`` body
field on the Queue client, the phase->task-id resolution in ``FlowService``,
and the ``flow run`` command wiring (including the ``--dry-run`` preview and
the mutually-exclusive selectors).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.flow_service import FlowService
from keboola_agent_cli.services.flow_validation import reachable_phases

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

# A small diamond graph so reachability is actually exercised:
#
#   p1 -> p2 -> p4 -> END
#     \-> p3 -/
#
# Every phase carries one task; p3's task is disabled.
_PHASES = [
    {"id": "p1", "name": "Extract", "next": [{"id": "a", "goto": "p2"}, {"id": "b", "goto": "p3"}]},
    {"id": "p2", "name": "Transform", "next": [{"id": "c", "goto": "p4"}]},
    {"id": "p3", "name": "Enrich", "next": [{"id": "d", "goto": "p4"}]},
    {"id": "p4", "name": "Load", "next": [{"id": "e", "goto": None}]},
]
_TASKS = [
    {"id": "t1", "name": "T1", "phase": "p1", "task": {"type": "job"}},
    {"id": "t2", "name": "T2", "phase": "p2", "task": {"type": "job"}},
    {"id": "t3", "name": "T3", "phase": "p3", "enabled": False, "task": {"type": "job"}},
    {"id": "t4", "name": "T4", "phase": "p4", "task": {"type": "job"}},
]


def _flow_service(phases=None, tasks=None) -> FlowService:
    """FlowService whose only live call is get_config_detail."""
    client = MagicMock()
    client.get_config_detail.return_value = {
        "id": "5",
        "name": "CF",
        "configuration": {
            "phases": _PHASES if phases is None else phases,
            "tasks": _TASKS if tasks is None else tasks,
        },
    }
    cs = MagicMock()
    config = MagicMock()
    config.projects = {
        "prod": MagicMock(
            stack_url="https://connection.keboola.com", token="tok", active_branch_id=None
        )
    }
    config.max_parallel_workers = 10
    cs.load.return_value = config
    cs.get_project.side_effect = lambda alias: config.projects.get(alias)
    return FlowService(
        config_store=cs,
        client_factory=lambda url, token: client,
        ai_client_factory=lambda url, token: MagicMock(),
        scheduler_client_factory=lambda url, token: MagicMock(),
    )


# ---------------------------------------------------------------------------
# reachable_phases (pure)
# ---------------------------------------------------------------------------


def test_reachable_phases_walks_forward_only():
    assert reachable_phases(_PHASES, "p2") == ["p2", "p4"]


def test_reachable_phases_covers_every_branch_of_a_fork():
    assert reachable_phases(_PHASES, "p1") == ["p1", "p2", "p3", "p4"]


def test_reachable_phases_returns_flow_order_not_visit_order():
    # BFS from p1 visits p2 before p3, but a graph whose list order differs
    # must still come back in list order.
    phases = [
        {"id": "z", "name": "Z", "next": [{"id": "n", "goto": None}]},
        {"id": "a", "name": "A", "next": [{"id": "n", "goto": "z"}]},
    ]
    assert reachable_phases(phases, "a") == ["z", "a"]


def test_reachable_phases_unknown_start_is_empty():
    assert reachable_phases(_PHASES, "nope") == []


def test_reachable_phases_tolerates_a_cycle():
    phases = [
        {"id": "p1", "next": [{"id": "n", "goto": "p2"}]},
        {"id": "p2", "next": [{"id": "n", "goto": "p1"}]},
    ]
    assert reachable_phases(phases, "p1") == ["p1", "p2"]


# ---------------------------------------------------------------------------
# FlowService.resolve_flow_task_ids
# ---------------------------------------------------------------------------


def test_resolve_from_phase_takes_downstream_tasks():
    result = _flow_service().resolve_flow_task_ids(alias="prod", config_id="5", from_phase="p2")
    assert result["task_ids"] == ["t2", "t4"]
    assert [p["id"] for p in result["selected_phases"]] == ["p2", "p4"]
    assert result["from_phase"] == "p2"


def test_resolve_from_phase_skips_disabled_tasks_silently():
    result = _flow_service().resolve_flow_task_ids(alias="prod", config_id="5", from_phase="p1")
    # t3 is disabled: excluded from the selection, reported separately.
    assert result["task_ids"] == ["t1", "t2", "t4"]
    assert result["skipped_disabled_task_ids"] == ["t3"]


def test_resolve_always_reports_conditions_are_not_evaluated():
    # The single most important field in the payload: a caller must never read
    # a selected run as a rehearsal of the flow's condition logic.
    result = _flow_service().resolve_flow_task_ids(alias="prod", config_id="5", from_phase="p1")
    assert result["conditions_evaluated"] is False


def test_resolve_unknown_phase_lists_the_valid_ones():
    with pytest.raises(KeboolaApiError) as exc:
        _flow_service().resolve_flow_task_ids(alias="prod", config_id="5", from_phase="nope")
    assert exc.value.error_code == ErrorCode.INVALID_ARGUMENT
    assert "p1, p2, p3, p4" in exc.value.message


def test_resolve_unreachable_phase_is_rejected_before_the_api_sees_it():
    phases = [
        {"id": "p1", "name": "Entry", "next": [{"id": "n", "goto": None}]},
        {"id": "orphan", "name": "Orphan", "next": []},
    ]
    tasks = [{"id": "t9", "name": "T9", "phase": "orphan", "task": {"type": "job"}}]
    with pytest.raises(KeboolaApiError) as exc:
        _flow_service(phases, tasks).resolve_flow_task_ids(
            alias="prod", config_id="5", from_phase="orphan"
        )
    assert exc.value.error_code == ErrorCode.INVALID_ARGUMENT
    assert "not reachable" in exc.value.message


def test_resolve_phase_whose_downstream_is_all_disabled_errors():
    phases = [{"id": "p1", "name": "P1", "next": [{"id": "n", "goto": None}]}]
    tasks = [{"id": "t1", "name": "T1", "phase": "p1", "enabled": False, "task": {"type": "job"}}]
    with pytest.raises(KeboolaApiError) as exc:
        _flow_service(phases, tasks).resolve_flow_task_ids(
            alias="prod", config_id="5", from_phase="p1"
        )
    assert exc.value.error_code == ErrorCode.INVALID_ARGUMENT
    assert "no runnable tasks" in exc.value.message


def test_resolve_only_task_ids_returns_them_in_flow_order():
    result = _flow_service().resolve_flow_task_ids(
        alias="prod", config_id="5", only_task_ids=["t4", "t1"]
    )
    # The caller listed t4 first; the run order is the flow's, so t1 comes back
    # first rather than echoing the caller's ordering.
    assert result["task_ids"] == ["t1", "t4"]


def test_resolve_only_task_ids_deduplicates():
    result = _flow_service().resolve_flow_task_ids(
        alias="prod", config_id="5", only_task_ids=["t1", "t1"]
    )
    assert result["task_ids"] == ["t1"]


def test_resolve_only_task_ids_rejects_unknown_id():
    with pytest.raises(KeboolaApiError) as exc:
        _flow_service().resolve_flow_task_ids(
            alias="prod", config_id="5", only_task_ids=["t1", "ghost"]
        )
    assert exc.value.error_code == ErrorCode.INVALID_ARGUMENT
    assert "ghost" in exc.value.message


def test_resolve_only_task_ids_rejects_a_disabled_id():
    # Named explicitly => a hard error, unlike the --from-phase silent skip.
    with pytest.raises(KeboolaApiError) as exc:
        _flow_service().resolve_flow_task_ids(alias="prod", config_id="5", only_task_ids=["t3"])
    assert exc.value.error_code == ErrorCode.INVALID_ARGUMENT
    assert "disabled" in exc.value.message


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"from_phase": "p1", "only_task_ids": ["t1"]},
    ],
)
def test_resolve_requires_exactly_one_selector(kwargs):
    with pytest.raises(KeboolaApiError) as exc:
        _flow_service().resolve_flow_task_ids(alias="prod", config_id="5", **kwargs)
    assert exc.value.error_code == ErrorCode.INVALID_ARGUMENT


# ---------------------------------------------------------------------------
# Queue client: the onlyFlowTaskIds body field
# ---------------------------------------------------------------------------


def _created_job_body(**kwargs: Any) -> dict[str, Any]:
    """Return the JSON body ``create_job`` would POST for these arguments."""
    from keboola_agent_cli.client import KeboolaClient

    client = KeboolaClient(stack_url="https://connection.keboola.com", token="tok")
    with patch.object(client, "_queue_request") as queue_request:
        queue_request.return_value.json.return_value = {"id": "123"}
        client.create_job(component_id="keboola.flow", config_id="5", **kwargs)
    body: dict[str, Any] = queue_request.call_args.kwargs["json"]
    return body


def test_create_job_sends_only_flow_task_ids():
    body = _created_job_body(only_flow_task_ids=["t2", "t4"])
    assert body["onlyFlowTaskIds"] == ["t2", "t4"]
    # The deprecated alias must NOT ride along -- the API rejects both together.
    assert "onlyOrchestrationTaskIds" not in body


@pytest.mark.parametrize("value", [None, []])
def test_create_job_omits_the_field_when_unset(value):
    assert "onlyFlowTaskIds" not in _created_job_body(only_flow_task_ids=value)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def _setup_config(config_dir: Path) -> ConfigStore:
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="999-token-abc",
            project_name="prod",
            project_id=1234,
        ),
    )
    return store


def _invoke(store: ConfigStore, flow_service, job_service, args: list[str]):
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.FlowService") as MockFlow,
        patch("keboola_agent_cli.cli.JobService") as MockJob,
    ):
        MockStore.return_value = store
        MockFlow.return_value = flow_service
        MockJob.return_value = job_service
        return runner.invoke(app, ["--json", "flow", "run", *args])


def _base_args(*extra: str) -> list[str]:
    return ["--project", "prod", "--flow-id", "5", *extra]


def test_flow_run_rejects_both_selectors(tmp_path: Path):
    store = _setup_config(tmp_path / "cfg")
    result = _invoke(
        store,
        MagicMock(),
        MagicMock(),
        _base_args("--from-phase", "p2", "--only-task", "t1"),
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_flow_run_rejects_dry_run_without_a_selector(tmp_path: Path):
    store = _setup_config(tmp_path / "cfg")
    result = _invoke(store, MagicMock(), MagicMock(), _base_args("--dry-run"))
    assert result.exit_code == 2
    assert "--dry-run needs" in result.output


def test_flow_run_dry_run_previews_without_creating_a_job(tmp_path: Path):
    store = _setup_config(tmp_path / "cfg")
    job_service = MagicMock()
    result = _invoke(
        store, _flow_service(), job_service, _base_args("--from-phase", "p2", "--dry-run")
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert payload["dry_run"] is True
    assert payload["task_ids"] == ["t2", "t4"]
    job_service.run_job.assert_not_called()


def test_flow_run_passes_resolved_ids_to_the_job_service(tmp_path: Path):
    store = _setup_config(tmp_path / "cfg")
    job_service = MagicMock()
    job_service.run_job.return_value = {"id": "999", "status": "created"}
    result = _invoke(store, _flow_service(), job_service, _base_args("--from-phase", "p2"))
    assert result.exit_code == 0, result.output
    kwargs = job_service.run_job.call_args.kwargs
    assert kwargs["component_id"] == "keboola.flow"
    assert kwargs["only_flow_task_ids"] == ["t2", "t4"]
    # The selection rides along on the result so a caller keeping only the JSON
    # can still tell which tasks ran, with their names and phases.
    assert json.loads(result.output)["data"]["flow_task_selection"]["task_ids"] == ["t2", "t4"]


def test_flow_run_without_a_selector_is_a_plain_full_run(tmp_path: Path):
    store = _setup_config(tmp_path / "cfg")
    flow_service = MagicMock()
    job_service = MagicMock()
    job_service.run_job.return_value = {"id": "999", "status": "created"}
    result = _invoke(store, flow_service, job_service, _base_args())
    assert result.exit_code == 0, result.output
    assert job_service.run_job.call_args.kwargs["only_flow_task_ids"] is None
    # No resolution happened at all -- a full run must not fetch the flow body.
    flow_service.resolve_flow_task_ids.assert_not_called()
    assert "flow_task_selection" not in json.loads(result.output)["data"]


def test_flow_run_maps_a_service_error_to_its_exit_code(tmp_path: Path):
    store = _setup_config(tmp_path / "cfg")
    flow_service = MagicMock()
    flow_service.resolve_flow_task_ids.side_effect = KeboolaApiError(
        message="Unknown phase id 'nope'.",
        status_code=0,
        error_code=ErrorCode.INVALID_ARGUMENT,
        retryable=False,
    )
    result = _invoke(store, flow_service, MagicMock(), _base_args("--from-phase", "nope"))
    assert result.exit_code == 2
    assert json.loads(result.output)["error"]["code"] == "INVALID_ARGUMENT"


def test_flow_run_is_a_write_operation():
    from keboola_agent_cli.permissions import OPERATION_REGISTRY

    assert OPERATION_REGISTRY["flow.run"] == "write"
