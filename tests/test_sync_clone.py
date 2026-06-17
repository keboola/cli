"""Unit tests for the pure clone helpers (sync/clone.py, issue #426).

These exercise the on-disk + in-memory-manifest mechanics of the three
declarative overrides (bucket_map, variable_values, instance_rename) plus the
tree copy and manifest re-point -- no API client involved.
"""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services._sync_bindings import resolve_flow_task_bindings
from keboola_agent_cli.services.sync_service import CreatedConfig, SyncService
from keboola_agent_cli.sync.clone import (
    apply_bucket_map,
    apply_instance_rename,
    apply_variable_values,
    branch_path_map,
    copy_reference_tree,
    repoint_manifest_project,
)
from keboola_agent_cli.sync.manifest import (
    Manifest,
    ManifestBranch,
    ManifestConfigRow,
    ManifestConfiguration,
    ManifestNaming,
    ManifestProject,
)


def _manifest(configurations: list[ManifestConfiguration]) -> Manifest:
    return Manifest(
        project=ManifestProject(id=1, apiHost="connection.keboola.com"),
        naming=ManifestNaming(),
        branches=[ManifestBranch(id=0, path="main")],
        configurations=configurations,
    )


def _write_config(root: Path, rel_path: str, data: dict[str, Any]) -> Path:
    config_dir = root / "main" / rel_path
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "_config.yml").write_text(yaml.dump(data, sort_keys=False), encoding="utf-8")
    return config_dir


def _read_config(root: Path, rel_path: str) -> dict[str, Any]:
    return yaml.safe_load((root / "main" / rel_path / "_config.yml").read_text())


class TestBranchPathMap:
    def test_maps_branch_id_to_dir(self) -> None:
        m = _manifest([])
        assert branch_path_map(m) == {0: "main"}


class TestCopyAndRepoint:
    def test_copy_reference_tree(self, tmp_path: Path) -> None:
        source = tmp_path / "src"
        (source / ".keboola").mkdir(parents=True)
        (source / ".keboola" / "manifest.json").write_text("{}")
        (source / "main").mkdir()
        target = tmp_path / "dst"
        copy_reference_tree(source, target)
        assert (target / ".keboola" / "manifest.json").exists()
        assert (target / "main").is_dir()

    def test_copy_refuses_existing_target(self, tmp_path: Path) -> None:
        source = tmp_path / "src"
        source.mkdir()
        target = tmp_path / "dst"
        target.mkdir()
        with pytest.raises(FileExistsError):
            copy_reference_tree(source, target)

    def test_repoint_manifest_project(self) -> None:
        m = _manifest([])
        repoint_manifest_project(m, project_id=999, api_host="other.keboola.com")
        assert m.project.id == 999
        assert m.project.api_host == "other.keboola.com"


class TestBucketMap:
    def _setup(self, tmp_path: Path) -> Manifest:
        _write_config(
            tmp_path,
            "extractor/keboola.ex-db/source",
            {
                "name": "Source",
                "input": {"tables": [{"source": "in.c-old.customers", "destination": "customers"}]},
                "output": {"tables": [{"source": "result", "destination": "out.c-old.result"}]},
                "_keboola": {"component_id": "keboola.ex-db", "config_id": "g1"},
            },
        )
        return _manifest(
            [
                ManifestConfiguration(
                    branchId=0,
                    componentId="keboola.ex-db",
                    id="g1",
                    path="extractor/keboola.ex-db/source",
                )
            ]
        )

    def test_rewrites_input_source_and_output_destination(self, tmp_path: Path) -> None:
        manifest = self._setup(tmp_path)
        n = apply_bucket_map(tmp_path, manifest, {"in.c-old": "in.c-new", "out.c-old": "out.c-new"})
        assert n == 2
        data = _read_config(tmp_path, "extractor/keboola.ex-db/source")
        assert data["input"]["tables"][0]["source"] == "in.c-new.customers"
        assert data["output"]["tables"][0]["destination"] == "out.c-new.result"
        # the non-bucket destination/source values are untouched
        assert data["input"]["tables"][0]["destination"] == "customers"
        assert data["output"]["tables"][0]["source"] == "result"

    def test_empty_map_is_noop(self, tmp_path: Path) -> None:
        manifest = self._setup(tmp_path)
        assert apply_bucket_map(tmp_path, manifest, {}) == 0

    def test_bucket_level_reference_mapped_whole(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            "extractor/keboola.ex-db/c",
            {"input": {"tables": [{"source": "in.c-old"}]}, "name": "c"},
        )
        manifest = _manifest(
            [
                ManifestConfiguration(
                    branchId=0,
                    componentId="keboola.ex-db",
                    id="g",
                    path="extractor/keboola.ex-db/c",
                )
            ]
        )
        apply_bucket_map(tmp_path, manifest, {"in.c-old": "in.c-new"})
        assert (
            _read_config(tmp_path, "extractor/keboola.ex-db/c")["input"]["tables"][0]["source"]
            == "in.c-new"
        )


