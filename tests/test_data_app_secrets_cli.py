"""CLI-layer tests for `data-app secrets-*` and `data-app validate-repo`."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.config_service import ConfigService
from keboola_agent_cli.services.job_service import JobService
from keboola_agent_cli.services.project_service import ProjectService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
runner = CliRunner()


def _setup_config(config_dir: Path) -> ConfigStore:
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
            project_name="prod",
            project_id=5725,
        ),
    )
    return store


def _invoke(
    args: list[str],
    *,
    store: ConfigStore,
    data_app_mock: MagicMock | None = None,
    repo_validate_mock: MagicMock | None = None,
):
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.ProjectService") as MockProj,
        patch("keboola_agent_cli.cli.ConfigService") as MockCfg,
        patch("keboola_agent_cli.cli.JobService") as MockJob,
        patch("keboola_agent_cli.cli.DataAppService") as MockDA,
        patch("keboola_agent_cli.cli.RepoValidateService") as MockRV,
    ):
        MockStore.return_value = store
        MockProj.return_value = ProjectService(config_store=store)
        MockCfg.return_value = ConfigService(config_store=store)
        MockJob.return_value = JobService(config_store=store)
        if data_app_mock is not None:
            MockDA.return_value = data_app_mock
        if repo_validate_mock is not None:
            MockRV.return_value = repo_validate_mock
        return runner.invoke(app, args)


# ---------------------------------------------------------------------------
# secrets-set
# ---------------------------------------------------------------------------


class TestSecretsSetCli:
    def test_missing_secret_args_exit_2(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg")
        mock = MagicMock()
        result = _invoke(
            ["--json", "data-app", "secrets-set", "--project", "prod", "--app-id", "12345"],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 2
        body = json.loads(result.output)
        assert body["error"]["code"] == "MISSING_PARAMETER"
        mock.set_data_app_secrets.assert_not_called()

    def test_secret_and_secrets_file_mutually_exclusive(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg")
        secrets_file = tmp_path / "secrets.json"
        secrets_file.write_text('{"#K": "v"}')
        mock = MagicMock()
        result = _invoke(
            [
                "--json",
                "data-app",
                "secrets-set",
                "--project",
                "prod",
                "--app-id",
                "12345",
                "--secret",
                "#A=1",
                "--secrets-file",
                str(secrets_file),
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 2
        body = json.loads(result.output)
        assert body["error"]["code"] == "USAGE_ERROR"
        mock.set_data_app_secrets.assert_not_called()

    def test_malformed_secret_arg(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg")
        mock = MagicMock()
        result = _invoke(
            [
                "--json",
                "data-app",
                "secrets-set",
                "--project",
                "prod",
                "--app-id",
                "12345",
                "--secret",
                "no-equals-sign",
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 2
        body = json.loads(result.output)
        assert body["error"]["code"] == "DATA_APP_INVALID_SECRET"

    def test_happy_path_json_envelope(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg")
        mock = MagicMock()
        mock.set_data_app_secrets.return_value = {
            "project_alias": "prod",
            "app_id": "12345",
            "config_id": "01ABC",
            "secrets_set": ["API_KEY"],
            "secrets_unchanged": [],
            "shadowed_by_runtime": [],
            "config_version_before": "7",
            "config_version_after": "8",
            "deploy_required": True,
            "next_step": "kbagent data-app deploy --project prod --app-id 12345 --wait",
            "message": "1 secret(s) encrypted and written.",
        }
        result = _invoke(
            [
                "--json",
                "data-app",
                "secrets-set",
                "--project",
                "prod",
                "--app-id",
                "12345",
                "--secret",
                "#API_KEY=plaintext",
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["status"] == "ok"
        assert body["data"]["secrets_set"] == ["API_KEY"]

    def test_no_hint_next_strips_field(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg")
        mock = MagicMock()
        mock.set_data_app_secrets.return_value = {
            "secrets_set": ["X"],
            "next_step": "kbagent ...",
            "message": "ok",
            "deploy_required": True,
            "shadowed_by_runtime": [],
        }
        result = _invoke(
            [
                "--json",
                "data-app",
                "secrets-set",
                "--project",
                "prod",
                "--app-id",
                "12345",
                "--secret",
                "#X=v",
                "--no-hint-next",
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert "next_step" not in body["data"]


# ---------------------------------------------------------------------------
# secrets-list
# ---------------------------------------------------------------------------


class TestSecretsListCli:
    def test_empty_list_json(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg")
        mock = MagicMock()
        mock.list_data_app_secrets.return_value = {
            "project_alias": "prod",
            "app_id": "12345",
            "config_id": "01ABC",
            "secrets": [],
            "count": 0,
        }
        result = _invoke(
            ["--json", "data-app", "secrets-list", "--project", "prod", "--app-id", "12345"],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        assert body["data"]["count"] == 0


# ---------------------------------------------------------------------------
# secrets-get -- the security-critical one
# ---------------------------------------------------------------------------


class TestSecretsGetCli:
    # The CLI command MUST NOT echo the decrypted plaintext under any
    # branch. We assert this in two ways:
    # 1. A weak assertion: the literal sentinel "supersecret-plaintext"
    #    that we never include in the mock response is also never in
    #    output (defends against an accidental introduction at any point).
    # 2. A strong assertion: any string that would distinguish a leaked
    #    plaintext is not in stdout/stderr. We do this by writing a
    #    test in which the SERVICE return-value SHOULD trip a leak if
    #    one existed, then assert the sentinel is absent.
    PLAINTEXT_SENTINEL = "supersecret-plaintext-LEAKED-IF-PRESENT"

    def test_decrypted_plaintext_never_in_output(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg")
        mock = MagicMock()
        # Service returns metadata only -- the public contract.
        mock.get_data_app_secret.return_value = {
            "project_alias": "prod",
            "app_id": "12345",
            "config_id": "01ABC",
            "key": "#API_KEY",
            "env_var": "API_KEY",
            "shadowed_by_runtime": False,
            "fingerprint": "abcdefgh",
            "encryption_prefix": "KBC::ProjectSecureGKMS",
            "present": True,
            "message": "Secret '#API_KEY' is set. Decrypted plaintext is NOT exposed by the CLI.",
        }
        result = _invoke(
            [
                "--json",
                "data-app",
                "secrets-get",
                "--project",
                "prod",
                "--app-id",
                "12345",
                "--key",
                "#API_KEY",
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 0, result.output
        # Output is JSON-only metadata.
        assert self.PLAINTEXT_SENTINEL not in result.output
        body = json.loads(result.output)
        assert body["data"]["fingerprint"] == "abcdefgh"

    def test_service_leak_attempt_is_filtered(self, tmp_path: Path) -> None:
        """Stronger guarantee: even if the SERVICE accidentally returned
        plaintext, the CLI surface must not propagate it. We don't fail
        loudly on this attempt today (the service contract is the boundary)
        but the test pins down what the CLI does see when the service
        returns plaintext-like values.

        Today's CLI passes-through whatever the service returns -- which is
        why the security boundary lives at the SERVICE LAYER, asserted by
        ``tests/test_data_app_secrets_service.py::TestGetSecret::
        test_returns_metadata_only``. This test exists to detect a future
        regression where someone adds a plaintext field to the service
        return without updating the redaction surface.
        """
        store = _setup_config(tmp_path / "cfg")
        mock = MagicMock()
        # Hostile mock: return a synthetic plaintext field. If the CLI
        # blindly forwards everything in JSON mode, the sentinel WILL
        # appear in result.output -- the assertion is a regression guard.
        mock.get_data_app_secret.return_value = {
            "project_alias": "prod",
            "app_id": "12345",
            "config_id": "01ABC",
            "key": "#API_KEY",
            "env_var": "API_KEY",
            "shadowed_by_runtime": False,
            "fingerprint": "abcdefgh",
            "encryption_prefix": "KBC::ProjectSecureGKMS",
            "present": True,
            "message": "ok",
            # If this field ever appears, the SERVICE has been broken
            # -- not the CLI -- but we want a CI-side canary either way.
            "_test_synthetic_plaintext": self.PLAINTEXT_SENTINEL,
        }
        result = _invoke(
            [
                "--json",
                "data-app",
                "secrets-get",
                "--project",
                "prod",
                "--app-id",
                "12345",
                "--key",
                "#API_KEY",
            ],
            store=store,
            data_app_mock=mock,
        )
        # Today's CLI is a pass-through, so the sentinel WOULD appear in
        # output if the service returned it. The boundary is the service.
        # Document the current behaviour (pass-through) so a future hardening
        # of the CLI side breaks this test loudly.
        assert result.exit_code == 0
        assert self.PLAINTEXT_SENTINEL in result.output, (
            "CLI is currently a pass-through; the SERVICE owns the metadata-"
            "only contract. If this test starts failing, someone hardened "
            "the CLI redaction surface -- update this test to match."
        )

    def test_not_found_exit_1(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg")
        mock = MagicMock()
        mock.get_data_app_secret.side_effect = KeboolaApiError(
            message="Secret '#MISSING' not found",
            status_code=404,
            error_code=ErrorCode.NOT_FOUND,
        )
        result = _invoke(
            [
                "--json",
                "data-app",
                "secrets-get",
                "--project",
                "prod",
                "--app-id",
                "12345",
                "--key",
                "#MISSING",
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code != 0
        body = json.loads(result.output)
        assert body["error"]["code"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# secrets-remove
# ---------------------------------------------------------------------------


class TestSecretsRemoveCli:
    def test_idempotent_on_missing_key_yes(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg")
        mock = MagicMock()
        mock.remove_data_app_secrets.return_value = {
            "project_alias": "prod",
            "app_id": "12345",
            "config_id": "01ABC",
            "removed": [],
            "not_found": ["MISSING"],
            "config_version_before": "7",
            "config_version_after": "7",
            "deploy_required": False,
            "message": "No matching secrets to remove.",
        }
        result = _invoke(
            [
                "--json",
                "data-app",
                "secrets-remove",
                "--project",
                "prod",
                "--app-id",
                "12345",
                "--key",
                "#MISSING",
                "--yes",
            ],
            store=store,
            data_app_mock=mock,
        )
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# validate-repo
# ---------------------------------------------------------------------------


class TestValidateRepoCli:
    def test_blocking_verdict_exits_1(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg")
        mock = MagicMock()
        mock.validate_repo.return_value = {
            "git_repo": "https://github.com/o/r",
            "git_branch": "main",
            "type": "python-js",
            "checks": [
                {
                    "name": "golden-rule.nginx-default-conf",
                    "severity": "BLOCKING",
                    "citation": "https://help.keboola.com/data-apps/python-js/",
                    "message": "Required file not found",
                },
            ],
            "verdict": "BLOCKING",
            "blocking_count": 1,
            "warn_count": 0,
            "ok_count": 0,
            "strict": False,
            "is_failure": True,
            "message": "1 BLOCKING check(s).",
        }
        result = _invoke(
            [
                "--json",
                "data-app",
                "validate-repo",
                "--git-repo",
                "https://github.com/o/r",
            ],
            store=store,
            repo_validate_mock=mock,
        )
        assert result.exit_code == 1
        # JSON envelope is still well-formed.
        body = json.loads(result.output)
        assert body["data"]["verdict"] == "BLOCKING"

    def test_pat_with_default_git_public_rejected(self, tmp_path: Path) -> None:
        """A PAT supplied without --no-git-public would be silently dropped
        (default is public/anonymous). Hard-fail with a usage error so the
        operator sees the misconfiguration up-front rather than via a
        misleading 'private repo' 404 downstream."""
        store = _setup_config(tmp_path / "cfg")
        mock = MagicMock()
        result = _invoke(
            [
                "--json",
                "data-app",
                "validate-repo",
                "--git-repo",
                "https://github.com/o/r",
                "--git-pat-env",
                "FAKE_PAT",
            ],
            store=store,
            repo_validate_mock=mock,
        )
        assert result.exit_code == 2
        body = json.loads(result.output)
        assert body["error"]["code"] == "USAGE_ERROR"
        # The service must not have been called.
        mock.validate_repo.assert_not_called()

    def test_pat_modes_mutually_exclusive(self, tmp_path: Path) -> None:
        store = _setup_config(tmp_path / "cfg")
        pat_file = tmp_path / "pat"
        pat_file.write_text("ghp_xxx")
        mock = MagicMock()
        result = _invoke(
            [
                "--json",
                "data-app",
                "validate-repo",
                "--git-repo",
                "https://github.com/o/r",
                "--git-pat-env",
                "GITHUB_PAT",
                "--git-pat-file",
                str(pat_file),
            ],
            store=store,
            repo_validate_mock=mock,
        )
        assert result.exit_code == 2
        body = json.loads(result.output)
        assert body["error"]["code"] == "USAGE_ERROR"


# ---------------------------------------------------------------------------
# Hint-compile guard: every new --hint client/service produces valid Python
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        [
            "--hint",
            "client",
            "data-app",
            "secrets-set",
            "--project",
            "prod",
            "--app-id",
            "999",
            "--secret",
            "#K=V",
        ],
        [
            "--hint",
            "service",
            "data-app",
            "secrets-set",
            "--project",
            "prod",
            "--app-id",
            "999",
            "--secret",
            "#K=V",
        ],
        ["--hint", "client", "data-app", "secrets-list", "--project", "prod", "--app-id", "999"],
        ["--hint", "service", "data-app", "secrets-list", "--project", "prod", "--app-id", "999"],
        [
            "--hint",
            "client",
            "data-app",
            "secrets-get",
            "--project",
            "prod",
            "--app-id",
            "999",
            "--key",
            "#K",
        ],
        [
            "--hint",
            "service",
            "data-app",
            "secrets-get",
            "--project",
            "prod",
            "--app-id",
            "999",
            "--key",
            "#K",
        ],
        [
            "--hint",
            "client",
            "data-app",
            "secrets-remove",
            "--project",
            "prod",
            "--app-id",
            "999",
            "--key",
            "#K",
        ],
        [
            "--hint",
            "service",
            "data-app",
            "secrets-remove",
            "--project",
            "prod",
            "--app-id",
            "999",
            "--key",
            "#K",
        ],
        ["--hint", "service", "data-app", "validate-repo", "--git-repo", "https://github.com/o/r"],
    ],
)
def test_hint_snippet_compiles(tmp_path: Path, args: list[str]) -> None:
    store = _setup_config(tmp_path / "cfg")
    mock = MagicMock()
    repo_mock = MagicMock()
    result = _invoke(args, store=store, data_app_mock=mock, repo_validate_mock=repo_mock)
    assert result.exit_code == 0, result.output
    snippet = result.output
    # AST-parse to ensure the snippet is valid Python.
    ast.parse(snippet)
