"""What `merge-request merge` reports when the merge call fails (DMD-2246).

The merge runs through the real HTTP layer (httpx_mock), so the errors are the
ones it really raises. The merge PUT must be sent exactly once in every case.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.client import KeboolaClient
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.merge_request_service import MergeRequestService, derive_state

ALIAS = "prod"
STACK = "https://connection.keboola.com"
TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
MR = f"{STACK}/v2/storage/merge-request/7"
CONFLICTS = [{"componentId": "c", "configurationId": "1"}]


def _wire(state: str, **extra: Any) -> dict[str, Any]:
    branch = None if state == "published" else 123
    return {"id": 7, "state": state, "branches": {"branchFromId": branch}, **extra}


@pytest.fixture
def store(tmp_path) -> ConfigStore:
    store = ConfigStore(config_dir=tmp_path)
    store.add_project(ALIAS, ProjectConfig(stack_url=STACK, token=TOKEN, project_id=1))
    store.set_project_branch(ALIAS, 123)
    return store


@pytest.fixture(autouse=True)
def _environment(monkeypatch) -> None:
    monkeypatch.setattr("keboola_agent_cli.http_base.time.sleep", lambda *_: None)
    monkeypatch.setattr(KeboolaClient, "has_feature", lambda self, name: True)
    monkeypatch.setattr(
        "keboola_agent_cli.sync.branch_mapping.cleanup_branch_id_from_mapping", lambda _: None
    )


@pytest.fixture
def merge(store):
    service = MergeRequestService(
        config_store=store, client_factory=lambda url, token: KeboolaClient(url, token)
    )
    return lambda: service.merge(ALIAS, 7)


def _puts(httpx_mock) -> int:
    return sum(r.method == "PUT" for r in httpx_mock.get_requests())


def _gets(httpx_mock, url: str) -> int:
    return sum(r.method == "GET" and str(r.url) == url for r in httpx_mock.get_requests())


def _fail_put(httpx_mock, failure: int | Exception) -> None:
    if isinstance(failure, int):
        httpx_mock.add_response(method="PUT", url=f"{MR}/merge", status_code=failure, text="down")
    else:
        httpx_mock.add_exception(failure, method="PUT", url=f"{MR}/merge")


UNCLEAR = [
    pytest.param(503, ErrorCode.API_ERROR, id="503"),
    pytest.param(httpx.ReadTimeout("t"), ErrorCode.TIMEOUT, id="read-timeout"),
    pytest.param(httpx.WriteTimeout("t"), ErrorCode.TIMEOUT, id="write-timeout"),
    pytest.param(httpx.ReadError("reset"), ErrorCode.CONNECTION_ERROR, id="read-error"),
    pytest.param(httpx.WriteError("reset"), ErrorCode.CONNECTION_ERROR, id="write-error"),
    pytest.param(
        httpx.RemoteProtocolError("closed"), ErrorCode.CONNECTION_ERROR, id="protocol-error"
    ),
]


class TestUnclearOutcome:
    @pytest.mark.parametrize(("failure", "code"), UNCLEAR)
    def test_in_merge_is_merge_running(self, merge, store, httpx_mock, failure, code) -> None:
        httpx_mock.add_response(method="GET", url=MR, json=_wire("approved"))
        _fail_put(httpx_mock, failure)
        httpx_mock.add_response(method="GET", url=MR, json=_wire("in_merge"))
        with pytest.raises(KeboolaApiError) as info:
            merge()
        assert info.value.error_code == ErrorCode.MR_MERGE_RUNNING
        assert info.value.retryable is False
        assert info.value.details["state"] == "in_merge"
        assert "no second merge" not in info.value.message
        assert _puts(httpx_mock) == 1
        assert store.get_project(ALIAS).active_branch_id == 123

    @pytest.mark.parametrize(("failure", "code"), UNCLEAR)
    def test_published_is_merged_with_cleanup(
        self, merge, store, httpx_mock, failure, code
    ) -> None:
        httpx_mock.add_response(method="GET", url=MR, json=_wire("approved"))
        _fail_put(httpx_mock, failure)
        httpx_mock.add_response(method="GET", url=MR, json=_wire("published"))
        result = merge()
        assert result["status"] == "already_merged"
        assert result["derived_state"] == "merged"
        assert "earlier call" not in result["message"]
        assert any("merge call failed" in w for w in result["warnings"])
        assert _puts(httpx_mock) == 1
        assert store.get_project(ALIAS).active_branch_id is None

    @pytest.mark.parametrize(("failure", "code"), UNCLEAR)
    def test_other_state_keeps_the_error(self, merge, store, httpx_mock, failure, code) -> None:
        httpx_mock.add_response(method="GET", url=MR, json=_wire("approved"))
        _fail_put(httpx_mock, failure)
        httpx_mock.add_response(method="GET", url=MR, json=_wire("approved"))
        with pytest.raises(KeboolaApiError) as info:
            merge()
        assert info.value.error_code == code
        assert info.value.retryable is False
        assert info.value.details["outcome_unknown"] is True
        assert "could not be confirmed" in info.value.message
        assert _puts(httpx_mock) == 1
        assert store.get_project(ALIAS).active_branch_id == 123

    @pytest.mark.parametrize("read_failure", [403, httpx.ReadError("reset")])
    def test_failed_read_back_keeps_the_error(self, merge, httpx_mock, read_failure) -> None:
        httpx_mock.add_response(method="GET", url=MR, json=_wire("approved"))
        _fail_put(httpx_mock, httpx.ReadTimeout("t"))
        if isinstance(read_failure, int):
            httpx_mock.add_response(method="GET", url=MR, status_code=read_failure, json={})
        else:
            httpx_mock.add_exception(read_failure, method="GET", url=MR)
        with pytest.raises(KeboolaApiError) as info:
            merge()
        assert info.value.error_code == ErrorCode.TIMEOUT
        assert "could not be read back" in info.value.message
        assert _puts(httpx_mock) == 1

    @pytest.mark.parametrize("state", ["approved", "in_merge", "published"])
    def test_read_error_through_the_cli(self, store, tmp_path, httpx_mock, state) -> None:
        httpx_mock.add_response(method="GET", url=MR, json=_wire("approved"))
        _fail_put(httpx_mock, httpx.ReadError("reset"))
        httpx_mock.add_response(method="GET", url=MR, json=_wire(state))
        args = ["--json", "--config-dir", str(tmp_path), "merge-request", "merge"]
        args += ["--project", ALIAS, "--merge-request-id", "7"]
        result = CliRunner().invoke(app, args)
        payload = json.loads(result.stdout)
        if state == "published":
            assert result.exit_code == 0, result.output
            assert payload["data"]["status"] == "already_merged"
        else:
            assert result.exit_code == 4, result.output
            expected = "MR_MERGE_RUNNING" if state == "in_merge" else "CONNECTION_ERROR"
            assert payload["error"]["code"] == expected


class TestLostMergeJob:
    """The merge PUT was accepted (202), then polling its job failed."""

    @pytest.mark.parametrize(
        "poll",
        [503, httpx.ReadTimeout("t"), httpx.ConnectError("x"), httpx.ReadError("reset")],
    )
    @pytest.mark.parametrize("state", ["in_merge", "published", "approved"])
    def test_poll_failure_is_read_back(self, merge, httpx_mock, poll, state) -> None:
        httpx_mock.add_response(method="GET", url=MR, json=_wire("approved"))
        httpx_mock.add_response(
            method="PUT", url=f"{MR}/merge", status_code=202, json={"id": 5, "status": "waiting"}
        )
        job = f"{STACK}/v2/storage/jobs/5"
        if isinstance(poll, int):
            httpx_mock.add_response(url=job, status_code=poll, text="x", is_reusable=True)
        else:
            httpx_mock.add_exception(poll, url=job, is_reusable=True)
        httpx_mock.add_response(method="GET", url=MR, json=_wire(state))
        if state == "published":
            assert merge()["status"] == "already_merged"
        else:
            with pytest.raises(KeboolaApiError) as info:
                merge()
            assert info.value.retryable is False
            assert info.value.details["job_id"] == 5
            if state == "in_merge":
                assert info.value.error_code == ErrorCode.MR_MERGE_RUNNING
            else:
                assert "The merge was started" in info.value.message
        assert _puts(httpx_mock) == 1


class TestKnownOutcomeIsNotReadBack:
    @pytest.mark.parametrize("status", [403, 422])
    def test_4xx(self, merge, httpx_mock, status) -> None:
        httpx_mock.add_response(method="GET", url=MR, json=_wire("approved"))
        httpx_mock.add_response(method="PUT", url=f"{MR}/merge", status_code=status, json={})
        with pytest.raises(KeboolaApiError) as info:
            merge()
        assert info.value.status_code == status
        assert _gets(httpx_mock, MR) == 1

    @pytest.mark.parametrize(
        ("failure", "code"),
        [
            (httpx.ConnectError("x"), ErrorCode.CONNECTION_ERROR),
            (httpx.ConnectTimeout("x"), ErrorCode.TIMEOUT),
        ],
    )
    def test_never_delivered(self, merge, httpx_mock, failure, code) -> None:
        httpx_mock.add_response(method="GET", url=MR, json=_wire("approved"))
        httpx_mock.add_exception(failure, method="PUT", url=f"{MR}/merge", is_reusable=True)
        with pytest.raises(KeboolaApiError) as info:
            merge()
        assert info.value.error_code == code
        assert info.value.retryable is True
        assert _gets(httpx_mock, MR) == 1

    def test_exhausted_429(self, merge, httpx_mock) -> None:
        httpx_mock.add_response(method="GET", url=MR, json=_wire("approved"))
        httpx_mock.add_response(
            method="PUT", url=f"{MR}/merge", status_code=429, json={}, is_reusable=True
        )
        with pytest.raises(KeboolaApiError) as info:
            merge()
        assert info.value.status_code == 429
        assert _gets(httpx_mock, MR) == 1

    @pytest.mark.parametrize("status", ["error", "waiting"])
    def test_job_failure_and_wait_timeout(self, merge, httpx_mock, monkeypatch, status) -> None:
        monkeypatch.setattr("keboola_agent_cli.client.merge_requests.MERGE_JOB_MAX_WAIT", 0)
        httpx_mock.add_response(method="GET", url=MR, json=_wire("approved"))
        httpx_mock.add_response(
            method="PUT", url=f"{MR}/merge", status_code=202, json={"id": 5, "status": status}
        )
        with pytest.raises(KeboolaApiError) as info:
            merge()
        assert info.value.error_code == (
            ErrorCode.STORAGE_JOB_FAILED if status == "error" else ErrorCode.STORAGE_JOB_TIMEOUT
        )
        assert _gets(httpx_mock, MR) == 1


class TestCodeless409:
    def _refuse(self, httpx_mock, body: dict[str, Any]) -> None:
        httpx_mock.add_response(method="GET", url=MR, json=_wire("approved"))
        httpx_mock.add_response(method="PUT", url=f"{MR}/merge", status_code=409, json=body)

    def test_live_conflicts(self, merge, httpx_mock) -> None:
        self._refuse(httpx_mock, {"error": "Conflict"})
        httpx_mock.add_response(method="GET", url=f"{MR}/conflicts", json=CONFLICTS)
        with pytest.raises(KeboolaApiError) as info:
            merge()
        assert info.value.error_code == ErrorCode.MR_MERGE_CONFLICT
        assert info.value.details["api_error_params"]["errors"] == CONFLICTS
        assert _gets(httpx_mock, MR) == 1

    def test_live_conflicts_are_bounded_and_rendered(self, store, tmp_path, httpx_mock) -> None:
        many = [{"componentId": "keboola.ex-db", "configurationId": f"cfg-{i}"} for i in range(500)]
        self._refuse(httpx_mock, {"error": "Conflict"})
        httpx_mock.add_response(method="GET", url=f"{MR}/conflicts", json=many)
        httpx_mock.add_response(method="GET", url=MR, json=_wire("approved"), is_optional=True)
        args = ["--config-dir", str(tmp_path), "merge-request", "merge", "--yes"]
        result = CliRunner().invoke(app, [*args, "--project", ALIAS, "--merge-request-id", "7"])
        assert result.exit_code == 1, result.output
        assert "keboola.ex-db/cfg-0" in result.output
        assert "cfg-499" not in result.output
        assert "list truncated" in result.output

    @pytest.mark.parametrize(
        ("state", "code"),
        [
            ("approved", ErrorCode.MR_NOT_READY_TO_MERGE),
            ("in_merge", ErrorCode.MR_MERGE_RUNNING),
            ("published", None),
        ],
    )
    def test_no_live_conflicts_is_not_ready(self, merge, httpx_mock, state, code) -> None:
        self._refuse(httpx_mock, {"error": "Conflict"})
        httpx_mock.add_response(method="GET", url=f"{MR}/conflicts", json=[])
        httpx_mock.add_response(method="GET", url=MR, json=_wire(state))
        if code is None:
            assert merge()["status"] == "already_merged"
        else:
            with pytest.raises(KeboolaApiError) as info:
                merge()
            assert info.value.error_code == code
            assert "api_error_code" not in info.value.details
        assert _gets(httpx_mock, f"{MR}/conflicts") == 1
        assert _gets(httpx_mock, MR) == 2

    def test_failed_conflicts_read_stays_a_conflict(self, merge, httpx_mock) -> None:
        self._refuse(httpx_mock, {"error": "Conflict"})
        httpx_mock.add_response(method="GET", url=f"{MR}/conflicts", status_code=403, json={})
        with pytest.raises(KeboolaApiError) as info:
            merge()
        assert info.value.error_code == ErrorCode.MR_MERGE_CONFLICT

    @pytest.mark.parametrize(
        ("body", "code"),
        [
            ({"params": {"errors": CONFLICTS}}, ErrorCode.MR_MERGE_CONFLICT),
            ({"code": "storage.mergeRequests.validation"}, ErrorCode.MR_MERGE_CONFLICT),
            ({"code": "storage.other"}, None),
        ],
    )
    def test_listed_or_coded_409_reads_nothing(self, merge, httpx_mock, body, code) -> None:
        self._refuse(httpx_mock, {"error": "Conflict", **body})
        with pytest.raises(KeboolaApiError) as info:
            merge()
        if code:
            assert info.value.error_code == code
        else:
            assert info.value.error_code != ErrorCode.MR_MERGE_CONFLICT
        assert _gets(httpx_mock, f"{MR}/conflicts") == 0


class TestDerivedStateUnknown:
    @pytest.mark.parametrize("mr", [{"state": "future"}, {}, {"state": None}])
    def test_unknown_raw_state(self, mr) -> None:
        assert derive_state(mr) == "unknown"

    def test_unknown_server_value_falls_back(self) -> None:
        assert derive_state({"state": "approved", "derivedState": "future"}) == "approved"
        assert derive_state({"state": "future", "derivedState": "rejected"}) == "rejected"


def test_merge_job_results_of_the_new_connection(merge, httpx_mock) -> None:
    """DMD-1988 T1-T3: the job results carry derivedState and mergeBlockers, not viewer/allowedActions."""
    httpx_mock.add_response(method="GET", url=MR, json=_wire("approved"))
    results = _wire("published", derivedState="merged", mergeBlockers=["state"])
    httpx_mock.add_response(
        method="PUT",
        url=f"{MR}/merge",
        status_code=202,
        json={"id": 5, "status": "success", "results": results},
    )
    result = merge()
    assert result["derived_state"] == "merged"
    assert result["allowed_actions"] == []
    assert "status" not in result
