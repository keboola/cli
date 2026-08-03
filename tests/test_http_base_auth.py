"""Tests for the additive ``http_auth`` plumbing across the HTTP clients (0.81.0).

``http_auth`` is the zero-churn seam that lets a programmatic-auth session
(bearer) reuse every existing client without touching the ~150
``(stack_url, token) -> client`` factory call sites. Two properties matter and
are asserted here:

1. **Omitting it changes nothing.** ``http_auth=None`` must be byte-identical
   to the pre-0.81.0 behaviour -- the static token still goes out as
   ``X-StorageApi-Token`` (resp. ``X-KBC-ManageApiToken``).
2. **Setting it removes the static header entirely.** A session-registered
   project's ``token`` is the ``kbc-session://`` sentinel, not a credential;
   sending it as ``X-StorageApi-Token`` would leak a meaningless value and
   produce a confusing 401 instead of a clean bearer call.

The sub-client assertions matter most: queue / query / encryption /
sync-actions are lazily built from copies of the main client's headers
(``client/_core.py``), so a missing ``auth=`` propagation there would silently
leave those four services unauthenticated in bearer mode while Storage worked
fine -- exactly the kind of partial breakage that only shows up in production.
"""

from __future__ import annotations

from collections.abc import Generator

import httpx
import pytest

from keboola_agent_cli.ai_client import AiServiceClient
from keboola_agent_cli.client import KeboolaClient
from keboola_agent_cli.data_science_client import DataScienceClient
from keboola_agent_cli.dev_portal_client import DeveloperPortalClient
from keboola_agent_cli.errors import ErrorCode, SessionAuthUnsupportedError
from keboola_agent_cli.manage_client import ManageClient
from keboola_agent_cli.metastore_client import MetastoreClient
from keboola_agent_cli.scheduler_client import SchedulerClient
from keboola_agent_cli.stream_client import StreamClient

STACK_URL = "https://connection.keboola.com"
QUEUE_URL = "https://queue.keboola.com"
QUERY_URL = "https://query.keboola.com"
ENCRYPT_URL = "https://encryption.keboola.com"
SYNC_ACTIONS_URL = "https://sync-actions.keboola.com"
STATIC_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
SENTINEL_TOKEN = "kbc-session://9840"
BEARER_TOKEN = "kbc_at_fakeAccessTokenForTestsOnly"


class _StubBearerAuth(httpx.Auth):
    """Minimal ``httpx.Auth`` stand-in.

    Deliberately not ``BearerAuth`` from ``auth.token_provider``: these tests
    are about the *plumbing* (is the hook installed and propagated?), not about
    refresh behaviour, which ``test_token_provider.py`` covers.
    """

    def __init__(self, token: str = BEARER_TOKEN, project_id: int | None = None) -> None:
        self._token = token
        self._project_id = project_id

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._token}"
        if self._project_id is not None:
            request.headers["X-KBC-ProjectId"] = str(self._project_id)
        yield request


# ----------------------------------------------------------------------------
# Storage client: static mode is unchanged
# ----------------------------------------------------------------------------


class TestStaticModeUnchanged:
    def test_static_token_header_still_sent(self, httpx_mock) -> None:
        """Without http_auth, the client behaves exactly as before 0.81.0."""
        httpx_mock.add_response(url=f"{STACK_URL}/v2/storage/buckets", json=[])

        with KeboolaClient(stack_url=STACK_URL, token=STATIC_TOKEN) as client:
            client.list_buckets()

        request = httpx_mock.get_requests()[0]
        assert request.headers["X-StorageApi-Token"] == STATIC_TOKEN
        assert "Authorization" not in request.headers

    def test_default_http_auth_is_none(self) -> None:
        """The parameter defaults to None so every existing call site is unaffected."""
        with KeboolaClient(stack_url=STACK_URL, token=STATIC_TOKEN) as client:
            assert client._http_auth is None


# ----------------------------------------------------------------------------
# Storage client: bearer mode
# ----------------------------------------------------------------------------


