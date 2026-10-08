"""CLI-layer tests for ``semantic-layer scope`` and the ``--scope``/
``--target-project`` flags on ``model create`` / ``add <kind>`` (PSGO-140).

Mirrors the test_semantic_layer_cli.py pattern: patch cli.py's service
factory so the runner sees a MagicMock SemanticLayerService, plus a REAL
ProjectService (built from the test ConfigStore) since ``resolve_scope_targets``
reads project aliases through it for the interactive-picker fallback.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.constants import EXIT_PERMISSION_DENIED
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.config_service import ConfigService
from keboola_agent_cli.services.job_service import JobService
from keboola_agent_cli.services.project_service import ProjectService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

runner = CliRunner()


def _setup_config(config_dir: Path, projects: dict[str, dict]) -> ConfigStore:
    store = ConfigStore(config_dir=config_dir)
    for alias, info in projects.items():
        store.add_project(
            alias,
            ProjectConfig(
                stack_url=info.get("stack_url", "https://connection.keboola.com"),
                token=info["token"],
                project_name=info.get("project_name", alias),
                project_id=info.get("project_id", 1234),
            ),
        )
    return store


def _invoke(args: list[str], *, store: ConfigStore, sl_mock: MagicMock):
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.ProjectService") as MockProj,
        patch("keboola_agent_cli.cli.ConfigService") as MockCfg,
        patch("keboola_agent_cli.cli.JobService") as MockJob,
        patch("keboola_agent_cli.cli.SemanticLayerService") as MockSL,
    ):
        MockStore.return_value = store
        MockProj.return_value = ProjectService(config_store=store)
        MockCfg.return_value = ConfigService(config_store=store)
        MockJob.return_value = JobService(config_store=store)
        MockSL.return_value = sl_mock
        return runner.invoke(app, args)


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    d = tmp_path / "config"
    d.mkdir()
    return d


@pytest.fixture
def store(cfg_dir: Path) -> ConfigStore:
    return _setup_config(
        cfg_dir,
        {
            "prod": {"token": TEST_TOKEN, "project_id": 5725},
            "analytics": {"token": TEST_TOKEN, "project_id": 1234},
        },
    )


def _sl(*args: str) -> list[str]:
    return ["--json", "semantic-layer", *args]


ITEM = ["--project", "prod", "--type", "dataset", "--context-id", "d1"]


# ---------------------------------------------------------------------------
# --scope / --target-project on model create / add <kind>
# ---------------------------------------------------------------------------


class TestCreateScopeFlags:
    def test_omitted_scope_is_passed_as_none_so_the_service_inherits(
        self, store: ConfigStore
    ) -> None:
        mock = MagicMock()
        mock.add_metric.return_value = {"id": "m", "attributes": {"name": "n"}}
        result = _invoke(
            _sl(
                *("add", "metric", "--project", "prod", "--name", "n", "--sql", "1"),
                *("--dataset", "t", "--yes"),
            ),
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        kwargs = mock.add_metric.call_args.kwargs
        assert kwargs["scope"] is None
        assert kwargs["target_projects"] is None

    def test_model_create_with_targeted_scope_passes_aliases_through(
        self, store: ConfigStore
    ) -> None:
        mock = MagicMock()
        mock.create_model.return_value = {"project": "prod", "model": {"id": "u", "attributes": {}}}
        result = _invoke(
            [
                *_sl(
                    *("model", "create", "--project", "prod", "--name", "n", "--scope", "targeted")
                ),
                "--target-project",
                "analytics,5678",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        kwargs = mock.create_model.call_args.kwargs
        assert kwargs["scope"] == "targeted"
        assert kwargs["target_projects"] == ["analytics,5678"]

    def test_targeted_scope_without_target_fails_fast_non_tty(self, store: ConfigStore) -> None:
        mock = MagicMock()
        result = _invoke(
            _sl(*("model", "create", "--project", "prod", "--name", "n", "--scope", "targeted")),
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 2, result.output
        mock.create_model.assert_not_called()
        assert json.loads(result.output)["error"]["code"] == "INVALID_ARGUMENT"

    @pytest.mark.parametrize(
        "scope_args", [[], ["--scope", "project"], ["--scope", "organization"]]
    )
    def test_target_project_without_targeted_scope_is_a_usage_error(
        self, store: ConfigStore, scope_args: list[str]
    ) -> None:
        mock = MagicMock()
        result = _invoke(
            [
                *_sl("model", "create", "--project", "prod", "--name", "n", *scope_args),
                "--target-project",
                "analytics",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 2, result.output
        mock.create_model.assert_not_called()
        assert json.loads(result.output)["error"]["code"] == "INVALID_ARGUMENT"

    def test_unknown_target_alias_is_exit_2_not_a_traceback(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.create_model.side_effect = KeboolaApiError(
            message="--target-project 'ghost' is neither a registered project alias nor an ID",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
        result = _invoke(
            [
                *_sl(
                    *("model", "create", "--project", "prod", "--name", "n", "--scope", "targeted")
                ),
                "--target-project",
                "ghost",
            ],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 2, result.output
        assert "--target-project" in json.loads(result.output)["error"]["message"]

    def test_bad_scope_value_is_a_usage_error(self, store: ConfigStore) -> None:
        result = _invoke(
            _sl("model", "create", "--project", "prod", "--name", "n", "--scope", "everyone"),
            store=store,
            sl_mock=MagicMock(),
        )
        assert result.exit_code == 2

    @pytest.mark.parametrize(
        ("flags", "blocked"),
        [
            (["--deny-destructive"], True),
            (["--deny-writes"], True),  # any create is a write
        ],
    )
    def test_organization_scope_create_is_gated_per_command(
        self, store: ConfigStore, flags: list[str], blocked: bool
    ) -> None:
        for command in (
            ["model", "create", "--name", "n"],
            ["add", "glossary", "--term", "t", "--definition", "d"],
            ["add", "dataset", "--name", "n", "--table-id", "a.b.c"],
        ):
            mock = MagicMock()
            result = _invoke(
                [*flags, *_sl(*command, "--project", "prod", "--scope", "organization")],
                store=store,
                sl_mock=mock,
            )
            assert (result.exit_code == EXIT_PERMISSION_DENIED) is blocked, result.output

    @pytest.mark.parametrize(
        ("model_scope", "blocked"),
        [("organization", True), ("targeted", False), ("project", False)],
    )
    def test_inherited_scope_is_gated_like_a_typed_one(
        self, store: ConfigStore, model_scope: str, blocked: bool
    ) -> None:
        """`add` without --scope takes the model's scope; an inherited organization scope must not
        bypass --deny-destructive."""
        mock = MagicMock()
        mock.child_scope.return_value = model_scope
        mock.add_glossary.return_value = {"id": "g", "attributes": {"term": "t"}}
        result = _invoke(
            [
                "--deny-destructive",
                *_sl("add", "glossary", "--project", "prod", "--term", "t", "--definition", "d"),
            ],
            store=store,
            sl_mock=mock,
        )
        assert (result.exit_code == EXIT_PERMISSION_DENIED) is blocked, result.output
        assert mock.add_glossary.called is (not blocked)

    def test_no_model_lookup_when_no_permission_policy_is_active(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.add_glossary.return_value = {"id": "g", "attributes": {"term": "t"}}
        result = _invoke(
            _sl("add", "glossary", "--project", "prod", "--term", "t", "--definition", "d"),
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        mock.child_scope.assert_not_called()

    def test_deny_destructive_still_allows_project_scope_create(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.create_model.return_value = {"project": "prod", "model": {"id": "u", "attributes": {}}}
        result = _invoke(
            ["--deny-destructive", *_sl("model", "create", "--project", "prod", "--name", "n")],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# semantic-layer scope <verb>
# ---------------------------------------------------------------------------


class TestScopeVerbs:
    @pytest.mark.parametrize(
        ("argv", "method", "expected"),
        [
            (["get"], "scope_get", {}),
            (
                ["add", "--target-project", "analytics", "--target-project", "5678"],
                "scope_update_targets",
                {"add": ["analytics", "5678"]},
            ),
            (
                ["remove", "--target-project", "analytics,5678"],
                "scope_update_targets",
                {"remove": ["analytics,5678"]},
            ),
            (["request-create"], "scope_request_create", {}),
            (["request-delete"], "scope_request_delete", {}),
        ],
    )
    def test_verb_calls_the_service(
        self, store: ConfigStore, argv: list[str], method: str, expected: dict
    ) -> None:
        mock = MagicMock()
        getattr(mock, method).return_value = {"scope": "targeted", "target_project_ids": []}
        result = _invoke(_sl("scope", *argv[:1], *ITEM, *argv[1:]), store=store, sl_mock=mock)
        assert result.exit_code == 0, result.output
        kwargs = getattr(mock, method).call_args.kwargs
        assert kwargs["alias"] == "prod"
        assert kwargs["kind"] == "dataset"
        assert kwargs["context_id"] == "d1"
        assert {k: kwargs[k] for k in expected} == expected

    @pytest.mark.parametrize("verb", ["add", "remove"])
    def test_add_and_remove_require_a_target(self, store: ConfigStore, verb: str) -> None:
        mock = MagicMock()
        result = _invoke(_sl("scope", verb, *ITEM), store=store, sl_mock=mock)
        assert result.exit_code == 2, result.output
        mock.scope_update_targets.assert_not_called()

    def test_empty_target_list_is_printed_not_hidden(self, store: ConfigStore) -> None:
        """A targeted item with no grants ([] = owner only) must still show the field."""
        mock = MagicMock()
        mock.scope_get.return_value = {"scope": "targeted", "target_project_ids": []}
        with patch("keboola_agent_cli.commands._semantic_layer_scope.get_formatter") as gf:
            fmt = MagicMock(json_mode=False)
            gf.return_value = fmt
            _invoke(["semantic-layer", "scope", "get", *ITEM], store=store, sl_mock=mock)
            human = fmt.output.call_args.args[1]
        console = MagicMock()
        human(console, {"scope": "targeted", "target_project_ids": []})
        assert any("target_project_ids" in str(c) for c in console.print.call_args_list)

    def test_bad_type_is_a_usage_error(self, store: ConfigStore) -> None:
        result = _invoke(
            _sl("scope", "get", "--project", "prod", "--type", "table", "--context-id", "x"),
            store=store,
            sl_mock=MagicMock(),
        )
        assert result.exit_code == 2

    def test_service_usage_error_exits_2(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.scope_set.side_effect = KeboolaApiError(
            message="scope set takes exactly one of ...", error_code=ErrorCode.INVALID_ARGUMENT
        )
        result = _invoke(
            _sl("scope", "set", *ITEM, "--clear", "--target-project", "analytics"),
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 2, result.output


class TestScopeSet:
    @pytest.mark.parametrize(
        ("extra", "expected"),
        [
            (["--target-project", "analytics,5678"], {"target_projects": ["analytics,5678"]}),
            (["--clear"], {"clear": True}),
            (["--scope", "organization", "--yes"], {"scope": "organization"}),
            (["--scope", "organization", "--dry-run"], {"scope": "organization", "dry_run": True}),
        ],
    )
    def test_modes(self, store: ConfigStore, extra: list[str], expected: dict) -> None:
        mock = MagicMock()
        mock.scope_set.return_value = {"scope": "organization"}
        result = _invoke(_sl("scope", "set", *ITEM, *extra), store=store, sl_mock=mock)
        assert result.exit_code == 0, result.output
        kwargs = mock.scope_set.call_args.kwargs
        assert {k: kwargs[k] for k in expected} == expected

    def test_bad_scope_value_is_a_usage_error(self, store: ConfigStore) -> None:
        result = _invoke(
            _sl("scope", "set", *ITEM, "--scope", "project"), store=store, sl_mock=MagicMock()
        )
        assert result.exit_code == 2

    def test_human_mode_declining_the_prompt_aborts_without_calling(
        self, store: ConfigStore
    ) -> None:
        mock = MagicMock()
        with patch("typer.confirm", return_value=False):
            result = _invoke(
                ["semantic-layer", "scope", "set", *ITEM, "--scope", "organization"],
                store=store,
                sl_mock=mock,
            )
        assert result.exit_code == 0
        mock.scope_set.assert_not_called()


class TestScopeRequestList:
    def test_defaults_and_pagination_envelope(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.scope_request_list.return_value = {
            "items": [{"id": "d1", "name": "x"}],
            "limit": 50,
            "offset": 0,
            "has_more": False,
        }
        result = _invoke(
            _sl("scope", "request-list", "--project", "prod", "--type", "dataset"),
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        kwargs = mock.scope_request_list.call_args.kwargs
        assert (kwargs["limit"], kwargs["offset"]) == (50, 0)
        data = json.loads(result.output)["data"]
        assert {"items", "limit", "offset", "has_more"} <= set(data)


# ---------------------------------------------------------------------------
# Permission gating
# ---------------------------------------------------------------------------

WRITE_VERBS = [
    ["add", "--target-project", "analytics"],
    ["remove", "--target-project", "analytics"],
    ["set", "--clear"],
    ["request-create"],
    ["request-delete"],
]
READ_VERBS = [["get", *ITEM], ["request-list", "--project", "prod", "--type", "dataset"]]


class TestScopePermissions:
    @pytest.mark.parametrize("verb", WRITE_VERBS, ids=lambda v: v[0])
    def test_deny_writes_blocks_write_verbs(self, store: ConfigStore, verb: list[str]) -> None:
        mock = MagicMock()
        result = _invoke(
            ["--deny-writes", *_sl("scope", verb[0], *ITEM, *verb[1:])], store=store, sl_mock=mock
        )
        assert result.exit_code == EXIT_PERMISSION_DENIED, result.output

    @pytest.mark.parametrize("verb", READ_VERBS, ids=lambda v: v[0])
    def test_deny_writes_allows_read_verbs(self, store: ConfigStore, verb: list[str]) -> None:
        mock = MagicMock()
        result = _invoke(["--deny-writes", *_sl("scope", *verb)], store=store, sl_mock=mock)
        assert result.exit_code == 0, result.output

    @pytest.mark.parametrize(
        ("extra", "blocked"),
        [
            (["--scope", "organization", "--yes"], True),
            (["--scope", "organization", "--dry-run"], True),  # same gate as `sync push --force`
            (["--clear"], False),
            (["--target-project", "analytics"], False),
        ],
    )
    def test_deny_destructive_blocks_only_the_elevation(
        self, store: ConfigStore, extra: list[str], blocked: bool
    ) -> None:
        mock = MagicMock()
        mock.scope_set.return_value = {"scope": "organization"}
        result = _invoke(
            ["--deny-destructive", *_sl("scope", "set", *ITEM, *extra)], store=store, sl_mock=mock
        )
        assert (result.exit_code == EXIT_PERMISSION_DENIED) is blocked, result.output
        assert mock.scope_set.called is (not blocked)


_COPY_COMMANDS = {
    "import": ("import", ["--project", "prod", "--file", "{snapshot}"], "import_snapshot"),
    "promote": (
        "promote",
        ["--from-project", "analytics", "--to-project", "prod", "--yes"],
        "promote_model",
    ),
    "build": (
        "build",
        ["--project", "prod", "--model", "m", "--tables", "a.b.c"],
        "build_model",
    ),
}


class TestCopyCommandsGateInheritedOrganizationScope:
    """`import`, `promote` and `build --model` create items at the target model's scope, so an
    organization-scope target model must not bypass --deny-destructive."""

    @pytest.mark.parametrize("command", ["import", "promote", "build"])
    @pytest.mark.parametrize(
        ("model_scope", "blocked"),
        [("organization", True), ("targeted", False), ("project", False)],
    )
    def test_inherited_scope_is_gated(
        self, store: ConfigStore, tmp_path: Path, command: str, model_scope: str, blocked: bool
    ) -> None:
        name, args, service_method = _COPY_COMMANDS[command]
        snapshot = tmp_path / "snapshot.json"
        snapshot.write_text("{}", encoding="utf-8")
        mock = MagicMock()
        mock.child_scope.return_value = model_scope
        getattr(mock, service_method).return_value = {}
        result = _invoke(
            ["--deny-destructive", *_sl(name, *[a.format(snapshot=snapshot) for a in args])],
            store=store,
            sl_mock=mock,
        )
        assert (result.exit_code == EXIT_PERMISSION_DENIED) is blocked, result.output
        assert getattr(mock, service_method).called is (not blocked)

    def test_promote_checks_the_target_model(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.child_scope.return_value = "project"
        mock.promote_model.return_value = {}
        _invoke(
            [
                "--deny-destructive",
                *_sl(
                    "promote",
                    "--from-project",
                    "analytics",
                    "--to-project",
                    "prod",
                    "--to-model",
                    "tgt",
                    "--yes",
                ),
            ],
            store=store,
            sl_mock=mock,
        )
        mock.child_scope.assert_called_once_with(alias="prod", model_name_or_uuid="tgt")

    def test_build_of_a_new_model_needs_no_lookup(self, store: ConfigStore) -> None:
        mock = MagicMock()
        mock.build_model.return_value = {}
        result = _invoke(
            ["--deny-destructive", *_sl("build", "--project", "prod", "--tables", "a.b.c")],
            store=store,
            sl_mock=mock,
        )
        assert result.exit_code == 0, result.output
        mock.child_scope.assert_not_called()

    def test_no_model_lookup_without_a_permission_policy(
        self, store: ConfigStore, tmp_path: Path
    ) -> None:
        snapshot = tmp_path / "snapshot.json"
        snapshot.write_text("{}", encoding="utf-8")
        mock = MagicMock()
        mock.import_snapshot.return_value = {}
        result = _invoke(
            _sl("import", "--project", "prod", "--file", str(snapshot)), store=store, sl_mock=mock
        )
        assert result.exit_code == 0, result.output
        mock.child_scope.assert_not_called()
