"""Tests for ``ProjectService.edit_project`` -- the ``--new-alias`` rename
cascade landed in v0.30.3.

Focus: pin the cascade contract end-to-end (config.json key swap +
``default_project`` cascade + nested-sync-dir on-disk rename) plus the
fail-closed guarantees (collision check before any state mutation,
empty / whitespace-mangled aliases rejected). Mirrors the fixture
pattern from ``tests/test_config_rename.py`` so the test suite reads
consistently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import make_mock_client, setup_single_project, setup_two_projects
from keboola_agent_cli.errors import ConfigError
from keboola_agent_cli.services.project_service import ProjectService


def _make_service(tmp_config_dir: Path, alias: str = "prod") -> ProjectService:
    """Wire a ProjectService with a mock client_factory.

    ``edit_project`` only invokes the client when ``--token`` is changing,
    so most of these tests never hit the factory; the rename-only cases
    pass with a no-op mock.
    """
    store = setup_single_project(tmp_config_dir, alias=alias)
    return ProjectService(
        config_store=store,
        client_factory=lambda url, token: make_mock_client(),
    )


# ---------------------------------------------------------------------------
# config.json: dict-key swap + default_project cascade
# ---------------------------------------------------------------------------


class TestRenameAliasConfigCascade:
    """Pin the config.json side of the rename cascade."""

    def test_rename_to_unique_alias_swaps_dict_key(self, tmp_config_dir: Path) -> None:
        service = _make_service(tmp_config_dir, alias="prod")
        result = service.edit_project(
            alias="prod",
            new_alias="production",
            search_root=tmp_config_dir,
        )
        config = service._config_store.load()
        assert "prod" not in config.projects
        assert "production" in config.projects
        assert result["alias"] == "production"
        assert result["old_alias"] == "prod"
        assert result["rename"]["old_alias"] == "prod"
        assert result["rename"]["new_alias"] == "production"

    def test_rename_collision_raises_config_error_no_state_mutation(
        self, tmp_config_dir: Path
    ) -> None:
        store = setup_two_projects(tmp_config_dir)
        service = ProjectService(config_store=store, client_factory=lambda u, t: make_mock_client())
        with pytest.raises(ConfigError, match="already in use"):
            service.edit_project(alias="prod", new_alias="dev", search_root=tmp_config_dir)
        # Both keys preserved; collision detected before any mutation.
        config = service._config_store.load()
        assert set(config.projects.keys()) == {"prod", "dev"}

    def test_rename_updates_default_project_when_match(self, tmp_config_dir: Path) -> None:
        service = _make_service(tmp_config_dir, alias="prod")
        # setup_single_project writes default_project="prod" via add_project.
        config = service._config_store.load()
        assert config.default_project == "prod"

        result = service.edit_project(
            alias="prod", new_alias="production", search_root=tmp_config_dir
        )

        config = service._config_store.load()
        assert config.default_project == "production"
        assert result["rename"]["default_project_updated"] is True

    def test_rename_does_not_touch_default_project_when_unrelated(
        self, tmp_config_dir: Path
    ) -> None:
        store = setup_two_projects(tmp_config_dir)
        # default_project is "prod" (first added). Rename "dev" -> "development".
        service = ProjectService(config_store=store, client_factory=lambda u, t: make_mock_client())
        result = service.edit_project(
            alias="dev", new_alias="development", search_root=tmp_config_dir
        )
        config = service._config_store.load()
        assert config.default_project == "prod"  # unchanged
        assert result["rename"]["default_project_updated"] is False


# ---------------------------------------------------------------------------
# Nested-sync-dir cascade
# ---------------------------------------------------------------------------


class TestRenameAliasSyncDirCascade:
    """Pin the on-disk rename of ``<search_root>/<alias>/``."""

    def test_rename_with_nested_sync_dir_moves_on_disk(
        self, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        service = _make_service(tmp_config_dir, alias="prod")
        # Pre-create a nested sync workspace at <tmp_path>/prod/.keboola/manifest.json
        sync_root = tmp_path / "workspace"
        sync_root.mkdir()
        (sync_root / "prod" / ".keboola").mkdir(parents=True)
        manifest = sync_root / "prod" / ".keboola" / "manifest.json"
        manifest.write_text('{"version": 2}', encoding="utf-8")

        result = service.edit_project(alias="prod", new_alias="production", search_root=sync_root)

        assert not (sync_root / "prod").exists()
        assert (sync_root / "production" / ".keboola" / "manifest.json").exists()
        assert result["rename"]["sync_dir"] is not None
        assert result["rename"]["sync_dir"]["old_path"] == str(sync_root / "prod")
        assert result["rename"]["sync_dir"]["new_path"] == str(sync_root / "production")

    def test_rename_with_no_sync_dir_skips_disk_op(
        self, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        service = _make_service(tmp_config_dir, alias="prod")
        empty_root = tmp_path / "no-workspace"
        empty_root.mkdir()

        result = service.edit_project(alias="prod", new_alias="production", search_root=empty_root)

        # Config still mutated; disk side reports None.
        config = service._config_store.load()
        assert "production" in config.projects
        assert result["rename"]["sync_dir"] is None

    def test_rename_with_collision_appends_suffix(
        self, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        service = _make_service(tmp_config_dir, alias="prod")
        sync_root = tmp_path / "workspace"
        sync_root.mkdir()
        (sync_root / "prod" / ".keboola").mkdir(parents=True)
        (sync_root / "prod" / ".keboola" / "manifest.json").write_text(
            '{"version": 2}', encoding="utf-8"
        )
        # Pre-existing collision dir at the target.
        (sync_root / "production").mkdir()

        result = service.edit_project(alias="prod", new_alias="production", search_root=sync_root)

        # Collision dir untouched; sync dir got the -2 suffix.
        assert (sync_root / "production").exists()
        assert (sync_root / "production-2" / ".keboola" / "manifest.json").exists()
        assert result["rename"]["sync_dir"]["new_path"] == str(sync_root / "production-2")


# ---------------------------------------------------------------------------
# Combined edit-and-rename in one atomic call
# ---------------------------------------------------------------------------


class TestRenameCombinedWithEdits:
    """Combined --new-alias + --url / --token; mutations target NEW alias."""

    def test_rename_combined_with_url_and_token_one_atomic_call(
        self, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        store = setup_single_project(
            tmp_config_dir, alias="prod", stack_url="https://old.example.com"
        )
        captured: dict[str, str] = {}

        def factory(url: str, token: str):
            captured["url"] = url
            captured["token"] = token
            return make_mock_client(project_id=999, project_name="Renamed Project")

        service = ProjectService(config_store=store, client_factory=factory)

        result = service.edit_project(
            alias="prod",
            new_alias="production",
            stack_url="https://new.example.com",
            token="901-NEW",
            search_root=tmp_path,
        )

        # Token verification used the NEW URL (post-rename effective_url logic).
        assert captured["url"] == "https://new.example.com"
        assert captured["token"] == "901-NEW"
        # Config mutation landed on the new alias key.
        config = service._config_store.load()
        assert "production" in config.projects
        assert config.projects["production"].stack_url == "https://new.example.com"
        assert config.projects["production"].project_id == 999
        # Result reflects new state.
        assert result["alias"] == "production"
        assert result["old_alias"] == "prod"
        assert result["stack_url"] == "https://new.example.com"
        assert result["project_id"] == 999

    def test_rename_to_same_alias_is_noop(self, tmp_path: Path, tmp_config_dir: Path) -> None:
        service = _make_service(tmp_config_dir, alias="prod")
        # No url/token/new-alias change -> ValidationError on the empty-changes
        # check would fire, BUT new_alias == alias is treated as no-op AT the
        # rename layer, so the "no changes" check still triggers because url
        # and token are None. Pass --url to make the no-changes check happy
        # while leaving alias untouched.
        result = service.edit_project(
            alias="prod",
            new_alias="prod",
            stack_url="https://still.example.com",
            search_root=tmp_path,
        )
        assert result["alias"] == "prod"
        assert "old_alias" not in result  # no rename happened
        assert "rename" not in result


# ---------------------------------------------------------------------------
# Validation -- empty / whitespace / no-changes
# ---------------------------------------------------------------------------


class TestRenameValidation:
    """Reject malformed --new-alias values BEFORE touching state."""

    def test_no_changes_provided_raises(self, tmp_config_dir: Path) -> None:
        service = _make_service(tmp_config_dir, alias="prod")
        with pytest.raises(ConfigError, match="No changes specified"):
            service.edit_project(alias="prod")

    def test_empty_new_alias_rejected(self, tmp_path: Path, tmp_config_dir: Path) -> None:
        service = _make_service(tmp_config_dir, alias="prod")
        with pytest.raises(ConfigError, match="must not be empty"):
            service.edit_project(alias="prod", new_alias="", search_root=tmp_path)

    def test_whitespace_new_alias_rejected(self, tmp_path: Path, tmp_config_dir: Path) -> None:
        service = _make_service(tmp_config_dir, alias="prod")
        with pytest.raises(ConfigError, match="empty or whitespace-only"):
            service.edit_project(alias="prod", new_alias="   ", search_root=tmp_path)

    def test_alias_with_internal_whitespace_rejected(
        self, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        service = _make_service(tmp_config_dir, alias="prod")
        with pytest.raises(ConfigError, match="must not contain whitespace"):
            service.edit_project(alias="prod", new_alias="has spaces", search_root=tmp_path)

    def test_unknown_old_alias_rejected(self, tmp_config_dir: Path) -> None:
        service = _make_service(tmp_config_dir, alias="prod")
        with pytest.raises(ConfigError, match="not found"):
            service.edit_project(alias="ghost", new_alias="something")

    def test_only_same_alias_no_other_changes_is_rejected(
        self, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        """``--new-alias prod`` on alias ``prod`` with no url/token = no change.

        Iter 2 review #6: the rename branch is skipped (new_alias == alias),
        so the "no changes specified" guard fires and the user gets a clean
        error instead of a silent no-op.
        """
        service = _make_service(tmp_config_dir, alias="prod")
        with pytest.raises(ConfigError, match="No changes specified"):
            service.edit_project(alias="prod", new_alias="prod", search_root=tmp_path)


# ---------------------------------------------------------------------------
# Path-traversal hardening (iter 2 review S1) -- pin the validator surface
# ---------------------------------------------------------------------------


class TestRenameAliasPathTraversalRejected:
    """Reject filesystem-unsafe ``--new-alias`` BEFORE any state mutation."""

    @pytest.mark.parametrize(
        "bad_alias,reason_substring",
        [
            ("..", "path-traversal"),
            ("../etc", "path-traversal"),
            ("foo..bar", "path-traversal"),
            ("foo/bar", "filesystem-safe slug"),
            ("foo\\bar", "filesystem-safe slug"),
            ("a\x00b", "filesystem-safe slug"),
            (".hidden", "filesystem-safe slug"),
            ("-leading-dash", "filesystem-safe slug"),
            ("with space", "must not contain whitespace"),
        ],
    )
    def test_traversal_and_unsafe_chars_rejected(
        self,
        tmp_path: Path,
        tmp_config_dir: Path,
        bad_alias: str,
        reason_substring: str,
    ) -> None:
        service = _make_service(tmp_config_dir, alias="prod")
        with pytest.raises(ConfigError, match=reason_substring):
            service.edit_project(alias="prod", new_alias=bad_alias, search_root=tmp_path)
        # State unchanged after the rejected attempt.
        config = service._config_store.load()
        assert "prod" in config.projects

    def test_legal_filesystem_safe_aliases_accepted(
        self, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        """The validator must accept the realistic alias shapes we ship with."""
        service = _make_service(tmp_config_dir, alias="prod")
        # Accept: alphanum, underscore, dash, dot in non-leading positions.
        # Mirrors the shape of e.g. `99_playground_max`, `prod-eu`, `kbc.demo`.
        for legal_alias in ("99_playground_max", "prod-eu", "kbc.demo", "_internal"):
            service._validate_alias_format(legal_alias)


# ---------------------------------------------------------------------------
# Disk-rename rollback (iter 2 review S2)
# ---------------------------------------------------------------------------


class TestRenameAliasRollback:
    """Failed disk rename must roll the config rename back."""

    def test_oserror_in_disk_rename_restores_config(
        self,
        tmp_path: Path,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        service = _make_service(tmp_config_dir, alias="prod")

        # Force the disk-side rename to raise OSError. Patch
        # _rename_nested_sync_dir on the class because the helper is a
        # @staticmethod called via the class.
        def _explode(*_args, **_kwargs):
            raise OSError("simulated disk failure")

        monkeypatch.setattr(ProjectService, "_rename_nested_sync_dir", _explode)

        with pytest.raises(ConfigError, match="Config rolled back"):
            service.edit_project(alias="prod", new_alias="production", search_root=tmp_path)

        # Config rolled back -- alias dict + default_project still original.
        config = service._config_store.load()
        assert "prod" in config.projects
        assert "production" not in config.projects
        assert config.default_project == "prod"

    def test_rollback_failure_is_swallowed_original_error_wins(
        self,
        tmp_path: Path,
        tmp_config_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the rollback itself fails, surface the ORIGINAL OSError (not a
        confusing rollback-secondary error). The original is what the user
        needs to see to understand what really broke.
        """
        service = _make_service(tmp_config_dir, alias="prod")

        def _explode(*_args, **_kwargs):
            raise OSError("primary failure")

        monkeypatch.setattr(ProjectService, "_rename_nested_sync_dir", _explode)

        # Make the rollback's rename_project also fail. Patch on the
        # instance so only the second call is affected -- the first
        # rename_project (the actual rename) still succeeds.
        original = service._config_store.rename_project
        call_count = {"n": 0}

        def _flaky_rename(old, new):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise ConfigError("rollback also failed")
            return original(old, new)

        monkeypatch.setattr(service._config_store, "rename_project", _flaky_rename)

        with pytest.raises(ConfigError, match="primary failure"):
            service.edit_project(alias="prod", new_alias="production", search_root=tmp_path)


