"""Input forms of the 1.0.0 interface (spec rules A4, I1, I3, I4).

- A4: a list option is repeatable and takes one value per occurrence; a
  comma in a value of an ID or fixed-value option is ``INVALID_ARGUMENT``.
- I1: a structured value is ``--X`` inline plus ``--X-file PATH`` (``-`` is
  stdin); ``@`` never reads a file and an inline ``@path`` or ``-`` names
  ``--X-file``.
- I2/I6: a text value is ``--X`` inline plus ``--X-file PATH`` (``-`` is stdin).
- I3: a free-text content option refuses ``@<existing file>`` with a hint
  that names the file option.
- I4: a bad input value is ``INVALID_ARGUMENT`` (exit 2) that names the
  option, under ``--json`` and in human mode, never a traceback.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.commands._helpers import (
    RefuseAtFile,
    parse_json_arg,
    parse_json_file_arg,
    split_int_multi,
    split_multi,
    structured_input,
    text_input,
)
from keboola_agent_cli.commands.dev_portal import _load_payload
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode, exit_code_for
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
    def test_repeated_values_are_kept_in_order(self) -> None:
        assert split_multi(["a", " b ", "c"], label="--x") == ["a", "b", "c"]

    def test_empty_values_and_duplicates_are_dropped(self) -> None:
        assert split_multi(["a", " ", "b", "a"], label="--x") == ["a", "b"]

    def test_none_is_an_empty_list(self) -> None:
        assert split_multi(None, label="--x") == []

    def test_a_comma_is_refused_naming_the_option(self) -> None:
        with pytest.raises(typer.BadParameter, match=r"--x takes one value per occurrence"):
            split_multi(["a", "b,c"], label="--x")

    def test_integer_ids(self) -> None:
        assert split_int_multi(["11", "22", "11"], label="--ids") == [11, 22]

    def test_a_non_integer_names_the_option(self) -> None:
        with pytest.raises(ValueError, match=r"--ids takes integer IDs, got: x"):
            split_int_multi(["11", "x"], label="--ids")

    def test_a_comma_in_an_integer_id_is_refused(self) -> None:
        with pytest.raises(typer.BadParameter, match=r"Repeat --ids once per value"):
            split_int_multi(["1,2"], label="--ids")

    @pytest.mark.parametrize("digit", ["\u00b2", "\u0663"])
    def test_a_unicode_digit_is_not_an_integer_id(self, digit: str) -> None:
        with pytest.raises(ValueError, match=r"--ids takes integer IDs"):
            split_int_multi([f"1{digit}"], label="--ids")


# --------------------------------------------------------------------------
# A4: the list options take every value, one per occurrence
# --------------------------------------------------------------------------


class TestRepeatedListOptions:
    def test_sharing_share_takes_every_project_alias_or_id(self, config_dir: Path) -> None:
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
                "--target-project",
                "11",
                "--target-project",
                "prod",
                "--target-project",
                "11",
            )
        assert result.exit_code == 0, result.output
        kwargs = mock_client.share_bucket.call_args.kwargs
        assert kwargs["target_project_ids"] == [11, 1234]
        assert json.loads(result.output)["data"]["target_project_ids"] == [11, 1234]

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
                "--target-user",
                "a@example.com",
                "--target-user",
                "b@example.com",
            )
        assert result.exit_code == 0, result.output
        assert service.share.call_args.kwargs["target_users"] == ["a@example.com", "b@example.com"]

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
                    "--project-id",
                    "1",
                    "--project-id",
                    "2",
                    "--dry-run",
                ],
            )
        assert result.exit_code == 0, result.output
        assert service.setup_organization.call_args.kwargs["project_ids"] == [1, 2]

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
                "-t",
                "dataset",
            )
        assert result.exit_code == 0, result.output
        assert service.get_schema.call_args.kwargs["types"] == ["model", "dataset"]

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
                "--table-id",
                "in.c-a.t1",
                "--table-id",
                "in.c-a.t2",
                "--dry-run",
            )
        assert result.exit_code == 0, result.output
        assert service.build_model.call_args.kwargs["table_ids"] == ["in.c-a.t1", "in.c-a.t2"]


# A value with a comma in an ID or fixed-value option: INVALID_ARGUMENT naming the option.
COMMA_VALUES: list[tuple[list[str], str]] = [
    (
        [
            "sharing",
            "share",
            "--bucket-id",
            "out.c-d",
            "--type",
            "selected-projects",
            "--target-project",
            "11,22",
        ],
        "--target-project",
    ),
    (
        [
            "sharing",
            "share",
            "--bucket-id",
            "out.c-d",
            "--type",
            "selected-users",
            "--target-user",
            "a@example.com,b@example.com",
        ],
        "--target-user",
    ),
    (
        [
            "semantic-layer",
            "add",
            "constraint",
            "--name",
            "c",
            "--constraint-type",
            "range",
            "--rule",
            "x > 0",
            "--metric",
            "m1,m2",
        ],
        "--metric",
    ),
    (
        ["semantic-layer", "edit", "constraint", "--name", "c", "--new-metric", "m1,m2"],
        "--new-metric",
    ),
    (["semantic-layer", "build", "--table-id", "in.c-a.t1,in.c-a.t2", "--dry-run"], "--table-id"),
    (["semantic-layer", "import", "--file", "f.json", "--type", "metrics,datasets"], "--type"),
    (["semantic-layer", "schema", "--type", "model,dataset"], "--type"),
    (
        ["workspace", "load", "--workspace-id", "7", "--table-id", "in.c-a.t1,in.c-a.t2"],
        "--table-id",
    ),
    (["storage", "download-table", "--table-id", "in.c-a.t1", "--column", "a,b"], "--column"),
    (["storage", "unload-table", "--table-id", "in.c-a.t1", "--column", "a,b"], "--column"),
    (["token", "list", "--column", "id,description"], "--column"),
]


class TestCommaValueIsRefused:
    @pytest.mark.parametrize(("argv", "option"), COMMA_VALUES)
    def test_json_mode(self, config_dir: Path, argv: list[str], option: str) -> None:
        result = _invoke(config_dir, *argv, "--project", "prod")
        _assert_invalid_argument(result, option)
        assert f"Repeat {option} once per value" in json.loads(result.output)["error"]["message"]

    @pytest.mark.parametrize(("argv", "option"), COMMA_VALUES[:2])
    def test_human_mode(self, config_dir: Path, argv: list[str], option: str) -> None:
        result = _invoke(config_dir, *argv, "--project", "prod", json_mode=False)
        _assert_human_usage_error(result, option)

    def test_org_setup_project_id(self, config_dir: Path) -> None:
        result = _invoke(
            config_dir,
            "org",
            "setup",
            "--url",
            "https://connection.keboola.com",
            "--project-id",
            "1,2",
            "--dry-run",
        )
        _assert_invalid_argument(result, "--project-id")

    def test_semantic_layer_target_project(self, config_dir: Path) -> None:
        result = _invoke(
            config_dir,
            "semantic-layer",
            "model",
            "create",
            "--project",
            "prod",
            "--name",
            "m",
            "--scope",
            "targeted",
            "--target-project",
            "11,22",
        )
        _assert_invalid_argument(result, "--target-project")


# --------------------------------------------------------------------------
# I1: parse_json_arg
# --------------------------------------------------------------------------


class TestParseJsonArg:
    def test_inline_yaml_when_allowed(self) -> None:
        assert parse_json_arg("phases:\n  - id: 1\n", label="--definition", allow_yaml=True) == {
            "phases": [{"id": 1}]
        }

    def test_yaml_file_when_allowed(self, tmp_path: Path) -> None:
        path = tmp_path / "flow.yaml"
        path.write_text("phases:\n  - id: 1\n", encoding="utf-8")
        assert parse_json_file_arg(path, label="--definition-file", allow_yaml=True) == {
            "phases": [{"id": 1}]
        }

    def test_bad_yaml_names_the_option(self) -> None:
        with pytest.raises(ValueError, match=r"--definition: invalid YAML"):
            parse_json_arg("a: [", label="--definition", allow_yaml=True)

    def test_a_binary_file_names_the_option(self, tmp_path: Path) -> None:
        path = tmp_path / "bin.json"
        path.write_bytes(b"\xff\xfe\x00")
        with pytest.raises(ValueError, match=r"--values-file: cannot read file"):
            parse_json_file_arg(path, label="--values-file")

    @pytest.mark.parametrize("allow_yaml", [False, True])
    def test_at_path_names_the_file_option(self, tmp_path: Path, allow_yaml: bool) -> None:
        path = tmp_path / "body.json"
        path.write_text("{}", encoding="utf-8")
        with pytest.raises(ValueError, match=rf"use --body-file {re.escape(str(path))}"):
            parse_json_arg(f"@{path}", label="--body", allow_yaml=allow_yaml)

    @pytest.mark.parametrize("allow_yaml", [False, True])
    def test_dash_names_the_file_option(self, allow_yaml: bool) -> None:
        with pytest.raises(ValueError, match=r"To read stdin, use --body-file -"):
            parse_json_arg("-", label="--body", allow_yaml=allow_yaml)

    def test_file_dash_reads_stdin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO('{"a": 1}'))
        assert parse_json_file_arg("-", label="--body-file") == {"a": 1}


class TestStructuredInput:
    def test_neither_is_none(self) -> None:
        assert structured_input(None, None, label="--body") is None

    def test_inline(self) -> None:
        assert structured_input('{"a": 1}', None, label="--body") == {"a": 1}

    def test_file(self, tmp_path: Path) -> None:
        path = tmp_path / "b.json"
        path.write_text('{"a": 2}', encoding="utf-8")
        assert structured_input(None, path, label="--body") == {"a": 2}

    def test_both_are_mutually_exclusive(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match=r"--body and --body-file are mutually exclusive"):
            structured_input("{}", tmp_path / "b.json", label="--body")

    def test_missing_file_names_the_file_option(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match=r"--body-file: file not found"):
            structured_input(None, tmp_path / "none.json", label="--body")


class TestTextInput:
    def test_inline_and_file(self, tmp_path: Path) -> None:
        path = tmp_path / "d.md"
        path.write_text("hello\n", encoding="utf-8")
        assert text_input("x", None, label="--description") == "x"
        assert text_input(None, path, label="--description") == "hello\n"
        assert text_input(None, None, label="--description") is None

    def test_both_are_mutually_exclusive(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match=r"--value and --value-file are mutually exclusive"):
            text_input("x", tmp_path / "v.txt", label="--value")


# --------------------------------------------------------------------------
# I3: the @file refusal
# --------------------------------------------------------------------------


_TEXT_COMMANDS = [
    (["project", "description-set", "--project", "prod"], "--description", "--description-file"),
    (["branch", "metadata-set", "--project", "prod", "--key", "k"], "--value", "--value-file"),
    (
        ["storage", "describe-bucket", "--project", "prod", "--bucket-id", "in.c-a"],
        "--description",
        "--description-file",
    ),
    (
        ["storage", "describe-table", "--project", "prod", "--table-id", "in.c-a.t"],
        "--description",
        "--description-file",
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
            config_dir, "project", "description-set", "--project", "prod", "--description", "@team"
        )
        _assert_invalid_argument(result, "--description")
        message = json.loads(result.output)["error"]["message"]
        assert "relative to the current directory" in message
        assert (
            "To store the text '@team' itself, write it to a file and pass that file with "
            "--description-file" in message
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
        long_text = "@team " + "x" * 300  # longer than a file name may be (ENAMETOOLONG)
        assert refusal.callback(MagicMock(), param, long_text) == long_text


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
            [*share, "--type", "selected-projects", "--target-project", "abc"],
            "--target-project",
        ),
        (
            ["org", "setup", "--url", "https://connection.keboola.com", "--project-id", "x"],
            "--project-id",
        ),
        (["http", "post", "/x", "--body", "{not json"], "--body"),
        (["http", "post", "/x", "--body", f"@{tmp_path / 'missing.json'}"], "--body-file"),
        (["http", "post", "/x", "--body-file", str(tmp_path / "missing.json")], "--body-file"),
        (
            ["project", "description-set", "--project", "prod", "--description-file", binary],
            "--description-file",
        ),
        # Linux raises ENAMETOOLONG here, Windows reports "not a file": both name the option.
        (
            ["project", "description-set", "--project", "prod", "--description-file", "x" * 300],
            "--description-file",
        ),
        (
            [
                *("project", "description-set", "--project", "prod"),
                *("--description-file", str(tmp_path / "missing.md")),
            ],
            "--description-file",
        ),
        (
            ["transformation", "create", "--project", "prod", "--name", "T", "--sql-file", binary],
            "--sql-file",
        ),
        (
            [
                *("data-app", "git-credentials-create", "--project", "prod", "--app-id", "a"),
                *("--type", "ssh_key", "--permissions", "readOnly", "--public-key-file", binary),
                "--yes",
            ],
            "--public-key-file",
        ),
        (["lineage", "server", "--file", binary], "--file"),
    ]


class TestBadInputValue:
    def test_json_mode(self, config_dir: Path, tmp_path: Path) -> None:
        for args, option in _bad_input_cases(tmp_path):
            _assert_invalid_argument(_invoke(config_dir, *args), option)

    def test_human_mode(self, config_dir: Path, tmp_path: Path) -> None:
        for args, option in _bad_input_cases(tmp_path):
            _assert_human_usage_error(_invoke(config_dir, *args, json_mode=False), option)

    def test_unreadable_config_json_input_is_exit_2(self, config_dir: Path, tmp_path: Path) -> None:
        """A structured file option keeps the code of its command, now without a crash."""
        result = _invoke(
            config_dir,
            *("config", "update", "--project", "prod", "--component-id", "c"),
            *("--config-id", "1", "--configuration-file", str(_binary_file(tmp_path))),
        )
        assert result.exit_code == 2, result.output
        error = json.loads(result.output)["error"]
        assert error["code"] == ErrorCode.VALIDATION_ERROR
        assert "--configuration-file: cannot read file" in error["message"]

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
        assert "--configuration must be a JSON object" in error["message"]

    def test_lineage_missing_file_keeps_file_not_found(
        self, config_dir: Path, tmp_path: Path
    ) -> None:
        result = _invoke(config_dir, "lineage", "server", "--file", str(tmp_path / "none.json"))
        assert json.loads(result.output)["error"]["code"] == ErrorCode.FILE_NOT_FOUND

    def test_binary_files_that_keep_their_code_do_not_crash(
        self, config_dir: Path, tmp_path: Path
    ) -> None:
        """--description-file, --secret-file and --members-file keep their code and its exit code."""
        binary = str(_binary_file(tmp_path))
        cases = [
            [
                *("data-app", "create", "--project", "prod", "--name", "n", "--slug", "s"),
                *("--git-repo", "https://example.com/r.git", "--description-file", binary),
            ],
            [
                *("data-app", "secrets-set", "--project", "prod", "--app-id", "a"),
                *("--secret-file", binary),
            ],
            [
                *("semantic-layer", "reference-data", "set", "--project", "prod"),
                *("--dimension", "d", "--members-file", binary),
            ],
        ]
        for args in cases:
            result = _invoke(config_dir, *args)
            error = json.loads(result.output)["error"]
            assert isinstance(result.exception, SystemExit), (args, result.output)
            assert result.exit_code == exit_code_for(error["code"]), (args, result.output)

    def test_dev_portal_data(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        for data_file in (bad, tmp_path / "missing.json"):
            formatter = MagicMock()
            with pytest.raises(typer.Exit) as exc_info:
                _load_payload(formatter, None, data_file)
            assert exc_info.value.exit_code == 2
            kwargs = formatter.error.call_args.kwargs
            assert kwargs["error_code"] == ErrorCode.INVALID_ARGUMENT
            assert kwargs["message"].startswith("--data-file:")

    def test_dev_portal_data_path_hint(self, tmp_path: Path) -> None:
        """0.x took a path in --data: a path that is a file gets a hint for --data-file."""
        payload = tmp_path / "app.json"
        payload.write_text("{}", encoding="utf-8")
        formatter = MagicMock()
        with pytest.raises(typer.Exit):
            _load_payload(formatter, str(payload), None)
        assert f"use --data-file {payload}" in formatter.error.call_args.kwargs["message"]

    def test_dev_portal_inline_data(self) -> None:
        assert _load_payload(MagicMock(), '{"a": 1}', None) == {"a": 1}


# --------------------------------------------------------------------------
# I1, I2, I6: --X inline plus --X-file PATH, where - is stdin
# --------------------------------------------------------------------------

_CONFIG = ("--project", "prod", "--component-id", "c", "--config-id", "1")
_OBJECT = {"k": 1}

# (service patch target, method, argv without the value, file option, inline option,
#  keyword the service gets, file content, the value the service gets)
_FILE_OPTION_CASES: list[tuple[str, str, list[str], str, str, str, str, Any]] = [
    (
        "cli.ConfigService",
        "update_config",
        ["config", "update", *_CONFIG],
        "--configuration-file",
        "--configuration",
        "configuration",
        json.dumps(_OBJECT),
        _OBJECT,
    ),
    (
        "cli.ConfigService",
        "create_config_row",
        ["config", "row-create", *_CONFIG, "--name", "r"],
        "--configuration-file",
        "--configuration",
        "configuration",
        json.dumps(_OBJECT),
        _OBJECT,
    ),
    (
        "cli.ConfigService",
        "update_config_row",
        ["config", "row-update", *_CONFIG, "--row-id", "2"],
        "--configuration-file",
        "--configuration",
        "configuration",
        json.dumps(_OBJECT),
        _OBJECT,
    ),
    (
        "cli.ConfigService",
        "set_config_state",
        ["config", "state-set", *_CONFIG, "--yes"],
        "--state-file",
        "--state",
        "state",
        json.dumps(_OBJECT),
        _OBJECT,
    ),
    (
        "cli.ComponentService",
        "run_sync_action",
        ["component", "sync-action", "testConnection", "--project", "prod", "--component-id", "c"],
        "--config-data-file",
        "--config-data",
        "config_data_override",
        json.dumps(_OBJECT),
        _OBJECT,
    ),
    (
        "commands.http_client._resolve_service",
        "request",
        ["http", "post", "/x"],
        "--body-file",
        "--body",
        "payload",
        json.dumps(_OBJECT),
        _OBJECT,
    ),
    (
        "commands.transformation.TransformationService",
        "edit",
        [
            *("transformation", "edit", "--project", "prod", "--config-id", "1"),
            *("--change-description", "d"),
        ],
        "--storage-file",
        "--storage",
        "storage",
        json.dumps(_OBJECT),
        _OBJECT,
    ),
    (
        "cli.MergeRequestService",
        "resolve_conflict",
        [
            *("merge-request", "resolve", "--project", "prod", "--merge-request-id", "1"),
            *("--component-id", "c", "--config-id", "1"),
        ],
        "--resolved-file",
        "--resolved",
        "resolved",
        json.dumps(_OBJECT),
        _OBJECT,
    ),
    (
        "cli.FlowService",
        "create_flow",
        ["flow", "new", "--project", "prod", "--name", "F"],
        "--definition-file",
        "--definition",
        "phases",
        "phases:\n  - id: '1'\n    name: P\ntasks: []\n",
        [{"id": "1", "name": "P"}],
    ),
    (
        "cli.EncryptService",
        "encrypt",
        ["encrypt", "values", "--project", "prod", "--component-id", "c"],
        "--values-file",
        "--values",
        "input_data",
        json.dumps({"#k": "v"}),
        {"#k": "v"},
    ),
    (
        "cli.SemanticLayerService",
        "set_reference_data",
        ["semantic-layer", "reference-data", "set", "--project", "prod", "--dimension", "d"],
        "--members-file",
        "--members",
        "members",
        json.dumps([{"id": "a"}]),
        [{"id": "a"}],
    ),
    (
        "cli.DataAppService",
        "set_data_app_secrets",
        ["data-app", "secrets-set", "--project", "prod", "--app-id", "a"],
        "--secret-file",
        "--secret",
        "secrets",
        json.dumps({"#k": "v"}),
        {"#k": "v"},
    ),
    (
        "cli.StorageService",
        "describe_bucket",
        ["storage", "describe-bucket", "--project", "prod", "--bucket-id", "in.c-a"],
        "--description-file",
        "--description",
        "description",
        "Sales data\n",
        "Sales data\n",
    ),
    (
        "cli.StorageService",
        "describe_table",
        ["storage", "describe-table", "--project", "prod", "--table-id", "in.c-a.t"],
        "--description-file",
        "--description",
        "description",
        "Sales data\n",
        "Sales data\n",
    ),
    (
        "cli.BranchService",
        "set_project_description",
        ["project", "description-set", "--project", "prod"],
        "--description-file",
        "--description",
        "description",
        "# Project\n",
        "# Project\n",
    ),
    (
        "cli.BranchService",
        "set_branch_metadata",
        ["branch", "metadata-set", "--project", "prod", "--key", "k"],
        "--value-file",
        "--value",
        "value",
        "v1\n",
        "v1\n",
    ),
    (
        "cli.WorkspaceService",
        "execute_query",
        ["workspace", "query", "--project", "prod", "--workspace-id", "7"],
        "--sql-file",
        "--sql",
        "sql",
        "SELECT 1;\n",
        "SELECT 1;\n",
    ),
]


# `http` checks these before it calls its (patched) service.
_SERVE_ENV = {"KBAGENT_SERVE_URL": "http://127.0.0.1:9", "KBAGENT_SERVE_TOKEN": "t"}


def _patched_service(target: str, method: str) -> Any:
    service = MagicMock()
    getattr(service, method).return_value = {"status": "ok"}
    return patch(f"keboola_agent_cli.{target}", return_value=service), service


def _service_call(
    config_dir: Path, target: str, method: str, args: list[str], stdin: str | None = None
) -> tuple[Any, Any]:
    patcher, service = _patched_service(target, method)
    with patcher:
        result = runner.invoke(
            app, ["--json", "--config-dir", str(config_dir), *args], input=stdin, env=_SERVE_ENV
        )
    return result, getattr(service, method)


def _case_id(case: tuple[Any, ...]) -> str:
    return f"{case[2][0]} {case[2][1]} {case[3]}"


_CASE_IDS = [_case_id(case) for case in _FILE_OPTION_CASES]
# The JSON/YAML options; --secret takes '#KEY=VALUE', not JSON.
_STRUCTURED_CASES = [
    case
    for case in _FILE_OPTION_CASES
    if case[6].startswith(("{", "[", "phases")) and case[4] != "--secret"
]
_STRUCTURED_IDS = [_case_id(case) for case in _STRUCTURED_CASES]
_EXCLUSIVE_CASES = [case for case in _FILE_OPTION_CASES if case[4] != "--secret"]
_EXCLUSIVE_IDS = [_case_id(case) for case in _EXCLUSIVE_CASES]
_PARAMS = ("target", "method", "argv", "file_option", "inline", "keyword", "content", "expected")


class TestFileOptions:
    @pytest.mark.parametrize(
        _PARAMS,
        _FILE_OPTION_CASES,
        ids=_CASE_IDS,
    )
    def test_file_option_reads_the_file(
        self,
        config_dir: Path,
        tmp_path: Path,
        target: str,
        method: str,
        argv: list[str],
        file_option: str,
        inline: str,
        keyword: str,
        content: str,
        expected: Any,
    ) -> None:
        path = tmp_path / "input.txt"
        path.write_text(content, encoding="utf-8")
        result, call = _service_call(config_dir, target, method, [*argv, file_option, str(path)])
        assert call.called, result.output
        assert call.call_args.kwargs[keyword] == expected

    @pytest.mark.parametrize(
        _PARAMS,
        _FILE_OPTION_CASES,
        ids=_CASE_IDS,
    )
    def test_dash_reads_stdin(
        self,
        config_dir: Path,
        target: str,
        method: str,
        argv: list[str],
        file_option: str,
        inline: str,
        keyword: str,
        content: str,
        expected: Any,
    ) -> None:
        result, call = _service_call(
            config_dir, target, method, [*argv, file_option, "-"], stdin=content
        )
        assert call.called, result.output
        assert call.call_args.kwargs[keyword] == expected

    @pytest.mark.parametrize(
        _PARAMS,
        _EXCLUSIVE_CASES,
        ids=_EXCLUSIVE_IDS,
    )
    def test_inline_and_file_are_mutually_exclusive(
        self,
        config_dir: Path,
        tmp_path: Path,
        target: str,
        method: str,
        argv: list[str],
        file_option: str,
        inline: str,
        keyword: str,
        content: str,
        expected: Any,
    ) -> None:
        path = tmp_path / "input.txt"
        path.write_text(content, encoding="utf-8")
        inline_value = content if content.startswith(("{", "[")) else "x"
        result, call = _service_call(
            config_dir, target, method, [*argv, inline, inline_value, file_option, str(path)]
        )
        assert result.exit_code == 2, result.output
        assert not call.called
        message = json.loads(result.output)["error"]["message"]
        assert inline in message
        assert file_option in message

    @pytest.mark.parametrize(
        _PARAMS,
        _STRUCTURED_CASES,
        ids=_STRUCTURED_IDS,
    )
    def test_structured_inline_value(
        self,
        config_dir: Path,
        target: str,
        method: str,
        argv: list[str],
        file_option: str,
        inline: str,
        keyword: str,
        content: str,
        expected: Any,
    ) -> None:
        result, call = _service_call(config_dir, target, method, [*argv, inline, content])
        assert call.called, result.output
        assert call.call_args.kwargs[keyword] == expected

    @pytest.mark.parametrize("value", ["@body.json", "-"])
    @pytest.mark.parametrize(
        _PARAMS,
        _STRUCTURED_CASES,
        ids=_STRUCTURED_IDS,
    )
    def test_at_or_dash_inline_names_the_file_option(
        self,
        config_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        value: str,
        target: str,
        method: str,
        argv: list[str],
        file_option: str,
        inline: str,
        keyword: str,
        content: str,
        expected: Any,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        (tmp_path / "body.json").write_text(content, encoding="utf-8")
        result, call = _service_call(config_dir, target, method, [*argv, inline, value])
        assert result.exit_code == 2, result.output
        assert not call.called
        message = json.loads(result.output)["error"]["message"]
        assert f"use {file_option} {value.lstrip('@')}" in message


class TestAgentDefinition:
    def test_definition_file_and_runtime_input_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """agent create --definition-file and agent run --runtime-input-file (agents.json is local)."""
        definition = tmp_path / "action.json"
        definition.write_text(
            json.dumps({"type": "cli_command", "params": {"argv": ["version"]}}), encoding="utf-8"
        )
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        created = runner.invoke(
            app,
            [
                *("--json", "--config-dir", str(config_dir), "agent", "create", "--name", "t"),
                *("--manual", "--definition-file", str(definition)),
            ],
        )
        assert created.exit_code == 0, created.output
        task = json.loads(created.output)["data"]
        assert task["action"]["params"]["argv"] == ["version"]

        with patch("keboola_agent_cli.cli.AgentService") as service_cls:
            service = service_cls.return_value
            service.run_task = AsyncMock(side_effect=RuntimeError("stop"))
            runner.invoke(
                app,
                [
                    *("--json", "--config-dir", str(config_dir), "agent", "run"),
                    *("--task-id", task["id"], "--runtime-input-file", "-"),
                ],
                input='{"prompt": "hi"}',
            )
        assert service.run_task.call_args.kwargs["runtime_input"] == {"prompt": "hi"}

    def test_definition_inline_at_names_the_file_option(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                *("--json", "--config-dir", str(tmp_path), "agent", "test"),
                *("--definition", "@action.json"),
            ],
        )
        assert result.exit_code == 2, result.output
        assert "use --definition-file action.json" in json.loads(result.output)["error"]["message"]
