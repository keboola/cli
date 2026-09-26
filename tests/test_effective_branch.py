"""Which branch a command uses, and how the CLI reports it (issue #766)."""

import ast
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli import cli
from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.effective_branch import (
    BranchTarget,
    record_branch,
    record_targets,
    recorded_targets,
    resolve_branch,
)
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.output import OutputFormatter, format_target_line, success_json
from keboola_agent_cli.services.sync_service import SyncService
from keboola_agent_cli.services.workspace_service import WorkspaceService
from keboola_agent_cli.sync.manifest import Manifest, ManifestBranch, ManifestProject

SRC = Path(__file__).parent.parent / "src" / "keboola_agent_cli"
TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
runner = CliRunner()

# Reads of `active_branch_id` outside effective_branch.py that do not choose the
# branch of an API call: they only show the active branch or reset it.
READS_THAT_DO_NOT_CHOOSE_A_BRANCH = {
    "services/branch_service.py::list_branches",
    "services/branch_service.py::reset_branch",
    "services/branch_service.py::delete_branch",
    "services/merge_request_service.py::merge",
    "services/project_service.py::list_projects",
    "services/project_service.py::_check_project_status",
}


def _project(active_id: int | None = None, active_name: str | None = None) -> ProjectConfig:
    return ProjectConfig(
        stack_url="https://connection.keboola.com",
        token=TOKEN,
        project_id=1,
        active_branch_id=active_id,
        active_branch_name=active_name,
    )


def _store(tmp_path: Path, **projects: ProjectConfig) -> ConfigStore:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    store = ConfigStore(config_dir=config_dir)
    for alias, project in projects.items():
        store.add_project(alias, project)
    return store


def _target(**changes: object) -> BranchTarget:
    values: dict = {
        "role": "target",
        "project_alias": "prod",
        "branch_id": None,
        "branch_name": None,
        "branch_source": "production",
        "active_id": None,
        "active_name": None,
    }
    values.update(changes)
    return BranchTarget(**values)


def _active_branch_reads(root: Path) -> set[str]:
    """``<file>::<function>`` for every attribute read of ``active_branch_id``."""
    found: set[str] = set()
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if rel == "effective_branch.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Attribute)
                and node.attr == "active_branch_id"
                and isinstance(node.ctx, ast.Load)
            ):
                continue
            names = []
            scope = parents.get(node)
            while scope is not None:
                if isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
                    names.append(scope.name)
                scope = parents.get(scope)
            found.add(f"{rel}::{'.'.join(reversed(names))}")
    return found


@pytest.fixture
def store(tmp_path: Path) -> ConfigStore:
    """`prod` and `p0`..`p7` have active branch 456; `dev`, `a`, `b`, `z` have none."""
    fan_out = {f"p{i}": _project(456, "feature-x") for i in range(8)}
    none = {alias: _project() for alias in ("dev", "a", "b", "z")}
    return _store(tmp_path, prod=_project(456, "feature-x"), **fan_out, **none)


@pytest.fixture
def printed() -> Iterator[list[BranchTarget]]:
    """Open a record the way the CLI root callback does; collect what it prints."""
    lines: list[BranchTarget] = []
    with record_targets(lines.append):
        yield lines