# ---------------------------------------------------------------------------
# Symlink-collision handling (iter 2 review S5)
# ---------------------------------------------------------------------------


class TestRenameAliasSymlinkSafety:
    """A pre-existing symlink at the target dir forces a -2 suffix bump."""

    def test_symlink_at_target_triggers_suffix(self, tmp_path: Path, tmp_config_dir: Path) -> None:
        service = _make_service(tmp_config_dir, alias="prod")
        sync_root = tmp_path / "workspace"
        sync_root.mkdir()
        (sync_root / "prod" / ".keboola").mkdir(parents=True)
        (sync_root / "prod" / ".keboola" / "manifest.json").write_text(
            '{"version": 2}', encoding="utf-8"
        )
        # Pre-existing symlink at the rename target -- must NOT be moved into.
        outside = tmp_path / "outside-target"
        outside.mkdir()
        (sync_root / "production").symlink_to(outside)

        result = service.edit_project(alias="prod", new_alias="production", search_root=sync_root)

        # Symlink preserved, sync dir went to the suffix bump.
        assert (sync_root / "production").is_symlink()
        assert (sync_root / "production-2" / ".keboola" / "manifest.json").exists()
        # Resolved path expected because _rename_project_alias canonicalises
        # search_root via Path.resolve() before the disk move.
        assert result["rename"]["sync_dir"]["new_path"] == str(sync_root.resolve() / "production-2")


