"""Alias computation and project registration for the `kbagent auth` group.

The result dataclasses and the alias/candidate/selection logic behind
`AuthService.list_project_candidates` / `register_projects` /
`login(register_projects=True)`. `AuthService` (services/auth_service.py) owns
the network flows and re-exports every public name here, so callers keep
importing from `services.auth_service`.

Everything below takes an explicit `ConfigStore` instead of reaching for one:
the alias rules are the part of the auth group that is pure decision-making
over `config.json`, and keeping them free of the login flow's browser/device
seams makes them directly unit-testable.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..auth.sentinel import is_session_token, make_session_token
from ..config_store import ConfigStore, validate_alias_format
from ..errors import ConfigError
from ..models import AppConfig, ProjectConfig, normalize_stack_url

# Command surfaces that refuse a `kbc-session://` project, one entry per
# `require_static_token` guard outside the Storage/Manage paths (v1 scope).
# `auth login` / `auth register-projects` disclose this at registration time so
# the scope is known before the first refusal instead of being discovered one
# failed command at a time. Keep it in step with the guards; a natural future
# home is `constants.py`.
SESSION_UNSUPPORTED_FEATURES: tuple[str, ...] = (
    "kbagent kai",
    "kbagent semantic-layer (Metastore Service)",
    "kbagent data-app (Data Science Service)",
    "kbagent stream (Data Streams Service)",
    "kbagent sharing, unless a master token is set in the environment",
    "AI Service paths: kbagent docs query, config examples, config new, "
    "component detail/search, flow new/update/validate",
    "Scheduler Service paths: kbagent flow schedule, flow schedule-remove",
    "the importable SDK (keboola_agent_cli.Client)",
)

_SLUG_INVALID_CHARS = re.compile(r"[^a-z0-9]+")


def default_unsupported_features() -> list[str]:
    """Fresh copy of `SESSION_UNSUPPORTED_FEATURES` for a result dataclass field."""
    return list(SESSION_UNSUPPORTED_FEATURES)


def slugify_project_name(name: str) -> str:
    """Turn a project name into a lowercase, hyphenated alias candidate."""
    return _SLUG_INVALID_CHARS.sub("-", name.strip().lower()).strip("-")


@dataclass(frozen=True)
class RegisteredProject:
    """Outcome of registering one accessible project as a `kbagent` alias."""

    alias: str
    project_id: int
    project_name: str
    status: str  # "registered" | "exists" | "skipped"
    note: str = ""


@dataclass(frozen=True)
class ProjectCandidate:
    """One project accessible to a session, offered up for local registration.

    `default_alias` is always collision-free -- computed against both
    `config.json` and every earlier candidate in the same batch (see
    `build_candidates`) -- so a caller (the picker, or
    `login(register_projects=True)`) can accept it blindly with no further
    validation beyond `validate_alias_format`.
    """

    project_id: int
    project_name: str
    role: str
    default_alias: str
    existing_alias: str  # "" unless already registered as a SESSION project
    registered: bool  # existing_alias != ""


@dataclass(frozen=True)
class ProjectCandidatesResult:
    """Result of `AuthService.list_project_candidates`."""

    stack_url: str
    candidates: list[ProjectCandidate]


@dataclass(frozen=True)
class ProjectSelection:
    """One caller's choice to register a project, with an optional alias override.

    An empty `alias` means "use the candidate's `default_alias`" -- this is
    how `login(register_projects=True)` (which wants every accessible
    project registered under its suggestion) and an explicit `--alias
    ID=ALIAS` override (which wants exactly one alias) share the same
    application path (`apply_selections`).
    """

    project_id: int
    alias: str = ""


@dataclass(frozen=True)
class RegisterProjectsResult:
    """Result of `AuthService.register_projects`."""

    status: str  # always "ok"
    stack_url: str
    registered_projects: list[RegisteredProject]
    warnings: list[str]
    session_unsupported_features: list[str] = field(default_factory=default_unsupported_features)


def build_candidates(
    config_store: ConfigStore, stack_url: str, projects: Sequence[Mapping[str, Any]]
) -> list[ProjectCandidate]:
    """Turn accessible-project entries into collision-free registration candidates.

    Network-free: `projects` entries carry `id` / `name` / `role` keys -- the
    exact shape of `LoginResult.accessible_projects` -- so the post-login
    picker can run against data `login()` already fetched, without a second
    introspect round trip. `AuthService.list_project_candidates` (which DOES
    introspect) delegates here too, so the two callers can never compute a
    different default alias for the same project.

    Default alias algorithm (processed in input order, so earlier candidates
    in the same batch can claim aliases before later ones):

    1. Already registered? Scan `config.json` for an entry whose token is a
       session sentinel AND whose `project_id` AND (normalized) `stack_url`
       match this project. Matching on (project_id, stack_url) -- never on the
       alias string -- means a project someone already registered under a
       hand-picked alias is reported as that alias rather than offered a
       second, colliding suggestion. When found, `existing_alias ==
       default_alias` and `registered=True`.
    2. Otherwise, slugify the project name (`project-{id}` when the name
       slugifies to nothing, e.g. all-punctuation names).
    3. Suffix with `-{id}`, then `-{id}-2`, `-{id}-3`, ... until a value is
       free, where *free* means absent from `config.json` AND not already
       claimed by an earlier candidate in this batch. Two projects sharing a
       name (or a name colliding with an existing *static-token* project)
       therefore each get a distinct, usable alias, and the static project is
       never touched.
    """
    config = config_store.load()
    claimed_aliases = set(config.projects.keys())
    candidates: list[ProjectCandidate] = []
    for project in projects:
        project_id = int(project["id"])
        project_name = str(project.get("name", ""))
        role = str(project.get("role", ""))

        existing_alias = _find_registered_alias(config, stack_url, project_id)
        if existing_alias:
            candidates.append(
                ProjectCandidate(
                    project_id=project_id,
                    project_name=project_name,
                    role=role,
                    default_alias=existing_alias,
                    existing_alias=existing_alias,
                    registered=True,
                )
            )
            continue

        base = slugify_project_name(project_name) or f"project-{project_id}"
        alias = _first_free_alias(base, project_id, claimed_aliases)
        claimed_aliases.add(alias)
        candidates.append(
            ProjectCandidate(
                project_id=project_id,
                project_name=project_name,
                role=role,
                default_alias=alias,
                existing_alias="",
                registered=False,
            )
        )
    return candidates


def _find_registered_alias(config: AppConfig, stack_url: str, project_id: int) -> str:
    """Return the alias `project_id`/`stack_url` is already registered under, or ""."""
    for alias, entry in config.projects.items():
        if (
            is_session_token(entry.token)
            and entry.project_id == project_id
            and normalize_stack_url(entry.stack_url) == stack_url
        ):
            return alias
    return ""


def _first_free_alias(base: str, project_id: int, claimed: set[str]) -> str:
    """First of `base`, `{base}-{id}`, `{base}-{id}-2`, ... absent from `claimed`."""
    if base not in claimed:
        return base
    with_id = f"{base}-{project_id}"
    if with_id not in claimed:
        return with_id
    suffix = 2
    while True:
        candidate = f"{with_id}-{suffix}"
        if candidate not in claimed:
            return candidate
        suffix += 1


def require_single_selection_mode(
    *,
    select_all: bool,
    project_ids: Sequence[int] | None,
    selections: Sequence[ProjectSelection] | None,
) -> None:
    """Reject an ambiguous or absent selection mode before any network call.

    `register_projects` has exactly three selectors and they are alternatives,
    not layers. Checking first means a caller that passes two (or none) pays
    no introspect round trip to find that out.
    """
    given = [
        name
        for name, chosen in (
            ("select_all", select_all),
            ("project_ids", project_ids is not None),
            ("selections", selections is not None),
        )
        if chosen
    ]
    if len(given) == 1:
        return
    if not given:
        raise ConfigError(
            "No project selection given: pass select_all=True, project_ids, or selections."
        )
    raise ConfigError(f"Selection modes are mutually exclusive; got {' + '.join(given)}.")


def resolve_selections(
    candidates: Sequence[ProjectCandidate],
    *,
    select_all: bool,
    project_ids: Sequence[int] | None,
    selections: Sequence[ProjectSelection] | None,
    alias_overrides: Mapping[int, str],
) -> list[ProjectSelection]:
    """Turn one of the three selectors into the single `ProjectSelection` list.

    `select_all` takes the whole candidate set; `project_ids` takes those ids
    verbatim, INCLUDING ids no candidate covers, so the caller's accessibility
    check can name the offending id instead of silently dropping it.

    `alias_overrides` fills in an alias only where the selection does not
    already carry one, so an alias the interactive picker resolved wins over a
    `--alias ID=ALIAS` that was already folded into it.
    """
    if select_all:
        chosen: list[ProjectSelection] = [
            ProjectSelection(project_id=candidate.project_id) for candidate in candidates
        ]
    elif project_ids is not None:
        chosen = [ProjectSelection(project_id=project_id) for project_id in project_ids]
    else:
        chosen = list(selections or [])
    return [
        ProjectSelection(
            project_id=selection.project_id,
            alias=selection.alias or alias_overrides.get(selection.project_id, ""),
        )
        for selection in chosen
    ]


def apply_selections(
    config_store: ConfigStore,
    stack_url: str,
    candidates_by_id: Mapping[int, ProjectCandidate],
    selections: Sequence[ProjectSelection],
    warnings: list[str],
) -> list[RegisteredProject]:
    """Apply each selection: validate the alias, then register/report/skip.

    Shared by `AuthService.register_projects` and
    `AuthService.login(register_projects=True)` so the two entry points can
    never drift on the exists/skip/overwrite rules. Per selection, with
    `alias = selection.alias or candidate.default_alias`:

    - `validate_alias_format` first -- rejects a hand-typed alias before it is
      ever compared against `config.json` or written to it.
    - Already registered (`candidate.registered`): requesting the exact
      `existing_alias` reports `status="exists"` with no write; any other
      alias is `status="skipped"` (never overwritten -- the existing
      registration is the one true entry, so the fix is `project edit
      --new-alias`, not a silent second write).
    - Alias already taken in `config.json` by anything else (including a
      static-token project): `status="skipped"`, never overwritten.
    - Otherwise: `config_store.add_project` under a session-sentinel token,
      `status="registered"`.

    `config_store.get_project(alias)` is re-read per selection (rather than
    once up front) so a duplicate `--alias ID=X` across two different project
    ids in the same batch is caught against what the earlier selection in this
    same call just wrote, not a stale snapshot.
    """
    registered: list[RegisteredProject] = []
    for selection in selections:
        candidate = candidates_by_id[selection.project_id]
        alias = selection.alias or candidate.default_alias
        validate_alias_format(alias, field="alias")

        if candidate.registered:
            if alias == candidate.existing_alias:
                registered.append(
                    RegisteredProject(
                        alias=alias,
                        project_id=candidate.project_id,
                        project_name=candidate.project_name,
                        status="exists",
                    )
                )
            else:
                note = (
                    f"Project {candidate.project_id} is already registered as "
                    f"'{candidate.existing_alias}'; run 'kbagent project edit "
                    f"--project {candidate.existing_alias} --new-alias {alias}' "
                    "to rename it."
                )
                warnings.append(note)
                registered.append(
                    RegisteredProject(
                        alias=alias,
                        project_id=candidate.project_id,
                        project_name=candidate.project_name,
                        status="skipped",
                        note=note,
                    )
                )
            continue

        if config_store.get_project(alias) is not None:
            note = f"Alias '{alias}' already points at a different project; not overwritten."
            warnings.append(note)
            registered.append(
                RegisteredProject(
                    alias=alias,
                    project_id=candidate.project_id,
                    project_name=candidate.project_name,
                    status="skipped",
                    note=note,
                )
            )
            continue

        config_store.add_project(
            alias,
            ProjectConfig(
                stack_url=stack_url,
                token=make_session_token(candidate.project_id),
                project_name=candidate.project_name,
                project_id=candidate.project_id,
            ),
        )
        registered.append(
            RegisteredProject(
                alias=alias,
                project_id=candidate.project_id,
                project_name=candidate.project_name,
                status="registered",
            )
        )
    return registered


__all__ = [
    "SESSION_UNSUPPORTED_FEATURES",
    "ProjectCandidate",
    "ProjectCandidatesResult",
    "ProjectSelection",
    "RegisterProjectsResult",
    "RegisteredProject",
    "apply_selections",
    "build_candidates",
    "default_unsupported_features",
    "require_single_selection_mode",
    "resolve_selections",
    "slugify_project_name",
]
