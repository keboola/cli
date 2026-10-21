"""CLI tests for `kbagent rls bind|unbind|bindings|bindings-sweep` (CliRunner, mocked service)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.constants import ENV_STACK_ID
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig

runner = CliRunner()
TEST_TOKEN = "999-token-abc"
STACK = "dev-keboola-aws-eu-west-1"
KID = f"KID:{STACK}:project-1234:workspace-42"
RLS_ID = "11111111-1111-4111-8111-111111111111"
CLS_ID = "33333333-3333-4333-8333-333333333333"


@pytest.fixture(autouse=True)
def _no_stack_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_STACK_ID, raising=False)


def _run(tmp_path: Path, args: list[str], service: MagicMock, input: str | None = None) -> Any:
    store = ConfigStore(config_dir=tmp_path / "cfg")
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
            project_name="prod",
            project_id=1234,
        ),
    )
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as mock_store,
        patch("keboola_agent_cli.cli.PolicyBindingService") as mock_service,
    ):
        mock_store.return_value = store
        mock_service.return_value = service
        return runner.invoke(app, args, input=input)


def _bound() -> dict[str, Any]:
    return {
        "project": "prod",
        "workspace_id": 42,
        "kid": KID,
        "bound": True,
        "binding_id": "b-1",
        "binding": {"id": "b-1", "kid": KID},
        "dry_run": True,
    }


class TestBind:
    def test_json_passes_the_split_lists(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.bind.return_value = {"kid": KID, "action": "created", "binding_id": "b-1"}

        result = _run(
            tmp_path,
            [
                "--json", "rls", "bind", "--project", "prod", "--workspace-id", "42",
                "--rls-policy", f"{RLS_ID},{RLS_ID}", "--cls-policy", CLS_ID,
                "--stack-id", STACK,
            ],
            service,
        )  # fmt: skip

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["data"]["kid"] == KID
        service.bind.assert_called_once_with(
            "prod",
            42,
            stack_id=STACK,
            rls_policies=[RLS_ID],
            cls_policies=[CLS_ID],
            dry_run=False,
        )

    def test_stack_id_from_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_STACK_ID, STACK)
        service = MagicMock()
        service.bind.return_value = {
            "kid": KID,
            "action": "replace",
            "rls_policy_ids": [RLS_ID],
            "cls_policy_ids": [],
            "warnings": ["not governed"],
        }

        result = _run(
            tmp_path,
            [
                "rls", "bind", "--project", "prod", "--workspace-id", "42",
                "--rls-policy", RLS_ID, "--dry-run",
            ],
            service,
        )  # fmt: skip

        assert result.exit_code == 0, result.output
        assert service.bind.call_args.kwargs["stack_id"] == STACK
        assert service.bind.call_args.kwargs["dry_run"] is True
        assert "Would replace the binding" in result.output
        assert "not governed" in result.output

    def test_usage_error_from_the_service_exits_2(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.bind.side_effect = KeboolaApiError(
            message="--stack-id is required", status_code=400, error_code=ErrorCode.INVALID_ARGUMENT
        )

        result = _run(
            tmp_path,
            ["--json", "rls", "bind", "--project", "prod", "--workspace-id", "42"],
            service,
        )

        assert result.exit_code == 2
        assert json.loads(result.stdout)["error"]["code"] == "INVALID_ARGUMENT"


class TestUnbind:
    ARGS: tuple[str, ...] = (
        "rls", "unbind", "--project", "prod", "--workspace-id", "42", "--stack-id", STACK,
    )  # fmt: skip

    def test_not_bound_needs_no_confirmation(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.unbind.return_value = {"kid": KID, "bound": False, "deleted": False}

        result = _run(tmp_path, list(self.ARGS), service)

        assert result.exit_code == 0, result.output
        assert "already gets every policy" in result.output
        service.unbind.assert_called_once()

    def test_dry_run_deletes_nothing(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.unbind.return_value = _bound()

        result = _run(tmp_path, ["--json", *self.ARGS, "--dry-run"], service)

        assert result.exit_code == 0, result.output
        service.unbind.assert_called_once_with("prod", 42, stack_id=STACK, dry_run=True)

    def test_without_terminal_refuses(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.unbind.return_value = _bound()

        result = _run(tmp_path, list(self.ARGS), service, input="y\n")

        assert result.exit_code == 2
        assert "CONFIRMATION_REQUIRED" in result.output
        assert service.unbind.call_count == 1  # the lookup only

    @pytest.mark.usefixtures("interactive_terminal")
    @pytest.mark.parametrize(("answer", "calls"), [("n\n", 1), ("y\n", 2)])
    def test_prompt(self, tmp_path: Path, answer: str, calls: int) -> None:
        service = MagicMock()
        service.unbind.side_effect = [_bound(), {"kid": KID, "deleted": True}]

        result = _run(tmp_path, list(self.ARGS), service, input=answer)

        assert result.exit_code == 0, result.output
        assert service.unbind.call_count == calls
        if calls == 2:
            assert "every policy of the project again" in result.output

    def test_yes_deletes(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.unbind.side_effect = [_bound(), {"kid": KID, "deleted": True}]

        result = _run(tmp_path, ["--json", *self.ARGS, "--yes"], service)

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["data"]["deleted"] is True

    def test_deny_destructive_blocks_it(self, tmp_path: Path) -> None:
        service = MagicMock()

        result = _run(tmp_path, ["--json", "--deny-destructive", *self.ARGS, "--yes"], service)

        assert result.exit_code == 6
        service.unbind.assert_not_called()


class TestBindings:
    def test_empty_human(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.list_bindings.return_value = {"project": "prod", "bindings": []}

        result = _run(tmp_path, ["rls", "bindings", "--project", "prod"], service)

        assert result.exit_code == 0, result.output
        assert "every workspace gets every policy" in result.output

    def test_table_and_filter(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.list_bindings.return_value = {
            "project": "prod",
            "bindings": [
                {
                    "kid": KID,
                    "rls_policy_ids": [RLS_ID],
                    "cls_policy_ids": [],
                    "updated": "2026-10-01T00:00:00Z",
                    "workspace_exists": False,
                }
            ],
        }

        result = _run(
            tmp_path,
            ["rls", "bindings", "--project", "prod", "--workspace-id", "42"],
            service,
        )

        assert result.exit_code == 0, result.output
        assert "NO" in result.output
        service.list_bindings.assert_called_once_with("prod", 42)


def _sweep_result(**counts: int) -> dict[str, Any]:
    summary = {"orphaned": 0, "deleted": 0, "kept": 0, "failed": 0} | counts
    return {
        "project": "prod",
        "dry_run": True,
        "references_checked": False,
        "references_note": "no reverse-reference lookup",
        "bindings": [
            {
                "kid": KID,
                "rls_policy_ids": [],
                "cls_policy_ids": [CLS_ID],
                "updated": None,
                "status": "orphaned",
                "reason": "gone",
            }
        ],
        "summary": summary,
    }


class TestSweep:
    def test_reports_by_default(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.sweep.return_value = _sweep_result(orphaned=1)

        result = _run(tmp_path, ["rls", "bindings-sweep", "--project", "prod"], service)

        assert result.exit_code == 0, result.output
        service.sweep.assert_called_once_with("prod", older_than_days=30, delete=False)
        assert "would be deleted" in result.output
        assert "no reverse-reference lookup" in result.output

    def test_yes_deletes(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.sweep.return_value = _sweep_result(deleted=1)

        result = _run(
            tmp_path,
            ["--json", "rls", "bindings-sweep", "--project", "prod", "--older-than", "7", "-y"],
            service,
        )

        assert result.exit_code == 0, result.output
        service.sweep.assert_called_once_with("prod", older_than_days=7, delete=True)

    def test_a_failed_item_exits_1(self, tmp_path: Path) -> None:
        service = MagicMock()
        service.sweep.return_value = _sweep_result(failed=1)

        result = _run(tmp_path, ["--json", "rls", "bindings-sweep", "--project", "prod"], service)

        assert result.exit_code == 1

    def test_dry_run_and_yes_conflict(self, tmp_path: Path) -> None:
        service = MagicMock()

        result = _run(
            tmp_path,
            ["--json", "rls", "bindings-sweep", "--project", "prod", "--dry-run", "--yes"],
            service,
        )

        assert result.exit_code == 2
        service.sweep.assert_not_called()

    @pytest.mark.parametrize(("flag", "exit_code"), [([], 0), (["--yes"], 6)])
    def test_deny_destructive_blocks_only_the_delete(
        self, tmp_path: Path, flag: list[str], exit_code: int
    ) -> None:
        service = MagicMock()
        service.sweep.return_value = _sweep_result()

        result = _run(
            tmp_path,
            ["--json", "--deny-destructive", "rls", "bindings-sweep", "--project", "prod", *flag],
            service,
        )

        assert result.exit_code == exit_code, result.output
        assert service.sweep.called is (exit_code == 0)
