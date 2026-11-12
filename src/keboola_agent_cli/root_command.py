"""The root command class of the 1.x command tree (keboola/cli#791, CLI-44).

:class:`RootGroup` is the class of the root command group. Its top-level
commands are the table :data:`COMMANDS`; the first read of one imports its
module (#801) and gives every group under it the class :class:`CommandGroup`
and every command the class :class:`LeafCommand`. Together they hold these rules of
``docs/cli-interface-spec.md``:

- O1: with ``--json``, stdout holds one JSON document for every exit code. A
  usage error, a bare group and a crash write a ``USAGE_ERROR`` or
  ``INTERNAL_ERROR`` envelope (:meth:`RootGroup.invoke`). A bad option value
  (Click's ``BadParameter``, from type conversion or from a command body) is
  ``INVALID_ARGUMENT`` (I4); a missing required option stays ``USAGE_ERROR``.
- O1, row 12: a bare group, and ``kbagent --json`` without a command, exit 2
  with the help on stderr (:class:`MissingCommandError`).
- O7, EV4: ``--json``/``-j``, ``--deny-writes`` and ``--deny-destructive`` are
  also accepted after the subcommand. Each group and command gets a hidden copy
  of them, so Click parses option values correctly and argv is never
  rewritten. :meth:`RootGroup.invoke` reads them before the root callback runs.
- A6, row 19: a single-value option given twice is a usage error on a
  ``write``, ``destructive`` or ``admin`` command and a ``warnings[]`` entry
  on a ``read`` command (:meth:`LeafCommand.parse_args`).
- T17: a command with an omitted single ``--project`` uses the T1
  chain (:meth:`LeafCommand.invoke`, ``commands/_project_ref.py``).

``-h`` (H5) is the root ``help_option_names`` context setting in ``cli.py``;
every child context inherits it.
"""

from __future__ import annotations

import contextlib
import copy
import functools
import importlib
import inspect
import sys
import traceback
from collections import Counter, UserDict
from typing import Any

import typer
from typer.core import TyperCommand, TyperGroup

from . import telemetry
from .commands._helpers import removed_argument, removed_form
from .commands._project_ref import ProjectRefGroup, add_project_ref_callbacks, use_active_project
from .errors import ErrorCode
from .output import OutputFormatter
from .permissions import OPERATION_REGISTRY

# The vendored Click of Typer >= 0.25 (`typer._click`), else the `click` package.
_click: Any = getattr(typer.core, "_click", None) or getattr(typer.core, "click")  # noqa: B009
UsageError = _click.exceptions.UsageError
BadParameter = _click.exceptions.BadParameter
MissingParameter = _click.exceptions.MissingParameter
NoSuchOption = _click.exceptions.NoSuchOption

# Root parameter name -> its option names. A hidden copy of each goes on every
# group and command (O7).
ROOT_FLAGS: dict[str, tuple[str, ...]] = {
    "json_output": ("--json", "-j"),
    "deny_writes": ("--deny-writes",),
    "deny_destructive": ("--deny-destructive",),
}
_COPY_PREFIX = "root_flag_copy_"

# A6: categories on which a repeated single-value option is a usage error.
# A read command gets a warning.
REPEAT_REFUSED_CATEGORIES = frozenset({"write", "destructive", "admin"})
REPEATED_OPTION_CODE = "REPEATED_OPTION"


class MissingCommandError(UsageError):
    """A group without a command, or a command without its arguments: exit 2, help on stderr.

    Click's own ``NoArgsIsHelpError`` cannot do this: with Rich, building its
    message prints the help to stdout.
    """


def is_root_flag_copy(param: Any) -> bool:
    """True for the hidden copy of a root flag on a group or command (not part of its interface)."""
    return (getattr(param, "name", None) or "").startswith(_COPY_PREFIX)


