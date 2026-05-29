"""Tests for StreamClient -- URL derivation, source/sink CRUD, task polling."""

from __future__ import annotations

import json

import pytest

from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.stream_client import StreamClient

STACK_URL = "https://connection.keboola.com"
STREAM_BASE_URL = "https://stream.keboola.com"
TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
BRANCH = "default"

SAMPLE_SOURCE = {
    "sourceId": "my-otlp",
    "type": "otlp",
    "name": "my-otlp",
    "otlp": {
        "url": "https://stream-in.keboola.com/otlp/123/my-otlp/SECRET",
        "baseUrl": "https://stream-in.keboola.com/otlp/123/my-otlp",
        "secret": "SECRET",
    },
}


class TestDeriveStreamUrl:
    """The control-plane base URL is connection.<region> -> stream.<region>."""

    def test_us_stack(self) -> None:
        assert (
            StreamClient._derive_service_url("https://connection.keboola.com", "stream")
            == "https://stream.keboola.com"
        )

    def test_eu_stack(self) -> None:
        assert (
            StreamClient._derive_service_url(
                "https://connection.eu-central-1.keboola.com", "stream"
            )
            == "https://stream.eu-central-1.keboola.com"
        )

    def test_gcp_stack(self) -> None:
        assert (
            StreamClient._derive_service_url(
                "https://connection.us-east4.gcp.keboola.com", "stream"
            )
            == "https://stream.us-east4.gcp.keboola.com"
        )


class TestSourceCrud:
    def test_list_sources(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STREAM_BASE_URL}/v1/branches/default/sources",
            json={"sources": [SAMPLE_SOURCE]},
        )
        client = StreamClient(stack_url=STACK_URL, token=TOKEN)
        try:
            result = client.list_sources(BRANCH)
            assert result["sources"][0]["sourceId"] == "my-otlp"
            # Auth header is the Storage token, not a manage token.
            assert httpx_mock.get_requests()[0].headers["X-StorageApi-Token"] == TOKEN
        finally:
            client.close()

    def test_get_source(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STREAM_BASE_URL}/v1/branches/default/sources/my-otlp",
            json=SAMPLE_SOURCE,
        )
        client = StreamClient(stack_url=STACK_URL, token=TOKEN)
        try:
            result = client.get_source(BRANCH, "my-otlp")
            assert result["otlp"]["secret"] == "SECRET"
        finally:
            client.close()

    def test_create_source_returns_task(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STREAM_BASE_URL}/v1/branches/default/sources",
            method="POST",
            json={"taskId": "t1", "isFinished": False, "outputs": {"sourceId": "my-otlp"}},
            status_code=202,
        )
        client = StreamClient(stack_url=STACK_URL, token=TOKEN)
        try:
            task = client.create_source(BRANCH, name="my-otlp", source_type="otlp")
            assert task["taskId"] == "t1"
            body = json.loads(httpx_mock.get_requests()[0].content)
            assert body == {"name": "my-otlp", "type": "otlp"}
        finally:
            client.close()

    def test_delete_source_returns_task(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STREAM_BASE_URL}/v1/branches/default/sources/my-otlp",
            method="DELETE",
            json={"taskId": "t2", "isFinished": False},
            status_code=202,
        )
        client = StreamClient(stack_url=STACK_URL, token=TOKEN)
        try:
            task = client.delete_source(BRANCH, "my-otlp")
            assert task["taskId"] == "t2"
        finally:
            client.close()

    def test_list_sinks(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STREAM_BASE_URL}/v1/branches/default/sources/my-otlp/sinks",
            json={"sinks": [{"sinkId": "logs", "table": {"tableId": "in.c-otlp-my-otlp.logs"}}]},
        )
        client = StreamClient(stack_url=STACK_URL, token=TOKEN)
        try:
            result = client.list_sinks(BRANCH, "my-otlp")
            assert result["sinks"][0]["table"]["tableId"] == "in.c-otlp-my-otlp.logs"
        finally:
            client.close()

    def test_create_sink_payload(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STREAM_BASE_URL}/v1/branches/default/sources/my-otlp/sinks",
            method="POST",
            json={"taskId": "sk1", "isFinished": False},
            status_code=202,
        )
        client = StreamClient(stack_url=STACK_URL, token=TOKEN)
        try:
            columns = [{"type": "uuid", "name": "id"}, {"type": "body", "name": "body"}]
            task = client.create_sink(
                BRANCH,
                "my-otlp",
                name="Logs",
                table_id="in.c-otlp-my-otlp.logs",
                columns=columns,
                allowed_signals=["logs"],
            )
            assert task["taskId"] == "sk1"
            body = json.loads(httpx_mock.get_requests()[0].content)
            assert body["type"] == "table"
            assert body["allowedSignals"] == ["logs"]
            assert body["table"]["tableId"] == "in.c-otlp-my-otlp.logs"
            assert body["table"]["mapping"]["columns"] == columns
        finally:
            client.close()


class TestWaitForTask:
    def test_already_finished(self) -> None:
        client = StreamClient(stack_url=STACK_URL, token=TOKEN)
        try:
            finished = {"taskId": "t1", "isFinished": True, "status": "success"}
            assert client.wait_for_task(finished) is finished
        finally:
            client.close()

    def test_polls_until_finished(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STREAM_BASE_URL}/v1/tasks/t1",
            json={"taskId": "t1", "isFinished": True, "status": "success"},
        )
        client = StreamClient(stack_url=STACK_URL, token=TOKEN)
        try:
            pending = {"taskId": "t1", "isFinished": False}
            done = client.wait_for_task(pending, poll_interval=0.0)
            assert done["isFinished"] is True
        finally:
            client.close()

    def test_uses_task_url_for_polling(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STREAM_BASE_URL}/v1/tasks/abc/def",
            json={"taskId": "abc/def", "isFinished": True, "status": "success"},
        )
        client = StreamClient(stack_url=STACK_URL, token=TOKEN)
        try:
            pending = {
                "taskId": "abc/def",
                "isFinished": False,
                "url": "https://stream.keboola.com/v1/tasks/abc/def",
            }
            done = client.wait_for_task(pending, poll_interval=0.0)
            assert done["isFinished"] is True
        finally:
            client.close()

    def test_error_task_raises(self) -> None:
        client = StreamClient(stack_url=STACK_URL, token=TOKEN)
        try:
            with pytest.raises(KeboolaApiError):
                client.wait_for_task({"isFinished": True, "status": "error", "error": "boom"})
        finally:
            client.close()

    def test_timeout_raises(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{STREAM_BASE_URL}/v1/tasks/t1",
            json={"taskId": "t1", "isFinished": False, "status": "processing"},
            is_reusable=True,
        )
        client = StreamClient(stack_url=STACK_URL, token=TOKEN)
        try:
            with pytest.raises(KeboolaApiError) as exc:
                client.wait_for_task(
                    {"taskId": "t1", "isFinished": False}, timeout=0.05, poll_interval=0.0
                )
            assert exc.value.error_code == "TIMEOUT"
        finally:
            client.close()
