"""Unit tests for NotificationService -- issue #600 (read path), #690 (write path).

Tests the business logic in isolation using a mocked KeboolaClient. Covers:

- ``_extract_subscription_fields``: the wire-format shapes that are easy to get
  wrong (dotted filter paths, webhook ``url`` vs email ``address``, filter-less
  project-wide subscriptions).
- ``list_subscriptions``: multi-project fan-out, client-side component/config
  filters, config-name join (pair match + unique-config-id fallback), the
  project-wide exclusion counter, and error accumulation.
- ``get_subscription_detail``: name join and tolerance of a deleted parent.
- ``TestCreateSubscription``: recipient/filter construction, and the created
  audit row.
- ``TestDeleteSubscription``: the delete call and its envelope.
- ``TestReplaceSubscriptionRecipient``: create-before-delete ordering, verbatim
  carry-over of the old event/filters/expiresAt, and the non-raising delete
  failure path (any exception, not just ``KeboolaApiError``).
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.services._notification_filters import (
    normalize_filters,
    parse_filter_expression,
)
from keboola_agent_cli.services.notification_service import (
    NotificationService,
    _extract_subscription_fields,
)

# ---------------------------------------------------------------------------
# Fixtures
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


def _make_service(
    mock_client: MagicMock | dict[str, MagicMock],
    projects: dict | None = None,
) -> NotificationService:
    if projects is None:
        projects = {"prod": {"url": "https://connection.keboola.com", "token": "tok"}}
    cs = _mock_config_store(projects)

    if isinstance(mock_client, dict):
        # Per-project clients keyed by stack URL, so a fan-out test can give
        # each project its own payload (and its own failure).
        def factory(url: str, _tok: str) -> MagicMock:
            return mock_client[url]
    else:

        def factory(_url: str, _tok: str) -> MagicMock:
            return mock_client

    return NotificationService(config_store=cs, client_factory=factory)


def _components_payload(configs: list[tuple[str, str, str]]) -> list[dict]:
    """Build a list_components_with_configs payload from (comp, cfg_id, name)."""
    by_component: dict[str, list[dict]] = {}
    for comp_id, cfg_id, name in configs:
        by_component.setdefault(comp_id, []).append({"id": cfg_id, "name": name})
    return [
        {"id": comp_id, "configurations": cfgs} for comp_id, cfgs in sorted(by_component.items())
    ]


def _wire_configs(client: MagicMock, configs: list[tuple[str, str, str]]) -> None:
    """Make both config-listing calls answer consistently for the same fixture.

    The service picks between them: a component-scoped join uses the cheap
    per-component listing, a config-id-only join needs the whole-project one.
    """
    client.list_components_with_configs.return_value = _components_payload(configs)

    def per_component(component_id: str, branch_id: int | None = None) -> list[dict]:
        return [{"id": cfg, "name": name} for comp, cfg, name in configs if comp == component_id]

    client.list_component_configs.side_effect = per_component


FLOW_SUBSCRIPTION = {
    "id": "1234",
    "event": "job-failed",
    "filters": [
        {"field": "job.component.id", "value": "keboola.flow"},
        {"field": "job.configuration.id", "value": "98765"},
    ],
    "recipient": {"channel": "email", "address": "ops@example.com"},
}

CATCHALL_SUBSCRIPTION = {
    "id": "1235",
    "event": "job-failed",
    "recipient": {"channel": "email", "address": "catchall@example.com"},
}

WEBHOOK_SUBSCRIPTION = {
    "id": "1236",
    "event": "job-processing-long",
    "expiresAt": "2027-01-07T14:00:00+01:00",
    "filters": [
        {"field": "job.configuration.id", "value": "98765"},
        {"field": "durationOvertimePercentage", "operator": ">=", "value": 0.75},
    ],
    "recipient": {"channel": "webhook", "url": "https://hooks.example.com/kbc"},
}

COMPONENT_ONLY_SUBSCRIPTION = {
    "id": "1238",
    "event": "job-failed",
    "filters": [{"field": "job.component.id", "value": "keboola.flow"}],
    "recipient": {"channel": "email", "address": "flowops@example.com"},
}

OTHER_COMPONENT_SUBSCRIPTION = {
    "id": "1239",
    "event": "job-failed",
    "filters": [{"field": "job.component.id", "value": "keboola.ex-db-snowflake"}],
    "recipient": {"channel": "email", "address": "dba@example.com"},
}

BRANCHED_SUBSCRIPTION = {
    "id": "1237",
    "event": "phase-job-failed",
    "filters": [
        {"field": "branch.id", "value": "4242"},
        {"field": "job.component.id", "value": "keboola.flow"},
        {"field": "job.configuration.id", "value": "98765"},
        {"field": "phase.id", "value": "7"},
    ],
    "recipient": {"channel": "email", "address": "dev@example.com"},
}


# ---------------------------------------------------------------------------
# _extract_subscription_fields
# ---------------------------------------------------------------------------


class TestExtractSubscriptionFields:
    def test_flow_scoped_email_subscription(self) -> None:
        row = _extract_subscription_fields(FLOW_SUBSCRIPTION)

        assert row["subscription_id"] == "1234"
        assert row["event"] == "job-failed"
        assert row["component_id"] == "keboola.flow"
        assert row["config_id"] == "98765"
        assert row["channel"] == "email"
        assert row["address"] == "ops@example.com"
        assert row["scope"] == "config"
        assert row["branch_id"] == ""
        assert row["expires_at"] == ""

    def test_subscription_without_filters_is_project_wide(self) -> None:
        """Only ``event`` and ``recipient`` are required -- filters are optional."""
        row = _extract_subscription_fields(CATCHALL_SUBSCRIPTION)

        assert row["scope"] == "project-wide"
        assert row["component_id"] == ""
        assert row["config_id"] == ""
        assert row["address"] == "catchall@example.com"

    def test_webhook_recipient_address_comes_from_url(self) -> None:
        """RecipientChannel_Webhook carries ``url``; only email carries ``address``."""
        row = _extract_subscription_fields(WEBHOOK_SUBSCRIPTION)

        assert row["channel"] == "webhook"
        assert row["address"] == "https://hooks.example.com/kbc"
        assert row["expires_at"] == "2027-01-07T14:00:00+01:00"

    def test_config_filter_without_component_still_scopes_to_config(self) -> None:
        row = _extract_subscription_fields(WEBHOOK_SUBSCRIPTION)

        assert row["config_id"] == "98765"
        assert row["component_id"] == ""
        assert row["scope"] == "config"

    def test_branch_and_phase_filters_are_extracted(self) -> None:
        row = _extract_subscription_fields(BRANCHED_SUBSCRIPTION)

        assert row["branch_id"] == "4242"
        assert row["phase_id"] == "7"
        assert row["event"] == "phase-job-failed"

    def test_raw_filters_are_preserved(self) -> None:
        """Threshold filters have no dedicated column -- callers keep the raw list."""
        row = _extract_subscription_fields(WEBHOOK_SUBSCRIPTION)

        assert {"field": "durationOvertimePercentage", "operator": ">=", "value": 0.75} in row[
            "filters"
        ]

    def test_numeric_filter_values_are_stringified(self) -> None:
        """The API allows integer values; columns must not leak raw ints."""
        row = _extract_subscription_fields(
            {
                "id": 7,
                "event": "job-failed",
                "filters": [{"field": "job.configuration.id", "value": 98765}],
                "recipient": {"channel": "email", "address": "a@b.c"},
            }
        )

        assert row["subscription_id"] == "7"
        assert row["config_id"] == "98765"

    def test_unknown_recipient_channel_degrades_gracefully(self) -> None:
        row = _extract_subscription_fields(
            {"id": "9", "event": "job-failed", "recipient": {"channel": "carrier-pigeon"}}
        )

        assert row["channel"] == "carrier-pigeon"
        assert row["address"] == ""


# ---------------------------------------------------------------------------
# list_subscriptions
# ---------------------------------------------------------------------------


class TestListSubscriptions:
    def test_single_project_resolves_config_name_from_pair(self) -> None:
        client = MagicMock()
        client.list_project_subscriptions.return_value = [FLOW_SUBSCRIPTION]
        _wire_configs(client, [("keboola.flow", "98765", "Daily ETL")])

        result = _make_service(client).list_subscriptions()

        assert len(result["subscriptions"]) == 1
        assert result["subscriptions"][0]["config_name"] == "Daily ETL"
        assert result["subscriptions"][0]["project_alias"] == "prod"
        assert result["errors"] == []

    def test_config_name_falls_back_to_unique_config_id_match(self) -> None:
        """A subscription filtering only on config id still resolves a name."""
        client = MagicMock()
        client.list_project_subscriptions.return_value = [WEBHOOK_SUBSCRIPTION]
        _wire_configs(client, [("keboola.flow", "98765", "Daily ETL")])

        result = _make_service(client).list_subscriptions()

        assert result["subscriptions"][0]["config_name"] == "Daily ETL"

    def test_ambiguous_config_id_leaves_name_empty(self) -> None:
        """Two components sharing a config id must not be guessed between."""
        client = MagicMock()
        client.list_project_subscriptions.return_value = [WEBHOOK_SUBSCRIPTION]
        _wire_configs(
            client,
            [
                ("keboola.flow", "98765", "Daily ETL"),
                ("keboola.orchestrator", "98765", "Legacy ETL"),
            ],
        )

        result = _make_service(client).list_subscriptions()

        assert result["subscriptions"][0]["config_name"] == ""

    def test_deleted_parent_config_leaves_name_empty(self) -> None:
        client = MagicMock()
        client.list_project_subscriptions.return_value = [FLOW_SUBSCRIPTION]
        _wire_configs(client, [])

        result = _make_service(client).list_subscriptions()

        assert result["subscriptions"][0]["config_name"] == ""

    def test_component_scoped_join_uses_the_cheap_per_component_listing(self) -> None:
        """`list_components_with_configs` sends `include=configuration,rows`.

        That is the whole project's config bodies and rows, fetched purely to
        map an ID to a display name. When every config-scoped row names its
        component, one listing per component answers the same question.
        """
        client = MagicMock()
        client.list_project_subscriptions.return_value = [FLOW_SUBSCRIPTION]
        _wire_configs(client, [("keboola.flow", "98765", "Daily ETL")])

        result = _make_service(client).list_subscriptions()

        client.list_components_with_configs.assert_not_called()
        client.list_component_configs.assert_called_once_with("keboola.flow", branch_id=None)
        assert result["subscriptions"][0]["config_name"] == "Daily ETL"

    def test_component_less_row_falls_back_to_the_whole_project_listing(self) -> None:
        """Resolving a bare config ID needs every component's configs."""
        client = MagicMock()
        client.list_project_subscriptions.return_value = [WEBHOOK_SUBSCRIPTION]
        _wire_configs(client, [("keboola.flow", "98765", "Daily ETL")])

        result = _make_service(client).list_subscriptions()

        client.list_components_with_configs.assert_called_once_with(branch_id=None)
        assert result["subscriptions"][0]["config_name"] == "Daily ETL"

    def test_each_component_is_listed_once_for_many_subscriptions(self) -> None:
        client = MagicMock()
        client.list_project_subscriptions.return_value = [
            FLOW_SUBSCRIPTION,
            BRANCHED_SUBSCRIPTION,
        ]
        _wire_configs(client, [("keboola.flow", "98765", "Daily ETL")])

        _make_service(client).list_subscriptions()

        assert client.list_component_configs.call_count == 1

    def test_no_join_call_when_nothing_is_config_scoped(self) -> None:
        """The join payload is proportional to project size -- skip it when useless."""
        client = MagicMock()
        client.list_project_subscriptions.return_value = [CATCHALL_SUBSCRIPTION]

        result = _make_service(client).list_subscriptions()

        client.list_components_with_configs.assert_not_called()
        client.list_component_configs.assert_not_called()
        assert result["subscriptions"][0]["config_name"] == ""

    def test_event_filter_is_applied_client_side(self) -> None:
        """The live service IGNORES ``?event=`` and returns everything.

        Verified against a real stack: ``GET /project-subscriptions?event=
        job-failed`` answers 200 with every subscription in the project,
        including ``job-succeeded`` ones. The swagger documents the parameter,
        so it is still sent -- but the rows must be narrowed here too, or
        ``--event job-failed`` silently answers "who gets paged on failure"
        with a superset that includes success recipients.
        """
        client = MagicMock()
        # The mock ignores the kwarg exactly like the live service does.
        client.list_project_subscriptions.return_value = [
            FLOW_SUBSCRIPTION,
            {
                "id": "1240",
                "event": "job-succeeded",
                "filters": [{"field": "job.configuration.id", "value": "98765"}],
                "recipient": {"channel": "email", "address": "ok@example.com"},
            },
        ]
        _wire_configs(client, [])

        result = _make_service(client).list_subscriptions(event="job-failed")

        assert [s["event"] for s in result["subscriptions"]] == ["job-failed"]

    def test_event_filter_is_still_sent_to_the_api(self) -> None:
        """Keep sending it: harmless today, correct for free if it is fixed."""
        client = MagicMock()
        client.list_project_subscriptions.return_value = []

        _make_service(client).list_subscriptions(event="job-failed")

        client.list_project_subscriptions.assert_called_once_with(event="job-failed")

    def test_unknown_event_narrows_to_nothing(self) -> None:
        """A typo must not silently return the whole project."""
        client = MagicMock()
        client.list_project_subscriptions.return_value = [FLOW_SUBSCRIPTION]
        _wire_configs(client, [])

        result = _make_service(client).list_subscriptions(event="jobFailed")

        assert result["subscriptions"] == []

    def test_component_filter_is_applied_client_side(self) -> None:
        """The API has no component filter -- only ``?event=``."""
        client = MagicMock()
        client.list_project_subscriptions.return_value = [
            FLOW_SUBSCRIPTION,
            {
                "id": "999",
                "event": "job-failed",
                "filters": [{"field": "job.component.id", "value": "keboola.ex-db-snowflake"}],
                "recipient": {"channel": "email", "address": "dba@example.com"},
            },
        ]
        _wire_configs(client, [])

        result = _make_service(client).list_subscriptions(component_id="keboola.flow")

        assert [s["subscription_id"] for s in result["subscriptions"]] == ["1234"]
        # The dropped row names a different component, so it cannot fire here.
        assert result["project_wide_excluded"] == 0

    def test_config_filter_is_applied_client_side(self) -> None:
        client = MagicMock()
        client.list_project_subscriptions.return_value = [FLOW_SUBSCRIPTION, BRANCHED_SUBSCRIPTION]
        _wire_configs(client, [])

        result = _make_service(client).list_subscriptions(config_id="98765")

        assert len(result["subscriptions"]) == 2

    def test_project_wide_subscriptions_are_counted_when_filtered_out(self) -> None:
        """A catch-all also pages on this flow -- never drop it silently."""
        client = MagicMock()
        client.list_project_subscriptions.return_value = [
            FLOW_SUBSCRIPTION,
            CATCHALL_SUBSCRIPTION,
        ]
        _wire_configs(client, [])

        result = _make_service(client).list_subscriptions(config_id="98765")

        assert [s["subscription_id"] for s in result["subscriptions"]] == ["1234"]
        assert result["project_wide_excluded"] == 1

    def test_kept_rows_are_never_counted_as_excluded(self) -> None:
        """A row the filter KEPT must not also be reported as hidden.

        `scope` is derived from the config filter alone, so a subscription
        filtering only on `job.component.id` is labelled project-wide -- but
        `--component-id` keeps it, and warning that it was hidden would
        contradict the table the user is looking at.
        """
        client = MagicMock()
        client.list_project_subscriptions.return_value = [COMPONENT_ONLY_SUBSCRIPTION]
        _wire_configs(client, [])

        result = _make_service(client).list_subscriptions(component_id="keboola.flow")

        assert [s["subscription_id"] for s in result["subscriptions"]] == ["1238"]
        assert result["project_wide_excluded"] == 0

    def test_dropped_component_only_subscription_is_counted(self) -> None:
        """It has no config filter, so it fires for the audited config too."""
        client = MagicMock()
        client.list_project_subscriptions.return_value = [COMPONENT_ONLY_SUBSCRIPTION]
        _wire_configs(client, [])

        result = _make_service(client).list_subscriptions(config_id="98765")

        assert result["subscriptions"] == []
        assert result["project_wide_excluded"] == 1

    def test_subscription_naming_another_component_is_not_counted(self) -> None:
        """An explicit, non-matching component filter proves irrelevance.

        The counter exists to say "these also page for what you audited". A
        subscription scoped to keboola.ex-db-snowflake can never fire for a
        keboola.flow job, so counting it is noise in exactly the
        incident-response workflow this command is for.
        """
        client = MagicMock()
        client.list_project_subscriptions.return_value = [OTHER_COMPONENT_SUBSCRIPTION]
        _wire_configs(client, [])

        result = _make_service(client).list_subscriptions(component_id="keboola.flow")

        assert result["subscriptions"] == []
        assert result["project_wide_excluded"] == 0

    def test_no_exclusion_counter_without_a_scope_filter(self) -> None:
        client = MagicMock()
        client.list_project_subscriptions.return_value = [CATCHALL_SUBSCRIPTION]

        result = _make_service(client).list_subscriptions()

        assert result["project_wide_excluded"] == 0
        assert len(result["subscriptions"]) == 1

    def test_multi_project_fanout_merges_rows(self) -> None:
        prod, dev = MagicMock(), MagicMock()
        prod.list_project_subscriptions.return_value = [FLOW_SUBSCRIPTION]
        _wire_configs(prod, [("keboola.flow", "98765", "Prod ETL")])
        dev.list_project_subscriptions.return_value = [CATCHALL_SUBSCRIPTION]

        service = _make_service(
            {"https://connection.keboola.com": prod, "https://connection.eu.keboola.com": dev},
            projects={
                "prod": {"url": "https://connection.keboola.com", "token": "t1"},
                "dev": {"url": "https://connection.eu.keboola.com", "token": "t2"},
            },
        )
        result = service.list_subscriptions()

        assert {s["project_alias"] for s in result["subscriptions"]} == {"prod", "dev"}
        assert len(result["subscriptions"]) == 2

    def test_one_project_failing_does_not_abort_the_others(self) -> None:
        prod, broken = MagicMock(), MagicMock()
        prod.list_project_subscriptions.return_value = [CATCHALL_SUBSCRIPTION]
        broken.list_project_subscriptions.side_effect = KeboolaApiError(
            message="Access denied", error_code="AUTH_ERROR", status_code=403
        )

        service = _make_service(
            {"https://connection.keboola.com": prod, "https://connection.eu.keboola.com": broken},
            projects={
                "prod": {"url": "https://connection.keboola.com", "token": "t1"},
                "broken": {"url": "https://connection.eu.keboola.com", "token": "t2"},
            },
        )
        result = service.list_subscriptions()

        assert len(result["subscriptions"]) == 1
        assert len(result["errors"]) == 1
        assert result["errors"][0]["project_alias"] == "broken"
        assert result["errors"][0]["error_code"] == "AUTH_ERROR"

    def test_unknown_alias_raises_config_error(self) -> None:
        with pytest.raises(ConfigError):
            _make_service(MagicMock()).list_subscriptions(aliases=["nope"])

    def test_rows_are_sorted_deterministically(self) -> None:
        client = MagicMock()
        client.list_project_subscriptions.return_value = [
            WEBHOOK_SUBSCRIPTION,
            CATCHALL_SUBSCRIPTION,
            FLOW_SUBSCRIPTION,
        ]
        _wire_configs(client, [])

        result = _make_service(client).list_subscriptions()

        assert [s["subscription_id"] for s in result["subscriptions"]] == ["1234", "1235", "1236"]

    def test_client_is_closed_even_on_failure(self) -> None:
        client = MagicMock()
        client.list_project_subscriptions.side_effect = KeboolaApiError(
            message="boom", error_code="API_ERROR", status_code=500
        )

        _make_service(client).list_subscriptions()

        client.close.assert_called_once()


