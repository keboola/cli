"""Tests for the device-enrollment facade methods (keboola_agent_cli.lib).

These primitives (scoped tokens + per-device OTLP stream sources, target
release 0.66.0) all delegate to the underlying ``KeboolaClient`` and wrap the
returned dict in a typed result model. Each test builds a ``Client`` whose
``_client`` is swapped for a ``MagicMock`` and asserts both the delegation
kwargs and the typed-model mapping. No network.
"""

from unittest.mock import MagicMock, patch

from keboola_agent_cli import Client, ScopedTokenResult, StreamSourceResult

# Canonical fake token (projectId-tokenId-secret); never a realistic secret.
FAKE_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
STACK_URL = "https://connection.keboola.com"


def _make_client(mock_kc: MagicMock) -> Client:
    """Build a Client and replace its underlying KeboolaClient with the mock."""
    with patch("keboola_agent_cli.lib.KeboolaClient", return_value=mock_kc):
        client = Client(url=STACK_URL, token=FAKE_TOKEN)
    client._client = mock_kc
    return client


class TestImportableModels:
    def test_models_importable_from_package_root(self) -> None:
        from keboola_agent_cli import ScopedTokenResult as STR
        from keboola_agent_cli import StreamSourceResult as SSR

        assert STR is ScopedTokenResult
        assert SSR is StreamSourceResult


class TestCreateScopedToken:
    def test_returns_typed_result_and_maps_fields(self) -> None:
        mock_kc = MagicMock()
        mock_kc.create_scoped_token.return_value = {
            "id": "12345",
            "token": "901-12345-freshSecretRevealedOnce",
            "description": "device cam-01 upload",
            "expires": "2026-08-01T00:00:00+0000",
            "canReadAllFileUploads": True,
        }
        client = _make_client(mock_kc)

        result = client.create_scoped_token(
            description="device cam-01 upload",
            bucket_permissions={"in.c-otlp-cam01": "write"},
            component_access=["keboola.ex-http"],
            can_read_all_file_uploads=True,
            expires_in=3600,
        )

        assert isinstance(result, ScopedTokenResult)
        assert result.id == "12345"
        assert result.token == "901-12345-freshSecretRevealedOnce"
        assert result.description == "device cam-01 upload"
        assert result.expires == "2026-08-01T00:00:00+0000"
        # canReadAllFileUploads alias -> can_read_all_file_uploads
        assert result.can_read_all_file_uploads is True

    def test_delegates_with_exact_kwargs(self) -> None:
        mock_kc = MagicMock()
        mock_kc.create_scoped_token.return_value = {"id": "1", "token": "t"}
        client = _make_client(mock_kc)

        client.create_scoped_token(
            description="d",
            bucket_permissions={"in.c-x": "read"},
            component_access=["keboola.wr-x"],
            can_read_all_file_uploads=True,
            expires_in=900,
        )

        mock_kc.create_scoped_token.assert_called_once_with(
            description="d",
            bucket_permissions={"in.c-x": "read"},
            component_access=["keboola.wr-x"],
            can_read_all_file_uploads=True,
            expires_in=900,
        )

    def test_defaults_forwarded(self) -> None:
        mock_kc = MagicMock()
        mock_kc.create_scoped_token.return_value = {"id": "1", "token": "t"}
        client = _make_client(mock_kc)

        result = client.create_scoped_token(description="minimal")

        call = mock_kc.create_scoped_token.call_args.kwargs
        assert call["bucket_permissions"] is None
        assert call["component_access"] is None
        assert call["can_read_all_file_uploads"] is False
        assert call["expires_in"] is None
        # absent canReadAllFileUploads defaults to False
        assert result.can_read_all_file_uploads is False


class TestDeleteToken:
    def test_delegates_and_returns_none(self) -> None:
        mock_kc = MagicMock()
        mock_kc.delete_token.return_value = None
        client = _make_client(mock_kc)

        assert client.delete_token("12345") is None
        mock_kc.delete_token.assert_called_once_with("12345")


class TestRefreshToken:
    def test_returns_typed_result(self) -> None:
        mock_kc = MagicMock()
        mock_kc.refresh_token.return_value = {
            "id": "12345",
            "token": "901-12345-rotatedSecretRevealedOnce",
            "description": "device cam-01 upload",
            "expires": None,
            "canReadAllFileUploads": False,
        }
        client = _make_client(mock_kc)

        result = client.refresh_token("12345")

        assert isinstance(result, ScopedTokenResult)
        assert result.id == "12345"
        assert result.token == "901-12345-rotatedSecretRevealedOnce"
        assert result.expires is None
        assert result.can_read_all_file_uploads is False
        mock_kc.refresh_token.assert_called_once_with("12345")


