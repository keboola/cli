"""Flow checks agree with the job queue on every surface (follow-ups of #725).

- ``job run --component-id keboola.flow`` refuses a flow the job queue refuses
  for every run, before the job is created (``flow run`` already did).
- ``flow validate`` (and ``flow new`` / ``update``) refuse a flow without
  exactly one first phase, and the unreachable-phase warning starts from that
  phase, not from the first one listed.
- Schema-less validation refuses ids, ``enabled`` and ``condition`` values of
  the wrong type instead of accepting them.
- ``flow run`` selects ids exactly: ``" t "`` is not ``t``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner, Result

from helpers import setup_single_project
from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.constants import ENV_AUTO_UPDATE
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.services.flow_run_selection import select_flow_tasks
from keboola_agent_cli.services.flow_validation import (
    find_unreachable_phases,
    validate_conditional_flow,
)
from keboola_agent_cli.services.job_service import JobService
from keboola_agent_cli.services.project_service import ProjectService

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_auto_update(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_AUTO_UPDATE, "false")


def _task(tid: str, phase: str) -> dict[str, Any]:
    return {"id": tid, "name": tid, "phase": phase, "enabled": True, "task": {"type": "job"}}


VALID = {
    "phases": [
        {"id": "p1", "name": "P1", "next": [{"id": "a", "goto": "p2"}]},
        {"id": "p2", "name": "P2", "next": [{"id": "end", "goto": None}]},
    ],
    "tasks": [_task("t1", "p1"), _task("t2", "p2")],
}
# p1 and island: two phases no transition goes to.
TWO_FIRST = {
    "phases": [*VALID["phases"], {"id": "island", "name": "I"}],
    "tasks": [*VALID["tasks"], _task("ti", "island")],
}


# ---------------------------------------------------------------------------
# job run --component-id keboola.flow
# ---------------------------------------------------------------------------


def _client(configuration: dict[str, Any]) -> MagicMock:
    client = MagicMock()
    client.get_config_detail.return_value = {"id": "01flow", "configuration": configuration}
    client.create_job.return_value = {"id": "900", "status": "created"}
    return client


def _job_run(store: ConfigStore, client: MagicMock, component_id: str = "keboola.flow") -> Result:
    args = ["--json", "job", "run", "--project", "prod", "--component-id", component_id]
    args += ["--config-id", "01flow"]
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as mock_store,
        patch("keboola_agent_cli.cli.ProjectService") as mock_projects,
        patch("keboola_agent_cli.cli.JobService") as mock_jobs,
    ):
        mock_store.return_value = store
        mock_projects.return_value = ProjectService(config_store=store)
        mock_jobs.return_value = JobService(
            config_store=store, client_factory=lambda url, token: client
        )
        return runner.invoke(app, args)


@pytest.mark.parametrize(
    ("configuration", "needle"),
    [
        (TWO_FIRST, "exactly one first phase"),
        (
            {"phases": VALID["phases"], "tasks": [*VALID["tasks"], _task("t1", "p2")]},
            "repeated task id(s): t1",
        ),
        (
            {
                "phases": [{"id": "p1", "next": [{"id": "x", "goto": "ghost"}]}],
                "tasks": [_task("t1", "p1")],
            },
            "ghost",
        ),
    ],
)
def test_job_run_of_a_broken_flow_creates_no_job(
    tmp_path: Path, configuration: dict[str, Any], needle: str
) -> None:
    client = _client(configuration)
    result = _job_run(setup_single_project(tmp_path / "cfg"), client)
    assert result.exit_code == 1, result.output
    error = json.loads(result.stdout)["error"]
    assert error["code"] == ErrorCode.INVALID_FLOW_DEFINITION
    assert needle in error["message"]
    client.create_job.assert_not_called()


def test_job_run_of_a_valid_flow_reads_it_once(tmp_path: Path) -> None:
    """The same read serves the check and the variables (no second GET)."""
    client = _client(VALID)
    result = _job_run(setup_single_project(tmp_path / "cfg"), client)
    assert result.exit_code == 0, result.output
    client.create_job.assert_called_once()
    assert client.get_config_detail.call_count == 1


def test_job_run_of_another_component_is_not_checked(tmp_path: Path) -> None:
    client = _client(TWO_FIRST)
    result = _job_run(setup_single_project(tmp_path / "cfg"), client, "keboola.ex-db")
    assert result.exit_code == 0, result.output
    client.create_job.assert_called_once()


def test_job_service_check_flow_false_skips_the_read(tmp_path: Path) -> None:
    """flow run passes check_flow=False: it checked its own read already."""
    client = _client(TWO_FIRST)
    service = JobService(
        config_store=setup_single_project(tmp_path / "cfg"),
        client_factory=lambda url, token: client,
    )
    service.run_job(
        alias="prod",
        component_id="keboola.flow",
        config_id="01flow",
        no_variables=True,
        check_flow=False,
    )
    client.get_config_detail.assert_not_called()
    client.create_job.assert_called_once()


# ---------------------------------------------------------------------------
# flow validate: first phase, unreachable phases
# ---------------------------------------------------------------------------


def test_two_first_phases_is_a_validation_error() -> None:
    errors = validate_conditional_flow(TWO_FIRST["phases"], TWO_FIRST["tasks"])
    assert any("exactly one first phase" in e and "p1, island" in e for e in errors)


def test_no_first_phase_is_a_validation_error() -> None:
    phases = [
        {"id": "a", "next": [{"id": "x", "goto": "b"}]},
        {"id": "b", "next": [{"id": "y", "goto": "a"}]},
    ]
    errors = validate_conditional_flow(phases, [_task("ta", "a"), _task("tb", "b")])
    assert any("found: none" in e for e in errors)


def test_valid_flow_is_still_valid() -> None:
    assert validate_conditional_flow(VALID["phases"], VALID["tasks"]) == []


def test_unreachable_starts_from_the_first_phase_not_the_first_listed() -> None:
    """b is listed first, but a is the phase no transition goes to: nothing is unreachable."""
    phases = [
        {"id": "b", "next": [{"id": "end", "goto": None}]},
        {"id": "a", "next": [{"id": "to-b", "goto": "b"}]},
    ]
    assert find_unreachable_phases(phases) == []
    assert validate_conditional_flow(phases, [_task("tb", "b"), _task("ta", "a")]) == []


def test_unreachable_is_still_reported_from_the_root() -> None:
    phases = [
        {"id": "r", "next": [{"id": "end", "goto": None}]},
        {"id": "x", "next": [{"id": "to-y", "goto": "y"}]},
        {"id": "y", "next": [{"id": "to-x", "goto": "x"}]},
    ]
    assert find_unreachable_phases(phases) == ["x", "y"]


# ---------------------------------------------------------------------------
# Schema-less value types
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("phases", "tasks", "needle"),
    [
        (
            [{"id": "p", "next": [{"goto": None, "condition": "oops"}]}],
            [_task("t", "p")],
            "'condition' must be an object",
        ),
        (
            [{"id": "p"}],
            [dict(_task("t", "p"), enabled="false")],
            "'enabled' must be true or false",
        ),
        ([{"id": ["p"]}], [_task("t", "p")], "'id' must be a string or a number"),
        (
            [{"id": "p"}],
            [dict(_task("t", "p"), phase=None)],
            "'phase' must be a string or a number",
        ),
        ([{"id": "p"}], [dict(_task("t", "p"), id=True)], "'id' must be a string or a number"),
    ],
)
def test_wrong_value_types_are_invalid(
    phases: list[dict[str, Any]], tasks: list[dict[str, Any]], needle: str
) -> None:
    assert any(needle in e for e in validate_conditional_flow(phases, tasks))


@pytest.mark.parametrize("value", ["p", 1, 1.0])
def test_numeric_and_string_ids_are_accepted(value: Any) -> None:
    phases = [{"id": value, "next": [{"id": "end", "goto": None}]}]
    assert validate_conditional_flow(phases, [dict(_task("t", "p"), phase=value)]) == []


def test_null_condition_is_still_accepted() -> None:
    phases = [{"id": "p", "next": [{"id": "end", "goto": None, "condition": None}]}]
    errors = validate_conditional_flow(phases, [_task("t", "p")])
    assert not any("must be an object" in e for e in errors)


# ---------------------------------------------------------------------------
# flow run: exact ids
# ---------------------------------------------------------------------------


def test_task_id_with_spaces_is_not_the_trimmed_id() -> None:
    with pytest.raises(KeboolaApiError) as exc:
        select_flow_tasks(VALID, task_ids=[" t1 "])
    assert exc.value.error_code == ErrorCode.INVALID_ARGUMENT
    assert "' t1 '" in exc.value.message


def test_task_id_with_spaces_selects_that_task_when_it_exists() -> None:
    configuration = {
        "phases": VALID["phases"],
        "tasks": [*VALID["tasks"], _task(" t1 ", "p2")],
    }
    assert select_flow_tasks(configuration, task_ids=[" t1 "]).task_ids == [" t1 "]


def test_from_phase_id_with_spaces_is_unknown() -> None:
    with pytest.raises(KeboolaApiError) as exc:
        select_flow_tasks(VALID, from_phase_id=" p2")
    assert "' p2'" in exc.value.message


def test_idempotency_replay_still_returns_the_earlier_job(tmp_path: Path) -> None:
    """The flow check runs only when a job is created, so a replay is not blocked."""
    client = _client(VALID)
    client.get_job_detail.return_value = {"id": "900", "status": "success", "isFinished": True}
    service = JobService(
        config_store=setup_single_project(tmp_path / "cfg"),
        client_factory=lambda url, token: client,
    )
    kwargs: dict[str, Any] = {
        "alias": "prod",
        "component_id": "keboola.flow",
        "config_id": "01flow",
        "no_variables": True,
        "idempotency_key": "k1",
    }
    first = service.run_job(**kwargs)
    client.get_config_detail.return_value = {"id": "01flow", "configuration": TWO_FIRST}
    second = service.run_job(**kwargs)
    assert first["id"] == second["id"] == "900"
    client.create_job.assert_called_once()


def test_float_ids_give_no_false_unreachable_warning() -> None:
    phases = [{"id": 1.0, "next": [{"id": "to-2", "goto": 2}]}, {"id": 2.0}]
    tasks = [dict(_task("a", "x"), phase=1), dict(_task("b", "x"), phase=2)]
    assert validate_conditional_flow(phases, tasks) == []
    assert find_unreachable_phases(phases) == []


def test_failed_flow_read_leaves_no_pending_idempotency_entry(tmp_path: Path) -> None:
    """A read error before the create is not an unknown create outcome."""
    client = _client(VALID)
    client.get_config_detail.side_effect = KeboolaApiError(
        message="Service unavailable", status_code=503, error_code="API_ERROR", retryable=True
    )
    service = JobService(
        config_store=setup_single_project(tmp_path / "cfg"),
        client_factory=lambda url, token: client,
    )
    kwargs: dict[str, Any] = {
        "alias": "prod",
        "component_id": "keboola.flow",
        "config_id": "01flow",
        "no_variables": True,
        "idempotency_key": "k2",
    }
    with pytest.raises(KeboolaApiError) as exc:
        service.run_job(**kwargs)
    assert exc.value.status_code == 503
    client.get_config_detail.side_effect = None
    client.get_config_detail.return_value = {"id": "01flow", "configuration": VALID}
    job = service.run_job(**kwargs)  # no "pending run" refusal
    assert job["id"] == "900"
    client.create_job.assert_called_once()


def test_broken_flow_with_a_key_leaves_no_pending_entry(tmp_path: Path) -> None:
    client = _client(TWO_FIRST)
    service = JobService(
        config_store=setup_single_project(tmp_path / "cfg"),
        client_factory=lambda url, token: client,
    )
    kwargs: dict[str, Any] = {
        "alias": "prod",
        "component_id": "keboola.flow",
        "config_id": "01flow",
        "no_variables": True,
        "idempotency_key": "k3",
    }
    with pytest.raises(KeboolaApiError) as exc:
        service.run_job(**kwargs)
    assert exc.value.error_code == ErrorCode.INVALID_FLOW_DEFINITION
    client.get_config_detail.return_value = {"id": "01flow", "configuration": VALID}
    assert service.run_job(**kwargs)["id"] == "900"