@functools.cache
def _root_flag_templates() -> tuple[Any, ...]:
    """The hidden root flag copies, built once by Typer itself from ``typer.Option``.

    ``TyperOption``'s constructor is not stable across Typer releases (0.29
    requires a ``type_descriptor`` from a private module, keboola/cli#970), so
    the options come from a throwaway command through the public
    ``typer.main.get_command``.
    """
    params = [
        inspect.Parameter(
            f"{_COPY_PREFIX}{name}",
            inspect.Parameter.KEYWORD_ONLY,
            default=typer.Option(
                False,
                *opts,
                hidden=True,
                expose_value=False,
                help=f"Same as the root option {opts[0]}.",
            ),
            annotation=bool,
        )
        for name, opts in ROOT_FLAGS.items()
    ]

    def flags() -> None:
        """Carries the root flag copies."""

    flags.__signature__ = inspect.Signature(params)  # ty: ignore[unresolved-attribute]
    flags.__annotations__ = {param.name: bool for param in params}
    app = typer.Typer(add_completion=False)
    app.command()(flags)
    return tuple(typer.main.get_command(app).params)


def _root_flag_copies() -> list[Any]:
    return [copy.copy(option) for option in _root_flag_templates()]


def protected_args(ctx: Any) -> list[str]:
    """The subcommand name and its arguments that a group parse keeps for its command.

    Click >= 8.2 (the one Typer >= 0.25 vendors) stores them as the private
    ``ctx._protected_args``; Click 8.1, which older Typer releases allowed by
    ``typer>=0.12`` run on, as ``ctx.protected_args``.
    """
    if hasattr(ctx, "_protected_args"):
        return list(ctx._protected_args)
    return list(ctx.protected_args)


class LeafCommand(TyperCommand):
    """The class of every command: reports a repeated single-value option (A6).

    It also gives a command with an omitted ``--project`` the active project (T17).
    """

    # The OPERATION_REGISTRY key, set by the tree walk of RootGroup.
    operation: str = ""

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        if not ctx.resilient_parsing:
            if not args and self.no_args_is_help:
                raise MissingCommandError("Missing arguments.", ctx)
            _report_repeated_options(self, ctx, args)
        typed = list(args)  # Click's parser consumes `args`
        try:
            return super().parse_args(ctx, args)
        except NoSuchOption as exc:
            entry = removed_form(_command_names(ctx), exc.option_name)
            if entry is not None:
                # C3: the error of a removed option names its replacement.
                exc.message = f"No such option: {exc.option_name}. {entry.message}"
                exc.possibilities = None
            raise
        except UsageError:
            # C3: a positional value given to a command whose positional
            # argument was removed gets an error that names the replacement,
            # also when Click first reports the missing new option.
            extra = _extra_arguments(self, ctx, typed)
            entry = removed_argument(_command_names(ctx)) if extra else None
            if entry is None:
                raise
            raise UsageError(
                f"Got unexpected extra argument ({' '.join(extra)}). {entry.message}", ctx
            ) from None

    def invoke(self, ctx: Any) -> Any:
        # The formatter needs the category for the T12 warning in --json.
        root_obj = ctx.find_root().obj
        formatter = root_obj.get("formatter") if isinstance(root_obj, dict) else None
        if formatter is not None:
            formatter.command_category = OPERATION_REGISTRY.get(self.operation, "write")
            use_active_project(ctx, self)
        return super().invoke(ctx)


class CommandGroup(TyperGroup):
    """The class of every group below the root: a bare group is a usage error (O1)."""

    def parse_args(self, ctx: Any, args: list[str]) -> list[str]:
        rest = super().parse_args(ctx, args) if args else []
        if not protected_args(ctx) and not ctx.resilient_parsing:
            raise MissingCommandError("Missing command.", ctx)
        return rest

    def resolve_command(self, ctx: Any, args: list[str]) -> Any:
        try:
            return super().resolve_command(ctx, args)
        except UsageError:
            # C3: the error of a removed command names its replacement.
            entry = removed_form([*_command_names(ctx), args[0]])
            if entry is None:
                raise
            raise UsageError(f"No such command '{args[0]}'. {entry.message}", ctx) from None


def _extra_arguments(command: LeafCommand, ctx: Any, args: list[str]) -> list[str]:
    """The positional values on the command line that no argument of ``command`` takes."""
    try:
        _, rest, _ = command.make_parser(ctx).parse_args(args=args)
    except UsageError:
        return []
    return list(rest)


def _command_names(ctx: Any) -> list[str]:
    """The command path of ``ctx`` without the root, as typed."""
    names: list[str] = []
    while ctx.parent is not None:
        names.append(ctx.info_name)
        ctx = ctx.parent
    return names[::-1]