class TestResolveBranch:
    def test_explicit_branch_wins_over_the_active_branch(self, store, printed) -> None:
        assert resolve_branch(store, "prod", 789) == 789
        assert recorded_targets()[0].branch_source == "explicit"

    def test_active_branch_applies_with_its_saved_name(self, store, printed) -> None:
        assert resolve_branch(store, "prod", None) == 456
        target = recorded_targets()[0]
        assert (target.branch_source, target.branch_name) == ("active_branch", "feature-x")

    def test_ignore_active_branch_uses_production_and_keeps_the_active_branch(
        self, store, printed
    ) -> None:
        assert resolve_branch(store, "prod", None, ignore_active_branch=True) is None
        target = recorded_targets()[0]
        assert (target.branch_source, target.active_id) == ("production", 456)

    def test_branch_zero_is_production(self, store, printed) -> None:
        # The API clients always sent 0 to the production endpoint.
        assert resolve_branch(store, "prod", 0) == 0
        # A service that resolves the command's result again keeps production.
        assert resolve_branch(store, "prod", 0) == 0
        assert [t.branch_source for t in recorded_targets()] == ["production"]
        # It is no explicit target for a command that needs a branch.
        assert resolve_branch(store, "dev", 0, required=True) is None
        assert len(recorded_targets()) == 1

    def test_manifest_branch_comes_after_the_active_branch(self, store, printed) -> None:
        assert resolve_branch(store, "prod", None, manifest_branch_id=388) == 456
        assert resolve_branch(store, "dev", None, manifest_branch_id=388) == 388
        assert [t.branch_source for t in recorded_targets()] == ["manifest", "active_branch"]

    def test_a_required_branch_that_is_missing_records_nothing(self, store, printed) -> None:
        # The command refuses to run; it has no target.
        assert resolve_branch(store, "dev", None, required=True) is None
        assert recorded_targets() == []

    def test_unknown_project_records_nothing(self, store, printed) -> None:
        # The command fails on the alias; it has no target.
        assert resolve_branch(store, "gone", None) is None
        assert recorded_targets() == []

    def test_without_a_command_record_it_only_returns_the_id(self, store) -> None:
        # `kbagent serve` and the SDK open no record.
        assert resolve_branch(store, "prod", None) == 456
        assert recorded_targets() == []


class TestRecord:
    def test_a_service_resolving_the_id_again_adds_nothing(self, store, printed) -> None:
        branch = resolve_branch(store, "prod", None)  # command layer
        resolve_branch(store, "prod", branch)  # the service gets it as --branch
        assert [t.branch_source for t in recorded_targets()] == ["active_branch"]
        assert len(printed) == 1

    def test_the_looked_up_production_id_replaces_the_unknown_one(self, store, printed) -> None:
        resolve_branch(store, "dev", None)
        record_branch(store, "dev", 3001, "production")
        assert [(t.branch_id, t.branch_source) for t in recorded_targets()] == [
            (3001, "production")
        ]
        assert len(printed) == 1

    def test_a_second_branch_of_one_project_is_a_second_target(self, store, printed) -> None:
        resolve_branch(store, "prod", None, ignore_active_branch=True)
        resolve_branch(store, "prod", None)
        assert [t.branch_id for t in recorded_targets()] == [None, 456]

    def test_sources_come_first_then_aliases_in_order(self, store, printed) -> None:
        resolve_branch(store, "b", None)
        resolve_branch(store, "a", None)
        resolve_branch(store, "z", None, role="source")
        assert [(t.role, t.project_alias) for t in recorded_targets()] == [
            ("source", "z"),
            ("target", "a"),
            ("target", "b"),
        ]

    def test_fan_out_workers_add_to_the_command_record(self, store, printed) -> None:
        aliases = [f"p{i}" for i in range(8)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda alias: resolve_branch(store, alias, None), aliases))
        assert [t.project_alias for t in recorded_targets()] == aliases

    def test_the_record_closes_with_the_command(self, store) -> None:
        with record_targets(lambda target: None):
            resolve_branch(store, "dev", None)
        assert recorded_targets() == []


class TestResolvers:
    def test_workspace_resolver_records_the_active_branch(self, tmp_path, printed) -> None:
        project = _project(456)
        service = WorkspaceService(config_store=_store(tmp_path, prod=project))
        assert service._resolve_branch_id("prod", project) == 456
        assert recorded_targets()[0].branch_source == "active_branch"

    def test_workspace_resolver_records_the_default_branch_id_as_production(
        self, tmp_path, printed
    ) -> None:
        project = _project()
        client = MagicMock()
        client.list_dev_branches.return_value = [{"id": 3001, "isDefault": True}]
        service = WorkspaceService(
            config_store=_store(tmp_path, prod=project), client_factory=lambda url, token: client
        )
        assert service._resolve_branch_id("prod", project) == 3001
        assert [(t.branch_id, t.branch_source) for t in recorded_targets()] == [
            (3001, "production")
        ]

    def test_sync_manifest_fallback_is_one_manifest_record(self, store, tmp_path, printed) -> None:
        # `sync clone --branch 388` writes 388 as the first manifest branch.
        manifest = Manifest.model_construct(
            project=ManifestProject(id=1, apiHost="connection.keboola.com"),
            branches=[ManifestBranch(id=388, path="main", metadata={})],
        )
        service = SyncService(config_store=store)
        assert service._resolve_branch_id("dev", manifest, tmp_path) == 388
        assert [(t.branch_id, t.branch_source) for t in printed] == [(388, "manifest")]


