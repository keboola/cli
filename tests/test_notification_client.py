"""Tests for the Notification Service L3 client -- issue #600.

Covers:
- Host derivation: connection.<region> -> notification.<region>, mirroring the
  queue / encryption / billing sibling-host pattern.
- ``list_project_subscriptions`` issuing a GET to the derived notification host,
  with and without the ``event`` query filter, returning the parsed JSON verbatim.
- ``get_project_subscription`` path building, including quoting of the
  subscription ID so a hostile ID cannot escape the path segment.
- Sub-client lifecycle: the lazily created notification client is closed by
  ``KeboolaClient.close()`` rather than leaked.

The payloads are the verbatim shapes from the service's public swagger
(https://notification.<region>.keboola.com/docs/swagger.yaml) -- note the
kebab-case event names and the dotted ``job.configuration.id`` filter fields,
which the issue's original draft got wrong.
"""

from __future__ import annotations

from keboola_agent_cli.client import KeboolaClient

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
STACK_URL = "https://connection.eu-central-1.keboola.com"
NOTIFICATION_URL = "https://notification.eu-central-1.keboola.com"

# Verbatim swagger-shaped subscriptions: one flow-scoped, one project-wide
# catch-all with no filters at all, one webhook recipient (``url``, not
# ``address``).
LIVE_SUBSCRIPTIONS = [
    {
        "id": "1234",
        "event": "job-failed",
        "filters": [
            {"field": "job.component.id", "value": "keboola.flow"},
            {"field": "job.configuration.id", "value": "98765"},
        ],
        "recipient": {"channel": "email", "address": "ops@example.com"},
    },
    {
        "id": "1235",
        "event": "job-failed",
        "recipient": {"channel": "email", "address": "catchall@example.com"},
    },
    {
        "id": "1236",
        "event": "job-processing-long",
        "filters": [
            {"field": "job.configuration.id", "value": "98765"},
            {"field": "durationOvertimePercentage", "operator": ">=", "value": 0.75},
        ],
        "recipient": {"channel": "webhook", "url": "https://hooks.example.com/kbc"},
    },
]


class TestDeriveNotificationUrl:
    """The base URL is connection.<region> -> notification.<region>."""

    def test_eu_central_stack(self) -> None:
        assert KeboolaClient._derive_service_url(STACK_URL, "notification") == NOTIFICATION_URL

    def test_us_stack(self) -> None:
        assert (
            KeboolaClient._derive_service_url("https://connection.keboola.com", "notification")
            == "https://notification.keboola.com"
        )

    def test_gcp_stack(self) -> None:
        assert (
            KeboolaClient._derive_service_url(
                "https://connection.us-east4.gcp.keboola.com", "notification"
            )
            == "https://notification.us-east4.gcp.keboola.com"
        )


class TestListProjectSubscriptions:
    def test_issues_get_to_notification_host(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{NOTIFICATION_URL}/project-subscriptions",
            method="GET",
            json=LIVE_SUBSCRIPTIONS,
            status_code=200,
        )

        with KeboolaClient(stack_url=STACK_URL, token=TEST_TOKEN) as client:
            result = client.list_project_subscriptions()

        assert result == LIVE_SUBSCRIPTIONS
        requests = httpx_mock.get_requests()
        assert len(requests) == 1
        assert str(requests[0].url) == f"{NOTIFICATION_URL}/project-subscriptions"
        # The read path authenticates with the plain project Storage token --
        # no elevated scope, no manage token.
        assert requests[0].headers["X-StorageApi-Token"] == TEST_TOKEN

    def test_event_filter_is_sent_as_query_param(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{NOTIFICATION_URL}/project-subscriptions?event=job-failed",
            method="GET",
            json=[LIVE_SUBSCRIPTIONS[0]],
        )

        with KeboolaClient(stack_url=STACK_URL, token=TEST_TOKEN) as client:
            result = client.list_project_subscriptions(event="job-failed")

        assert result == [LIVE_SUBSCRIPTIONS[0]]
        assert str(httpx_mock.get_requests()[0].url).endswith("?event=job-failed")

    def test_empty_event_is_not_sent(self, httpx_mock) -> None:
        """An empty string must not become ``?event=`` -- the API would 400."""
        httpx_mock.add_response(
            url=f"{NOTIFICATION_URL}/project-subscriptions",
            method="GET",
            json=[],
        )

        with KeboolaClient(stack_url=STACK_URL, token=TEST_TOKEN) as client:
            client.list_project_subscriptions(event="")

        assert "event" not in str(httpx_mock.get_requests()[0].url)

    def test_returns_raw_json_verbatim(self, httpx_mock) -> None:
        """No shaping in L3 -- unknown keys survive for the service layer."""
        payload = [{"id": "1", "event": "job-failed", "unknownField": "passthrough"}]
        httpx_mock.add_response(url=f"{NOTIFICATION_URL}/project-subscriptions", json=payload)

        with KeboolaClient(stack_url=STACK_URL, token=TEST_TOKEN) as client:
            assert client.list_project_subscriptions() == payload


class TestGetProjectSubscription:
    def test_issues_get_to_subscription_path(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{NOTIFICATION_URL}/project-subscriptions/1234",
            method="GET",
            json=LIVE_SUBSCRIPTIONS[0],
        )

        with KeboolaClient(stack_url=STACK_URL, token=TEST_TOKEN) as client:
            assert client.get_project_subscription("1234") == LIVE_SUBSCRIPTIONS[0]

    def test_subscription_id_is_quoted(self, httpx_mock) -> None:
        """A slash in the ID must not silently address a different endpoint."""
        httpx_mock.add_response(
            url=f"{NOTIFICATION_URL}/project-subscriptions/12%2F34",
            method="GET",
            json={},
        )

        with KeboolaClient(stack_url=STACK_URL, token=TEST_TOKEN) as client:
            client.get_project_subscription("12/34")

        assert str(httpx_mock.get_requests()[0].url).endswith("/project-subscriptions/12%2F34")


class TestSubClientLifecycle:
    def test_close_closes_the_notification_sub_client(self, httpx_mock) -> None:
        httpx_mock.add_response(url=f"{NOTIFICATION_URL}/project-subscriptions", json=[])

        client = KeboolaClient(stack_url=STACK_URL, token=TEST_TOKEN)
        client.list_project_subscriptions()
        sub_client = client._notification_client
        assert sub_client is not None

        client.close()

        assert sub_client.is_closed
