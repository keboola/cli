"""Ratcheting API call-count tests for hot read commands (issue #802).

Each test runs a real command end-to-end -- Typer ``CliRunner`` -> command ->
service -> real HTTP client -- against pytest-httpx, then asserts the EXACT
number and sequence of ``(METHOD, path)`` calls it made. The point is to catch
N+1 regressions (a per-item lookup sneaking into a list loop) and duplicate
calls before they reach users as slow commands and rate-limit pressure.

THE EXPECTED CALL LISTS ARE A RATCHET. They are committed literals, not
computed from the fixture data. A change that makes a command issue MORE
calls must edit the literal here, which puts the increase in front of a
reviewer on purpose -- justify it in the PR. A change that makes a command
issue FEWER calls should lower the literal in the same PR so the win cannot
silently regress.

Size-parametrized tests (1 vs 10 items) pin the design property that a list
command's call count does NOT grow with the number of items it returns.

Routing ignores host and query (see ``helpers.mock_api_routes``); where the
query string is part of the contract (job list sorting/paging) the assertion
uses ``include_query=True``. Multi-project fan-out runs on a thread pool, so
those assertions compare sorted multisets (``ordered=False``).
"""

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from helpers import (
    assert_api_calls,
    mock_api_routes,
    setup_single_project,
    setup_two_projects,
)
from keboola_agent_cli.cli import app

runner = CliRunner()

DEFAULT_BRANCH_ID = 100
SIZES = [1, 10]

# Shared route bodies ---------------------------------------------------------

TOKEN_VERIFY = {
    "id": "12345",
    "description": "My Token",
    "owner": {"id": 258, "name": "Production", "defaultBackend": "snowflake"},
}
DEV_BRANCHES = [{"id": DEFAULT_BRANCH_ID, "name": "Main", "isDefault": True}]


def _invoke(config_dir: Path, *args: str) -> Result:
    result = runner.invoke(app, ["--config-dir", str(config_dir), "--json", *args])
    assert result.exit_code == 0, result.output
    return result


def _data(result: Result) -> Any:
    return json.loads(result.output)["data"]


def _configs(n: int) -> list[dict[str, Any]]:
    return [
        {
            "id": str(i),
            "name": f"Config {i}",
            "description": "",
            "configuration": {"parameters": {}},
            "rows": [],
            "currentVersion": {
                "created": "2026-09-01T00:00:00+0000",
                "creatorToken": {"description": "me"},
                "changeDescription": "",
            },
        }
        for i in range(n)
    ]


# project ---------------------------------------------------------------------


class TestProjectCallCounts:
    def test_project_list_is_offline(self, tmp_config_dir: Path, httpx_mock) -> None:
        """`project list` reads config.json only -- zero API calls."""
        setup_single_project(tmp_config_dir)
        mock_api_routes(httpx_mock, {})
        result = _invoke(tmp_config_dir, "project", "list")
        assert [p["alias"] for p in _data(result)] == ["prod"]
        assert_api_calls(httpx_mock, [])

    def test_project_status_single(self, tmp_config_dir: Path, httpx_mock) -> None:
        setup_single_project(tmp_config_dir)
        mock_api_routes(httpx_mock, {("GET", "/v2/storage/tokens/verify"): TOKEN_VERIFY})
        _invoke(tmp_config_dir, "project", "status", "--project", "prod")
        assert_api_calls(httpx_mock, [("GET", "/v2/storage/tokens/verify")])

    def test_project_status_fan_out(self, tmp_config_dir: Path, httpx_mock) -> None:
        """One token verify per project, nothing else."""
        setup_two_projects(tmp_config_dir)
        mock_api_routes(httpx_mock, {("GET", "/v2/storage/tokens/verify"): TOKEN_VERIFY})
        result = _invoke(tmp_config_dir, "project", "status")
        assert len(_data(result)) == 2
        assert_api_calls(
            httpx_mock,
            [("GET", "/v2/storage/tokens/verify")] * 2,
            ordered=False,
        )


# config ----------------------------------------------------------------------


