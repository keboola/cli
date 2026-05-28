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


class TestLoginMfaPath:
    def test_mfa_prompt_completes_login(self, httpx_mock, monkeypatch):
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/auth/login",
            json={"session": "sess-1"},
            status_code=200,
            match_json={"email": "u@k.com", "password": "p"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/auth/login",
            json={"token": "Bearer xyz"},
            status_code=200,
            match_json={"email": "u@k.com", "session": "sess-1", "code": "123456"},
        )
        # Mock the /dev/tty MFA prompt.
        monkeypatch.setattr(
            "keboola_agent_cli.dev_portal_client._tty_prompt",
            lambda label, secret=False: "123456",
        )
        ident = DeveloperPortalIdentity(username="u@k.com", password="p")
        with DeveloperPortalClient(ident) as client:
            client._ensure_authenticated()
            assert client._bearer == "Bearer xyz"

    def test_mfa_no_tty_raises_mfa_required(self, httpx_mock, monkeypatch):
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/auth/login",
            json={"session": "sess-1"},
            status_code=200,
        )
        # _tty_prompt returns None when no terminal is available.
        monkeypatch.setattr(
            "keboola_agent_cli.dev_portal_client._tty_prompt",
            lambda label, secret=False: None,
        )
        ident = DeveloperPortalIdentity(username="u@k.com", password="p")
        with DeveloperPortalClient(ident) as client:
            with pytest.raises(KeboolaApiError) as exc:
                client._ensure_authenticated()
            assert exc.value.error_code == ErrorCode.DP_MFA_REQUIRED
