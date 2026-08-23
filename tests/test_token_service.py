"""Tests for TokenService -- scoped-token create / delete / refresh over a mock client."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar
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
    # Explicit master-token info so tests exercising `create` pass the
    # pre-flight guard deliberately, not via a truthy MagicMock return.
    mock.get_project_info.return_value = {
        "id": "6610637",
        "description": "master",
        "isMasterToken": True,
    }
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


class TestMasterTokenGuard:
    """`token create` pre-flight: the acting token must be a master token.

    The Storage API's `CreateTokenVoter` treats "a normal token carrying
    `canManageTokens`" as an impossible state and throws a `LogicException`,
    surfaced to the caller as a generic 500 "Application error." -- exactly the
    shape of token `org setup` / `project refresh` mint (issue #599). The
    guard turns that into a clean local MISSING_MASTER_TOKEN before any write,
    mirroring the existing `config oauth-url` guard. It covers the mint ONLY --
    refresh/list/delete have no such defect and stay unguarded.
    """

    NON_MASTER_INFO: ClassVar[dict[str, object]] = {
        "id": "7434918",
        "description": "kbagent-cli [petr@keboola.com]",
        "isMasterToken": False,
    }

    def test_create_rejects_non_master_token_before_any_write(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.get_project_info.return_value = self.NON_MASTER_INFO
        with pytest.raises(KeboolaApiError) as excinfo:
            _svc(store, factory).create_scoped_token(alias=ALIAS, description="repro")
        assert excinfo.value.error_code == ErrorCode.MISSING_MASTER_TOKEN
        assert excinfo.value.status_code == 403
        # the guard must fire BEFORE the POST -- nothing may reach the API
        mock.create_scoped_token.assert_not_called()
        mock.close.assert_called_once()

    def test_create_error_names_the_remedy(self, store, client_factory) -> None:
        """The message must carry the token identity and the `project edit` fix."""
        factory, mock = client_factory
        mock.get_project_info.return_value = self.NON_MASTER_INFO
        with pytest.raises(KeboolaApiError) as excinfo:
            _svc(store, factory).create_scoped_token(alias=ALIAS, description="repro")
        message = str(excinfo.value)
        assert "7434918" in message
        assert f"kbagent project edit --project {ALIAS}" in message

    def test_create_passes_on_master_token(self, store, client_factory) -> None:
        factory, mock = client_factory
        mock.create_scoped_token.return_value = {"id": "1", "token": "t"}
        result = _svc(store, factory).create_scoped_token(alias=ALIAS, description="ok")
        assert result["id"] == "1"
        mock.get_project_info.assert_called_once()
        mock.create_scoped_token.assert_called_once()

    def test_refresh_delete_and_list_are_not_guarded(self, store, client_factory) -> None:
        """Only the mint is guarded -- the other three must keep working.

        The `CreateTokenVoter` defect is create-only. `RefreshTokenVoter` lets
        any token rotate itself and a `canManageTokens` token rotate another,
        and list/delete need the same flag (all verified in #599), so guarding
        them would block working paths -- most sharply the incident one:
        rotating a leaked device token from an `org setup` project token.
        """
        factory, mock = client_factory
        mock.get_project_info.return_value = self.NON_MASTER_INFO
        mock.refresh_token.return_value = {"id": "999", "token": "999-new"}
        mock.list_tokens.return_value = []
        svc = _svc(store, factory)

        assert svc.refresh_token(alias=ALIAS, token_id="999")["id"] == "999"
        svc.delete_token(alias=ALIAS, token_id="1")
        svc.list_tokens(alias=ALIAS)

        mock.refresh_token.assert_called_once_with("999")
        mock.delete_token.assert_called_once()
        mock.list_tokens.assert_called_once()
        # not even the pre-flight verify goes out -- no wasted request either
        mock.get_project_info.assert_not_called()


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


class TestListTokensAll:
    """`list_tokens_all` -- cross-project token listing (mirrors JobService.list_jobs)."""

    def _store_with_two_projects(self, tmp_config_dir: Path) -> ConfigStore:
        s = ConfigStore(config_dir=tmp_config_dir)
        s.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-prod-fakeTestTokenDoNotUseXXXXXX",
            ),
        )
        s.add_project(
            "dev",
            ProjectConfig(
                stack_url="https://connection.north-europe.azure.keboola.com",
                token="901-dev-fakeTestTokenDoNotUseXXXXXXX",
            ),
        )
        return s

    def test_multi_project_aggregation_stamps_project_alias(self, tmp_config_dir: Path) -> None:
        store = self._store_with_two_projects(tmp_config_dir)
        prod_client = MagicMock()
        prod_client.list_tokens.return_value = [
            {"id": "1", "description": "prod-a"},
            {"id": "2", "description": "prod-b"},
        ]
        dev_client = MagicMock()
        dev_client.list_tokens.return_value = [{"id": "3", "description": "dev-a"}]

        def factory(url, token):
            return prod_client if "prod" in token else dev_client

        service = TokenService(store, client_factory=factory)
        result = service.list_tokens_all()

        assert result["count"] == 3
        assert result["errors"] == []
        by_id = {t["id"]: t for t in result["tokens"]}
        assert by_id["1"]["project_alias"] == "prod"
        assert by_id["2"]["project_alias"] == "prod"
        assert by_id["3"]["project_alias"] == "dev"
        # grouped by project_alias ("dev" < "prod"), per-project order preserved
        # within each group (id "1" before id "2", both from prod).
        assert [t["id"] for t in result["tokens"]] == ["3", "1", "2"]
        prod_client.close.assert_called_once()
        dev_client.close.assert_called_once()

    def test_secrets_stripped_across_projects(self, tmp_config_dir: Path) -> None:
        store = self._store_with_two_projects(tmp_config_dir)
        prod_client = MagicMock()
        prod_client.list_tokens.return_value = [
            {"id": "1", "token": "1-liveSecretValue", "description": "master"}
        ]
        dev_client = MagicMock()
        dev_client.list_tokens.return_value = [
            {"id": "2", "token": "2-anotherSecret", "description": "device"}
        ]

        def factory(url, token):
            return prod_client if "prod" in token else dev_client

        result = TokenService(store, client_factory=factory).list_tokens_all()

        assert "liveSecretValue" not in str(result)
        assert "anotherSecret" not in str(result)
        for token in result["tokens"]:
            assert "token" not in token

    def test_partial_failure_degrades_into_errors(self, tmp_config_dir: Path) -> None:
        store = self._store_with_two_projects(tmp_config_dir)
        prod_client = MagicMock()
        prod_client.list_tokens.return_value = [{"id": "1", "description": "ok"}]
        dev_client = MagicMock()
        dev_client.list_tokens.side_effect = KeboolaApiError(
            message="Token expired",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )

        def factory(url, token):
            return prod_client if "prod" in token else dev_client

        result = TokenService(store, client_factory=factory).list_tokens_all()

        assert result["count"] == 1
        assert result["tokens"][0]["project_alias"] == "prod"
        assert len(result["errors"]) == 1
        assert result["errors"][0]["project_alias"] == "dev"
        assert result["errors"][0]["error_code"] == "INVALID_TOKEN"

    def test_aliases_none_resolves_all_registered_projects(self, tmp_config_dir: Path) -> None:
        store = self._store_with_two_projects(tmp_config_dir)
        prod_client = MagicMock()
        prod_client.list_tokens.return_value = []
        dev_client = MagicMock()
        dev_client.list_tokens.return_value = []

        def factory(url, token):
            return prod_client if "prod" in token else dev_client

        TokenService(store, client_factory=factory).list_tokens_all(aliases=None)

        prod_client.list_tokens.assert_called_once()
        dev_client.list_tokens.assert_called_once()

    def test_alias_filter_only_queries_specified_projects(self, tmp_config_dir: Path) -> None:
        store = self._store_with_two_projects(tmp_config_dir)
        prod_client = MagicMock()
        prod_client.list_tokens.return_value = [{"id": "1"}]
        dev_client = MagicMock()
        dev_client.list_tokens.return_value = [{"id": "2"}]

        def factory(url, token):
            return prod_client if "prod" in token else dev_client

        result = TokenService(store, client_factory=factory).list_tokens_all(aliases=["prod"])

        assert result["count"] == 1
        assert result["tokens"][0]["project_alias"] == "prod"
        dev_client.list_tokens.assert_not_called()

    def test_unknown_alias_raises(self, tmp_config_dir: Path) -> None:
        store = self._store_with_two_projects(tmp_config_dir)
        service = TokenService(store, client_factory=MagicMock())
        with pytest.raises(ConfigError):
            service.list_tokens_all(aliases=["nope"])

    def test_with_last_used_global_dormant_first_ordering(self, tmp_config_dir: Path) -> None:
        """Dormant-first ordering spans ALL projects together, not grouped per project.

        `prod` has a token used recently and one never used; `dev` has one
        token whose activity has aged out of retention. The global order must
        be never -> unknown -> recent-use, regardless of which project each
        token belongs to.
        """
        store = self._store_with_two_projects(tmp_config_dir)
        prod_client = MagicMock()
        prod_client.list_tokens.return_value = [
            {"id": "prod-recent", "created": _ago(days=10)},
            {"id": "prod-never", "created": _ago(days=10)},
        ]
        dev_client = MagicMock()
        dev_client.list_tokens.return_value = [
            {"id": "dev-unknown", "created": _ago(days=400)},
        ]

        prod_events = {
            "prod-recent": [
                {"created": "2026-08-20T09:00:00+0200", "event": "storage.tablesListed"}
            ],
            "prod-never": [],
        }
        dev_events: dict[str, list] = {"dev-unknown": []}
        prod_client.list_token_events.side_effect = lambda token_id: prod_events[token_id]
        dev_client.list_token_events.side_effect = lambda token_id: dev_events[token_id]

        def factory(url, token):
            return prod_client if "prod" in token else dev_client

        result = TokenService(store, client_factory=factory).list_tokens_all(with_last_used=True)

        assert [t["id"] for t in result["tokens"]] == [
            "prod-never",
            "dev-unknown",
            "prod-recent",
        ]

    def test_with_last_used_per_token_errors_carry_project_alias(
        self, tmp_config_dir: Path
    ) -> None:
        store = self._store_with_two_projects(tmp_config_dir)
        prod_client = MagicMock()
        prod_client.list_tokens.return_value = [
            {"id": "1", "description": "boom", "created": _ago(days=10)}
        ]
        prod_client.list_token_events.side_effect = KeboolaApiError(
            "nope", error_code=ErrorCode.API_ERROR
        )
        dev_client = MagicMock()
        dev_client.list_tokens.return_value = []

        def factory(url, token):
            return prod_client if "prod" in token else dev_client

        result = TokenService(store, client_factory=factory).list_tokens_all(with_last_used=True)

        # Per-token lookup failures land in token_errors, NOT errors: the
        # project itself listed fine, so reporting it as unlistable (the
        # errors[] meaning) would be wrong.
        assert result["errors"] == []
        assert len(result["token_errors"]) == 1
        assert result["token_errors"][0]["token_id"] == "1"
        assert result["token_errors"][0]["project_alias"] == "prod"

    def test_no_projects_configured_returns_empty(self, tmp_config_dir: Path) -> None:
        store = ConfigStore(config_dir=tmp_config_dir)
        service = TokenService(store, client_factory=MagicMock())
        result = service.list_tokens_all()
        assert result == {"tokens": [], "count": 0, "errors": [], "token_errors": []}

    def test_clients_are_closed_for_every_project(self, tmp_config_dir: Path) -> None:
        store = self._store_with_two_projects(tmp_config_dir)
        prod_client = MagicMock()
        prod_client.list_tokens.return_value = []
        dev_client = MagicMock()
        dev_client.list_tokens.return_value = []

        def factory(url, token):
            return prod_client if "prod" in token else dev_client

        TokenService(store, client_factory=factory).list_tokens_all()

        prod_client.close.assert_called_once()
        dev_client.close.assert_called_once()
