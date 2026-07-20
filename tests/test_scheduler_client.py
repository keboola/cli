"""Tests for SchedulerClient -- URL derivation, request envelopes, error mapping.

Mirrors the test_metastore_client.py pattern: drive the client through
pytest-httpx mocks and verify the verb-level contract (URL derivation,
POST /schedules body, DELETE /configurations/{id} path, auth header,
error normalization inherited from BaseHttpClient).
"""

from __future__ import annotations

import json

import pytest

from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.scheduler_client import SchedulerClient

STACK_URL_US = "https://connection.keboola.com"
SCHEDULER_URL_US = "https://scheduler.keboola.com"
TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable retry-backoff sleeps so the suite stays fast."""
    import keboola_agent_cli.http_base as http_base_module

    monkeypatch.setattr(http_base_module.time, "sleep", lambda _x: None)


class TestUrlDerivation:
    """Verify SchedulerClient maps ``connection.<host>`` to ``scheduler.<host>``."""

    def test_us_stack(self) -> None:
        result = SchedulerClient._derive_service_url("https://connection.keboola.com", "scheduler")
        assert result == "https://scheduler.keboola.com"

    def test_eu_gcp_stack(self) -> None:
        result = SchedulerClient._derive_service_url(
            "https://connection.europe-west3.gcp.keboola.com", "scheduler"
        )
        assert result == "https://scheduler.europe-west3.gcp.keboola.com"


class TestActivateSchedule:
    """activate_schedule posts the scheduler config id to POST /schedules."""

    def test_posts_configuration_id(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{SCHEDULER_URL_US}/schedules",
            method="POST",
            json={"id": "1", "configurationId": "77"},
            status_code=201,
        )
        with SchedulerClient(stack_url=STACK_URL_US, token=TOKEN) as client:
            result = client.activate_schedule("77")
        request = httpx_mock.get_requests()[0]
        assert json.loads(request.content) == {"configurationId": "77"}
        assert request.headers["X-StorageApi-Token"] == TOKEN
        assert result["configurationId"] == "77"

    def test_forbidden_maps_to_access_denied(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{SCHEDULER_URL_US}/schedules",
            method="POST",
            json={"error": "Insufficient permissions"},
            status_code=403,
        )
        with (
            SchedulerClient(stack_url=STACK_URL_US, token=TOKEN) as client,
            pytest.raises(KeboolaApiError) as excinfo,
        ):
            client.activate_schedule("77")
        assert excinfo.value.error_code == ErrorCode.ACCESS_DENIED
        assert excinfo.value.status_code == 403


class TestRemoveSchedule:
    """remove_schedule deregisters via DELETE /configurations/{id}."""

    def test_deletes_configuration(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{SCHEDULER_URL_US}/configurations/77",
            method="DELETE",
            status_code=204,
        )
        with SchedulerClient(stack_url=STACK_URL_US, token=TOKEN) as client:
            client.remove_schedule("77")
        request = httpx_mock.get_requests()[0]
        assert request.method == "DELETE"
        assert request.headers["X-StorageApi-Token"] == TOKEN

    def test_missing_registration_maps_to_not_found(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{SCHEDULER_URL_US}/configurations/77",
            method="DELETE",
            json={"error": "Schedule not found"},
            status_code=404,
        )
        with (
            SchedulerClient(stack_url=STACK_URL_US, token=TOKEN) as client,
            pytest.raises(KeboolaApiError) as excinfo,
        ):
            client.remove_schedule("77")
        assert excinfo.value.error_code == ErrorCode.NOT_FOUND
