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

from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.errors import ConfigError, KeboolaApiError
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

    def test_api_404_propagates(self) -> None:
        client = MagicMock()
        client.delete_project_subscription.side_effect = KeboolaApiError(
            message="Not found", error_code="NOT_FOUND", status_code=404
        )

        with pytest.raises(KeboolaApiError):
            _make_service(client).delete_subscription("prod", "999")

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

    def test_non_dict_old_recipient_raises_config_error_not_attribute_error(self) -> None:
        """A malformed ``recipient`` (not a dict) must degrade to ConfigError.

        Mirrors the same guard on the read path (``_extract_subscription_fields``)
        -- without it, ``old_recipient.get(...)`` on a non-dict value would
        raise a raw ``AttributeError`` instead of the intended "channel is
        unknown, pass one explicitly" error.
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

    def test_unknown_alias_raises_config_error(self) -> None:
        with pytest.raises(ConfigError):
            _make_service(MagicMock()).replace_subscription_recipient(
                "nope", "1", "new@example.com"
            )
