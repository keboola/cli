"""Tests for the billing (PAYG credits) L3 client -- issue #594.

Covers:
- Host derivation: connection.<region> -> billing.<region>, mirroring the
  sync-actions / stream sibling-host pattern (test_component_sync_action.py,
  test_stream_client.py).
- `KeboolaClient.get_credits()` issuing a GET to the derived billing host and
  returning the parsed JSON verbatim.
- The verbatim live payload from the billing contract round-tripping through
  `ProjectCredits`, including `stats.workspaceJobs` arriving as a LIST (the
  live API's actual shape, which diverges from the public docs' object shape).
- `ProjectCredits` tolerance: missing `stats`, `stats.workspaceJobs` absent,
  and unknown extra top-level keys must never raise.
- A regression guard on the money rule: `POST /credits` triggers a REAL
  automatic top-up, so the mixin must expose no public method that could
  issue a POST to the billing host.
"""

from __future__ import annotations

import inspect

import pytest

from keboola_agent_cli.client import KeboolaClient
from keboola_agent_cli.client._core import _CoreClient
from keboola_agent_cli.client.billing import _BillingMixin
from keboola_agent_cli.models import (
    CreditStats,
    ProjectCredits,
    WorkspaceJobCredits,
)

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
STACK_URL = "https://connection.north-europe.azure.keboola.com"
BILLING_URL = "https://billing.north-europe.azure.keboola.com"

# Verbatim live payload from the billing contract (north-europe.azure).
LIVE_PAYLOAD = {
    "consumed": 100.5,
    "stats": {
        "componentJobs": {"consumed": 95.25},
        "workspaceJobs": [
            {"workspaceType": "sandbox-sql", "warehouseSize": "small", "consumed": 5.0},
            {"workspaceType": "writer", "warehouseSize": "small", "consumed": 0.25},
        ],
    },
    "remaining": 25.5,
}


class TestDeriveBillingUrl:
    """The control-plane base URL is connection.<region> -> billing.<region>."""

    def test_north_europe_azure_stack(self) -> None:
        assert KeboolaClient._derive_service_url(STACK_URL, "billing") == BILLING_URL

    def test_us_stack(self) -> None:
        assert (
            KeboolaClient._derive_service_url("https://connection.keboola.com", "billing")
            == "https://billing.keboola.com"
        )

    def test_gcp_stack(self) -> None:
        assert (
            KeboolaClient._derive_service_url(
                "https://connection.us-east4.gcp.keboola.com", "billing"
            )
            == "https://billing.us-east4.gcp.keboola.com"
        )


class TestGetCredits:
    def test_get_credits_issues_get_to_billing_host(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{BILLING_URL}/credits",
            method="GET",
            json=LIVE_PAYLOAD,
            status_code=200,
        )

        with KeboolaClient(stack_url=STACK_URL, token=TEST_TOKEN) as client:
            result = client.get_credits()

        assert result == LIVE_PAYLOAD
        requests = httpx_mock.get_requests()
        assert len(requests) == 1
        assert requests[0].method == "GET"
        assert str(requests[0].url) == f"{BILLING_URL}/credits"
        # Sub-client inherits the Storage API token header from the main client.
        assert requests[0].headers["X-StorageApi-Token"] == TEST_TOKEN

    def test_get_credits_returns_raw_json_verbatim(self, httpx_mock) -> None:
        """No shaping happens in the client layer -- the dict is returned as-is."""
        payload = {"consumed": 1.0, "remaining": 2.0, "unknownField": "passthrough"}
        httpx_mock.add_response(url=f"{BILLING_URL}/credits", method="GET", json=payload)

        with KeboolaClient(stack_url=STACK_URL, token=TEST_TOKEN) as client:
            result = client.get_credits()

        assert result == payload


