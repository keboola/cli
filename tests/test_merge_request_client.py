"""Tests for the merge-request client family (client/merge_requests.py + configs.py).

Pins the wire contract from docs/merge-requests-layer3-rfc.md (Testing
section): path construction (MR paths never branch-prefixed, diff/rebase
always), JSON encoding (the surrounding configs.py teaches form encoding --
the opposite), presence detection, the ``diff`` envelope, the empty-object
delete resolution, implicit merge-job waiting, ``include=activityLog``, and
the namespace-never-touches-the-client seam.
"""

import json
from typing import Any

import httpx
import pytest

from keboola_agent_cli.client import KeboolaClient
from keboola_agent_cli.client.merge_requests import MergeRequests
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError

STACK_URL = "https://connection.keboola.com"
TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
MR_BASE = f"{STACK_URL}/v2/storage/merge-request"

SAMPLE_MR = {
    "id": 42,
    "title": "Promote revenue pipeline",
    "state": "development",
    "branches": {"branchFromId": 123, "branchIntoId": 1},
}


@pytest.fixture
def client():
    c = KeboolaClient(stack_url=STACK_URL, token=TOKEN)
    yield c
    c.close()


def _sent_json(request: httpx.Request) -> Any:
    """Decode a captured request body, asserting it is JSON-encoded."""
    assert request.headers["Content-Type"] == "application/json"
    return json.loads(request.content)


class TestMergeRequestPaths:
    """MR endpoints are project-level -- never branch-prefixed."""

    def test_list_hits_bare_path(self, client, httpx_mock) -> None:
        """list() hits /v2/storage/merge-request with no branch prefix and no params."""
        httpx_mock.add_response(url=MR_BASE, json=[SAMPLE_MR])

        result = client.merge_requests.list()

        assert result == [SAMPLE_MR]
        request = httpx_mock.get_requests()[0]
        assert request.url.path == "/v2/storage/merge-request"
        assert request.url.query == b""

    def test_get_hits_id_path(self, client, httpx_mock) -> None:
        """get() hits /merge-request/{id} without include by default."""
        httpx_mock.add_response(url=f"{MR_BASE}/42", json=SAMPLE_MR)

        result = client.merge_requests.get(42)

        assert result == SAMPLE_MR
        request = httpx_mock.get_requests()[0]
        assert request.url.path == "/v2/storage/merge-request/42"
        assert request.url.query == b""

    def test_get_include_activity_log_only_when_asked(self, client, httpx_mock) -> None:
        """include=activityLog is present exactly when include_activity_log=True."""
        httpx_mock.add_response(url=f"{MR_BASE}/42?include=activityLog", json=SAMPLE_MR)

        client.merge_requests.get(42, include_activity_log=True)

        request = httpx_mock.get_requests()[0]
        assert request.url.params["include"] == "activityLog"

    def test_conflicts_path(self, client, httpx_mock) -> None:
        """conflicts() hits /merge-request/{id}/conflicts."""
        httpx_mock.add_response(url=f"{MR_BASE}/42/conflicts", json=[])

        result = client.merge_requests.conflicts(42)

        assert result == []
        assert httpx_mock.get_requests()[0].url.path == "/v2/storage/merge-request/42/conflicts"


class TestCreateAndUpdateBodies:
    """JSON encoding + presence detection on the two body-carrying writes."""

    def test_create_sends_real_json_types(self, client, httpx_mock) -> None:
        """Branch ids go as JSON numbers, reviewerIds as an int array, not strings."""
        httpx_mock.add_response(url=MR_BASE, json=SAMPLE_MR, status_code=201)

        client.merge_requests.create(
            branch_from_id=123,
            branch_into_id=1,
            title="Promote revenue pipeline",
            description="Q3 changes",
            reviewer_ids=[7, 9],
        )

        body = _sent_json(httpx_mock.get_requests()[0])
        assert body["branchFromId"] == 123
        assert body["branchIntoId"] == 1
        assert isinstance(body["branchFromId"], int)
        assert isinstance(body["branchIntoId"], int)
        assert body["title"] == "Promote revenue pipeline"
        assert body["description"] == "Q3 changes"
        assert body["reviewerIds"] == [7, 9]

    def test_create_omits_unset_optionals(self, client, httpx_mock) -> None:
        """Unset optionals are absent from the body, not sent as null."""
        httpx_mock.add_response(url=MR_BASE, json=SAMPLE_MR, status_code=201)

        client.merge_requests.create(branch_from_id=123, branch_into_id=1, title="T")

        body = _sent_json(httpx_mock.get_requests()[0])
        assert set(body) == {"branchFromId", "branchIntoId", "title"}

    def test_create_sends_auto_merge_and_external_id(self, client, httpx_mock) -> None:
        """autoMergeStrategy / autoMergeAt / externalId pass through verbatim."""
        httpx_mock.add_response(url=MR_BASE, json=SAMPLE_MR, status_code=201)

        client.merge_requests.create(
            branch_from_id=123,
            branch_into_id=1,
            title="T",
            auto_merge_strategy="scheduled",
            auto_merge_at="2026-09-01T06:00:00+00:00",
            external_id="DMD-1701",
        )

        body = _sent_json(httpx_mock.get_requests()[0])
        assert body["autoMergeStrategy"] == "scheduled"
        assert body["autoMergeAt"] == "2026-09-01T06:00:00+00:00"
        assert body["externalId"] == "DMD-1701"

    def test_update_sends_only_provided_fields(self, client, httpx_mock) -> None:
        """update() omits unset fields; provided ones go as real JSON types."""
        httpx_mock.add_response(url=f"{MR_BASE}/42", json=SAMPLE_MR)

        client.merge_requests.update(42, title="New title", reviewer_ids=[7])

        request = httpx_mock.get_requests()[0]
        assert request.method == "PUT"
        body = _sent_json(request)
        assert body == {"title": "New title", "reviewerIds": [7]}


