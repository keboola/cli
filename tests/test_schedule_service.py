"""Unit tests for ScheduleService.

Tests the business logic in isolation using a mocked KeboolaClient.
Covers:

- ``list_schedules``: multi-project fan-out, enabled_only filter, error accumulation.
- ``get_schedule_detail``: parent name join, orphaned parent tolerance.
- ``find_schedules``: cron-window + not-run-since filters, AND semantics.
- Internal helpers ``parse_cron_window`` / ``cron_in_window`` / ``job_is_stale``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.services.schedule_service import (
    SCHEDULER_COMPONENT_ID,
    ScheduleService,
    _extract_schedule_fields,
    cron_in_window,
    job_is_stale,
    parse_cron_window,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _mock_config_store(projects: dict) -> MagicMock:
    cs = MagicMock()
    config = MagicMock()
    config.projects = {
        alias: MagicMock(
            stack_url=v["url"],
            token=v["token"],
            active_branch_id=None,
        )
        for alias, v in projects.items()
    }
    config.max_parallel_workers = 10
    cs.load.return_value = config
    cs.get_project.side_effect = lambda alias: config.projects.get(alias)
    return cs


def _make_service(mock_client: MagicMock, projects: dict | None = None) -> ScheduleService:
    if projects is None:
        projects = {"prod": {"url": "https://connection.keboola.com", "token": "tok"}}
    cs = _mock_config_store(projects)
    return ScheduleService(config_store=cs, client_factory=lambda url, tok: mock_client)


def _build_components_payload(
    schedules: list[dict] | None = None,
    parents: list[dict] | None = None,
    extra_components: list[dict] | None = None,
) -> list[dict]:
    """Build the shape returned by list_components_with_configs."""
    result: list[dict] = []

    if schedules is not None:
        result.append(
            {
                "id": SCHEDULER_COMPONENT_ID,
                "configurations": schedules,
            }
        )

    if parents:
        for parent_comp in parents:
            result.append(parent_comp)

    if extra_components:
        result.extend(extra_components)

    return result


def _scheduler_cfg(
    *,
    config_id: str,
    name: str,
    target_component: str,
    target_config_id: str,
    cron: str = "0 6 * * *",
    tz: str = "UTC",
    state: str = "enabled",
) -> dict:
    return {
        "id": config_id,
        "name": name,
        "configuration": {
            "schedule": {
                "cronTab": cron,
                "timezone": tz,
                "state": state,
            },
            "target": {
                "mode": "run",
                "componentId": target_component,
                "configurationId": target_config_id,
            },
        },
    }


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------


class TestExtractScheduleFields:
    def test_string_state_enabled(self) -> None:
        cfg = _scheduler_cfg(
            config_id="sc1",
            name="Schedule 1",
            target_component="keboola.orchestrator",
            target_config_id="111",
            state="enabled",
        )
        row = _extract_schedule_fields(cfg)
        assert row["enabled"] is True
        assert row["schedule_id"] == "sc1"
        assert row["cron"] == "0 6 * * *"

    def test_string_state_disabled(self) -> None:
        cfg = _scheduler_cfg(
            config_id="sc2",
            name="Schedule 2",
            target_component="keboola.flow",
            target_config_id="222",
            state="disabled",
        )
        assert _extract_schedule_fields(cfg)["enabled"] is False

    def test_dict_state_fallback(self) -> None:
        """Forward-compat: dict {state: {enabled: bool}} should still parse."""
        cfg = {
            "id": "sc3",
            "name": "Schedule",
            "configuration": {
                "schedule": {
                    "cronTab": "*/10 * * * *",
                    "timezone": "UTC",
                    "state": {"enabled": True},
                },
                "target": {
                    "componentId": "keboola.ex-http",
                    "configurationId": "999",
                },
            },
        }
        row = _extract_schedule_fields(cfg)
        assert row["enabled"] is True

    def test_json_string_configuration(self) -> None:
        cfg = {
            "id": "sc4",
            "name": "Schedule",
            "configuration": (
                '{"schedule": {"cronTab": "0 0 * * *", "timezone": "UTC", '
                '"state": "enabled"}, "target": {"componentId": "c", '
                '"configurationId": "2"}}'
            ),
        }
        row = _extract_schedule_fields(cfg)
        assert row["cron"] == "0 0 * * *"
        assert row["parent_config_id"] == "2"


class TestParseCronWindow:
    def test_valid_window(self) -> None:
        assert parse_cron_window("02:00-04:00") == (2, 4)

    def test_single_digit_hour(self) -> None:
        assert parse_cron_window("2:00-4:00") == (2, 4)

    def test_invalid_format(self) -> None:
        with pytest.raises(ValueError):
            parse_cron_window("02:00 to 04:00")

    def test_hour_out_of_range(self) -> None:
        with pytest.raises(ValueError):
            parse_cron_window("02:00-25:00")

    def test_end_before_start(self) -> None:
        with pytest.raises(ValueError):
            parse_cron_window("04:00-02:00")


class TestCronInWindow:
    def test_single_hour_in_window(self) -> None:
        assert cron_in_window("0 6 * * *", 5, 7) is True

    def test_single_hour_outside_window(self) -> None:
        assert cron_in_window("0 10 * * *", 5, 7) is False

    def test_star_hour_not_confined(self) -> None:
        """* covers every hour -> never confined to any bounded window."""
        assert cron_in_window("0 * * * *", 0, 23) is False

    def test_range_inside_window(self) -> None:
        assert cron_in_window("0 2-3 * * *", 1, 4) is True

    def test_range_partially_outside(self) -> None:
        assert cron_in_window("0 2-10 * * *", 0, 5) is False

    def test_comma_list(self) -> None:
        assert cron_in_window("0 2,3 * * *", 1, 4) is True
        assert cron_in_window("0 2,10 * * *", 1, 4) is False

    def test_step_notation(self) -> None:
        # */6 -> 0, 6, 12, 18 -- all within [0, 18]
        assert cron_in_window("0 */6 * * *", 0, 18) is True
        # */4 -> 0, 4, 8, 12, 16, 20 -- 20 is outside [0, 18]
        assert cron_in_window("0 */4 * * *", 0, 18) is False

    def test_empty_or_invalid(self) -> None:
        assert cron_in_window("", 0, 23) is False
        assert cron_in_window("not a cron", 0, 23) is False

    def test_fewer_than_five_fields(self) -> None:
        assert cron_in_window("0 6 * *", 0, 23) is False


class TestJobIsStale:
    def test_none_counts_as_stale(self) -> None:
        assert job_is_stale(None, 30) is True

    def test_unparseable_counts_as_stale(self) -> None:
        assert job_is_stale("not a timestamp", 30) is True

    def test_recent_job_not_stale(self) -> None:
        now = datetime.now(tz=UTC)
        recent = (now - timedelta(days=5)).isoformat()
        assert job_is_stale(recent, 30, now=now) is False

    def test_old_job_stale(self) -> None:
        now = datetime.now(tz=UTC)
        old = (now - timedelta(days=60)).isoformat()
        assert job_is_stale(old, 30, now=now) is True

    def test_z_suffix_parses(self) -> None:
        now = datetime.now(tz=UTC)
        old = (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert job_is_stale(old, 30, now=now) is True


# ---------------------------------------------------------------------------
# ScheduleService.list_schedules
# ---------------------------------------------------------------------------


class TestListSchedules:
    def test_single_project_happy_path(self) -> None:
        client = MagicMock()
        client.list_components_with_configs.return_value = _build_components_payload(
            schedules=[
                _scheduler_cfg(
                    config_id="sc1",
                    name="Schedule 1",
                    target_component="keboola.orchestrator",
                    target_config_id="111",
                )
            ],
            parents=[
                {
                    "id": "keboola.orchestrator",
                    "configurations": [{"id": "111", "name": "Daily ETL"}],
                }
            ],
        )
        service = _make_service(client)
        result = service.list_schedules(aliases=["prod"])

        assert result["errors"] == []
        assert len(result["schedules"]) == 1
        row = result["schedules"][0]
        assert row["schedule_id"] == "sc1"
        assert row["parent_name"] == "Daily ETL"
        assert row["enabled"] is True
        assert row["project_alias"] == "prod"

    def test_enabled_only_drops_disabled(self) -> None:
        client = MagicMock()
        client.list_components_with_configs.return_value = _build_components_payload(
            schedules=[
                _scheduler_cfg(
                    config_id="on1",
                    name="On",
                    target_component="keboola.orchestrator",
                    target_config_id="111",
                    state="enabled",
                ),
                _scheduler_cfg(
                    config_id="off1",
                    name="Off",
                    target_component="keboola.orchestrator",
                    target_config_id="222",
                    state="disabled",
                ),
            ]
        )
        service = _make_service(client)
        result = service.list_schedules(aliases=["prod"], enabled_only=True)
        ids = {s["schedule_id"] for s in result["schedules"]}
        assert ids == {"on1"}

    def test_api_error_captured_in_errors(self) -> None:
        client = MagicMock()
        client.list_components_with_configs.side_effect = KeboolaApiError(
            message="Auth fail", status_code=401, error_code="INVALID_TOKEN", retryable=False
        )
        service = _make_service(client)
        result = service.list_schedules(aliases=["prod"])
        assert result["schedules"] == []
        assert result["errors"]
        assert result["errors"][0]["error_code"] == "INVALID_TOKEN"

    def test_multi_project_merges_results(self) -> None:
        client = MagicMock()
        client.list_components_with_configs.return_value = _build_components_payload(
            schedules=[
                _scheduler_cfg(
                    config_id="sc1",
                    name="Schedule",
                    target_component="keboola.orchestrator",
                    target_config_id="111",
                )
            ]
        )
        service = _make_service(
            client,
            projects={
                "a": {"url": "https://k.com", "token": "t1"},
                "b": {"url": "https://k.com", "token": "t2"},
            },
        )
        result = service.list_schedules()
        projects_seen = {s["project_alias"] for s in result["schedules"]}
        assert projects_seen == {"a", "b"}

    def test_client_closed(self) -> None:
        client = MagicMock()
        client.list_components_with_configs.return_value = []
        service = _make_service(client)
        service.list_schedules(aliases=["prod"])
        client.close.assert_called()

    def test_no_schedulers_component_returns_empty(self) -> None:
        """When keboola.scheduler simply isn't in the components list."""
        client = MagicMock()
        client.list_components_with_configs.return_value = [
            {
                "id": "keboola.orchestrator",
                "configurations": [{"id": "111", "name": "Daily"}],
            }
        ]
        service = _make_service(client)
        result = service.list_schedules(aliases=["prod"])
        assert result["schedules"] == []
        assert result["errors"] == []