def _single_value(param: Any) -> bool:
    """An option that keeps one value: not repeatable, not a count, not a plain on-flag."""
    if getattr(param, "param_type_name", "") != "option" or is_root_flag_copy(param):
        return False
    if getattr(param, "multiple", False) or getattr(param, "count", False):
        return False
    # `--yes --yes` cannot change the result; `--x --no-x` can.
    return not (getattr(param, "is_flag", False) and not getattr(param, "secondary_opts", []))


def _option_name(param: Any) -> str:
    return next((o for o in param.opts if o.startswith("--")), param.opts[0])


def _report_repeated_options(command: LeafCommand, ctx: Any, args: list[str]) -> None:
    try:
        _, _, order = command.make_parser(ctx).parse_args(args=list(args))
    except UsageError:
        return  # the real parse reports it
    if any(set(getattr(p, "opts", ())) & set(ctx.help_option_names) for p in order):
        return  # --help wins
    counts = Counter(p for p in order if _single_value(p))
    repeated = sorted(_option_name(p) for p, n in counts.items() if n > 1)
    if not repeated:
        return
    names = ", ".join(repeated)
    category = OPERATION_REGISTRY.get(command.operation, "write")
    if category in REPEAT_REFUSED_CATEGORIES:
        raise UsageError(
            f"{names} given more than once. '{ctx.command_path}' is a {category} "
            "command and takes one value of each: give each option once.",
            ctx,
        )
    formatter = (ctx.obj or {}).get("formatter") if isinstance(ctx.obj, dict) else None
    if formatter is not None:
        formatter.warning(
            f"{names} given more than once; the last value is used.",
            code=REPEATED_OPTION_CODE,
            hint="Give each option once.",
            options=repeated,
        )


_SETUP = "Setup & Info"
_PROJ = "Project Management"
_BROWSE = "Browse & Inspect"
_FLOWS = "Flows"
_DEV = "Development"

# The top-level commands in the order of `kbagent --help`: name -> ("module:attribute"
# in commands/, help panel, other options of `Typer.command` / `Typer.add_typer`).
COMMANDS: dict[str, tuple[str, str, dict[str, Any]]] = {
    "init": ("init:init_command", _SETUP, {}),
    "doctor": ("doctor:doctor_command", _SETUP, {}),
    "version": ("version:version_command", _SETUP, {}),
    "update": ("version:update_command", _SETUP, {}),
    "changelog": ("changelog:changelog_command", _SETUP, {}),
    "context": ("context:context_command", _SETUP, {}),
    "repl": ("repl:repl_command", _SETUP, {}),
    "serve": ("serve:serve_command", _SETUP, {}),
    "search": (
        "search:search_command",
        _BROWSE,
        {
            "help": "Search for items (tables, buckets, configs, flows, …) by name or content.",
            "no_args_is_help": True,
        },
    ),
    "permissions": ("permissions:permissions_app", _SETUP, {}),
    "auth": ("auth:auth_app", _SETUP, {}),
    "project": ("project:project_app", _PROJ, {}),
    "org": ("org:org_app", _PROJ, {}),
    "feature": ("feature:feature_app", _PROJ, {}),
    "token": ("token:token_app", _PROJ, {}),
    "billing": ("billing:billing_app", _PROJ, {}),
    "component": ("component:component_app", _BROWSE, {}),
    "config": ("config:config_app", _BROWSE, {}),
    "data-app": ("data_app:data_app_app", _BROWSE, {}),
    "job": ("job:job_app", _BROWSE, {}),
    "storage": ("storage:storage_app", _BROWSE, {}),
    "stream": ("stream:stream_app", _BROWSE, {}),
    "sharing": ("sharing:sharing_app", _BROWSE, {}),
    "lineage": ("lineage:lineage_app", _BROWSE, {}),
    "kai": ("kai:kai_app", _BROWSE, {}),
    "docs": ("docs:docs_app", _BROWSE, {}),
    "transformation": ("transformation:transformation_app", _BROWSE, {}),
    "flow": ("flow:flow_app", _FLOWS, {}),
    "schedule": ("schedule:schedule_app", _FLOWS, {}),
    "notification": ("notification:notification_app", _FLOWS, {}),
    "branch": ("branch:branch_app", _DEV, {}),
    "merge-request": ("merge_request:merge_request_app", _DEV, {}),
    "mr": ("merge_request:merge_request_app", _DEV, {"hidden": True}),
    "workspace": ("workspace:workspace_app", _DEV, {}),
    "sync": ("sync:sync_app", _DEV, {}),
    "encrypt": ("encrypt:encrypt_app", _DEV, {}),
    "semantic-layer": ("semantic_layer:semantic_layer_app", _DEV, {}),
    "sl": ("semantic_layer:semantic_layer_app", _DEV, {"hidden": True}),
    "rls": ("rls:rls_app", _DEV, {}),
    "cls": ("cls:cls_app", _DEV, {}),
    "http": ("http_client:http_app", _DEV, {}),
    "agent": ("agent:agent_app", _DEV, {}),
    "dev-portal": ("dev_portal:dev_portal_app", _DEV, {}),
}


