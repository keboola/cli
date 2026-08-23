"""Programmatic-auth session endpoints -- read/audit + local-alias registration only.

Mirrors the read/audit half of the `kbagent auth` command group:
`GET /auth/projects` (the interactive picker's data source), `POST
/auth/register-projects`, and `GET /auth/status`. Every operation acts on a
session already established via a browser login on the host -- none of them
can create or destroy that session, and none of them ever return a token
value (`AuthStatusResult` / `ProjectCandidatesResult` / `RegisterProjectsResult`
are token-free by construction, see `services/auth_service.py`).

`login` / `login-password` / `logout` deliberately have NO endpoints here:

- `auth login` opens a browser (or a device-flow code) on the host machine
  and only completes there -- a REST caller has no way to sit in that loop,
  and "a browser login only completes on the host" is exactly the property
  ``ServiceRegistry`` documents about session projects served over `serve`.
- `auth login-password` takes a plaintext password (and, for MFA accounts,
  a TOTP seed) as input. That credential is meant to flow from a CI secrets
  store straight into one `kbagent` CLI invocation, never as a REST request
  body sitting behind this server's own bearer token.
- `auth logout` revokes the live session backing every session-registered
  project reachable through this very server. Destroying that session is a
  deliberate host-operator action taken at the CLI, not something a REST
  client holding the serve bearer token should be able to trigger remotely.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from ..dependencies import ServiceRegistry, get_registry, require_permission

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterProjectsBody(BaseModel):
    stack: str | None = None
    select_all: bool = Field(default=False, alias="all")
    project_ids: list[int] | None = None
    aliases: dict[int, str] | None = None  # id -> alias override

    model_config = ConfigDict(populate_by_name=True)


@router.get(
    "/projects",
    summary="List the session's registerable project candidates",
    dependencies=[Depends(require_permission("auth.projects"))],
)
def list_project_candidates(
    stack: str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Projects the current session for ``stack`` can access, with a
    collision-free suggested alias each. No CLI leaf command -- the terminal
    equivalent is the interactive picker inside `kbagent auth
    register-projects`. Read-only: never writes `config.json`.
    """
    return asdict(registry.auth.list_project_candidates(stack=stack))


@router.post(
    "/register-projects",
    summary="Register accessible projects as local aliases",
    dependencies=[Depends(require_permission("auth.register-projects"))],
)
def register_projects(
    body: RegisterProjectsBody,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Register accessible projects as `kbagent` aliases under session-sentinel
    tokens. Mirrors `kbagent auth register-projects --all` / `--project-id`.

    Exactly one of `all` (aliased to `select_all`) or `project_ids` selects
    the batch -- the service raises `ConfigError` for zero or both, which
    propagates to the central error handler unchanged. `aliases` overrides
    the suggested alias per project id; keys arrive as `int` (coerced from
    the JSON body's string keys). The interactive picker's own `selections`
    parameter is intentionally unreachable from this body -- it is a CLI-only
    concept with no REST representation.
    """
    return asdict(
        registry.auth.register_projects(
            stack=body.stack,
            select_all=body.select_all,
            project_ids=body.project_ids,
            alias_overrides=body.aliases,
        )
    )


@router.get(
    "/status",
    summary="Session health for a stack",
    dependencies=[Depends(require_permission("auth.status"))],
)
def auth_status(
    stack: str | None = None,
    registry: ServiceRegistry = Depends(get_registry),
) -> dict[str, Any]:
    """Report whether the stored session for `stack` is live, refreshed,
    degraded (offline), expired, or missing -- without mutating it. Mirrors
    `kbagent auth status`.
    """
    return asdict(registry.auth.status(stack=stack))