# ---------------------------------------------------------------------------
# ScheduleService.get_schedule_detail
# ---------------------------------------------------------------------------


class TestGetScheduleDetail:
    def test_happy_path(self) -> None:
        client = MagicMock()
        client.get_config_detail.side_effect = [
            {
                "id": "sc1",
                "name": "Schedule 1",
                "configuration": {
                    "schedule": {
                        "cronTab": "0 6 * * *",
                        "timezone": "UTC",
                        "state": "enabled",
                    },
                    "target": {
                        "componentId": "keboola.orchestrator",
                        "configurationId": "111",
                    },
                },
                "version": 1,
                "created": "2026-04-23T15:00:00+0000",
                "changeDescription": "Created",
            },
            {"id": "111", "name": "Daily ETL"},
        ]
        service = _make_service(client)
        result = service.get_schedule_detail("prod", "sc1")

        assert result["parent_name"] == "Daily ETL"
        assert result["schedule_id"] == "sc1"
        assert result["cron"] == "0 6 * * *"
        assert result["enabled"] is True

    def test_orphaned_parent_returns_empty_parent_name(self) -> None:
        """Parent config was deleted -- schedule detail must still succeed."""
        client = MagicMock()
        client.get_config_detail.side_effect = [
            {
                "id": "sc1",
                "name": "Orphaned",
                "configuration": {
                    "schedule": {
                        "cronTab": "0 0 * * *",
                        "timezone": "UTC",
                        "state": "enabled",
                    },
                    "target": {
                        "componentId": "keboola.flow",
                        "configurationId": "deleted",
                    },
                },
            },
            KeboolaApiError(
                message="Not found",
                status_code=404,
                error_code="NOT_FOUND",
                retryable=False,
            ),
        ]
        service = _make_service(client)
        result = service.get_schedule_detail("prod", "sc1")
        assert result["parent_name"] == ""
        assert result["parent_config_id"] == "deleted"

    def test_unknown_alias_raises_config_error(self) -> None:
        client = MagicMock()
        service = _make_service(client)
        with pytest.raises(ConfigError):
            service.get_schedule_detail("ghost", "sc1")

    def test_client_closed_on_error(self) -> None:
        client = MagicMock()
        client.get_config_detail.side_effect = KeboolaApiError(
            message="boom", status_code=500, error_code="UNKNOWN", retryable=True
        )
        service = _make_service(client)
        with pytest.raises(KeboolaApiError):
            service.get_schedule_detail("prod", "sc1")
        client.close.assert_called()