# ---------------------------------------------------------------------------
# get_subscription_detail
# ---------------------------------------------------------------------------


class TestGetSubscriptionDetail:
    def test_returns_row_with_resolved_config_name(self) -> None:
        client = MagicMock()
        client.get_project_subscription.return_value = FLOW_SUBSCRIPTION
        _wire_configs(client, [("keboola.flow", "98765", "Daily ETL")])

        result = _make_service(client).get_subscription_detail("prod", "1234")

        assert result["subscription_id"] == "1234"
        assert result["config_name"] == "Daily ETL"
        assert result["project_alias"] == "prod"
        client.get_project_subscription.assert_called_once_with("1234")

    def test_project_wide_subscription_skips_the_join(self) -> None:
        client = MagicMock()
        client.get_project_subscription.return_value = CATCHALL_SUBSCRIPTION

        result = _make_service(client).get_subscription_detail("prod", "1235")

        client.list_components_with_configs.assert_not_called()
        client.list_component_configs.assert_not_called()
        assert result["scope"] == "project-wide"

    def test_join_failure_still_returns_the_subscription(self) -> None:
        """The name is a nicety; a broken lookup must not hide the recipient."""
        client = MagicMock()
        client.get_project_subscription.return_value = FLOW_SUBSCRIPTION
        client.list_component_configs.side_effect = KeboolaApiError(
            message="boom", error_code="API_ERROR", status_code=500
        )

        result = _make_service(client).get_subscription_detail("prod", "1234")

        assert result["config_name"] == ""
        assert result["address"] == "ops@example.com"

    def test_unknown_alias_raises_config_error(self) -> None:
        with pytest.raises(ConfigError):
            _make_service(MagicMock()).get_subscription_detail("nope", "1")