class TestVariableValues:
    def _setup(self, tmp_path: Path) -> Manifest:
        row = ManifestConfigRow(id="r1", path="variables/rows/default")
        _write_config(
            tmp_path,
            "variables/rows/default",
            {
                "name": "default",
                "values": [
                    {"name": "db_host", "value": "old-host", "type": "string"},
                    {"name": "db_port", "value": "5432"},
                ],
                "_keboola": {"component_id": "keboola.variables", "row_id": "r1"},
            },
        )
        return _manifest(
            [
                ManifestConfiguration(
                    branchId=0,
                    componentId="keboola.variables",
                    id="v1",
                    path="variables",
                    rows=[row],
                )
            ]
        )

    def test_overrides_matching_values(self, tmp_path: Path) -> None:
        manifest = self._setup(tmp_path)
        n = apply_variable_values(tmp_path, manifest, {"db_host": "new-host"})
        assert n == 1
        values = _read_config(tmp_path, "variables/rows/default")["values"]
        assert values[0]["value"] == "new-host"
        assert values[1]["value"] == "5432"  # untouched

    def test_coerces_value_to_string(self, tmp_path: Path) -> None:
        manifest = self._setup(tmp_path)
        # Deliberately pass a non-str value to verify the helper coerces via str().
        apply_variable_values(tmp_path, manifest, {"db_port": 9999})  # ty: ignore[invalid-argument-type]
        values = _read_config(tmp_path, "variables/rows/default")["values"]
        assert values[1]["value"] == "9999"

    def test_ignores_non_variables_components(self, tmp_path: Path) -> None:
        _write_config(
            tmp_path,
            "extractor/keboola.ex-db/c",
            {"values": [{"name": "x", "value": "1"}], "name": "c"},
        )
        manifest = _manifest(
            [
                ManifestConfiguration(
                    branchId=0,
                    componentId="keboola.ex-db",
                    id="g",
                    path="extractor/keboola.ex-db/c",
                )
            ]
        )
        assert apply_variable_values(tmp_path, manifest, {"x": "2"}) == 0


class TestInstanceRename:
    def test_renames_dir_and_manifest_paths(self, tmp_path: Path) -> None:
        row = ManifestConfigRow(id="r1", path="extractor/keboola.ex-db/Acme/rows/ep")
        _write_config(tmp_path, "extractor/keboola.ex-db/Acme", {"name": "Acme cfg"})
        _write_config(tmp_path, "extractor/keboola.ex-db/Acme/rows/ep", {"name": "ep"})
        manifest = _manifest(
            [
                ManifestConfiguration(
                    branchId=0,
                    componentId="keboola.ex-db",
                    id="g1",
                    path="extractor/keboola.ex-db/Acme",
                    rows=[row],
                )
            ]
        )
        n = apply_instance_rename(
            tmp_path,
            manifest,
            {"extractor/keboola.ex-db/Acme": "extractor/keboola.ex-db/Globex"},
        )
        assert n == 1
        # on-disk subtree moved
        assert not (tmp_path / "main" / "extractor/keboola.ex-db/Acme").exists()
        assert (tmp_path / "main" / "extractor/keboola.ex-db/Globex" / "_config.yml").exists()
        assert (
            tmp_path / "main" / "extractor/keboola.ex-db/Globex/rows/ep" / "_config.yml"
        ).exists()
        # manifest paths rewritten (config + row)
        assert manifest.configurations[0].path == "extractor/keboola.ex-db/Globex"
        assert manifest.configurations[0].rows[0].path == "extractor/keboola.ex-db/Globex/rows/ep"

    def test_empty_renames_noop(self, tmp_path: Path) -> None:
        manifest = _manifest([])
        assert apply_instance_rename(tmp_path, manifest, {}) == 0


