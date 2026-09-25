"""Unit tests for the pure clone helpers (sync/clone.py, issue #426).

These exercise the on-disk + in-memory-manifest mechanics of the three
declarative overrides (bucket_map, variable_values, instance_rename) plus the
tree copy and manifest re-point -- no API client involved.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services._sync_bindings import resolve_flow_task_bindings
from keboola_agent_cli.services._sync_storage import _parse_bucket_id, create_buckets_from_export
from keboola_agent_cli.services.sync_service import CreatedConfig, SyncService
from keboola_agent_cli.sync.clone import (
    _config_dir,
    _default_branch_dir,
    apply_bucket_map,
    apply_instance_rename,
    apply_variable_values,
    branch_path_map,
    copy_reference_tree,
    repoint_default_branch_configs,
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
        # without a target default branch id the branch id is left as-is
        assert [b.id for b in m.branches] == [0]

    def test_repoint_manifest_project_remaps_default_branch(self) -> None:
        # CLI-5: given the target's default branch id, the production branch
        # (the fallback branch _resolve_branch_id uses) is re-pointed too, not
        # left on the source project's id.
        m = _manifest([])  # branches = [ManifestBranch(id=0, path="main")]
        repoint_manifest_project(
            m, project_id=999, api_host="other.keboola.com", default_branch_id=52099
        )
        assert m.project.id == 999
        assert [b.id for b in m.branches] == [52099]

    def test_repoint_default_branch_configs_stamps_only_source_default(self) -> None:
        # CLI-9: configs on the source's default branch move onto the target's
        # push branch; a dev-branch config keeps its id (and its own tree).
        m = _manifest(
            [
                ManifestConfiguration(branchId=100, componentId="c", id="prod", path="p"),
                ManifestConfiguration(branchId=200, componentId="c", id="dev", path="d"),
            ]
        )
        repoint_default_branch_configs(m, source_default_branch_id=100, new_branch_id=555)
        assert {c.id: c.branch_id for c in m.configurations} == {"prod": 555, "dev": 200}

    def test_repoint_default_branch_configs_production_zero(self) -> None:
        # A git-branching production clone resolves the push branch to None,
        # normalized to 0 so the writeback `branch_id or 0` comparison matches.
        m = _manifest([ManifestConfiguration(branchId=100, componentId="c", id="ext", path="p")])
        repoint_default_branch_configs(m, source_default_branch_id=100, new_branch_id=0)
        assert [c.branch_id for c in m.configurations] == [0]

    def test_config_dir_falls_back_to_default_branch_path(self) -> None:
        # An id not registered in the manifest resolves to branches[0].path
        # (like branch_scope.branch_tree_path), never a hardcoded "main" -- so a
        # git-branching / --branch clone with a "master" dir still resolves.
        m = Manifest(
            project=ManifestProject(id=1, apiHost="h"),
            naming=ManifestNaming(),
            branches=[ManifestBranch(id=100, path="master")],
            configurations=[],
        )
        default_dir = _default_branch_dir(m)
        branch_map = branch_path_map(m)
        assert default_dir == "master"
        # 0 (production) and 777 (--branch) are not in branch_map -> "master".
        assert _config_dir(Path("/t"), branch_map, 0, "p", default_dir) == Path("/t/master/p")
        assert _config_dir(Path("/t"), branch_map, 777, "p", default_dir) == Path("/t/master/p")


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
    def test_clone_propagates_config_folder_to_target(
        self, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        # CLI-9 end-to-end: a prior pull captured a config folder into the
        # source manifest; clone must recreate it in the target. This drives
        # the REAL push (not mocked), which the other clone tests skip -- so it
        # exercises the branch-id mismatch that used to drop the folder.
        from keboola_agent_cli.sync.manifest import save_manifest

        source = tmp_path / "golden"
        _write_config(
            source,
            "extractor/keboola.ex-db/source",
            {
                "version": 3,
                "name": "Source",
                "description": "",
                "parameters": {},
                "_keboola": {"component_id": "keboola.ex-db", "config_id": "ext-golden"},
            },
        )
        (source / ".keboola").mkdir(parents=True, exist_ok=True)
        save_manifest(
            source,
            Manifest(
                project=ManifestProject(id=1, apiHost="source.keboola.com"),
                naming=ManifestNaming(),
                # SOURCE production branch id, different from the target default.
                branches=[ManifestBranch(id=12345, path="main")],
                configurations=[
                    ManifestConfiguration(
                        branchId=12345,
                        componentId="keboola.ex-db",
                        id="ext-golden",
                        path="extractor/keboola.ex-db/source",
                        metadata={
                            "pull_hash": "fh",
                            "pull_config_hash": "ch",
                            "KBC.configuration.folderName": "Extractors",
                        },
                    )
                ],
            ),
        )

        client = MagicMock()
        # TARGET default branch, resolved like `sync init` (CLI-5).
        client.list_dev_branches.return_value = [{"id": 555, "isDefault": True}]
        client.list_components_with_configs.return_value = []  # fresh target
        client.create_config.return_value = {"id": "NEW-ULID-1"}
        svc = _service(tmp_config_dir, client)

        result = svc.clone_project(
            source=source, target_alias="target", target_dir=tmp_path / "clone"
        )

        assert result["status"] == "cloned"
        assert result["created"] == 1
        # The folder reached the target via the create-path metadata POST.
        client.set_config_metadata.assert_called_once()
        call = client.set_config_metadata.call_args
        assert call.kwargs["config_id"] == "NEW-ULID-1"
        assert dict(call.kwargs["entries"]) == {"KBC.configuration.folderName": "Extractors"}

    def test_clone_propagates_folder_git_branching(
        self, tmp_path: Path, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # CLI-9 / zajca B-2: a git-branching production clone must STILL recreate
        # the folder. Push resolves the production branch to None, so the config
        # branch id is normalized to 0 for the writeback to match. Drives the
        # REAL push.
        from keboola_agent_cli.sync import git_utils
        from keboola_agent_cli.sync.manifest import (
            ManifestGitBranching,
            load_manifest,
            save_manifest,
        )

        source = tmp_path / "golden"
        cfg_dir = source / "master" / "extractor/keboola.ex-db/source"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "_config.yml").write_text(
            yaml.dump(
                {
                    "version": 3,
                    "name": "Source",
                    "parameters": {},
                    "_keboola": {"component_id": "keboola.ex-db", "config_id": "ext-golden"},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (source / ".keboola").mkdir(parents=True, exist_ok=True)
        save_manifest(
            source,
            Manifest(
                project=ManifestProject(id=1, apiHost="source.keboola.com"),
                naming=ManifestNaming(),
                gitBranching=ManifestGitBranching(enabled=True, defaultBranch="master"),
                branches=[ManifestBranch(id=100, path="master")],
                configurations=[
                    ManifestConfiguration(
                        branchId=100,
                        componentId="keboola.ex-db",
                        id="ext-golden",
                        path="extractor/keboola.ex-db/source",
                        metadata={
                            "pull_hash": "fh",
                            "pull_config_hash": "ch",
                            "KBC.configuration.folderName": "Extractors",
                        },
                    )
                ],
            ),
        )
        # Production on the default git branch -> _resolve_branch_id returns None.
        monkeypatch.setattr(git_utils, "get_current_branch", lambda _root: "master")

        client = MagicMock()
        client.list_dev_branches.return_value = [{"id": 555, "isDefault": True}]
        client.list_components_with_configs.return_value = []
        client.create_config.return_value = {"id": "NEW-ULID-1"}
        svc = _service(tmp_config_dir, client)

        result = svc.clone_project(
            source=source, target_alias="target", target_dir=tmp_path / "clone"
        )

        assert result["status"] == "cloned"
        assert result["created"] == 1
        client.set_config_metadata.assert_called_once()
        assert dict(client.set_config_metadata.call_args.kwargs["entries"]) == {
            "KBC.configuration.folderName": "Extractors"
        }
        # The created entry sits on production (0) -- what push resolves.
        post = load_manifest(tmp_path / "clone")
        assert [c.branch_id for c in post.configurations if c.id == "NEW-ULID-1"] == [0]

    def test_clone_branch_override_resolves_non_main_dir(
        self, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        # CLI-9 / zajca B-1: --branch + overrides on a non-'main' branch dir. The
        # config is stamped with the override branch id (not in branches), so
        # _config_dir must fall back to branches[0].path ("master") or the bucket
        # rewrite silently no-ops. Drives the REAL push.
        from keboola_agent_cli.sync.manifest import save_manifest

        source = tmp_path / "golden"
        cfg_dir = source / "master" / "extractor/keboola.ex-db/source"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "_config.yml").write_text(
            yaml.dump(
                {
                    "version": 3,
                    "name": "Source",
                    "input": {"tables": [{"source": "in.c-ref.users", "destination": "users"}]},
                    "_keboola": {"component_id": "keboola.ex-db", "config_id": "ext-golden"},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (source / ".keboola").mkdir(parents=True, exist_ok=True)
        save_manifest(
            source,
            Manifest(
                project=ManifestProject(id=1, apiHost="source.keboola.com"),
                naming=ManifestNaming(),
                branches=[ManifestBranch(id=100, path="master")],
                configurations=[
                    ManifestConfiguration(
                        branchId=100,
                        componentId="keboola.ex-db",
                        id="ext-golden",
                        path="extractor/keboola.ex-db/source",
                        metadata={
                            "pull_hash": "fh",
                            "pull_config_hash": "ch",
                            "KBC.configuration.folderName": "Extractors",
                        },
                    )
                ],
            ),
        )

        client = MagicMock()
        client.list_components_with_configs.return_value = []
        client.create_config.return_value = {"id": "NEW-ULID-1"}
        svc = _service(tmp_config_dir, client)

        result = svc.clone_project(
            source=source,
            target_alias="target",
            target_dir=tmp_path / "clone",
            branch_override=777,
            overrides={"bucket_map": {"in.c-ref": "in.c-prod"}},
        )

        assert result["status"] == "cloned"
        assert result["created"] == 1
        # The override resolved the 'master' dir, not a hardcoded 'main'.
        assert result["bucket_rewrites"] == 1
        copied = yaml.safe_load(
            (
                tmp_path / "clone" / "master" / "extractor/keboola.ex-db/source" / "_config.yml"
            ).read_text()
        )
        assert copied["input"]["tables"][0]["source"] == "in.c-prod.users"
        client.set_config_metadata.assert_called_once()
        # --branch skips the target-default fetch (CLI-5 / #744).
        client.list_dev_branches.assert_not_called()

    def test_clone_folder_no_duplicate_manifest_entry(
        self, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        # Regression guard (CLI-9 / zajca). The bug's whole failure mode was a
        # branch-id mismatch: the create-path writeback did NOT match its
        # placeholder, so a SECOND manifest entry with no KBC.* metadata was
        # appended and the folder was never POSTed. The old tests missed it
        # because their fixtures used branch id 0. Pin the symptom directly:
        # after a clone with a realistic source branch id (!= target default),
        # each config keeps exactly ONE entry, that entry carries the folder,
        # and its branch id equals what push resolves (so the writeback matches).
        from keboola_agent_cli.sync.manifest import load_manifest, save_manifest

        source = tmp_path / "golden"
        _write_config(
            source,
            "extractor/keboola.ex-db/source",
            {
                "version": 3,
                "name": "Source",
                "parameters": {},
                "_keboola": {"component_id": "keboola.ex-db", "config_id": "ext-golden"},
            },
        )
        (source / ".keboola").mkdir(parents=True, exist_ok=True)
        save_manifest(
            source,
            Manifest(
                project=ManifestProject(id=1, apiHost="source.keboola.com"),
                naming=ManifestNaming(),
                branches=[ManifestBranch(id=12345, path="main")],  # source prod id
                configurations=[
                    ManifestConfiguration(
                        branchId=12345,
                        componentId="keboola.ex-db",
                        id="ext-golden",
                        path="extractor/keboola.ex-db/source",
                        metadata={
                            "pull_hash": "fh",
                            "pull_config_hash": "ch",
                            "KBC.configuration.folderName": "Extractors",
                        },
                    )
                ],
            ),
        )

        client = MagicMock()
        client.list_dev_branches.return_value = [{"id": 555, "isDefault": True}]  # target default
        client.list_components_with_configs.return_value = []
        client.create_config.return_value = {"id": "NEW-ULID-1"}
        svc = _service(tmp_config_dir, client)

        result = svc.clone_project(
            source=source, target_alias="target", target_dir=tmp_path / "clone"
        )

        assert result["status"] == "cloned"
        # The folder was POSTed (not silently dropped).
        client.set_config_metadata.assert_called_once()
        assert dict(client.set_config_metadata.call_args.kwargs["entries"]) == {
            "KBC.configuration.folderName": "Extractors"
        }
        post = load_manifest(tmp_path / "clone")
        # Exactly ONE entry -- no stale duplicate from a failed writeback match.
        assert len(post.configurations) == 1
        entry = post.configurations[0]
        assert entry.id == "NEW-ULID-1"
        assert entry.metadata.get("KBC.configuration.folderName") == "Extractors"
        # Its branch id is what push resolved (the target default), so the
        # writeback matched in place instead of appending.
        assert entry.branch_id == 555

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

    def test_clone_repoints_branch_to_target_default(
        self, tmp_path: Path, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # CLI-5: a fresh clone re-points the manifest branch onto the target's
        # own default branch (from list_dev_branches), so a later diff/push with
        # no --branch does not resolve the source project's branch id -- which
        # does not exist in the target.
        source = tmp_path / "golden"
        _golden_source(source)  # source manifest branch id = 0
        target_dir = tmp_path / "clone"

        client = MagicMock()
        client.list_dev_branches.return_value = [{"id": 52099, "isDefault": True}]
        svc = _service(tmp_config_dir, client)
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
            svc,
            "push",
            MagicMock(
                return_value={"status": "pushed", "created": 1, "flow_task_remaps": 0, "errors": []}
            ),
        )

        svc.clone_project(source=source, target_alias="target", target_dir=target_dir)

        from keboola_agent_cli.sync.manifest import load_manifest

        assert [b.id for b in load_manifest(target_dir).branches] == [52099]

    def test_clone_with_explicit_branch_skips_default_branch_fetch(
        self, tmp_path: Path, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #744: an explicit --branch wins in _resolve_branch_id, so clone must
        # not spend a list_dev_branches call resolving the target's default.
        source = tmp_path / "golden"
        _golden_source(source)
        client = MagicMock()
        svc = _service(tmp_config_dir, client)
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
            svc,
            "push",
            MagicMock(
                return_value={"status": "pushed", "created": 1, "flow_task_remaps": 0, "errors": []}
            ),
        )

        svc.clone_project(
            source=source,
            target_alias="target",
            target_dir=tmp_path / "clone",
            branch_override=52099,
        )

        client.list_dev_branches.assert_not_called()

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
                "buckets_created": 1,
                "buckets_skipped": 0,
                "linked_buckets": [
                    {
                        "bucket_id": "in.c-shared",
                        "source_bucket_id": "out.c-origin",
                        "source_project_id": 42,
                    }
                ],
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
        assert call["overrides"]["create_buckets"] is True
        assert "Cloned into target" in result.output
        assert "Buckets: 1 created, 1 linked, 0 already present" in result.output
        assert "Linked in.c-shared -> project 42 bucket out.c-origin" in result.output

    def test_no_create_buckets_forwards_false(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from typer.testing import CliRunner

        from keboola_agent_cli.cli import app

        runner = CliRunner()
        source = tmp_path / "golden"
        _golden_source(source)

        with patch("keboola_agent_cli.cli.SyncService") as MockSync:
            svc = MagicMock()
            svc.clone_project.return_value = {"status": "cloned", "errors": []}
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
                    "--no-create-buckets",
                ],
            )

        assert result.exit_code == 0, result.output
        assert svc.clone_project.call_args.kwargs["overrides"]["create_buckets"] is False

    def test_nested_override_value_errors(self, tmp_path: Path) -> None:
        """A non-scalar override value (fat-fingered colon) is rejected, not stringified."""
        from unittest.mock import patch

        from typer.testing import CliRunner

        from keboola_agent_cli.cli import app

        runner = CliRunner()
        source = tmp_path / "golden"
        _golden_source(source)
        bmap = tmp_path / "buckets.yaml"
        bmap.write_text("in.c-ref:\n  new: in.c-prod\n", encoding="utf-8")

        with patch("keboola_agent_cli.cli.SyncService") as MockSync:
            svc = MagicMock()
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
        assert result.exit_code == 5
        assert "in.c-ref" in result.output
        assert "mapping" in result.output
        svc.clone_project.assert_not_called()

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


def _write_buckets_export(root: Path, buckets: list[dict[str, Any]]) -> None:
    """Write a current-format export: every record carries ``source_bucket`` (None if unset)."""
    storage_dir = root / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    records = [{"source_bucket": None, **b} for b in buckets]
    (storage_dir / "buckets.json").write_text(json.dumps(records), encoding="utf-8")


class TestParseBucketId:
    def test_in_and_out_buckets_parse(self) -> None:
        assert _parse_bucket_id("in.c-foo") == ("in", "foo")
        assert _parse_bucket_id("out.c-bar") == ("out", "bar")

    def test_non_creatable_ids_return_none(self) -> None:
        assert _parse_bucket_id("sys.foo") is None
        assert _parse_bucket_id("in") is None
        assert _parse_bucket_id("in.c-") is None


class TestCreateBucketsFromExport:
    def test_creates_missing_and_skips_existing(self, tmp_path: Path) -> None:
        _write_buckets_export(
            tmp_path,
            [
                {
                    "id": "in.c-new",
                    "name": "new",
                    "stage": "in",
                    "description": "d",
                    "backend": "snowflake",
                },
                {"id": "in.c-existing", "name": "existing", "stage": "in", "description": ""},
            ],
        )
        client = MagicMock()
        client.list_buckets.return_value = [{"id": "in.c-existing"}]

        result = create_buckets_from_export(client, tmp_path, {})

        assert result.created == ["in.c-new"]
        assert result.skipped == ["in.c-existing"]
        assert result.errors == []
        client.create_bucket.assert_called_once_with(
            stage="in", name="new", description="d", backend="snowflake"
        )

    def test_bucket_map_is_applied_to_created_id(self, tmp_path: Path) -> None:
        _write_buckets_export(
            tmp_path, [{"id": "in.c-foo", "name": "foo", "stage": "in", "description": ""}]
        )
        client = MagicMock()
        client.list_buckets.return_value = []

        result = create_buckets_from_export(client, tmp_path, {"in.c-foo": "out.c-bar"})

        assert result.created == ["out.c-bar"]
        client.create_bucket.assert_called_once_with(
            stage="out", name="bar", description="", backend=None
        )

    def test_per_bucket_error_is_collected_not_raised(self, tmp_path: Path) -> None:
        _write_buckets_export(
            tmp_path,
            [
                {"id": "in.c-ok", "name": "ok", "stage": "in", "description": ""},
                {"id": "in.c-bad", "name": "bad", "stage": "in", "description": ""},
            ],
        )
        client = MagicMock()
        client.list_buckets.return_value = []

        def _create(
            *, stage: str, name: str, description: str = "", backend: str | None = None
        ) -> dict[str, Any]:
            if name == "bad":
                raise KeboolaApiError(
                    message="boom", status_code=400, error_code=ErrorCode.API_ERROR
                )
            return {"id": f"{stage}.c-{name}"}

        client.create_bucket.side_effect = _create

        result = create_buckets_from_export(client, tmp_path, {})

        assert result.created == ["in.c-ok"]
        assert result.errors == [{"bucket_id": "in.c-bad", "error": "boom"}]

    def test_linked_bucket_is_linked_to_its_source(self, tmp_path: Path) -> None:
        _write_buckets_export(
            tmp_path,
            [
                {
                    "id": "in.c-shared",
                    "name": "shared",
                    "stage": "in",
                    "description": "",
                    "source_bucket": {"bucket_id": "out.c-origin", "project_id": 42},
                }
            ],
        )
        client = MagicMock()
        client.list_buckets.return_value = []

        result = create_buckets_from_export(client, tmp_path, {})

        client.create_bucket.assert_not_called()
        client.link_bucket.assert_called_once_with(
            source_project_id=42, source_bucket_id="out.c-origin", name="shared", stage="in"
        )
        assert result.created == []
        assert result.linked == [
            {
                "bucket_id": "in.c-shared",
                "source_bucket_id": "out.c-origin",
                "source_project_id": 42,
            }
        ]

    def test_refused_link_is_collected_not_raised(self, tmp_path: Path) -> None:
        _write_buckets_export(
            tmp_path,
            [
                {
                    "id": "in.c-shared",
                    "name": "shared",
                    "stage": "in",
                    "description": "",
                    "source_bucket": {"bucket_id": "out.c-origin", "project_id": 42},
                }
            ],
        )
        client = MagicMock()
        client.list_buckets.return_value = []
        client.link_bucket.side_effect = KeboolaApiError(
            message="not shared", status_code=500, error_code=ErrorCode.STORAGE_JOB_FAILED
        )

        result = create_buckets_from_export(client, tmp_path, {})

        client.create_bucket.assert_not_called()
        assert result.linked == []
        assert result.errors == [{"bucket_id": "in.c-shared", "error": "not shared"}]

    def test_list_failure_is_collected_not_raised(self, tmp_path: Path) -> None:
        _write_buckets_export(
            tmp_path, [{"id": "in.c-foo", "name": "foo", "stage": "in", "description": ""}]
        )
        client = MagicMock()
        client.list_buckets.side_effect = KeboolaApiError(
            message="denied", status_code=403, error_code=ErrorCode.API_ERROR
        )

        result = create_buckets_from_export(client, tmp_path, {})

        client.create_bucket.assert_not_called()
        assert result.errors == [{"bucket_id": "", "error": "Cannot list buckets: denied"}]

    def test_legacy_export_creates_nothing(self, tmp_path: Path) -> None:
        # An older pull wrote no source_bucket key: a linked bucket would be
        # created empty and every later re-clone would skip it.
        storage_dir = tmp_path / "storage"
        storage_dir.mkdir()
        (storage_dir / "buckets.json").write_text(
            json.dumps([{"id": "in.c-foo", "name": "foo", "stage": "in", "description": ""}]),
            encoding="utf-8",
        )
        client = MagicMock()

        result = create_buckets_from_export(client, tmp_path, {})

        client.list_buckets.assert_not_called()
        client.create_bucket.assert_not_called()
        assert result.created == []
        assert len(result.errors) == 1
        assert "older kbagent pull" in result.errors[0]["error"]

    def test_no_export_returns_empty_without_listing(self, tmp_path: Path) -> None:
        client = MagicMock()

        result = create_buckets_from_export(client, tmp_path, {})

        assert (result.created, result.skipped, result.errors) == ([], [], [])
        client.list_buckets.assert_not_called()


def _golden_source_with_buckets(root: Path) -> None:
    """A golden reference tree plus a one-bucket ``storage/buckets.json`` export."""
    _golden_source(root)
    storage_dir = root / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / "buckets.json").write_text(
        json.dumps(
            [
                {
                    "id": "in.c-ref",
                    "name": "ref",
                    "stage": "in",
                    "description": "",
                    "source_bucket": None,
                }
            ]
        ),
        encoding="utf-8",
    )


class TestCloneCreatesBuckets:
    """clone_project wires the storage-bucket create step (diff/push mocked)."""

    def _svc(
        self, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[SyncService, MagicMock]:
        client = MagicMock()
        client.list_dev_branches.return_value = [{"id": 555, "isDefault": True}]
        client.list_buckets.return_value = []
        svc = _service(tmp_config_dir, client)
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
        return svc, client

    def test_clone_creates_missing_buckets(
        self, tmp_path: Path, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = tmp_path / "golden"
        _golden_source_with_buckets(source)
        svc, client = self._svc(tmp_config_dir, monkeypatch)

        result = svc.clone_project(
            source=source,
            target_alias="target",
            target_dir=tmp_path / "clone",
            overrides={"create_buckets": True},
        )

        client.create_bucket.assert_called_once_with(
            stage="in", name="ref", description="", backend=None
        )
        assert result["buckets_created"] == 1
        assert result["bucket_errors"] == []

    def test_clone_creates_buckets_at_production_level_with_branch(
        self, tmp_path: Path, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Buckets are project-level: an explicit --branch scopes the push only.
        source = tmp_path / "golden"
        _golden_source_with_buckets(source)
        svc, client = self._svc(tmp_config_dir, monkeypatch)

        svc.clone_project(
            source=source,
            target_alias="target",
            target_dir=tmp_path / "clone",
            overrides={"create_buckets": True},
            branch_override=52099,
        )

        client.create_bucket.assert_called_once()
        assert "branch_id" not in client.create_bucket.call_args.kwargs
        client.list_buckets.assert_called_once_with()

    def test_clone_opt_out_skips_bucket_creation(
        self, tmp_path: Path, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = tmp_path / "golden"
        _golden_source_with_buckets(source)
        svc, client = self._svc(tmp_config_dir, monkeypatch)

        result = svc.clone_project(
            source=source,
            target_alias="target",
            target_dir=tmp_path / "clone",
            overrides={"create_buckets": False},
        )

        client.create_bucket.assert_not_called()
        assert result["buckets_created"] == 0

    def test_service_default_creates_buckets(
        self, tmp_path: Path, tmp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The service default matches the CLI default, so a caller that passes
        # no create_buckets key still gets a complete clone.
        source = tmp_path / "golden"
        _golden_source_with_buckets(source)
        svc, client = self._svc(tmp_config_dir, monkeypatch)

        result = svc.clone_project(
            source=source, target_alias="target", target_dir=tmp_path / "clone"
        )

        client.create_bucket.assert_called_once()
        assert result["buckets_created"] == 1
