"""Drift guards: the CLI choice StrEnums must match the constants they mirror."""

from keboola_agent_cli.commands.dev_portal import RoleHint
from keboola_agent_cli.commands.job import JobMode, PollStrategy
from keboola_agent_cli.commands.project import ProjectRole
from keboola_agent_cli.constants import (
    PROJECT_ROLES,
    VALID_JOB_MODES,
    VALID_POLL_STRATEGIES,
)


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
