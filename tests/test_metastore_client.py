"""Tests for MetastoreClient -- URL derivation, envelope shape, error normalization.

Mirrors the test_ai_client.py pattern: drive the client through pytest-httpx
mocks and verify the verb-level contract (URL derivation, request envelope,
the "duplicate name -> 500" normalization to ALREADY_EXISTS).
"""

from __future__ import annotations

import json
from typing import ClassVar

import pytest

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
        assert "keboola-cli/" in request.headers["User-Agent"]


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
        assert body["schemaVersion"] == "1.1.0"
        assert body["scope"] == "project"
        assert body["data"]["sql"] == "SUM(x)"
        assert body["data"]["modelUUID"] == "u"
        assert "targetProjectIds" not in body


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
        """Legacy / pre-fix metastore still returns 500 -- retain the workaround.

        A single response: ``post_item`` is a POST, which is no longer retried
        on a 5xx (issue #599), so normalisation has to happen on attempt one.
        """
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


class TestPutItem:
    """put_item wraps the same envelope as post_item but targets PUT /{type}/{id}."""

    def test_put_envelope_and_url(self, httpx_mock) -> None:
        httpx_mock.add_response(
            method="PUT",
            url=f"{METASTORE_URL_US}/api/v1/repository/semantic-reference-data/rec-1",
            json={
                "data": {
                    "type": "semantic-reference-data",
                    "id": "rec-1",
                    "attributes": {"dimensionName": "chart_of_accounts"},
                    "meta": {"revision": 2},
                }
            },
            status_code=200,
        )
        client = MetastoreClient(stack_url=STACK_URL_US, token=TOKEN)
        try:
            stored = client.put_item(
                "semantic-reference-data",
                "rec-1",
                name="chart_of_accounts",
                data={"modelUUID": "u", "dimensionName": "chart_of_accounts", "members": []},
            )
        finally:
            client.close()
        assert stored["id"] == "rec-1"
        assert stored["meta"]["revision"] == 2

        request = httpx_mock.get_requests()[0]
        assert request.method == "PUT"
        body = json.loads(request.content)
        assert body["name"] == "chart_of_accounts"
        assert body["branch"] == "main"
        assert body["schemaVersion"] == "1.1.0"
        assert "scope" not in body
        assert body["data"]["dimensionName"] == "chart_of_accounts"
        assert body["data"]["members"] == []

    def test_put_404(self, httpx_mock) -> None:
        httpx_mock.add_response(
            method="PUT",
            url=f"{METASTORE_URL_US}/api/v1/repository/semantic-reference-data/missing",
            status_code=404,
            json={"error": "not found"},
        )
        client = MetastoreClient(stack_url=STACK_URL_US, token=TOKEN)
        try:
            with pytest.raises(KeboolaApiError) as excinfo:
                client.put_item(
                    "semantic-reference-data", "missing", name="d", data={"members": []}
                )
        finally:
            client.close()
        assert excinfo.value.error_code == ErrorCode.NOT_FOUND


class TestSemanticTypes:
    """Sanity-check the SEMANTIC_TYPES tuple has the expected slugs."""

    def test_semantic_types_complete(self) -> None:
        assert set(SEMANTIC_TYPES) == {
            "semantic-model",
            "semantic-dataset",
            "semantic-metric",
            "semantic-relationship",
            "semantic-constraint",
            "semantic-glossary",
            "semantic-reference-data",
        }


