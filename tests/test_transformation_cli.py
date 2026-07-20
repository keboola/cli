"""Tests for the `kbagent transformation` command group (issue #396).

Exercises the Typer commands through CliRunner against a REAL
TransformationService wired to a mocked KeboolaClient, so payload shaping
(create bucket derivation, dialect default via verify_token, edit batch
re-splitting) is covered end-to-end without HTTP.

The transformation sub-app is mounted on a standalone root app here (the
group is wired into cli.py separately); ctx.obj mirrors what cli.py
provides (formatter, config_store, project_service).
"""

import copy
import json
from pathlib import Path
from unittest.mock import MagicMock

import typer
from typer.testing import CliRunner

from keboola_agent_cli.commands.transformation import transformation_app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig, TokenVerifyResponse
from keboola_agent_cli.output import OutputFormatter
from keboola_agent_cli.services.project_service import ProjectService
from keboola_agent_cli.services.transformation_service import TransformationService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

runner = CliRunner()


def _make_store(tmp_path: Path) -> ConfigStore:
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
            project_name="prod",
            project_id=1234,
        ),
    )
    return store


def _verify_response(backend: str) -> TokenVerifyResponse:
    return TokenVerifyResponse(
        token_id="1",
        token_description="test token",
        project_id=1234,
        project_name="prod",
        owner_name="prod",
        default_backend=backend,
    )


def _make_client(backend: str = "snowflake") -> MagicMock:
    client = MagicMock()
    client.verify_token.return_value = _verify_response(backend)
    client.create_config.return_value = {"id": "9001", "version": 1}
    client.update_config.return_value = {"id": "123", "version": 5}
    return client


