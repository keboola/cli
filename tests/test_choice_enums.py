"""Drift guards + behavior for the CLI choice StrEnums."""

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.commands.dev_portal import RoleHint
from keboola_agent_cli.commands.job import JobMode, PollStrategy
from keboola_agent_cli.commands.project import ProjectRole
from keboola_agent_cli.constants import (
    PROJECT_ROLES,
    VALID_JOB_MODES,
    VALID_POLL_STRATEGIES,
)

# Every CLI option backed by a choice StrEnum, with a deliberately invalid value.
_JOB = ["job", "run", "--project", "p", "--component-id", "c", "--config-id", "1"]
_INVALID_CHOICE_CASES = [
    ("job run --mode", [*_JOB, "--mode", "BOGUS"]),
    ("job run --poll-strategy", [*_JOB, "--poll-strategy", "BOGUS"]),
    (
        "project invite --role",
        ["project", "invite", "--project", "p", "--email", "a@b.c", "--role", "BOGUS"],
    ),
    ("project invite --default-role", ["project", "invite", "--default-role", "BOGUS"]),
    (
        "project member-set-role --role",
        ["project", "member-set-role", "--project", "p", "--email", "a@b.c", "--role", "BOGUS"],
    ),
    (
        "dev-portal identity add --role-hint",
        [
            "dev-portal",
            "identity",
            "add",
            "--alias",
            "a",
            "--username",
            "u",
            "--password",
            "p",
            "--role-hint",
            "BOGUS",
        ],
    ),
    (
        "dev-portal identity edit --role-hint",
        ["dev-portal", "identity", "edit", "--alias", "a", "--role-hint", "BOGUS"],
    ),
]


def test_job_mode_enum_matches_constant() -> None:
    assert {m.value for m in JobMode} == set(VALID_JOB_MODES)


def test_poll_strategy_enum_matches_constant() -> None:
    assert {m.value for m in PollStrategy} == set(VALID_POLL_STRATEGIES)


def test_project_role_enum_matches_constant() -> None:
    # Order matters: the --role help text renders " | ".join(PROJECT_ROLES).
    assert tuple(m.value for m in ProjectRole) == tuple(PROJECT_ROLES)


def test_role_hint_enum_values() -> None:
    # dev-portal role_hint has no shared constant; the values are the apps-api
    # contract ("vendor" -> vendor endpoint, "admin" -> PATCH /admin/apps/{app}).
    assert {m.value for m in RoleHint} == {"vendor", "admin"}


@pytest.mark.parametrize(
    "argv", [c[1] for c in _INVALID_CHOICE_CASES], ids=[c[0] for c in _INVALID_CHOICE_CASES]
)
def test_invalid_choice_exits_2_without_traceback(argv: list[str]) -> None:
    """An invalid choice value is a clean usage error (exit 2), not a traceback.

    Regression: a standalone click.Choice raised an exception class Typer's
    vendored-Click parser does not catch -> uncaught traceback, exit 1.
    """
    result = CliRunner().invoke(app, argv)
    assert result.exit_code == 2, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"uncaught exception: {result.exception!r}"
    )
