"""Let every ``--project`` option take a project ID as well as an alias (CLI-22).

There is no shared ``--project`` option object: each command declares its
own, as a single value or a repeatable list. :class:`ProjectRefGroup` is the
class of the root command group, so Typer builds it last, with every command
already built; it then walks the tree once and adds
:func:`_translate_project_ref` as the callback of each ``--project`` option.
The same goes for the few other parameters that name an existing alias
(:data:`ALIAS_ARGUMENTS`, :data:`ALIAS_OPTIONS`).
Click runs that callback while it parses the command's arguments, which is
after the root callback put the config store into ``ctx.obj`` and before the
command body runs. The command and its services therefore only ever see the
alias. The rules are in :mod:`keboola_agent_cli.project_ref`;
``tests/test_project_ref.py`` invokes every command with ``--project``, and
every table entry, to prove the translation reaches it.

A command whose single ``--project`` is omitted gets the project of the T1
chain from :func:`use_active_project` (spec T17), which the root command
class calls after Click checked every parameter and before the command body
runs. Such a command declares ``project: str = typer.Option(None, ...)``: the
body always receives an alias. No command requires ``--project``.
"""

from collections.abc import Callable
from typing import Any

import typer
from typer.core import TyperGroup

from ..effective_branch import record_project
from ..errors import ConfigError, ErrorCode
from ..project_ref import alias_shadow_notice, is_project_id, resolve_project_ref
from ..services.base import BaseService

# T11, T17: what an omitted --project means, in the words of every --project help.
PROJECT_DEFAULT = "default: KBAGENT_PROJECT, then the active project, then the sole project"
PROJECT_HELP = f"Project alias or ID ({PROJECT_DEFAULT})"

# Commands whose --project is NOT a lookup in the registered projects, so it
# is never translated:
# - `lineage show`: the value filters the aliases inside an offline graph
#   file; the command needs no config at all.
NO_LOOKUP_COMMANDS: frozenset[tuple[str, ...]] = frozenset({("lineage", "show")})

# Commands whose --project names a registered project but not the project the
# command works in, so an omitted one stays omitted:
# - `agent prompt-improve`: a hint in the improved prompt.
# - `init`: the projects to copy from the global configuration.
NOT_A_TARGET_COMMANDS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("agent", "prompt-improve"),
        ("init",),
    }
)

# Commands that decide in their body what an omitted single --project means:
# - `flow schema` / `flow validate`: the T1 chain, but when it finds no
#   project they work offline (the bundled schema, the semantic checks).
# - `project invite`: with --from-csv the projects come from the file, and
#   --project is refused; without it, the body uses the T1 chain.
OWN_PROJECT_DEFAULT_COMMANDS: frozenset[tuple[str, ...]] = frozenset(
    {
        ("flow", "schema"),
        ("flow", "validate"),
        ("project", "invite"),
    }
)

# Positional arguments that name an existing alias, keyed by command path.
ALIAS_ARGUMENTS: dict[tuple[str, ...], str] = {("project", "use"): "alias"}

# Options other than --project that name an existing alias, keyed by command
# path. `sl` is the hidden second name of `semantic-layer`, a separate subtree.
# `--stack` takes a stack URL or an alias; only the alias form is translated.
ALIAS_OPTIONS: dict[tuple[str, ...], frozenset[str]] = {
    ("auth", "login"): frozenset({"--stack"}),
    ("auth", "login-password"): frozenset({"--stack"}),
    ("auth", "status"): frozenset({"--stack"}),
    ("auth", "logout"): frozenset({"--stack"}),
    ("auth", "register-projects"): frozenset({"--stack"}),
    ("config", "clone"): frozenset({"--target-project"}),
    ("sync", "clone"): frozenset({"--target"}),
    ("semantic-layer", "promote"): frozenset({"--from-project", "--to-project"}),
    ("semantic-layer", "diff"): frozenset({"--project-a", "--project-b"}),
    ("sl", "promote"): frozenset({"--from-project", "--to-project"}),
    ("sl", "diff"): frozenset({"--project-a", "--project-b"}),
}

# Click context, parameter, value. Typed Any: Typer >= 0.25 vendors Click as
# `typer._click`, older releases use the `click` package, and pyproject.toml
# allows both (typer>=0.12), so neither module can be imported here.
ParamCallback = Callable[[Any, Any, Any], Any]


class ProjectRefGroup(TyperGroup):
    """Root command group that adds the project-ID translation to the command tree."""

    def __init__(self, **attrs: Any) -> None:
        super().__init__(**attrs)
        add_project_ref_callbacks(self)


def add_project_ref_callbacks(group: TyperGroup, path: tuple[str, ...] = ()) -> None:
    """Add the translation to every parameter under ``group`` that names an existing alias."""
    for name, command in group.commands.items():
        command_path = (*path, name)
        if isinstance(command, TyperGroup):
            add_project_ref_callbacks(command, command_path)
            continue
        if command_path in NO_LOOKUP_COMMANDS:
            continue
        for param in command.params:
            if _names_existing_alias(command_path, param):
                param.callback = _with_translation(param.callback)


def _names_existing_alias(command_path: tuple[str, ...], param: Any) -> bool:
    """True for ``--project`` and for the parameters listed in the two tables above."""
    if "--project" in param.opts or ALIAS_ARGUMENTS.get(command_path) == param.name:
        return True
    return not ALIAS_OPTIONS.get(command_path, frozenset()).isdisjoint(param.opts)