def _build_app(store: ConfigStore, client: MagicMock) -> typer.Typer:
    """Standalone root app mirroring cli.py's ctx.obj wiring."""
    root = typer.Typer()

    @root.callback()
    def _root(
        ctx: typer.Context,
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        ctx.obj = {
            "formatter": OutputFormatter(json_mode=json_output, no_color=True),
            "config_store": store,
            "project_service": ProjectService(config_store=store),
            "transformation_service": TransformationService(
                config_store=store,
                client_factory=lambda _url, _token: client,
            ),
        }

    root.add_typer(transformation_app, name="transformation")
    return root


def _config_detail() -> dict:
    """Config detail fixture: one statement-array code + one legacy string code."""
    return {
        "id": "123",
        "name": "My Transform",
        "description": "desc",
        "version": 3,
        "configuration": {
            "parameters": {
                "blocks": [
                    {
                        "name": "Main",
                        "codes": [
                            {"name": "load", "script": ["SELECT 1;", "SELECT 2;"]},
                            {"name": "clean", "script": "DELETE FROM t;"},
                        ],
                    }
                ]
            },
            "storage": {"input": {"tables": []}, "output": {"tables": []}},
        },
    }


class TestTransformationCreate:
    def test_create_payload_shape_and_bucket_derivation(self, tmp_path: Path) -> None:
        """Blocks/Code shaping, statement split, and diacritics-stripped bucket."""
        client = _make_client("snowflake")
        app = _build_app(_make_store(tmp_path), client)

        result = runner.invoke(
            app,
            [
                "--json",
                "transformation",
                "create",
                "--project",
                "prod",
                "--name",
                "Můj Report",
                "--sql",
                'CREATE TABLE "report" AS SELECT * FROM "src"; DELETE FROM "tmp";',
                "--created-table",
                "report",
                "--description",
                "test transform",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "ok"
        assert payload["data"]["component_id"] == "keboola.snowflake-transformation"
        assert payload["data"]["config_id"] == "9001"

        client.create_config.assert_called_once()
        kwargs = client.create_config.call_args.kwargs
        assert kwargs["component_id"] == "keboola.snowflake-transformation"
        assert kwargs["name"] == "Můj Report"
        assert kwargs["description"] == "test transform"
        configuration = kwargs["configuration"]
        assert configuration["parameters"]["blocks"] == [
            {
                "name": "Blocks",
                "codes": [
                    {
                        "name": "Code",
                        "script": [
                            'CREATE TABLE "report" AS SELECT * FROM "src";',
                            'DELETE FROM "tmp";',
                        ],
                    }
                ],
            }
        ]
        # Bucket derived from the transformation name, diacritics stripped.
        assert configuration["storage"]["output"]["tables"] == [
            {"source": "report", "destination": "out.c-Muj-Report.report"}
        ]
        assert configuration["storage"]["input"]["tables"] == []

    def test_create_bigquery_backend_default(self, tmp_path: Path) -> None:
        client = _make_client("bigquery")
        app = _build_app(_make_store(tmp_path), client)

        result = runner.invoke(
            app,
            [
                "--json",
                "transformation",
                "create",
                "--project",
                "prod",
                "--name",
                "BQ",
                "--sql",
                "SELECT 1;",
            ],
        )

        assert result.exit_code == 0, result.output
        client.verify_token.assert_called_once()
        kwargs = client.create_config.call_args.kwargs
        assert kwargs["component_id"] == "keboola.google-bigquery-transformation"

    def test_create_unsupported_backend_is_config_error(self, tmp_path: Path) -> None:
        client = _make_client("exasol")
        app = _build_app(_make_store(tmp_path), client)

        result = runner.invoke(
            app,
            [
                "--json",
                "transformation",
                "create",
                "--project",
                "prod",
                "--name",
                "X",
                "--sql",
                "SELECT 1;",
            ],
        )

        assert result.exit_code == 5, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "CONFIG_ERROR"
        assert "exasol" in payload["error"]["message"]
        client.create_config.assert_not_called()

    def test_create_explicit_component_id_skips_verify(self, tmp_path: Path) -> None:
        client = _make_client("snowflake")
        app = _build_app(_make_store(tmp_path), client)

        result = runner.invoke(
            app,
            [
                "--json",
                "transformation",
                "create",
                "--project",
                "prod",
                "--name",
                "Explicit",
                "--sql",
                "SELECT 1;",
                "--component-id",
                "keboola.google-bigquery-transformation",
            ],
        )

        assert result.exit_code == 0, result.output
        client.verify_token.assert_not_called()
        kwargs = client.create_config.call_args.kwargs
        assert kwargs["component_id"] == "keboola.google-bigquery-transformation"

    def test_create_dry_run_makes_no_api_write(self, tmp_path: Path) -> None:
        client = _make_client("snowflake")
        app = _build_app(_make_store(tmp_path), client)

        result = runner.invoke(
            app,
            [
                "--json",
                "transformation",
                "create",
                "--project",
                "prod",
                "--name",
                "Dry",
                "--sql",
                "SELECT 1;",
                "--dry-run",
            ],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["data"]["dry_run"] is True
        assert "config_id" not in payload["data"]
        assert payload["data"]["configuration"]["parameters"]["blocks"][0]["name"] == "Blocks"
        client.create_config.assert_not_called()

    def test_create_sql_file(self, tmp_path: Path) -> None:
        client = _make_client("snowflake")
        app = _build_app(_make_store(tmp_path), client)
        sql_file = tmp_path / "query.sql"
        sql_file.write_text("SELECT 1;\nSELECT 2;", encoding="utf-8")

        result = runner.invoke(
            app,
            [
                "--json",
                "transformation",
                "create",
                "--project",
                "prod",
                "--name",
                "FromFile",
                "--sql-file",
                str(sql_file),
            ],
        )

        assert result.exit_code == 0, result.output
        kwargs = client.create_config.call_args.kwargs
        script = kwargs["configuration"]["parameters"]["blocks"][0]["codes"][0]["script"]
        assert script == ["SELECT 1;", "SELECT 2;"]

    def test_create_sql_and_sql_file_conflict(self, tmp_path: Path) -> None:
        client = _make_client("snowflake")
        app = _build_app(_make_store(tmp_path), client)
        sql_file = tmp_path / "query.sql"
        sql_file.write_text("SELECT 1;", encoding="utf-8")

        result = runner.invoke(
            app,
            [
                "--json",
                "transformation",
                "create",
                "--project",
                "prod",
                "--name",
                "X",
                "--sql",
                "SELECT 1;",
                "--sql-file",
                str(sql_file),
            ],
        )

        assert result.exit_code == 2, result.output
        assert json.loads(result.output)["error"]["code"] == "INVALID_ARGUMENT"

    def test_create_neither_sql_nor_file(self, tmp_path: Path) -> None:
        client = _make_client("snowflake")
        app = _build_app(_make_store(tmp_path), client)

        result = runner.invoke(
            app,
            ["--json", "transformation", "create", "--project", "prod", "--name", "X"],
        )

        assert result.exit_code == 2, result.output
        assert json.loads(result.output)["error"]["code"] == "INVALID_ARGUMENT"

    def test_create_empty_sql_is_validation_error(self, tmp_path: Path) -> None:
        client = _make_client("snowflake")
        app = _build_app(_make_store(tmp_path), client)

        result = runner.invoke(
            app,
            [
                "--json",
                "transformation",
                "create",
                "--project",
                "prod",
                "--name",
                "X",
                "--sql",
                "   ",
            ],
        )

        assert result.exit_code == 1, result.output
        assert json.loads(result.output)["error"]["code"] == "VALIDATION_ERROR"
        client.create_config.assert_not_called()


class TestTransformationShow:
    def test_show_tree_with_synthetic_ids(self, tmp_path: Path) -> None:
        client = _make_client()
        client.get_config_detail.return_value = _config_detail()
        app = _build_app(_make_store(tmp_path), client)

        result = runner.invoke(
            app,
            [
                "--json",
                "transformation",
                "show",
                "--project",
                "prod",
                "--config-id",
                "123",
                "--component-id",
                "keboola.snowflake-transformation",
            ],
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["config_id"] == "123"
        assert data["component_id"] == "keboola.snowflake-transformation"
        assert data["name"] == "My Transform"
        assert data["version"] == 3
        blocks = data["blocks"]
        assert blocks[0]["id"] == "b0"
        assert blocks[0]["name"] == "Main"
        assert blocks[0]["codes"][0]["id"] == "b0.c0"
        assert blocks[0]["codes"][0]["script"] == ["SELECT 1;", "SELECT 2;"]
        assert blocks[0]["codes"][0]["script_text"] == "SELECT 1;\n\nSELECT 2;"
        # Legacy string-shaped script is normalized to a statement array.
        assert blocks[0]["codes"][1]["id"] == "b0.c1"
        assert blocks[0]["codes"][1]["script"] == ["DELETE FROM t;"]
        assert data["storage"] == {"input": {"tables": []}, "output": {"tables": []}}

    def test_show_human_mode_renders_ids(self, tmp_path: Path) -> None:
        client = _make_client()
        client.get_config_detail.return_value = _config_detail()
        app = _build_app(_make_store(tmp_path), client)

        result = runner.invoke(
            app,
            [
                "transformation",
                "show",
                "--project",
                "prod",
                "--config-id",
                "123",
                "--component-id",
                "keboola.snowflake-transformation",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "b0" in result.output
        assert "b0.c0" in result.output
        assert "My Transform" in result.output

    def test_show_component_fallback(self, tmp_path: Path) -> None:
        """Snowflake 404s, BigQuery hits -> component resolved to BigQuery."""
        client = _make_client()

        def _detail(component_id: str, config_id: str, branch_id=None) -> dict:
            if component_id == "keboola.google-bigquery-transformation":
                return _config_detail()
            raise KeboolaApiError(
                message="not found", status_code=404, error_code=ErrorCode.NOT_FOUND
            )

        client.get_config_detail.side_effect = _detail
        app = _build_app(_make_store(tmp_path), client)

        result = runner.invoke(
            app,
            ["--json", "transformation", "show", "--project", "prod", "--config-id", "123"],
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["component_id"] == "keboola.google-bigquery-transformation"
        # Snowflake tried first (preference order), then BigQuery.
        tried = [call.args[0] for call in client.get_config_detail.call_args_list]
        assert tried[:2] == [
            "keboola.snowflake-transformation",
            "keboola.google-bigquery-transformation",
        ]

    def test_show_not_found_anywhere(self, tmp_path: Path) -> None:
        client = _make_client()
        client.get_config_detail.side_effect = KeboolaApiError(
            message="not found", status_code=404, error_code=ErrorCode.NOT_FOUND
        )
        app = _build_app(_make_store(tmp_path), client)

        result = runner.invoke(
            app,
            ["--json", "transformation", "show", "--project", "prod", "--config-id", "999"],
        )

        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["error"]["code"] == "NOT_FOUND"
        assert "999" in payload["error"]["message"]


class TestTransformationEdit:
    def test_edit_batch_ops_applied_and_resplit(self, tmp_path: Path) -> None:
        client = _make_client()
        client.get_config_detail.return_value = _config_detail()
        app = _build_app(_make_store(tmp_path), client)

        result = runner.invoke(
            app,
            [
                "--json",
                "transformation",
                "edit",
                "--project",
                "prod",
                "--config-id",
                "123",
                "--component-id",
                "keboola.snowflake-transformation",
                "--change-description",
                "rework load",
                "--op",
                '{"op": "rename_block", "block_id": "b0", "block_name": "Main Renamed"}',
                "--op",
                '{"op": "set_code", "block_id": "b0", "code_id": "b0.c0",'
                ' "script": "SELECT 100; SELECT 200;"}',
                "--op",
                '{"op": "str_replace", "search_for": "200", "replace_with": "300"}',
            ],
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["version"] == 5
        assert data["structural_change"] is False
        assert len(data["operations_applied"]) == 3
        assert data["blocks"][0]["name"] == "Main Renamed"
        assert data["blocks"][0]["codes"][0]["script"] == ["SELECT 100;", "SELECT 300;"]

        client.update_config.assert_called_once()
        kwargs = client.update_config.call_args.kwargs
        assert kwargs["component_id"] == "keboola.snowflake-transformation"
        assert kwargs["config_id"] == "123"
        assert kwargs["change_description"] == "rework load"
        blocks = kwargs["configuration"]["parameters"]["blocks"]
        assert blocks[0]["name"] == "Main Renamed"
        # Multi-statement set_code re-split into one statement per element.
        assert blocks[0]["codes"][0]["script"] == ["SELECT 100;", "SELECT 300;"]
        # Legacy string script normalized to array on the round trip.
        assert blocks[0]["codes"][1]["script"] == ["DELETE FROM t;"]
        # No synthetic ids persisted.
        assert "id" not in blocks[0]
        assert "id" not in blocks[0]["codes"][0]

    def test_edit_dry_run_makes_no_api_write(self, tmp_path: Path) -> None:
        client = _make_client()
        client.get_config_detail.return_value = _config_detail()
        app = _build_app(_make_store(tmp_path), client)

        result = runner.invoke(
            app,
            [
                "--json",
                "transformation",
                "edit",
                "--project",
                "prod",
                "--config-id",
                "123",
                "--component-id",
                "keboola.snowflake-transformation",
                "--change-description",
                "preview",
                "--op",
                '{"op": "remove_code", "block_id": "b0", "code_id": "b0.c1"}',
                "--dry-run",
            ],
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["dry_run"] is True
        assert data["structural_change"] is True
        assert "version" not in data
        assert len(data["blocks"][0]["codes"]) == 1
        client.update_config.assert_not_called()

    def test_edit_storage_wholesale_replacement(self, tmp_path: Path) -> None:
        client = _make_client()
        detail = _config_detail()
        client.get_config_detail.return_value = detail
        app = _build_app(_make_store(tmp_path), client)

        new_storage = {
            "input": {"tables": [{"source": "in.c-main.orders", "destination": "orders"}]},
            "output": {"tables": []},
        }
        result = runner.invoke(
            app,
            [
                "--json",
                "transformation",
                "edit",
                "--project",
                "prod",
                "--config-id",
                "123",
                "--component-id",
                "keboola.snowflake-transformation",
                "--change-description",
                "remap inputs",
                "--storage",
                json.dumps(new_storage),
            ],
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["storage_replaced"] is True
        assert data["operations_applied"] == []
        kwargs = client.update_config.call_args.kwargs
        assert kwargs["configuration"]["storage"] == new_storage
        # No ops given -> parameters untouched (byte-for-byte as fetched).
        assert kwargs["configuration"]["parameters"] == copy.deepcopy(
            detail["configuration"]["parameters"]
        )

    def test_edit_op_file(self, tmp_path: Path) -> None:
        client = _make_client()
        client.get_config_detail.return_value = _config_detail()
        app = _build_app(_make_store(tmp_path), client)
        ops_file = tmp_path / "ops.json"
        ops_file.write_text(
            json.dumps(
                [
                    {
                        "op": "add_code",
                        "block_id": "b0",
                        "code": {"name": "extra", "script": "SELECT 9;"},
                    }
                ]
            ),
            encoding="utf-8",
        )

        result = runner.invoke(
            app,
            [
                "--json",
                "transformation",
                "edit",
                "--project",
                "prod",
                "--config-id",
                "123",
                "--component-id",
                "keboola.snowflake-transformation",
                "--change-description",
                "add extra code",
                "--op-file",
                str(ops_file),
            ],
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert [c["name"] for c in data["blocks"][0]["codes"]] == ["load", "clean", "extra"]
        assert data["blocks"][0]["codes"][2]["id"] == "b0.c2"

    def test_edit_unknown_block_id_lists_valid_ids(self, tmp_path: Path) -> None:
        client = _make_client()
        client.get_config_detail.return_value = _config_detail()
        app = _build_app(_make_store(tmp_path), client)

        result = runner.invoke(
            app,
            [
                "--json",
                "transformation",
                "edit",
                "--project",
                "prod",
                "--config-id",
                "123",
                "--component-id",
                "keboola.snowflake-transformation",
                "--change-description",
                "bad op",
                "--op",
                '{"op": "rename_block", "block_id": "b9", "block_name": "X"}',
            ],
        )

        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["error"]["code"] == "VALIDATION_ERROR"
        assert "Valid block ids: b0" in payload["error"]["message"]
        client.update_config.assert_not_called()

    def test_edit_malformed_op_json(self, tmp_path: Path) -> None:
        client = _make_client()
        app = _build_app(_make_store(tmp_path), client)

        result = runner.invoke(
            app,
            [
                "--json",
                "transformation",
                "edit",
                "--project",
                "prod",
                "--config-id",
                "123",
                "--change-description",
                "bad json",
                "--op",
                "{not valid json",
            ],
        )

        assert result.exit_code == 2, result.output
        assert json.loads(result.output)["error"]["code"] == "INPUT_ERROR"
        client.get_config_detail.assert_not_called()
        client.update_config.assert_not_called()

    def test_edit_op_and_op_file_conflict(self, tmp_path: Path) -> None:
        client = _make_client()
        app = _build_app(_make_store(tmp_path), client)
        ops_file = tmp_path / "ops.json"
        ops_file.write_text("[]", encoding="utf-8")

        result = runner.invoke(
            app,
            [
                "--json",
                "transformation",
                "edit",
                "--project",
                "prod",
                "--config-id",
                "123",
                "--change-description",
                "conflict",
                "--op",
                '{"op": "remove_block", "block_id": "b0"}',
                "--op-file",
                str(ops_file),
            ],
        )

        assert result.exit_code == 2, result.output
        assert json.loads(result.output)["error"]["code"] == "INVALID_ARGUMENT"

    def test_edit_requires_ops_or_storage(self, tmp_path: Path) -> None:
        client = _make_client()
        app = _build_app(_make_store(tmp_path), client)

        result = runner.invoke(
            app,
            [
                "--json",
                "transformation",
                "edit",
                "--project",
                "prod",
                "--config-id",
                "123",
                "--change-description",
                "noop",
            ],
        )

        assert result.exit_code == 2, result.output
        assert json.loads(result.output)["error"]["code"] == "INVALID_ARGUMENT"