# ---------------------------------------------------------------------------
# Phase D: flow task configId remap (exercised via the service method)
# ---------------------------------------------------------------------------

FAKE_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"


def _service(tmp_config_dir: Path, client: MagicMock) -> SyncService:
    store = ConfigStore(config_dir=tmp_config_dir)
    store.add_project(
        "target",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token=FAKE_TOKEN,
            project_name="Target",
            project_id=4242,
        ),
    )
    return SyncService(config_store=store, client_factory=lambda url, token: client)


class TestResolveFlowTaskBindings:
    def test_remaps_and_puts_flow(self, tmp_path: Path, tmp_config_dir: Path) -> None:
        # On-disk flow config with a job task pointing at a reference id.
        flow_dir = _write_config(
            tmp_path,
            "other/keboola.flow/Flow",
            {
                "name": "My Flow",
                "_configuration_extra": {
                    "phases": [{"id": "p1", "name": "Extract", "next": []}],
                    "tasks": [
                        {
                            "id": "t1",
                            "phase": "p1",
                            "task": {
                                "type": "job",
                                "componentId": "keboola.ex-http",
                                "configId": "ext-golden",
                            },
                        }
                    ],
                },
                "_keboola": {"component_id": "keboola.flow", "config_id": "flow-new"},
            },
        )
        manifest = _manifest(
            [
                ManifestConfiguration(
                    branchId=0,
                    componentId="keboola.flow",
                    id="flow-new",
                    path="other/keboola.flow/Flow",
                )
            ]
        )

        client = MagicMock()
        svc = _service(tmp_config_dir, client)
        created = [CreatedConfig("keboola.flow", "flow-new", flow_dir)]
        created_id_map = {("keboola.ex-http", "ext-golden"): "ext-new"}

        result = resolve_flow_task_bindings(
            svc,
            client,
            created_configs=created,
            created_id_map=created_id_map,
            manifest=manifest,
            branch_id=None,
        )

        assert result.configs_rewritten == 1
        assert result.tasks_remapped == 1
        # The flow was PUT with the remapped task configId.
        client.update_config.assert_called_once()
        put_config = client.update_config.call_args.kwargs["configuration"]
        assert put_config["tasks"][0]["task"]["configId"] == "ext-new"
        # The local file was rewritten too.
        on_disk = _read_config(tmp_path, "other/keboola.flow/Flow")
        assert on_disk["_configuration_extra"]["tasks"][0]["task"]["configId"] == "ext-new"
        # Manifest hashes were refreshed (no longer empty).
        assert manifest.configurations[0].metadata.get("pull_hash")

    def test_noop_when_no_match(self, tmp_path: Path, tmp_config_dir: Path) -> None:
        flow_dir = _write_config(
            tmp_path,
            "other/keboola.flow/Flow",
            {
                "name": "F",
                "_configuration_extra": {
                    "tasks": [
                        {
                            "id": "t1",
                            "task": {
                                "type": "job",
                                "componentId": "keboola.ex-http",
                                "configId": "preexisting",
                            },
                        }
                    ]
                },
                "_keboola": {"component_id": "keboola.flow", "config_id": "flow-new"},
            },
        )
        manifest = _manifest(
            [
                ManifestConfiguration(
                    branchId=0,
                    componentId="keboola.flow",
                    id="flow-new",
                    path="other/keboola.flow/Flow",
                )
            ]
        )
        client = MagicMock()
        svc = _service(tmp_config_dir, client)
        result = resolve_flow_task_bindings(
            svc,
            client,
            created_configs=[CreatedConfig("keboola.flow", "flow-new", flow_dir)],
            created_id_map={("keboola.ex-http", "other-golden"): "x"},
            manifest=manifest,
            branch_id=None,
        )
        assert result.tasks_remapped == 0
        client.update_config.assert_not_called()