class TestOutput:
    @pytest.mark.parametrize(
        ("changes", "line"),
        [
            ({}, "Target: project 'prod', production"),
            ({"branch_id": 3001}, "Target: project 'prod', production branch 3001"),
            (
                {
                    "branch_id": 456,
                    "branch_name": "feature-x",
                    "branch_source": "active_branch",
                    "active_id": 456,
                    "active_name": "feature-x",
                },
                "Target: project 'prod', branch 456 'feature-x' (from 'kbagent branch use')",
            ),
            (
                {"branch_id": 789, "branch_source": "explicit", "active_id": 456},
                "Target: project 'prod', branch 789 (from the command line)",
            ),
            (
                {"branch_id": 388, "branch_source": "manifest"},
                "Target: project 'prod', branch 388 (from .keboola/manifest.json)",
            ),
            (
                {"active_id": 456, "active_name": "feature-x"},
                (
                    "Target: project 'prod', production "
                    "(active branch 456 'feature-x' not used; pass --branch 456 to use it)"
                ),
            ),
            (
                {"branch_id": 388, "branch_source": "git_mapping"},
                "Target: project 'prod', branch 388 (from .keboola/branch-mapping.json)",
            ),
            ({"role": "source"}, "Source: project 'prod', production"),
            (
                {"role": "source", "branch_id": 999, "branch_source": "merge_request"},
                "Source: project 'prod', branch 999 (from the merge request)",
            ),
        ],
    )
    def test_target_line(self, changes, line) -> None:
        assert format_target_line(_target(**changes)) == line

    def test_fixed_production_gives_no_active_branch_hint(self, store, printed) -> None:
        record_branch(store, "prod", None, "production", fixed=True)
        assert format_target_line(recorded_targets()[0]) == "Target: project 'prod', production"

    def test_success_envelope_without_a_target_is_unchanged(self) -> None:
        assert json.loads(success_json({"value": None})) == {
            "status": "ok",
            "data": {"value": None},
        }

    def test_success_envelope_lists_the_targets(self, store, printed) -> None:
        resolve_branch(store, "prod", None)
        assert json.loads(success_json({}))["targets"] == [
            {
                "role": "target",
                "project_alias": "prod",
                "branch_id": 456,
                "branch_name": "feature-x",
                "branch_source": "active_branch",
                "active_branch": {"branch_id": 456, "branch_name": "feature-x"},
            }
        ]

    def test_error_envelope_lists_the_targets(self, store, printed, capsys) -> None:
        resolve_branch(store, "prod", None)
        OutputFormatter(json_mode=True).error("boom", error_code="API_ERROR")
        body = json.loads(capsys.readouterr().out)
        assert body["targets"][0]["branch_id"] == 456
        assert body["error"]["message"] == "boom"