# ---------------------------------------------------------------------------
# ScheduleService.find_schedules
# ---------------------------------------------------------------------------


class TestFindSchedules:
    def _setup_client_with_schedules(self, schedules: list[dict]) -> MagicMock:
        client = MagicMock()
        client.list_components_with_configs.return_value = _build_components_payload(
            schedules=schedules,
            parents=[
                {
                    "id": "keboola.orchestrator",
                    "configurations": [{"id": "111", "name": "Daily"}],
                }
            ],
        )
        return client

    def test_no_filters_equivalent_to_list_plus_extras(self) -> None:
        client = self._setup_client_with_schedules(
            [
                _scheduler_cfg(
                    config_id="sc1",
                    name="Schedule",
                    target_component="keboola.orchestrator",
                    target_config_id="111",
                )
            ]
        )
        service = _make_service(client)
        result = service.find_schedules()
        assert len(result["schedules"]) == 1
        row = result["schedules"][0]
        assert "matches_cron_window" in row
        assert "last_run_at" in row
        assert result["filters"]["cron_window"] is None
        assert result["filters"]["not_run_since_days"] is None

    def test_cron_window_filter_in_window(self) -> None:
        client = self._setup_client_with_schedules(
            [
                _scheduler_cfg(
                    config_id="sc1",
                    name="Schedule",
                    target_component="keboola.orchestrator",
                    target_config_id="111",
                    cron="0 3 * * *",
                )
            ]
        )
        service = _make_service(client)
        result = service.find_schedules(cron_window="02:00-04:00")
        assert len(result["schedules"]) == 1

    def test_cron_window_filter_out_of_window(self) -> None:
        client = self._setup_client_with_schedules(
            [
                _scheduler_cfg(
                    config_id="sc1",
                    name="Schedule",
                    target_component="keboola.orchestrator",
                    target_config_id="111",
                    cron="0 12 * * *",
                )
            ]
        )
        service = _make_service(client)
        result = service.find_schedules(cron_window="02:00-04:00")
        assert result["schedules"] == []

    def test_not_run_since_filter_includes_never_run(self) -> None:
        client = self._setup_client_with_schedules(
            [
                _scheduler_cfg(
                    config_id="sc1",
                    name="Schedule",
                    target_component="keboola.orchestrator",
                    target_config_id="111",
                )
            ]
        )
        client.list_jobs.return_value = []  # parent has never run
        service = _make_service(client)
        result = service.find_schedules(not_run_since_days=30)
        assert len(result["schedules"]) == 1
        assert result["schedules"][0]["last_run_at"] is None

    def test_not_run_since_filter_excludes_recent(self) -> None:
        now = datetime.now(tz=UTC)
        recent = (now - timedelta(days=5)).isoformat()
        client = self._setup_client_with_schedules(
            [
                _scheduler_cfg(
                    config_id="sc1",
                    name="Schedule",
                    target_component="keboola.orchestrator",
                    target_config_id="111",
                )
            ]
        )
        client.list_jobs.return_value = [{"startTime": recent}]
        service = _make_service(client)
        result = service.find_schedules(not_run_since_days=30)
        assert result["schedules"] == []

    def test_combined_and_logic(self) -> None:
        # Schedule matches the cron window but parent ran recently -> excluded.
        now = datetime.now(tz=UTC)
        recent = (now - timedelta(days=1)).isoformat()
        client = self._setup_client_with_schedules(
            [
                _scheduler_cfg(
                    config_id="sc1",
                    name="Schedule",
                    target_component="keboola.orchestrator",
                    target_config_id="111",
                    cron="0 3 * * *",
                )
            ]
        )
        client.list_jobs.return_value = [{"startTime": recent}]
        service = _make_service(client)
        result = service.find_schedules(cron_window="02:00-04:00", not_run_since_days=30)
        assert result["schedules"] == []

    def test_invalid_cron_window_raises_config_error(self) -> None:
        service = _make_service(MagicMock())
        with pytest.raises(ConfigError):
            service.find_schedules(cron_window="bad input")

    def test_negative_days_raises_config_error(self) -> None:
        service = _make_service(MagicMock())
        with pytest.raises(ConfigError):
            service.find_schedules(not_run_since_days=-1)

    def test_list_jobs_failure_treated_as_never_ran(self) -> None:
        client = self._setup_client_with_schedules(
            [
                _scheduler_cfg(
                    config_id="sc1",
                    name="Schedule",
                    target_component="keboola.orchestrator",
                    target_config_id="111",
                )
            ]
        )
        client.list_jobs.side_effect = KeboolaApiError(
            message="boom", status_code=500, error_code="UNKNOWN", retryable=True
        )
        service = _make_service(client)
        result = service.find_schedules(not_run_since_days=30)
        assert len(result["schedules"]) == 1
        assert result["schedules"][0]["last_run_at"] is None


