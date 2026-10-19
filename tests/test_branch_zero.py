"""`--branch 0` means production in the commands that build a branch path or match a branch ID.

``effective_branch.resolve_branch`` returns 0 for an explicit production
choice, and the Storage client paths send a falsy branch ID to the production
endpoint. These commands used 0 as a real branch ID instead:

- ``job terminate --status ... --branch 0`` compared each job's ``branchId``
  with "0", which no job has, so it terminated nothing;
- ``storage swap-tables --branch 0`` refused with "requires a branch" (and the
  service called ``/v2/storage/branch/0/...`` for a REST caller);
- ``storage clone-table --branch 0`` refused with a config error that asked
  for ``--branch`` (and the service called ``/v2/storage/branch/0/.../pull``);
- ``storage create-table --source-branch-id 0`` sent ``source.branchId: 0``.

The CLI tests run the real command, service and HTTP client against
pytest-httpx, with an active branch set, so they also prove that ``--branch 0``
wins over the active branch.
"""

import importlib.util
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner, Result

from helpers import assert_api_calls, mock_api_routes, setup_single_project
from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.constants import ENV_AUTO_UPDATE
from keboola_agent_cli.errors import ConfigError, ErrorCode, KeboolaApiError
from keboola_agent_cli.services.job_service import JobService
from keboola_agent_cli.services.storage_service import StorageService

runner = CliRunner()

DEFAULT_BRANCH_ID = 100
DEV_BRANCH_ID = 456
DEV_BRANCHES = [
    {"id": DEFAULT_BRANCH_ID, "name": "Main", "isDefault": True},
    {"id": DEV_BRANCH_ID, "name": "feature", "isDefault": False},
]
DEV_BRANCHES_CALL = ("GET", "/v2/storage/dev-branches")