class TestStateTransitions:
    """request-review / approve / request-changes."""

    def test_request_review(self, client, httpx_mock) -> None:
        """request_review() PUTs the request-review path with no body."""
        httpx_mock.add_response(url=f"{MR_BASE}/42/request-review", json=SAMPLE_MR)

        result = client.merge_requests.request_review(42)

        assert result == SAMPLE_MR
        request = httpx_mock.get_requests()[0]
        assert request.method == "PUT"
        assert request.content == b""

    def test_approve(self, client, httpx_mock) -> None:
        """approve() PUTs the approve path with no body."""
        httpx_mock.add_response(url=f"{MR_BASE}/42/approve", json=SAMPLE_MR)

        result = client.merge_requests.approve(42)

        assert result == SAMPLE_MR
        assert httpx_mock.get_requests()[0].method == "PUT"

    def test_request_changes_with_reason(self, client, httpx_mock) -> None:
        """request_changes() sends {"reason": ...} as JSON when given."""
        httpx_mock.add_response(url=f"{MR_BASE}/42/request-changes", json=SAMPLE_MR)

        client.merge_requests.request_changes(42, reason="Please split the flow")

        body = _sent_json(httpx_mock.get_requests()[0])
        assert body == {"reason": "Please split the flow"}

    def test_request_changes_without_reason_sends_empty_object(self, client, httpx_mock) -> None:
        """request_changes() without a reason sends {} (reason omitted, not null)."""
        httpx_mock.add_response(url=f"{MR_BASE}/42/request-changes", json=SAMPLE_MR)

        client.merge_requests.request_changes(42)

        assert _sent_json(httpx_mock.get_requests()[0]) == {}