class TestBearerModeStorage:
    def test_bearer_mode_sends_no_storage_api_token(self, httpx_mock) -> None:
        """The sentinel/placeholder token must never go on the wire as a header."""
        httpx_mock.add_response(url=f"{STACK_URL}/v2/storage/buckets", json=[])

        with KeboolaClient(stack_url=STACK_URL, token="", http_auth=_StubBearerAuth()) as client:
            client.list_buckets()

        request = httpx_mock.get_requests()[0]
        assert "X-StorageApi-Token" not in request.headers
        assert request.headers["Authorization"] == f"Bearer {BEARER_TOKEN}"

    def test_project_id_header_is_stamped(self, httpx_mock) -> None:
        """Session auth binds the project per request via X-KBC-ProjectId."""
        httpx_mock.add_response(url=f"{STACK_URL}/v2/storage/buckets", json=[])

        auth = _StubBearerAuth(project_id=10105)
        with KeboolaClient(stack_url=STACK_URL, token="", http_auth=auth) as client:
            client.list_buckets()

        assert httpx_mock.get_requests()[0].headers["X-KBC-ProjectId"] == "10105"


# ----------------------------------------------------------------------------
# Sub-client propagation
# ----------------------------------------------------------------------------


class TestSubClientAuthPropagation:
    """queue / query / encryption / sync-actions must inherit the bearer hook.

    A regression here would leave those four services silently unauthenticated
    in bearer mode while Storage kept working.
    """

    def test_queue_sub_client_inherits_bearer(self, httpx_mock) -> None:
        httpx_mock.add_response(url=f"{QUEUE_URL}/search/jobs?limit=1&offset=0", json=[])

        with KeboolaClient(stack_url=STACK_URL, token="", http_auth=_StubBearerAuth()) as client:
            client.list_jobs(limit=1)

        request = httpx_mock.get_requests()[0]
        assert request.headers["Authorization"] == f"Bearer {BEARER_TOKEN}"
        assert "X-StorageApi-Token" not in request.headers

    def test_encrypt_sub_client_inherits_bearer(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{ENCRYPT_URL}/encrypt?projectId=123&componentId=keboola.ex-db-snowflake",
            method="POST",
            json={"#password": "KBC::ProjectSecure::abc"},
        )

        with KeboolaClient(stack_url=STACK_URL, token="", http_auth=_StubBearerAuth()) as client:
            client.encrypt_values(
                project_id=123,
                component_id="keboola.ex-db-snowflake",
                data={"#password": "secret"},
            )

        assert httpx_mock.get_requests()[0].headers["Authorization"] == f"Bearer {BEARER_TOKEN}"

    def test_query_sub_client_inherits_bearer(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{QUERY_URL}/api/v1/queries/job-1",
            json={"id": "job-1", "status": "processing"},
        )

        with KeboolaClient(stack_url=STACK_URL, token="", http_auth=_StubBearerAuth()) as client:
            client.get_query_job("job-1")

        request = httpx_mock.get_requests()[0]
        assert request.headers["Authorization"] == f"Bearer {BEARER_TOKEN}"
        assert "X-StorageApi-Token" not in request.headers

    def test_sync_actions_sub_client_inherits_bearer(self, httpx_mock) -> None:
        httpx_mock.add_response(
            url=f"{SYNC_ACTIONS_URL}/actions",
            method="POST",
            json={"status": "success"},
        )

        with KeboolaClient(stack_url=STACK_URL, token="", http_auth=_StubBearerAuth()) as client:
            client.run_sync_action("keboola.ex-db-mysql", "testConnection", {})

        request = httpx_mock.get_requests()[0]
        assert request.headers["Authorization"] == f"Bearer {BEARER_TOKEN}"
        assert "X-StorageApi-Token" not in request.headers

    def test_sub_clients_carry_no_auth_in_static_mode(self, httpx_mock) -> None:
        """Static mode keeps sending the storage token to sub-clients, as before."""
        httpx_mock.add_response(url=f"{QUEUE_URL}/search/jobs?limit=1&offset=0", json=[])

        with KeboolaClient(stack_url=STACK_URL, token=STATIC_TOKEN) as client:
            client.list_jobs(limit=1)

        request = httpx_mock.get_requests()[0]
        assert request.headers["X-StorageApi-Token"] == STATIC_TOKEN
        assert "Authorization" not in request.headers


