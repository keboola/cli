#!/usr/bin/env python3
"""Generate command-reference.md by introspecting the live Typer app.

Zero-drift by construction: the output is derived from the same Click command
tree that renders ``--help``, so it cannot disagree with the shipped CLI. The
release workflow attaches the result as a GitHub Release asset next to the
wheel; downstream consumers (help.keboola.com CLI section, the Claude Code
plugin, third parties) fetch it pinned to a released version instead of
hand-copying command lists. See issue #498 and CONTRIBUTING.md
"Plugin synchronization map" for the drift problem this solves.

Hidden commands and groups (e.g. the ``sl`` alias) are skipped together with
their subtree, matching scripts/check_command_sync.py -- they are not part of
the public documented surface.

Metavar column contract (STABLE -- do not let it drift under a dependency bump):
    Value-taking options render as ``| `--flag` `<type>` | required | help |``.
    The metavar span is ALWAYS wrapped in angle brackets (``<str>``, ``<int>``,
    ``<path>``, ``<a|b|c>`` for choices); flags carry no metavar span at all.
    This is derived from Click's version-stable ``ParamType.name`` (see
    ``_stable_option_metavar``), NOT from ``make_metavar()``, whose default
    drifted between releases (bare ``TEXT`` at Click 8.x vs ``<str>`` later).
    A downstream consumer -- the connection-docs freshness gate
    (keboola/connection-docs#1037) -- detects value-taking options by matching
    ``/^<.*>$/`` on this span, so the shape is a published contract. See issue
    #513; ``tests/test_gen_command_reference.py`` fails CI if it drifts.

Usage (run from repo root):
    python scripts/gen_command_reference.py                    # print to stdout
    python scripts/gen_command_reference.py --output PATH      # write to file
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import click
import typer.main

from keboola_agent_cli import __version__
from keboola_agent_cli.cli import app
from keboola_agent_cli.commands.repl import _is_group

PROG = "kbagent"

# NOTE on typing: Typer >=0.25 vendors Click (typer._click), so isinstance
# checks against the plain `click` classes FAIL for the objects returned by
# typer.main.get_command(). Everything below is duck-typed, following the
# established pattern of commands/repl.py::_is_group. Parameters are told
# apart via `param_type_name` ("option" / "argument"), which both Click
# lineages set.


def _short_help(cmd: click.Command, limit: int = 120) -> str:
    return cmd.get_short_help_str(limit=limit)


def _metavar(param: click.Parameter, ctx: click.Context) -> str:
    """make_metavar across Click lineages (newer requires ctx, older forbids it)."""
    try:
        return param.make_metavar(ctx)  # type: ignore[call-arg]
    except TypeError:
        return param.make_metavar()  # ty: ignore[missing-argument]


# Click's ``ParamType.name`` is a version-stable token; ``make_metavar()`` output
# is not (see module docstring). Map the stable name to the documented metavar so
# the reference-asset contract can't drift under a Typer/Click bump.
_METAVAR_BY_TYPE_NAME: dict[str, str] = {
    "text": "str",
    "integer": "int",
    "integer range": "int",
    "float": "float",
    "float range": "float",
    "path": "path",
    "filename": "path",
    "file": "file",
    "boolean": "bool",
    "uuid": "uuid",
    "datetime": "datetime",
}


def _stable_option_metavar(param: click.Parameter) -> str:
    """Angle-bracket-wrapped, Click-version-independent metavar for a value option.

    Choices keep their literal case (``<admin|guest|readOnly|share>``) -- they are
    real CLI tokens. Scalars map through ``ParamType.name``; an author-set metavar
    wins and is lowercased. Always returns a ``<...>`` span (never empty, never a
    bare uppercase ``TEXT``), which is the shape the downstream docs gate matches.
    """
    explicit = getattr(param, "metavar", None)
    if explicit:
        return f"<{explicit.strip('<>[] ').lower()}>"
    ptype = param.type
    choices = getattr(ptype, "choices", None)
    if choices:
        return f"<{'|'.join(str(choice) for choice in choices)}>"
    type_name = getattr(ptype, "name", None) or "text"
    inner = _METAVAR_BY_TYPE_NAME.get(type_name, type_name.replace(" ", "-"))
    return f"<{inner}>"


def _format_param(param: click.Parameter, ctx: click.Context) -> str | None:
    """Render one parameter as a markdown table row, or None if hidden/help."""
    kind = getattr(param, "param_type_name", "")
    if kind == "option":
        if getattr(param, "hidden", False) or "--help" in param.opts:
            return None
        names = " / ".join(f"`{opt}`" for opt in [*param.opts, *param.secondary_opts])
        metavar = "" if getattr(param, "is_flag", False) else f" `{_stable_option_metavar(param)}`"
        required = "yes" if param.required else ""
        help_text = (getattr(param, "help", "") or "").replace("\n", " ").strip()
        return f"| {names}{metavar} | {required} | {help_text} |"
    if kind == "argument":
        required = "yes" if param.required else ""
        return f"| `{_metavar(param, ctx)}` (positional) | {required} | |"
    return None


def _render_command(path: tuple[str, ...], cmd: click.Command, ctx: click.Context) -> list[str]:
    lines = [f"### `{PROG} {' '.join(path)}`", ""]
    help_line = _short_help(cmd)
    if help_line:
        lines += [help_line, ""]
    rows = [row for p in cmd.params for row in [_format_param(p, ctx)] if row]
    if rows:
        lines += ["| Option | Required | Description |", "|---|---|---|", *rows, ""]
    return lines


@dataclass(frozen=True)
class LeafCommand:
    """One visible leaf command with the context it renders under."""

    path: tuple[str, ...]
    command: click.Command
    ctx: click.Context


def _walk(
    group: click.Group, ctx: click.Context, prefix: tuple[str, ...] = ()
) -> list[LeafCommand]:
    """Collect every visible leaf command, depth-first."""
    leaves: list[LeafCommand] = []
    for name in group.list_commands(ctx):
        cmd = group.get_command(ctx, name)
        if cmd is None or cmd.hidden:
            continue
        path = (*prefix, name)
        if _is_group(cmd):
            with click.Context(cmd, parent=ctx, info_name=name) as sub_ctx:
                leaves.extend(_walk(cmd, sub_ctx, path))
        else:
            leaves.append(LeafCommand(path=path, command=cmd, ctx=ctx))
    return leaves


def build_reference() -> str:
    click_app = typer.main.get_command(app)
    assert _is_group(click_app), "root Typer app did not resolve to a command group"
    out: list[str] = [
        f"# {PROG} command reference",
        "",
        f"Generated from {PROG} v{__version__} by `scripts/gen_command_reference.py`.",
        "Derived from the CLI's own command tree -- do not edit by hand.",
        "",
        "## Global options",
        "",
        "Available on every command:",
        "",
    ]
    with click.Context(click_app, info_name=PROG) as root_ctx:
        root_rows = [row for p in click_app.params for row in [_format_param(p, root_ctx)] if row]
        out += ["| Option | Required | Description |", "|---|---|---|", *root_rows, ""]

        top_level: list[list[str]] = []
        for name in click_app.list_commands(root_ctx):
            cmd = click_app.get_command(root_ctx, name)
            if cmd is None or cmd.hidden:
                continue
            if _is_group(cmd):
                out += [f"## `{name}`", ""]
                group_help = _short_help(cmd)
                if group_help:
                    out += [group_help, ""]
                with click.Context(cmd, parent=root_ctx, info_name=name) as group_ctx:
                    for leaf in _walk(cmd, group_ctx, (name,)):
                        out += _render_command(leaf.path, leaf.command, leaf.ctx)
            else:
                top_level += [_render_command((name,), cmd, root_ctx)]
        if top_level:
            out += ["## Top-level commands", ""]
            for block in top_level:
                out += block
    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=None, help="Write to file (default stdout)")
    args = parser.parse_args()

    reference = build_reference()
    if args.output:
        args.output.write_text(reference, encoding="utf-8")
        commands = reference.count(f"### `{PROG} ")
        print(f"Wrote {args.output} ({commands} commands, v{__version__})")
    else:
        sys.stdout.write(reference)
    return 0


if __name__ == "__main__":
    sys.exit(main())