class TestCommands:
    """The CLI root callback opens the record; the formatter reports it."""

    def _workspace_detail(self, tmp_path: Path, *args: str):
        store = _store(tmp_path, prod=_project(456, "feature-x"))
        client = MagicMock()
        client.get_workspace.return_value = {"id": 7, "connection": {}}
        service = WorkspaceService(config_store=store, client_factory=lambda url, token: client)
        with (
            patch("keboola_agent_cli.cli.ConfigStore", return_value=store),
            patch("keboola_agent_cli.cli.WorkspaceService", return_value=service),
        ):
            result = runner.invoke(
                app, [*args, "workspace", "detail", "--project", "prod", "--workspace-id", "7"]
            )
        assert result.exit_code == 0, result.output
        assert client.get_workspace.call_args.kwargs["branch_id"] == 456
        return result

    def test_json_reports_the_active_branch_a_service_applied(self, tmp_path) -> None:
        # Issue #766: the workspace service applied the active branch silently.
        body = json.loads(self._workspace_detail(tmp_path, "--json").output)
        assert body["targets"][0]["branch_source"] == "active_branch"

    def test_human_output_prints_the_target_line(self, tmp_path) -> None:
        result = self._workspace_detail(tmp_path)
        expected = "Target: project 'prod', branch 456 'feature-x' (from 'kbagent branch use')"
        assert expected in result.output

    def test_dry_run_reports_the_branch_the_real_run_uses(self, tmp_path) -> None:
        store = _store(tmp_path, prod=_project(456))
        with patch("keboola_agent_cli.cli.ConfigStore", return_value=store):
            result = runner.invoke(
                app,
                ["--json", "flow", "delete", "--project", "prod", "--flow-id", "9", "--dry-run"],
            )
        body = json.loads(result.output)
        assert body["data"]["would_delete"]["branch_id"] == 456
        assert body["targets"][0]["branch_id"] == 456

    def test_the_target_line_comes_before_the_confirmation(self, tmp_path) -> None:
        store = _store(tmp_path, prod=_project(456))
        with patch("keboola_agent_cli.cli.ConfigStore", return_value=store):
            result = runner.invoke(
                app, ["flow", "delete", "--project", "prod", "--flow-id", "9"], input="n\n"
            )
        assert result.output.index("Target: project 'prod', branch 456") < result.output.index(
            "Delete flow"
        )

    def test_merge_names_its_source_branch_and_production_before_the_prompt(self, tmp_path) -> None:
        store = _store(tmp_path, prod=_project(456, "feature-x"))
        service = MagicMock()
        service.find_merge_request_for_branch.return_value = {"id": 7, "title": "t"}
        with (
            patch("keboola_agent_cli.cli.ConfigStore", return_value=store),
            patch("keboola_agent_cli.cli.MergeRequestService", return_value=service),
        ):
            result = runner.invoke(
                app, ["merge-request", "merge", "--project", "prod"], input="n\n"
            )
        source = result.output.index("Source: project 'prod', branch 456 'feature-x'")
        target = result.output.index("Target: project 'prod', production\n")
        assert source < target < result.output.index("will be merged into production")
        service.merge.assert_not_called()

    def test_a_command_without_a_branch_has_no_targets_key(self, tmp_path) -> None:
        store = _store(tmp_path, prod=_project(456))
        with patch("keboola_agent_cli.cli.ConfigStore", return_value=store):
            result = runner.invoke(app, ["--json", "project", "list"])
        assert result.exit_code == 0, result.output
        assert "targets" not in json.loads(result.output)

    @pytest.mark.parametrize(
        ("argv", "opened"), [(["serve", "--help"], False), (["config", "list", "--help"], True)]
    )
    def test_serve_opens_no_record(self, argv, opened) -> None:
        # uvicorn serves each request in a thread that inherits the caller's
        # state, so a record opened for `serve` would collect every request.
        with patch.object(cli, "record_targets", wraps=record_targets) as spy:
            runner.invoke(app, argv)
        assert spy.called is opened


class TestOnlyThisModuleAppliesTheActiveBranch:
    def test_no_new_read_of_active_branch_id(self) -> None:
        unexpected = _active_branch_reads(SRC) - READS_THAT_DO_NOT_CHOOSE_A_BRANCH
        assert not unexpected, (
            f"{sorted(unexpected)} read ProjectConfig.active_branch_id. Call resolve_branch() "
            "from effective_branch.py, which reports the branch. A read that only shows or "
            "manages the active branch goes in READS_THAT_DO_NOT_CHOOSE_A_BRANCH."
        )

    def test_every_listed_function_still_reads_it(self) -> None:
        assert _active_branch_reads(SRC) >= READS_THAT_DO_NOT_CHOOSE_A_BRANCH