# ---------------------------------------------------------------------------
# clone_project orchestration (diff/push mocked to isolate the composite logic)
# ---------------------------------------------------------------------------


def _golden_source(root: Path) -> None:
    """A minimal reference synced tree: an extractor + a flow that targets it."""
    (root / ".keboola").mkdir(parents=True)
    _write_config(
        root,
        "extractor/keboola.ex-db/source",
        {
            "name": "Source",
            "input": {"tables": [{"source": "in.c-ref.customers", "destination": "customers"}]},
            "_keboola": {"component_id": "keboola.ex-db", "config_id": "ext-golden"},
        },
    )
    manifest = _manifest(
        [
            ManifestConfiguration(
                branchId=0,
                componentId="keboola.ex-db",
                id="ext-golden",
                path="extractor/keboola.ex-db/source",
            )
        ]
    )
    from keboola_agent_cli.sync.manifest import save_manifest

    save_manifest(root, manifest)


class TestCloneProjectOrchestration:
    def test_clone_applies_overrides_and_pushes(
        self, tmp_path: Path, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = tmp_path / "golden"
        _golden_source(source)
        target_dir = tmp_path / "clone"

        svc = _service(tmp_config_dir, MagicMock())
        push_mock = MagicMock(
            return_value={"status": "pushed", "created": 1, "flow_task_remaps": 0, "errors": []}
        )
        monkeypatch.setattr(
            svc,
            "diff",
            MagicMock(
                return_value={
                    "changes": [{"change_type": "added", "component_id": "keboola.ex-db"}]
                }
            ),
        )
        monkeypatch.setattr(svc, "push", push_mock)

        result = svc.clone_project(
            source=source,
            target_alias="target",
            target_dir=target_dir,
            overrides={"bucket_map": {"in.c-ref": "in.c-prod"}},
        )

        assert result["status"] == "cloned"
        assert result["created"] == 1
        assert result["bucket_rewrites"] == 1
        # the override was applied to the COPIED tree
        copied = _read_config(target_dir, "extractor/keboola.ex-db/source")
        assert copied["input"]["tables"][0]["source"] == "in.c-prod.customers"
        # the manifest was re-pointed at the target project
        from keboola_agent_cli.sync.manifest import load_manifest

        assert load_manifest(target_dir).project.id == 4242
        push_mock.assert_called_once()

    def test_fresh_target_guard_rejects_collision(
        self, tmp_path: Path, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = tmp_path / "golden"
        _golden_source(source)
        svc = _service(tmp_config_dir, MagicMock())
        # diff reports a non-'added' change -> target already has the config.
        monkeypatch.setattr(
            svc,
            "diff",
            MagicMock(
                return_value={
                    "changes": [
                        {
                            "change_type": "modified",
                            "component_id": "keboola.ex-db",
                            "config_id": "ext-golden",
                        }
                    ]
                }
            ),
        )
        push_mock = MagicMock()
        monkeypatch.setattr(svc, "push", push_mock)

        with pytest.raises(ConfigError, match="fresh target"):
            svc.clone_project(source=source, target_alias="target", target_dir=tmp_path / "clone")
        push_mock.assert_not_called()

    def test_idempotent_rerun_skips_copy(
        self, tmp_path: Path, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = tmp_path / "golden"
        _golden_source(source)
        target_dir = tmp_path / "clone"

        svc = _service(tmp_config_dir, MagicMock())
        monkeypatch.setattr(
            svc,
            "diff",
            MagicMock(
                return_value={
                    "changes": [{"change_type": "added", "component_id": "keboola.ex-db"}]
                }
            ),
        )
        monkeypatch.setattr(
            svc, "push", MagicMock(return_value={"status": "pushed", "created": 1, "errors": []})
        )
        svc.clone_project(source=source, target_alias="target", target_dir=target_dir)

        # Second run: target_dir exists -> already-cloned path. push now reports no_changes.
        monkeypatch.setattr(
            svc,
            "push",
            MagicMock(return_value={"status": "no_changes", "created": 0, "errors": []}),
        )
        result = svc.clone_project(
            source=source,
            target_alias="target",
            target_dir=target_dir,
            overrides={"bucket_map": {"in.c-ref": "in.c-prod"}},
        )
        assert result["status"] == "no_changes"
        assert result["created"] == 0
        # overrides are NOT re-applied on a re-run (copy was skipped)
        assert result["bucket_rewrites"] == 0

    def test_dry_run_reports_diff_without_push(
        self, tmp_path: Path, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = tmp_path / "golden"
        _golden_source(source)
        svc = _service(tmp_config_dir, MagicMock())
        monkeypatch.setattr(
            svc,
            "diff",
            MagicMock(return_value={"summary": {"added": 1, "modified": 0, "deleted": 0}}),
        )
        push_mock = MagicMock()
        monkeypatch.setattr(svc, "push", push_mock)
        result = svc.clone_project(
            source=source,
            target_alias="target",
            target_dir=tmp_path / "clone",
            dry_run=True,
        )
        assert result["status"] == "dry_run"
        push_mock.assert_not_called()

    def test_unsynced_source_raises(self, tmp_path: Path, tmp_config_dir: Path) -> None:
        source = tmp_path / "not-synced"
        source.mkdir()
        svc = _service(tmp_config_dir, MagicMock())
        with pytest.raises(ConfigError, match="not a synced project"):
            svc.clone_project(source=source, target_alias="target", target_dir=tmp_path / "clone")


# ---------------------------------------------------------------------------
# CLI: `kbagent sync clone`
# ---------------------------------------------------------------------------


class TestSyncCloneCLI:
    def test_forwards_args_and_overrides(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from keboola_agent_cli.cli import app

        runner = CliRunner()
        source = tmp_path / "golden"
        _golden_source(source)
        bmap = tmp_path / "buckets.json"
        bmap.write_text('{"in.c-ref": "in.c-prod"}')

        from unittest.mock import patch

        with patch("keboola_agent_cli.cli.SyncService") as MockSync:
            svc = MagicMock()
            svc.clone_project.return_value = {
                "status": "cloned",
                "target_alias": "target",
                "target_dir": str(tmp_path / "clone"),
                "created": 2,
                "bucket_rewrites": 1,
                "variable_overrides": 0,
                "renamed_instances": 0,
                "flow_task_remaps": 1,
                "errors": [],
            }
            MockSync.return_value = svc
            result = runner.invoke(
                app,
                [
                    "sync",
                    "clone",
                    "--source",
                    str(source),
                    "--target",
                    "target",
                    "--target-dir",
                    str(tmp_path / "clone"),
                    "--bucket-map",
                    str(bmap),
                ],
            )

        assert result.exit_code == 0, result.output
        svc.clone_project.assert_called_once()
        call = svc.clone_project.call_args.kwargs
        assert call["target_alias"] == "target"
        assert call["overrides"]["bucket_map"] == {"in.c-ref": "in.c-prod"}
        assert "Cloned into target" in result.output

    def test_missing_override_file_errors(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from typer.testing import CliRunner

        from keboola_agent_cli.cli import app

        runner = CliRunner()
        source = tmp_path / "golden"
        _golden_source(source)

        with patch("keboola_agent_cli.cli.SyncService") as MockSync:
            MockSync.return_value = MagicMock()
            result = runner.invoke(
                app,
                [
                    "sync",
                    "clone",
                    "--source",
                    str(source),
                    "--target",
                    "target",
                    "--target-dir",
                    str(tmp_path / "clone"),
                    "--bucket-map",
                    str(tmp_path / "does-not-exist.json"),
                ],
            )
        assert result.exit_code == 5
