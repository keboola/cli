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


class TestPortalReads:
    def test_list_apps(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/auth/login",
            json={"token": "Bearer abc"},
        )
        httpx_mock.add_response(
            method="GET",
            url="https://apps-api.keboola.com/vendors/keboola/apps?limit=1000",
            json={"apps": [{"id": "keboola.ex-foo"}]},
        )
        with DeveloperPortalClient(_identity()) as client:
            apps = client.list_apps("keboola")
            assert apps == [{"id": "keboola.ex-foo"}]

    def test_get_app_404(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/auth/login",
            json={"token": "Bearer abc"},
        )
        httpx_mock.add_response(
            method="GET",
            url="https://apps-api.keboola.com/vendors/keboola/apps/keboola.missing",
            status_code=404,
            json={"error": "not found"},
        )
        with DeveloperPortalClient(_identity()) as client:
            with pytest.raises(KeboolaApiError) as exc:
                client.get_app("keboola", "keboola.missing")
            assert exc.value.error_code == ErrorCode.DP_APP_NOT_FOUND


class TestPortalWrites:
    def test_create_app(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/auth/login",
            json={"token": "Bearer abc"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/vendors/keboola/apps",
            json={"id": "ex-foo", "name": "Foo"},
        )
        with DeveloperPortalClient(_identity()) as client:
            resp = client.create_app(
                "keboola", {"id": "ex-foo", "name": "Foo", "type": "extractor"}
            )
            assert resp["id"] == "ex-foo"

    def test_patch_app(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/auth/login",
            json={"token": "Bearer abc"},
        )
        httpx_mock.add_response(
            method="PATCH",
            url="https://apps-api.keboola.com/vendors/keboola/apps/keboola.ex-foo",
            json={"id": "ex-foo", "name": "Foo 2"},
        )
        with DeveloperPortalClient(_identity()) as client:
            resp = client.patch_app("keboola", "keboola.ex-foo", {"name": "Foo 2"})
            assert resp["name"] == "Foo 2"

    def test_publish_app(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/auth/login",
            json={"token": "Bearer abc"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/vendors/keboola/apps/keboola.ex-foo/publish",
            json={"status": "submitted"},
        )
        with DeveloperPortalClient(_identity()) as client:
            assert client.publish_app("keboola", "keboola.ex-foo")["status"] == "submitted"

    def test_deprecate_app(self, httpx_mock):
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/auth/login",
            json={"token": "Bearer abc"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/vendors/keboola/apps/keboola.ex-foo/deprecate",
            json={"status": "deprecated"},
        )
        with DeveloperPortalClient(_identity()) as client:
            assert client.deprecate_app("keboola", "keboola.ex-foo")["status"] == "deprecated"


class TestIconUpload:
    def test_upload_icon_two_hop(self, httpx_mock, monkeypatch):
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/auth/login",
            json={"token": "Bearer abc"},
        )
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/vendors/keboola/apps/keboola.ex-foo/icon",
            json={"link": "https://s3.example/presigned"},
        )
        # The S3 PUT bypasses httpx; we mock urllib.request.urlopen.
        seen = {}

        class _FakeResp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        def fake_urlopen(req):
            seen["url"] = req.full_url
            seen["data"] = req.data
            seen["method"] = req.method
            return _FakeResp()

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        with DeveloperPortalClient(_identity()) as client:
            client.upload_icon("keboola", "keboola.ex-foo", b"\x89PNG\r\n\x1a\nrest")
        assert seen["url"] == "https://s3.example/presigned"
        assert seen["data"] == b"\x89PNG\r\n\x1a\nrest"
        assert seen["method"] == "PUT"

    def test_upload_icon_presign_failure(self, httpx_mock, monkeypatch):
        httpx_mock.add_response(
            method="POST",
            url="https://apps-api.keboola.com/auth/login",
            json={"token": "Bearer abc"},
        )
        # Add 3 responses (MAX_RETRIES=3) since 500 is retryable.
        for _ in range(3):
            httpx_mock.add_response(
                method="POST",
                url="https://apps-api.keboola.com/vendors/keboola/apps/keboola.ex-foo/icon",
                status_code=500,
                json={"error": "boom"},
            )
        # Suppress retry sleeps.
        import keboola_agent_cli.http_base as http_base_module

        monkeypatch.setattr(http_base_module.time, "sleep", lambda _: None)

        with DeveloperPortalClient(_identity()) as client:
            with pytest.raises(KeboolaApiError) as exc:
                client.upload_icon("keboola", "keboola.ex-foo", b"data")
            assert exc.value.error_code == ErrorCode.DP_ICON_UPLOAD_FAILED