# ---------------------------------------------------------------------------
# create_subscription / delete_subscription / replace_subscription_recipient
# (issue #690)
# ---------------------------------------------------------------------------


class TestCreateSubscription:
    def test_builds_email_recipient(self) -> None:
        client = MagicMock()
        client.create_project_subscription.return_value = {
            "id": "9001",
            "event": "job-failed",
            "recipient": {"channel": "email", "address": "ops@example.com"},
        }

        _make_service(client).create_subscription(
            "prod", event="job-failed", channel="email", address="ops@example.com"
        )

        client.create_project_subscription.assert_called_once_with(
            "job-failed", {"channel": "email", "address": "ops@example.com"}, None, None
        )

    def test_builds_webhook_recipient(self) -> None:
        client = MagicMock()
        client.create_project_subscription.return_value = {
            "id": "9002",
            "event": "job-failed",
            "recipient": {"channel": "webhook", "url": "https://hooks.example.com/x"},
        }

        _make_service(client).create_subscription(
            "prod",
            event="job-failed",
            channel="webhook",
            address="https://hooks.example.com/x",
        )

        client.create_project_subscription.assert_called_once_with(
            "job-failed",
            {"channel": "webhook", "url": "https://hooks.example.com/x"},
            None,
            None,
        )

    def test_builds_filters_in_component_config_branch_order_stringified(self) -> None:
        """Filter values must be stringified regardless of the caller's type.

        ``config_id`` is declared ``str | None`` (the CLI and ``serve`` both
        only ever pass a string), so this test passes it as ``str`` to stay
        within the public signature. ``branch_id`` IS declared ``int | None``
        -- it is passed as a real ``int`` here on purpose, so the assertion
        below still exercises ``_build_filters``'s ``str(branch_id)``
        stringification rather than a no-op string-to-string passthrough.
        """
        client = MagicMock()
        client.create_project_subscription.return_value = {
            "id": "9003",
            "event": "job-failed",
            "recipient": {"channel": "email", "address": "ops@example.com"},
        }

        _make_service(client).create_subscription(
            "prod",
            event="job-failed",
            channel="email",
            address="ops@example.com",
            component_id="keboola.flow",
            config_id="98765",
            branch_id=4242,
        )

        client.create_project_subscription.assert_called_once_with(
            "job-failed",
            {"channel": "email", "address": "ops@example.com"},
            [
                {"field": "job.component.id", "value": "keboola.flow"},
                {"field": "job.configuration.id", "value": "98765"},
                {"field": "branch.id", "value": "4242"},
            ],
            None,
        )

    def test_omits_filters_when_no_scope_args(self) -> None:
        client = MagicMock()
        client.create_project_subscription.return_value = {
            "id": "9004",
            "event": "job-failed",
            "recipient": {"channel": "email", "address": "ops@example.com"},
        }

        _make_service(client).create_subscription(
            "prod", event="job-failed", channel="email", address="ops@example.com"
        )

        args, _ = client.create_project_subscription.call_args
        assert args[2] is None

    def test_passes_expires_at(self) -> None:
        client = MagicMock()
        client.create_project_subscription.return_value = {
            "id": "9005",
            "event": "job-failed",
            "recipient": {"channel": "email", "address": "ops@example.com"},
        }

        _make_service(client).create_subscription(
            "prod",
            event="job-failed",
            channel="email",
            address="ops@example.com",
            expires_at="2027-01-01T00:00:00+01:00",
        )

        client.create_project_subscription.assert_called_once_with(
            "job-failed",
            {"channel": "email", "address": "ops@example.com"},
            None,
            "2027-01-01T00:00:00+01:00",
        )

    def test_returns_audit_row_with_project_alias_and_config_name(self) -> None:
        client = MagicMock()
        client.create_project_subscription.return_value = {
            "id": "9006",
            "event": "job-failed",
            "filters": [
                {"field": "job.component.id", "value": "keboola.flow"},
                {"field": "job.configuration.id", "value": "98765"},
            ],
            "recipient": {"channel": "email", "address": "ops@example.com"},
        }
        _wire_configs(client, [("keboola.flow", "98765", "Daily ETL")])

        result = _make_service(client).create_subscription(
            "prod",
            event="job-failed",
            channel="email",
            address="ops@example.com",
            component_id="keboola.flow",
            config_id="98765",
        )

        assert result["subscription_id"] == "9006"
        assert result["project_alias"] == "prod"
        assert result["config_name"] == "Daily ETL"
        client.close.assert_called_once()

    def test_invalid_channel_raises_config_error(self) -> None:
        with pytest.raises(ConfigError):
            _make_service(MagicMock()).create_subscription(
                "prod", event="job-failed", channel="carrier-pigeon", address="x"
            )

    def test_unknown_alias_raises_config_error(self) -> None:
        with pytest.raises(ConfigError):
            _make_service(MagicMock()).create_subscription(
                "nope", event="job-failed", channel="email", address="ops@example.com"
            )


class TestDeleteSubscription:
    def test_calls_delete_and_returns_envelope(self) -> None:
        client = MagicMock()

        result = _make_service(client).delete_subscription("prod", "123")

        client.delete_project_subscription.assert_called_once_with("123")
        assert result == {"project_alias": "prod", "subscription_id": "123", "deleted": True}
        client.close.assert_called_once()

    def test_api_404_propagates(self) -> None:
        """The client must still be closed even though the exception propagates.

        The client is opened before the ``try``/``finally`` and the delete
        call is what fails, so the `finally` runs on the way out -- a
        regression that dropped it would leak the underlying connection on
        every failed delete.
        """
        client = MagicMock()
        client.delete_project_subscription.side_effect = KeboolaApiError(
            message="Not found", error_code="NOT_FOUND", status_code=404
        )

        with pytest.raises(KeboolaApiError):
            _make_service(client).delete_subscription("prod", "999")

        client.close.assert_called_once()

    def test_unknown_alias_raises_config_error(self) -> None:
        with pytest.raises(ConfigError):
            _make_service(MagicMock()).delete_subscription("nope", "1")


