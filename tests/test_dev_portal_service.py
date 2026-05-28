"""Tests for DeveloperPortalService — identity CRUD, prepare/apply, diff, validation."""

from __future__ import annotations

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
