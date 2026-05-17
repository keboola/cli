"""CLI-layer tests for the ``semantic-layer`` command group via CliRunner.

Mirrors the test_data_app_cli.py pattern: patch the cli.py service factory
so the runner sees a MagicMock; assert exit codes, JSON envelopes, and
the mutual-exclusion / permission-denied branches.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.constants import EXIT_PERMISSION_DENIED
from keboola_agent_cli.errors import ConfigError, ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.config_service import ConfigService
from keboola_agent_cli.services.job_service import JobService
from keboola_agent_cli.services.project_service import ProjectService

TEST_TOKEN = "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_config(
    config_dir: Path,
    projects: dict[str, dict] | None = None,
) -> ConfigStore:
    store = ConfigStore(config_dir=config_dir)
    if projects:
        for alias, info in projects.items():
            store.add_project(
                alias,
                ProjectConfig(
                    stack_url=info.get("stack_url", "https://connection.keboola.com"),
                    token=info["token"],
                    project_name=info.get("project_name", alias),
                    project_id=info.get("project_id", 1234),
                ),
            )
    return store


def _invoke(
    args: list[str],
    *,
    store: ConfigStore,
    sl_mock: MagicMock,
):
    """Run the CLI with cli.py services patched to mocks."""
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.ProjectService") as MockProj,
        patch("keboola_agent_cli.cli.ConfigService") as MockCfg,
        patch("keboola_agent_cli.cli.JobService") as MockJob,
        patch("keboola_agent_cli.cli.SemanticLayerService") as MockSL,
    ):
        MockStore.return_value = store
        MockProj.return_value = ProjectService(config_store=store)
        MockCfg.return_value = ConfigService(config_store=store)
        MockJob.return_value = JobService(config_store=store)
        MockSL.return_value = sl_mock
        return runner.invoke(app, args)


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    d = tmp_path / "config"
    d.mkdir()
    return d


@pytest.fixture
def store(cfg_dir: Path) -> ConfigStore:
    return _setup_config(cfg_dir, {"prod": {"token": TEST_TOKEN}})


# ---------------------------------------------------------------------------
# semantic-layer model list
# ---------------------------------------------------------------------------


class TestModelList:
    def test_json_success(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.list_models.return_value = {
            "project": "prod",
            "models": [
                {"id": "U1", "name": "default", "description": "", "sql_dialect": "Snowflake"}
            ],
        }
        result = _invoke(
            ["--json", "semantic-layer", "model", "list", "--project", "prod"],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["status"] == "ok"
        assert body["data"]["models"][0]["id"] == "U1"

    def test_human_empty(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.list_models.return_value = {"project": "prod", "models": []}
        result = _invoke(
            ["semantic-layer", "model", "list", "--project", "prod"],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        assert "No semantic-layer models" in result.output

    def test_config_error_exits_5(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.list_models.side_effect = ConfigError("Project 'ghost' not found.")
        result = _invoke(
            ["--json", "semantic-layer", "model", "list", "--project", "ghost"],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 5, result.output
        body = json.loads(result.output)
        assert body["error"]["code"] == "CONFIG_ERROR"

    def test_api_error_invalid_token_exits_3(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.list_models.side_effect = KeboolaApiError(
            message="bad token", status_code=401, error_code="INVALID_TOKEN"
        )
        result = _invoke(
            ["--json", "semantic-layer", "model", "list", "--project", "prod"],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 3
        body = json.loads(result.output)
        assert body["error"]["code"] == "INVALID_TOKEN"

    def test_missing_project_arg_exit_2(self, store: ConfigStore) -> None:
        mock = MagicMock()
        result = _invoke(
            ["--json", "semantic-layer", "model", "list"],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 2


# ---------------------------------------------------------------------------
# semantic-layer model create
# ---------------------------------------------------------------------------


class TestModelCreate:
    def test_create_success(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.create_model.return_value = {
            "project": "prod",
            "model": {
                "id": "new-uuid",
                "attributes": {"name": "default", "sql_dialect": "Snowflake"},
            },
        }
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "model",
                "create",
                "--project",
                "prod",
                "--name",
                "default",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["data"]["model"]["id"] == "new-uuid"

    def test_create_general_api_error_exits_1(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.create_model.side_effect = KeboolaApiError(
            message="boom", status_code=500, error_code=ErrorCode.API_ERROR
        )
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "model",
                "create",
                "--project",
                "prod",
                "--name",
                "x",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# semantic-layer model delete (with --yes)
# ---------------------------------------------------------------------------


class TestModelDelete:
    def test_delete_yes_success(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.delete_model.return_value = {
            "project": "prod",
            "deleted": {"id": "u1", "name": "default"},
            "cascade": {
                "attempted": True,
                "deleted": {
                    "datasets": 0,
                    "metrics": 0,
                    "relationships": 0,
                    "glossary": 0,
                    "constraints": 0,
                },
                "failures": [],
                "parent_deleted": True,
            },
            "orphaned_children": {
                "datasets": 0,
                "metrics": 0,
                "relationships": 0,
                "glossary": 0,
                "constraints": 0,
            },
        }
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "model",
                "delete",
                "--project",
                "prod",
                "--model",
                "default",
                "--yes",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["data"]["deleted"]["id"] == "u1"


# ---------------------------------------------------------------------------
# semantic-layer show
# ---------------------------------------------------------------------------


class TestShow:
    def test_show_summary_json(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.show_model.return_value = {
            "project": "prod",
            "model": {"id": "u1", "name": "default"},
            "datasets": [],
            "metrics": [],
            "relationships": [],
            "constraints": [],
            "glossary": [],
        }
        result = _invoke(
            ["--json", "semantic-layer", "show", "--project", "prod"],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["data"]["model"]["id"] == "u1"

    def test_show_with_type_filter(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.show_model.return_value = {
            "project": "prod",
            "model": {"id": "u1", "name": "default"},
            "metrics": [{"name": "rev", "id": "m1"}],
        }
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "show",
                "--project",
                "prod",
                "--type",
                "metric",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert "metrics" in body["data"]
        assert "datasets" not in body["data"]

    def test_show_human_renders_table(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.show_model.return_value = {
            "project": "prod",
            "model": {"id": "u1", "name": "default"},
            "datasets": [],
            "metrics": [],
            "relationships": [],
            "constraints": [],
            "glossary": [],
        }
        result = _invoke(
            ["semantic-layer", "show", "--project", "prod"],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0
        # Some kind of header / table marker
        assert "default" in result.output


# ---------------------------------------------------------------------------
# semantic-layer validate
# ---------------------------------------------------------------------------


class TestValidate:
    def test_validate_clean(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.validate_model.return_value = {
            "project": "prod",
            "model": {"id": "u1", "name": "default"},
            "errors": [],
            "warnings": [],
            "deep": False,
            "valid": True,
        }
        result = _invoke(
            ["--json", "semantic-layer", "validate", "--project", "prod"],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["data"]["valid"] is True

    def test_validate_deep_flag_propagates(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.validate_model.return_value = {
            "project": "prod",
            "model": {"id": "u1", "name": "default"},
            "errors": [],
            "warnings": [],
            "deep": True,
            "valid": True,
        }
        result = _invoke(
            ["--json", "semantic-layer", "validate", "--project", "prod", "--deep"],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0
        # Verify deep=True propagated to service
        _, kwargs = mock.validate_model.call_args
        assert kwargs["deep"] is True


# ---------------------------------------------------------------------------
# semantic-layer export
# ---------------------------------------------------------------------------


class TestExport:
    def test_export_json(self, store: ConfigStore, tmp_path: Path) -> None:
        mock = MagicMock()
        out = tmp_path / "snap.json"
        mock.export_model.return_value = {
            "exported_at": "2026-05-14T00:00:00Z",
            "project": "prod",
            "model": {"id": "u1", "name": "x"},
            "datasets": [],
            "metrics": [],
            "relationships": [],
            "constraints": [],
            "glossary": [],
            "counts": {
                "datasets": 0,
                "metrics": 0,
                "relationships": 0,
                "constraints": 0,
                "glossary": 0,
            },
            "path": str(out),
        }
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "export",
                "--project",
                "prod",
                "--output",
                str(out),
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["data"]["path"].endswith("snap.json")


# ---------------------------------------------------------------------------
# semantic-layer diff
# ---------------------------------------------------------------------------


class TestDiff:
    def test_diff_requires_one_of_project_or_file_for_each_side(self, store: ConfigStore) -> None:
        mock = MagicMock()
        # No --project-a or --file-a
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "diff",
                "--project-b",
                "prod",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 2
        body = json.loads(result.output)
        assert body["error"]["code"] == "USAGE_ERROR"
        mock.diff.assert_not_called()

    def test_diff_both_project_a_and_file_a_rejected(self, store: ConfigStore) -> None:
        mock = MagicMock()
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "diff",
                "--project-a",
                "prod",
                "--file-a",
                "snap.json",
                "--project-b",
                "prod",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 2

    def test_diff_happy_path(self, store: ConfigStore, tmp_path: Path) -> None:
        mock = MagicMock()
        mock.diff.return_value = {
            "left": {"source": "project", "ref": "prod", "model": {}},
            "right": {"source": "file", "ref": "x.json", "model": {}},
            "datasets": {"added": [], "removed": [], "changed": []},
            "metrics": {"added": [], "removed": [], "changed": []},
            "relationships": {"added": [], "removed": [], "changed": []},
            "constraints": {"added": [], "removed": [], "changed": []},
            "glossary": {"added": [], "removed": [], "changed": []},
        }
        f = tmp_path / "x.json"
        f.write_text("{}")
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "diff",
                "--project-a",
                "prod",
                "--file-b",
                str(f),
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# semantic-layer add metric|dataset|relationship|constraint|glossary
# ---------------------------------------------------------------------------


class TestAddMetric:
    def test_happy_path(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.add_metric.return_value = {
            "id": "m1",
            "attributes": {"name": "rev"},
        }
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "add",
                "metric",
                "--project",
                "prod",
                "--name",
                "rev",
                "--sql",
                "COUNT(*)",
                "--dataset",
                "out.c.t",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output

    def test_validation_error_exit_1(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.add_metric.side_effect = KeboolaApiError(
            message="bad", status_code=400, error_code=ErrorCode.VALIDATION_ERROR
        )
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "add",
                "metric",
                "--project",
                "prod",
                "--name",
                "rev",
                "--sql",
                "x",
                "--dataset",
                "out.c.t",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 1


class TestAddDataset:
    def test_happy_path(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.add_dataset.return_value = {
            "id": "d1",
            "attributes": {"name": "fact_x"},
        }
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "add",
                "dataset",
                "--project",
                "prod",
                "--name",
                "fact_x",
                "--table-id",
                "out.c-gold.FACT_X",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output


class TestAddRelationship:
    def test_happy_path(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.add_relationship.return_value = {"id": "r1", "attributes": {"name": "r"}}
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "add",
                "relationship",
                "--project",
                "prod",
                "--name",
                "r",
                "--from",
                "out.c.a",
                "--to",
                "out.c.b",
                "--on",
                "a.id=b.id",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output


class TestAddConstraint:
    def test_happy_path(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.add_constraint.return_value = {"id": "c1", "attributes": {"name": "rev_warning"}}
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "add",
                "constraint",
                "--project",
                "prod",
                "--name",
                "rev_warning",
                "--constraint-type",
                "inequality",
                "--rule",
                "value >= 0",
                "--metrics",
                "rev",
                "--severity",
                "warning",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output

    def test_empty_metrics_rejected(self, store: ConfigStore) -> None:
        """An empty --metrics value must exit 2 with USAGE_ERROR."""
        mock = MagicMock()
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "add",
                "constraint",
                "--project",
                "prod",
                "--name",
                "rev_warning",
                "--constraint-type",
                "inequality",
                "--rule",
                "x",
                "--metrics",
                ",,",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 2
        mock.add_constraint.assert_not_called()


class TestAddGlossary:
    def test_happy_path(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.add_glossary.return_value = {"id": "g1", "attributes": {"term": "GMV"}}
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "add",
                "glossary",
                "--project",
                "prod",
                "--term",
                "GMV",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# semantic-layer edit
# ---------------------------------------------------------------------------


class TestEditMetric:
    def test_rename_with_yes(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.edit_metric.return_value = {
            "updated": {"id": "m_new", "attributes": {"name": "revenue"}},
            "cascaded_constraints": [],
            "rollback": None,
        }
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "edit",
                "metric",
                "--project",
                "prod",
                "--name",
                "rev",
                "--new-name",
                "revenue",
                "--yes",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        _, kwargs = mock.edit_metric.call_args
        assert kwargs["assume_yes"] is True

    def test_metric_not_found_exit_1(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.edit_metric.side_effect = KeboolaApiError(
            message="not found", status_code=404, error_code=ErrorCode.NOT_FOUND
        )
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "edit",
                "metric",
                "--project",
                "prod",
                "--name",
                "ghost",
                "--new-sql",
                "1",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 1

    def test_partial_state_banner_in_human_output(self, store: ConfigStore) -> None:
        """Human-mode CLI must print PARTIAL STATE banner when service returns partial_state=True (issue #294)."""
        mock = MagicMock()
        mock.edit_metric.return_value = {
            "updated": {"id": "m_new", "attributes": {"name": "revenue"}},
            "cascaded_constraints": [
                {"constraint": "rev_warning", "status": "failed", "error": "boom"},
            ],
            "rollback": None,
            "partial_state": True,
            "recovery_hint": (
                "1 cascade constraint(s) failed to repoint to 'revenue'. "
                "Run `kbagent semantic-layer validate` to surface the dangling "
                "references, then re-run each failed cascade via "
                "`kbagent semantic-layer edit constraint --new-metrics ...`."
            ),
        }
        result = _invoke(
            [
                # No --json: exercise the human-mode renderer.
                "semantic-layer",
                "edit",
                "metric",
                "--project",
                "prod",
                "--name",
                "rev",
                "--new-name",
                "revenue",
                "--yes",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        assert "PARTIAL STATE" in result.output
        assert "Recovery:" in result.output

    def test_partial_state_in_json_output(self, store: ConfigStore) -> None:
        """--json mode must surface partial_state + recovery_hint at the envelope top level (issue #294, NB-2).

        Catches a regression where the formatter layer strips or renames the
        new keys -- the service-layer test wouldn't catch that; AI agents and
        kbagent serve consumers always use --json, so this path is what they
        depend on.
        """
        mock = MagicMock()
        mock.edit_metric.return_value = {
            "updated": {"id": "m_new", "attributes": {"name": "revenue"}},
            "cascaded_constraints": [
                {"constraint": "rev_warning", "status": "failed", "error": "boom"},
            ],
            "rollback": None,
            "partial_state": True,
            "recovery_hint": "Re-run `kbagent semantic-layer validate` and re-cascade manually.",
        }
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "edit",
                "metric",
                "--project",
                "prod",
                "--name",
                "rev",
                "--new-name",
                "revenue",
                "--yes",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["status"] == "ok"
        assert body["data"]["partial_state"] is True
        assert body["data"]["recovery_hint"] is not None
        assert "validate" in body["data"]["recovery_hint"]


class TestEditDataset:
    def test_happy_path(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.edit_dataset.return_value = {
            "updated": {"id": "d1", "attributes": {"name": "fact_x"}},
            "cascaded_constraints": [],
            "rollback": None,
        }
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "edit",
                "dataset",
                "--project",
                "prod",
                "--name",
                "fact_x",
                "--new-description",
                "updated",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0


class TestEditConstraint:
    def test_metrics_list_parsed(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.edit_constraint.return_value = {
            "updated": {"id": "c1", "attributes": {"name": "rev_warning"}},
            "cascaded_constraints": [],
            "rollback": None,
        }
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "edit",
                "constraint",
                "--project",
                "prod",
                "--name",
                "rev_warning",
                "--new-metrics",
                "rev, profit",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0
        _, kwargs = mock.edit_constraint.call_args
        assert kwargs["new_metrics"] == ["rev", "profit"]


# ---------------------------------------------------------------------------
# semantic-layer remove (with orphan warning + prompts)
# ---------------------------------------------------------------------------


class TestRemove:
    def test_remove_metric_yes_skips_prompt(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.preview_remove.return_value = {
            "kind": "metric",
            "id": "m1",
            "name": "rev",
            "orphaned_constraints": [],
        }
        mock.remove_item.return_value = {
            "removed": {"type": "semantic-metric", "id": "m1", "name": "rev"},
            "orphaned_constraints": [],
        }
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "remove",
                "metric",
                "--project",
                "prod",
                "--name",
                "rev",
                "--yes",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        mock.remove_item.assert_called_once()

    def test_remove_without_yes_in_non_tty_exit_2(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.preview_remove.return_value = {
            "kind": "metric",
            "id": "m1",
            "name": "rev",
            "orphaned_constraints": [],
        }
        # Default CliRunner has non-TTY stdin
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "remove",
                "metric",
                "--project",
                "prod",
                "--name",
                "rev",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 2
        mock.remove_item.assert_not_called()

    def test_remove_relationship_yes(self, store: ConfigStore) -> None:
        """`remove relationship` is destructive but never orphans (leaf entity)."""
        mock = MagicMock()
        mock.preview_remove.return_value = {
            "kind": "relationship",
            "id": "r1",
            "name": "fact_to_dim",
            "orphaned_constraints": [],
        }
        mock.remove_item.return_value = {
            "removed": {"type": "semantic-relationship", "id": "r1", "name": "fact_to_dim"},
            "orphaned_constraints": [],
        }
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "remove",
                "relationship",
                "--project",
                "prod",
                "--name",
                "fact_to_dim",
                "--yes",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        _, kwargs = mock.remove_item.call_args
        assert kwargs["kind"] == "relationship"

    def test_remove_glossary_yes_uses_term(self, store: ConfigStore) -> None:
        """`remove glossary --term ...` -- term is the identity for glossary."""
        mock = MagicMock()
        mock.preview_remove.return_value = {
            "kind": "glossary",
            "id": "g1",
            "name": "MRR",
            "orphaned_constraints": [],
        }
        mock.remove_item.return_value = {
            "removed": {"type": "semantic-glossary", "id": "g1", "name": "MRR"},
            "orphaned_constraints": [],
        }
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "remove",
                "glossary",
                "--project",
                "prod",
                "--term",
                "MRR",
                "--yes",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        _, kwargs = mock.remove_item.call_args
        assert kwargs["kind"] == "glossary"
        assert kwargs["name"] == "MRR"


# ---------------------------------------------------------------------------
# semantic-layer edit relationship / glossary (NB-5)
# ---------------------------------------------------------------------------


class TestEditRelationship:
    def test_edit_relationship_happy(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.edit_relationship.return_value = {
            "updated": {"id": "r2", "attributes": {"name": "fact_to_dim"}},
            "cascaded_constraints": [],
            "rollback": None,
        }
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "edit",
                "relationship",
                "--project",
                "prod",
                "--name",
                "fact_to_dim",
                "--new-from",
                "out.c.fact_v2",
                "--new-type",
                "inner",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        _, kwargs = mock.edit_relationship.call_args
        assert kwargs["new_from"] == "out.c.fact_v2"
        assert kwargs["new_type"] == "inner"

    def test_edit_relationship_propagates_service_error(self, store: ConfigStore) -> None:
        """Service-layer VALIDATION_ERROR surfaces as exit 1 with envelope."""
        from keboola_agent_cli.errors import ErrorCode as EC
        from keboola_agent_cli.errors import KeboolaApiError

        mock = MagicMock()
        mock.edit_relationship.side_effect = KeboolaApiError(
            message="bogus type",
            error_code=EC.VALIDATION_ERROR,
        )
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "edit",
                "relationship",
                "--project",
                "prod",
                "--name",
                "x",
                "--new-type",
                "bogus",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 1
        assert "VALIDATION_ERROR" in result.output


class TestEditGlossary:
    def test_edit_definition_only(self, store: ConfigStore) -> None:
        """Definition-only edit needs no --yes (term identity is preserved)."""
        mock = MagicMock()
        mock.edit_glossary.return_value = {
            "updated": {"id": "g2", "attributes": {"term": "MRR"}},
            "cascaded_constraints": [],
            "rollback": None,
        }
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "edit",
                "glossary",
                "--project",
                "prod",
                "--term",
                "MRR",
                "--new-definition",
                "Updated def",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        _, kwargs = mock.edit_glossary.call_args
        assert kwargs["new_definition"] == "Updated def"
        assert kwargs["new_term"] is None

    def test_edit_rename_term_without_yes_in_non_tty_exit_2(self, store: ConfigStore) -> None:
        """Renaming the term is destructive; non-TTY without --yes refuses."""
        mock = MagicMock()
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "edit",
                "glossary",
                "--project",
                "prod",
                "--term",
                "MRR",
                "--new-term",
                "RECURRING_REVENUE",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 2
        mock.edit_glossary.assert_not_called()

    def test_edit_rename_term_with_yes(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.edit_glossary.return_value = {
            "updated": {"id": "g2", "attributes": {"term": "RECURRING_REVENUE"}},
            "cascaded_constraints": [],
            "rollback": None,
        }
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "edit",
                "glossary",
                "--project",
                "prod",
                "--term",
                "MRR",
                "--new-term",
                "RECURRING_REVENUE",
                "--yes",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        _, kwargs = mock.edit_glossary.call_args
        assert kwargs["new_term"] == "RECURRING_REVENUE"


# ---------------------------------------------------------------------------
# semantic-layer import
# ---------------------------------------------------------------------------


class TestImport:
    def test_dry_run(self, store: ConfigStore, tmp_path: Path) -> None:
        mock = MagicMock()
        mock.import_snapshot.return_value = {
            "target_project": "prod",
            "target_model": "u1",
            "source_model": "src",
            "dry_run": True,
            "overwrite": False,
            "imported": {},
        }
        snap = tmp_path / "snap.json"
        snap.write_text("{}")
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "import",
                "--project",
                "prod",
                "--file",
                str(snap),
                "--dry-run",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        _, kwargs = mock.import_snapshot.call_args
        assert kwargs["dry_run"] is True


# ---------------------------------------------------------------------------
# semantic-layer promote
# ---------------------------------------------------------------------------


class TestPromote:
    def test_promote_with_yes(self, tmp_path: Path) -> None:
        cfg = tmp_path / "cfg"
        cfg.mkdir()
        st = _setup_config(
            cfg,
            {
                "source": {"token": TEST_TOKEN, "project_id": 1},
                "target": {"token": TEST_TOKEN, "project_id": 2},
            },
        )
        mock = MagicMock()
        mock.promote_model.return_value = {
            "from_project": "source",
            "to_project": "target",
            "from_model": "us",
            "to_model": "ut",
            "dry_run": False,
            "datasets": {"new": 0, "overwritten": 0, "identical": 0, "failed": [], "changes": []},
            "metrics": {"new": 0, "overwritten": 0, "identical": 0, "failed": [], "changes": []},
        }
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "promote",
                "--from-project",
                "source",
                "--to-project",
                "target",
                "--yes",
            ],
            store=st,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# semantic-layer build
# ---------------------------------------------------------------------------


class TestBuild:
    def test_missing_tables_exit_2(self, store: ConfigStore) -> None:
        mock = MagicMock()
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "build",
                "--project",
                "prod",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 2
        body = json.loads(result.output)
        assert body["error"]["code"] == "MISSING_PARAMETER"
        mock.build_model.assert_not_called()

    def test_dry_run_happy(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.build_model.return_value = {
            "project": "prod",
            "dry_run": True,
            "fallback_used": "heuristic",
            "fetch_errors": [],
            "generated": {
                "name": "kbagent_build_model",
                "datasets": [{"name": "ds"}],
                "metrics": [{"name": "ds_row_count"}],
                "relationships": [],
                "constraints": [],
                "glossary": [{"term": "ds"}],
            },
            "validation": {"errors": [], "warnings": []},
            "validated": True,
        }
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "build",
                "--project",
                "prod",
                "--tables",
                "out.c.t",
                "--dry-run",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["data"]["fallback_used"] == "heuristic"

    def test_keep_on_failure_flag_propagates_through_cli(self, store: ConfigStore) -> None:
        """The --keep-on-failure flag must propagate to build_model kwargs (issue #295).

        Service-side rollback semantics are covered by
        TestBuildModelRollback in test_semantic_layer_service.py.
        """
        mock = MagicMock()
        mock.build_model.return_value = {
            "project": "prod",
            "dry_run": True,
            "keep_on_failure": True,
            "fallback_used": "heuristic",
            "fetch_errors": [],
            "generated": {
                "name": "kbagent_build_model",
                "datasets": [],
                "metrics": [],
                "relationships": [],
                "constraints": [],
                "glossary": [],
            },
            "validation": {"errors": [], "warnings": []},
            "validated": True,
        }
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "build",
                "--project",
                "prod",
                "--tables",
                "out.c.t",
                "--dry-run",
                "--keep-on-failure",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        _, kwargs = mock.build_model.call_args
        assert kwargs["keep_on_failure"] is True


# ---------------------------------------------------------------------------
# semantic-layer token --encrypt
# ---------------------------------------------------------------------------


class TestToken:
    def test_without_encrypt_flag_exit_2(self, store: ConfigStore) -> None:
        mock = MagicMock()
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "token",
                "--project",
                "prod",
                "--component-id",
                "keboola.ex-db-snowflake",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 2
        body = json.loads(result.output)
        assert body["error"]["code"] == "USAGE_ERROR"
        mock.encrypt_token.assert_not_called()

    def test_with_encrypt_success(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.encrypt_token.return_value = {
            "project": "prod",
            "component_id": "keboola.ex-db-snowflake",
            "encrypted": {"#metastore_token": "KBC::ProjectSecureGKMS::cipher"},
        }
        result = _invoke(
            [
                "--json",
                "semantic-layer",
                "token",
                "--encrypt",
                "--project",
                "prod",
                "--component-id",
                "keboola.ex-db-snowflake",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["data"]["encrypted"]["#metastore_token"].startswith("KBC::")


# ---------------------------------------------------------------------------
# Permission gating: --deny-writes blocks write subcommands
# ---------------------------------------------------------------------------


class TestPermissions:
    def test_deny_writes_blocks_add_metric(self, store: ConfigStore) -> None:
        mock = MagicMock()
        result = _invoke(
            [
                "--deny-writes",
                "--json",
                "semantic-layer",
                "add",
                "metric",
                "--project",
                "prod",
                "--name",
                "rev",
                "--sql",
                "x",
                "--dataset",
                "out.c.t",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == EXIT_PERMISSION_DENIED
        mock.add_metric.assert_not_called()

    def test_deny_writes_blocks_model_create(self, store: ConfigStore) -> None:
        mock = MagicMock()
        result = _invoke(
            [
                "--deny-writes",
                "--json",
                "semantic-layer",
                "model",
                "create",
                "--project",
                "prod",
                "--name",
                "x",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == EXIT_PERMISSION_DENIED
        mock.create_model.assert_not_called()

    def test_deny_writes_blocks_edit_metric(self, store: ConfigStore) -> None:
        mock = MagicMock()
        result = _invoke(
            [
                "--deny-writes",
                "--json",
                "semantic-layer",
                "edit",
                "metric",
                "--project",
                "prod",
                "--name",
                "rev",
                "--new-sql",
                "1",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == EXIT_PERMISSION_DENIED
        mock.edit_metric.assert_not_called()

    def test_deny_destructive_blocks_remove(self, store: ConfigStore) -> None:
        mock = MagicMock()
        result = _invoke(
            [
                "--deny-destructive",
                "--json",
                "semantic-layer",
                "remove",
                "metric",
                "--project",
                "prod",
                "--name",
                "rev",
                "--yes",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == EXIT_PERMISSION_DENIED
        mock.remove_item.assert_not_called()

    def test_read_subcommand_allowed_with_deny_writes(self, store: ConfigStore) -> None:
        """--deny-writes must NOT block read subcommands like `show`."""
        mock = MagicMock()
        mock.show_model.return_value = {
            "project": "prod",
            "model": {"id": "u1", "name": "x"},
            "datasets": [],
            "metrics": [],
            "relationships": [],
            "constraints": [],
            "glossary": [],
        }
        result = _invoke(
            ["--deny-writes", "--json", "semantic-layer", "show", "--project", "prod"],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output

    def test_token_subcommand_denied_with_deny_writes(self, store: ConfigStore) -> None:
        """`token --encrypt` is classified `write` (parity with `encrypt.values`).

        It calls EncryptService to mint ciphertext that downstream agents could
        paste into a transformation config; treating it as `read` would let
        ``--deny-writes`` callers emit secrets the firewall meant to gate.
        """
        mock = MagicMock()
        mock.encrypt_token.return_value = {
            "project": "prod",
            "component_id": "keboola.ex-db-snowflake",
            "encrypted": {"#metastore_token": "KBC::ProjectSecureGKMS::cipher"},
        }
        result = _invoke(
            [
                "--deny-writes",
                "--json",
                "semantic-layer",
                "token",
                "--encrypt",
                "--project",
                "prod",
                "--component-id",
                "keboola.ex-db-snowflake",
            ],
            store=store,
            sl_mock=mock,
        )
        # EXIT_PERMISSION_DENIED == 6
        assert result.exit_code == 6, result.output
        assert "PERMISSION_DENIED" in result.output

    def test_model_list_allowed_with_deny_writes(self, store: ConfigStore) -> None:
        """`model list` is read-only and must succeed under --deny-writes.

        Regression test for iter-2/iter-3: the parent `semantic-layer`
        callback fires before the `model` sub-app's per-subcommand callback,
        so the parent-level operation key `semantic-layer.model` must exist
        in the registry at the least-privileged (read) classification.
        Without it, fail-closed defaults to `write` and `model list` is
        denied even though the leaf is correctly classified as read.
        """
        mock = MagicMock()
        mock.list_models.return_value = {"project": "prod", "models": []}
        result = _invoke(
            [
                "--deny-writes",
                "--json",
                "semantic-layer",
                "model",
                "list",
                "--project",
                "prod",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        assert "PERMISSION_DENIED" not in result.output

    def test_model_create_denied_with_deny_writes(self, store: ConfigStore) -> None:
        """`model create` mutates state and must be denied under --deny-writes."""
        mock = MagicMock()
        mock.create_model.return_value = {
            "project": "prod",
            "model": {"id": "x", "attributes": {"name": "x"}},
        }
        result = _invoke(
            [
                "--deny-writes",
                "--json",
                "semantic-layer",
                "model",
                "create",
                "--project",
                "prod",
                "--name",
                "x",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 6, result.output
        assert "PERMISSION_DENIED" in result.output

    def test_model_delete_denied_with_deny_destructive(self, store: ConfigStore) -> None:
        """`model delete` is destructive — narrower flag still blocks it."""
        mock = MagicMock()
        mock.delete_model.return_value = {"deleted": {"id": "x", "name": "x"}}
        result = _invoke(
            [
                "--deny-destructive",
                "--json",
                "semantic-layer",
                "model",
                "delete",
                "--project",
                "prod",
                "--model",
                "x",
                "--yes",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 6, result.output
        assert "PERMISSION_DENIED" in result.output