class TestConfigCallCounts:
    @pytest.mark.parametrize("n", SIZES)
    def test_config_list_constant_in_config_count(
        self, tmp_config_dir: Path, httpx_mock, n: int
    ) -> None:
        """Components (with configs inlined) + default-branch lookup + folder metadata.

        The count must not grow with the number of configurations.
        """
        setup_single_project(tmp_config_dir)
        mock_api_routes(
            httpx_mock,
            {
                ("GET", "/v2/storage/components"): [
                    {
                        "id": "keboola.ex-db",
                        "name": "DB",
                        "type": "extractor",
                        "configurations": _configs(n),
                    }
                ],
                ("GET", "/v2/storage/dev-branches"): DEV_BRANCHES,
                (
                    "GET",
                    f"/v2/storage/branch/{DEFAULT_BRANCH_ID}/search/component-configurations",
                ): [],
            },
        )
        result = _invoke(tmp_config_dir, "config", "list", "--project", "prod")
        assert len(_data(result)["configs"]) == n
        assert_api_calls(
            httpx_mock,
            [
                ("GET", "/v2/storage/components"),
                ("GET", "/v2/storage/dev-branches"),
                (
                    "GET",
                    f"/v2/storage/branch/{DEFAULT_BRANCH_ID}/search/component-configurations",
                ),
            ],
        )

    @pytest.mark.parametrize("n_rows", SIZES)
    def test_config_detail_single_call(self, tmp_config_dir: Path, httpx_mock, n_rows: int) -> None:
        """Rows come inline with the config -- no per-row fetch."""
        setup_single_project(tmp_config_dir)
        body = _configs(1)[0]
        body["rows"] = [
            {"id": f"r{i}", "name": f"Row {i}", "configuration": {}} for i in range(n_rows)
        ]
        mock_api_routes(
            httpx_mock,
            {("GET", "/v2/storage/components/keboola.ex-db/configs/0"): body},
        )
        result = _invoke(
            tmp_config_dir,
            "config",
            "detail",
            "--project",
            "prod",
            "--component-id",
            "keboola.ex-db",
            "--config-id",
            "0",
        )
        assert len(_data(result)["rows"]) == n_rows
        assert_api_calls(
            httpx_mock,
            [("GET", "/v2/storage/components/keboola.ex-db/configs/0")],
        )


# job -------------------------------------------------------------------------


def _jobs(n: int) -> list[dict[str, Any]]:
    return [
        {
            "id": str(1000 + i),
            "status": "error" if i % 2 else "success",
            "component": "keboola.ex-db",
            "config": "0",
            "startTime": f"2026-09-01T00:{i:02d}:00+00:00",
        }
        for i in range(n)
    ]


JOB_SEARCH_CALL = (
    "GET",
    "/search/jobs?limit=50&offset=0&sortBy=startTime&sortOrder=desc",
)


class TestJobCallCounts:
    @pytest.mark.parametrize("n", SIZES)
    def test_job_list_single_project(self, tmp_config_dir: Path, httpx_mock, n: int) -> None:
        """One search call regardless of job count (failed jobs included)."""
        setup_single_project(tmp_config_dir)
        mock_api_routes(httpx_mock, {("GET", "/search/jobs"): _jobs(n)})
        result = _invoke(tmp_config_dir, "job", "list", "--project", "prod")
        assert len(_data(result)["jobs"]) == n
        assert_api_calls(httpx_mock, [JOB_SEARCH_CALL], include_query=True)

    def test_job_list_multi_project_fan_out(self, tmp_config_dir: Path, httpx_mock) -> None:
        """Exactly one search per registered project; merged client-side."""
        setup_two_projects(tmp_config_dir)
        mock_api_routes(httpx_mock, {("GET", "/search/jobs"): _jobs(3)})
        result = _invoke(tmp_config_dir, "job", "list")
        assert len(_data(result)["jobs"]) == 6
        assert_api_calls(
            httpx_mock,
            [JOB_SEARCH_CALL] * 2,
            include_query=True,
            ordered=False,
        )

    def test_job_detail_single_call(self, tmp_config_dir: Path, httpx_mock) -> None:
        """No log tail by default -> just the job itself (flow job: hint is static)."""
        setup_single_project(tmp_config_dir)
        mock_api_routes(
            httpx_mock,
            {
                ("GET", "/jobs/1001"): {
                    "id": "1001",
                    "status": "error",
                    "component": "keboola.flow",
                    "config": "9",
                }
            },
        )
        result = _invoke(tmp_config_dir, "job", "detail", "--project", "prod", "--job-id", "1001")
        assert "trigger_hint" in _data(result)
        assert_api_calls(httpx_mock, [("GET", "/jobs/1001")])


