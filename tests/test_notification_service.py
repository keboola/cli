"""Unit tests for NotificationService (issue #600).

Tests the business logic in isolation using mocked KeboolaClient instances.
Payload shapes below are taken verbatim from the notification service's own
OpenAPI examples -- kebab-case event names, `job.component.id` /
`job.configuration.id` / `branch.id` filter fields, and the two recipient
shapes (email carries `address`, webhook carries `url`).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.errors import ConfigError, ErrorCode, KeboolaApiError
from keboola_agent_cli.services.notification_service import NotificationService

_TOKEN_A = "901-storage-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_TOKEN_B = "901-storage-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

FLOW_COMPONENT = "keboola.flow"

# One subscription per shape the service can answer with.
SUB_FLOW_FAILED: dict[str, Any] = {
    "id": "101",
    "event": "job-failed",
    "filters": [
        {"field": "job.component.id", "value": FLOW_COMPONENT},
        {"field": "job.configuration.id", "value": "9001"},
    ],
    "recipient": {"channel": "email", "address": "ops@example.com"},
}
SUB_PROJECT_WIDE: dict[str, Any] = {
    "id": "102",
    "event": "job-failed",
    "recipient": {"channel": "email", "address": "catchall@example.com"},
}
SUB_WEBHOOK_LONG: dict[str, Any] = {
    "id": "103",
    "event": "job-processing-long",
    "filters": [
        {"field": "job.component.id", "value": FLOW_COMPONENT},
        {"field": "job.configuration.id", "value": "9002"},
        {"field": "durationOvertimePercentage", "operator": ">=", "value": 0.75},
    ],
    "recipient": {"channel": "webhook", "url": "https://hooks.example.com/kbc"},
    "expiresAt": "2026-01-07T14:00:00+01:00",
}
SUB_BRANCHED: dict[str, Any] = {
    "id": "104",
    "event": "job-failed",
    "filters": [
        {"field": "branch.id", "value": "1234"},
        {"field": "job.component.id", "value": FLOW_COMPONENT},
        {"field": "job.configuration.id", "value": "9001"},
    ],
    "recipient": {"channel": "email", "address": "dev@example.com"},
}

FLOW_CONFIGS = [
    {"id": "9001", "name": "Daily ingest"},
    {"id": "9002", "name": "Nightly rebuild"},
]


def _mock_config_store(projects: dict) -> MagicMock:
    cs = MagicMock()
    config = MagicMock()
    config.projects = {
        alias: MagicMock(
            stack_url=v["url"],
            token=v["token"],
            active_branch_id=v.get("active_branch_id"),
            project_id=v.get("project_id"),
        )
        for alias, v in projects.items()
    }
    config.max_parallel_workers = 10
    cs.load.return_value = config
    cs.get_project.side_effect = lambda alias: config.projects.get(alias)
    return cs


def _make_service(client_or_map: Any, projects: dict | None = None) -> NotificationService:
    if projects is None:
        projects = {
            "prod": {"url": "https://connection.keboola.com", "token": _TOKEN_A},
        }
    cs = _mock_config_store(projects)
    if isinstance(client_or_map, dict):

        def factory(url: str, token: str) -> MagicMock:
            return client_or_map[token]
    else:

        def factory(url: str, token: str) -> MagicMock:
            return client_or_map

    return NotificationService(config_store=cs, client_factory=factory)


def _client(subscriptions: list[dict[str, Any]], configs: Any = None) -> MagicMock:
    client = MagicMock()
    client.list_project_subscriptions.return_value = subscriptions
    client.list_component_configs.return_value = FLOW_CONFIGS if configs is None else configs
    return client


class TestShaping:
    def test_config_scoped_row_is_fully_resolved(self) -> None:
        service = _make_service(_client([SUB_FLOW_FAILED]))
        result = service.list_subscriptions(aliases=["prod"])

        assert result["errors"] == []
        (row,) = result["subscriptions"]
        assert row["project_alias"] == "prod"
        assert row["subscription_id"] == "101"
        assert row["event"] == "job-failed"
        assert row["scope"] == "config"
        assert row["component_id"] == FLOW_COMPONENT
        assert row["config_id"] == "9001"
        assert row["config_name"] == "Daily ingest"
        assert row["channel"] == "email"
        assert row["address"] == "ops@example.com"
        assert row["branch_id"] == ""
        assert row["expires_at"] == ""

    def test_subscription_without_filters_is_project_wide(self) -> None:
        """The catch-all 'page me on any failure' must not read as a broken flow row."""
        client = _client([SUB_PROJECT_WIDE])
        service = _make_service(client)
        result = service.list_subscriptions(aliases=["prod"])

        (row,) = result["subscriptions"]
        assert row["scope"] == "project-wide"
        assert row["config_id"] == ""
        assert row["config_name"] == ""
        assert row["address"] == "catchall@example.com"
        # No config to name -> no reason to pay for a config listing at all.
        client.list_component_configs.assert_not_called()

    def test_webhook_recipient_uses_url_field(self) -> None:
        """email carries `address`, webhook carries `url` -- both are the recipient."""
        service = _make_service(_client([SUB_WEBHOOK_LONG]))
        (row,) = service.list_subscriptions(aliases=["prod"])["subscriptions"]

        assert row["channel"] == "webhook"
        assert row["address"] == "https://hooks.example.com/kbc"
        assert row["expires_at"] == "2026-01-07T14:00:00+01:00"

    def test_threshold_filter_survives_verbatim(self) -> None:
        """A `>=` filter loses its meaning if collapsed to a scalar -- keep it raw."""
        service = _make_service(_client([SUB_WEBHOOK_LONG]))
        (row,) = service.list_subscriptions(aliases=["prod"])["subscriptions"]

        assert {
            "field": "durationOvertimePercentage",
            "operator": ">=",
            "value": 0.75,
        } in row["filters"]

    def test_branch_filter_is_surfaced(self) -> None:
        service = _make_service(_client([SUB_BRANCHED]))
        (row,) = service.list_subscriptions(aliases=["prod"])["subscriptions"]

        assert row["branch_id"] == "1234"

    def test_dangling_subscription_keeps_empty_name(self) -> None:
        """A subscription pointing at a deleted flow is a finding, not an error."""
        service = _make_service(_client([SUB_FLOW_FAILED], configs=[]))
        (row,) = service.list_subscriptions(aliases=["prod"])["subscriptions"]

        assert row["config_id"] == "9001"
        assert row["config_name"] == ""


class TestFilters:
    def test_event_is_forwarded_to_the_api(self) -> None:
        client = _client([SUB_FLOW_FAILED])
        service = _make_service(client)
        service.list_subscriptions(aliases=["prod"], event="job-failed")

        client.list_project_subscriptions.assert_called_once_with(event="job-failed")

    def test_config_id_filters_client_side(self) -> None:
        service = _make_service(_client([SUB_FLOW_FAILED, SUB_WEBHOOK_LONG]))
        result = service.list_subscriptions(aliases=["prod"], config_id="9002")

        assert [r["subscription_id"] for r in result["subscriptions"]] == ["103"]

    def test_component_id_filters_client_side(self) -> None:
        service = _make_service(_client([SUB_FLOW_FAILED, SUB_PROJECT_WIDE]))
        result = service.list_subscriptions(aliases=["prod"], component_id="keboola.orchestrator")

        assert result["subscriptions"] == []

    def test_branch_filter_keeps_only_that_branch(self) -> None:
        service = _make_service(_client([SUB_FLOW_FAILED, SUB_BRANCHED]))
        result = service.list_subscriptions(aliases=["prod"], branch_id=1234)

        assert [r["subscription_id"] for r in result["subscriptions"]] == ["104"]

    def test_active_branch_never_narrows_the_audit(self) -> None:
        """Production recipients must stay visible on a project with an active branch.

        Branch is a filter field here, not a scope -- inheriting the project's
        active branch would silently hide exactly what the audit looks for.
        """
        projects = {
            "prod": {
                "url": "https://connection.keboola.com",
                "token": _TOKEN_A,
                "active_branch_id": 1234,
            }
        }
        service = _make_service(_client([SUB_FLOW_FAILED, SUB_BRANCHED]), projects)
        result = service.list_subscriptions(aliases=["prod"])

        assert {r["subscription_id"] for r in result["subscriptions"]} == {"101", "104"}


class TestFanOut:
    def test_per_project_error_does_not_abort_the_run(self) -> None:
        good = _client([SUB_FLOW_FAILED])
        bad = MagicMock()
        bad.list_project_subscriptions.side_effect = KeboolaApiError(
            message="Invalid or expired token",
            status_code=401,
            error_code=ErrorCode.INVALID_TOKEN,
        )
        projects = {
            "prod": {"url": "https://connection.keboola.com", "token": _TOKEN_A},
            "dev": {"url": "https://connection.keboola.com", "token": _TOKEN_B},
        }
        service = _make_service({_TOKEN_A: good, _TOKEN_B: bad}, projects)
        result = service.list_subscriptions()

        assert [r["project_alias"] for r in result["subscriptions"]] == ["prod"]
        assert len(result["errors"]) == 1
        assert result["errors"][0]["project_alias"] == "dev"
        assert result["errors"][0]["error_code"] == ErrorCode.INVALID_TOKEN

    def test_clients_are_always_closed(self) -> None:
        client = _client([SUB_FLOW_FAILED])
        service = _make_service(client)
        service.list_subscriptions(aliases=["prod"])

        client.close.assert_called_once()

    def test_unknown_alias_raises_config_error(self) -> None:
        service = _make_service(_client([]))
        with pytest.raises(ConfigError):
            service.list_subscriptions(aliases=["nope"])

    def test_config_names_cost_one_call_per_branch_and_component(self) -> None:
        """Name resolution is O(branch x component), never N+1 over subscriptions."""
        client = _client([SUB_FLOW_FAILED, SUB_WEBHOOK_LONG, SUB_BRANCHED])
        service = _make_service(client)
        service.list_subscriptions(aliases=["prod"])

        # Two production flows share one call; the dev-branch one needs its own,
        # because a branch config is invisible from production.
        assert client.list_component_configs.call_count == 2
        assert {
            call.kwargs["branch_id"] for call in client.list_component_configs.call_args_list
        } == {None, 1234}
        # The heavy whole-project fetch is what this deliberately avoids.
        client.list_components_with_configs.assert_not_called()

    def test_branch_config_name_is_looked_up_in_that_branch(self) -> None:
        """A dev-branch subscription must not be reported as pointing at a deleted flow."""
        client = MagicMock()
        client.list_project_subscriptions.return_value = [SUB_BRANCHED]
        client.list_component_configs.side_effect = lambda component, branch_id=None: (
            [{"id": "9001", "name": "Daily ingest (branch)"}] if branch_id == 1234 else []
        )
        service = _make_service(client)
        (row,) = service.list_subscriptions(aliases=["prod"])["subscriptions"]

        assert row["config_name"] == "Daily ingest (branch)"
