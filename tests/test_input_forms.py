"""Input forms of the 1.0.0 interface (spec rules A4, I1, I3, I4).

- A4: the nine list options are repeatable and keep the comma list.
- I1: one parser for ``JSON|@file|-`` values (``parse_json_arg``).
- I3: a free-text content option refuses ``@<existing file>`` with a hint
  that names the file option.
- I4: a bad input value is ``INVALID_ARGUMENT`` (exit 2) that names the
  option, under ``--json`` and in human mode, never a traceback.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.commands._helpers import (
    RefuseAtFile,
    parse_json_arg,
    split_int_multi,
    split_multi,
)
from keboola_agent_cli.commands.dev_portal import _load_payload
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.sharing_service import SharingService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_network(httpx_mock: object) -> None:
    """pytest-httpx fails any HTTP request: these tests run without a Keboola stack."""


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """A config directory with one registered project, alias ``prod``."""
    directory = tmp_path / "config"
    directory.mkdir()
    ConfigStore(config_dir=directory).add_project(
        "prod",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
            project_name="Prod",
            project_id=1234,
        ),
    )
    return directory


def _invoke(config_dir: Path, *args: str, json_mode: bool = True) -> Any:
    root = ["--json"] if json_mode else []
    return runner.invoke(app, [*root, "--config-dir", str(config_dir), *args])


def _assert_invalid_argument(result: Any, option: str) -> None:
    """Under --json: one error document with INVALID_ARGUMENT that names the option."""
    assert result.exit_code == 2, result.output
    error = json.loads(result.output)["error"]
    assert error["code"] == ErrorCode.INVALID_ARGUMENT
    assert option in error["message"]


def _assert_human_usage_error(result: Any, option: str) -> None:
    """In human mode: exit 2, the option in the message, no traceback."""
    assert result.exit_code == 2, result.output
    assert isinstance(result.exception, SystemExit)
    assert option in result.output
    assert "Traceback" not in result.output


# --------------------------------------------------------------------------
# A4: split_multi
# --------------------------------------------------------------------------


class TestSplitMulti:
    def test_repeated_and_comma_values_are_flattened_in_order(self) -> None:
        assert split_multi(["a", "b, c", "d"]) == ["a", "b", "c", "d"]

    def test_empty_parts_and_duplicates_are_dropped(self) -> None:
        assert split_multi(["a,,b", " ", "a"]) == ["a", "b"]

    def test_none_is_an_empty_list(self) -> None:
        assert split_multi(None) == []

    def test_integer_ids(self) -> None:
        assert split_int_multi(["11", "22,33"], label="--ids") == [11, 22, 33]

    def test_a_non_integer_names_the_option(self) -> None:
        with pytest.raises(ValueError, match=r"--ids takes integer IDs, got: x"):
            split_int_multi(["11,x"], label="--ids")

    @pytest.mark.parametrize("digit", ["\u00b2", "\u0663"])
    def test_a_unicode_digit_is_not_an_integer_id(self, digit: str) -> None:
        with pytest.raises(ValueError, match=r"--ids takes integer IDs"):
            split_int_multi([f"1,{digit}"], label="--ids")


# --------------------------------------------------------------------------
# A4: the nine options take every value
# --------------------------------------------------------------------------


class TestRepeatedListOptions:
    def test_sharing_share_takes_every_project_id(self, config_dir: Path) -> None:
        mock_client = MagicMock()
        mock_client.share_bucket.return_value = {"status": "success"}
        store = ConfigStore(config_dir=config_dir)
        with (
            patch("keboola_agent_cli.cli.SharingService") as service_cls,
            patch.object(SharingService, "resolve_master_token", return_value=TEST_TOKEN),
        ):
            service_cls.return_value = SharingService(
                config_store=store, client_factory=lambda url, token: mock_client
            )
            result = _invoke(
                config_dir,
                "sharing",
                "share",
                "--project",
                "prod",
                "--bucket-id",
                "out.c-data",
                "--type",
                "selected-projects",
                "--target-project-ids",
                "11",
                "--target-project-ids",
                "22,33",
            )
        assert result.exit_code == 0, result.output
        kwargs = mock_client.share_bucket.call_args.kwargs
        assert kwargs["target_project_ids"] == [11, 22, 33]
        assert json.loads(result.output)["data"]["target_project_ids"] == [11, 22, 33]

    def test_sharing_share_takes_every_user(self, config_dir: Path) -> None:
        with patch("keboola_agent_cli.cli.SharingService") as service_cls:
            service = service_cls.return_value
            service.share.return_value = {"message": "ok"}
            result = _invoke(
                config_dir,
                "sharing",
                "share",
                "--project",
                "prod",
                "--bucket-id",
                "out.c-data",
                "--type",
                "selected-users",
                "--target-users",
                "a@example.com",
                "--target-users",
                "b@example.com,c@example.com",
            )
        assert result.exit_code == 0, result.output
        assert service.share.call_args.kwargs["target_users"] == [
            "a@example.com",
            "b@example.com",
            "c@example.com",
        ]

    def test_org_setup_takes_every_project_id(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KBC_MANAGE_API_TOKEN", "manage-token")
        with patch("keboola_agent_cli.cli.OrgService") as service_cls:
            service = service_cls.return_value
            service.setup_organization.return_value = {"projects_added": []}
            result = runner.invoke(
                app,
                [
                    "--json",
                    "--allow-env-manage-token",
                    "--config-dir",
                    str(config_dir),
                    "org",
                    "setup",
                    "--url",
                    "https://connection.keboola.com",
                    "--project-ids",
                    "1,2",
                    "--project-ids",
                    "3",
                    "--dry-run",
                ],
            )
        assert result.exit_code == 0, result.output
        assert service.setup_organization.call_args.kwargs["project_ids"] == [1, 2, 3]

    def test_semantic_layer_schema_takes_every_type(self, config_dir: Path) -> None:
        with patch("keboola_agent_cli.cli.SemanticLayerService") as service_cls:
            service = service_cls.return_value
            service.get_schema.return_value = {"schemas": {}}
            result = _invoke(
                config_dir,
                "semantic-layer",
                "schema",
                "--project",
                "prod",
                "--type",
                "model",
                "--type",
                "dataset,metric",
            )
        assert result.exit_code == 0, result.output
        assert service.get_schema.call_args.kwargs["types"] == ["model", "dataset", "metric"]

    def test_semantic_layer_build_takes_every_table(self, config_dir: Path) -> None:
        with patch("keboola_agent_cli.cli.SemanticLayerService") as service_cls:
            service = service_cls.return_value
            service.build_model.return_value = {"fetch_errors": []}
            result = _invoke(
                config_dir,
                "semantic-layer",
                "build",
                "--project",
                "prod",
                "--tables",
                "in.c-a.t1",
                "--tables",
                "in.c-a.t2,in.c-a.t3",
                "--dry-run",
            )
        assert result.exit_code == 0, result.output
        assert service.build_model.call_args.kwargs["table_ids"] == [
            "in.c-a.t1",
            "in.c-a.t2",
            "in.c-a.t3",
        ]


# --------------------------------------------------------------------------
# I1: parse_json_arg
# --------------------------------------------------------------------------


class TestParseJsonArg:
    def test_yaml_when_allowed(self, tmp_path: Path) -> None:
        path = tmp_path / "flow.yaml"
        path.write_text("phases:\n  - id: 1\n", encoding="utf-8")
        assert parse_json_arg(f"@{path}", label="--file", allow_yaml=True) == {
            "phases": [{"id": 1}]
        }

    def test_bad_yaml_names_the_option(self) -> None:
        with pytest.raises(ValueError, match=r"--file: invalid YAML"):
            parse_json_arg("a: [", label="--file", allow_yaml=True)

    def test_a_binary_file_names_the_option(self, tmp_path: Path) -> None:
        path = tmp_path / "bin.json"
        path.write_bytes(b"\xff\xfe\x00")
        with pytest.raises(ValueError, match=r"--input: cannot read input"):
            parse_json_arg(f"@{path}", label="--input")


# --------------------------------------------------------------------------
# I3: the @file refusal
# --------------------------------------------------------------------------


_TEXT_COMMANDS = [
    (["project", "description-set", "--project", "prod"], "--text", "--file"),
    (["branch", "metadata-set", "--project", "prod", "--key", "k"], "--text", "--file"),
    (
        ["storage", "describe-bucket", "--project", "prod", "--bucket-id", "in.c-a"],
        "--text",
        "--file",
    ),
    (
        ["storage", "describe-table", "--project", "prod", "--table-id", "in.c-a.t"],
        "--text",
        "--file",
    ),
    (
        ["transformation", "create", "--project", "prod", "--name", "T"],
        "--sql",
        "--sql-file",
    ),
]


class TestRefuseAtFile:
    @pytest.mark.parametrize(("command", "option", "file_option"), _TEXT_COMMANDS)
    def test_json_mode_refuses_with_a_hint(
        self, config_dir: Path, tmp_path: Path, command: list[str], option: str, file_option: str
    ) -> None:
        notes = tmp_path / "notes.md"
        notes.write_text("# notes\n", encoding="utf-8")
        result = _invoke(config_dir, *command, option, f"@{notes}")
        _assert_invalid_argument(result, option)
        assert f"Use {file_option} {notes}" in json.loads(result.output)["error"]["message"]

    def test_hint_gives_a_way_to_store_text_that_starts_with_at(
        self, config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A relative @name is checked in the current directory; the hint names the file option."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "team").write_text("x\n", encoding="utf-8")
        result = _invoke(
            config_dir, "project", "description-set", "--project", "prod", "--text", "@team"
        )
        _assert_invalid_argument(result, "--text")
        message = json.loads(result.output)["error"]["message"]
        assert "relative to the current directory" in message
        assert (
            "To store the text '@team' itself, write it to a file and pass that file with --file"
            in message
        )

    @pytest.mark.parametrize(("command", "option", "file_option"), _TEXT_COMMANDS)
    def test_human_mode_refuses_with_a_hint(
        self, config_dir: Path, tmp_path: Path, command: list[str], option: str, file_option: str
    ) -> None:
        notes = tmp_path / "notes.md"
        notes.write_text("# notes\n", encoding="utf-8")
        result = _invoke(config_dir, *command, option, f"@{notes}", json_mode=False)
        _assert_human_usage_error(result, file_option)

    def test_at_without_a_file_stays_text(self, tmp_path: Path) -> None:
        refusal = RefuseAtFile("--file")
        param = MagicMock(opts=["--text"])
        missing = f"@{tmp_path / 'missing.md'}"
        assert refusal.callback(MagicMock(), param, missing) == missing
        assert refusal.callback(MagicMock(), param, "@team") == "@team"
        assert refusal.callback(MagicMock(), param, None) is None


# --------------------------------------------------------------------------
# I4: a bad value is INVALID_ARGUMENT naming the option
# --------------------------------------------------------------------------


def _binary_file(tmp_path: Path) -> Path:
    path = tmp_path / "binary.bin"
    path.write_bytes(b"\xff\xfe\x00\x81")
    return path


def _bad_input_cases(tmp_path: Path) -> list[tuple[list[str], str]]:
    """Bad integer list, bad JSON, missing @file, unreadable file: the args and the option."""
    binary = str(_binary_file(tmp_path))
    share = ["sharing", "share", "--project", "prod", "--bucket-id", "out.c-data"]
    return [
        (
            [*share, "--type", "selected-projects", "--target-project-ids", "11,abc"],
            "--target-project-ids",
        ),
        (
            ["org", "setup", "--url", "https://connection.keboola.com", "--project-ids", "1,x"],
            "--project-ids",
        ),
        (["http", "post", "/x", "--body", "{not json"], "--body"),
        (["http", "post", "/x", "--body", f"@{tmp_path / 'missing.json'}"], "--body"),
        (["project", "description-set", "--project", "prod", "--file", binary], "--file"),
        (
            ["transformation", "create", "--project", "prod", "--name", "T", "--sql-file", binary],
            "--sql-file",
        ),
        (
            [
                *("config", "update", "--project", "prod", "--component-id", "c"),
                *("--config-id", "1", "--configuration-file", binary),
            ],
            "--configuration-file",
        ),
        (
            [
                *("data-app", "git-credentials-create", "--project", "prod", "--app-id", "a"),
                *("--type", "ssh_key", "--permissions", "readOnly", "--public-key-file", binary),
                "--yes",
            ],
            "--public-key-file",
        ),
        (["lineage", "server", "--load", binary], "--load"),
    ]


class TestBadInputValue:
    def test_json_mode(self, config_dir: Path, tmp_path: Path) -> None:
        for args, option in _bad_input_cases(tmp_path):
            _assert_invalid_argument(_invoke(config_dir, *args), option)

    def test_human_mode(self, config_dir: Path, tmp_path: Path) -> None:
        for args, option in _bad_input_cases(tmp_path):
            _assert_human_usage_error(_invoke(config_dir, *args, json_mode=False), option)

    def test_unreadable_config_json_input_is_exit_2(self, config_dir: Path, tmp_path: Path) -> None:
        """A parser copy that moved to parse_json_arg keeps its error code, now without a crash."""
        result = _invoke(
            config_dir,
            *("config", "update", "--project", "prod", "--component-id", "c"),
            *("--config-id", "1", "--configuration", f"@{_binary_file(tmp_path)}"),
        )
        assert result.exit_code == 2, result.output
        error = json.loads(result.output)["error"]
        assert error["code"] == ErrorCode.VALIDATION_ERROR
        assert "--configuration: cannot read input" in error["message"]

    def test_configuration_file_must_hold_an_object(self, config_dir: Path, tmp_path: Path) -> None:
        body = tmp_path / "list.json"
        body.write_text("[1]", encoding="utf-8")
        result = _invoke(
            config_dir,
            *("config", "update", "--project", "prod", "--component-id", "c"),
            *("--config-id", "1", "--configuration-file", str(body)),
        )
        assert result.exit_code == 2, result.output
        error = json.loads(result.output)["error"]
        assert error["code"] == ErrorCode.VALIDATION_ERROR
        assert "--configuration-file must contain a JSON object" in error["message"]

    def test_lineage_missing_load_keeps_file_not_found(
        self, config_dir: Path, tmp_path: Path
    ) -> None:
        result = _invoke(config_dir, "lineage", "server", "--load", str(tmp_path / "none.json"))
        assert json.loads(result.output)["error"]["code"] == ErrorCode.FILE_NOT_FOUND

    def test_binary_files_that_keep_their_code_do_not_crash(
        self, config_dir: Path, tmp_path: Path
    ) -> None:
        """--description-file, --secrets-file and --members-file keep their code, exit 2."""
        binary = str(_binary_file(tmp_path))
        cases = [
            [
                *("data-app", "create", "--project", "prod", "--name", "n", "--slug", "s"),
                *("--git-repo", "https://example.com/r.git", "--description-file", binary),
            ],
            [
                *("data-app", "secrets-set", "--project", "prod", "--app-id", "a"),
                *("--secrets-file", binary),
            ],
            [
                *("semantic-layer", "reference-data", "set", "--project", "prod"),
                *("--dimension", "d", "--members-file", binary),
            ],
        ]
        for args in cases:
            result = _invoke(config_dir, *args)
            assert result.exit_code == 2, (args, result.output)
            assert json.loads(result.output)["status"] == "error"

    def test_dev_portal_data(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        for data in (str(bad), str(tmp_path / "missing.json")):
            formatter = MagicMock()
            with pytest.raises(typer.Exit) as exc_info:
                _load_payload(formatter, data)
            assert exc_info.value.exit_code == 2
            kwargs = formatter.error.call_args.kwargs
            assert kwargs["error_code"] == ErrorCode.INVALID_ARGUMENT
            assert kwargs["message"].startswith("--data:")