# storage ---------------------------------------------------------------------


def _tables(n: int) -> list[dict[str, Any]]:
    return [
        {
            "id": f"in.c-main.t{i}",
            "name": f"t{i}",
            "displayName": f"t{i}",
            "bucket": {"id": "in.c-main", "backendPath": ["KBC_DB", "in.c-main"]},
            "columns": ["id", "value"],
            "primaryKey": ["id"],
            "rowsCount": i,
            "dataSizeBytes": 1024 * i,
        }
        for i in range(n)
    ]


class TestStorageCallCounts:
    @pytest.mark.parametrize("n", SIZES)
    def test_storage_tables_constant_in_table_count(
        self, tmp_config_dir: Path, httpx_mock, n: int
    ) -> None:
        """One list call regardless of table count -- no per-table detail fetch."""
        setup_single_project(tmp_config_dir)
        mock_api_routes(httpx_mock, {("GET", "/v2/storage/tables"): _tables(n)})
        result = _invoke(tmp_config_dir, "storage", "tables", "--project", "prod")
        assert len(_data(result)["tables"]) == n
        assert_api_calls(httpx_mock, [("GET", "/v2/storage/tables")])

    def test_storage_table_detail_single_call(self, tmp_config_dir: Path, httpx_mock) -> None:
        """Bucket backendPath comes inline with the table -- no bucket fetch."""
        setup_single_project(tmp_config_dir)
        mock_api_routes(
            httpx_mock,
            {("GET", "/v2/storage/tables/in.c-main.t0"): _tables(1)[0]},
        )
        result = _invoke(
            tmp_config_dir,
            "storage",
            "table-detail",
            "--project",
            "prod",
            "--table-id",
            "in.c-main.t0",
        )
        assert _data(result)["table_id"] == "in.c-main.t0"
        assert_api_calls(httpx_mock, [("GET", "/v2/storage/tables/in.c-main.t0")])


# flow ------------------------------------------------------------------------


def _flow(flow_id: str, n_tasks: int) -> dict[str, Any]:
    return {
        "id": flow_id,
        "name": f"Flow {flow_id}",
        "description": "",
        "isDisabled": False,
        "configuration": {
            "phases": [{"id": "1", "name": "Extract", "next": []}],
            "tasks": [
                {
                    "id": str(t),
                    "name": f"Task {t}",
                    "phase": "1",
                    "enabled": True,
                    "task": {
                        "type": "job",
                        "componentId": "keboola.ex-db",
                        "configId": str(t),
                        "mode": "run",
                    },
                }
                for t in range(n_tasks)
            ],
        },
    }


class TestFlowCallCounts:
    @pytest.mark.parametrize("n", SIZES)
    def test_flow_list_constant_in_flow_count(
        self, tmp_config_dir: Path, httpx_mock, n: int
    ) -> None:
        """keboola.flow configs + the legacy keboola.orchestrator count probe."""
        setup_single_project(tmp_config_dir)
        mock_api_routes(
            httpx_mock,
            {
                ("GET", "/v2/storage/components/keboola.flow/configs"): [
                    _flow(str(i), 3) for i in range(n)
                ],
                ("GET", "/v2/storage/components/keboola.orchestrator/configs"): [],
            },
        )
        result = _invoke(tmp_config_dir, "flow", "list", "--project", "prod")
        assert len(_data(result)["flows"]) == n
        assert_api_calls(
            httpx_mock,
            [
                ("GET", "/v2/storage/components/keboola.flow/configs"),
                ("GET", "/v2/storage/components/keboola.orchestrator/configs"),
            ],
        )

    @pytest.mark.parametrize("n_tasks", SIZES)
    def test_flow_detail_constant_in_task_count(
        self, tmp_config_dir: Path, httpx_mock, n_tasks: int
    ) -> None:
        """No per-task component/config lookup."""
        setup_single_project(tmp_config_dir)
        mock_api_routes(
            httpx_mock,
            {("GET", "/v2/storage/components/keboola.flow/configs/9"): _flow("9", n_tasks)},
        )
        result = _invoke(tmp_config_dir, "flow", "detail", "--project", "prod", "--flow-id", "9")
        assert _data(result)["task_count"] == n_tasks
        assert_api_calls(
            httpx_mock,
            [("GET", "/v2/storage/components/keboola.flow/configs/9")],
        )