class TestReplaceSubscriptionRecipient:
    def test_creates_new_before_deleting_old_and_keeps_old_channel(self) -> None:
        client = MagicMock()
        client.get_project_subscription.return_value = FLOW_SUBSCRIPTION
        client.create_project_subscription.return_value = {
            "id": "9100",
            "event": "job-failed",
            "filters": FLOW_SUBSCRIPTION["filters"],
            "recipient": {"channel": "email", "address": "new@example.com"},
        }
        _wire_configs(client, [("keboola.flow", "98765", "Daily ETL")])

        result = _make_service(client).replace_subscription_recipient(
            "prod", "1234", "new@example.com"
        )

        client.create_project_subscription.assert_called_once_with(
            "job-failed",
            {"channel": "email", "address": "new@example.com"},
            FLOW_SUBSCRIPTION["filters"],
            None,
        )
        client.delete_project_subscription.assert_called_once_with("1234")

        call_names = [
            call[0]
            for call in client.mock_calls
            if call[0] in ("create_project_subscription", "delete_project_subscription")
        ]
        assert call_names.index("create_project_subscription") < call_names.index(
            "delete_project_subscription"
        )

        assert result["old_subscription_id"] == "1234"
        assert result["new_subscription_id"] == "9100"
        assert result["old_address"] == "ops@example.com"
        assert result["old_deleted"] is True
        assert result["warnings"] == []
        assert result["subscription_id"] == "9100"
        assert result["config_name"] == "Daily ETL"
        client.close.assert_called_once()

    def test_delete_failure_sets_old_deleted_false_with_warning_and_does_not_raise(self) -> None:
        client = MagicMock()
        client.get_project_subscription.return_value = FLOW_SUBSCRIPTION
        client.create_project_subscription.return_value = {
            "id": "9101",
            "event": "job-failed",
            "filters": FLOW_SUBSCRIPTION["filters"],
            "recipient": {"channel": "email", "address": "new@example.com"},
        }
        client.delete_project_subscription.side_effect = KeboolaApiError(
            message="boom", error_code="API_ERROR", status_code=500
        )
        _wire_configs(client, [])

        result = _make_service(client).replace_subscription_recipient(
            "prod", "1234", "new@example.com"
        )

        assert result["old_deleted"] is False
        assert any("1234" in w for w in result["warnings"])
        assert result["new_subscription_id"] == "9101"
        # The client stays open across the caught delete failure -- it must
        # still be closed once the method returns, same as the happy path.
        client.close.assert_called_once()

    def test_non_api_delete_failure_also_sets_old_deleted_false_without_raising(self) -> None:
        """A raw transport failure on the delete must be swallowed too.

        ``_do_request`` lets httpx transport errors (``ReadError``,
        ``WriteError``, ``RemoteProtocolError``, ``ProxyError``, ...)
        propagate as-is -- they are not wrapped into ``KeboolaApiError``. The
        new subscription already exists by the time the delete runs, so ANY
        exception here must degrade to ``old_deleted: False`` + a warning,
        never escape: letting it escape would lose ``new_subscription_id``
        from the response, and a caller's retry would then mint a THIRD
        subscription.
        """
        client = MagicMock()
        client.get_project_subscription.return_value = FLOW_SUBSCRIPTION
        client.create_project_subscription.return_value = {
            "id": "9103",
            "event": "job-failed",
            "filters": FLOW_SUBSCRIPTION["filters"],
            "recipient": {"channel": "email", "address": "new@example.com"},
        }
        client.delete_project_subscription.side_effect = RuntimeError("connection reset")
        _wire_configs(client, [])

        result = _make_service(client).replace_subscription_recipient(
            "prod", "1234", "new@example.com"
        )

        assert result["old_deleted"] is False
        assert any("1234" in w for w in result["warnings"])
        assert result["new_subscription_id"] == "9103"
        client.close.assert_called_once()

    def test_expires_at_is_forwarded_verbatim(self) -> None:
        """The old subscription's ``expiresAt`` must ride along untouched.

        Every other replace test uses ``FLOW_SUBSCRIPTION``, which has no
        ``expiresAt`` key, so the 4th positional arg to
        ``create_project_subscription`` was previously only ever pinned to
        ``None`` -- never actually exercising the pass-through.
        """
        old_with_expiry = {**FLOW_SUBSCRIPTION, "expiresAt": "2027-01-01T00:00:00+01:00"}
        client = MagicMock()
        client.get_project_subscription.return_value = old_with_expiry
        client.create_project_subscription.return_value = {
            "id": "9104",
            "event": "job-failed",
            "filters": FLOW_SUBSCRIPTION["filters"],
            "expiresAt": "2027-01-01T00:00:00+01:00",
            "recipient": {"channel": "email", "address": "new@example.com"},
        }
        _wire_configs(client, [])

        _make_service(client).replace_subscription_recipient("prod", "1234", "new@example.com")

        client.create_project_subscription.assert_called_once_with(
            "job-failed",
            {"channel": "email", "address": "new@example.com"},
            FLOW_SUBSCRIPTION["filters"],
            "2027-01-01T00:00:00+01:00",
        )

    def test_new_channel_webhook_switches_recipient_key(self) -> None:
        client = MagicMock()
        client.get_project_subscription.return_value = FLOW_SUBSCRIPTION
        client.create_project_subscription.return_value = {
            "id": "9102",
            "event": "job-failed",
            "filters": FLOW_SUBSCRIPTION["filters"],
            "recipient": {"channel": "webhook", "url": "https://hooks.example.com/y"},
        }
        _wire_configs(client, [])

        _make_service(client).replace_subscription_recipient(
            "prod", "1234", "https://hooks.example.com/y", new_channel="webhook"
        )

        client.create_project_subscription.assert_called_once_with(
            "job-failed",
            {"channel": "webhook", "url": "https://hooks.example.com/y"},
            FLOW_SUBSCRIPTION["filters"],
            None,
        )

    def test_blank_old_channel_without_new_channel_raises_config_error(self) -> None:
        """ConfigError raised before the replacement is created.

        The client is opened before this check runs (``get_project_subscription``
        already happened), so ``finally`` still closes it on the way out --
        without the ``finally``, this path would leak the connection on
        every malformed-recipient replace attempt.
        """
        client = MagicMock()
        client.get_project_subscription.return_value = {
            "id": "1234",
            "event": "job-failed",
            "recipient": {},
        }

        with pytest.raises(ConfigError):
            _make_service(client).replace_subscription_recipient("prod", "1234", "new@example.com")

        client.create_project_subscription.assert_not_called()
        client.delete_project_subscription.assert_not_called()
        client.close.assert_called_once()

    def test_non_dict_old_recipient_raises_config_error_not_attribute_error(self) -> None:
        """A malformed ``recipient`` (not a dict) must degrade to ConfigError.

        Mirrors the same guard on the read path (``_extract_subscription_fields``)
        -- without it, ``old_recipient.get(...)`` on a non-dict value would
        raise a raw ``AttributeError`` instead of the intended "channel is
        unknown, pass one explicitly" error. Also confirms the client is
        still closed on this path, same as the blank-channel one above.
        """
        client = MagicMock()
        client.get_project_subscription.return_value = {
            "id": "1234",
            "event": "job-failed",
            "recipient": "not-a-dict",
        }

        with pytest.raises(ConfigError):
            _make_service(client).replace_subscription_recipient("prod", "1234", "new@example.com")

        client.create_project_subscription.assert_not_called()
        client.delete_project_subscription.assert_not_called()
        client.close.assert_called_once()

    def test_unknown_alias_raises_config_error(self) -> None:
        with pytest.raises(ConfigError):
            _make_service(MagicMock()).replace_subscription_recipient(
                "nope", "1", "new@example.com"
            )


# ---------------------------------------------------------------------------
# Overtime threshold (issue #989)
# ---------------------------------------------------------------------------


def _created(event: str = "job-processing-long", filters: list | None = None) -> dict:
    return {
        "id": "9200",
        "event": event,
        "filters": filters or [],
        "recipient": {"channel": "email", "address": "ops@example.com"},
    }


class TestCreateSubscriptionOvertime:
    """``overtime_percent`` writes the same filter the Keboola UI writes."""

    @pytest.mark.parametrize("event", ["job-processing-long", "phase-job-processing-long"])
    def test_writes_ui_shaped_threshold_filter_last(self, event: str) -> None:
        client = MagicMock()
        client.create_project_subscription.return_value = _created(event)

        _make_service(client).create_subscription(
            "prod",
            event=event,
            channel="email",
            address="ops@example.com",
            component_id="keboola.flow",
            config_id="98765",
            branch_id=4242,
            overtime_percent=50,
        )

        args, _ = client.create_project_subscription.call_args
        assert args[2] == [
            {"field": "job.component.id", "value": "keboola.flow"},
            {"field": "job.configuration.id", "value": "98765"},
            {"field": "branch.id", "value": "4242"},
            {"field": "durationOvertimePercentage", "value": 0.5, "operator": ">="},
        ]

    @pytest.mark.parametrize(
        ("percent", "fraction"), [(1, 0.01), (7, 0.07), (150, 1.5), (501, 5.01), (10000, 100.0)]
    )
    def test_value_is_percent_divided_by_100_as_the_ui_sends_it(
        self, percent: int, fraction: float
    ) -> None:
        client = MagicMock()
        client.create_project_subscription.return_value = _created()

        _make_service(client).create_subscription(
            "prod",
            event="job-processing-long",
            channel="email",
            address="ops@example.com",
            overtime_percent=percent,
        )

        args, _ = client.create_project_subscription.call_args
        sent = args[2][-1]
        assert sent["value"] == fraction
        assert isinstance(sent["value"], float)

    def test_omitted_keeps_previous_body(self) -> None:
        client = MagicMock()
        client.create_project_subscription.return_value = _created()

        _make_service(client).create_subscription(
            "prod", event="job-processing-long", channel="email", address="ops@example.com"
        )

        args, _ = client.create_project_subscription.call_args
        assert args[2] is None

    @pytest.mark.parametrize("event", ["job-failed", "job-succeeded", "phase-job-failed", "x"])
    def test_other_event_is_invalid_argument_before_any_call(self, event: str) -> None:
        client = MagicMock()

        with pytest.raises(KeboolaApiError) as excinfo:
            _make_service(client).create_subscription(
                "prod",
                event=event,
                channel="email",
                address="ops@example.com",
                overtime_percent=50,
            )

        assert excinfo.value.error_code == "INVALID_ARGUMENT"
        assert "--overtime-percent" in excinfo.value.message
        client.create_project_subscription.assert_not_called()

    @pytest.mark.parametrize(
        "bad",
        [
            0,
            -1,
            True,
            False,
            1.5,
            "50",
            10001,
            pytest.param(10**400, id="10e400"),
            pytest.param(10**5000, id="10e5000"),
        ],
    )
    def test_non_integer_or_negative_is_invalid_argument(self, bad: object) -> None:
        client = MagicMock()

        with pytest.raises(KeboolaApiError) as excinfo:
            _make_service(client).create_subscription(
                "prod",
                event="job-processing-long",
                channel="email",
                address="ops@example.com",
                overtime_percent=bad,  # ty: ignore[invalid-argument-type]
            )

        assert excinfo.value.error_code == "INVALID_ARGUMENT"
        client.create_project_subscription.assert_not_called()

    def test_created_row_reports_overtime_percent(self) -> None:
        client = MagicMock()
        client.create_project_subscription.return_value = _created(
            filters=[{"field": "durationOvertimePercentage", "value": 0.5, "operator": ">="}]
        )

        row = _make_service(client).create_subscription(
            "prod",
            event="job-processing-long",
            channel="email",
            address="ops@example.com",
            overtime_percent=50,
        )

        assert row["overtime_percent"] == 50


