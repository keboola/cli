"""Unit tests for the task selection of `kbagent flow run` (#725, design D2-D7, D12).

The selection is a pure function over a parsed keboola.flow configuration, so
every rule (default-transition traversal, malformed flows, disabled tasks,
conditional phases, de-duplication, task results) is tested here without an
HTTP client.
"""

from __future__ import annotations

from typing import Any

import pytest

from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.services.flow_run_selection import (
    CONDITIONAL_PHASES_SKIPPED,
    PARTIAL_RUN_IGNORES_CONDITIONS,
    SELECTED_TASK_FAILED,
    check_selector_values,
    select_flow_tasks,
    selection_warnings,
    summarize_task_results,
    task_failure_warning,
)

COND = {"type": "operator", "operator": "ALL_TASKS_IN_PHASE", "phase": "x", "operands": []}


def _phase(pid: str, *next_: dict[str, Any]) -> dict[str, Any]:
    return {"id": pid, "name": f"Phase {pid}", "next": list(next_)}


def _default(goto: str | None) -> dict[str, Any]:
    return {"id": f"to-{goto}", "goto": goto}


def _cond(goto: str | None, condition: Any = COND) -> dict[str, Any]:
    return {"id": f"if-{goto}", "goto": goto, "condition": condition}


def _task(tid: str, phase: str, enabled: bool = True, kind: str = "job") -> dict[str, Any]:
    return {
        "id": tid,
        "name": f"Task {tid}",
        "phase": phase,
        "enabled": enabled,
        "task": {"type": kind},
    }


def _linear() -> dict[str, Any]:
    """p1 -> p2 -> p3, plus pfail reached from p2 only through a conditional transition."""
    return {
        "phases": [
            _phase("p1", _default("p2")),
            _phase("p2", _cond("pfail"), _default("p3")),
            _phase("p3", _default(None)),
            _phase("pfail"),
        ],
        "tasks": [
            _task("t1", "p1"),
            _task("t2", "p2"),
            _task("t3", "p3"),
            _task("tf", "pfail"),
        ],
    }


def _invalid(exc: pytest.ExceptionInfo[KeboolaApiError]) -> str:
    assert exc.value.error_code == ErrorCode.INVALID_ARGUMENT
    return exc.value.message


class TestSelectorPresence:
    def test_no_selector_is_whole_flow_with_potential_tasks(self) -> None:
        config = _linear()
        config["tasks"].append(_task("td", "p3", enabled=False))
        sel = select_flow_tasks(config)
        assert sel.partial is False
        assert sel.task_ids == ["t1", "t2", "t3", "tf"]
        assert sel.skipped_disabled_task_ids == ["td"]
        assert selection_warnings(sel) == []

    @pytest.mark.parametrize("task_ids", [[], [" "], ["t1", ""]])
    def test_empty_or_blank_task_ids_refused(self, task_ids: list[str]) -> None:
        with pytest.raises(KeboolaApiError) as exc:
            select_flow_tasks(_linear(), task_ids=task_ids)
        _invalid(exc)

    def test_blank_from_phase_refused(self) -> None:
        with pytest.raises(KeboolaApiError) as exc:
            check_selector_values(None, "  ")
        _invalid(exc)

    def test_none_is_no_selector(self) -> None:
        check_selector_values(None, None)