def _with_translation(original: ParamCallback | None) -> ParamCallback:
    """Run the translation after the callback the option already has, if any."""
    if original is None:
        return _translate_and_record

    def _chained(ctx: Any, param: Any, value: Any) -> Any:
        return _translate_and_record(ctx, param, original(ctx, param, value))

    return _chained


def _translate_and_record(ctx: Any, param: Any, value: Any) -> Any:
    """Translate ``value`` to aliases, then record each registered one as ``explicit`` (T6)."""
    value = _translate_project_ref(ctx, param, value)
    _record_explicit(ctx, param, value)
    return value


def target_project_param(command: Any) -> Any | None:
    """The ``--project`` of ``command`` when it names the target project, or None.

    None for the commands of ``NO_LOOKUP_COMMANDS`` and ``NOT_A_TARGET_COMMANDS``:
    their ``--project`` is a new alias, a filter or a hint. ``command.operation``
    is the canonical path that the root command class set.
    """
    path = tuple(command.operation.split("."))
    if path in NO_LOOKUP_COMMANDS or path in NOT_A_TARGET_COMMANDS:
        return None
    return next((p for p in command.params if "--project" in p.opts), None)


def active_project_param(command: Any) -> Any | None:
    """The ``--project`` that :func:`use_active_project` fills when it is omitted, or None.

    That is the single target ``--project`` of every command, except a command
    with an all-projects option, which resolves its projects in its body (T2),
    and the commands of ``OWN_PROJECT_DEFAULT_COMMANDS``.
    """
    param = target_project_param(command)
    if param is None or param.multiple:
        return None
    if tuple(command.operation.split(".")) in OWN_PROJECT_DEFAULT_COMMANDS:
        return None
    return None if any(p.name == "all_projects" for p in command.params) else param


def use_active_project(ctx: Any, command: Any) -> None:
    """Fill an omitted single ``--project`` from the T1 chain (spec T17).

    ``KBAGENT_PROJECT`` > active project > sole registered project, recorded
    with its source in ``targets``; no project is ``CONFIG_ERROR`` (exit 5).
    """
    param = active_project_param(command)
    if param is None or ctx.params.get(param.name) is not None:
        return
    obj = ctx.find_root().obj
    try:
        alias, _source = BaseService(obj["config_store"]).resolve_pinned_alias()
    except ConfigError as exc:
        obj["formatter"].error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None
    ctx.params[param.name] = alias


def _record_explicit(ctx: Any, param: Any, value: Any) -> None:
    """Record the projects an option names as given on the command line (``project_source``).

    ``--stack`` names a stack, not a target project, and the ``project use``
    argument is the choice being saved, so neither is recorded.
    """
    aliases = [v for v in (value if isinstance(value, tuple | list) else [value]) if v]
    obj = ctx.obj if isinstance(ctx.obj, dict) else {}
    config_store = obj.get("config_store")
    if not aliases or config_store is None or param.param_type_name == "argument":
        return
    if "--stack" in param.opts:
        return
    try:
        projects = config_store.load().projects
    except ConfigError:
        return  # the command reports the broken config itself
    for alias in aliases:
        if alias in projects:
            record_project(alias, "explicit", projects[alias])


def _translate_project_ref(ctx: Any, param: Any, value: Any) -> Any:
    """Replace each project ID in ``value`` (one string or a tuple of them) with its alias."""
    refs = list(value) if isinstance(value, tuple | list) else [value]
    obj = ctx.obj if isinstance(ctx.obj, dict) else {}
    config_store = obj.get("config_store")
    if config_store is None or not any(ref and is_project_id(ref) for ref in refs):
        return value

    formatter = obj["formatter"]
    try:
        projects = config_store.load().projects
        aliases = [resolve_project_ref(projects, ref) for ref in refs]
    except ConfigError as exc:
        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)
        raise typer.Exit(code=5) from None

    for ref, alias in zip(refs, aliases, strict=True):
        if ref != alias and not formatter.json_mode:
            formatter.err_console.print(
                f"Project ID {ref} resolved to alias '{alias}'",
                style="dim",
                markup=False,
                highlight=False,
            )
        # On stderr in --json mode too: the alias silently won over an ID the
        # caller may have meant, and stdout must stay one JSON document.
        notice = alias_shadow_notice(projects, ref)
        if notice:
            formatter.err_console.print(
                f"Warning: {notice}", style="yellow", markup=False, highlight=False
            )
    return type(value)(aliases) if isinstance(value, tuple | list) else aliases[0]


def env_override_warning(current: dict[str, Any]) -> str | None:
    """The `project current` warning for a KBAGENT_PROJECT no command can use, or None.

    ``current`` is the ``ProjectService.current_project()`` result. An ID
    registered under several aliases (``env_error``, CLI-22) is not "missing"
    from the config, so it gets its own message.
    """
    if current.get("env_error"):
        return f"{current['env_error']} Commands that use KBAGENT_PROJECT will fail."
    if current.get("env_points_to_configured_project") is False:
        return (
            f"'{current['alias']}' is NOT in your configured projects. "
            "Commands that use KBAGENT_PROJECT will fail."
        )
    return None
