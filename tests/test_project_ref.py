"""A project ID works wherever a registered alias is expected (CLI-22).

Covers the resolver rules (``project_ref.resolve_project_ref``), the CLI entry
point that applies them to every ``--project`` option before a command runs
(``commands/_project_ref.py``), ``project use``, and ``KBAGENT_PROJECT``. The
REST API is covered in ``test_server_project_ref.py``.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import typer
from typer.core import TyperGroup
from typer.testing import CliRunner

from keboola_agent_cli.auth.sentinel import make_session_token
from keboola_agent_cli.cli import app
from keboola_agent_cli.commands._project_ref import (
    ALIAS_ARGUMENTS,
    ALIAS_OPTIONS,
    NO_LOOKUP_COMMANDS,
)
from keboola_agent_cli.config_store import PROJECT_ID_HINT, ConfigStore
from keboola_agent_cli.constants import ENV_KBAGENT_PROJECT
from keboola_agent_cli.errors import ConfigError
from keboola_agent_cli.models import AppConfig, ProjectConfig
from keboola_agent_cli.project_ref import alias_shadow_notice, resolve_project_ref
from keboola_agent_cli.services.project_service import ProjectService

US = "https://connection.keboola.com"
EU = "https://connection.eu-central-1.keboola.com"
TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"
PROJECT_ID = 4242

runner = CliRunner()


def _project(project_id: int | None, *, stack: str = US, token: str = TOKEN) -> ProjectConfig:
    return ProjectConfig(stack_url=stack, token=token, project_id=project_id)


def _write_config(config_dir: Path, projects: dict[str, ProjectConfig]) -> ConfigStore:
    store = ConfigStore(config_dir=config_dir)
    store.save(AppConfig(projects=projects))
    return store


class TestResolveProjectRef:
    """The resolution rules, on a plain alias -> ProjectConfig mapping."""

    def test_project_id_resolves_to_its_alias(self) -> None:
        projects = {"prod": _project(PROJECT_ID), "dev": _project(1)}
        assert resolve_project_ref(projects, str(PROJECT_ID)) == "prod"

    def test_alias_wins_over_another_projects_id(self) -> None:
        # Alias "4242" is project 5555; project 4242 is registered as "prod".
        projects = {"4242": _project(5555), "prod": _project(PROJECT_ID)}
        assert resolve_project_ref(projects, "4242") == "4242"

    def test_alias_is_returned_unchanged(self) -> None:
        assert resolve_project_ref({"prod": _project(PROJECT_ID)}, "prod") == "prod"

    def test_no_match_returns_value_unchanged(self) -> None:
        assert resolve_project_ref({"prod": _project(PROJECT_ID)}, "999") == "999"

    def test_non_numeric_value_is_not_looked_up_as_id(self) -> None:
        assert resolve_project_ref({"prod": _project(PROJECT_ID)}, "4242x") == "4242x"

    def test_project_without_stored_id_is_not_matched(self) -> None:
        projects = {"legacy": _project(None)}
        assert resolve_project_ref(projects, str(PROJECT_ID)) == str(PROJECT_ID)

    def test_two_static_aliases_on_one_stack_are_ambiguous(self) -> None:
        projects = {"prod": _project(PROJECT_ID), "prod-ro": _project(PROJECT_ID)}
        with pytest.raises(ConfigError) as exc_info:
            resolve_project_ref(projects, str(PROJECT_ID))
        assert f"'prod' ({US})" in exc_info.value.message
        assert f"'prod-ro' ({US})" in exc_info.value.message

    def test_same_id_on_two_stacks_is_ambiguous(self) -> None:
        projects = {"us": _project(PROJECT_ID), "eu": _project(PROJECT_ID, stack=EU)}
        with pytest.raises(ConfigError, match=r"'eu' \(https://connection\.eu-central-1"):
            resolve_project_ref(projects, str(PROJECT_ID))

    def test_session_alias_wins_over_static_alias_on_one_stack(self) -> None:
        projects = {
            "prod": _project(PROJECT_ID),
            "prod-session": _project(PROJECT_ID, token=make_session_token(PROJECT_ID)),
        }
        assert resolve_project_ref(projects, str(PROJECT_ID)) == "prod-session"

    def test_session_and_static_plus_another_stack_is_ambiguous(self) -> None:
        projects = {
            "prod": _project(PROJECT_ID),
            "prod-session": _project(PROJECT_ID, token=make_session_token(PROJECT_ID)),
            "eu": _project(PROJECT_ID, stack=EU),
        }
        with pytest.raises(ConfigError) as exc_info:
            resolve_project_ref(projects, str(PROJECT_ID))
        for alias in ("prod", "prod-session", "eu"):
            assert f"'{alias}'" in exc_info.value.message

    def test_two_session_aliases_on_one_stack_are_ambiguous(self) -> None:
        session = make_session_token(PROJECT_ID)
        projects = {
            "a": _project(PROJECT_ID, token=session),
            "b": _project(PROJECT_ID, token=session),
        }
        with pytest.raises(ConfigError, match="matches more than one registered project"):
            resolve_project_ref(projects, str(PROJECT_ID))

    def test_overlong_digit_string_is_not_an_id(self) -> None:
        # int() refuses more than 4300 digits; 19+ digits is no project ID anyway.
        projects = {"prod": _project(PROJECT_ID)}
        for ref in ("1" * 19, "9" * 5000):
            assert resolve_project_ref(projects, ref) == ref

    def test_shadow_notice_names_the_project_the_id_would_pick(self) -> None:
        projects = {"4242": _project(5555), "prod": _project(PROJECT_ID)}
        notice = alias_shadow_notice(projects, "4242")
        assert notice == (
            "'4242' is an alias of project 5555; project ID 4242 is registered as 'prod'. "
            "Pass 'prod' to use that project."
        )

    def test_no_shadow_notice_for_aliases_of_the_same_project(self) -> None:
        projects = {"4242": _project(PROJECT_ID), "prod": _project(PROJECT_ID)}
        assert alias_shadow_notice(projects, "4242") is None
        assert alias_shadow_notice({"prod": _project(PROJECT_ID)}, "prod") is None


# --- Every --project option, proven by invoking the real command tree -------------------

# Commands whose --project is not a registry lookup (a new alias, an offline
# graph filter), so an ID must reach them unchanged.
EXPECTED_EXCLUDED = {("project", "add"), ("project", "create"), ("lineage", "show")}

# Options whose flag says project / alias / stack but that never name an
# existing registered alias, so they are rightly NOT translated.
NOT_AN_ALIAS: dict[str, str] = {
    "--alias": "auth register-projects: ID=ALIAS names a NEW alias; dev-portal: an identity",
    "--new-alias": "names the NEW alias (project edit) or identity (dev-portal identity edit)",
    "alias": "dev-portal identity use: a developer-portal identity, not a project",
    "--project-id": "already a project ID (auth register-projects)",
    "--project-ids": "already project IDs (org setup)",
    "--source-project-id": "already a project ID (sharing link)",
    "--target-project-ids": "already project IDs (sharing share)",
    "--target-project": (
        "semantic-layer: alias OR numeric ID resolved by the service itself (a target need not "
        "be registered); config clone's --target-project is in ALIAS_OPTIONS; "
        "rls/cls create|update: a numeric project ID the policy is granted to, not an alias"
    ),
    "--all-projects": "a boolean flag",
    "--clear-target-projects": "a boolean flag (rls/cls update): revoke every grant, takes no project",
    "--register-projects": "a boolean flag",
    "--remove-projects": "a boolean flag",
}


def _commands_with_project_option(
    group: Any, path: tuple[str, ...] = ()
) -> list[tuple[tuple[str, ...], Any]]:
    """Every leaf command that has a --project option, found by walking list_commands()."""
    found = []
    for name in group.list_commands(typer.Context(group)):
        command = group.get_command(typer.Context(group), name)
        command_path = (*path, name)
        if hasattr(command, "list_commands"):
            found += _commands_with_project_option(command, command_path)
        elif any("--project" in param.opts for param in command.params):
            found.append((command_path, command))
    return found


def _leaf_commands(group: Any, path: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    """Every leaf command, by path, found through ``group.commands`` (not list_commands)."""
    found: dict[tuple[str, ...], Any] = {}
    for name, command in group.commands.items():
        if hasattr(command, "commands"):
            found.update(_leaf_commands(command, (*path, name)))
        else:
            found[(*path, name)] = command
    return found


def _dummy_value(param: Any, existing_file: Path) -> str:
    """A value Click accepts for ``param`` (the command body never runs)."""
    type_name = type(param.type).__name__
    if type_name in ("TyperChoice", "Choice"):
        return str(param.type.choices[0])
    if type_name == "TyperPath":
        return str(existing_file)
    if type_name == "IntParamType":
        return "1"
    return "x"


def _argv_for(command: Any, given: Any, flag: str, existing_file: Path) -> list[str]:
    """``flag PROJECT_ID`` for the ``given`` parameter, dummies for the other required ones."""
    options: list[str] = []
    arguments: list[str] = []
    for param in command.params:
        if param is given:
            value = str(PROJECT_ID)
        elif param.required:
            value = _dummy_value(param, existing_file)
        else:
            continue
        if param.param_type_name == "argument":
            arguments.append(value)
        else:
            options += [flag if param is given else param.opts[0], value]
    return options + arguments


def _command_at(root: Any, path: tuple[str, ...]) -> Any:
    """The command at ``path`` in the tree; fails when it no longer exists."""
    node = root
    for name in path:
        node = node.get_command(typer.Context(node), name)
        assert node is not None, f"{' '.join(path)} no longer exists"
    return node


def _invoke_recording(root: Any, config_dir: Path, path: tuple[str, ...], argv: list[str]) -> Any:
    """Run ``path`` with the body replaced by a recorder; return the recorded params."""
    received: dict[str, Any] = {}
    _command_at(root, path).callback = received.update
    args = ["--config-dir", str(config_dir), *path, *argv]
    exit_code = root.main(args=args, prog_name="kbagent", standalone_mode=False)
    assert exit_code in (None, 0), f"{' '.join(path)} exited {exit_code}"
    return received


def _first(value: Any) -> Any:
    return value[0] if isinstance(value, tuple | list) else value


class TestEveryProjectOptionIsTranslated:
    """Invoke every command that has --project with a project ID, and record what it gets.

    The command bodies are replaced by a recorder on the real Click tree, so the
    root callback, the group callbacks and Click's own parsing run exactly as in
    production. A new command with --project is picked up without editing this test.
    """

    def test_every_command_receives_the_alias(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        _write_config(config_dir, {"prod": _project(PROJECT_ID), "other": _project(1)})
        existing_file = tmp_path / "input.txt"
        existing_file.write_text("x")

        root = typer.main.get_command(app)
        assert isinstance(root, TyperGroup)
        commands = _commands_with_project_option(root)
        # A walk that stops early would test only part of the tree: compare it
        # with a second, independent walk over `group.commands`.
        independent = {
            path
            for path, command in _leaf_commands(root).items()
            if any("--project" in param.opts for param in command.params)
        }
        assert {path for path, _ in commands} == independent

        for path, command in commands:
            param = next(p for p in command.params if "--project" in p.opts)
            argv = _argv_for(command, param, "--project", existing_file)
            received = _invoke_recording(root, config_dir, path, argv)
            expected = str(PROJECT_ID) if path in EXPECTED_EXCLUDED else "prod"
            got = _first(received[param.name])
            assert got == expected, f"{' '.join(path)} received {received[param.name]!r}"

    def test_every_table_entry_receives_the_alias(self, tmp_path: Path) -> None:
        """ALIAS_OPTIONS and ALIAS_ARGUMENTS: each entry must exist and get the alias."""
        config_dir = tmp_path / "config"
        _write_config(config_dir, {"prod": _project(PROJECT_ID), "other": _project(1)})
        existing_file = tmp_path / "input.txt"
        existing_file.write_text("x")
        entries = [(path, flag) for path, flags in ALIAS_OPTIONS.items() for flag in sorted(flags)]
        entries += list(ALIAS_ARGUMENTS.items())
        root = typer.main.get_command(app)

        for path, flag in entries:
            command = _command_at(root, path)
            param = next((p for p in command.params if flag in p.opts), None)
            assert param is not None, f"{' '.join(path)} has no {flag} any more"
            argv = _argv_for(command, param, flag, existing_file)
            received = _invoke_recording(root, config_dir, path, argv)
            got = _first(received[param.name])
            assert got == "prod", f"{' '.join(path)} {flag} received {received[param.name]!r}"

    def test_every_project_alias_or_stack_option_is_decided(self) -> None:
        """A parameter whose flag says project / alias / stack is translated or allowlisted.

        A new option such as `--source-project` fails here until it is added to
        ALIAS_OPTIONS (it names an existing alias) or to NOT_AN_ALIAS (it does not).
        """
        root = typer.main.get_command(app)
        undecided = []
        for path, command in _leaf_commands(root).items():
            for param in command.params:
                for flag in param.opts:
                    if not any(word in flag for word in ("project", "alias", "stack")):
                        continue
                    translated = (
                        "--project" in param.opts
                        or flag in ALIAS_OPTIONS.get(path, frozenset())
                        or ALIAS_ARGUMENTS.get(path) == param.name
                    )
                    if not translated and flag not in NOT_AN_ALIAS:
                        undecided.append(f"{' '.join(path)} {flag}")
        assert not undecided, f"add to ALIAS_OPTIONS or NOT_AN_ALIAS: {undecided}"

    def test_exclusions_are_exactly_the_no_lookup_commands(self) -> None:
        assert NO_LOOKUP_COMMANDS == EXPECTED_EXCLUDED
        root = typer.main.get_command(app)
        assert isinstance(root, TyperGroup)
        paths = {path for path, _ in _commands_with_project_option(root)}
        assert paths >= EXPECTED_EXCLUDED, "an excluded command no longer exists"

    def test_repeatable_option_translates_each_value(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        _write_config(config_dir, {"prod": _project(PROJECT_ID), "other": _project(1)})
        root = typer.main.get_command(app)
        assert isinstance(root, TyperGroup)
        argv = ["--project", str(PROJECT_ID), "--project", "other", "--project", "1"]
        received = _invoke_recording(root, config_dir, ("config", "list"), argv)

        assert list(received["project"]) == ["prod", "other", "other"]


def _flow_detail(config_dir: Path, project: str, *global_flags: str) -> Any:
    """Run `flow detail` -- a command that records its target -- with ``--project project``."""
    argv = ["--config-dir", str(config_dir), *global_flags, "flow", "detail"]
    return runner.invoke(app, [*argv, "--project", project, "--flow-id", "9"])


class TestCliBehavior:
    """What a caller sees: the alias in the output, the notice, the errors."""

    @pytest.fixture
    def config_dir(self, tmp_path: Path) -> Path:
        config_dir = tmp_path / "config"
        _write_config(
            config_dir,
            {
                "prod": _project(PROJECT_ID),
                "a": _project(77),
                "b": _project(77, stack=EU),
            },
        )
        return config_dir

    def test_command_body_and_targets_name_the_alias(self, config_dir: Path) -> None:
        with patch("keboola_agent_cli.cli.FlowService") as flow_service_cls:
            service = flow_service_cls.return_value
            service.get_flow_detail.return_value = {"id": "9"}
            result = _flow_detail(config_dir, str(PROJECT_ID), "--json")

        assert result.exit_code == 0, result.output
        assert service.get_flow_detail.call_args.kwargs["alias"] == "prod"
        envelope = json.loads(result.stdout)
        assert [target["project_alias"] for target in envelope["targets"]] == ["prod"]
        assert "resolved to alias" not in result.stderr

    def test_human_mode_prints_the_resolved_alias(self, config_dir: Path) -> None:
        with patch("keboola_agent_cli.cli.FlowService") as flow_service_cls:
            flow_service_cls.return_value.get_flow_detail.return_value = {"id": "9"}
            result = _flow_detail(config_dir, str(PROJECT_ID))

        assert f"Project ID {PROJECT_ID} resolved to alias 'prod'" in result.stderr
        assert "Target: project 'prod'" in result.stderr

    def test_ambiguous_id_is_a_config_error_listing_the_aliases(self, config_dir: Path) -> None:
        result = _flow_detail(config_dir, "77", "--json")

        assert result.exit_code == 5
        error = json.loads(result.stdout)["error"]
        assert error["code"] == "CONFIG_ERROR"
        assert f"'a' ({US})" in error["message"]
        assert f"'b' ({EU})" in error["message"]

    def test_unmatched_id_keeps_the_not_found_error(self, config_dir: Path) -> None:
        result = runner.invoke(
            app, ["--config-dir", str(config_dir), "--json", "project", "info", "--project", "999"]
        )

        assert result.exit_code == 5
        message = json.loads(result.stdout)["error"]["message"]
        assert "Project '999' not found" in message
        assert "no registered alias '999'" in message
        assert PROJECT_ID_HINT in message

    def test_project_use_pins_the_alias(self, config_dir: Path) -> None:
        result = runner.invoke(
            app, ["--config-dir", str(config_dir), "--json", "project", "use", str(PROJECT_ID)]
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["data"]["alias"] == "prod"
        assert ConfigStore(config_dir=config_dir).load().default_project == "prod"


class TestAliasShadowsAnId:
    """Alias '4242' is project 5555 while project 4242 is 'prod': the alias wins, with a notice."""

    NOTICE = (
        "'4242' is an alias of project 5555; project ID 4242 is registered as 'prod'. "
        "Pass 'prod' to use that project."
    )

    @pytest.fixture
    def config_dir(self, tmp_path: Path) -> Path:
        config_dir = tmp_path / "config"
        _write_config(config_dir, {"4242": _project(5555), "prod": _project(PROJECT_ID)})
        return config_dir

    @pytest.mark.parametrize("global_flags", [(), ("--json",)], ids=["human", "json"])
    def test_notice_on_stderr_in_both_modes(
        self, config_dir: Path, global_flags: tuple[str, ...]
    ) -> None:
        with patch("keboola_agent_cli.cli.FlowService") as flow_service_cls:
            service = flow_service_cls.return_value
            service.get_flow_detail.return_value = {"id": "9"}
            result = _flow_detail(config_dir, "4242", *global_flags)

        assert result.exit_code == 0, result.output
        assert service.get_flow_detail.call_args.kwargs["alias"] == "4242"
        assert self.NOTICE in " ".join(result.stderr.split())
        assert "resolved to alias" not in result.stderr
        if global_flags:
            assert json.loads(result.stdout)["status"] == "ok"
            assert "is an alias of project" not in result.stdout


class TestEnvVar:
    """KBAGENT_PROJECT takes a project ID too."""

    @pytest.fixture
    def service(self, tmp_path: Path) -> ProjectService:
        store = _write_config(
            tmp_path / "config", {"prod": _project(PROJECT_ID), "dev": _project(1)}
        )
        return ProjectService(config_store=store, client_factory=MagicMock())

    def test_env_project_id_resolves_to_alias(
        self, service: ProjectService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_KBAGENT_PROJECT, str(PROJECT_ID))
        assert service.resolve_pinned_alias() == ("prod", "env")

    def test_explicit_project_id_resolves_to_alias(self, service: ProjectService) -> None:
        assert service.resolve_pinned_alias(explicit=str(PROJECT_ID)) == ("prod", "explicit")

    def test_env_unmatched_id_says_nothing_matches(
        self, service: ProjectService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_KBAGENT_PROJECT, "999")
        with pytest.raises(ConfigError, match="no alias or project ID matches it"):
            service.resolve_pinned_alias()

    def test_project_current_reports_the_alias(
        self, service: ProjectService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_KBAGENT_PROJECT, str(PROJECT_ID))
        current = service.current_project()
        assert current["alias"] == "prod"
        assert current["env_override"] == str(PROJECT_ID)
        assert current["env_points_to_configured_project"] is True

    def test_project_current_reports_an_ambiguous_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_dir = tmp_path / "ambiguous"
        _write_config(config_dir, {"a": _project(77), "b": _project(77, stack=EU)})
        monkeypatch.setenv(ENV_KBAGENT_PROJECT, "77")

        current = ProjectService(config_store=ConfigStore(config_dir=config_dir)).current_project()
        assert current["alias"] == "77"
        assert current["env_points_to_configured_project"] is False
        assert "matches more than one registered project" in current["env_error"]

        result = runner.invoke(app, ["--config-dir", str(config_dir), "project", "current"])
        output = " ".join(result.output.split())
        assert result.exit_code == 0, result.output
        assert "matches more than one registered project" in output
        assert "NOT in your configured projects" not in output