class TestProjectCreditsParsing:
    """`models.ProjectCredits` must tolerate the live API's real shape."""

    def test_parses_verbatim_live_payload(self) -> None:
        credits = ProjectCredits.model_validate(LIVE_PAYLOAD)

        assert credits.consumed == 100.5
        assert credits.remaining == 25.5
        assert credits.stats is not None
        assert isinstance(credits.stats, CreditStats)
        assert credits.stats.component_jobs is not None
        assert credits.stats.component_jobs.consumed == 95.25

        # workspaceJobs is a LIST of two entries in the live payload -- the
        # public docs show an object, but the tolerant model parses the array.
        assert isinstance(credits.stats.workspace_jobs, list)
        assert len(credits.stats.workspace_jobs) == 2
        first, second = credits.stats.workspace_jobs
        assert isinstance(first, WorkspaceJobCredits)
        assert first.workspace_type == "sandbox-sql"
        assert first.warehouse_size == "small"
        assert first.consumed == 5.0
        assert second.workspace_type == "writer"
        assert second.consumed == 0.25

    def test_tolerates_missing_stats(self) -> None:
        credits = ProjectCredits.model_validate({"consumed": 10.0, "remaining": 5.0})
        assert credits.stats is None
        assert credits.consumed == 10.0
        assert credits.remaining == 5.0

    def test_tolerates_stats_without_workspace_jobs(self) -> None:
        credits = ProjectCredits.model_validate(
            {
                "consumed": 10.0,
                "remaining": 5.0,
                "stats": {"componentJobs": {"consumed": 10.0}},
            }
        )
        assert credits.stats is not None
        assert credits.stats.workspace_jobs == []
        assert credits.stats.component_jobs is not None
        assert credits.stats.component_jobs.consumed == 10.0

    def test_tolerates_stats_none(self) -> None:
        credits = ProjectCredits.model_validate({"consumed": 10.0, "remaining": 5.0, "stats": None})
        assert credits.stats is None

    def test_tolerates_unknown_extra_top_level_keys(self) -> None:
        credits = ProjectCredits.model_validate(
            {
                "consumed": 10.0,
                "remaining": 5.0,
                "purchased": 15.0,
                "currency": "EUR",
                "someFutureField": {"nested": True},
            }
        )
        assert credits.consumed == 10.0
        assert credits.remaining == 5.0

    def test_tolerates_completely_empty_payload(self) -> None:
        credits = ProjectCredits.model_validate({})
        assert credits.consumed == 0.0
        assert credits.remaining == 0.0
        assert credits.stats is None

    def test_tolerates_unknown_extra_keys_on_nested_models(self) -> None:
        credits = ProjectCredits.model_validate(
            {
                "consumed": 1.0,
                "remaining": 2.0,
                "stats": {
                    "componentJobs": {"consumed": 1.0, "extraField": "x"},
                    "workspaceJobs": [
                        {
                            "workspaceType": "sandbox-sql",
                            "warehouseSize": "small",
                            "consumed": 1.0,
                            "extraField": "y",
                        }
                    ],
                    "extraStatsField": "z",
                },
            }
        )
        assert credits.stats is not None
        assert credits.stats.workspace_jobs[0].workspace_type == "sandbox-sql"


class TestNoTopUpHelper:
    """Regression guard: POST /credits triggers a REAL automatic top-up.

    `_BillingMixin` must expose exactly one public method (`get_credits`) and
    must never issue a POST request anywhere in its own source -- both are
    checked so an unnoticed future addition of a "top up" / "purchase" helper
    fails this test loudly.
    """

    def test_only_get_credits_is_defined_on_the_mixin(self) -> None:
        own_public_methods = [
            name
            for name, value in vars(_BillingMixin).items()
            if not name.startswith("_") and callable(value)
        ]
        assert own_public_methods == ["get_credits"]

    def test_billing_module_source_never_issues_a_post(self) -> None:
        import keboola_agent_cli.client.billing as billing_module

        source = inspect.getsource(billing_module)
        assert '"POST"' not in source
        assert "'POST'" not in source

    def test_get_credits_never_sends_a_post_request(self, httpx_mock) -> None:
        httpx_mock.add_response(url=f"{BILLING_URL}/credits", method="GET", json=LIVE_PAYLOAD)

        with KeboolaClient(stack_url=STACK_URL, token=TEST_TOKEN) as client:
            client.get_credits()

        for request in httpx_mock.get_requests():
            assert request.method != "POST"

    def test_billing_dispatcher_takes_no_http_method(self) -> None:
        """The signature itself, not just this module's source, forbids a POST.

        The source-scan above only covers `client/billing.py`. A future caller
        elsewhere could reach the dispatcher directly, so the dispatcher takes
        no `method` argument at all -- unlike its `_queue_request` /
        `_sync_actions_request` siblings. Pinned here because reintroducing a
        `method` parameter would silently reopen the real-money path.
        """
        assert not hasattr(_CoreClient, "_billing_request"), (
            "_billing_request is back: a caller can now pass method='POST' to the "
            "billing host, which triggers a real-money automatic top-up."
        )
        params = list(inspect.signature(_CoreClient._billing_get).parameters)
        assert params == ["self", "path", "kwargs"], (
            f"_billing_get must stay (self, path, **kwargs) with the verb hardcoded; got {params}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
