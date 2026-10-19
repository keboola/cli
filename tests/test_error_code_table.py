"""The error code table (spec E1, E2, E5).

One table ``ErrorCode -> (error_type, exit_code)`` in ``errors.py``.
``map_error_to_exit_code`` reads it, ``docs/error-codes.md`` repeats it, and a
per-item failure entry carries ``error_code``, ``retryable`` and ``message``.
The four paths whose exit code did not match the table before 1.0.0 have their
own tests next to their commands (``transformation`` in
``test_transformation_cli.py``, ``lineage show`` in
``test_deep_lineage_service.py``, ``flow schema`` in ``test_flow_cli.py``,
``http`` in ``test_http_client_cmd.py``). The tests below cover the rest.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.commands._helpers import map_error_to_exit_code
from keboola_agent_cli.constants import ENV_KBAGENT_SERVE_TOKEN, ENV_KBAGENT_SERVE_URL
from keboola_agent_cli.errors import (
    ERROR_CODE_TABLE,
    ERROR_TYPES,
    ConfigError,
    ErrorCode,
    KeboolaApiError,
    SessionAuthUnsupportedError,
    exit_code_for,
    map_error_code_to_type,
)
from keboola_agent_cli.services.base import item_failure

runner = CliRunner()

_GUARD_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_error_codes.py"


def _load_guard() -> Any:
    spec = importlib.util.spec_from_file_location("_check_error_codes_table", _GUARD_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTable:
    def test_every_code_has_an_entry(self) -> None:
        assert set(ERROR_CODE_TABLE) == set(ErrorCode)

    def test_entries_use_known_types_and_error_exit_codes(self) -> None:
        for code, entry in ERROR_CODE_TABLE.items():
            assert entry.error_type in ERROR_TYPES, code
            assert entry.exit_code in {1, 2, 3, 4, 5, 6, 7}, code

    @pytest.mark.parametrize("code", list(ErrorCode))
    def test_map_error_to_exit_code_reads_the_table(self, code: ErrorCode) -> None:
        exc = KeboolaApiError("boom", error_code=code)
        assert map_error_to_exit_code(exc) == ERROR_CODE_TABLE[code].exit_code

    def test_a_code_outside_the_enum_is_unknown_error(self) -> None:
        assert exit_code_for("NOT_A_CODE") == 1
        assert map_error_code_to_type("NOT_A_CODE") == "api"

    @pytest.mark.parametrize(
        ("code", "error_type"),
        [
            (ErrorCode.INVALID_ARGUMENT, "validation"),
            (ErrorCode.USAGE_ERROR, "validation"),
            (ErrorCode.MISSING_PARAMETER, "validation"),
            (ErrorCode.FILE_NOT_FOUND, "not_found"),
        ],
    )
    def test_the_four_codes_that_had_no_explicit_type(
        self, code: ErrorCode, error_type: str
    ) -> None:
        """Breaking-change row 13: these four took the `api` default before 1.0.0."""
        assert map_error_code_to_type(code) == error_type

    @pytest.mark.parametrize(
        ("code", "exit_code"),
        [
            (ErrorCode.VALIDATION_ERROR, 2),
            (ErrorCode.INVALID_ARGUMENT, 2),
            (ErrorCode.INVALID_FORMAT, 2),
            (ErrorCode.MISSING_PARAMETER, 2),
            (ErrorCode.FILE_NOT_FOUND, 2),
            (ErrorCode.DATA_APP_INVALID_SECRET, 2),
            (ErrorCode.AUTH_MFA_INVALID, 3),
            (ErrorCode.AUTH_NOT_SUPPORTED_ON_STACK, 5),
            (ErrorCode.CONFIG_ERROR, 5),
            (ErrorCode.PERMISSION_DENIED, 6),
            (ErrorCode.JOB_TIMEOUT_TERMINATED, 7),
        ],
    )
    def test_codes_whose_api_error_path_exited_1(self, code: ErrorCode, exit_code: int) -> None:
        """Breaking-change row 21: through `map_error_to_exit_code` these exited 1 before 1.0.0."""
        assert map_error_to_exit_code(KeboolaApiError("boom", error_code=code)) == exit_code


class TestGuard:
    def test_repository_passes(self) -> None:
        assert _load_guard().code_table_violations() == {}

    def test_doc_row_that_disagrees_with_the_table_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        guard = _load_guard()
        doc = guard.DOC_PATH.read_text(encoding="utf-8")
        row = "| `NOT_FOUND` | 1 | `not_found` |"
        assert row in doc
        planted = tmp_path / "error-codes.md"
        planted.write_text(doc.replace(row, "| `NOT_FOUND` | 4 | `not_found` |"), encoding="utf-8")
        monkeypatch.setattr(guard, "DOC_PATH", planted)
        assert set(guard.code_table_violations()) == {"NOT_FOUND"}

    def test_code_without_an_entry_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        guard = _load_guard()
        table = dict(ERROR_CODE_TABLE)
        del table[ErrorCode.KAI_ERROR]
        monkeypatch.setattr("keboola_agent_cli.errors.ERROR_CODE_TABLE", table)
        assert guard.code_table_violations() == {
            "KAI_ERROR": "no entry in ERROR_CODE_TABLE (errors.py)"
        }


class TestCorrectedPaths:
    @pytest.mark.parametrize("command", ["info", "server"])
    def test_lineage_missing_cache_file_exits_2(self, command: str) -> None:
        result = runner.invoke(
            app, ["--json", "lineage", command, "--load", "/nonexistent/lineage.json"]
        )
        assert result.exit_code == 2, result.output
        error = json.loads(result.output)["error"]
        assert error["code"] == "FILE_NOT_FOUND"
        assert error["error_type"] == "not_found"

    def test_http_invalid_body_is_invalid_argument(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_KBAGENT_SERVE_URL, "http://127.0.0.1:8001")
        monkeypatch.setenv(ENV_KBAGENT_SERVE_TOKEN, "test-bearer-fixture")
        result = runner.invoke(app, ["--json", "http", "post", "/x", "--body", "not json{"])
        assert result.exit_code == 2, result.output
        assert json.loads(result.output)["error"]["code"] == "INVALID_ARGUMENT"

    def test_http_missing_env_is_config_error_exit_5(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_KBAGENT_SERVE_URL, raising=False)
        monkeypatch.delenv(ENV_KBAGENT_SERVE_TOKEN, raising=False)
        result = runner.invoke(app, ["--json", "http", "get", "/projects"])
        assert result.exit_code == 5, result.output
        error = json.loads(result.output)["error"]
        assert error["code"] == "CONFIG_ERROR"
        assert error["error_type"] == "configuration"


class TestItemFailure:
    def test_typed_error_keeps_code_retryable_and_message(self) -> None:
        exc = KeboolaApiError("Bucket busy", error_code=ErrorCode.API_ERROR, retryable=True)
        assert item_failure(exc, id="in.c-a", error=exc.message) == {
            "id": "in.c-a",
            "error": "Bucket busy",
            "error_code": "API_ERROR",
            "retryable": True,
            "message": "Bucket busy",
        }

    def test_config_error_subclass_keeps_its_code(self) -> None:
        entry = item_failure(SessionAuthUnsupportedError("kbagent kai"), alias="prod")
        assert entry["error_code"] == "AUTH_NOT_SUPPORTED_ON_STACK"
        assert entry["retryable"] is False

    def test_plain_config_error_is_config_error(self) -> None:
        entry = item_failure(ConfigError("Project 'x' not found."), alias="x", error="e")
        assert entry["error_code"] == "CONFIG_ERROR"
        assert entry["message"] == "Project 'x' not found."
        assert entry["error"] == "e"

    def test_unexpected_exception_gets_the_fallback_and_a_short_message(self) -> None:
        entry = item_failure(RuntimeError("x" * 5000), name="m", reason="kept")
        assert entry["error_code"] == "UNEXPECTED_ERROR"
        assert entry["reason"] == "kept"
        assert len(entry["message"]) < 5000


class TestSearchExitCodes:
    """`search` takes its exit code from the table and keeps a typed config code."""

    def _run(self, tmp_path: Path, side_effect: Exception) -> Any:
        from unittest.mock import MagicMock, patch

        from keboola_agent_cli.config_store import ConfigStore
        from keboola_agent_cli.models import ProjectConfig
        from keboola_agent_cli.services.project_service import ProjectService

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-55555-fakeTestTokenDoNotUseXXXXXXXX",
                project_name="prod",
                project_id=1234,
            ),
        )
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as mock_store,
            patch("keboola_agent_cli.cli.ProjectService") as mock_projects,
            patch("keboola_agent_cli.cli.SearchService") as mock_search,
        ):
            mock_store.return_value = store
            mock_projects.return_value = ProjectService(config_store=store)
            mock_svc = MagicMock()
            mock_svc.search.side_effect = side_effect
            mock_search.return_value = mock_svc
            return runner.invoke(app, ["--json", "search", "orders"])

    def test_api_error_exit_comes_from_the_table(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, KeboolaApiError("bad", error_code=ErrorCode.INVALID_TOKEN))
        assert result.exit_code == 3, result.output
        assert json.loads(result.output)["error"]["code"] == "INVALID_TOKEN"

    def test_config_error_keeps_its_typed_code(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, SessionAuthUnsupportedError("search"))
        assert result.exit_code == 5, result.output
        assert json.loads(result.output)["error"]["code"] == "AUTH_NOT_SUPPORTED_ON_STACK"


def test_data_app_unreadable_description_file_exits_1(tmp_path: Path) -> None:
    """READ_ERROR exits 1 (spec E1). This path exited 2 before 1.0.0."""
    # A directory passes Click's exists/readable check and fails read_text().
    unreadable = tmp_path
    result = runner.invoke(
        app,
        [
            "--json",
            "data-app",
            "create",
            "--project",
            "prod",
            "--name",
            "App",
            "--slug",
            "app",
            "--git-repo",
            "https://github.com/example/app",
            "--description-file",
            str(unreadable),
            "--dry-run",
        ],
    )
    assert result.exit_code == 1, result.output
    assert json.loads(result.output)["error"]["code"] == "READ_ERROR"