class TestDefaultPath:
    def test_from_phase_follows_default_transitions(self) -> None:
        sel = select_flow_tasks(_linear(), from_phase_id="p2")
        assert sel.partial is True
        assert sel.task_ids == ["t2", "t3"]
        assert [p["id"] for p in sel.phases] == ["p2", "p3"]
        assert sel.conditional_phase_ids == ["pfail"]

    def test_missing_next_stops(self) -> None:
        config = {
            "phases": [{"id": "a"}, {"id": "b"}],
            "tasks": [_task("ta", "a"), _task("tb", "b")],
        }
        assert select_flow_tasks(config, from_phase_id="a").task_ids == ["ta"]

    def test_default_not_last_is_followed(self) -> None:
        config = {
            "phases": [_phase("a", _default("b"), _cond("c")), _phase("b"), _phase("c")],
            "tasks": [_task("ta", "a"), _task("tb", "b"), _task("tc", "c")],
        }
        sel = select_flow_tasks(config, from_phase_id="a")
        assert sel.task_ids == ["ta", "tb"]
        assert sel.conditional_phase_ids == ["c"]

    def test_only_conditional_transitions_stop_and_report(self) -> None:
        config = {
            "phases": [_phase("a", _cond("b")), _phase("b")],
            "tasks": [_task("ta", "a"), _task("tb", "b")],
        }
        sel = select_flow_tasks(config, from_phase_id="a")
        assert sel.task_ids == ["ta"]
        assert sel.conditional_phase_ids == ["b"]

    @pytest.mark.parametrize("condition", [None, {}])
    def test_null_or_empty_condition_is_not_a_default(self, condition: Any) -> None:
        config = {
            "phases": [_phase("a", _cond("b", condition)), _phase("b")],
            "tasks": [_task("ta", "a"), _task("tb", "b")],
        }
        sel = select_flow_tasks(config, from_phase_id="a")
        assert sel.task_ids == ["ta"]
        assert sel.conditional_phase_ids == ["b"]

    def test_multiple_defaults_refused(self) -> None:
        config = {
            "phases": [_phase("a", _default("b"), _default("c")), _phase("b"), _phase("c")],
            "tasks": [_task("ta", "a")],
        }
        with pytest.raises(KeboolaApiError) as exc:
            select_flow_tasks(config, from_phase_id="a")
        assert "phase 'a'" in _invalid(exc)

    def test_dangling_default_refused(self) -> None:
        config = {"phases": [_phase("a", _default("ghost"))], "tasks": [_task("ta", "a")]}
        with pytest.raises(KeboolaApiError) as exc:
            select_flow_tasks(config, from_phase_id="a")
        assert "ghost" in _invalid(exc)

    def test_cycle_stops_and_is_noted(self) -> None:
        config = {
            "phases": [_phase("a", _default("b")), _phase("b", _default("a"))],
            "tasks": [_task("ta", "a"), _task("tb", "b")],
        }
        sel = select_flow_tasks(config, from_phase_id="b")
        assert sel.task_ids == ["ta", "tb"]
        assert sel.default_path_cycle is True
        assert sel.to_dict()["default_path_cycle"] is True

    def test_unknown_phase_refused(self) -> None:
        with pytest.raises(KeboolaApiError) as exc:
            select_flow_tasks(_linear(), from_phase_id="p9")
        assert "p9" in _invalid(exc)


class TestConditionalWarning:
    def test_branch_rejoining_default_path_is_not_reported(self) -> None:
        config = {
            "phases": [
                _phase("a", _cond("fix"), _default("b")),
                _phase("fix", _default("b")),
                _phase("b"),
            ],
            "tasks": [_task("ta", "a"), _task("tfix", "fix"), _task("tb", "b")],
        }
        sel = select_flow_tasks(config, from_phase_id="a")
        # `fix` is skipped, `b` (the rejoin point) is on the default path.
        assert sel.conditional_phase_ids == ["fix"]

    def test_explicitly_added_task_removes_phase_from_warning(self) -> None:
        sel = select_flow_tasks(_linear(), task_ids=["tf"], from_phase_id="p2")
        assert sel.task_ids == ["t2", "t3", "tf"]
        assert sel.conditional_phase_ids == []
        assert [w.code for w in selection_warnings(sel)] == [PARTIAL_RUN_IGNORES_CONDITIONS]

    def test_deeper_conditional_branch_reported_cycle_safe(self) -> None:
        config = {
            "phases": [
                _phase("a", _cond("x"), _default(None)),
                _phase("x", _default("y")),
                _phase("y", _cond("x"), _default("x")),
            ],
            "tasks": [_task("ta", "a"), _task("tx", "x"), _task("ty", "y")],
        }
        sel = select_flow_tasks(config, from_phase_id="a")
        assert sel.conditional_phase_ids == ["x", "y"]
        warning = next(w for w in selection_warnings(sel) if w.code == CONDITIONAL_PHASES_SKIPPED)
        assert warning.extra == {"phase_ids": ["x", "y"]}
        assert "--task-id" in warning.message

    def test_task_ids_only_never_warns_about_conditional_phases(self) -> None:
        sel = select_flow_tasks(_linear(), task_ids=["t3"])
        assert [w.code for w in selection_warnings(sel)] == [PARTIAL_RUN_IGNORES_CONDITIONS]


class TestTaskValidation:
    def test_unknown_ids_all_listed(self) -> None:
        with pytest.raises(KeboolaApiError) as exc:
            select_flow_tasks(_linear(), task_ids=["nope", "t1", "gone"], from_phase_id="p9")
        message = _invalid(exc)
        assert "nope" in message and "gone" in message and "p9" in message

    def test_explicit_disabled_task_refused(self) -> None:
        config = _linear()
        config["tasks"][2]["enabled"] = False
        with pytest.raises(KeboolaApiError) as exc:
            select_flow_tasks(config, task_ids=["t3"])
        assert "t3" in _invalid(exc)

    def test_disabled_task_on_expanded_phase_skipped_and_listed(self) -> None:
        config = _linear()
        config["tasks"][2]["enabled"] = False
        sel = select_flow_tasks(config, from_phase_id="p2")
        assert sel.task_ids == ["t2"]
        assert sel.skipped_disabled_task_ids == ["t3"]

    def test_all_disabled_is_empty_selection(self) -> None:
        config = _linear()
        config["tasks"][2]["enabled"] = False
        with pytest.raises(KeboolaApiError) as exc:
            select_flow_tasks(config, from_phase_id="p3")
        assert "empty" in _invalid(exc)

    def test_union_deduplicated_in_config_order(self) -> None:
        sel = select_flow_tasks(_linear(), task_ids=["t3", "t1", "t3"], from_phase_id="p3")
        assert sel.task_ids == ["t1", "t3"]
        assert [t["id"] for t in sel.tasks] == ["t1", "t3"]
        assert sel.tasks[0] == {"id": "t1", "name": "Task t1", "phase_id": "p1", "type": "job"}


