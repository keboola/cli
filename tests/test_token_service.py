"""Tests for TokenService -- scoped-token create / delete / refresh over a mock client."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.token_service import TokenService

STACK_URL = "https://connection.keboola.com"
TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
ALIAS = "padak"


def _ago(*, days: int) -> str:
    """A token `created` timestamp N days in the past, in the API's format."""
    moment = datetime.now(UTC) - timedelta(days=days)
    return moment.strftime("%Y-%m-%dT%H:%M:%S%z")


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


class TestListTokensWithLastUsed:
    """`token list --with-last-used` derives recency from each token's event feed.

    The derivation has to answer three states apart, because they lead to three
    different decisions: a token still in use (leave it), a token minted and
    never used (a mis-provisioning worth investigating), and a token whose
    activity -- if any -- predates the API's event retention (unknown, judge it
    by hand).
    """

    def test_default_makes_no_event_calls_and_adds_no_keys(self, store, client_factory) -> None:
        """Without the flag the command must cost exactly what it costs today.

        The enrichment is N+1 requests; a plain `token list` that just wants an
        id to feed `token delete` must not pay for it, and machine consumers
        must keep the response shape they parse today.
        """
        factory, mock = client_factory
        mock.list_tokens.return_value = [{"id": "1", "description": "master"}]

        result = _svc(store, factory).list_tokens(alias=ALIAS)

        mock.list_token_events.assert_not_called()
        assert set(result["tokens"][0]) == {"id", "description"}
        assert "errors" not in result

    def test_recent_activity_is_reported_with_its_event_name(self, store, client_factory) -> None:
        """The event name distinguishes a human on `storage.*` from an agent on MCP."""
        factory, mock = client_factory
        mock.list_tokens.return_value = [
            {"id": "1", "description": "mcp", "created": _ago(days=30)}
        ]
        mock.list_token_events.return_value = [
            {
                "uuid": "01a01e1f-5c53-725a-9fe7-56945f67487a",
                "created": "2026-08-20T09:13:33+0200",
                "event": "ext.keboola.mcp-server-tool.query",
            }
        ]

        token = _svc(store, factory).list_tokens(alias=ALIAS, with_last_used=True)["tokens"][0]

        assert token["lastUsedStatus"] == "used"
        assert token["lastUsed"] == "2026-08-20T09:13:33+0200"
        assert token["lastUsedEvent"] == "ext.keboola.mcp-server-tool.query"
        mock.list_token_events.assert_called_once_with("1")

    def test_empty_feed_within_retention_is_never_used(self, store, client_factory) -> None:
        """Minted inside the retention window with no activity => provably never used.

        This is the finding the audit exists for, and the one a naive
        `events[0]` read gets exactly backwards.
        """
        factory, mock = client_factory
        mock.list_tokens.return_value = [
            {"id": "1", "description": "dead", "created": _ago(days=30)}
        ]
        mock.list_token_events.return_value = []

        token = _svc(store, factory).list_tokens(alias=ALIAS, with_last_used=True)["tokens"][0]

        assert token["lastUsedStatus"] == "never"
        assert token["lastUsed"] is None
        assert token["lastUsedEvent"] is None

    def test_empty_feed_older_than_retention_is_unknown(self, store, client_factory) -> None:
        """Older than retention with no activity => the API simply cannot say.

        Reporting this as "never used" would be a lie about the exact tokens
        someone is most likely to go and revoke.
        """
        factory, mock = client_factory
        mock.list_tokens.return_value = [
            {"id": "1", "description": "ancient", "created": _ago(days=400)}
        ]
        mock.list_token_events.return_value = []

        token = _svc(store, factory).list_tokens(alias=ALIAS, with_last_used=True)["tokens"][0]

        assert token["lastUsedStatus"] == "unknown"
        assert token["lastUsed"] is None

    def test_missing_created_date_is_unknown_not_never(self, store, client_factory) -> None:
        """No creation date => cannot prove "never"; degrade to unknown."""
        factory, mock = client_factory
        mock.list_tokens.return_value = [{"id": "1", "description": "no date"}]
        mock.list_token_events.return_value = []

        token = _svc(store, factory).list_tokens(alias=ALIAS, with_last_used=True)["tokens"][0]

        assert token["lastUsedStatus"] == "unknown"

    def test_one_failing_token_does_not_abort_the_others(self, store, client_factory) -> None:
        """Per-token failures degrade individually and are accumulated.

        Same contract as the rest of the CLI's fan-outs: one project (here, one
        token) failing must not cost the caller the whole audit.
        """
        factory, mock = client_factory
        mock.list_tokens.return_value = [
            {"id": "1", "description": "ok", "created": _ago(days=10)},
            {"id": "2", "description": "boom", "created": _ago(days=10)},
        ]

        def events(token_id: str) -> list[dict[str, str]]:
            if token_id == "2":
                raise KeboolaApiError("nope", error_code=ErrorCode.API_ERROR)
            return [{"created": "2026-08-20T09:13:33+0200", "event": "storage.tablesListed"}]

        mock.list_token_events.side_effect = events

        result = _svc(store, factory).list_tokens(alias=ALIAS, with_last_used=True)

        by_id = {t["id"]: t for t in result["tokens"]}
        assert by_id["1"]["lastUsedStatus"] == "used"
        assert by_id["2"]["lastUsedStatus"] == "error"
        assert by_id["2"]["lastUsed"] is None
        assert [e["token_id"] for e in result["errors"]] == ["2"]

    def test_sorted_dormant_first(self, store, client_factory) -> None:
        """Reading order IS the cleanup order: never, unknown, then oldest use first."""
        factory, mock = client_factory
        mock.list_tokens.return_value = [
            {"id": "recent", "created": _ago(days=10)},
            {"id": "never", "created": _ago(days=10)},
            {"id": "stale", "created": _ago(days=10)},
            {"id": "unknown", "created": _ago(days=400)},
        ]
        feeds = {
            "recent": [{"created": "2026-08-20T09:00:00+0200", "event": "storage.tablesListed"}],
            "never": [],
            "stale": [{"created": "2026-04-01T09:00:00+0200", "event": "storage.tablesListed"}],
            "unknown": [],
        }
        mock.list_token_events.side_effect = lambda token_id: feeds[token_id]

        result = _svc(store, factory).list_tokens(alias=ALIAS, with_last_used=True)

        assert [t["id"] for t in result["tokens"]] == ["never", "unknown", "stale", "recent"]

    def test_client_is_closed_after_the_fan_out(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.list_tokens.return_value = [{"id": "1", "created": _ago(days=10)}]
        mock.list_token_events.return_value = []

        _svc(store, factory).list_tokens(alias=ALIAS, with_last_used=True)

        mock.close.assert_called_once()
