"""Tests for KeboolaClient.list_token_events -- the last-used derivation feed.

``GET /v2/storage/tokens/{id}/events`` answers with BOTH the actions performed
*by* the token and the lifecycle events performed *on* it (the server ORs
``token.id == id`` with ``objectId == id AND objectType == 'token'``). Only the
first group is evidence of use, so the client narrows server-side with
``q=token.id:{id}`` rather than fetching a mixed feed and guessing client-side.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from keboola_agent_cli.client import KeboolaClient

STACK_URL = "https://connection.keboola.com"
TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"


def _make_client() -> KeboolaClient:
    return KeboolaClient(stack_url=STACK_URL, token=TOKEN)


def _query_of(request) -> dict[str, list[str]]:
    return parse_qs(urlsplit(str(request.url)).query)


class TestListTokenEvents:
    def test_narrows_to_events_performed_by_the_token(self, httpx_mock) -> None:
        """The request must carry q=token.id:{id} -- the performed-by filter.

        Without it the newest event for a token an admin just refreshed is
        ``storage.tokenRefreshed`` (an event *about* the token), which would be
        read as the token's own activity or -- once filtered out client-side --
        leave nothing at all and report a used token as never used.
        """
        httpx_mock.add_response(
            method="GET",
            json=[],
            status_code=200,
        )

        client = _make_client()
        try:
            client.list_token_events("9001")
        finally:
            client.close()

        request = httpx_mock.get_requests()[0]
        assert request.method == "GET"
        assert urlsplit(str(request.url)).path == "/v2/storage/tokens/9001/events"
        assert _query_of(request)["q"] == ["token.id:9001"]

    def test_asks_for_a_single_newest_event_by_default(self, httpx_mock) -> None:
        """limit=1 is enough: the feed is sorted newest-first by default."""
        httpx_mock.add_response(
            method="GET",
            json=[],
            status_code=200,
        )

        client = _make_client()
        try:
            client.list_token_events("9001")
        finally:
            client.close()

        assert _query_of(httpx_mock.get_requests()[0])["limit"] == ["1"]

    def test_returns_the_event_array_verbatim(self, httpx_mock) -> None:
        httpx_mock.add_response(
            method="GET",
            json=[
                {
                    "uuid": "01a01e1f-5c53-725a-9fe7-56945f67487a",
                    "created": "2026-08-20T09:13:33+0200",
                    "event": "storage.tablesListed",
                }
            ],
            status_code=200,
        )

        client = _make_client()
        try:
            events = client.list_token_events("9001")
        finally:
            client.close()

        assert [e["event"] for e in events] == ["storage.tablesListed"]

    def test_non_array_payload_degrades_to_empty(self, httpx_mock) -> None:
        """An envelope object instead of the documented array must not explode.

        Mirrors ``list_tokens``: an unexpected shape yields no events rather
        than raising inside the caller's parallel fan-out.
        """
        httpx_mock.add_response(
            method="GET",
            json={"error": "unexpected envelope"},
            status_code=200,
        )

        client = _make_client()
        try:
            assert client.list_token_events("9001") == []
        finally:
            client.close()

    def test_token_id_is_url_quoted(self, httpx_mock) -> None:
        """Same defensive quoting as delete_token / refresh_token."""
        httpx_mock.add_response(
            method="GET",
            json=[],
            status_code=200,
        )

        client = _make_client()
        try:
            client.list_token_events("9001/../9002")
        finally:
            client.close()

        path = urlsplit(str(httpx_mock.get_requests()[0].url)).path
        assert path == "/v2/storage/tokens/9001%2F..%2F9002/events"
