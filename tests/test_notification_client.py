"""Client-layer tests for the notification service mixin (issue #600).

Pins the wire contract: the derived host, the GET-only dispatcher, the
`?event=` passthrough, and tolerance of the payload shapes the endpoint can
answer with.
"""

from __future__ import annotations

from keboola_agent_cli.client import KeboolaClient

STACK_URL = "https://connection.north-europe.azure.keboola.com"
NOTIFICATION_URL = "https://notification.north-europe.azure.keboola.com"
TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

SUBSCRIPTION = {
    "id": "101",
    "event": "job-failed",
    "filters": [
        {"field": "job.component.id", "value": "keboola.flow"},
        {"field": "job.configuration.id", "value": "9001"},
    ],
    "recipient": {"channel": "email", "address": "ops@example.com"},
}


def _client() -> KeboolaClient:
    return KeboolaClient(stack_url=STACK_URL, token=TOKEN)


class TestNotificationHost:
    def test_base_url_is_derived_from_the_stack(self) -> None:
        """No hardcoded hostname: connection.<region> -> notification.<region>."""
        client = _client()
        try:
            assert client._notification_base_url == NOTIFICATION_URL
        finally:
            client.close()

    def test_list_hits_project_subscriptions_with_the_storage_token(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{NOTIFICATION_URL}/project-subscriptions",
            json=[SUBSCRIPTION],
        )

        client = _client()
        try:
            assert client.list_project_subscriptions() == [SUBSCRIPTION]
        finally:
            client.close()

        request = httpx_mock.get_requests()[0]
        assert request.method == "GET"
        assert request.headers["X-StorageApi-Token"] == TOKEN

    def test_event_is_sent_as_a_query_param(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{NOTIFICATION_URL}/project-subscriptions?event=job-failed",
            json=[SUBSCRIPTION],
        )

        client = _client()
        try:
            client.list_project_subscriptions(event="job-failed")
        finally:
            client.close()

        assert httpx_mock.get_requests()[0].url.params["event"] == "job-failed"

    def test_no_event_sends_a_clean_url(self, httpx_mock) -> None:
        """An empty `?event=` would be a different request; omit the param."""
        httpx_mock.add_response(url=f"{NOTIFICATION_URL}/project-subscriptions", json=[])

        client = _client()
        try:
            client.list_project_subscriptions()
        finally:
            client.close()

        assert str(httpx_mock.get_requests()[0].url) == (
            f"{NOTIFICATION_URL}/project-subscriptions"
        )


class TestPayloadTolerance:
    def test_wrapped_payload_is_unwrapped(self, httpx_mock) -> None:
        """Documented as a bare array; a wrapped shape must not raise."""
        httpx_mock.add_response(
            url=f"{NOTIFICATION_URL}/project-subscriptions",
            json={"subscriptions": [SUBSCRIPTION]},
        )

        client = _client()
        try:
            assert client.list_project_subscriptions() == [SUBSCRIPTION]
        finally:
            client.close()

    def test_unexpected_payload_degrades_to_empty(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{NOTIFICATION_URL}/project-subscriptions", json={"unexpected": True}
        )

        client = _client()
        try:
            assert client.list_project_subscriptions() == []
        finally:
            client.close()


class TestReadOnlyByConstruction:
    def test_dispatcher_exposes_no_verb_parameter(self) -> None:
        """The write path must be unreachable through this dispatcher.

        `POST` / `DELETE /project-subscriptions` change who gets paged when
        production breaks. `_notification_get` hardcodes the verb, so no
        future caller can construct such a request through it -- the same
        guarantee `_billing_get` gives against a real-money top-up.
        """
        import inspect

        from keboola_agent_cli.client._core import _CoreClient

        params = inspect.signature(_CoreClient._notification_get).parameters
        assert "method" not in params
