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

Size-parametrized tests (1 vs 10 items) expect the same calls at both sizes.
That catches a call made once per item, but not one made once per batch or
page of more than 10 items. The fixtures look like real API payloads (several component types, a
transformation with rows and storage mappings, Queue jobs with ``runId``,
alias and shared tables), so a per-item call gated on one of those fields
is caught too.

Routing ignores host and query (see ``helpers.mock_api_routes``); where the
query string is part of the contract (config list and storage tables
includes, job list sorting/paging) the assertion uses ``include_query=True``,
which compares the parsed, sorted query. Multi-project fan-out runs on a
thread pool, so those assertions compare sorted multisets (``ordered=False``)
and record each call's token (``include_token=True``) to show that every
project got its own call.

The tests invoke ``app``, not the ``run()`` console-script wrapper, so the
best-effort telemetry ``POST /v2/storage/events`` that ``run()`` sends after a
command is not counted here.
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
from keboola_agent_cli.constants import ENV_AUTO_UPDATE

runner = CliRunner()

DEFAULT_BRANCH_ID = 100
SIZES = [1, 10]
# The tokens `helpers.setup_two_projects` gives the `prod` and `dev` projects.
PROD_TOKEN = "901-xxx"
DEV_TOKEN = "7012-yyy"


@pytest.fixture(autouse=True)
def _no_auto_update(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the startup auto-update check out of the recorded calls.

    Without it the exact counts hold only because an editable install skips the
    check (``_is_dev_install``); an installed wheel would add a GitHub
    ``releases/latest`` call to the first invocation.
    """
    monkeypatch.setenv(ENV_AUTO_UPDATE, "false")


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
    # A list command exits 0 even when a project call fails -- the failure goes
    # into `errors` -- so the exit code alone proves little.
    data = _data(result)
    if isinstance(data, dict) and "errors" in data:
        assert data["errors"] == [], data["errors"]
    return result


def _data(result: Result) -> Any:
    return json.loads(result.output)["data"]


def _configs(n: int) -> list[dict[str, Any]]:
    return [
        {
            "id": str(i),
            "name": f"Config {i}",
            "description": "",
            "created": "2026-09-01T00:00:00+0000",
            "creatorToken": {"id": 7, "description": "me"},
            "version": 3,
            "changeDescription": "",
            "isDeleted": False,
            "isDisabled": False,
            "configuration": {"parameters": {}},
            "rows": [],
            "state": {},
            "currentVersion": {
                "created": "2026-09-01T00:00:00+0000",
                "creatorToken": {"id": 7, "description": "me"},
                "changeDescription": "",
            },
        }
        for i in range(n)
    ]


def _transformation_configs(n: int) -> list[dict[str, Any]]:
    """SQL transformation configs: a code block, storage input/output mappings and a row."""
    return [
        {
            **cfg,
            "configuration": {
                "parameters": {
                    "blocks": [
                        {
                            "name": "Block 1",
                            "codes": [
                                {
                                    "name": "Code",
                                    "script": [f'CREATE TABLE "out{i}" AS SELECT * FROM "t{i}";'],
                                }
                            ],
                        }
                    ]
                },
                "storage": {
                    "input": {"tables": [{"source": f"in.c-main.t{i}", "destination": f"t{i}"}]},
                    "output": {
                        "tables": [{"source": f"out{i}", "destination": f"out.c-main.out{i}"}]
                    },
                },
            },
            "rows": [
                {
                    "id": f"{i}01",
                    "name": "Row 1",
                    "isDisabled": False,
                    "configuration": {"parameters": {}},
                }
            ],
        }
        for i, cfg in enumerate(_configs(n))
    ]


def _components(n: int) -> list[dict[str, Any]]:
    """An extractor, a SQL transformation and a writer, each with ``n`` configs."""
    return [
        {"id": "keboola.ex-db", "name": "DB", "type": "extractor", "configurations": _configs(n)},
        {
            "id": "keboola.snowflake-transformation",
            "name": "Snowflake SQL",
            "type": "transformation",
            "configurations": _transformation_configs(n),
        },
        {
            "id": "keboola.wr-db-snowflake",
            "name": "Snowflake",
            "type": "writer",
            "configurations": _configs(n),
        },
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
        result = _invoke(tmp_config_dir, "project", "status", "--project", "prod")
        assert [(p["alias"], p["status"]) for p in _data(result)] == [("prod", "ok")]
        assert_api_calls(httpx_mock, [("GET", "/v2/storage/tokens/verify")])

    def test_project_status_fan_out(self, tmp_config_dir: Path, httpx_mock) -> None:
        """One token verify per project, nothing else."""
        setup_two_projects(tmp_config_dir)
        mock_api_routes(httpx_mock, {("GET", "/v2/storage/tokens/verify"): TOKEN_VERIFY})
        result = _invoke(tmp_config_dir, "project", "status")
        assert sorted((p["alias"], p["status"]) for p in _data(result)) == [
            ("dev", "ok"),
            ("prod", "ok"),
        ]
        assert_api_calls(
            httpx_mock,
            [
                ("GET", "/v2/storage/tokens/verify", PROD_TOKEN),
                ("GET", "/v2/storage/tokens/verify", DEV_TOKEN),
            ],
            include_token=True,
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
                ("GET", "/v2/storage/components"): _components(n),
                ("GET", "/v2/storage/dev-branches"): DEV_BRANCHES,
                (
                    "GET",
                    f"/v2/storage/branch/{DEFAULT_BRANCH_ID}/search/component-configurations",
                ): [],
            },
        )
        result = _invoke(tmp_config_dir, "config", "list", "--project", "prod")
        assert len(_data(result)["configs"]) == 3 * n
        assert_api_calls(
            httpx_mock,
            [
                ("GET", "/v2/storage/components?include=configuration"),
                ("GET", "/v2/storage/dev-branches"),
                (
                    "GET",
                    (
                        f"/v2/storage/branch/{DEFAULT_BRANCH_ID}/search/component-configurations"
                        "?metadataKeys[]=KBC.configuration.folderName&include=filteredMetadata"
                    ),
                ),
            ],
            include_query=True,
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
            "runId": str(1000 + i),
            "parentRunId": "",
            "branchId": str(DEFAULT_BRANCH_ID),
            "status": "error" if i % 2 else "success",
            "isFinished": True,
            "mode": "run",
            "component": "keboola.ex-db",
            "config": "0",
            "createdTime": f"2026-09-01T00:{i:02d}:00+00:00",
            "startTime": f"2026-09-01T00:{i:02d}:05+00:00",
            "endTime": f"2026-09-01T00:{i:02d}:35+00:00",
            "durationSeconds": 30,
            "result": (
                {"message": "Table import failed.", "error": {"type": "user"}}
                if i % 2
                else {"message": "Component processing finished."}
            ),
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
            [(*JOB_SEARCH_CALL, PROD_TOKEN), (*JOB_SEARCH_CALL, DEV_TOKEN)],
            include_query=True,
            include_token=True,
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


def _bucket(bucket_id: str, **extra: Any) -> dict[str, Any]:
    stage, name = bucket_id.split(".", 1)
    return {
        "id": bucket_id,
        "name": name,
        "stage": stage,
        "backend": "snowflake",
        "backendPath": ["KBC_DB", bucket_id],
        "sharing": None,
        **extra,
    }


def _table(table_id: str, bucket: dict[str, Any], i: int, **extra: Any) -> dict[str, Any]:
    name = table_id.rsplit(".", 1)[1]
    return {
        "uri": f"https://connection.keboola.com/v2/storage/tables/{table_id}",
        "id": table_id,
        "name": name,
        "displayName": name,
        "bucket": bucket,
        "columns": ["id", "value"],
        "primaryKey": ["id"],
        "created": "2026-09-01T00:00:00+0000",
        "lastImportDate": "2026-09-01T00:00:00+0000",
        "lastChangeDate": "2026-09-01T00:00:00+0000",
        "rowsCount": i,
        "dataSizeBytes": 1024 * i,
        "isAlias": False,
        "isAliasable": True,
        "isTyped": False,
        **extra,
    }


def _tables(n: int) -> list[dict[str, Any]]:
    """``n`` plain tables, ``n`` alias tables of them, and ``n`` tables in a shared bucket."""
    main = _bucket("in.c-main")
    aliases = _bucket("out.c-aliases")
    shared = _bucket(
        "out.c-shared",
        sharing="organization",
        sharedBy={"id": 7, "name": "me", "date": "2026-09-01T00:00:00+0000"},
    )
    return [
        *[_table(f"in.c-main.t{i}", main, i) for i in range(n)],
        *[
            _table(
                f"out.c-aliases.t{i}",
                aliases,
                i,
                isAlias=True,
                isAliasable=False,
                aliasColumnsAutoSync=True,
                sourceTable={
                    "id": f"in.c-main.t{i}",
                    "uri": f"https://connection.keboola.com/v2/storage/tables/in.c-main.t{i}",
                    "name": f"t{i}",
                    "project": {"id": 901, "name": "Production"},
                },
            )
            for i in range(n)
        ],
        *[_table(f"out.c-shared.s{i}", shared, i) for i in range(n)],
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
        assert len(_data(result)["tables"]) == 3 * n
        # Pins the query too: the list call sends no `include` parameter.
        assert_api_calls(httpx_mock, [("GET", "/v2/storage/tables")], include_query=True)

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
