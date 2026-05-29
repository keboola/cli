"""Tests for DeveloperPortalService — identity CRUD, prepare/apply, diff, validation."""

from __future__ import annotations

from typing import ClassVar
from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import DeveloperPortalIdentity
from keboola_agent_cli.services.dev_portal_service import DeveloperPortalService


@pytest.fixture
def fake_client():
    mock = MagicMock()
    # Make the mock a self-returning context manager so that
    # `with factory(ident) as client:` binds `client` to the same object
    # we configure side_effects on, not to a child MagicMock.
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    return mock


@pytest.fixture
def service(config_store, fake_client):
    def factory(identity):
        return fake_client

    return DeveloperPortalService(config_store=config_store, client_factory=factory)


class TestIdentityCrud:
    def test_add_and_list(self, service, fake_client):
        # add_identity also runs verify (login probe).
        fake_client.list_apps.return_value = []
        ident = DeveloperPortalIdentity(username="u", password="p")
        service.add_identity("alpha", ident)
        result = service.list_identities()
        assert "alpha" in result
        assert result["alpha"].username == "u"

    def test_add_verify_failure_does_not_persist(self, service, fake_client, config_store):
        fake_client._ensure_authenticated.side_effect = KeboolaApiError(
            message="bad creds",
            error_code=ErrorCode.DP_LOGIN_FAILED,
        )
        ident = DeveloperPortalIdentity(username="u", password="bad")
        with pytest.raises(KeboolaApiError) as exc:
            service.add_identity("alpha", ident)
        assert exc.value.error_code == ErrorCode.DP_LOGIN_FAILED
        assert config_store.load().dev_portal_identities == {}

    def test_use_sets_default(self, service, fake_client, config_store):
        fake_client._ensure_authenticated.return_value = None
        ident = DeveloperPortalIdentity(username="u", password="p")
        service.add_identity("alpha", ident)
        service.add_identity("beta", ident)
        service.use_identity("beta")
        assert config_store.load().default_dev_portal_identity == "beta"

    def test_remove(self, service, fake_client, config_store):
        fake_client._ensure_authenticated.return_value = None
        ident = DeveloperPortalIdentity(username="u", password="p")
        service.add_identity("alpha", ident)
        service.remove_identity("alpha")
        assert "alpha" not in config_store.load().dev_portal_identities


class TestReadsAndPrepareApply:
    def _setup(self, service, fake_client):
        fake_client._ensure_authenticated.return_value = None
        ident = DeveloperPortalIdentity(username="u", password="p")
        service.add_identity("alpha", ident)

    def test_list_apps(self, service, fake_client):
        self._setup(service, fake_client)
        fake_client.list_apps.return_value = [{"id": "ex-a"}]
        assert service.list_apps("alpha", "keboola") == [{"id": "ex-a"}]
        fake_client.list_apps.assert_called_with("keboola")

    def test_get_app(self, service, fake_client):
        self._setup(service, fake_client)
        fake_client.get_app.return_value = {"id": "ex-a", "name": "Hello"}
        assert service.get_app("alpha", "keboola", "keboola.ex-a")["name"] == "Hello"

    def test_prepare_create_requires_id_name_type(self, service, fake_client):
        self._setup(service, fake_client)
        with pytest.raises(KeboolaApiError, match="payload must include 'id'"):
            service.prepare_create("alpha", "keboola", {"name": "F", "type": "extractor"})

    def test_prepare_create_rejects_banned_words_in_name(self, service, fake_client):
        self._setup(service, fake_client)
        with pytest.raises(KeboolaApiError, match="must not contain"):
            service.prepare_create(
                "alpha",
                "keboola",
                {"id": "x", "name": "Foo extractor", "type": "extractor"},
            )

    def test_prepare_patch_diff(self, service, fake_client):
        self._setup(service, fake_client)
        fake_client.get_app.return_value = {
            "id": "ex-a",
            "name": "Old",
            "shortDescription": "same",
        }
        pending = service.prepare_patch(
            "alpha",
            "keboola",
            "keboola.ex-a",
            {"name": "New", "shortDescription": "same"},
        )
        keys = {d.key for d in pending.diff}
        assert keys == {"name"}  # shortDescription unchanged is filtered out
        assert pending.diff[0].current == "Old"
        assert pending.diff[0].new == "New"

    def test_apply_patch_calls_client(self, service, fake_client):
        self._setup(service, fake_client)
        fake_client.get_app.return_value = {"id": "ex-a", "name": "Old"}
        fake_client.patch_app.return_value = {"id": "ex-a", "name": "New"}
        pending = service.prepare_patch("alpha", "keboola", "keboola.ex-a", {"name": "New"})
        result = service.apply(pending)
        assert result["name"] == "New"
        fake_client.patch_app.assert_called_with("keboola", "keboola.ex-a", {"name": "New"})

    def test_prepare_publish_missing_fields(self, service, fake_client):
        self._setup(service, fake_client)
        fake_client.get_app.return_value = {
            "id": "ex-a",
            "name": "Foo",
            "type": "extractor",
            # missing icon, repository, descriptions, license, docs
        }
        with pytest.raises(KeboolaApiError) as exc:
            service.prepare_publish("alpha", "keboola", "keboola.ex-a")
        assert exc.value.error_code == ErrorCode.DP_PUBLISH_REQUIREMENTS_MISSING
        assert "icon" in str(exc.value)


class _CountingClient:
    """Fake portal client that counts how many times it actually logs in.

    Mirrors the real client's contract: methods call `_ensure_authenticated`,
    which only logs in when no bearer is present. `seed_bearer` injects a
    bearer obtained by an earlier client for the same identity.
    """

    instances: ClassVar[list[_CountingClient]] = []

    def __init__(self, identity):
        self._bearer = None
        self.login_count = 0
        _CountingClient.instances.append(self)

    @property
    def bearer(self):
        return self._bearer

    def seed_bearer(self, bearer):
        self._bearer = bearer

    def _ensure_authenticated(self):
        if self._bearer is None:
            self.login_count += 1
            self._bearer = "tok-123"

    def get_app(self, vendor, app_id):
        self._ensure_authenticated()
        return {"id": "ex-a", "name": "Old"}

    def patch_app(self, vendor, app_id, payload):
        self._ensure_authenticated()
        return {"id": "ex-a", **payload}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestSingleLoginAcrossPrepareApply:
    """Regression: patch must authenticate once, not twice (no double MFA prompt)."""

    def test_patch_reuses_bearer_no_second_login(self, config_store):
        _CountingClient.instances = []
        ident = DeveloperPortalIdentity(username="u", password="p")
        config_store.add_dev_portal_identity("alpha", ident)

        def factory(identity):
            return _CountingClient(identity)

        svc = DeveloperPortalService(config_store=config_store, client_factory=factory)
        pending = svc.prepare_patch("alpha", "keboola", "keboola.ex-a", {"name": "New"})
        svc.apply(pending)

        # A fresh client is built for prepare and again for apply (matches the
        # real prepare/apply split), but only the first one logs in.
        assert len(_CountingClient.instances) == 2
        assert _CountingClient.instances[0].login_count == 1
        assert _CountingClient.instances[1].login_count == 0
