"""Tests for the `config update --set` / `config row-update --set` sibling-path
guard (issue #593 Part A) and for `set_nested_value`'s bracket-syntax rejection.

Covers:
* `validate_set_paths` (pure function) rejects every guarded prefix with the
  right routing hint.
* `ConfigService.update_config` / `update_config_row` invoke the guard
  BEFORE any client/network call -- including with `dry_run=True`.
* A normal `--set 'parameters.x=y'` (and a nested `parameters.state.x` path,
  where `state` is not the first segment) still works.
* `set_nested_value` raises a clear error for bracket syntax (`files[0]`)
  instead of silently creating a literal `"files[0]"` key, while the existing
  `files.0` dotted-integer form keeps working.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner, Result

from helpers import setup_single_project
from keboola_agent_cli.cli import app
from keboola_agent_cli.constants import CONFIG_SET_GUARDED_PREFIXES
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.json_utils import set_nested_value
from keboola_agent_cli.services.config_service import ConfigService, validate_set_paths

runner = CliRunner()

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _config_detail(configuration: dict | None = None) -> dict:
    return {
        "id": "cfg-001",
        "name": "My Config",
        "description": "desc",
        "version": 3,
        "configuration": configuration if configuration is not None else {"parameters": {"a": 1}},
    }


def _row_detail(configuration: dict | None = None) -> dict:
    return {
        "id": "row-001",
        "name": "My Row",
        "description": "desc",
        "isDisabled": False,
        "configuration": configuration if configuration is not None else {"parameters": {"a": 1}},
    }


def _make_service(tmp_config_dir: Path) -> tuple[ConfigService, MagicMock]:
    store = setup_single_project(tmp_config_dir)
    mock_client = MagicMock()
    mock_client.get_config_detail.return_value = _config_detail()
    mock_client.get_config_row.return_value = _row_detail()
    mock_client.update_config.return_value = {"id": "cfg-001", "name": "My Config"}
    mock_client.update_config_row.return_value = {"id": "row-001", "name": "My Row"}
    service = ConfigService(
        config_store=store,
        client_factory=lambda url, token: mock_client,
    )
    return service, mock_client


# ---------------------------------------------------------------------------
# validate_set_paths (pure function) -- one case per guarded prefix
# ---------------------------------------------------------------------------


class TestValidateSetPathsGuardedPrefixes:
    """Every guarded prefix must be rejected, with the right routing hint."""

    @pytest.mark.parametrize("prefix", sorted(CONFIG_SET_GUARDED_PREFIXES))
    def test_every_guarded_prefix_is_rejected(self, prefix: str) -> None:
        with pytest.raises(KeboolaApiError) as exc_info:
            validate_set_paths([(f"{prefix}.sub", "value")])
        assert exc_info.value.error_code == ErrorCode.INVALID_ARGUMENT
        # Names the offending path and its first segment.
        assert f"{prefix}.sub" in exc_info.value.message
        assert f"'{prefix}'" in exc_info.value.message
        # Explains --set only edits configuration.*.
        assert "configuration.*" in exc_info.value.message
        # Escape hatch mentioned.
        assert "--configuration" in exc_info.value.message

    @pytest.mark.parametrize("prefix", sorted(CONFIG_SET_GUARDED_PREFIXES))
    def test_bare_prefix_without_dot_is_also_rejected(self, prefix: str) -> None:
        """A path that IS the guarded key (no further segments) is rejected too."""
        with pytest.raises(KeboolaApiError):
            validate_set_paths([(prefix, "value")])

    def test_state_hints_at_state_set(self) -> None:
        with pytest.raises(KeboolaApiError, match="config state-set"):
            validate_set_paths([("state.lastId", 123)])

    def test_name_hints_at_name_flag(self) -> None:
        with pytest.raises(KeboolaApiError, match="--name"):
            validate_set_paths([("name", "New Name")])

    def test_description_hints_at_description_flag(self) -> None:
        with pytest.raises(KeboolaApiError, match="--description"):
            validate_set_paths([("description", "New description")])

    def test_is_disabled_hints_at_row_update_flags(self) -> None:
        with pytest.raises(KeboolaApiError) as exc_info:
            validate_set_paths([("isDisabled", True)])
        message = exc_info.value.message
        assert "--is-disabled" in message
        assert "--is-enabled" in message

    def test_unmapped_prefix_falls_back_to_generic_message(self) -> None:
        with pytest.raises(KeboolaApiError, match="not settable via --set"):
            validate_set_paths([("creatorToken", "x")])


class TestValidateSetPathsRegression:
    """Non-guarded paths must pass through untouched."""

    def test_none_is_a_noop(self) -> None:
        validate_set_paths(None)  # no raise

    def test_empty_list_is_a_noop(self) -> None:
        validate_set_paths([])  # no raise

    def test_normal_parameters_path_passes(self) -> None:
        validate_set_paths([("parameters.db.host", "new-host")])  # no raise

    def test_nested_state_segment_is_not_guarded(self) -> None:
        """The guard only inspects the FIRST segment -- `state` deeper in the
        path (e.g. a component that legitimately has configuration.parameters.state)
        must not be blocked."""
        validate_set_paths([("parameters.state.x", "y")])  # no raise

    def test_multiple_paths_one_bad_one_good(self) -> None:
        with pytest.raises(KeboolaApiError):
            validate_set_paths(
                [
                    ("parameters.x", "y"),
                    ("state.lastId", 1),
                ]
            )


# ---------------------------------------------------------------------------
# ConfigService.update_config -- guard fires before any client call
# ---------------------------------------------------------------------------


class TestUpdateConfigGuard:
    def test_guarded_set_blocks_before_get_config_detail(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir)

        with pytest.raises(KeboolaApiError) as exc_info:
            service.update_config(
                alias="prod",
                component_id="keboola.ex-db-snowflake",
                config_id="cfg-001",
                set_paths=[("state.lastId", 123)],
            )

        assert exc_info.value.error_code == ErrorCode.INVALID_ARGUMENT
        client.get_config_detail.assert_not_called()
        client.update_config.assert_not_called()

    def test_guarded_set_blocks_with_dry_run(self, tmp_config_dir: Path) -> None:
        """--dry-run must fail too -- no network call, no plausible diff."""
        service, client = _make_service(tmp_config_dir)

        with pytest.raises(KeboolaApiError):
            service.update_config(
                alias="prod",
                component_id="keboola.ex-db-snowflake",
                config_id="cfg-001",
                set_paths=[("name", "New Name")],
                dry_run=True,
            )

        client.get_config_detail.assert_not_called()
        client.update_config.assert_not_called()

    def test_normal_set_still_works(self, tmp_config_dir: Path) -> None:
        """Regression: a plain configuration.* --set is unaffected."""
        service, client = _make_service(tmp_config_dir)

        service.update_config(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            set_paths=[("parameters.x", "y")],
        )

        client.get_config_detail.assert_called_once()
        client.update_config.assert_called_once()
        written = client.update_config.call_args.kwargs["configuration"]
        assert written["parameters"]["x"] == "y"
        # Original sibling keys under parameters survive (read-modify-write).
        assert written["parameters"]["a"] == 1

    def test_nested_state_segment_path_still_works(self, tmp_config_dir: Path) -> None:
        """`parameters.state.x` -- `state` is NOT the first segment, so it must
        pass the guard and land inside configuration.parameters.state.x."""
        service, client = _make_service(tmp_config_dir)

        service.update_config(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            set_paths=[("parameters.state.x", "y")],
        )

        written = client.update_config.call_args.kwargs["configuration"]
        assert written["parameters"]["state"]["x"] == "y"


# ---------------------------------------------------------------------------
# ConfigService.update_config_row -- same guard, same consistency
# ---------------------------------------------------------------------------


class TestUpdateConfigRowGuard:
    def test_guarded_set_blocks_before_get_config_row(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir)

        with pytest.raises(KeboolaApiError) as exc_info:
            service.update_config_row(
                alias="prod",
                component_id="keboola.ex-db-snowflake",
                config_id="cfg-001",
                row_id="row-001",
                set_paths=[("isDisabled", True)],
            )

        assert exc_info.value.error_code == ErrorCode.INVALID_ARGUMENT
        client.get_config_row.assert_not_called()
        client.update_config_row.assert_not_called()

    def test_guarded_set_blocks_with_dry_run(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir)

        with pytest.raises(KeboolaApiError):
            service.update_config_row(
                alias="prod",
                component_id="keboola.ex-db-snowflake",
                config_id="cfg-001",
                row_id="row-001",
                set_paths=[("state.x", 1)],
                dry_run=True,
            )

        client.get_config_row.assert_not_called()
        client.update_config_row.assert_not_called()

    def test_normal_set_still_works(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir)

        service.update_config_row(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            row_id="row-001",
            set_paths=[("parameters.table", "orders")],
        )

        client.get_config_row.assert_called_once()
        client.update_config_row.assert_called_once()
        written = client.update_config_row.call_args.kwargs["configuration"]
        assert written["parameters"]["table"] == "orders"


# ---------------------------------------------------------------------------
# set_nested_value -- bracket-syntax guard + files.0 regression
# ---------------------------------------------------------------------------


class TestSetNestedValueBracketGuard:
    def test_bracket_syntax_raises_clear_error(self) -> None:
        with pytest.raises(ValueError, match=r"files\.0"):
            set_nested_value({"files": [1, 2, 3]}, "files[0]", 99)

    def test_bracket_syntax_deep_in_path_also_raises(self) -> None:
        with pytest.raises(ValueError, match=r"files\.0"):
            set_nested_value(
                {"parameters": {"files": [{"name": "a"}]}},
                "parameters.files[0].name",
                "b",
            )

    def test_dotted_integer_index_still_works(self) -> None:
        """Regression: the existing supported form must keep working."""
        result = set_nested_value({"files": [1, 2, 3]}, "files.0", 99)
        assert result["files"] == [99, 2, 3]

    def test_dotted_integer_index_on_nested_list_still_works(self) -> None:
        result = set_nested_value(
            {"parameters": {"files": [{"name": "a"}, {"name": "b"}]}},
            "parameters.files.1.name",
            "changed",
        )
        assert result["parameters"]["files"][1]["name"] == "changed"

    def test_bracket_syntax_does_not_create_literal_key(self) -> None:
        """Defense-in-depth: confirm the rejected call never returns a dict at
        all -- i.e. there is no code path where a literal "files[0]" key could
        slip through, since the function raises before constructing a result."""
        obj = {"files": [1, 2, 3]}
        try:
            set_nested_value(obj, "files[0]", 99)
        except ValueError:
            pass
        else:
            pytest.fail("expected ValueError for bracket syntax")
        assert "files[0]" not in obj


# ---------------------------------------------------------------------------
# CLI layer -- exit code 2 for the guard (issue #593 Part A follow-up)
#
# `validate_set_paths` raises KeboolaApiError(INVALID_ARGUMENT), which
# `map_error_to_exit_code` maps to the generic exit 1 (INVALID_ARGUMENT has
# no special-cased mapping). The spec requires a USAGE-error exit code (2)
# for this guard specifically, so `config update` / `config row-update` call
# `validate_set_paths` directly right after building `parsed_sets` and force
# `typer.Exit(code=2)` on failure -- see commands/config.py.
# ---------------------------------------------------------------------------


class TestConfigSetGuardCliExitCode:
    """CLI-level exit-code coverage for the --set sibling-path guard."""

    @staticmethod
    def _invoke(tmp_config_dir: Path, args: list[str]) -> Result:
        return runner.invoke(app, ["--json", "--config-dir", str(tmp_config_dir), *args])

    @staticmethod
    def _patch_service(mp: pytest.MonkeyPatch, service: ConfigService) -> None:
        mp.setattr(
            "keboola_agent_cli.commands.config.get_service",
            lambda ctx, name: service,
        )

    def test_config_update_set_state_exits_2(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir)

        with pytest.MonkeyPatch.context() as mp:
            self._patch_service(mp, service)
            result = self._invoke(
                tmp_config_dir,
                [
                    "config",
                    "update",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--set",
                    "state.foo=1",
                ],
            )

        assert result.exit_code == 2, result.output
        client.get_config_detail.assert_not_called()
        client.update_config.assert_not_called()

    def test_config_update_set_state_dry_run_also_exits_2(self, tmp_config_dir: Path) -> None:
        """--dry-run must not bypass the guard -- no plausible preview for a
        usage mistake."""
        service, client = _make_service(tmp_config_dir)

        with pytest.MonkeyPatch.context() as mp:
            self._patch_service(mp, service)
            result = self._invoke(
                tmp_config_dir,
                [
                    "config",
                    "update",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--set",
                    "state.foo=1",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 2, result.output
        client.get_config_detail.assert_not_called()

    def test_config_update_set_parameters_regression_not_exit_2(self, tmp_config_dir: Path) -> None:
        """Regression: a normal configuration.* --set passes the guard and
        reaches the (mocked) client -- exit code is NOT 2."""
        service, client = _make_service(tmp_config_dir)

        with pytest.MonkeyPatch.context() as mp:
            self._patch_service(mp, service)
            result = self._invoke(
                tmp_config_dir,
                [
                    "config",
                    "update",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--set",
                    "parameters.x=y",
                ],
            )

        assert result.exit_code == 0, result.output
        client.update_config.assert_called_once()

    def test_config_row_update_set_state_exits_2(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir)

        with pytest.MonkeyPatch.context() as mp:
            self._patch_service(mp, service)
            result = self._invoke(
                tmp_config_dir,
                [
                    "config",
                    "row-update",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--row-id",
                    "row-001",
                    "--set",
                    "isDisabled=true",
                ],
            )

        assert result.exit_code == 2, result.output
        client.get_config_row.assert_not_called()
        client.update_config_row.assert_not_called()

    def test_config_row_update_set_state_dry_run_also_exits_2(self, tmp_config_dir: Path) -> None:
        service, client = _make_service(tmp_config_dir)

        with pytest.MonkeyPatch.context() as mp:
            self._patch_service(mp, service)
            result = self._invoke(
                tmp_config_dir,
                [
                    "config",
                    "row-update",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--row-id",
                    "row-001",
                    "--set",
                    "state.x=1",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 2, result.output
        client.get_config_row.assert_not_called()

    def test_config_row_update_set_parameters_regression_not_exit_2(
        self, tmp_config_dir: Path
    ) -> None:
        service, client = _make_service(tmp_config_dir)

        with pytest.MonkeyPatch.context() as mp:
            self._patch_service(mp, service)
            result = self._invoke(
                tmp_config_dir,
                [
                    "config",
                    "row-update",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-001",
                    "--row-id",
                    "row-001",
                    "--set",
                    "parameters.table=orders",
                ],
            )

        assert result.exit_code == 0, result.output
        client.update_config_row.assert_called_once()
