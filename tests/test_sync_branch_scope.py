"""Tests for sync/branch_scope.py: which entries count as fetched from the target (#792 H)."""

from __future__ import annotations

from pathlib import Path

from keboola_agent_cli.constants import CONFIG_FILENAME
from keboola_agent_cli.sync.branch_scope import scope_manifest
from keboola_agent_cli.sync.manifest import (
    Manifest,
    ManifestBranch,
    ManifestConfigRow,
    ManifestConfiguration,
    ManifestNaming,
    ManifestProject,
)

PROD, DEV = 100, 200


def _manifest(root: Path) -> Manifest:
    entries = [
        ManifestConfiguration(
            branchId=PROD,
            componentId="c",
            id="pulled",
            path="c/pulled",
            metadata={"pull_hash": "h"},
            rows=[
                ManifestConfigRow(id="row-pulled", path="rows/a", metadata={"pull_hash": "h"}),
                ManifestConfigRow(id="row-failed", path="rows/b", metadata={}),
            ],
        ),
        ManifestConfiguration(branchId=PROD, componentId="c", id="placeholder", path="c/new"),
    ]
    for entry in entries:
        config_dir = root / "main" / entry.path
        config_dir.mkdir(parents=True)
        (config_dir / CONFIG_FILENAME).write_text("name: x\n")
    return Manifest(
        project=ManifestProject(id=1, apiHost="connection.keboola.com"),
        naming=ManifestNaming(),
        branches=[ManifestBranch(id=PROD, path="main"), ManifestBranch(id=DEV, path="dev")],
        configurations=entries,
    )


def test_on_target_needs_a_pull_hash(tmp_path: Path) -> None:
    """A placeholder entry and a row whose create failed were never on the target."""
    scope = scope_manifest(_manifest(tmp_path), tmp_path, "main", set(), target_branch_id=PROD)

    assert scope.target_tracked_keys == {"c/pulled"}
    assert scope.target_tracked_row_keys == {"c/pulled/rows/row-pulled"}


def test_promote_push_source_entries_are_not_on_the_target(tmp_path: Path) -> None:
    """``main/`` read for a dev branch with no tree of its own: nothing is on the target yet."""
    scope = scope_manifest(_manifest(tmp_path), tmp_path, "main", set(), target_branch_id=DEV)

    assert {cfg.id for cfg in scope.in_tree} == {"pulled", "placeholder"}
    assert scope.target_tracked_keys == set()
