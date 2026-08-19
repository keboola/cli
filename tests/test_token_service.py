"""Tests for TokenService -- scoped-token create / delete / refresh over a mock client."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.token_service import TokenService

STACK_URL = "https://connection.keboola.com"
TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
ALIAS = "padak"


@pytest.fixture
def store(tmp_config_dir: Path) -> ConfigStore:
    s = ConfigStore(config_dir=tmp_config_dir)
    s.add_project(
        ALIAS,
        ProjectConfig(stack_url=STACK_URL, token=TOKEN, project_name="Padak 2.0", project_id=10539),
    )
    return s


@pytest.fixture
def client_factory() -> tuple[MagicMock, MagicMock]:
    mock = MagicMock()
    factory = MagicMock(return_value=mock)
    return factory, mock


def _svc(store: ConfigStore, factory: MagicMock) -> TokenService:
    return TokenService(store, client_factory=factory)


class TestCreateScopedToken:
    def test_returns_alias_plus_client_result(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.create_scoped_token.return_value = {
            "id": "12345",
            "token": "12345-secretValue",
            "description": "device enrollment",
            "expires": None,
        }
        result = _svc(store, factory).create_scoped_token(
            alias=ALIAS, description="device enrollment"
        )
        assert result["alias"] == ALIAS
        assert result["id"] == "12345"
        assert result["token"] == "12345-secretValue"
        factory.assert_called_once_with(STACK_URL, TOKEN)
        mock.close.assert_called_once()

    def test_read_then_write_override(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.create_scoped_token.return_value = {"id": "1", "token": "t"}
        _svc(store, factory).create_scoped_token(
            alias=ALIAS,
            description="scoped",
            bucket_read=["in.c-a", "in.c-shared"],
            bucket_write=["in.c-shared", "out.c-b"],
        )
        kwargs = mock.create_scoped_token.call_args.kwargs
        # write is the stronger grant -- it wins over read on the same bucket.
        assert kwargs["bucket_permissions"] == {
            "in.c-a": "read",
            "in.c-shared": "write",
            "out.c-b": "write",
        }

    def test_no_bucket_permissions_passes_none(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.create_scoped_token.return_value = {"id": "1", "token": "t"}
        _svc(store, factory).create_scoped_token(alias=ALIAS, description="plain")
        kwargs = mock.create_scoped_token.call_args.kwargs
        # empty maps/lists collapse to None so the client omits the form fields.
        assert kwargs["bucket_permissions"] is None
        assert kwargs["component_access"] is None

    def test_forwards_all_flags(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.create_scoped_token.return_value = {"id": "1", "token": "t"}
        _svc(store, factory).create_scoped_token(
            alias=ALIAS,
            description="full",
            bucket_write=["out.c-b"],
            component_access=["keboola.ex-db-mysql"],
            can_read_all_file_uploads=True,
            expires_in=3600,
        )
        kwargs = mock.create_scoped_token.call_args.kwargs
        assert kwargs["description"] == "full"
        assert kwargs["bucket_permissions"] == {"out.c-b": "write"}
        assert kwargs["component_access"] == ["keboola.ex-db-mysql"]
        assert kwargs["can_read_all_file_uploads"] is True
        assert kwargs["expires_in"] == 3600

    def test_unknown_alias_raises(self, store, client_factory) -> None:
        factory, _ = client_factory
        with pytest.raises(ConfigError):
            _svc(store, factory).create_scoped_token(alias="nope", description="x")


class TestDeleteToken:
    def test_delete_shape(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.delete_token.return_value = None
        result = _svc(store, factory).delete_token(alias=ALIAS, token_id="999")
        assert result == {"status": "deleted", "alias": ALIAS, "token_id": "999"}
        mock.delete_token.assert_called_once_with("999")
        factory.assert_called_once_with(STACK_URL, TOKEN)
        mock.close.assert_called_once()

    def test_unknown_alias_raises(self, store, client_factory) -> None:
        factory, _ = client_factory
        with pytest.raises(ConfigError):
            _svc(store, factory).delete_token(alias="nope", token_id="1")


class TestRefreshToken:
    def test_refresh_shape(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.refresh_token.return_value = {
            "id": "999",
            "token": "999-newSecret",
            "expires": None,
        }
        result = _svc(store, factory).refresh_token(alias=ALIAS, token_id="999")
        assert result["alias"] == ALIAS
        assert result["id"] == "999"
        assert result["token"] == "999-newSecret"
        mock.refresh_token.assert_called_once_with("999")
        mock.close.assert_called_once()

    def test_unknown_alias_raises(self, store, client_factory) -> None:
        factory, _ = client_factory
        with pytest.raises(ConfigError):
            _svc(store, factory).refresh_token(alias="nope", token_id="1")


class TestListTokens:
    def test_returns_alias_and_tokens(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.list_tokens.return_value = [
            {"id": "1", "description": "master", "isMasterToken": True},
            {"id": "2", "description": "device", "isMasterToken": False},
        ]
        result = _svc(store, factory).list_tokens(alias=ALIAS)
        assert result["alias"] == ALIAS
        assert result["count"] == 2
        assert [t["id"] for t in result["tokens"]] == ["1", "2"]
        factory.assert_called_once_with(STACK_URL, TOKEN)
        mock.close.assert_called_once()

    def test_secret_values_are_stripped(self, store, client_factory) -> None:
        """A project with `force-decrypted-token` returns live secrets in the list.

        `token create` reveals a secret ONCE by design; a listing that dumped
        every live token's value to stdout would break that contract wholesale.
        """
        factory, mock = client_factory
        mock.list_tokens.return_value = [
            {"id": "1", "description": "master", "token": "1-liveSecretValue"},
            {"id": "2", "description": "device"},
        ]
        result = _svc(store, factory).list_tokens(alias=ALIAS)
        assert "token" not in result["tokens"][0]
        assert "liveSecretValue" not in str(result)
        # everything else survives untouched
        assert result["tokens"][0]["description"] == "master"

    def test_empty_list(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.list_tokens.return_value = []
        result = _svc(store, factory).list_tokens(alias=ALIAS)
        assert result["tokens"] == []
        assert result["count"] == 0

    def test_unknown_alias_raises(self, store, client_factory) -> None:
        factory, _ = client_factory
        with pytest.raises(ConfigError):
            _svc(store, factory).list_tokens(alias="nope")

    def test_client_closed_when_listing_raises(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.list_tokens.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError):
            _svc(store, factory).list_tokens(alias=ALIAS)
        mock.close.assert_called_once()