@pytest.fixture(autouse=True)
def _no_auto_update(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the startup auto-update check out of the recorded calls."""
    monkeypatch.setenv(ENV_AUTO_UPDATE, "false")


def _store_with_active_branch(config_dir: Path) -> ConfigStore:
    store = setup_single_project(config_dir)
    config = store.load()
    config.projects["prod"].active_branch_id = DEV_BRANCH_ID
    store.save(config)
    return store


def _invoke(config_dir: Path, *args: str) -> Result:
    return runner.invoke(app, ["--config-dir", str(config_dir), "--json", *args])


def _service_client() -> MagicMock:
    client = MagicMock()
    client.list_dev_branches.return_value = DEV_BRANCHES
    return client


def _job(job_id: str, branch_id: str | None) -> dict[str, Any]:
    return {"id": job_id, "branchId": branch_id, "status": "processing", "isFinished": False}


JOBS = [_job("1", str(DEFAULT_BRANCH_ID)), _job("2", str(DEV_BRANCH_ID)), _job("3", None)]


needs_server = pytest.mark.skipif(
    importlib.util.find_spec("fastapi") is None, reason="needs the server extra"
)


def _post_rest(config_dir: Path, path: str, body: dict[str, Any], **services: Any) -> Any:
    """POST to a `kbagent serve` route whose registry holds the given real services."""
    from fastapi.testclient import TestClient

    from keboola_agent_cli.server import create_app
    from keboola_agent_cli.server.dependencies import ServiceRegistry, get_registry

    registry = ServiceRegistry.__new__(ServiceRegistry)
    for name, service in services.items():
        setattr(registry, name, service)
    rest = create_app(config_dir=str(config_dir), auth_token="test-token")
    rest.dependency_overrides[get_registry] = lambda: registry
    with TestClient(rest) as client:
        return client.post(path, json=body, headers={"Authorization": "Bearer test-token"})


class TestJobTerminateBranchZero:
    def test_service_matches_the_default_branch_id(self, tmp_config_dir: Path) -> None:
        client = _service_client()
        client.list_jobs.return_value = JOBS
        service = JobService(setup_single_project(tmp_config_dir), lambda url, token: client)

        jobs = service.resolve_job_ids_by_filter("prod", status="processing", branch_id=0)

        # A job without a branchId is not production for sure, so it is not matched.
        assert [j["id"] for j in jobs] == ["1"]
        client.list_dev_branches.assert_called_once_with()

    def test_service_keeps_a_dev_branch_filter_unchanged(self, tmp_config_dir: Path) -> None:
        client = _service_client()
        client.list_jobs.return_value = JOBS
        service = JobService(setup_single_project(tmp_config_dir), lambda url, token: client)

        jobs = service.resolve_job_ids_by_filter("prod", branch_id=DEV_BRANCH_ID)

        assert [j["id"] for j in jobs] == ["2"]
        client.list_dev_branches.assert_not_called()

    def test_service_fails_when_the_project_reports_no_default_branch(
        self, tmp_config_dir: Path
    ) -> None:
        client = _service_client()
        client.list_jobs.return_value = JOBS
        client.list_dev_branches.return_value = [{"id": DEV_BRANCH_ID, "isDefault": False}]
        service = JobService(setup_single_project(tmp_config_dir), lambda url, token: client)

        with pytest.raises(ConfigError, match="reports no default branch"):
            service.resolve_job_ids_by_filter("prod", branch_id=0)

    def test_cli_branch_zero_selects_production_jobs(
        self, tmp_config_dir: Path, httpx_mock
    ) -> None:
        _store_with_active_branch(tmp_config_dir)
        mock_api_routes(
            httpx_mock, {("GET", "/search/jobs"): JOBS, DEV_BRANCHES_CALL: DEV_BRANCHES}
        )

        result = _invoke(
            tmp_config_dir,
            *("job", "terminate", "--project", "prod", "--status", "processing"),
            *("--branch", "0", "--dry-run"),
        )

        assert result.exit_code == 0, result.output
        envelope = json.loads(result.output)
        assert envelope["data"]["would_terminate"] == ["1"]
        assert envelope["targets"][0]["branch_source"] == "production"
        assert envelope["targets"][0]["branch_id"] == DEFAULT_BRANCH_ID

    def test_cli_branch_zero_kills_only_default_branch_jobs(
        self, tmp_config_dir: Path, httpx_mock
    ) -> None:
        _store_with_active_branch(tmp_config_dir)
        kill_one = ("POST", "/jobs/1/kill")
        mock_api_routes(
            httpx_mock,
            {
                ("GET", "/search/jobs"): JOBS,
                DEV_BRANCHES_CALL: DEV_BRANCHES,
                kill_one: {"id": "1", "status": "processing", "desiredStatus": "terminating"},
            },
        )

        result = _invoke(
            tmp_config_dir,
            *("job", "terminate", "--project", "prod", "--status", "processing"),
            *("--branch", "0", "--yes"),
        )

        assert result.exit_code == 0, result.output
        assert [k["id"] for k in json.loads(result.output)["data"]["killed"]] == ["1"]
        assert_api_calls(httpx_mock, [("GET", "/search/jobs"), DEV_BRANCHES_CALL, kill_one])

    def test_cli_no_default_branch_is_a_config_error(
        self, tmp_config_dir: Path, httpx_mock
    ) -> None:
        setup_single_project(tmp_config_dir)
        mock_api_routes(
            httpx_mock,
            {("GET", "/search/jobs"): JOBS, DEV_BRANCHES_CALL: [{"id": 7, "isDefault": False}]},
        )

        result = _invoke(
            tmp_config_dir,
            *("job", "terminate", "--project", "prod", "--status", "processing"),
            *("--branch", "0", "--yes"),
        )

        assert result.exit_code == 5, result.output
        error = json.loads(result.output)["error"]
        assert error["code"] == ErrorCode.CONFIG_ERROR
        assert "kbagent branch list" in error["message"]

    @needs_server
    def test_rest_branch_zero_kills_only_default_branch_jobs(self, tmp_config_dir: Path) -> None:
        client = _service_client()
        client.list_jobs.return_value = JOBS
        client.kill_job.return_value = {"status": "processing", "desiredStatus": "terminating"}
        service = JobService(setup_single_project(tmp_config_dir), lambda url, token: client)

        response = _post_rest(
            tmp_config_dir,
            "/jobs/prod/terminate",
            {"status": "processing", "branch_id": 0},
            job=service,
        )

        assert response.status_code == 200, response.text
        client.kill_job.assert_called_once_with("1")


class TestSwapTablesBranchZero:
    def test_service_swaps_on_the_default_branch_id(self, tmp_config_dir: Path) -> None:
        client = _service_client()
        client.swap_tables.return_value = {"status": "success"}
        service = StorageService(setup_single_project(tmp_config_dir), lambda url, token: client)

        result = service.swap_tables("prod", "in.c-foo.a", "in.c-foo.b", branch_id=0)

        assert client.swap_tables.call_args.kwargs["branch_id"] == DEFAULT_BRANCH_ID
        assert result["branch_id"] == DEFAULT_BRANCH_ID

    def test_cli_branch_zero_swaps_in_production(self, tmp_config_dir: Path, httpx_mock) -> None:
        _store_with_active_branch(tmp_config_dir)
        swap_path = f"/v2/storage/branch/{DEFAULT_BRANCH_ID}/tables/in.c-foo.a/swap"
        mock_api_routes(
            httpx_mock,
            {
                DEV_BRANCHES_CALL: DEV_BRANCHES,
                ("POST", swap_path): {"id": 1, "status": "success", "operationName": "tableSwap"},
            },
        )

        result = _invoke(
            tmp_config_dir,
            *("storage", "swap-tables", "--project", "prod"),
            *("--table-id", "in.c-foo.a", "--target-table-id", "in.c-foo.b"),
            *("--branch", "0", "--yes"),
        )

        assert result.exit_code == 0, result.output
        assert_api_calls(httpx_mock, [DEV_BRANCHES_CALL, ("POST", swap_path)])
        envelope = json.loads(result.output)
        assert envelope["data"]["branch_id"] == DEFAULT_BRANCH_ID
        assert envelope["targets"][0]["branch_source"] == "production"

    def test_cli_dry_run_makes_no_call(self, tmp_config_dir: Path, httpx_mock) -> None:
        _store_with_active_branch(tmp_config_dir)
        mock_api_routes(httpx_mock, {})

        result = _invoke(
            tmp_config_dir,
            *("storage", "swap-tables", "--project", "prod"),
            *("--table-id", "in.c-foo.a", "--target-table-id", "in.c-foo.b"),
            *("--branch", "0", "--dry-run"),
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["branch_id"] == 0
        assert_api_calls(httpx_mock, [])

    @needs_server
    def test_rest_branch_zero_swaps_on_the_default_branch_id(self, tmp_config_dir: Path) -> None:
        client = _service_client()
        client.swap_tables.return_value = {"status": "success"}
        service = StorageService(setup_single_project(tmp_config_dir), lambda url, token: client)

        response = _post_rest(
            tmp_config_dir,
            "/storage/tables/prod/in.c-foo.a/swap",
            {"target_table_id": "in.c-foo.b", "branch_id": 0},
            storage=service,
        )

        assert response.status_code == 200, response.text
        assert client.swap_tables.call_args.kwargs["branch_id"] == DEFAULT_BRANCH_ID


class TestCloneTableBranchZero:
    def test_service_refuses_production_before_any_call(self, tmp_config_dir: Path) -> None:
        factory = MagicMock()
        service = StorageService(setup_single_project(tmp_config_dir), factory)

        with pytest.raises(KeboolaApiError) as exc_info:
            service.clone_table("prod", "in.c-foo.a", branch_id=0)

        assert exc_info.value.error_code == ErrorCode.INVALID_ARGUMENT
        assert "must be a dev branch" in exc_info.value.message
        factory.assert_not_called()

    def test_cli_branch_zero_is_a_usage_error(self, tmp_config_dir: Path, httpx_mock) -> None:
        _store_with_active_branch(tmp_config_dir)
        mock_api_routes(httpx_mock, {})

        result = _invoke(
            tmp_config_dir,
            *("storage", "clone-table", "--project", "prod", "--table-id", "in.c-foo.a"),
            *("--branch", "0"),
        )

        assert result.exit_code == 2, result.output
        error = json.loads(result.output)["error"]
        assert error["code"] == ErrorCode.INVALID_ARGUMENT
        assert "must be a dev branch" in error["message"]
        assert_api_calls(httpx_mock, [])

    @needs_server
    def test_rest_branch_zero_is_a_bad_request(self, tmp_config_dir: Path) -> None:
        factory = MagicMock()
        service = StorageService(setup_single_project(tmp_config_dir), factory)

        response = _post_rest(
            tmp_config_dir,
            "/storage/tables/prod/in.c-foo.a/pull",
            {"branch_id": 0},
            storage=service,
        )

        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == ErrorCode.INVALID_ARGUMENT
        factory.assert_not_called()


class TestCreateTableSourceBranchZero:
    @staticmethod
    def _create(service: StorageService, branch_id: int | None) -> None:
        service.create_table(
            alias="prod",
            bucket_id="in.c-foo",
            name="a_repart",
            columns=[],
            branch_id=branch_id,
            source_table_id="in.c-foo.a",
            source_branch_id=0,
        )

    @staticmethod
    def _bigquery_client() -> MagicMock:
        client = _service_client()
        client.verify_token.return_value.default_backend = "bigquery"
        client.create_table.return_value = {"id": "in.c-foo.a_repart", "columns": ["id"]}
        return client

    def test_production_request_omits_the_source_branch(self, tmp_config_dir: Path) -> None:
        client = self._bigquery_client()
        service = StorageService(setup_single_project(tmp_config_dir), lambda url, token: client)

        self._create(service, branch_id=None)

        assert client.create_table.call_args.kwargs["source"] == {"tableId": "in.c-foo.a"}
        client.list_dev_branches.assert_not_called()

    def test_dev_branch_request_sends_the_default_branch_id(self, tmp_config_dir: Path) -> None:
        client = self._bigquery_client()
        service = StorageService(setup_single_project(tmp_config_dir), lambda url, token: client)

        self._create(service, branch_id=DEV_BRANCH_ID)

        assert client.create_table.call_args.kwargs["source"] == {
            "tableId": "in.c-foo.a",
            "branchId": DEFAULT_BRANCH_ID,
        }

    def test_cli_source_branch_zero_in_a_dev_branch(self, tmp_config_dir: Path, httpx_mock) -> None:
        _store_with_active_branch(tmp_config_dir)
        branch_path = f"/v2/storage/branch/{DEV_BRANCH_ID}"
        create_path = f"{branch_path}/buckets/in.c-foo/tables-definition"
        verify = {"id": "1", "owner": {"id": 258, "name": "P", "defaultBackend": "bigquery"}}
        mock_api_routes(
            httpx_mock,
            {
                DEV_BRANCHES_CALL: DEV_BRANCHES,
                ("GET", "/v2/storage/tokens/verify"): verify,
                ("GET", f"{branch_path}/buckets/in.c-foo"): {"id": "in.c-foo"},
                ("POST", create_path): {"id": 7, "status": "success", "results": {"id": "x"}},
            },
        )

        result = _invoke(
            tmp_config_dir,
            *("storage", "create-table", "--project", "prod", "--bucket-id", "in.c-foo"),
            *("--name", "a_repart", "--source-table-id", "in.c-foo.a", "--source-branch-id", "0"),
        )
        assert result.exit_code == 0, result.output
        create = [r for r in httpx_mock.get_requests() if r.url.path == create_path]
        source = json.loads(create[0].content)["source"]
        assert source == {"tableId": "in.c-foo.a", "branchId": DEFAULT_BRANCH_ID}
        source_target = json.loads(result.output)["targets"][0]
        assert (source_target["role"], source_target["branch_source"]) == ("source", "production")
        assert source_target["branch_id"] == DEFAULT_BRANCH_ID

    def test_cli_source_branch_zero_in_production(self, tmp_config_dir: Path, httpx_mock) -> None:
        setup_single_project(tmp_config_dir)
        create_path = "/v2/storage/buckets/in.c-foo/tables-definition"
        mock_api_routes(httpx_mock, self._production_routes(create_path))

        result = _invoke(
            tmp_config_dir,
            *("storage", "create-table", "--project", "prod", "--bucket-id", "in.c-foo"),
            *("--name", "a_repart", "--source-table-id", "in.c-foo.a", "--source-branch-id", "0"),
        )

        assert result.exit_code == 0, result.output
        create = [r for r in httpx_mock.get_requests() if r.url.path == create_path]
        assert json.loads(create[0].content)["source"] == {"tableId": "in.c-foo.a"}
        assert DEV_BRANCHES_CALL not in [(r.method, r.url.path) for r in httpx_mock.get_requests()]

    def test_human_mode_prints_a_production_source_line(
        self, tmp_config_dir: Path, httpx_mock
    ) -> None:
        setup_single_project(tmp_config_dir)
        mock_api_routes(
            httpx_mock, self._production_routes("/v2/storage/buckets/in.c-foo/tables-definition")
        )

        result = runner.invoke(
            app,
            [
                *("--config-dir", str(tmp_config_dir), "storage", "create-table"),
                *("--project", "prod", "--bucket-id", "in.c-foo", "--name", "a_repart"),
                *("--source-table-id", "in.c-foo.a", "--source-branch-id", "0"),
            ],
        )

        assert result.exit_code == 0, result.output
        # Before, --source-branch-id 0 recorded no source, so no Source: line was printed.
        assert "Source: project 'prod', production" in result.stderr

    @staticmethod
    def _production_routes(create_path: str) -> dict[Any, Any]:
        verify = {"id": "1", "owner": {"id": 258, "name": "P", "defaultBackend": "bigquery"}}
        return {
            ("GET", "/v2/storage/tokens/verify"): verify,
            ("GET", "/v2/storage/buckets/in.c-foo"): {"id": "in.c-foo"},
            ("POST", create_path): {"id": 7, "status": "success", "results": {"id": "x"}},
        }
