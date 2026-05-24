"""Tests for MetastoreClient -- URL derivation, envelope shape, error normalization.

Mirrors the test_ai_client.py pattern: drive the client through pytest-httpx
mocks and verify the verb-level contract (URL derivation, request envelope,
the "duplicate name -> 500" normalization to ALREADY_EXISTS).
"""

from __future__ import annotations

import json

import pytest

from keboola_agent_cli.constants import MAX_RETRIES
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.metastore_client import (
    SEMANTIC_TYPES,
    MetastoreClient,
)

STACK_URL_US = "https://connection.keboola.com"
METASTORE_URL_US = "https://metastore.keboola.com"
TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable retry-backoff sleeps so the suite stays fast."""
    import keboola_agent_cli.http_base as http_base_module

    monkeypatch.setattr(http_base_module.time, "sleep", lambda _x: None)


class TestUrlDerivation:
    """Verify MetastoreClient maps ``connection.<host>`` to ``metastore.<host>``."""

    def test_us_stack(self) -> None:
        result = MetastoreClient._derive_service_url("https://connection.keboola.com", "metastore")
        assert result == "https://metastore.keboola.com"

    def test_eu_gcp_stack(self) -> None:
        result = MetastoreClient._derive_service_url(
            "https://connection.europe-west3.gcp.keboola.com", "metastore"
        )
        assert result == "https://metastore.europe-west3.gcp.keboola.com"

    def test_aws_stack(self) -> None:
        result = MetastoreClient._derive_service_url(
            "https://connection.eu-west-1.aws.keboola.com", "metastore"
        )
        assert result == "https://metastore.eu-west-1.aws.keboola.com"

    def test_azure_stack(self) -> None:
        result = MetastoreClient._derive_service_url(
            "https://connection.westeurope.azure.keboola.com", "metastore"
        )
        assert result == "https://metastore.westeurope.azure.keboola.com"


class TestAuthHeader:
    """Verify X-StorageApi-Token is sent on every request."""

    def test_token_header_set_on_get(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{METASTORE_URL_US}/api/v1/repository/semantic-model",
            json={"data": []},
            status_code=200,
        )
        client = MetastoreClient(stack_url=STACK_URL_US, token=TOKEN)
        try:
            client.list_items("semantic-model")
        finally:
            client.close()
        request = httpx_mock.get_requests()[0]
        assert request.headers["X-StorageApi-Token"] == TOKEN
        assert "keboola-agent-cli/" in request.headers["User-Agent"]


class TestListItems:
    """list_items returns raw item shapes and supports model_uuid filtering."""

    def test_list_items_returns_data_array(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{METASTORE_URL_US}/api/v1/repository/semantic-dataset",
            json={
                "data": [
                    {"type": "semantic-dataset", "id": "a1", "attributes": {"name": "x"}},
                    {"type": "semantic-dataset", "id": "a2", "attributes": {"name": "y"}},
                ]
            },
            status_code=200,
        )
        client = MetastoreClient(stack_url=STACK_URL_US, token=TOKEN)
        try:
            items = client.list_items("semantic-dataset")
        finally:
            client.close()
        assert len(items) == 2
        assert items[0]["id"] == "a1"

    def test_list_items_filter_by_model_uuid(self, httpx_mock) -> None:
        """Client-side filter on attributes.modelUUID."""
        httpx_mock.add_response(
            url=f"{METASTORE_URL_US}/api/v1/repository/semantic-metric",
            json={
                "data": [
                    {"id": "m1", "attributes": {"name": "a", "modelUUID": "U1"}},
                    {"id": "m2", "attributes": {"name": "b", "modelUUID": "U2"}},
                    {"id": "m3", "attributes": {"name": "c", "modelUUID": "U1"}},
                ]
            },
            status_code=200,
        )
        client = MetastoreClient(stack_url=STACK_URL_US, token=TOKEN)
        try:
            items = client.list_items("semantic-metric", model_uuid="U1")
        finally:
            client.close()
        assert {i["id"] for i in items} == {"m1", "m3"}


class TestPostItem:
    """post_item must wrap payload in the {name, data, branch, schemaVersion, scope} envelope."""

    def test_post_envelope(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{METASTORE_URL_US}/api/v1/repository/semantic-metric",
            json={
                "data": {
                    "type": "semantic-metric",
                    "id": "new-id",
                    "attributes": {"name": "rev", "sql": "SUM(x)"},
                }
            },
            status_code=201,
        )
        client = MetastoreClient(stack_url=STACK_URL_US, token=TOKEN)
        try:
            stored = client.post_item(
                "semantic-metric",
                name="rev",
                data={"name": "rev", "sql": "SUM(x)", "modelUUID": "u"},
            )
        finally:
            client.close()
        assert stored["id"] == "new-id"

        request = httpx_mock.get_requests()[0]
        body = json.loads(request.content)
        assert body["name"] == "rev"
        assert body["branch"] == "main"
        assert body["schemaVersion"] == "1.0.0"
        assert body["scope"] == "project"
        assert body["data"]["sql"] == "SUM(x)"
        assert body["data"]["modelUUID"] == "u"


@pytest.fixture
def metastore_client():
    """Open a `MetastoreClient` and close it after the test."""
    client = MetastoreClient(stack_url=STACK_URL_US, token=TOKEN)
    try:
        yield client
    finally:
        client.close()


class TestDuplicateNameNormalization:
    """Server returns 409 (post-fix) or 500 (legacy) for duplicate names;
    client normalizes both into ALREADY_EXISTS."""

    def test_duplicate_name_409_becomes_already_exists(self, httpx_mock, metastore_client) -> None:
        """Post go-monorepo PR #513 the metastore returns a proper 409 Conflict.

        409 is not in ``RETRYABLE_STATUS_CODES`` so only a single response is
        registered -- the client must not retry before normalising.
        """
        httpx_mock.add_response(
            url=f"{METASTORE_URL_US}/api/v1/repository/semantic-metric",
            status_code=409,
            json={"error": "Object with this name already exists in this project"},
        )
        with pytest.raises(KeboolaApiError) as excinfo:
            metastore_client.post_item("semantic-metric", name="foo", data={"name": "foo"})
        assert excinfo.value.error_code == ErrorCode.ALREADY_EXISTS
        assert excinfo.value.status_code == 409
        assert "already exists" in excinfo.value.message
        assert "foo" in excinfo.value.message
        assert excinfo.value.retryable is False

    def test_duplicate_name_500_becomes_already_exists(self, httpx_mock, metastore_client) -> None:
        """Legacy / pre-fix metastore still returns 500 -- retain the workaround."""
        for _ in range(MAX_RETRIES):
            httpx_mock.add_response(
                url=f"{METASTORE_URL_US}/api/v1/repository/semantic-metric",
                status_code=500,
                json={"error": "Failed to create meta object: duplicate name 'foo'"},
            )
        with pytest.raises(KeboolaApiError) as excinfo:
            metastore_client.post_item("semantic-metric", name="foo", data={"name": "foo"})
        assert excinfo.value.error_code == ErrorCode.ALREADY_EXISTS
        assert excinfo.value.status_code == 500
        assert "already exists" in excinfo.value.message
        assert "foo" in excinfo.value.message
        assert excinfo.value.retryable is False

    def test_unrelated_500_passes_through(self, httpx_mock, metastore_client) -> None:
        """A 500 without the magic phrase keeps its API_ERROR code."""
        for _ in range(MAX_RETRIES):
            httpx_mock.add_response(
                url=f"{METASTORE_URL_US}/api/v1/repository/semantic-metric",
                status_code=500,
                json={"error": "some unrelated internal error"},
            )
        with pytest.raises(KeboolaApiError) as excinfo:
            metastore_client.post_item("semantic-metric", name="foo", data={"name": "foo"})
        assert excinfo.value.error_code != ErrorCode.ALREADY_EXISTS


class TestDeleteItem:
    """delete_item returns silently on 204 and raises NOT_FOUND on 404."""

    def test_delete_204(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{METASTORE_URL_US}/api/v1/repository/semantic-dataset/abc",
            status_code=204,
        )
        client = MetastoreClient(stack_url=STACK_URL_US, token=TOKEN)
        try:
            assert client.delete_item("semantic-dataset", "abc") is None
        finally:
            client.close()

    def test_delete_404(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{METASTORE_URL_US}/api/v1/repository/semantic-dataset/missing",
            status_code=404,
            json={"error": "not found"},
        )
        client = MetastoreClient(stack_url=STACK_URL_US, token=TOKEN)
        try:
            with pytest.raises(KeboolaApiError) as excinfo:
                client.delete_item("semantic-dataset", "missing")
        finally:
            client.close()
        assert excinfo.value.error_code == ErrorCode.NOT_FOUND


class TestSemanticTypes:
    """Sanity-check the SEMANTIC_TYPES tuple has the six expected slugs."""

    def test_semantic_types_complete(self) -> None:
        assert set(SEMANTIC_TYPES) == {
            "semantic-model",
            "semantic-dataset",
            "semantic-metric",
            "semantic-relationship",
            "semantic-constraint",
            "semantic-glossary",
        }