# ----------------------------------------------------------------------------
# ManageClient parity
# ----------------------------------------------------------------------------


class TestManageClientParity:
    def test_static_mode_sends_manage_token(self, httpx_mock) -> None:
        httpx_mock.add_response(url=f"{STACK_URL}/manage/projects/123", json={"id": 123})

        with ManageClient(stack_url=STACK_URL, manage_token="manage-token") as client:
            client.get_project(123)

        request = httpx_mock.get_requests()[0]
        assert request.headers["X-KBC-ManageApiToken"] == "manage-token"
        assert "Authorization" not in request.headers

    def test_bearer_mode_omits_manage_token_header(self, httpx_mock) -> None:
        httpx_mock.add_response(url=f"{STACK_URL}/manage/projects/123", json={"id": 123})

        with ManageClient(
            stack_url=STACK_URL, manage_token="", http_auth=_StubBearerAuth()
        ) as client:
            client.get_project(123)

        request = httpx_mock.get_requests()[0]
        assert "X-KBC-ManageApiToken" not in request.headers
        assert request.headers["Authorization"] == f"Bearer {BEARER_TOKEN}"


class TestSessionAuthFeatureGuard:
    """``BaseHttpClient.SESSION_AUTH_FEATURE`` guards static-token-only clients.

    Before this, each service's client factory called ``require_static_token``
    itself, so a new factory -- or a direct constructor call like the one in
    ``client/stream.py`` -- silently sent the sentinel instead of failing fast.
    Declaring the feature on the class moves the check to the one place every
    caller must go through.
    """

    def test_guarded_client_rejects_a_sentinel_token(self) -> None:
        for cls in (AiServiceClient, DataScienceClient, MetastoreClient, SchedulerClient):
            with pytest.raises(SessionAuthUnsupportedError) as exc_info:
                cls(stack_url=STACK_URL, token=SENTINEL_TOKEN)
            assert exc_info.value.error_code == ErrorCode.AUTH_NOT_SUPPORTED_ON_STACK
            assert cls.SESSION_AUTH_FEATURE in str(exc_info.value)

        with pytest.raises(SessionAuthUnsupportedError):
            StreamClient(stack_url=STACK_URL, token=SENTINEL_TOKEN)

    def test_guarded_client_accepts_a_static_token(self) -> None:
        for cls in (AiServiceClient, DataScienceClient, MetastoreClient, SchedulerClient):
            with cls(stack_url=STACK_URL, token="123-static"):
                pass
        with StreamClient(stack_url=STACK_URL, token="123-static"):
            pass

    def test_bearer_capable_clients_are_not_guarded(self) -> None:
        """A sentinel must not be *rejected* by clients that support sessions.

        ``KeboolaClient`` / ``ManageClient`` reach Storage and Manage over bearer,
        so guarding them would break the supported path. ``DeveloperPortalClient``
        authenticates with its own identity and never sees a project token.
        """
        assert KeboolaClient.SESSION_AUTH_FEATURE is None
        assert ManageClient.SESSION_AUTH_FEATURE is None
        assert DeveloperPortalClient.SESSION_AUTH_FEATURE is None

    def test_supplying_http_auth_bypasses_the_guard(self) -> None:
        """In session mode the token is empty and the credential is the auth hook.

        The guard must not fire then, or every bearer-mode sub-client would break.
        """
        with StreamClient(stack_url=STACK_URL, token="", http_auth=_StubBearerAuth()):
            pass

    def test_stream_sub_client_inherits_the_bearer_hook(self, httpx_mock) -> None:
        """``client/stream.py`` builds a ``StreamClient`` from the main client.

        Without propagating ``http_auth`` it would send an empty
        ``X-StorageApi-Token`` in session mode and draw an opaque 401.
        """
        httpx_mock.add_response(
            url="https://stream.keboola.com/v1/branches/default/sources",
            json={"sources": []},
        )
        with KeboolaClient(stack_url=STACK_URL, token="", http_auth=_StubBearerAuth()) as client:
            client._get_stream_client().list_sources("default")

        request = httpx_mock.get_requests()[0]
        assert request.headers["Authorization"] == f"Bearer {BEARER_TOKEN}"
        assert not request.headers.get("X-StorageApi-Token")