# ---------------------------------------------------------------------------
# Flow list --with-schedules enrichment (cross-service verification)
# ---------------------------------------------------------------------------


class TestFlowListWithSchedulesEnrichment:
    def test_schedules_joined_on_parent_id(self) -> None:
        """Exercises FlowService --with-schedules path via ScheduleService fixtures."""
        from keboola_agent_cli.services.flow_service import FlowService

        client = MagicMock()

        def side_effect(component_id: str, branch_id=None):
            if component_id == "keboola.orchestrator":
                return [
                    {
                        "id": "orchestrator-1",
                        "name": "Daily ETL",
                        "description": "",
                        "isDisabled": False,
                    }
                ]
            if component_id == "keboola.flow":
                return []
            if component_id == SCHEDULER_COMPONENT_ID:
                return [
                    _scheduler_cfg(
                        config_id="sc1",
                        name="Schedule",
                        target_component="keboola.orchestrator",
                        target_config_id="orchestrator-1",
                        state="enabled",
                    )
                ]
            raise AssertionError(f"unexpected component_id {component_id}")

        client.list_component_configs.side_effect = side_effect

        cs = _mock_config_store({"prod": {"url": "https://k.com", "token": "t"}})
        service = FlowService(config_store=cs, client_factory=lambda u, t: client)
        result = service.list_flows(aliases=["prod"], with_schedules=True)

        flow_row = result["flows"][0]
        assert "schedules" in flow_row
        assert len(flow_row["schedules"]) == 1
        assert flow_row["schedules"][0]["cron"] == "0 6 * * *"
        assert flow_row["schedules"][0]["enabled"] is True

    def test_without_flag_schedules_key_absent(self) -> None:
        from keboola_agent_cli.services.flow_service import FlowService

        client = MagicMock()

        def side_effect(component_id: str, branch_id=None):
            if component_id == "keboola.orchestrator":
                return [{"id": "o1", "name": "Flow", "description": "", "isDisabled": False}]
            return []

        client.list_component_configs.side_effect = side_effect

        cs = _mock_config_store({"prod": {"url": "https://k.com", "token": "t"}})
        service = FlowService(config_store=cs, client_factory=lambda u, t: client)
        result = service.list_flows(aliases=["prod"])
        assert "schedules" not in result["flows"][0]
