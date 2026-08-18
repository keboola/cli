"""Unit tests for BillingService.

Tests the business logic in isolation using mocked KeboolaClient instances.
Covers:

- ``get_credits`` happy path against the verbatim live payload from issue #594.
- The PAYG feature gate short-circuiting before any billing call.
- Mixed multi-project fan-out (success + non-PAYG + API error).
- ``ConfigError`` propagation for an unknown alias.
- Deterministic ordering of both ``credits`` and ``errors``.
- Tolerant parsing of a payload missing ``stats`` entirely.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.errors import ConfigError, ErrorCode, KeboolaApiError
from keboola_agent_cli.services.billing_service import BillingService

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

# Fake tokens follow the repo convention: "901-<role>-<value>".
_TOKEN_A = "901-storage-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_TOKEN_B = "901-storage-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
_TOKEN_C = "901-storage-cccccccccccccccccccccccccccccccc"

# The exact live payload from the billing contract (issue #594).
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


def _mock_config_store(projects: dict) -> MagicMock:
    """Build a config-store double mirroring test_schedule_service.py's helper.

    Each ``projects`` value is a dict with ``url``, ``token``, and optionally
    ``project_id`` -- explicitly set (rather than left as an auto-generated
    ``MagicMock``) so row assertions against ``project_id`` are meaningful.
    """
    cs = MagicMock()
    config = MagicMock()
    config.projects = {
        alias: MagicMock(
            stack_url=v["url"],
            token=v["token"],
            active_branch_id=None,
            project_id=v.get("project_id"),
        )
        for alias, v in projects.items()
    }
    config.max_parallel_workers = 10
    cs.load.return_value = config
    cs.get_project.side_effect = lambda alias: config.projects.get(alias)
    return cs


def _make_service(mock_client: MagicMock, projects: dict | None = None) -> BillingService:
    if projects is None:
        projects = {
            "prod": {"url": "https://connection.keboola.com", "token": _TOKEN_A, "project_id": 123}
        }
    cs = _mock_config_store(projects)
    return BillingService(config_store=cs, client_factory=lambda url, tok: mock_client)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestGetCreditsHappyPath:
    def test_live_payload_produces_exact_row(self) -> None:
        client = MagicMock()
        client.has_feature.return_value = True
        client.get_credits.return_value = LIVE_PAYLOAD

        service = _make_service(client)
        result = service.get_credits(aliases=["prod"])

        assert result["errors"] == []
        assert len(result["credits"]) == 1
        row = result["credits"][0]

        assert row["project_alias"] == "prod"
        assert row["project_id"] == 123
        assert row["consumed"] == 100.5
        assert row["remaining"] == 25.5
        assert row["total"] == 126.0  # 100.5 + 25.5
        assert row["consumed_minutes"] == 100.5 * 60
        assert row["remaining_minutes"] == 25.5 * 60
        assert row["component_jobs_consumed"] == 95.25
        assert row["workspace_jobs"] == [
            {"workspace_type": "sandbox-sql", "warehouse_size": "small", "consumed": 5.0},
            {"workspace_type": "writer", "warehouse_size": "small", "consumed": 0.25},
        ]

    def test_feature_gate_checked_before_get_credits(self) -> None:
        """has_feature must be called, and checked, before get_credits fires."""
        client = MagicMock()
        client.has_feature.return_value = True
        client.get_credits.return_value = LIVE_PAYLOAD

        service = _make_service(client)
        service.get_credits(aliases=["prod"])

        client.has_feature.assert_called_once_with("pay-as-you-go")
        client.get_credits.assert_called_once()

    def test_client_closed_on_success(self) -> None:
        client = MagicMock()
        client.has_feature.return_value = True
        client.get_credits.return_value = LIVE_PAYLOAD

        service = _make_service(client)
        service.get_credits(aliases=["prod"])

        client.close.assert_called_once()


# ---------------------------------------------------------------------------
# Non-PAYG feature gate
# ---------------------------------------------------------------------------


class TestPaygFeatureGate:
    def test_missing_feature_short_circuits(self) -> None:
        client = MagicMock()
        client.has_feature.return_value = False

        service = _make_service(client)
        result = service.get_credits(aliases=["prod"])

        assert result["credits"] == []
        assert len(result["errors"]) == 1
        entry = result["errors"][0]
        assert entry["project_alias"] == "prod"
        assert entry["error_code"] == str(ErrorCode.PAYG_NOT_AVAILABLE)
        assert "pay-as-you-go" in entry["message"]

    def test_get_credits_never_called_when_gate_fails(self) -> None:
        """The whole point of the gate: never touch the (possibly NXDOMAIN) billing host."""
        client = MagicMock()
        client.has_feature.return_value = False

        service = _make_service(client)
        service.get_credits(aliases=["prod"])

        client.get_credits.assert_not_called()

    def test_client_closed_even_when_gate_fails(self) -> None:
        client = MagicMock()
        client.has_feature.return_value = False

        service = _make_service(client)
        service.get_credits(aliases=["prod"])

        client.close.assert_called_once()


# ---------------------------------------------------------------------------
# Mixed multi-project fan-out
# ---------------------------------------------------------------------------


class TestMixedFanOut:
    def _client_factory(self):
        """Return a (url, token) -> client factory keyed by token.

        ``a`` succeeds, ``b`` is non-PAYG, ``c`` raises KeboolaApiError from
        the billing call itself.
        """
        client_a = MagicMock()
        client_a.has_feature.return_value = True
        client_a.get_credits.return_value = LIVE_PAYLOAD

        client_b = MagicMock()
        client_b.has_feature.return_value = False

        client_c = MagicMock()
        client_c.has_feature.return_value = True
        client_c.get_credits.side_effect = KeboolaApiError(
            message="Internal server error",
            status_code=500,
            error_code="API_ERROR",
            retryable=False,
        )

        by_token = {_TOKEN_A: client_a, _TOKEN_B: client_b, _TOKEN_C: client_c}
        clients = {"a": client_a, "b": client_b, "c": client_c}

        def factory(url: str, token: str) -> MagicMock:
            return by_token[token]

        return factory, clients

    def test_three_projects_all_outcomes_present(self) -> None:
        factory, clients = self._client_factory()
        cs = _mock_config_store(
            {
                "a": {"url": "https://k.com", "token": _TOKEN_A, "project_id": 1},
                "b": {"url": "https://k.com", "token": _TOKEN_B, "project_id": 2},
                "c": {"url": "https://k.com", "token": _TOKEN_C, "project_id": 3},
            }
        )
        service = BillingService(config_store=cs, client_factory=factory)
        result = service.get_credits()

        # Success row for "a" still returned.
        assert len(result["credits"]) == 1
        assert result["credits"][0]["project_alias"] == "a"
        assert result["credits"][0]["consumed"] == 100.5

        # Both "b" (non-PAYG) and "c" (API error) degrade to error entries;
        # neither one aborts the fan-out.
        assert len(result["errors"]) == 2
        error_aliases = {e["project_alias"] for e in result["errors"]}
        assert error_aliases == {"b", "c"}

        b_entry = next(e for e in result["errors"] if e["project_alias"] == "b")
        assert b_entry["error_code"] == str(ErrorCode.PAYG_NOT_AVAILABLE)

        c_entry = next(e for e in result["errors"] if e["project_alias"] == "c")
        assert c_entry["error_code"] == "API_ERROR"

        # Every client is closed regardless of outcome.
        for client in clients.values():
            client.close.assert_called_once()

    def test_connection_error_message_mentions_unreachable(self) -> None:
        """A connection/DNS failure that slips past the gate gets a clear message."""
        client = MagicMock()
        client.has_feature.return_value = True
        client.get_credits.side_effect = KeboolaApiError(
            message="Cannot connect to https://billing.example.com (token: ***)",
            status_code=0,
            error_code=ErrorCode.CONNECTION_ERROR,
            retryable=True,
        )
        service = _make_service(client)
        result = service.get_credits(aliases=["prod"])

        assert result["credits"] == []
        entry = result["errors"][0]
        assert entry["error_code"] == str(ErrorCode.CONNECTION_ERROR)
        assert "could not reach" in entry["message"].lower()
        assert "billing" in entry["message"].lower()


# ---------------------------------------------------------------------------
# Unknown alias
# ---------------------------------------------------------------------------


class TestUnknownAlias:
    def test_unknown_alias_raises_config_error(self) -> None:
        client = MagicMock()
        service = _make_service(client)
        with pytest.raises(ConfigError):
            service.get_credits(aliases=["ghost"])

    def test_unknown_alias_does_not_become_a_per_project_error(self) -> None:
        """ConfigError from resolve_projects must propagate, not be swallowed."""
        client = MagicMock()
        service = _make_service(client)
        with pytest.raises(ConfigError):
            service.get_credits(aliases=["prod", "ghost"])
        # No billing call should have been attempted for either alias --
        # resolution happens before the fan-out starts.
        client.has_feature.assert_not_called()
        client.get_credits.assert_not_called()


# ---------------------------------------------------------------------------
# Deterministic ordering
# ---------------------------------------------------------------------------


class TestDeterministicOrdering:
    def test_credits_sorted_by_alias(self) -> None:
        client = MagicMock()
        client.has_feature.return_value = True
        client.get_credits.return_value = LIVE_PAYLOAD

        cs = _mock_config_store(
            {
                "zeta": {"url": "https://k.com", "token": _TOKEN_A, "project_id": 1},
                "alpha": {"url": "https://k.com", "token": _TOKEN_A, "project_id": 2},
                "mid": {"url": "https://k.com", "token": _TOKEN_A, "project_id": 3},
            }
        )
        service = BillingService(config_store=cs, client_factory=lambda url, tok: client)
        result = service.get_credits()

        aliases = [row["project_alias"] for row in result["credits"]]
        assert aliases == sorted(aliases)
        assert aliases == ["alpha", "mid", "zeta"]

    def test_errors_sorted_by_alias(self) -> None:
        client = MagicMock()
        client.has_feature.return_value = False

        cs = _mock_config_store(
            {
                "zeta": {"url": "https://k.com", "token": _TOKEN_A, "project_id": 1},
                "alpha": {"url": "https://k.com", "token": _TOKEN_A, "project_id": 2},
            }
        )
        service = BillingService(config_store=cs, client_factory=lambda url, tok: client)
        result = service.get_credits()

        aliases = [e["project_alias"] for e in result["errors"]]
        assert aliases == ["alpha", "zeta"]


# ---------------------------------------------------------------------------
# Tolerant parsing: missing ``stats``
# ---------------------------------------------------------------------------


class TestMissingStats:
    def test_payload_without_stats_zeroes_breakdown(self) -> None:
        client = MagicMock()
        client.has_feature.return_value = True
        client.get_credits.return_value = {"consumed": 10.0, "remaining": 5.0}

        service = _make_service(client)
        result = service.get_credits(aliases=["prod"])

        assert result["errors"] == []
        row = result["credits"][0]
        assert row["consumed"] == 10.0
        assert row["remaining"] == 5.0
        assert row["total"] == 15.0
        assert row["component_jobs_consumed"] == 0.0
        assert row["workspace_jobs"] == []

    def test_completely_empty_payload_does_not_raise(self) -> None:
        client = MagicMock()
        client.has_feature.return_value = True
        client.get_credits.return_value = {}

        service = _make_service(client)
        result = service.get_credits(aliases=["prod"])

        assert result["errors"] == []
        row = result["credits"][0]
        assert row["consumed"] == 0.0
        assert row["remaining"] == 0.0
        assert row["total"] == 0.0
        assert row["component_jobs_consumed"] == 0.0
        assert row["workspace_jobs"] == []