class TestCreateStreamSource:
    def test_returns_typed_result_and_maps_fields(self) -> None:
        mock_kc = MagicMock()
        mock_kc.create_stream_source.return_value = {
            "id": "cam01-src",
            "source_id": "cam01-src",
            "name": "cam01",
            "type": "otlp",
            "description": "camera 01 telemetry",
            "branch_id": "default",
            "otlp_url": "https://stream-in.eu/otlp/9999/cam01/secret123",
            "otlp_secret": "secret123",
            "base_endpoint": "https://stream-in.eu/otlp/9999/cam01",
            "sink_bucket_id": "in.c-otlp-cam01-src",
            "source": {"raw": "object"},
        }
        client = _make_client(mock_kc)

        result = client.create_stream_source(
            "cam01",
            source_type="otlp",
            description="camera 01 telemetry",
            branch_id="default",
            provision_sinks=True,
        )

        assert isinstance(result, StreamSourceResult)
        assert result.id == "cam01-src"
        assert result.source_id == "cam01-src"
        assert result.name == "cam01"
        assert result.type == "otlp"
        assert result.otlp_url == "https://stream-in.eu/otlp/9999/cam01/secret123"
        assert result.otlp_secret == "secret123"
        assert result.base_endpoint == "https://stream-in.eu/otlp/9999/cam01"
        assert result.sink_bucket_id == "in.c-otlp-cam01-src"

    def test_delegates_with_exact_kwargs(self) -> None:
        mock_kc = MagicMock()
        mock_kc.create_stream_source.return_value = {"id": "s", "name": "s"}
        client = _make_client(mock_kc)

        client.create_stream_source(
            "cam02",
            source_type="http",
            description="d",
            branch_id="42",
            provision_sinks=False,
        )

        mock_kc.create_stream_source.assert_called_once_with(
            "cam02",
            source_type="http",
            description="d",
            branch_id="42",
            provision_sinks=False,
        )

    def test_source_id_alias_maps_to_id(self) -> None:
        """A payload that only carries ``sourceId`` still populates ``id``."""
        mock_kc = MagicMock()
        mock_kc.create_stream_source.return_value = {
            "sourceId": "aliased-src",
            "name": "cam03",
            "type": "otlp",
        }
        client = _make_client(mock_kc)

        result = client.create_stream_source("cam03")

        assert result.id == "aliased-src"
        assert result.source_id == "aliased-src"
        # sink_bucket_id absent in payload -> None
        assert result.sink_bucket_id is None


class TestGetStreamSource:
    def test_returns_typed_result(self) -> None:
        mock_kc = MagicMock()
        mock_kc.get_stream_source.return_value = {
            "id": "cam01-src",
            "source_id": "cam01-src",
            "name": "cam01",
            "type": "otlp",
            "otlp_url": "https://stream-in.eu/otlp/9999/cam01/secret123",
            "sink_bucket_id": "in.c-otlp-cam01-src",
        }
        client = _make_client(mock_kc)

        result = client.get_stream_source("cam01-src", branch_id="default")

        assert isinstance(result, StreamSourceResult)
        assert result.id == "cam01-src"
        assert result.source_id == "cam01-src"
        assert result.otlp_url == "https://stream-in.eu/otlp/9999/cam01/secret123"
        assert result.sink_bucket_id == "in.c-otlp-cam01-src"
        mock_kc.get_stream_source.assert_called_once_with("cam01-src", branch_id="default")


class TestListStreamSources:
    def test_returns_raw_list(self) -> None:
        mock_kc = MagicMock()
        raw = [
            {"sourceId": "cam01-src", "name": "cam01"},
            {"sourceId": "cam02-src", "name": "cam02"},
        ]
        mock_kc.list_stream_sources.return_value = raw
        client = _make_client(mock_kc)

        result = client.list_stream_sources(branch_id="default")

        # facade returns the raw list untouched (find-or-create by name)
        assert result is raw
        mock_kc.list_stream_sources.assert_called_once_with(branch_id="default")


class TestDeleteStreamSource:
    def test_delegates_and_returns_none(self) -> None:
        mock_kc = MagicMock()
        mock_kc.delete_stream_source.return_value = None
        client = _make_client(mock_kc)

        assert client.delete_stream_source("cam01-src", branch_id="default") is None
        mock_kc.delete_stream_source.assert_called_once_with("cam01-src", branch_id="default")


class TestListTokens:
    def test_returns_typed_entries(self) -> None:
        mock_kc = MagicMock()
        mock_kc.list_tokens.return_value = [
            {
                "id": "12345",
                "description": "device 42",
                "created": "2026-08-01T10:00:00+0200",
                "expires": None,
                "isExpired": False,
                "isMasterToken": False,
            }
        ]
        result = _make_client(mock_kc).list_tokens()
        assert len(result) == 1
        entry = result[0]
        assert entry.id == "12345"
        assert entry.description == "device 42"
        assert entry.expires is None
        assert entry.is_expired is False
        assert entry.is_master_token is False
        mock_kc.list_tokens.assert_called_once_with()

    def test_secret_never_reaches_the_caller(self) -> None:
        """`create_scoped_token` is the one and only secret reveal."""
        mock_kc = MagicMock()
        mock_kc.list_tokens.return_value = [
            {"id": "1", "description": "master", "token": "1-liveSecretValue"}
        ]
        result = _make_client(mock_kc).list_tokens()
        assert "liveSecretValue" not in str(result[0].model_dump())

    def test_empty_project(self) -> None:
        mock_kc = MagicMock()
        mock_kc.list_tokens.return_value = []
        assert _make_client(mock_kc).list_tokens() == []
