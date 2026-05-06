"""Tests for ``parameters.blocks[].codes[].script`` normalization (issue #245).

The Storage API silently accepts a string for ``script`` while the runtime
validator requires an array. Issue #245 closes the gap on the kbagent
write side: SQL transformations get statement-level split via the existing
:func:`split_statements` state machine; Python / R / custom-Python apps
get a single-element wrap.

Covers:
- helper function ``normalize_blocks_codes_script`` directly
- ``ConfigService.update_config`` writes normalized arrays and exposes
  the change record on the result envelope
- CLI human mode emits a yellow warning + per-element trace
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from helpers import setup_single_project
from keboola_agent_cli.cli import app
from keboola_agent_cli.services.config_service import ConfigService
from keboola_agent_cli.sync.code_extraction import (
    SQL_TRANSFORMATION_COMPONENTS,
    is_sql_transformation_component,
    normalize_blocks_codes_script,
)

runner = CliRunner()


# ---------------------------------------------------------------------------
# is_sql_transformation_component
# ---------------------------------------------------------------------------


class TestIsSqlTransformationComponent:
    @pytest.mark.parametrize(
        "component_id",
        [
            "keboola.snowflake-transformation",
            "keboola.synapse-transformation",
            "keboola.oracle-transformation",
            "keboola.redshift-sql-transformation",
            "keboola.google-bigquery-transformation",
            "keboola.duckdb-transformation",
        ],
    )
    def test_known_sql_components(self, component_id: str) -> None:
        assert is_sql_transformation_component(component_id) is True
        assert component_id in SQL_TRANSFORMATION_COMPONENTS

    @pytest.mark.parametrize(
        "component_id",
        [
            # variant naming covered by fragment fallback
            "keboola.snowflake-transformation-v2",
            "keboola.bigquery-transformation",
            "custom.exasol-transformation",
            "self-hosted.teradata-transformation",
        ],
    )
    def test_fragment_fallback(self, component_id: str) -> None:
        assert is_sql_transformation_component(component_id) is True

    @pytest.mark.parametrize(
        "component_id",
        [
            "keboola.python-transformation-v2",
            "kds-team.app-custom-python",
            "keboola.ex-db-mysql",
            "keboola.wr-google-bigquery-v2",
            "keboola.orchestrator",
            "",
        ],
    )
    def test_non_sql_components(self, component_id: str) -> None:
        assert is_sql_transformation_component(component_id) is False


# ---------------------------------------------------------------------------
# normalize_blocks_codes_script
# ---------------------------------------------------------------------------


class TestNormalizeBlocksCodesScript:
    def test_sql_string_split_into_statements(self) -> None:
        cfg = {
            "parameters": {
                "blocks": [
                    {
                        "name": "B1",
                        "codes": [
                            {
                                "name": "c1",
                                "script": "CREATE TABLE x AS SELECT 1; INSERT INTO x VALUES (2);",
                            }
                        ],
                    }
                ]
            }
        }
        out, norms = normalize_blocks_codes_script(
            "keboola.snowflake-transformation", copy.deepcopy(cfg)
        )
        assert out["parameters"]["blocks"][0]["codes"][0]["script"] == [
            "CREATE TABLE x AS SELECT 1;",
            "INSERT INTO x VALUES (2);",
        ]
        assert len(norms) == 1
        assert norms[0]["action"] == "sql_split"
        assert norms[0]["after_length"] == 2
        assert norms[0]["path"] == "parameters.blocks[0].codes[0].script"

    def test_sql_split_respects_block_comments(self) -> None:
        """Semicolons inside ``/* ... */`` must not split the statement."""
        cfg = {
            "parameters": {
                "blocks": [
                    {
                        "name": "B",
                        "codes": [
                            {
                                "name": "c",
                                "script": "/* note; with; semicolons */ SELECT 1;",
                            }
                        ],
                    }
                ]
            }
        }
        out, _ = normalize_blocks_codes_script(
            "keboola.snowflake-transformation", copy.deepcopy(cfg)
        )
        assert out["parameters"]["blocks"][0]["codes"][0]["script"] == [
            "/* note; with; semicolons */ SELECT 1;"
        ]

    def test_sql_split_respects_string_literals(self) -> None:
        """Semicolons inside ``'...'`` must not split the statement."""
        cfg = {
            "parameters": {
                "blocks": [
                    {
                        "name": "B",
                        "codes": [
                            {
                                "name": "c",
                                "script": "SELECT 'a;b;c' AS literal; SELECT 2;",
                            }
                        ],
                    }
                ]
            }
        }
        out, _ = normalize_blocks_codes_script(
            "keboola.snowflake-transformation", copy.deepcopy(cfg)
        )
        scripts = out["parameters"]["blocks"][0]["codes"][0]["script"]
        assert scripts == ["SELECT 'a;b;c' AS literal;", "SELECT 2;"]

    def test_python_string_wraps_to_single_element(self) -> None:
        cfg = {
            "parameters": {
                "blocks": [
                    {
                        "name": "B",
                        "codes": [
                            {
                                "name": "c",
                                "script": "import os\nprint('hi')",
                            }
                        ],
                    }
                ]
            }
        }
        out, norms = normalize_blocks_codes_script(
            "keboola.python-transformation-v2", copy.deepcopy(cfg)
        )
        assert out["parameters"]["blocks"][0]["codes"][0]["script"] == ["import os\nprint('hi')"]
        assert norms[0]["action"] == "wrap_array"
        assert norms[0]["after_length"] == 1

    def test_custom_python_app_wraps(self) -> None:
        cfg = {
            "parameters": {"blocks": [{"name": "B", "codes": [{"name": "c", "script": "x = 1"}]}]}
        }
        out, norms = normalize_blocks_codes_script("kds-team.app-custom-python", copy.deepcopy(cfg))
        assert out["parameters"]["blocks"][0]["codes"][0]["script"] == ["x = 1"]
        assert norms[0]["action"] == "wrap_array"

    def test_already_array_passthrough(self) -> None:
        cfg = {
            "parameters": {
                "blocks": [
                    {
                        "name": "B",
                        "codes": [{"name": "c", "script": ["SELECT 1;", "SELECT 2;"]}],
                    }
                ]
            }
        }
        before = copy.deepcopy(cfg)
        out, norms = normalize_blocks_codes_script("keboola.snowflake-transformation", cfg)
        assert out == before
        assert norms == []

    def test_no_blocks_passthrough(self) -> None:
        cfg = {"parameters": {"db": {"host": "x"}}}
        before = copy.deepcopy(cfg)
        out, norms = normalize_blocks_codes_script("keboola.snowflake-transformation", cfg)
        assert out == before
        assert norms == []

    def test_no_parameters_passthrough(self) -> None:
        cfg: dict = {"storage": {}}
        _, norms = normalize_blocks_codes_script(
            "keboola.snowflake-transformation", copy.deepcopy(cfg)
        )
        assert norms == []

    def test_empty_string_collapses_to_empty_list(self) -> None:
        """A whitespace-only / empty string yields ``[]`` (runtime no-op)."""
        cfg = {
            "parameters": {"blocks": [{"name": "B", "codes": [{"name": "c", "script": "   \n  "}]}]}
        }
        out, _ = normalize_blocks_codes_script(
            "keboola.python-transformation-v2", copy.deepcopy(cfg)
        )
        assert out["parameters"]["blocks"][0]["codes"][0]["script"] == []

    def test_multi_block_multi_code_normalizes_each(self) -> None:
        cfg = {
            "parameters": {
                "blocks": [
                    {
                        "name": "B1",
                        "codes": [
                            {"name": "c1", "script": "SELECT 1;"},
                            {"name": "c2", "script": ["already array"]},
                        ],
                    },
                    {
                        "name": "B2",
                        "codes": [{"name": "c3", "script": "SELECT 2; SELECT 3;"}],
                    },
                ]
            }
        }
        out, norms = normalize_blocks_codes_script(
            "keboola.snowflake-transformation", copy.deepcopy(cfg)
        )
        assert len(norms) == 2  # c1 and c3, not c2 (already array)
        paths = {n["path"] for n in norms}
        assert paths == {
            "parameters.blocks[0].codes[0].script",
            "parameters.blocks[1].codes[0].script",
        }
        assert out["parameters"]["blocks"][0]["codes"][1]["script"] == ["already array"]

    def test_bigquery_uses_sql_split(self) -> None:
        cfg = {
            "parameters": {
                "blocks": [
                    {
                        "name": "B",
                        "codes": [{"name": "c", "script": "SELECT 1; SELECT 2;"}],
                    }
                ]
            }
        }
        out, norms = normalize_blocks_codes_script(
            "keboola.google-bigquery-transformation", copy.deepcopy(cfg)
        )
        assert len(out["parameters"]["blocks"][0]["codes"][0]["script"]) == 2
        assert norms[0]["action"] == "sql_split"

    def test_duckdb_uses_sql_split(self) -> None:
        cfg = {
            "parameters": {
                "blocks": [
                    {
                        "name": "B",
                        "codes": [{"name": "c", "script": "SELECT 1; SELECT 2;"}],
                    }
                ]
            }
        }
        out, norms = normalize_blocks_codes_script(
            "keboola.duckdb-transformation", copy.deepcopy(cfg)
        )
        assert len(out["parameters"]["blocks"][0]["codes"][0]["script"]) == 2
        assert norms[0]["action"] == "sql_split"


# ---------------------------------------------------------------------------
# ConfigService integration
# ---------------------------------------------------------------------------


def _make_service(tmp_config_dir: Path, current_cfg: dict) -> tuple[ConfigService, MagicMock]:
    store = setup_single_project(tmp_config_dir)
    mock_client = MagicMock()
    mock_client.get_config_detail.return_value = {
        "id": "cfg-001",
        "name": "T",
        "description": "",
        "configuration": current_cfg,
    }
    mock_client.update_config.return_value = {
        "id": "cfg-001",
        "name": "T",
        "componentId": "keboola.snowflake-transformation",
    }
    service = ConfigService(
        config_store=store,
        client_factory=lambda url, token: mock_client,
    )
    return service, mock_client


class TestConfigServiceUpdateNormalizes:
    def test_sql_string_normalized_before_push(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir, current_cfg={})
        new_cfg = {
            "parameters": {
                "blocks": [
                    {
                        "name": "B",
                        "codes": [{"name": "c", "script": "SELECT 1; SELECT 2;"}],
                    }
                ]
            }
        }

        result = service.update_config(
            alias="prod",
            component_id="keboola.snowflake-transformation",
            config_id="cfg-001",
            configuration=new_cfg,
        )

        sent = client.update_config.call_args.kwargs["configuration"]
        # The script that reached the API is an ARRAY, not the original string.
        assert sent["parameters"]["blocks"][0]["codes"][0]["script"] == [
            "SELECT 1;",
            "SELECT 2;",
        ]
        # The result envelope exposes what was changed.
        assert len(result["normalizations"]) == 1
        assert result["normalizations"][0]["action"] == "sql_split"

    def test_passthrough_when_already_array(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir, current_cfg={})
        new_cfg = {
            "parameters": {
                "blocks": [
                    {
                        "name": "B",
                        "codes": [{"name": "c", "script": ["SELECT 1;", "SELECT 2;"]}],
                    }
                ]
            }
        }

        result = service.update_config(
            alias="prod",
            component_id="keboola.snowflake-transformation",
            config_id="cfg-001",
            configuration=new_cfg,
        )

        sent = client.update_config.call_args.kwargs["configuration"]
        assert sent["parameters"]["blocks"][0]["codes"][0]["script"] == [
            "SELECT 1;",
            "SELECT 2;",
        ]
        assert result["normalizations"] == []

    def test_dry_run_shows_normalized_new_configuration(self, tmp_config_dir: Path) -> None:
        """Dry-run output reflects what WOULD be pushed (post-normalize)."""
        service, client = _make_service(tmp_config_dir, current_cfg={"parameters": {}})
        new_cfg = {
            "parameters": {
                "blocks": [
                    {
                        "name": "B",
                        "codes": [{"name": "c", "script": "SELECT 1; SELECT 2;"}],
                    }
                ]
            }
        }

        result = service.update_config(
            alias="prod",
            component_id="keboola.snowflake-transformation",
            config_id="cfg-001",
            configuration=new_cfg,
            dry_run=True,
        )

        assert result["dry_run"] is True
        assert result["new_configuration"]["parameters"]["blocks"][0]["codes"][0]["script"] == [
            "SELECT 1;",
            "SELECT 2;",
        ]
        assert len(result["normalizations"]) == 1
        client.update_config.assert_not_called()

    def test_set_path_writing_string_script_is_normalized(self, tmp_config_dir: Path) -> None:
        """``--set parameters.blocks.0.codes.0.script="..."`` also normalizes."""
        current_cfg = {
            "parameters": {
                "blocks": [
                    {
                        "name": "B",
                        "codes": [{"name": "c", "script": ["original;"]}],
                    }
                ]
            }
        }
        service, client = _make_service(tmp_config_dir, current_cfg=current_cfg)

        service.update_config(
            alias="prod",
            component_id="keboola.snowflake-transformation",
            config_id="cfg-001",
            set_paths=[("parameters.blocks.0.codes.0.script", "SELECT 1; SELECT 2;")],
        )

        sent = client.update_config.call_args.kwargs["configuration"]
        # --set bypassed the array, but the post-write normalization caught it.
        assert sent["parameters"]["blocks"][0]["codes"][0]["script"] == [
            "SELECT 1;",
            "SELECT 2;",
        ]

    def test_non_transformation_component_passthrough(self, tmp_config_dir: Path) -> None:
        """Configs without blocks/codes/script schema are untouched."""
        service, client = _make_service(tmp_config_dir, current_cfg={})
        new_cfg = {"parameters": {"db": {"host": "x"}}}

        result = service.update_config(
            alias="prod",
            component_id="keboola.ex-db-mysql",
            config_id="cfg-001",
            configuration=new_cfg,
        )

        sent = client.update_config.call_args.kwargs["configuration"]
        assert sent == new_cfg
        assert result["normalizations"] == []


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


class TestConfigUpdateCliNormalization:
    def _invoke_json(self, tmp_config_dir: Path, args: list[str]) -> object:
        return runner.invoke(
            app,
            ["--json", "--config-dir", str(tmp_config_dir), "config", "update", *args],
        )

    def _invoke_human(self, tmp_config_dir: Path, args: list[str]) -> object:
        return runner.invoke(
            app,
            ["--config-dir", str(tmp_config_dir), "config", "update", *args],
        )

    def test_json_envelope_carries_normalizations(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.update_config.return_value = {
            "id": "cfg-001",
            "name": "T",
            "componentId": "keboola.snowflake-transformation",
        }

        cfg_payload = json.dumps(
            {
                "parameters": {
                    "blocks": [
                        {
                            "name": "B",
                            "codes": [{"name": "c", "script": "SELECT 1; SELECT 2;"}],
                        }
                    ]
                }
            }
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.config.get_service",
                lambda ctx, name: ConfigService(
                    config_store=store,
                    client_factory=lambda url, token: mock_client,
                ),
            )
            result = self._invoke_json(
                tmp_config_dir,
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.snowflake-transformation",
                    "--config-id",
                    "cfg-001",
                    "--configuration",
                    cfg_payload,
                ],
            )

        assert result.exit_code == 0, result.output
        envelope = json.loads(result.stdout)
        normalizations = envelope["data"]["normalizations"]
        assert len(normalizations) == 1
        assert normalizations[0]["action"] == "sql_split"
        assert normalizations[0]["after_length"] == 2

    def test_human_mode_emits_warning(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.update_config.return_value = {
            "id": "cfg-001",
            "name": "T",
            "componentId": "keboola.snowflake-transformation",
        }

        cfg_payload = json.dumps(
            {
                "parameters": {
                    "blocks": [
                        {
                            "name": "B",
                            "codes": [{"name": "c", "script": "SELECT 1; SELECT 2;"}],
                        }
                    ]
                }
            }
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "keboola_agent_cli.commands.config.get_service",
                lambda ctx, name: ConfigService(
                    config_store=store,
                    client_factory=lambda url, token: mock_client,
                ),
            )
            result = self._invoke_human(
                tmp_config_dir,
                [
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.snowflake-transformation",
                    "--config-id",
                    "cfg-001",
                    "--configuration",
                    cfg_payload,
                ],
            )

        assert result.exit_code == 0, result.output
        assert "Auto-normalized" in result.output
        assert "sql_split" in result.output