class TestTaskResults:
    def _sel(self) -> Any:
        return select_flow_tasks(_linear(), from_phase_id="p2")

    def test_all_success(self) -> None:
        job = {
            "result": {
                "tasks": [
                    {"id": "t2", "phase": "p2", "status": "success", "jobId": "11"},
                    {"id": "t3", "phase": "p3", "status": "warning", "jobId": "12"},
                ]
            }
        }
        results = summarize_task_results(job, self._sel())
        assert results.failed_task_ids == []
        assert results.task_results[0] == {
            "task_id": "t2",
            "phase_id": "p2",
            "status": "success",
            "job_id": "11",
        }

    def test_missing_and_failed_distinguished(self) -> None:
        job = {"result": {"tasks": [{"id": "t2", "phase": "p2", "status": "error", "jobId": "11"}]}}
        results = summarize_task_results(job, self._sel())
        assert results.failed_task_ids == ["t2", "t3"]
        assert results.missing_task_ids == ["t3"]
        assert results.task_results[1]["status"] == "missing"
        warning = task_failure_warning(results)
        assert warning.code == SELECTED_TASK_FAILED
        assert "failed: t2" in warning.message
        assert "did not run): t3" in warning.message

    def test_result_without_tasks_marks_every_task_missing(self) -> None:
        results = summarize_task_results({"result": "oops"}, self._sel())
        assert results.missing_task_ids == ["t2", "t3"]


class TestFixRound1:
    """Regression tests for review fix round 1 (F3, F4, F6, F7)."""

    @pytest.mark.parametrize(
        ("task_ids", "from_phase_id"), [(["t,1"], None), (["t1", "t2,t3"], None), (None, "p,1")]
    )
    def test_comma_in_selector_refused(
        self, task_ids: list[str] | None, from_phase_id: str | None
    ) -> None:
        with pytest.raises(KeboolaApiError) as exc:
            select_flow_tasks(_linear(), task_ids=task_ids, from_phase_id=from_phase_id)
        assert "comma" in _invalid(exc)

    def test_duplicate_task_ids_in_config_selected_once(self) -> None:
        config = _linear()
        config["tasks"].append(_task("t3", "p3"))
        assert select_flow_tasks(config, task_ids=["t3"]).task_ids == ["t3"]
        assert select_flow_tasks(config, from_phase_id="p3").task_ids == ["t3"]
        assert select_flow_tasks(config).task_ids == ["t1", "t2", "t3", "tf"]

    def test_phase_without_enabled_task_not_reported(self) -> None:
        config = {
            "phases": [
                _phase(
                    "a", _cond("disabled_only"), _cond("routing"), _cond("real"), _default(None)
                ),
                _phase("disabled_only"),
                _phase("routing"),
                _phase("real"),
            ],
            "tasks": [
                _task("ta", "a"),
                _task("td", "disabled_only", enabled=False),
                _task("tr", "real"),
            ],
        }
        sel = select_flow_tasks(config, from_phase_id="a")
        assert sel.conditional_phase_ids == ["real"]

    def test_wording_names_the_start_phase_for_an_upstream_retry_loop(self) -> None:
        # p2 retries back to p1 (the flow start) on a condition: p1 is upstream, not a
        # phase "reached only through a conditional transition" in the flow as a whole.
        config = {
            "phases": [
                _phase("p1", _default("p2")),
                _phase("p2", _cond("p1"), _default(None)),
            ],
            "tasks": [_task("t1", "p1"), _task("t2", "p2")],
        }
        sel = select_flow_tasks(config, from_phase_id="p2")
        assert sel.conditional_phase_ids == ["p1"]
        warning = next(w for w in selection_warnings(sel) if w.code == CONDITIONAL_PHASES_SKIPPED)
        assert "not on the default path from p2" in warning.message
        assert "reachable from it only through a conditional transition" in warning.message
        assert "reached only through" not in warning.message

    def test_selected_task_in_undefined_phase_refused(self) -> None:
        config = _linear()
        config["tasks"].append(_task("tx", "ghost-phase"))
        with pytest.raises(KeboolaApiError) as exc:
            select_flow_tasks(config, task_ids=["tx", "t1"])
        message = _invalid(exc)
        assert "tx" in message and "phase" in message