class TestProjectScope401Reclassification:
    """The metastore's project-admin gate 401 becomes MISSING_MASTER_TOKEN.

    Pre-PSGO-282, the metastore auth middleware collapsed every project-scope
    resolution failure into a 401 ``"Failed to create project scope"``,
    including plain reads from a valid non-master token (issue #711). Since
    PSGO-282 (go-monorepo#596) a fixed metastore only emits this 401 for a
    WRITE against a token that is not a project admin -- reads succeed for
    any valid token. The client still funnels every verb through the
    reclassification as a safety net for a not-yet-upgraded deployment.
    """

    _SCOPE_401_BODY: ClassVar[dict] = {
        "error": 401,
        "code": "401",
        "exception": "Failed to create project scope",
        "exceptionId": "metastore-fbfeCiBSXXBLk7D",
        "status": "error",
    }

    def test_scope_401_becomes_missing_master_token(self, httpx_mock, metastore_client) -> None:
        """The reported case: GET on a repository endpoint with a non-master token."""
        httpx_mock.add_response(
            url=f"{METASTORE_URL_US}/api/v1/repository/semantic-model",
            status_code=401,
            json=self._SCOPE_401_BODY,
        )
        with pytest.raises(KeboolaApiError) as excinfo:
            metastore_client.list_items("semantic-model")
        exc = excinfo.value
        assert exc.error_code == ErrorCode.MISSING_MASTER_TOKEN
        assert exc.status_code == 401
        assert exc.retryable is False
        # The message must carry the actual remedy, not a support escalation.
        assert "master" in exc.message.lower()
        assert "Failed to create project scope" in exc.message
        # Neither wrong diagnosis may survive.
        assert "Invalid or expired token" not in exc.message
        assert "escalate" not in exc.message.lower()

    def test_scope_401_keeps_the_exception_id(self, httpx_mock, metastore_client) -> None:
        """The support trace handle survives the reclassification."""
        httpx_mock.add_response(
            url=f"{METASTORE_URL_US}/api/v1/repository/semantic-model",
            status_code=401,
            json=self._SCOPE_401_BODY,
        )
        with pytest.raises(KeboolaApiError) as excinfo:
            metastore_client.list_items("semantic-model")
        assert "[exceptionId: metastore-fbfeCiBSXXBLk7D]" in excinfo.value.message

    def test_scope_401_masks_the_token(self, httpx_mock, metastore_client) -> None:
        httpx_mock.add_response(
            url=f"{METASTORE_URL_US}/api/v1/repository/semantic-model",
            status_code=401,
            json=self._SCOPE_401_BODY,
        )
        with pytest.raises(KeboolaApiError) as excinfo:
            metastore_client.list_items("semantic-model")
        assert TOKEN not in excinfo.value.message

    def test_scope_401_without_exception_id_has_no_suffix(
        self, httpx_mock, metastore_client
    ) -> None:
        httpx_mock.add_response(
            url=f"{METASTORE_URL_US}/api/v1/repository/semantic-model",
            status_code=401,
            json={"exception": "Failed to create project scope"},
        )
        with pytest.raises(KeboolaApiError) as excinfo:
            metastore_client.list_items("semantic-model")
        exc = excinfo.value
        assert exc.error_code == ErrorCode.MISSING_MASTER_TOKEN
        assert "exceptionId" not in exc.message

    def test_other_metastore_401_stays_on_the_generic_mapping(
        self, httpx_mock, metastore_client
    ) -> None:
        """A 401 that does not carry the scope phrase is not reclassified.

        ``"Token is disabled"`` mentions the token, so the base mapping keeps
        it INVALID_TOKEN -- the metastore override must not touch it.
        """
        httpx_mock.add_response(
            url=f"{METASTORE_URL_US}/api/v1/repository/semantic-model",
            status_code=401,
            json={"exception": "Token is disabled"},
        )
        with pytest.raises(KeboolaApiError) as excinfo:
            metastore_client.list_items("semantic-model")
        assert excinfo.value.error_code == ErrorCode.INVALID_TOKEN

    def test_scope_401_reclassified_on_writes_too(self, httpx_mock, metastore_client) -> None:
        """POST goes through the same funnel as GET (no per-verb gaps)."""
        httpx_mock.add_response(
            url=f"{METASTORE_URL_US}/api/v1/repository/semantic-metric",
            status_code=401,
            json=self._SCOPE_401_BODY,
        )
        with pytest.raises(KeboolaApiError) as excinfo:
            metastore_client.post_item("semantic-metric", name="foo", data={"name": "foo"})
        assert excinfo.value.error_code == ErrorCode.MISSING_MASTER_TOKEN


class TestPostItemScopeValidation:
    """post_item validates scope/target_project_ids client-side (PSGO-140)."""

    def test_rejects_unknown_scope(self, metastore_client) -> None:
        with pytest.raises(KeboolaApiError) as excinfo:
            metastore_client.post_item("semantic-metric", name="x", data={}, scope="bogus")
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_rejects_target_project_ids_without_targeted_scope(self, metastore_client) -> None:
        with pytest.raises(KeboolaApiError) as excinfo:
            metastore_client.post_item(
                "semantic-metric", name="x", data={}, scope="project", target_project_ids=[1]
            )
        assert excinfo.value.error_code == ErrorCode.VALIDATION_ERROR

    def test_targeted_scope_sends_target_project_ids(self, httpx_mock, metastore_client) -> None:
        httpx_mock.add_response(
            url=f"{METASTORE_URL_US}/api/v1/repository/semantic-metric",
            json={"data": {"type": "semantic-metric", "id": "new-id", "attributes": {}}},
            status_code=201,
        )
        metastore_client.post_item(
            "semantic-metric",
            name="x",
            data={},
            scope="targeted",
            target_project_ids=[123, 456],
        )
        body = json.loads(httpx_mock.get_requests()[0].content)
        assert body["scope"] == "targeted"
        assert body["targetProjectIds"] == [123, 456]

    def test_organization_scope_omits_target_project_ids_key(
        self, httpx_mock, metastore_client
    ) -> None:
        httpx_mock.add_response(
            url=f"{METASTORE_URL_US}/api/v1/repository/semantic-metric",
            json={"data": {"type": "semantic-metric", "id": "new-id", "attributes": {}}},
            status_code=201,
        )
        metastore_client.post_item("semantic-metric", name="x", data={}, scope="organization")
        body = json.loads(httpx_mock.get_requests()[0].content)
        assert body["scope"] == "organization"
        assert "targetProjectIds" not in body


