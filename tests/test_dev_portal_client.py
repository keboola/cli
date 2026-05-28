"""Tests for DeveloperPortalClient — login, MFA, CRUD against apps-api."""

from __future__ import annotations

import pytest

from keboola_agent_cli.dev_portal_client import DeveloperPortalClient
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import DeveloperPortalIdentity


def _identity(**overrides) -> DeveloperPortalIdentity:
    defaults = dict(username="service.keboola.x", password="p")
    defaults.update(overrides)
    return DeveloperPortalIdentity(**defaults)


class TestLoginTokenPath:
    def test_login_returns_bearer(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/auth/login",
            json={"token": "Bearer abc"},
            status_code=200,
        )
        with DeveloperPortalClient(_identity()) as client:
            client._ensure_authenticated()
            assert client._bearer == "Bearer abc"
            assert len(httpx_mock.get_requests()) == 1

    def test_login_bad_credentials_raises(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/auth/login",
            json={"error": "invalid credentials"},
            status_code=401,
        )
        with DeveloperPortalClient(_identity()) as client:
            with pytest.raises(KeboolaApiError) as exc:
                client._ensure_authenticated()
            assert exc.value.error_code == ErrorCode.DP_LOGIN_FAILED
