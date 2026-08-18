"""Tests for the device-enrollment primitives on KeboolaClient (0.66.0).

Covers the new scoped-token lifecycle (create/delete/refresh) and the
per-device stream-source lifecycle (create with sink provisioning, get, list,
delete). Storage endpoints resolve to host ``connection.keboola.com``; the
Stream control plane resolves to the sibling host ``stream.keboola.com``
(derived ``connection.<region>`` -> ``stream.<region>``).
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs

import pytest

from keboola_agent_cli.client import KeboolaClient
from keboola_agent_cli.errors import KeboolaApiError

STACK_URL = "https://connection.keboola.com"
STREAM_BASE_URL = "https://stream.keboola.com"
TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
BRANCH = "default"

# A raw Stream source object as returned by GET .../sources/{id}. The OTLP
# block carries the ingest secret embedded in the URL -- the client flattens it
# UNMASKED (the lib layer reveals it to the device once, never persists it).
OTLP_SOURCE: dict[str, Any] = {
    "sourceId": "device-42",
    "type": "otlp",
    "name": "device-42",
    "description": "capture device 42",
    "otlp": {
        "url": "https://stream-in.keboola.com/otlp/123/device-42/S3CRET",
        "baseUrl": "https://stream-in.keboola.com/otlp/123/device-42",
        "secret": "S3CRET",
    },
}


def _make_client() -> KeboolaClient:
    return KeboolaClient(stack_url=STACK_URL, token=TOKEN)


# ----------------------------------------------------------------------------
# Scoped-token lifecycle
# ----------------------------------------------------------------------------


class TestCreateScopedToken:
    """POST /v2/storage/tokens as FORM data with the right scope fields."""

    def test_files_upload_plus_bucket_write_form_body(self, httpx_mock) -> None:
        """A capture-device token (write one sink bucket + expiring) posts
        bucketPermissions[<id>]=write, expiresIn and description -- and does NOT
        send componentAccess / canReadAllFileUploads when they were not asked
        for (unset scope stays minimal)."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/v2/storage/tokens",
            method="POST",
            json={
                "id": "9001",
                "token": "REVEALED-ONCE-SECRET",
                "description": "device 42",
                "expires": "2026-08-01T00:00:00+0000",
            },
            status_code=201,
        )

        client = _make_client()
        try:
            result = client.create_scoped_token(
                description="device 42",
                bucket_permissions={"in.c-otlp-device-42": "write"},
                expires_in=3600,
            )
        finally:
            client.close()

        assert result["id"] == "9001"
        # One-time secret reveal is echoed straight through.
        assert result["token"] == "REVEALED-ONCE-SECRET"

        request = httpx_mock.get_requests()[0]
        # httpx sends form data (application/x-www-form-urlencoded), not JSON.
        assert "application/x-www-form-urlencoded" in request.headers["content-type"]
        body = parse_qs(request.read().decode())
        assert body["description"] == ["device 42"]
        assert body["expiresIn"] == ["3600"]
        assert body["bucketPermissions[in.c-otlp-device-42]"] == ["write"]
        # Not requested => must be absent, not sent as a falsy value.
        assert "componentAccess[]" not in body
        assert "canReadAllFileUploads" not in body

    def test_can_read_all_file_uploads_and_component_access(self, httpx_mock) -> None:
        """can_read_all_file_uploads=True adds canReadAllFileUploads=1 and a
        non-empty component_access adds componentAccess[] entries."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/v2/storage/tokens",
            method="POST",
            json={"id": "9002", "token": "SECRET2", "description": "broad"},
            status_code=201,
        )

        client = _make_client()
        try:
            client.create_scoped_token(
                description="broad",
                component_access=["keboola.ex-aws-s3", "keboola.wr-db-snowflake"],
                can_read_all_file_uploads=True,
            )
        finally:
            client.close()

        body = parse_qs(httpx_mock.get_requests()[0].read().decode())
        assert body["description"] == ["broad"]
        assert body["canReadAllFileUploads"] == ["1"]
        # parse_qs collapses repeated keys into a list, preserving order.
        assert body["componentAccess[]"] == [
            "keboola.ex-aws-s3",
            "keboola.wr-db-snowflake",
        ]
        # No bucket grants / no expiry requested here.
        assert not any(k.startswith("bucketPermissions[") for k in body)
        assert "expiresIn" not in body


class TestCreateScopedTokenIsNotReplayed:
    """``POST /v2/storage/tokens`` mints a credential -- never replay it (#599)."""

    def test_persistent_500_makes_exactly_one_attempt(self, httpx_mock) -> None:
        """The reported EU-GCP 500 must cost one attempt, not three mint tries."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/v2/storage/tokens",
            status_code=500,
            json={"error": "Application error."},
        )

        client = _make_client()
        try:
            with pytest.raises(KeboolaApiError) as exc_info:
                client.create_scoped_token(description="device 42", expires_in=60)
            assert len(httpx_mock.get_requests()) == 1
            assert exc_info.value.retryable is False
        finally:
            client.close()


class TestDeleteToken:
    def test_delete_returns_none(self, httpx_mock) -> None:
        """DELETE /v2/storage/tokens/{id} -> 204, no body, returns None."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/v2/storage/tokens/9001",
            method="DELETE",
            status_code=204,
        )

        client = _make_client()
        try:
            assert client.delete_token("9001") is None
        finally:
            client.close()

        request = httpx_mock.get_requests()[0]
        assert request.method == "DELETE"
        assert str(request.url) == f"{STACK_URL}/v2/storage/tokens/9001"