def _build_command(name: str) -> Any:
    """Import the module of a top-level command and build the command as Typer does."""
    target, panel, options = COMMANDS[name]
    module, attribute = target.split(":")
    value = getattr(importlib.import_module(f".commands.{module}", __package__), attribute)
    parent = typer.Typer()
    if isinstance(value, typer.Typer):
        parent.add_typer(value, name=name, rich_help_panel=panel, **options)
    else:
        parent.command(name, rich_help_panel=panel, **options)(value)
    group = typer.main.get_group(parent)
    add_project_ref_callbacks(group)
    # A hidden alias (`mr`, `sl`) gets the operation keys of the command it aliases.
    mount = next(other for other, entry in COMMANDS.items() if entry[0] == target)
    _apply(group.commands[name], (mount,))
    return group.commands[name]


class LazyCommands(UserDict[str, Any]):
    """The top-level commands by name. The first read of a command imports its module."""

    def __getitem__(self, key: str) -> Any:
        if self.data[key] is None:
            self.data[key] = _build_command(key)
        return self.data[key]


class RootGroup(ProjectRefGroup):
    """The root command group: one JSON document for every exit code, root flags anywhere."""

    def __init__(self, **attrs: Any) -> None:
        super().__init__(**attrs)
        apply_command_classes(self)
        self.commands = LazyCommands({**dict.fromkeys(COMMANDS), **self.commands})

    def make_context(
        self, info_name: str | None, args: list[str], parent: Any = None, **extra: Any
    ) -> Any:
        tokens = set(args)  # the parser consumes `args`
        try:
            return super().make_context(info_name, args, parent=parent, **extra)
        except UsageError as exc:
            # A bad root option: no root parameter is parsed yet, so look at the tokens.
            if not ({"--json", "-j"} & tokens):
                raise
            _emit_error(None, exc.format_message(), click_error_code(exc))
            raise typer.Exit(code=exc.exit_code) from None

    def invoke(self, ctx: Any) -> Any:
        for name in _root_flags_after_command(self, ctx):
            ctx.params[name] = True
        try:
            return super().invoke(ctx)
        except MissingCommandError as exc:
            with contextlib.redirect_stdout(sys.stderr):
                help_text = exc.ctx.get_help()
            if help_text:
                sys.stderr.write(help_text + "\n")
            if ctx.params.get("json_output"):
                _emit_error(
                    ctx,
                    f"{exc.message} Run '{exc.ctx.command_path} --help' for the usage.",
                    ErrorCode.USAGE_ERROR,
                )
            raise typer.Exit(code=exc.exit_code) from None
        except (typer.Exit, SystemExit) as exc:
            # A non-zero exit that wrote no document still writes one (O1).
            code = exc.exit_code if isinstance(exc, typer.Exit) else exc.code
            if ctx.params.get("json_output") and code == 130:
                _emit_error(ctx, "The command was interrupted.", ErrorCode.INTERRUPTED)
            elif ctx.params.get("json_output") and code not in (0, None):
                _emit_error(ctx, f"The command exited with code {code}.", ErrorCode.UNKNOWN_ERROR)
            raise
        except (typer.Abort, KeyboardInterrupt):
            # Typer prints "Aborted!" (exit 1), or turns Ctrl+C into exit 130.
            if ctx.params.get("json_output"):
                _emit_error(ctx, "The command was interrupted.", ErrorCode.INTERRUPTED)
            raise
        except _click.ClickException as exc:
            if not ctx.params.get("json_output"):
                raise
            _emit_error(ctx, exc.format_message(), click_error_code(exc))
            raise typer.Exit(code=exc.exit_code) from None
        except Exception as exc:
            if not ctx.params.get("json_output"):
                raise
            telemetry.note_command_error(f"{type(exc).__name__}: {exc}")
            traceback.print_exception(exc, file=sys.stderr)
            _emit_error(
                ctx, f"Unexpected error: {type(exc).__name__}: {exc}", ErrorCode.INTERNAL_ERROR
            )
            raise typer.Exit(code=1) from None


