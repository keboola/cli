"""Tests for plaintext #-secret detection in synced configs (issue #378).

Covers find_plaintext_secret_keys (helper), scan_synced_plaintext_secrets
(sync_service, the in-sync vs pending distinction), the sync status warning, and
the doctor sync_secrets check.
"""

import hashlib
from pathlib import Path

import pytest
import yaml

from helpers import setup_single_project
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.services._encryption import find_plaintext_secret_keys
from keboola_agent_cli.services.doctor_service import DoctorService
from keboola_agent_cli.services.sync_service import SyncService, scan_synced_plaintext_secrets
from keboola_agent_cli.sync.manifest import (
    Manifest,
    ManifestBranch,
    ManifestConfigRow,
    ManifestConfiguration,
    ManifestNaming,
    ManifestProject,
    save_manifest,
)

COMPONENT_ID = "keboola.ex-db-mysql"
CONFIG_REL = "extractor/keboola.ex-db-mysql/my-config"


def _write_yaml(path: Path, body: dict) -> str:
    """Write a _config.yml and return its sha256 hex digest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_tree(
    project_root: Path,
    *,
    parameters: dict,
    in_sync: bool,
    row_parameters: dict | None = None,
    row_in_sync: bool = True,
) -> None:
    """Build a minimal sync working tree with one config (and optional row)."""
    config_dir = project_root / "main" / CONFIG_REL
    config_file = config_dir / "_config.yml"
    cfg_hash = _write_yaml(config_file, {"name": "my-config", "parameters": parameters})
    # in_sync => manifest pull_hash matches the file; else a stale hash (= pending edit)
    cfg_pull_hash = cfg_hash if in_sync else "0" * 64

    rows: list[ManifestConfigRow] = []
    if row_parameters is not None:
        row_file = config_dir / "rows/row-a" / "_config.yml"
        row_hash = _write_yaml(row_file, {"name": "row-a", "parameters": row_parameters})
        rows.append(
            ManifestConfigRow(
                id="row-1",
                path="rows/row-a",
                metadata={"pull_hash": row_hash if row_in_sync else "0" * 64},
            )
        )

    manifest = Manifest(
        project=ManifestProject(id=258, apiHost="connection.keboola.com"),
        naming=ManifestNaming(),
        branches=[ManifestBranch(id=0, path="main")],
        configurations=[
            ManifestConfiguration(
                branchId=0,
                componentId=COMPONENT_ID,
                id="config-1",
                path=CONFIG_REL,
                metadata={"pull_hash": cfg_pull_hash},
                rows=rows,
            )
        ],
    )
    save_manifest(project_root, manifest)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


class TestFindPlaintextSecretKeys:
    def test_returns_only_plaintext_hash_keys(self) -> None:
        cfg = {
            "parameters": {
                "#password": "plain",
                "#token": "KBC::ProjectSecure::abc",  # already encrypted -> skip
                "user": "admin",  # not a secret -> skip
            }
        }
        assert find_plaintext_secret_keys(cfg) == ["#parameters.#password"]

    def test_empty_when_no_plaintext(self) -> None:
        cfg = {"parameters": {"#password": "KBC::ProjectSecure::abc", "user": "x"}}
        assert find_plaintext_secret_keys(cfg) == []


# ---------------------------------------------------------------------------
# scan_synced_plaintext_secrets
# ---------------------------------------------------------------------------


class TestScanSyncedPlaintextSecrets:
    def test_in_sync_plaintext_is_flagged(self, tmp_path: Path) -> None:
        _build_tree(tmp_path, parameters={"#password": "plain", "user": "x"}, in_sync=True)
        warnings = scan_synced_plaintext_secrets(tmp_path)
        assert len(warnings) == 1
        assert warnings[0]["scope"] == "config"
        assert warnings[0]["config_id"] == "config-1"
        assert "#parameters.#password" in warnings[0]["secret_keys"]

    def test_in_sync_encrypted_is_not_flagged(self, tmp_path: Path) -> None:
        _build_tree(
            tmp_path,
            parameters={"#password": "KBC::ProjectSecure::abc"},
            in_sync=True,
        )
        assert scan_synced_plaintext_secrets(tmp_path) == []

    def test_pending_edit_plaintext_is_not_flagged(self, tmp_path: Path) -> None:
        # hash mismatch => local edit not yet pushed; push >=0.54.0 will encrypt.
        _build_tree(tmp_path, parameters={"#password": "plain"}, in_sync=False)
        assert scan_synced_plaintext_secrets(tmp_path) == []

    def test_no_secrets_is_not_flagged(self, tmp_path: Path) -> None:
        _build_tree(tmp_path, parameters={"user": "x", "host": "db"}, in_sync=True)
        assert scan_synced_plaintext_secrets(tmp_path) == []

    def test_row_plaintext_is_flagged(self, tmp_path: Path) -> None:
        _build_tree(
            tmp_path,
            parameters={"user": "x"},
            in_sync=True,
            row_parameters={"#password": "plain"},
            row_in_sync=True,
        )
        warnings = scan_synced_plaintext_secrets(tmp_path)
        assert len(warnings) == 1
        assert warnings[0]["scope"] == "row"
        assert warnings[0]["row_id"] == "row-1"


# ---------------------------------------------------------------------------
# sync status integration
# ---------------------------------------------------------------------------


class TestSyncStatusWarning:
    def test_status_surfaces_plaintext_warnings(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "cfg"
        store = setup_single_project(config_dir)
        svc = SyncService(config_store=store, client_factory=lambda url, token: None)

        project_root = tmp_path / "project"
        _build_tree(project_root, parameters={"#password": "plain"}, in_sync=True)

        result = svc.status(project_root=project_root)
        assert len(result["plaintext_secret_warnings"]) == 1
        assert result["plaintext_secret_warnings"][0]["config_id"] == "config-1"


# ---------------------------------------------------------------------------
# doctor check
# ---------------------------------------------------------------------------


class TestDoctorSyncSecretsCheck:
    def _doctor(self, tmp_path: Path) -> DoctorService:
        return DoctorService(config_store=ConfigStore(config_dir=tmp_path / "cfg"))

    def test_skip_outside_sync_tree(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()
        monkeypatch.chdir(plain)
        check = self._doctor(tmp_path)._check_sync_secrets()
        assert check["status"] == "skip"

    def test_warn_in_sync_tree_with_plaintext(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project_root = tmp_path / "project"
        _build_tree(project_root, parameters={"#password": "plain"}, in_sync=True)
        monkeypatch.chdir(project_root)
        check = self._doctor(tmp_path)._check_sync_secrets()
        assert check["status"] == "warn"
        assert "PLAINTEXT" in check["message"]

    def test_pass_in_sync_tree_when_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project_root = tmp_path / "project"
        _build_tree(
            project_root,
            parameters={"#password": "KBC::ProjectSecure::abc"},
            in_sync=True,
        )
        monkeypatch.chdir(project_root)
        check = self._doctor(tmp_path)._check_sync_secrets()
        assert check["status"] == "pass"
