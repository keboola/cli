"""Tests for the Storage API triggers L3 client -- issue #714.

Covers the wire contract of ``GET /v2/storage/triggers``: the query filters the
Storage controller declares (``component`` / ``configurationId``), the response
shape from the API's own OpenAPI example, and the fact that the route is a
plain Storage path authenticated with the project Storage token.

The payload below is the verbatim example from
``Controller/Storage/Triggers/TriggerListAction`` -- taken from the controller
rather than guessed, because the field names are easy to get wrong (``tables``
is a list of ``{"tableId": ...}`` objects, not a list of strings, and
``lastRun`` is nullable).
"""

from __future__ import annotations

from keboola_agent_cli.client import KeboolaClient

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
STACK_URL = "https://connection.keboola.com"
TRIGGERS_URL = f"{STACK_URL}/v2/storage/triggers"

LIVE_TRIGGERS = [
    {
        "id": "3",
        "runWithTokenId": 123,
        "component": "orchestration",
        "configurationId": "config-100",
        "lastRun": "2017-02-13T16:42:00+0100",
        "creatorToken": {"id": 1, "description": "dev@keboola.com"},
        "coolDownPeriodMinutes": 20,
        "tables": [
            {"tableId": "in.c-test.watched-1"},
            {"tableId": "in.c-prod.watched-5"},
        ],
    }
]


class TestListTriggers:
    """``list_triggers`` returns the API payload verbatim; shaping is L2's job."""

    def test_issues_get_to_the_storage_triggers_path(self, httpx_mock) -> None:
        httpx_mock.add_response(url=TRIGGERS_URL, method="GET", json=LIVE_TRIGGERS)

        with KeboolaClient(stack_url=STACK_URL, token=TEST_TOKEN) as client:
            result = client.list_triggers()

        assert result == LIVE_TRIGGERS
        requests = httpx_mock.get_requests()
        assert len(requests) == 1
        assert str(requests[0].url) == TRIGGERS_URL
        assert requests[0].headers["X-StorageApi-Token"] == TEST_TOKEN

    def test_configuration_id_is_sent_as_query_param(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{TRIGGERS_URL}?configurationId=config-100",
            method="GET",
            json=LIVE_TRIGGERS,
        )

        with KeboolaClient(stack_url=STACK_URL, token=TEST_TOKEN) as client:
            client.list_triggers(configuration_id="config-100")

        assert "configurationId=config-100" in str(httpx_mock.get_requests()[0].url)

    def test_component_filter_is_sent_as_query_param(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{TRIGGERS_URL}?component=keboola.flow",
            method="GET",
            json=[],
        )

        with KeboolaClient(stack_url=STACK_URL, token=TEST_TOKEN) as client:
            client.list_triggers(component="keboola.flow")

        assert "component=keboola.flow" in str(httpx_mock.get_requests()[0].url)

    def test_numeric_configuration_id_is_coerced_to_string(self, httpx_mock) -> None:
        """Flow ids are strings in kbagent but numeric on some stacks."""
        httpx_mock.add_response(url=f"{TRIGGERS_URL}?configurationId=500", method="GET", json=[])

        with KeboolaClient(stack_url=STACK_URL, token=TEST_TOKEN) as client:
            client.list_triggers(configuration_id=500)  # ty: ignore[invalid-argument-type]

        assert "configurationId=500" in str(httpx_mock.get_requests()[0].url)

    def test_no_filters_sends_no_query_string(self, httpx_mock) -> None:
        """An empty filter must be omitted, not sent as an empty parameter."""
        httpx_mock.add_response(url=TRIGGERS_URL, method="GET", json=[])

        with KeboolaClient(stack_url=STACK_URL, token=TEST_TOKEN) as client:
            client.list_triggers(component=None, configuration_id=None)

        assert str(httpx_mock.get_requests()[0].url) == TRIGGERS_URL