class TestElevateToOrganization:
    """PATCH /{type}/{id} with {"scope": "organization"} only."""

    def test_sends_scope_only_patch(self, httpx_mock, metastore_client) -> None:
        httpx_mock.add_response(
            method="PATCH",
            url=f"{METASTORE_URL_US}/api/v1/repository/semantic-dataset/abc",
            json={
                "data": {
                    "type": "semantic-dataset",
                    "id": "abc",
                    "attributes": {},
                    "meta": {"scope": "organization"},
                }
            },
            status_code=200,
        )
        result = metastore_client.elevate_to_organization("semantic-dataset", "abc")
        assert result["meta"]["scope"] == "organization"
        request = httpx_mock.get_requests()[0]
        assert request.method == "PATCH"
        assert json.loads(request.content) == {"scope": "organization"}

    def test_403_maps_to_access_denied(self, httpx_mock, metastore_client) -> None:
        httpx_mock.add_response(
            method="PATCH",
            url=f"{METASTORE_URL_US}/api/v1/repository/semantic-dataset/abc",
            status_code=403,
            json={"error": "Insufficient permissions"},
        )
        with pytest.raises(KeboolaApiError) as excinfo:
            metastore_client.elevate_to_organization("semantic-dataset", "abc")
        assert excinfo.value.error_code == ErrorCode.ACCESS_DENIED


class TestPutTargetProjects:
    """PUT /{type}/{id}/target-projects replaces the whole grant set; 204 no body."""

    def test_replaces_target_projects(self, httpx_mock, metastore_client) -> None:
        httpx_mock.add_response(
            method="PUT",
            url=f"{METASTORE_URL_US}/api/v1/repository/semantic-dataset/abc/target-projects",
            status_code=204,
        )
        assert metastore_client.put_target_projects("semantic-dataset", "abc", [1, 2]) is None
        request = httpx_mock.get_requests()[0]
        assert json.loads(request.content) == {"targetProjectIds": [1, 2]}

    def test_clears_with_empty_list(self, httpx_mock, metastore_client) -> None:
        httpx_mock.add_response(
            method="PUT",
            url=f"{METASTORE_URL_US}/api/v1/repository/semantic-dataset/abc/target-projects",
            status_code=204,
        )
        metastore_client.put_target_projects("semantic-dataset", "abc", [])
        request = httpx_mock.get_requests()[0]
        assert json.loads(request.content) == {"targetProjectIds": []}


class TestScopeElevationRequest:
    """PUT/DELETE .../scope-elevation-request: empty body, 200 with the updated item."""

    def test_request_elevation(self, httpx_mock, metastore_client) -> None:
        httpx_mock.add_response(
            method="PUT",
            url=f"{METASTORE_URL_US}/api/v1/repository/semantic-dataset/abc/scope-elevation-request",
            json={
                "data": {
                    "type": "semantic-dataset",
                    "id": "abc",
                    "attributes": {},
                    "meta": {"scopeElevationRequestedAt": "2026-08-28T00:00:00Z"},
                }
            },
            status_code=200,
        )
        result = metastore_client.request_scope_elevation("semantic-dataset", "abc")
        assert result["meta"]["scopeElevationRequestedAt"]

    def test_withdraw_elevation(self, httpx_mock, metastore_client) -> None:
        httpx_mock.add_response(
            method="DELETE",
            url=f"{METASTORE_URL_US}/api/v1/repository/semantic-dataset/abc/scope-elevation-request",
            json={"data": {"type": "semantic-dataset", "id": "abc", "attributes": {}, "meta": {}}},
            status_code=200,
        )
        result = metastore_client.withdraw_scope_elevation("semantic-dataset", "abc")
        assert result["id"] == "abc"


class TestListOrganizationItems:
    """GET /{type}/organization with the generic filter/limit/offset query language."""

    def test_pending_elevation_filter_and_pagination(self, httpx_mock, metastore_client) -> None:
        httpx_mock.add_response(
            url=(
                f"{METASTORE_URL_US}/api/v1/repository/semantic-dataset/organization"
                "?scope_elevation_requested_at%5Bnot%5D%5Bnull%5D=true&limit=5&offset=10"
            ),
            json={"data": [{"type": "semantic-dataset", "id": "a", "attributes": {}}]},
            status_code=200,
        )
        result = metastore_client.list_organization_items(
            "semantic-dataset", pending_elevation_only=True, limit=5, offset=10
        )
        assert len(result) == 1
        assert result[0]["id"] == "a"

    def test_no_filters_sends_bare_request(self, httpx_mock, metastore_client) -> None:
        httpx_mock.add_response(
            url=f"{METASTORE_URL_US}/api/v1/repository/semantic-model/organization",
            json={"data": []},
            status_code=200,
        )
        assert metastore_client.list_organization_items("semantic-model") == []