class TestMerge:
    """merge() awaits the Storage job implicitly (RFC, D3)."""

    def test_merge_waits_for_job_and_returns_completed_job(
        self, client, httpx_mock, monkeypatch
    ) -> None:
        """202's job is polled to success; the completed job dict is returned."""
        monkeypatch.setattr("keboola_agent_cli.client._core.time.sleep", lambda _: None)
        httpx_mock.add_response(
            url=f"{MR_BASE}/42/merge",
            json={"id": 555, "status": "waiting"},
            status_code=202,
        )
        completed = {"id": 555, "status": "success", "results": SAMPLE_MR}
        httpx_mock.add_response(url=f"{STACK_URL}/v2/storage/jobs/555", json=completed)

        result = client.merge_requests.merge(42)

        assert result == completed
        assert result["results"] == SAMPLE_MR
        merge_request = httpx_mock.get_requests()[0]
        assert merge_request.method == "PUT"
        assert merge_request.url.path == "/v2/storage/merge-request/42/merge"

    def test_merge_failed_job_raises_storage_job_failed(
        self, client, httpx_mock, monkeypatch
    ) -> None:
        """A failed merge job surfaces STORAGE_JOB_FAILED from the shared helper."""
        monkeypatch.setattr("keboola_agent_cli.client._core.time.sleep", lambda _: None)
        httpx_mock.add_response(
            url=f"{MR_BASE}/42/merge",
            json={"id": 555, "status": "waiting"},
            status_code=202,
        )
        httpx_mock.add_response(
            url=f"{STACK_URL}/v2/storage/jobs/555",
            json={"id": 555, "status": "error", "error": {"message": "merge conflict"}},
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            client.merge_requests.merge(42)

        assert exc_info.value.error_code == ErrorCode.STORAGE_JOB_FAILED
        assert "merge conflict" in str(exc_info.value)


class TestConfigDiff:
    """get_config_diff -- branch-prefixed, branch_id required (RFC, D5)."""

    def test_diff_path_is_branch_prefixed(self, client, httpx_mock) -> None:
        """diff always hits /v2/storage/branch/{id}/... -- no production fallback."""
        diff = {"base": None, "ours": {"version": 3}, "theirs": {"version": 7}}
        httpx_mock.add_response(
            url=f"{STACK_URL}/v2/storage/branch/123/components/keboola.ex-http/configs/cfg-1/diff",
            json=diff,
        )

        result = client.get_config_diff("keboola.ex-http", "cfg-1", branch_id=123)

        assert result == diff

    def test_diff_quotes_component_and_config_ids(self, client, httpx_mock) -> None:
        """Component/config ids are percent-encoded like everywhere in configs.py."""
        httpx_mock.add_response(
            url=f"{STACK_URL}/v2/storage/branch/123/components/vendor%2Fapp/configs/c%2F1/diff",
            json={},
        )

        client.get_config_diff("vendor/app", "c/1", branch_id=123)

        assert len(httpx_mock.get_requests()) == 1


class TestRebaseEnvelope:
    """The diff envelope -- version at the top level, content inside diff."""

    REBASE_URL = (
        f"{STACK_URL}/v2/storage/branch/123/components/keboola.ex-http/configs/cfg-1/rebase"
    )

    def test_keep_rebase_builds_the_envelope(self, client, httpx_mock) -> None:
        """version stays top-level; name/rows/optionals live inside diff; JSON types."""
        httpx_mock.add_response(url=self.REBASE_URL, json={"id": "cfg-1", "version": 8})

        client.rebase_config(
            "keboola.ex-http",
            "cfg-1",
            branch_id=123,
            version=7,
            name="My config",
            rows=[{"id": "r1", "name": "Row 1"}],
            configuration={"parameters": {"baseUrl": "https://example.com"}},
            description="resolved",
            change_description="rebase onto v7",
            is_disabled=False,
        )

        body = _sent_json(httpx_mock.get_requests()[0])
        assert body["version"] == 7
        assert isinstance(body["version"], int)
        assert set(body) == {"version", "diff"}, "nothing content-like at the top level"
        diff = body["diff"]
        assert diff["name"] == "My config"
        assert diff["rows"] == [{"id": "r1", "name": "Row 1"}]
        assert diff["configuration"] == {"parameters": {"baseUrl": "https://example.com"}}
        assert isinstance(diff["configuration"], dict), "real nested JSON, not a dumped string"
        assert diff["description"] == "resolved"
        assert diff["changeDescription"] == "rebase onto v7"
        assert diff["isDisabled"] is False, "is_disabled=False is sent, as a JSON boolean"

    def test_keep_rebase_omits_unset_optionals_and_sends_empty_rows(
        self, client, httpx_mock
    ) -> None:
        """is_disabled=None is omitted; rows=[] is sent (it deletes all rows)."""
        httpx_mock.add_response(url=self.REBASE_URL, json={})

        client.rebase_config(
            "keboola.ex-http", "cfg-1", branch_id=123, version=7, name="My config", rows=[]
        )

        body = _sent_json(httpx_mock.get_requests()[0])
        assert body["diff"] == {"name": "My config", "rows": []}

    def test_delete_rebase_sends_empty_diff_object(self, client, httpx_mock) -> None:
        """Delete resolution is exactly {"version": N, "diff": {}} -- diff a JSON object."""
        httpx_mock.add_response(url=self.REBASE_URL, json={})

        client.rebase_config_delete("keboola.ex-http", "cfg-1", branch_id=123, version=7)

        request = httpx_mock.get_requests()[0]
        assert request.content == b'{"version": 7, "diff": {}}' or _sent_json(request) == {
            "version": 7,
            "diff": {},
        }
        # Pin the empty-object serialisation explicitly: null / "" / [] would
        # all be a malformed-diff 400 (or a silent behaviour change) server-side.
        assert b'"diff": {}' in request.content or b'"diff":{}' in request.content


class TestRequesterSeam:
    """The namespace depends on the StorageRequester Protocol, not the client."""

    def test_namespace_works_against_a_stub_requester(self) -> None:
        """A stub Protocol implementation is all MergeRequests needs -- no HTTP client."""
        calls: list[tuple[str, str]] = []

        class StubRequester:
            def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
                calls.append((method, path))
                return httpx.Response(200, json=[SAMPLE_MR], request=httpx.Request(method, path))

            def wait_for_storage_job(self, job: dict[str, Any]) -> dict[str, Any]:
                return job

        namespace = MergeRequests(StubRequester())

        assert namespace.list() == [SAMPLE_MR]
        assert calls == [("GET", "/v2/storage/merge-request")]

    def test_namespace_is_cached_on_the_client(self) -> None:
        """client.merge_requests returns the same namespace instance every time."""
        client = KeboolaClient(stack_url=STACK_URL, token=TOKEN)
        try:
            assert client.merge_requests is client.merge_requests
        finally:
            client.close()
