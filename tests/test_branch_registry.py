"""Unit tests for sync/branch_registry.py (issue #644).

Covers the placement policy that decides where a ``config new --push``
scaffold lands: default-branch prefix for production creates, branch
subtree (with on-demand registration) for dev-branch creates, and the
``branch-{id}/`` degrade path that must NEVER retarget files to the
default tree.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.errors import ConfigError
from keboola_agent_cli.sync.branch_registry import (
    ScaffoldPlacement,
    default_branch_prefix,
    ensure_branch_registered,
    fallback_branch_dir,
    register_branch_dir,
    resolve_scaffold_placement,
)
from keboola_agent_cli.sync.manifest import load_manifest


def _write_manifest(root: Path, branches: list[dict], project_id: int = 1234) -> None:
    keboola = root / ".keboola"
    keboola.mkdir(parents=True, exist_ok=True)
    manifest = {
        "version": 2,
        "project": {"id": project_id, "apiHost": "connection.keboola.com"},
        "naming": {},
        "branches": branches,
        "configurations": [],
    }
    (keboola / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _project(project_id: int | None = 1234) -> MagicMock:
    project = MagicMock()
    project.stack_url = "https://connection.keboola.com"
    project.token = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
    project.project_id = project_id
    return project


def _client_factory_returning(branches: list[dict]) -> MagicMock:
    client = MagicMock()
    client.list_dev_branches.return_value = branches
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    factory = MagicMock(return_value=client)
    return factory


class TestFallbackBranchDir:
    def test_canonical_spelling(self) -> None:
        assert fallback_branch_dir(51406) == "branch-51406"


class TestDefaultBranchPrefix:
    def test_no_manifest_returns_none(self, tmp_path: Path) -> None:
        assert default_branch_prefix(tmp_path) is None

    def test_returns_first_branch_path(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, [{"id": 10, "path": "main"}, {"id": 20, "path": "dev"}])
        assert default_branch_prefix(tmp_path) == "main"

    def test_unreadable_manifest_returns_none(self, tmp_path: Path) -> None:
        keboola = tmp_path / ".keboola"
        keboola.mkdir()
        (keboola / "manifest.json").write_text("{not json", encoding="utf-8")
        assert default_branch_prefix(tmp_path) is None


class TestRegisterBranchDir:
    def test_already_registered_returns_path_without_client(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, [{"id": 10, "path": "main"}, {"id": 20, "path": "dev-x"}])
        factory = MagicMock()
        assert register_branch_dir(_project(), tmp_path, 20, factory) == "dev-x"
        factory.assert_not_called()

    def test_registers_unknown_branch_and_persists(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, [{"id": 10, "path": "main"}])
        factory = _client_factory_returning([{"id": 20, "name": "Feature X"}])
        path = register_branch_dir(_project(), tmp_path, 20, factory)
        assert path == "feature-x"
        manifest = load_manifest(tmp_path)
        assert [(b.id, b.path) for b in manifest.branches] == [(10, "main"), (20, "feature-x")]

    def test_project_mismatch_raises(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, [{"id": 10, "path": "main"}], project_id=9999)
        with pytest.raises(ConfigError, match="belongs to project 9999"):
            register_branch_dir(_project(1234), tmp_path, 20, MagicMock())


class TestEnsureBranchRegistered:
    def test_unknown_name_falls_back_to_branch_id_dir(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, [{"id": 10, "path": "main"}])
        manifest = load_manifest(tmp_path)
        client = MagicMock()
        client.list_dev_branches.return_value = []  # API knows nothing
        assert ensure_branch_registered(manifest, 20, client) == "branch-20"


class TestResolveScaffoldPlacement:
    def test_production_create_uses_default_prefix(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, [{"id": 10, "path": "main"}])
        placement = resolve_scaffold_placement(None, tmp_path, None, MagicMock())
        assert placement == ScaffoldPlacement("main")

    def test_production_create_flat_without_manifest(self, tmp_path: Path) -> None:
        placement = resolve_scaffold_placement(None, tmp_path, None, MagicMock())
        assert placement == ScaffoldPlacement(None)

    def test_branch_create_without_manifest_is_flat(self, tmp_path: Path) -> None:
        placement = resolve_scaffold_placement(_project(), tmp_path, 20, MagicMock())
        assert placement == ScaffoldPlacement(None)

    def test_branch_create_registers_and_places(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, [{"id": 10, "path": "main"}])
        factory = _client_factory_returning([{"id": 20, "name": "Feature X"}])
        placement = resolve_scaffold_placement(_project(), tmp_path, 20, factory)
        assert placement == ScaffoldPlacement("feature-x")

    def test_registration_failure_degrades_to_branch_dir_with_warning(self, tmp_path: Path) -> None:
        """NEVER the default tree -- the duplicate factory issue #644 removes."""
        _write_manifest(tmp_path, [{"id": 10, "path": "main"}])
        factory = MagicMock(side_effect=RuntimeError("api down"))
        placement = resolve_scaffold_placement(_project(), tmp_path, 20, factory)
        assert placement.branch_prefix == "branch-20"
        assert placement.warning is not None
        assert "sync pull --branch 20" in placement.warning

    def test_project_mismatch_degrades_with_named_mismatch(self, tmp_path: Path) -> None:
        """A workspace of a different project: files still land (inert, under
        branch-{id}/) and the warning names the mismatch -- the remote config
        already exists, so losing the files entirely would be worse."""
        _write_manifest(tmp_path, [{"id": 10, "path": "main"}], project_id=9999)
        placement = resolve_scaffold_placement(_project(1234), tmp_path, 20, MagicMock())
        assert placement.branch_prefix == "branch-20"
        assert placement.warning is not None
        assert "belongs to project 9999" in placement.warning


class TestProductionMismatchGuard:
    def test_production_create_into_foreign_workspace_goes_flat_with_warning(
        self, tmp_path: Path
    ) -> None:
        """A production create pointed at ANOTHER project's workspace must not
        write into that workspace's main/ tree -- its next sync push would
        create the config in the WRONG project (PR #653 review sweep). Flat
        files sit outside every branch tree, so they are inert there."""
        _write_manifest(tmp_path, [{"id": 10, "path": "main"}], project_id=9999)
        placement = resolve_scaffold_placement(_project(1234), tmp_path, None, MagicMock())
        assert placement.branch_prefix is None
        assert placement.warning is not None
        assert "belongs to project 9999" in placement.warning

    def test_production_create_matching_project_uses_default_prefix(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, [{"id": 10, "path": "main"}], project_id=1234)
        placement = resolve_scaffold_placement(_project(1234), tmp_path, None, MagicMock())
        assert placement == ScaffoldPlacement("main")