def click_error_code(exc: Any) -> str:
    """The error code of a Click exception under ``--json``.

    A bad value of a well-formed option (``BadParameter``) is ``INVALID_ARGUMENT``
    (I4), the same code the commands use for a bad value. A missing required
    option (``MissingParameter``, a ``BadParameter`` subclass) and every other
    usage error are ``USAGE_ERROR``. Any other Click exception is
    ``UNKNOWN_ERROR``.
    """
    if isinstance(exc, BadParameter) and not isinstance(exc, MissingParameter):
        return ErrorCode.INVALID_ARGUMENT
    if isinstance(exc, UsageError):
        return ErrorCode.USAGE_ERROR
    return ErrorCode.UNKNOWN_ERROR


def _emit_error(ctx: Any, message: str, error_code: str) -> None:
    """The error envelope on stdout, unless the command already wrote its document."""
    obj = ctx.obj if ctx is not None and isinstance(ctx.obj, dict) else {}
    formatter = obj.get("formatter")
    if formatter is None or not formatter.json_mode:
        formatter = OutputFormatter(json_mode=True)
    # After NDJSON lines, `error()` adds one more line, not a second document.
    if formatter.streaming or not formatter.document_written:
        formatter.error(message=message, error_code=error_code)


def _root_flags_after_command(root: Any, ctx: Any) -> set[str]:
    """Root parameters given after the subcommand, read with Click's own parser.

    Walks the groups to the command the arguments name and parses each level
    with its parser, so an option value equal to ``--json`` stays a value. A
    parse error ends the walk; the real parse reports it, and a ``--json`` or
    ``-j`` among the remaining tokens still selects the JSON error envelope.
    """
    found: set[str] = set()
    node, parent, args = root, ctx, [*protected_args(ctx), *ctx.args]
    while isinstance(node, TyperGroup) and args:
        name, *args = args
        sub = node.get_command(parent, name)
        if sub is None:
            break
        sub_ctx = sub.context_class(sub, info_name=name, parent=parent)
        tokens = set(args)  # the parser consumes the list it gets
        try:
            opts, args, _ = sub.make_parser(sub_ctx).parse_args(args=list(args))
        except UsageError:
            if {"--json", "-j"} & tokens:
                found.add("json_output")
            break
        found |= {name for name in ROOT_FLAGS if opts.get(f"{_COPY_PREFIX}{name}")}
        node, parent = sub, sub_ctx
    return found


def apply_command_classes(root: Any) -> None:
    """Give every group and command under ``root`` its class and the hidden root flags.

    A hidden top-level group whose callback is the callback of a visible group
    (``mr``, ``sl``) is an alias: its commands get the operation keys of the
    visible group, as its permission check does.
    """
    canonical = {
        inspect.unwrap(group.callback): name
        for name, group in root.commands.items()
        if isinstance(group, TyperGroup) and not group.hidden and group.callback
    }
    for name, command in root.commands.items():
        callback = inspect.unwrap(command.callback) if command.callback else None
        mount = canonical.get(callback, name) if getattr(command, "hidden", False) else name
        _apply(command, (mount,))


def _apply(command: Any, path: tuple[str, ...]) -> None:
    if isinstance(command, LeafCommand | CommandGroup):
        return  # already done (a command object mounted twice)
    if type(command) not in (TyperGroup, TyperCommand):
        # Replacing the class would drop the behavior of a custom `cls=`.
        raise TypeError(
            f"'{' '.join(path)}' has the custom command class {type(command).__name__}; "
            "the root command class cannot replace it. Subclass LeafCommand or "
            "CommandGroup from root_command.py instead."
        )
    command.params.extend(_root_flag_copies())
    if isinstance(command, TyperGroup):
        command.__class__ = CommandGroup
        for name, sub in command.commands.items():
            _apply(sub, (*path, name))
        return
    command.__class__ = LeafCommand
    command.operation = ".".join(path)