class TestRefreshToken:
    def test_refresh_returns_new_token_dict(self, httpx_mock) -> None:
        """POST /v2/storage/tokens/{id}/refresh -> the rotated token dict."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/v2/storage/tokens/9001/refresh",
            method="POST",
            json={"id": "9001", "token": "ROTATED-SECRET", "description": "device 42"},
            status_code=200,
        )

        client = _make_client()
        try:
            result = client.refresh_token("9001")
        finally:
            client.close()

        assert result["id"] == "9001"
        assert result["token"] == "ROTATED-SECRET"
        request = httpx_mock.get_requests()[0]
        assert request.method == "POST"
        assert str(request.url) == f"{STACK_URL}/v2/storage/tokens/9001/refresh"


# ----------------------------------------------------------------------------
# Per-device stream-source lifecycle
# ----------------------------------------------------------------------------


class TestCreateStreamSource:
    def test_otlp_provisions_sinks(self, httpx_mock) -> None:
        """create_stream_source(otlp, provision_sinks=True): POST source (202
        finished Task with outputs.sourceId), list_sinks (empty), 3x create_sink
        (finished Task), GET source. Returned dict is flattened + unmasked."""
        # 1. POST create source -> 202 Task, already finished.
        httpx_mock.add_response(
            url=f"{STREAM_BASE_URL}/v1/branches/default/sources",
            method="POST",
            json={
                "taskId": "task-src",
                "isFinished": True,
                "status": "success",
                "outputs": {"sourceId": "device-42"},
            },
            status_code=202,
        )
        # 2. list_sinks -> empty (nothing provisioned yet).
        httpx_mock.add_response(
            url=f"{STREAM_BASE_URL}/v1/branches/default/sources/device-42/sinks",
            method="GET",
            json={"sinks": []},
        )
        # 3. Three create_sink POSTs (logs/metrics/traces), each a finished Task.
        httpx_mock.add_response(
            url=f"{STREAM_BASE_URL}/v1/branches/default/sources/device-42/sinks",
            method="POST",
            json={"taskId": "task-sink", "isFinished": True, "status": "success"},
            status_code=202,
            is_reusable=True,
        )
        # 4. GET the finished source (carries the OTLP endpoint + secret).
        httpx_mock.add_response(
            url=f"{STREAM_BASE_URL}/v1/branches/default/sources/device-42",
            method="GET",
            json=OTLP_SOURCE,
        )

        client = _make_client()
        try:
            result = client.create_stream_source("device-42", source_type="otlp")
        finally:
            client.close()

        assert result["id"] == "device-42"
        assert result["source_id"] == "device-42"
        assert result["type"] == "otlp"
        # OTLP endpoint is flattened and the secret is UNMASKED (embedded in URL).
        assert result["otlp_url"] == OTLP_SOURCE["otlp"]["url"]
        assert "S3CRET" in result["otlp_url"]
        assert result["otlp_secret"] == "S3CRET"
        assert result["base_endpoint"] == OTLP_SOURCE["otlp"]["baseUrl"]
        # Sinks provisioned => bucket id is in.c-otlp-<sourceId>.
        assert result["sink_bucket_id"] == "in.c-otlp-device-42"
        # Raw source echoed under "source".
        assert result["source"] == OTLP_SOURCE

        # Exactly three sink-create POSTs, one per signal, restricted correctly.
        sink_posts = [
            r
            for r in httpx_mock.get_requests()
            if r.method == "POST" and r.url.path.endswith("/sinks")
        ]
        assert len(sink_posts) == 3
        signals = sorted(json.loads(r.content)["allowedSignals"][0] for r in sink_posts)
        assert signals == ["logs", "metrics", "traces"]

    def test_no_sinks_leaves_bucket_none(self, httpx_mock) -> None:
        """provision_sinks=False: no list_sinks / no create_sink calls, and
        sink_bucket_id comes back None (bare source)."""
        httpx_mock.add_response(
            url=f"{STREAM_BASE_URL}/v1/branches/default/sources",
            method="POST",
            json={
                "taskId": "task-src",
                "isFinished": True,
                "status": "success",
                "outputs": {"sourceId": "device-42"},
            },
            status_code=202,
        )
        httpx_mock.add_response(
            url=f"{STREAM_BASE_URL}/v1/branches/default/sources/device-42",
            method="GET",
            json=OTLP_SOURCE,
        )

        client = _make_client()
        try:
            result = client.create_stream_source(
                "device-42", source_type="otlp", provision_sinks=False
            )
        finally:
            client.close()

        assert result["source_id"] == "device-42"
        assert result["sink_bucket_id"] is None
        # No sink traffic at all.
        assert not [r for r in httpx_mock.get_requests() if r.url.path.endswith("/sinks")]


class TestGetStreamSource:
    def test_happy_path(self, httpx_mock) -> None:
        """get_stream_source flattens the source and derives the sink bucket from ACTUAL sinks."""
        httpx_mock.add_response(
            url=f"{STREAM_BASE_URL}/v1/branches/default/sources/device-42",
            method="GET",
            json=OTLP_SOURCE,
        )
        # sink_bucket_id is derived from the source's real sinks (their table id),
        # not assumed from the source type.
        httpx_mock.add_response(
            url=f"{STREAM_BASE_URL}/v1/branches/default/sources/device-42/sinks",
            method="GET",
            json={"sinks": [{"table": {"tableId": "in.c-otlp-device-42.logs"}}]},
        )

        client = _make_client()
        try:
            result = client.get_stream_source("device-42")
        finally:
            client.close()

        assert result["id"] == "device-42"
        assert result["otlp_secret"] == "S3CRET"
        assert result["sink_bucket_id"] == "in.c-otlp-device-42"

    def test_no_sinks_returns_none_bucket(self, httpx_mock) -> None:
        """An OTLP source with no provisioned sinks reports sink_bucket_id=None.

        Guards against assuming ``in.c-otlp-<id>`` exists when it does not (a
        consumer would otherwise scope a device token to a missing bucket).
        """
        httpx_mock.add_response(
            url=f"{STREAM_BASE_URL}/v1/branches/default/sources/device-42",
            method="GET",
            json=OTLP_SOURCE,
        )
        httpx_mock.add_response(
            url=f"{STREAM_BASE_URL}/v1/branches/default/sources/device-42/sinks",
            method="GET",
            json={"sinks": []},
        )

        client = _make_client()
        try:
            result = client.get_stream_source("device-42")
        finally:
            client.close()

        assert result["sink_bucket_id"] is None


class TestListStreamSources:
    def test_returns_raw_source_list(self, httpx_mock) -> None:
        """list_stream_sources returns the raw ``sources`` array unchanged."""
        httpx_mock.add_response(
            url=f"{STREAM_BASE_URL}/v1/branches/default/sources",
            method="GET",
            json={"sources": [OTLP_SOURCE]},
        )

        client = _make_client()
        try:
            result = client.list_stream_sources()
        finally:
            client.close()

        assert isinstance(result, list)
        assert result[0]["sourceId"] == "device-42"


class TestDeleteStreamSource:
    def test_delete_polls_task(self, httpx_mock) -> None:
        """delete_stream_source issues DELETE (202 Task) and polls to completion."""
        httpx_mock.add_response(
            url=f"{STREAM_BASE_URL}/v1/branches/default/sources/device-42",
            method="DELETE",
            json={"taskId": "task-del", "isFinished": True, "status": "success"},
            status_code=202,
        )

        client = _make_client()
        try:
            assert client.delete_stream_source("device-42") is None
        finally:
            client.close()

        request = httpx_mock.get_requests()[0]
        assert request.method == "DELETE"
        assert str(request.url) == (f"{STREAM_BASE_URL}/v1/branches/default/sources/device-42")