# ---------------------------------------------------------------------------
# --dry-run path (PR #266 review NIT) -- preview without mutation
# ---------------------------------------------------------------------------


class TestRenameAliasDryRun:
    """Dry-run validates everything but mutates nothing."""

    def test_dry_run_no_mutation_returns_planned_dict(
        self, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        service = _make_service(tmp_config_dir, alias="prod")
        result = service.edit_project(
            alias="prod",
            new_alias="production",
            search_root=tmp_path,
            dry_run=True,
        )
        # State unchanged -- alias still "prod" in the config.
        config = service._config_store.load()
        assert "prod" in config.projects
        assert "production" not in config.projects
        # Result reports dry_run=True + planned shape.
        assert result["dry_run"] is True
        assert result["alias"] == "prod"  # original alias, not the new one
        assert result["planned"]["new_alias"] == "production"
        assert result["planned"]["old_alias"] == "prod"
        assert result["planned"]["rename"]["new_alias"] == "production"
        assert result["planned"]["rename"]["default_project_would_update"] is True

    def test_dry_run_collision_still_raises(self, tmp_config_dir: Path) -> None:
        store = setup_two_projects(tmp_config_dir)
        service = ProjectService(config_store=store, client_factory=lambda u, t: make_mock_client())
        with pytest.raises(ConfigError, match="already in use"):
            service.edit_project(
                alias="prod", new_alias="dev", search_root=tmp_config_dir, dry_run=True
            )
        # Both projects intact -- validation fired before any mutation.
        config = service._config_store.load()
        assert set(config.projects.keys()) == {"prod", "dev"}

    def test_dry_run_format_validation_still_raises(
        self, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        service = _make_service(tmp_config_dir, alias="prod")
        with pytest.raises(ConfigError, match="path-traversal"):
            service.edit_project(
                alias="prod", new_alias="../etc", search_root=tmp_path, dry_run=True
            )

    def test_dry_run_with_nested_sync_dir_predicts_method_no_disk_change(
        self, tmp_path: Path, tmp_config_dir: Path
    ) -> None:
        service = _make_service(tmp_config_dir, alias="prod")
        sync_root = tmp_path / "workspace"
        sync_root.mkdir()
        (sync_root / "prod" / ".keboola").mkdir(parents=True)
        (sync_root / "prod" / ".keboola" / "manifest.json").write_text(
            '{"version": 2}', encoding="utf-8"
        )

        result = service.edit_project(
            alias="prod",
            new_alias="production",
            search_root=sync_root,
            dry_run=True,
        )

        sync_planned = result["planned"]["rename"]["sync_dir_would_move"]
        assert sync_planned is not None
        assert sync_planned["planned_new_path"] == str(sync_root.resolve() / "production")
        assert sync_planned["planned_method"] in ("git_mv", "shutil_move")
        assert sync_planned["collision_suffix"] is None  # no pre-existing target

        # Source dir untouched, target NEVER created on disk.
        assert (sync_root / "prod" / ".keboola" / "manifest.json").exists()
        assert not (sync_root / "production").exists()