class TestOvertimePercentReadSide:
    """Audit rows expose the threshold only when it is unambiguous."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0.5, 50),
            (0.07, 7),
            (0.29, 29),
            (0.57, 57),
            (1.5, 150),
            (0, 0),
            (0.0, 0),
            ("0.25", 25),
            (0.125, 12.5),
            (0.000000001, 1e-07),
            (0.123456789, 12.3456789),
        ],
    )
    def test_single_ge_filter_is_percent(self, value: object, expected: float) -> None:
        row = _extract_subscription_fields(
            _created(
                filters=[{"field": "durationOvertimePercentage", "value": value, "operator": ">="}]
            )
        )
        assert row["overtime_percent"] == expected
        assert type(row["overtime_percent"]) is type(expected)

    @pytest.mark.parametrize(
        "filters",
        [
            [],
            [{"field": "branch.id", "value": "1"}],
            [{"field": "durationOvertimePercentage", "value": 0.5}],
            [{"field": "durationOvertimePercentage", "value": 0.5, "operator": "=="}],
            [
                {"field": "durationOvertimePercentage", "value": 0.5, "operator": ">="},
                {"field": "durationOvertimePercentage", "value": 0.7, "operator": ">="},
            ],
            [{"field": "durationOvertimePercentage", "value": "abc", "operator": ">="}],
            [{"field": "durationOvertimePercentage", "value": True, "operator": ">="}],
            [{"field": "durationOvertimePercentage", "value": None, "operator": ">="}],
            [{"field": "durationOvertimePercentage", "value": float("inf"), "operator": ">="}],
            [{"field": "durationOvertimePercentage", "value": float("nan"), "operator": ">="}],
            [{"field": "durationOvertimePercentage", "value": "nan", "operator": ">="}],
            [{"field": "durationOvertimePercentage", "value": "1_0", "operator": ">="}],
            [{"field": "durationOvertimePercentage", "value": " 0.5 ", "operator": ">="}],
            [{"field": "durationOvertimePercentage", "value": -0.5, "operator": ">="}],
            [{"field": "durationOvertimePercentage", "value": 1e308, "operator": ">="}],
            [{"field": "durationOvertimePercentage", "value": 10**400, "operator": ">="}],
            [{"field": "durationOvertimePercentage", "value": 10**5000, "operator": ">="}],
            [{"field": "durationOvertimePercentage", "value": 0.5, "operator": ">= "}],
            [{"field": "durationOvertimePercentage", "value": "1e999999", "operator": ">="}],
            [{"field": "durationOvertimePercentage", "value": "-1e999999", "operator": ">="}],
            ["not-a-dict"],
        ],
    )
    def test_absent_ambiguous_or_unreadable_threshold_is_none(self, filters: list) -> None:
        row = _extract_subscription_fields(_created(filters=filters))
        assert row["overtime_percent"] is None
        assert row["filters"] == filters


class TestReplaceRecipientKeepsOvertime:
    def test_threshold_filter_with_operator_is_carried_verbatim(self) -> None:
        old_filters = [
            {"field": "job.component.id", "value": "keboola.flow", "operator": "=="},
            {"field": "durationOvertimePercentage", "value": 0.125, "operator": ">="},
        ]
        client = MagicMock()
        client.get_project_subscription.return_value = {
            "id": "1234",
            "event": "job-processing-long",
            "filters": old_filters,
            "recipient": {"channel": "email", "address": "ops@example.com"},
        }
        client.create_project_subscription.return_value = {
            "id": "9300",
            "event": "job-processing-long",
            "filters": old_filters,
            "recipient": {"channel": "email", "address": "new@example.com"},
        }

        result = _make_service(client).replace_subscription_recipient(
            "prod", "1234", "new@example.com"
        )

        args, _ = client.create_project_subscription.call_args
        assert args[2] == old_filters
        assert result["overtime_percent"] == 12.5


# ---------------------------------------------------------------------------
# Generic filters and clone (issue #989, rest)
# ---------------------------------------------------------------------------


def _code(exc: KeboolaApiError) -> str:
    return str(exc.error_code)


class TestParseFilterExpression:
    @pytest.mark.parametrize(
        ("expr", "field", "operator", "value"),
        [
            ("averageDuration>=60", "averageDuration", ">=", "60"),
            ("a.b=x", "a.b", "==", "x"),
            ("a==x", "a", "==", "x"),
            ("a<=1", "a", "<=", "1"),
            ("a<1", "a", "<", "1"),
            ("a>1", "a", ">", "1"),
            ("job-x.y_z>=1", "job-x.y_z", ">=", "1"),
            # VALUE is kept byte-for-byte: ':', '=', '<', ',' and spaces survive.
            ("x=c:d=e<f, g h", "x", "==", "c:d=e<f, g h"),
            ("x==<5", "x", "==", "<5"),
            ("x=", "x", "==", ""),
            ("x= padded ", "x", "==", " padded "),
            ("job.configuration.id=00123", "job.configuration.id", "==", "00123"),
        ],
    )
    def test_valid_expressions(self, expr: str, field: str, operator: str, value: str) -> None:
        assert parse_filter_expression(expr) == {
            "field": field,
            "operator": operator,
            "value": value,
        }

    def test_value_is_always_a_string(self) -> None:
        parsed = parse_filter_expression("averageDuration>=123")
        assert parsed["value"] == "123"
        assert isinstance(parsed["value"], str)

    @pytest.mark.parametrize("expr", ["noop", "a..b=1", "=1", ".a=1", "a b=1", "", "a!=1"])
    def test_malformed_is_invalid_argument_naming_the_option(self, expr: str) -> None:
        with pytest.raises(KeboolaApiError) as exc_info:
            parse_filter_expression(expr)
        assert _code(exc_info.value) == "INVALID_ARGUMENT"
        assert "--filter" in exc_info.value.message


class TestNormalizeFilters:
    def test_normalizes_operator_and_keeps_typed_values(self) -> None:
        result = normalize_filters(
            [
                {"field": "a", "operator": "=", "value": 1},
                {"field": "b", "value": "x"},
                {"field": "c", "operator": ">=", "value": 0.5},
                {"field": "d", "operator": "==", "value": None},
                {"field": "e", "value": True},
            ]
        )
        assert result == [
            {"field": "a", "operator": "==", "value": 1},
            {"field": "b", "value": "x"},
            {"field": "c", "operator": ">=", "value": 0.5},
            {"field": "d", "operator": "==", "value": None},
            {"field": "e", "value": True},
        ]

    def test_none_and_empty_are_no_filters(self) -> None:
        assert normalize_filters(None) == []
        assert normalize_filters([]) == []

    @pytest.mark.parametrize(
        "bad",
        [
            [{"field": "a"}],  # value key missing
            [{"field": "a", "value": [1]}],
            [{"field": "a", "value": {"x": 1}}],
            [{"field": "a", "value": float("nan")}],
            [{"field": "a", "value": float("inf")}],
            [{"field": "a", "value": 1, "extra": 2}],
            [{"field": "a..b", "value": 1}],
            [{"field": "", "value": 1}],
            [{"field": 5, "value": 1}],
            [{"field": "a", "operator": "!=", "value": 1}],
            ["a=1"],
            {"field": "a", "value": 1},
        ],
    )
    def test_invalid_entries_are_invalid_argument(self, bad: object) -> None:
        with pytest.raises(KeboolaApiError) as exc_info:
            normalize_filters(bad)  # ty: ignore[invalid-argument-type]  -- bad input on purpose
        assert _code(exc_info.value) == "INVALID_ARGUMENT"


def _created_any(filters: list | None = None, event: str = "job-processing-long") -> dict:
    return {
        "id": "9400",
        "event": event,
        "filters": filters or [],
        "recipient": {"channel": "email", "address": "ops@example.com"},
    }


class TestCreateSubscriptionGenericFilters:
    def test_generic_filters_follow_the_dedicated_ones(self) -> None:
        client = MagicMock()
        client.create_project_subscription.return_value = _created_any()

        _make_service(client).create_subscription(
            "prod",
            event="job-processing-long",
            channel="email",
            address="ops@example.com",
            config_id="98765",
            overtime_percent=50,
            filters=[{"field": "averageDuration", "operator": ">=", "value": "60"}],
        )

        args, _ = client.create_project_subscription.call_args
        assert args[2] == [
            {"field": "job.configuration.id", "value": "98765"},
            {"field": "durationOvertimePercentage", "value": 0.5, "operator": ">="},
            {"field": "averageDuration", "operator": ">=", "value": "60"},
        ]

    def test_string_value_is_sent_as_string(self) -> None:
        client = MagicMock()
        client.create_project_subscription.return_value = _created_any()

        _make_service(client).create_subscription(
            "prod",
            event="job-failed",
            channel="email",
            address="ops@example.com",
            filters=[parse_filter_expression("job.configuration.id=00123")],
        )

        args, _ = client.create_project_subscription.call_args
        assert args[2] == [{"field": "job.configuration.id", "operator": "==", "value": "00123"}]

    def test_upper_overtime_bound_alone_is_allowed(self) -> None:
        client = MagicMock()
        client.create_project_subscription.return_value = _created_any()

        _make_service(client).create_subscription(
            "prod",
            event="job-processing-long",
            channel="email",
            address="ops@example.com",
            filters=[parse_filter_expression("durationOvertimePercentage<5")],
        )

        args, _ = client.create_project_subscription.call_args
        assert args[2] == [{"field": "durationOvertimePercentage", "operator": "<", "value": "5"}]

    @pytest.mark.parametrize(
        ("kwargs", "expr", "option"),
        [
            ({"overtime_percent": 50}, "durationOvertimePercentage<5", "--overtime-percent"),
            ({"component_id": "keboola.flow"}, "job.component.id=x", "--component-id"),
            ({"config_id": "1"}, "job.configuration.id=2", "--config-id"),
            ({"branch_id": 7}, "branch.id=8", "--branch"),
        ],
    )
    def test_dedicated_option_and_filter_on_same_field_is_refused(
        self, kwargs: dict, expr: str, option: str
    ) -> None:
        client = MagicMock()

        with pytest.raises(KeboolaApiError) as exc_info:
            _make_service(client).create_subscription(
                "prod",
                event="job-processing-long",
                channel="email",
                address="ops@example.com",
                filters=[parse_filter_expression(expr)],
                **kwargs,
            )

        assert _code(exc_info.value) == "INVALID_ARGUMENT"
        assert option in exc_info.value.message
        assert "--filter" in exc_info.value.message
        client.create_project_subscription.assert_not_called()

    def test_same_field_and_operator_twice_is_refused(self) -> None:
        client = MagicMock()

        with pytest.raises(KeboolaApiError) as exc_info:
            _make_service(client).create_subscription(
                "prod",
                event="job-failed",
                channel="email",
                address="ops@example.com",
                filters=[parse_filter_expression("x=1"), parse_filter_expression("x==2")],
            )

        assert _code(exc_info.value) == "INVALID_ARGUMENT"
        client.create_project_subscription.assert_not_called()

    def test_omitted_operator_and_equality_are_the_same_operator(self) -> None:
        client = MagicMock()

        with pytest.raises(KeboolaApiError):
            _make_service(client).create_subscription(
                "prod",
                event="job-failed",
                channel="email",
                address="ops@example.com",
                filters=[{"field": "x", "value": 1}, {"field": "x", "operator": "==", "value": 2}],
            )
        client.create_project_subscription.assert_not_called()

    def test_range_on_one_field_is_allowed(self) -> None:
        client = MagicMock()
        client.create_project_subscription.return_value = _created_any()

        _make_service(client).create_subscription(
            "prod",
            event="job-failed",
            channel="email",
            address="ops@example.com",
            filters=[parse_filter_expression("x>=1"), parse_filter_expression("x<5")],
        )

        args, _ = client.create_project_subscription.call_args
        assert [(f["field"], f["operator"]) for f in args[2]] == [("x", ">="), ("x", "<")]

    def test_empty_filter_list_sends_no_filters(self) -> None:
        client = MagicMock()
        client.create_project_subscription.return_value = _created_any()

        _make_service(client).create_subscription(
            "prod", event="job-failed", channel="email", address="ops@example.com", filters=[]
        )

        args, _ = client.create_project_subscription.call_args
        assert args[2] is None

    def test_service_refusal_of_unknown_field_is_preserved(self) -> None:
        client = MagicMock()
        client.create_project_subscription.side_effect = KeboolaApiError(
            message="Field 'nope' is not valid for event job-failed",
            status_code=400,
            error_code="VALIDATION_ERROR",
            details={"field": "nope"},
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            _make_service(client).create_subscription(
                "prod",
                event="job-failed",
                channel="email",
                address="ops@example.com",
                filters=[parse_filter_expression("nope=1")],
            )

        assert exc_info.value.error_code == "VALIDATION_ERROR"
        assert exc_info.value.status_code == 400
        assert exc_info.value.details == {"field": "nope"}
        assert "nope" in exc_info.value.message

    def test_lost_response_says_not_to_rerun(self) -> None:
        client = MagicMock()
        client.create_project_subscription.side_effect = KeboolaApiError(
            message="Gateway timeout", status_code=504, error_code="API_ERROR"
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            _make_service(client).create_subscription(
                "prod", event="job-failed", channel="email", address="ops@example.com"
            )

        assert client.create_project_subscription.call_count == 1
        assert exc_info.value.retryable is False
        assert "notification list" in exc_info.value.message
        assert "re-run" in exc_info.value.message


class TestFilterValueReadsEqualityOnly:
    def test_inequality_branch_filter_is_not_a_branch(self) -> None:
        row = _extract_subscription_fields(
            {
                "id": "1",
                "event": "job-failed",
                "filters": [{"field": "branch.id", "operator": "<", "value": "123"}],
                "recipient": {"channel": "email", "address": "a@example.com"},
            }
        )
        assert row["branch_id"] == ""

    def test_inequality_config_filter_does_not_scope_the_row(self) -> None:
        row = _extract_subscription_fields(
            {
                "id": "1",
                "event": "job-failed",
                "filters": [{"field": "job.configuration.id", "operator": ">=", "value": "5"}],
                "recipient": {"channel": "email", "address": "a@example.com"},
            }
        )
        assert row["config_id"] == ""
        assert row["scope"] == "project-wide"

    def test_explicit_equality_operator_is_read(self) -> None:
        row = _extract_subscription_fields(
            {
                "id": "1",
                "event": "job-failed",
                "filters": [
                    {"field": "branch.id", "operator": ">", "value": "1"},
                    {"field": "branch.id", "operator": "==", "value": "42"},
                ],
                "recipient": {"channel": "email", "address": "a@example.com"},
            }
        )
        assert row["branch_id"] == "42"


CLONE_SOURCE: dict[str, Any] = {
    "id": "500",
    "event": "job-processing-long",
    "expiresAt": "2027-01-01T00:00:00Z",
    "filters": [
        {"field": "job.component.id", "value": "keboola.flow"},
        {"field": "job.configuration.id", "value": "111"},
        {"field": "branch.id", "value": "4242"},
        {"field": "durationOvertimePercentage", "operator": ">=", "value": 0.5},
        {"field": "someFutureField", "value": None},
    ],
    "recipient": {"channel": "email", "address": "ops@example.com", "extra": "kept"},
}


def _clone_client(source: dict | None = None) -> MagicMock:
    src = copy.deepcopy(source if source is not None else CLONE_SOURCE)
    client = MagicMock()
    client.get_project_subscription.return_value = src

    def create(event: str, recipient: dict, filters: list | None, expires_at: str | None) -> dict:
        return {
            "id": "900",
            "event": event,
            "filters": filters or [],
            "recipient": recipient,
            **({"expiresAt": expires_at} if expires_at else {}),
        }

    client.create_project_subscription.side_effect = create
    client.list_component_configs.return_value = []
    return client


def _posted(client: MagicMock) -> tuple:
    args, _ = client.create_project_subscription.call_args
    return args


class TestCloneSubscription:
    def test_no_override_duplicates_the_source_verbatim(self) -> None:
        client = _clone_client()

        result = _make_service(client).clone_subscription("prod", "500")

        client.get_project_subscription.assert_called_once_with("500")
        assert client.create_project_subscription.call_count == 1
        event, recipient, filters, expires_at = _posted(client)
        assert event == "job-processing-long"
        assert recipient == CLONE_SOURCE["recipient"]
        assert filters == CLONE_SOURCE["filters"]
        assert expires_at == "2027-01-01T00:00:00Z"
        assert result["source_subscription_id"] == "500"
        assert result["new_subscription_id"] == "900"
        assert result["dry_run"] is False
        assert result["filters_replaced"] == []
        assert result["filters_added"] == []
        assert result["filters_kept"] == CLONE_SOURCE["filters"]
        client.delete_project_subscription.assert_not_called()

    def test_config_override_replaces_only_that_field(self) -> None:
        client = _clone_client()

        result = _make_service(client).clone_subscription("prod", "500", config_id="222")

        _, _, filters, _ = _posted(client)
        assert filters == [
            {"field": "job.component.id", "value": "keboola.flow"},
            {"field": "branch.id", "value": "4242"},
            {"field": "durationOvertimePercentage", "operator": ">=", "value": 0.5},
            {"field": "someFutureField", "value": None},
            {"field": "job.configuration.id", "value": "222"},
        ]
        assert result["filters_replaced"] == [
            {
                "field": "job.configuration.id",
                "old": [{"field": "job.configuration.id", "value": "111"}],
                "new": [{"field": "job.configuration.id", "value": "222"}],
            }
        ]
        assert result["config_id"] == "222"
        assert result["branch_id"] == "4242"
        assert result["overtime_percent"] == 50

    def test_two_bound_range_is_replaced_as_a_group(self) -> None:
        source = copy.deepcopy(CLONE_SOURCE)
        source["filters"] = [
            {"field": "a", "value": "1"},
            {"field": "averageDuration", "operator": ">=", "value": "10"},
            {"field": "b", "value": "2"},
            {"field": "averageDuration", "operator": "<", "value": "100"},
            {"field": "c", "value": "3"},
        ]
        client = _clone_client(source)

        result = _make_service(client).clone_subscription(
            "prod",
            "500",
            filters=[
                parse_filter_expression("averageDuration>=60"),
                parse_filter_expression("averageDuration<600"),
            ],
        )

        _, _, filters, _ = _posted(client)
        assert filters == [
            {"field": "a", "value": "1"},
            {"field": "b", "value": "2"},
            {"field": "c", "value": "3"},
            {"field": "averageDuration", "operator": ">=", "value": "60"},
            {"field": "averageDuration", "operator": "<", "value": "600"},
        ]
        assert result["filters_replaced"][0]["field"] == "averageDuration"
        assert len(result["filters_replaced"][0]["old"]) == 2
        assert len(result["filters_replaced"][0]["new"]) == 2

    def test_new_field_is_reported_as_added(self) -> None:
        client = _clone_client()

        result = _make_service(client).clone_subscription(
            "prod", "500", filters=[parse_filter_expression("averageDuration>=60")]
        )

        assert result["filters_added"] == [
            {"field": "averageDuration", "operator": ">=", "value": "60"}
        ]
        assert result["filters_replaced"] == []

    def test_active_branch_never_replaces_the_source_branch(self) -> None:
        client = _clone_client()
        service = _make_service(client)
        project = service._config_store.get_project("prod")
        assert project is not None
        project.active_branch_id = 999

        service.clone_subscription("prod", "500", config_id="222")

        _, _, filters, _ = _posted(client)
        branch_filters = [f for f in filters if f["field"] == "branch.id"]
        assert branch_filters == [{"field": "branch.id", "value": "4242"}]

    def test_branch_override_replaces_the_branch_filter(self) -> None:
        client = _clone_client()

        _make_service(client).clone_subscription("prod", "500", branch_id=7)

        _, _, filters, _ = _posted(client)
        assert [f for f in filters if f["field"] == "branch.id"] == [
            {"field": "branch.id", "value": "7"}
        ]

    def test_source_without_branch_filter_warns_about_the_default_branch(self) -> None:
        source = copy.deepcopy(CLONE_SOURCE)
        source["filters"] = [f for f in source["filters"] if f["field"] != "branch.id"]
        client = _clone_client(source)

        result = _make_service(client).clone_subscription("prod", "500", config_id="222")

        assert any("default branch" in w for w in result["warnings"])

    def test_address_only_keeps_the_source_channel(self) -> None:
        source = copy.deepcopy(CLONE_SOURCE)
        source["recipient"] = {"channel": "webhook", "url": "https://hooks.example.com/a"}
        client = _clone_client(source)

        _make_service(client).clone_subscription(
            "prod", "500", address="https://hooks.example.com/b"
        )

        _, recipient, _, _ = _posted(client)
        assert recipient == {"channel": "webhook", "url": "https://hooks.example.com/b"}

    def test_address_and_channel_switch_the_recipient(self) -> None:
        client = _clone_client()

        _make_service(client).clone_subscription(
            "prod", "500", address="https://hooks.example.com/b", channel="webhook"
        )

        _, recipient, _, _ = _posted(client)
        assert recipient == {"channel": "webhook", "url": "https://hooks.example.com/b"}

    def test_channel_without_address_is_refused_before_any_call(self) -> None:
        client = _clone_client()

        with pytest.raises(KeboolaApiError) as exc_info:
            _make_service(client).clone_subscription("prod", "500", channel="webhook")

        assert _code(exc_info.value) == "INVALID_ARGUMENT"
        assert "--address" in exc_info.value.message
        client.get_project_subscription.assert_not_called()
        client.create_project_subscription.assert_not_called()

    def test_invalid_channel_is_invalid_argument(self) -> None:
        client = _clone_client()

        with pytest.raises(KeboolaApiError) as exc_info:
            _make_service(client).clone_subscription(
                "prod", "500", address="a@example.com", channel="slack"
            )

        assert _code(exc_info.value) == "INVALID_ARGUMENT"
        client.create_project_subscription.assert_not_called()

    def test_expires_at_override_replaces_an_expired_source(self) -> None:
        source = copy.deepcopy(CLONE_SOURCE)
        source["expiresAt"] = "2020-01-01T00:00:00Z"
        client = _clone_client(source)

        _make_service(client).clone_subscription("prod", "500", expires_at="2030-01-01T00:00:00Z")

        assert _posted(client)[3] == "2030-01-01T00:00:00Z"

    def test_clear_expires_at_sends_no_expiry(self) -> None:
        client = _clone_client()

        _make_service(client).clone_subscription("prod", "500", clear_expires_at=True)

        assert _posted(client)[3] is None

    def test_expires_at_and_clear_together_are_refused(self) -> None:
        client = _clone_client()

        with pytest.raises(KeboolaApiError) as exc_info:
            _make_service(client).clone_subscription(
                "prod", "500", expires_at="2030-01-01T00:00:00Z", clear_expires_at=True
            )

        assert _code(exc_info.value) == "INVALID_ARGUMENT"
        client.get_project_subscription.assert_not_called()

    def test_overtime_on_a_non_overtime_source_makes_no_post(self) -> None:
        source = copy.deepcopy(CLONE_SOURCE)
        source["event"] = "job-failed"
        source["filters"] = []
        client = _clone_client(source)

        with pytest.raises(KeboolaApiError) as exc_info:
            _make_service(client).clone_subscription("prod", "500", overtime_percent=50)

        assert _code(exc_info.value) == "INVALID_ARGUMENT"
        client.create_project_subscription.assert_not_called()

    def test_overtime_override_replaces_the_threshold_group(self) -> None:
        client = _clone_client()

        result = _make_service(client).clone_subscription("prod", "500", overtime_percent=75)

        _, _, filters, _ = _posted(client)
        overtime = [f for f in filters if f["field"] == "durationOvertimePercentage"]
        assert overtime == [
            {"field": "durationOvertimePercentage", "value": 0.75, "operator": ">="}
        ]
        assert result["event"] == "job-processing-long"

    def test_event_is_preserved(self) -> None:
        source = copy.deepcopy(CLONE_SOURCE)
        source["event"] = "phase-job-failed"
        client = _clone_client(source)

        _make_service(client).clone_subscription("prod", "500", config_id="222")

        assert _posted(client)[0] == "phase-job-failed"

    def test_inherited_phase_on_config_override_warns(self) -> None:
        source = copy.deepcopy(CLONE_SOURCE)
        source["filters"].append({"field": "phase.id", "value": "ph-1"})
        client = _clone_client(source)

        result = _make_service(client).clone_subscription("prod", "500", config_id="222")

        assert any("phase.id" in w for w in result["warnings"])

    def test_inherited_phase_on_generic_config_filter_warns(self) -> None:
        # Devin on #1010: a --filter on job.configuration.id changes the flow
        # exactly like --config-id, so the inherited phase warning must fire.
        source = copy.deepcopy(CLONE_SOURCE)
        source["filters"].append({"field": "phase.id", "value": "ph-1"})
        client = _clone_client(source)

        result = _make_service(client).clone_subscription(
            "prod", "500", filters=[parse_filter_expression("job.configuration.id=222")]
        )

        assert any("phase.id" in w for w in result["warnings"])

    def test_inherited_phase_without_flow_change_does_not_warn(self) -> None:
        source = copy.deepcopy(CLONE_SOURCE)
        source["filters"].append({"field": "phase.id", "value": "ph-1"})
        client = _clone_client(source)

        result = _make_service(client).clone_subscription("prod", "500", address="new@example.com")

        assert not any("phase.id" in w for w in result["warnings"])

    def test_overridden_phase_does_not_warn(self) -> None:
        source = copy.deepcopy(CLONE_SOURCE)
        source["filters"].append({"field": "phase.id", "value": "ph-1"})
        client = _clone_client(source)

        result = _make_service(client).clone_subscription(
            "prod", "500", config_id="222", filters=[parse_filter_expression("phase.id=ph-2")]
        )

        assert not any("phase.id" in w for w in result["warnings"])

    def test_dry_run_reads_once_and_writes_nothing(self) -> None:
        client = _clone_client()

        result = _make_service(client).clone_subscription(
            "prod", "500", config_id="222", dry_run=True
        )

        client.get_project_subscription.assert_called_once_with("500")
        client.create_project_subscription.assert_not_called()
        client.delete_project_subscription.assert_not_called()
        client.list_component_configs.assert_not_called()
        client.list_components_with_configs.assert_not_called()
        assert result["dry_run"] is True
        assert "new_subscription_id" not in result
        assert result["planned"]["event"] == "job-processing-long"
        assert result["planned"]["recipient"] == CLONE_SOURCE["recipient"]
        assert result["planned"]["expiresAt"] == "2027-01-01T00:00:00Z"
        assert {"field": "job.configuration.id", "value": "222"} in result["planned"]["filters"]

    def test_lost_post_response_is_one_post_and_says_not_to_rerun(self) -> None:
        client = _clone_client()
        client.create_project_subscription.side_effect = KeboolaApiError(
            message="Bad gateway", status_code=502, error_code="API_ERROR", retryable=True
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            _make_service(client).clone_subscription("prod", "500", config_id="222")

        assert client.create_project_subscription.call_count == 1
        assert exc_info.value.retryable is False
        assert "notification list" in exc_info.value.message
        assert "re-run" in exc_info.value.message

    def test_name_stamping_failure_still_returns_the_new_id(self) -> None:
        client = _clone_client()
        client.list_component_configs.side_effect = RuntimeError("boom")

        result = _make_service(client).clone_subscription("prod", "500", config_id="222")

        assert result["new_subscription_id"] == "900"
        assert result["warnings"]

    def test_legacy_filters_round_trip_exactly(self) -> None:
        source = copy.deepcopy(CLONE_SOURCE)
        source["filters"] = [
            {"field": "unknownLegacy", "value": None},
            {"field": "noOperator", "value": 1},
            {"field": "branch.id", "value": 4242},
        ]
        client = _clone_client(source)

        _make_service(client).clone_subscription("prod", "500")

        assert _posted(client)[2] == source["filters"]

    @pytest.mark.parametrize(
        "bad_filters",
        [
            ["job.component.id=x"],
            [{"value": "x"}],
            [{"field": "a"}],
            {"field": "a", "value": 1},
        ],
    )
    def test_malformed_source_filters_refuse_the_clone(self, bad_filters: object) -> None:
        source = copy.deepcopy(CLONE_SOURCE)
        source["filters"] = bad_filters
        client = _clone_client(source)

        with pytest.raises(KeboolaApiError) as exc_info:
            _make_service(client).clone_subscription("prod", "500")

        assert _code(exc_info.value) == "INVALID_ARGUMENT"
        assert "500" in exc_info.value.message
        assert "create" in exc_info.value.message
        client.create_project_subscription.assert_not_called()

    def test_dedicated_and_generic_filter_on_one_field_refused_before_get(self) -> None:
        client = _clone_client()

        with pytest.raises(KeboolaApiError) as exc_info:
            _make_service(client).clone_subscription(
                "prod",
                "500",
                config_id="222",
                filters=[parse_filter_expression("job.configuration.id=333")],
            )

        assert _code(exc_info.value) == "INVALID_ARGUMENT"
        client.get_project_subscription.assert_not_called()

    def test_unknown_alias_raises_config_error(self) -> None:
        client = _clone_client()
        with pytest.raises(ConfigError):
            _make_service(client).clone_subscription("nope", "500")


# ---------------------------------------------------------------------------
# Review findings on #989 (Astra): every case is INVALID_ARGUMENT or an
# uncertain-outcome error, never a crash or a silent fallback.
# ---------------------------------------------------------------------------


class TestNormalizeFiltersNeverCrashes:
    @pytest.mark.parametrize("operator", [[], {}, 5, (">=",)])
    def test_non_string_operator_is_invalid_argument(self, operator: object) -> None:
        with pytest.raises(KeboolaApiError) as exc_info:
            normalize_filters([{"field": "x", "operator": operator, "value": 1}])
        assert _code(exc_info.value) == "INVALID_ARGUMENT"

    def test_non_string_extra_key_is_invalid_argument(self) -> None:
        with pytest.raises(KeboolaApiError) as exc_info:
            normalize_filters([{"field": "x", "value": 1, 7: 2}])
        assert _code(exc_info.value) == "INVALID_ARGUMENT"
        assert "7" in exc_info.value.message


class TestCloneOvertimeTypeCheckedFirst:
    @pytest.mark.parametrize("bad", ["50", True, 50.0, 0, 10001])
    def test_bad_overtime_is_invalid_argument_before_any_call(self, bad: object) -> None:
        client = _clone_client()

        with pytest.raises(KeboolaApiError) as exc_info:
            _make_service(client).clone_subscription(
                "prod",
                "500",
                overtime_percent=bad,  # ty: ignore[invalid-argument-type]  -- bad input on purpose
            )

        assert _code(exc_info.value) == "INVALID_ARGUMENT"
        client.get_project_subscription.assert_not_called()
        client.create_project_subscription.assert_not_called()


class TestCloneSourceWithoutFilters:
    @pytest.mark.parametrize("missing", ["absent", "null"])
    def test_missing_or_null_filters_refuse_the_clone(self, missing: str) -> None:
        source = copy.deepcopy(CLONE_SOURCE)
        if missing == "absent":
            del source["filters"]
        else:
            source["filters"] = None
        client = _clone_client(source)

        with pytest.raises(KeboolaApiError) as exc_info:
            _make_service(client).clone_subscription("prod", "500", config_id="222")

        assert _code(exc_info.value) == "INVALID_ARGUMENT"
        assert "500" in exc_info.value.message
        assert "create" in exc_info.value.message
        client.create_project_subscription.assert_not_called()

    def test_empty_filter_list_is_copied_as_project_wide(self) -> None:
        source = copy.deepcopy(CLONE_SOURCE)
        source["filters"] = []
        client = _clone_client(source)

        result = _make_service(client).clone_subscription("prod", "500")

        assert client.create_project_subscription.call_count == 1
        assert _posted(client)[2] is None
        assert result["filters_kept"] == []


_TRANSPORT_ERRORS = [
    httpx.RemoteProtocolError("peer closed connection"),
    httpx.ReadError("read failed"),
    httpx.WriteError("write failed"),
]


class TestUnwrappedTransportErrorOnCreatePost:
    @pytest.mark.parametrize("error", _TRANSPORT_ERRORS)
    def test_create_says_not_to_rerun(self, error: Exception) -> None:
        client = MagicMock()
        client.create_project_subscription.side_effect = error

        with pytest.raises(KeboolaApiError) as exc_info:
            _make_service(client).create_subscription(
                "prod", event="job-failed", channel="email", address="ops@example.com"
            )

        assert client.create_project_subscription.call_count == 1
        assert exc_info.value.retryable is False
        assert "notification list" in exc_info.value.message
        assert "re-run" in exc_info.value.message

    @pytest.mark.parametrize("error", _TRANSPORT_ERRORS)
    def test_clone_says_not_to_rerun(self, error: Exception) -> None:
        client = _clone_client()
        client.create_project_subscription.side_effect = error

        with pytest.raises(KeboolaApiError) as exc_info:
            _make_service(client).clone_subscription("prod", "500", config_id="222")

        assert client.create_project_subscription.call_count == 1
        assert exc_info.value.retryable is False
        assert "notification list" in exc_info.value.message
        assert "re-run" in exc_info.value.message


class TestEmptyValuesAreNotOmitted:
    @pytest.mark.parametrize(
        ("kwargs", "option"),
        [
            ({"component_id": ""}, "--component-id"),
            ({"config_id": ""}, "--config-id"),
            ({"config_id": "  "}, "--config-id"),
        ],
    )
    def test_create_refuses_an_empty_dedicated_id(self, kwargs: dict, option: str) -> None:
        client = MagicMock()

        with pytest.raises(KeboolaApiError) as exc_info:
            _make_service(client).create_subscription(
                "prod", event="job-failed", channel="email", address="ops@example.com", **kwargs
            )

        assert _code(exc_info.value) == "INVALID_ARGUMENT"
        assert option in exc_info.value.message
        client.create_project_subscription.assert_not_called()

    def test_empty_config_id_does_not_bypass_the_conflict_check(self) -> None:
        client = MagicMock()

        with pytest.raises(KeboolaApiError) as exc_info:
            _make_service(client).create_subscription(
                "prod",
                event="job-failed",
                channel="email",
                address="ops@example.com",
                config_id="",
                filters=[parse_filter_expression("job.configuration.id=5")],
            )

        assert _code(exc_info.value) == "INVALID_ARGUMENT"
        client.create_project_subscription.assert_not_called()

    @pytest.mark.parametrize(
        ("kwargs", "option"),
        [({"component_id": ""}, "--component-id"), ({"config_id": ""}, "--config-id")],
    )
    def test_clone_refuses_an_empty_dedicated_id(self, kwargs: dict, option: str) -> None:
        client = _clone_client()

        with pytest.raises(KeboolaApiError) as exc_info:
            _make_service(client).clone_subscription("prod", "500", **kwargs)

        assert _code(exc_info.value) == "INVALID_ARGUMENT"
        assert option in exc_info.value.message
        client.get_project_subscription.assert_not_called()
        client.create_project_subscription.assert_not_called()

    @pytest.mark.parametrize("empty", ["", "   "])
    def test_create_refuses_an_empty_expiry(self, empty: str) -> None:
        client = MagicMock()

        with pytest.raises(KeboolaApiError) as exc_info:
            _make_service(client).create_subscription(
                "prod",
                event="job-failed",
                channel="email",
                address="ops@example.com",
                expires_at=empty,
            )

        assert _code(exc_info.value) == "INVALID_ARGUMENT"
        assert "--expires-at" in exc_info.value.message
        client.create_project_subscription.assert_not_called()

    def test_clone_refuses_an_empty_expiry_instead_of_inheriting(self) -> None:
        client = _clone_client()

        with pytest.raises(KeboolaApiError) as exc_info:
            _make_service(client).clone_subscription("prod", "500", expires_at="")

        assert _code(exc_info.value) == "INVALID_ARGUMENT"
        assert "--expires-at" in exc_info.value.message
        client.create_project_subscription.assert_not_called()
