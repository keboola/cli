#!/usr/bin/env python3
"""CI gate: the command-line interface follows its spec (`make cli-spec-check`).

Walks the live Typer command tree, the permission and serve registries and the
source of `commands/`, evaluates every checkable rule of the CLI interface spec
(keboola/cli#791), and compares the result with the committed baseline
`scripts/cli_spec_baseline.json`.

Ratchet (per rule; the baseline holds a `blocking` flag for every rule):
  - new violation (in neither `violations` nor `exceptions`): FAIL;
  - stale entry (listed, no longer violating): FAIL, so the baseline shrinks;
  - unknown rule id, or a rule without a `blocking` flag: FAIL;
  - with --base-baseline (the baseline of the base branch), for a blocking rule:
    more `violations` entries than the base (counted per rule, so a renamed
    command or a moved function is no growth), a subject added to `exceptions`
    (unless --allow-new-exceptions), a rule changed from blocking to
    non-blocking, or a rule of the base that is missing: FAIL.
  A non-blocking rule prints the same lines as NOTE and never fails.

`help.manifest-fresh` takes no baseline: `docs/cli-manifest.json` must always
equal the manifest generated from the live tree (`make cli-manifest`).

Subjects: tree rules use the command path as a user types it; option rules
append the first long option (`sync pull --force`), short-flag rules the short
flag (`http post -d`), argument rules the metavar; REST rules use
`METHOD /path`; source rules use `<module>:<function>` relative to the package
(`commands/flow.py:flow_delete`), so that a moved line is not a new violation.

Rules are run on the canonical tree only. A hidden group whose callback is the
callback of a visible group is an alias (`mr`, `sl`); it is compared with its
visible group (`naming.alias-parity`) and is otherwise skipped.

Usage (from the repository root, with the `server` extra):
    python scripts/check_cli_spec.py                       # the gate
    python scripts/check_cli_spec.py --base-baseline OLD   # also fail on growth
    python scripts/check_cli_spec.py --details RULE        # every subject of one rule
    python scripts/check_cli_spec.py --list-rules          # all rule ids and status
    python scripts/check_cli_spec.py --report              # per-command inventory
    python scripts/check_cli_spec.py --write-baseline      # `make cli-spec-baseline`
    python scripts/check_cli_spec.py --write-manifest      # `make cli-manifest`
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import inspect
import json
import re
import sys
import tempfile
import textwrap
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import typer.main

from keboola_agent_cli.cli import app
from keboola_agent_cli.permissions import OPERATION_REGISTRY
from keboola_agent_cli.root_command import is_root_flag_copy

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_ROOT = REPO_ROOT / "src" / "keboola_agent_cli"
COMMANDS_DIR = PKG_ROOT / "commands"
CLI_PY = PKG_ROOT / "cli.py"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
BASELINE_PATH = REPO_ROOT / "scripts" / "cli_spec_baseline.json"
MANIFEST_PATH = REPO_ROOT / "docs" / "cli-manifest.json"
SPEC_DOC = "docs/cli-interface-spec.md"

# --------------------------------------------------------------------------- #
# Reviewed data (rule parameters that are decisions, proposal section 7.2)
# --------------------------------------------------------------------------- #

# A2: one short flag = one long option; a new short flag needs an entry here.
# The global short flags (-j, -v, -V) keep their global meaning. The others are
# fixed from the majority use at the introduction of the check.
SHORT_FLAGS: dict[str, str] = {
    "-V": "--version",
    "-a": "--allow",
    "-d": "--directory",
    "-e": "--email",
    "-f": "--feature",
    "-i": "--ignore-case",
    "-j": "--json",
    "-l": "--load",
    "-m": "--message",
    "-o": "--output",
    "-p": "--project",
    "-q": "--query",
    "-t": "--type",
    "-v": "--verbose",
    "-y": "--yes",
}

# A2: short flags whose uses are tied between two meanings (-c: --category and
# --column; -n: --limit and --name; -r: --regex and --role). No new use with
# either meaning; each existing use is an `exceptions` entry in the baseline.
RESERVED_SHORT_FLAGS: frozenset[str] = frozenset({"-c", "-n", "-r"})

# S3: what `--force` overrides, per command (the guards it names in its help).
# A new `--force` fails the blocking rule safety.force-meaning-registered until
# it has an entry here.
FORCE_MEANINGS: dict[str, str] = {
    "project refresh": "legacy meaning: refresh tokens that are still valid too",
    "storage delete-bucket": "the guard for a non-empty bucket (deletes its tables), "
    "never a shared bucket",
    "storage delete-column": "the guard for a column that table aliases reference",
    "storage delete-table": "the guard for a table with aliases in other projects "
    "(deletes the aliases)",
    "stream delete": "none: hidden deprecated alias of --yes until its DEPRECATIONS "
    "remove_after date (C3)",
    "sync pull": "legacy meaning: pull again, keep local changes, abort on a conflict",
    "sync push": "the guard that holds back remote deletions of configs and rows",
    "workspace load": "the size guard that asks before a COPY larger than 1 GB",
}

# N4: hidden alias path -> canonical path. No hidden leaf exists yet.
COMMAND_ALIASES: dict[str, str] = {}

# V9: REST routes that change only the fields in the request (#791 comment),
# and, by decision D10, the two routes that merge by default (variables, secrets).
PARTIAL_UPDATE_ROUTES: frozenset[str] = frozenset(
    {
        "/semantic-layer/items/{kind}/{name}",
        "/merge-requests/{project}/{merge_request_id}",
        "/configs/{project}/{component_id}/{config_id}/variables",
        "/data-apps/{project}/{app_id}/secrets",
    }
)

# I-rules: file input is `--file` or `--<field>-file` next to `--<field>`.
_INPUT_EXTRA = frozenset({"--from-file", "--stdin", "--from-csv", "--input"})
# S1: leaf verbs that delete; such a leaf is `destructive` or `admin`.
_DESTRUCTIVE_VERBS = (
    "delete",
    "remove",
    "purge",
    "drop",
    "clear",
    "gc",
    "unlink",
    "unshare",
    "deprecate",
)
# T11: words with which a `--project` help states what an omitted value does.
_PROJECT_DEFAULT_WORDS = re.compile(
    r"default|active|pinned|all (registered )?projects|every (registered )?project|"
    r"omit|KBAGENT_PROJECT|if not (given|set)",
    re.IGNORECASE,
)
_COMMA_LIST = re.compile(r"comma[- ]separated|comma-delimited", re.IGNORECASE)

# --------------------------------------------------------------------------- #
# Tree model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Leaf:
    path: tuple[str, ...]
    category: str | None
    params: tuple[Any, ...]
    callback: Any
    help: str
    hidden: bool

    @property
    def name(self) -> str:
        return " ".join(self.path)


@dataclass(frozen=True)
class Group:
    path: tuple[str, ...]
    callback: Any
    node: Any


@dataclass
class Tree:
    root: Any
    leaves: list[Leaf]
    groups: list[Group]
    aliases: dict[str, str | None] = field(default_factory=dict)
    alias_nodes: dict[str, Any] = field(default_factory=dict)
    by_name: dict[str, Leaf] = field(default_factory=dict)

    @property
    def root_opts(self) -> set[str]:
        return {o for p in self.root.params for o in all_opts(p)}


def is_group(cmd: object) -> bool:
    return callable(getattr(cmd, "list_commands", None))


def children(node: Any) -> list[tuple[str, Any]]:
    ctx = type(node).context_class(node)
    found = []
    for name in node.list_commands(ctx):
        sub = node.get_command(ctx, name)
        if sub is not None:
            found.append((name, sub))
    return found


def _unwrap(fn: Any) -> Any:
    return inspect.unwrap(fn) if fn is not None else None


def _walk(tree: Tree, node: Any, prefix: tuple[str, ...]) -> None:
    for name, sub in children(node):
        path = (*prefix, name)
        if is_group(sub):
            if getattr(sub, "hidden", False) and not prefix:
                continue  # an alias group; load_tree() records it
            tree.groups.append(Group(path, _unwrap(sub.callback), sub))
            _walk(tree, sub, path)
        else:
            tree.leaves.append(
                Leaf(
                    path=path,
                    category=OPERATION_REGISTRY.get(".".join(path)),
                    # The hidden copies of the root flags (O7) are not options of the command.
                    params=tuple(p for p in sub.params if not is_root_flag_copy(p)),
                    callback=_unwrap(sub.callback),
                    help=(sub.help or "").strip(),
                    hidden=bool(getattr(sub, "hidden", False)),
                )
            )


def load_tree() -> Tree:
    root = typer.main.get_command(app)
    tree = Tree(root=root, leaves=[], groups=[])
    _walk(tree, root, ())
    tree.by_name = {leaf.name: leaf for leaf in tree.leaves}
    visible_top = {g.path[0]: g for g in tree.groups if len(g.path) == 1}
    for name, sub in children(root):
        if not (getattr(sub, "hidden", False) and is_group(sub)):
            continue
        callback = _unwrap(sub.callback)
        tree.aliases[name] = next(
            (v for v, g in visible_top.items() if callback is not None and g.callback is callback),
            None,
        )
        tree.alias_nodes[name] = sub
    return tree


def visible_leaves(tree: Tree) -> list[Leaf]:
    """Leaves of the documented command tree (what scripts/check_command_sync.py walks)."""
    return [lf for lf in tree.leaves if not lf.hidden]


# --------------------------------------------------------------------------- #
# Parameter helpers
# --------------------------------------------------------------------------- #


def is_option(p: Any) -> bool:
    return getattr(p, "param_type_name", "") == "option"


def is_argument(p: Any) -> bool:
    return getattr(p, "param_type_name", "") == "argument"


def all_opts(p: Any) -> list[str]:
    return [*p.opts, *getattr(p, "secondary_opts", [])]


def longs(p: Any) -> list[str]:
    return [o for o in all_opts(p) if o.startswith("--")]


def shorts(p: Any) -> list[str]:
    return [o for o in all_opts(p) if o.startswith("-") and not o.startswith("--")]


def opt_names(leaf: Leaf) -> set[str]:
    return {o for p in leaf.params for o in all_opts(p)}


def param_with(leaf: Leaf, opt: str) -> Any | None:
    return next((p for p in leaf.params if opt in all_opts(p)), None)


def label(p: Any) -> str:
    return longs(p)[0] if longs(p) else (p.metavar or p.name.upper())


def type_name(p: Any) -> str:
    name = getattr(p.type, "name", str(p.type))
    return "integer" if name == "integer range" else name


def shape(p: Any) -> str:
    if getattr(p, "is_flag", False):
        return "flag"
    return f"{type_name(p)}{'[]' if getattr(p, 'multiple', False) else ''}"


# --------------------------------------------------------------------------- #
# Source helpers (the `ast` rules)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CallSite:
    module: str  # path relative to the package, e.g. "commands/flow.py"
    function: str  # qualified name of the enclosing function, or "<module>"
    call: ast.Call
    handlers: tuple[ast.ExceptHandler, ...]  # enclosing except clauses, innermost last

    @property
    def subject(self) -> str:
        return f"{self.module}:{self.function}"


def _calls(module: str, tree: ast.AST) -> Iterator[CallSite]:
    def visit(
        node: ast.AST, scope: tuple[str, ...], handlers: tuple[ast.ExceptHandler, ...]
    ) -> Iterator[CallSite]:
        for child in ast.iter_child_nodes(node):
            inner_scope, inner_handlers = scope, handlers
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                inner_scope = (*scope, child.name)
            if isinstance(child, ast.ExceptHandler):
                inner_handlers = (*handlers, child)
            if isinstance(child, ast.Call):
                yield CallSite(module, ".".join(scope) or "<module>", child, handlers)
            yield from visit(child, inner_scope, inner_handlers)

    yield from visit(tree, (), ())


def modules(root: Path) -> list[tuple[str, ast.Module]]:
    """Every module under ``root``: its path relative to the package and its AST."""
    return [
        (path.relative_to(PKG_ROOT).as_posix(), ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(root.rglob("*.py"))
    ]


def call_sites(root: Path) -> list[CallSite]:
    sites: list[CallSite] = []
    for path in sorted(root.rglob("*.py")):
        module = path.relative_to(PKG_ROOT).as_posix()
        sites.extend(_calls(module, ast.parse(path.read_text(encoding="utf-8"))))
    return sites


def dotted(node: ast.AST) -> str:
    """`typer.echo` for `typer.echo(...)`, `formatter.error` for `formatter.error(...)`."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #

Violations = dict[str, str]  # subject -> one-line explanation


@dataclass
class Context:
    tree: Tree
    _cache: dict[str, Any] = field(default_factory=dict)

    def cached(self, key: str, build: Callable[[], Any]) -> Any:
        if key not in self._cache:
            self._cache[key] = build()
        return self._cache[key]

    @property
    def command_calls(self) -> list[CallSite]:
        return self.cached("command_calls", lambda: call_sites(COMMANDS_DIR))

    @property
    def command_modules(self) -> list[tuple[str, ast.Module]]:
        return self.cached("command_modules", lambda: modules(COMMANDS_DIR))

    @property
    def package_calls(self) -> list[CallSite]:
        return self.cached("package_calls", lambda: call_sites(PKG_ROOT))

    @property
    def routes(self) -> list[Any]:
        return self.cached("routes", _load_routes)


@dataclass(frozen=True)
class Rule:
    id: str
    kind: str  # "tree" or "ast"
    text: str
    evaluate: Callable[[Context], Violations]
    baseline: bool = True  # False: a violation always fails (no baseline entry)


def r_destructive_dry_run(c: Context) -> Violations:
    return {
        lf.name: "destructive command without --dry-run"
        for lf in c.tree.leaves
        if lf.category == "destructive" and "--dry-run" not in opt_names(lf)
    }


def r_destructive_yes(c: Context) -> Violations:
    found = {}
    for lf in c.tree.leaves:
        if lf.category == "destructive" and "--yes" not in opt_names(lf):
            extra = " (--force does not count)" if "--force" in opt_names(lf) else ""
            found[lf.name] = f"destructive command without --yes{extra}"
    return found


def r_command_help(c: Context) -> Violations:
    return {lf.name: "empty command help" for lf in c.tree.leaves if not lf.help}


def r_param_help(c: Context) -> Violations:
    found = {}
    for lf in c.tree.leaves:
        for p in lf.params:
            if not getattr(p, "hidden", False) and not (getattr(p, "help", None) or "").strip():
                found[f"{lf.name} {label(p)}"] = "parameter without help text"
    return found


def _top_level_guarded() -> set[str]:
    """The `_top_level_commands` set that cli.py checks permissions for."""
    for node in ast.walk(ast.parse(CLI_PY.read_text(encoding="utf-8"))):
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "_top_level_commands" for t in node.targets)
            and isinstance(node.value, ast.Set)
        ):
            return {
                e.value
                for e in node.value.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            }
    return set()


_PERM_CALL = re.compile(r"check_cli_permission\(\s*ctx\s*,\s*\"([^\"]+)\"\s*\)")


def _group_guard_literal(g: Group) -> str | None:
    if g.callback is None:
        return None
    try:
        source = inspect.getsource(g.callback)
    except (OSError, TypeError):
        return None
    match = _PERM_CALL.search(source)
    return match.group(1) if match else None


def r_permission_literal(c: Context) -> Violations:
    found = {}
    for g in c.tree.groups:
        literal, want = _group_guard_literal(g), ".".join(g.path)
        if literal is not None and literal != want:
            found[" ".join(g.path)] = f'callback checks "{literal}", the group is "{want}"'
    return found


def r_runtime_guard(c: Context) -> Violations:
    top = _top_level_guarded()
    guarded = {g.path: _group_guard_literal(g) for g in c.tree.groups}
    found = {}
    for lf in c.tree.leaves:
        if len(lf.path) == 1:
            if lf.path[0] not in top:
                found[lf.name] = "top-level command missing from cli.py _top_level_commands"
            continue
        runtime_key = None
        for k in range(len(lf.path) - 1, 0, -1):
            if guarded.get(lf.path[:k]) == ".".join(lf.path[:k]):
                runtime_key = ".".join(lf.path[: k + 1])
                break
        if runtime_key is None:
            found[lf.name] = "no group callback calls check_cli_permission"
            continue
        got = OPERATION_REGISTRY.get(runtime_key, "write")
        if got != lf.category:
            found[lf.name] = f'the check uses "{runtime_key}" ({got}), the command is {lf.category}'
    return found


_KEBAB_OPT = re.compile(r"^--[a-z0-9]+(-[a-z0-9]+)*$")
_KEBAB_CMD = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def r_kebab(c: Context) -> Violations:
    found = {}
    for lf in c.tree.leaves:
        for seg in lf.path:
            if not _KEBAB_CMD.match(seg):
                found[lf.name] = f"command segment {seg!r} is not kebab-case"
        for p in lf.params:
            for o in longs(p):
                if not _KEBAB_OPT.match(o):
                    found[f"{lf.name} {o}"] = "option name is not kebab-case"
    return found


def _signature(node: Any) -> list[tuple[str, tuple[str, ...]]]:
    sig: list[tuple[str, tuple[str, ...]]] = []

    def rec(n: Any, prefix: tuple[str, ...]) -> None:
        for name, sub in children(n):
            if is_group(sub):
                rec(sub, (*prefix, name))
            else:
                opts = tuple(sorted(o for p in sub.params for o in all_opts(p)))
                sig.append((" ".join((*prefix, name)), opts))

    rec(node, ())
    return sorted(sig)


def r_alias_parity(c: Context) -> Violations:
    found = {}
    visible = {g.path[0]: g for g in c.tree.groups if len(g.path) == 1}
    for alias, target in c.tree.aliases.items():
        if target is None:
            found[alias] = "hidden group is not the app of any visible group"
        elif _signature(c.tree.alias_nodes[alias]) != _signature(visible[target].node):
            found[alias] = f"alias commands or options differ from {target}"
    return found


def r_hidden_leaf_canonical(c: Context) -> Violations:
    return {
        lf.name: "hidden command without a COMMAND_ALIASES entry"
        for lf in c.tree.leaves
        if lf.hidden and lf.name not in COMMAND_ALIASES
    }


def r_short_one_meaning(c: Context) -> Violations:
    found = {}
    for lf in c.tree.leaves:
        for p in filter(is_option, lf.params):
            for s in shorts(p):
                want = SHORT_FLAGS.get(s)
                if s in RESERVED_SHORT_FLAGS:
                    found[f"{lf.name} {s}"] = f"{s} is a reserved short flag (no new use)"
                elif want is None:
                    found[f"{lf.name} {s}"] = f"{s} is not in SHORT_FLAGS"
                elif label(p) != want:
                    found[f"{lf.name} {s}"] = f"{s} means {label(p)} here, {want} in SHORT_FLAGS"
    return found


def r_no_global_shadow(c: Context) -> Violations:
    root = c.tree.root_opts
    return {
        f"{lf.name} {o}": f"{o} is also a global option"
        for lf in c.tree.leaves
        for p in lf.params
        for o in all_opts(p)
        if o in root
    }


def r_long_one_shape(c: Context) -> Violations:
    """A1: one long name has one shape inside its top-level command group."""
    shapes: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for lf in c.tree.leaves:
        for p in filter(is_option, lf.params):
            shapes[(lf.path[0], label(p))][shape(p)] += 1
    found = {}
    for lf in c.tree.leaves:
        for p in filter(is_option, lf.params):
            counts = shapes[(lf.path[0], label(p))]
            canon = min(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
            if len(counts) > 1 and shape(p) != canon:
                found[f"{lf.name} {label(p)}"] = (
                    f"{label(p)} is {shape(p)} here, {canon} elsewhere in {lf.path[0]}"
                )
    return found


def r_force_registered(c: Context) -> Violations:
    return {
        f"{lf.name} --force": "--force without a FORCE_MEANINGS entry"
        for lf in c.tree.leaves
        if "--force" in opt_names(lf) and lf.name not in FORCE_MEANINGS
    }


def r_yes_short_y(c: Context) -> Violations:
    found = {}
    for lf in c.tree.leaves:
        p = param_with(lf, "--yes")
        if p is not None and "-y" not in all_opts(p):
            found[f"{lf.name} --yes"] = "--yes without -y"
    return found


def r_verb_category(c: Context) -> Violations:
    found = {}
    for lf in c.tree.leaves:
        hit = [v for v in _DESTRUCTIVE_VERBS if re.search(rf"(^|-){v}($|-)", lf.path[-1])]
        if hit and lf.category not in ("destructive", "admin"):
            found[lf.name] = f"verb '{hit[0]}' but category {lf.category}"
    return found


def r_file_vocabulary(c: Context) -> Violations:
    found = {}
    for lf in c.tree.leaves:
        names = opt_names(lf)
        for p in filter(is_option, lf.params):
            o = label(p)
            if getattr(p, "is_flag", False) or o.startswith(("--output", "--no-")):
                continue
            if o in _INPUT_EXTRA:
                found[f"{lf.name} {o}"] = "input option outside the file-input vocabulary"
            elif o.endswith("-file") and o != "--file" and o[: -len("-file")] not in names:
                found[f"{lf.name} {o}"] = f"{o} has no inline option {o[: -len('-file')]}"
    return found


def r_no_bare_id(c: Context) -> Violations:
    return {
        f"{lf.name} --id": "generic --id; name the object (--task-id)"
        for lf in c.tree.leaves
        if "--id" in opt_names(lf)
    }


def r_no_positional(c: Context) -> Violations:
    return {
        f"{lf.name} {label(p)}": "positional argument"
        for lf in c.tree.leaves
        for p in lf.params
        if is_argument(p)
    }


def r_list_limit(c: Context) -> Violations:
    def is_list(lf: Leaf) -> bool:
        last = lf.path[-1]
        return last == "list" or last.endswith("-list") or last.startswith("list-")

    return {
        lf.name: "list command without --limit"
        for lf in c.tree.leaves
        if is_list(lf) and "--limit" not in opt_names(lf)
    }


def r_write_dry_run(c: Context) -> Violations:
    return {
        lf.name: "write command without --dry-run"
        for lf in c.tree.leaves
        if lf.category == "write" and "--dry-run" not in opt_names(lf)
    }


def r_list_option_repeatable(c: Context) -> Violations:
    return {
        f"{lf.name} {label(p)}": "list option that takes one comma-separated string"
        for lf in c.tree.leaves
        for p in filter(is_option, lf.params)
        if not getattr(p, "multiple", False)
        and not getattr(p, "is_flag", False)
        and _COMMA_LIST.search(getattr(p, "help", None) or "")
    }


def r_project_help_default(c: Context) -> Violations:
    found = {}
    for lf in c.tree.leaves:
        p = param_with(lf, "--project")
        if p is None or (p.required and not getattr(p, "multiple", False)):
            continue
        if not _PROJECT_DEFAULT_WORDS.search(getattr(p, "help", None) or ""):
            found[f"{lf.name} --project"] = "help does not say what an omitted --project does"
    return found


def r_no_write_fanout(c: Context) -> Violations:
    found = {}
    for lf in c.tree.leaves:
        p = param_with(lf, "--project")
        if (
            lf.category in ("write", "destructive")
            and p is not None
            and getattr(p, "multiple", False)
            and not p.required
            and "--all-projects" not in opt_names(lf)
        ):
            found[lf.name] = f"{lf.category} command fans out without --all-projects"
    return found


def r_dead_parameter(c: Context) -> Violations:
    found = {}
    for lf in c.tree.leaves:
        try:
            source = textwrap.dedent(inspect.getsource(lf.callback))
        except (OSError, TypeError):
            continue
        func = ast.parse(source).body[0]
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        used = {n.id for stmt in func.body for n in ast.walk(stmt) if isinstance(n, ast.Name)}
        for p in lf.params:
            if p.name not in used:
                found[f"{lf.name} {label(p)}"] = f"parameter {p.name!r} is never read"
    return found


def r_single_parser(c: Context) -> Violations:
    return {
        site.subject: 'parses "@" values outside parse_json_arg'
        for site in c.package_calls
        if isinstance(site.call.func, ast.Attribute)
        and site.call.func.attr == "startswith"
        and site.call.args
        and isinstance(site.call.args[0], ast.Constant)
        and site.call.args[0].value == "@"
        and not site.function.endswith("parse_json_arg")
    }


def r_forward_retryable(c: Context) -> Violations:
    found = {}
    for site in c.package_calls:
        code = keyword(site.call, "error_code")
        if (
            dotted(site.call.func).endswith(".error")
            and isinstance(code, ast.Attribute)
            and code.attr == "error_code"
            and keyword(site.call, "retryable") is None
        ):
            found[site.subject] = "forwards exc.error_code without retryable="
    return found


def _catches_config_error(handler: ast.ExceptHandler) -> bool:
    caught = handler.type
    names = caught.elts if isinstance(caught, ast.Tuple) else [caught] if caught else []
    return any(dotted(n).split(".")[-1] == "ConfigError" for n in names)


def r_config_error_keeps_code(c: Context) -> Violations:
    found = {}
    for site in c.package_calls:
        code = keyword(site.call, "error_code")
        if (
            site.handlers
            and _catches_config_error(site.handlers[-1])
            and dotted(site.call.func).endswith(".error")
            and isinstance(code, ast.Attribute)
            and code.attr == "CONFIG_ERROR"
        ):
            found[site.subject] = (
                "a ConfigError handler replaces the error code with CONFIG_ERROR; pass "
                'error_code=getattr(exc, "error_code", ErrorCode.CONFIG_ERROR)'
            )
    return found


def r_code_has_type_and_exit(c: Context) -> Violations:
    """`scripts/check_error_codes.py` owns the rule. This reports it in the spec check too."""
    path = Path(__file__).with_name("check_error_codes.py")
    spec = importlib.util.spec_from_file_location("_check_error_codes_for_spec", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.code_table_violations()


def r_confirm_via_helper(c: Context) -> Violations:
    return {
        site.subject: "direct confirmation prompt"
        for site in c.command_calls
        if dotted(site.call.func) in ("typer.confirm", "click.confirm")
        and site.subject != "commands/_helpers.py:_confirm_or_abort"
    }


_DIRECT_PRINT = {
    "print": "print",
    "typer.echo": "typer.echo",
    "typer.secho": "typer.secho",
    "click.echo": "click.echo",
    "click.secho": "click.secho",
    "sys.stdout.write": "sys.stdout.write",
    "sys.stderr.write": "sys.stderr.write",
    "Console": "Console()",
    "rich.console.Console": "Console()",
}


def r_no_direct_print(c: Context) -> Violations:
    found = {}
    for site in c.command_calls:
        kind = _DIRECT_PRINT.get(dotted(site.call.func))
        if kind is not None:
            found[f"{site.subject} {kind}"] = f"{kind} in commands/ bypasses OutputFormatter"
    return found


# `env.registry-complete` (EV1): reads of the environment in src/.
_ENV_GETTERS = frozenset({"environ.get", "environ.pop", "os.getenv", "getenv"})
_ENV_READ_HELPERS = frozenset({"_env_flag_set", "_env_truthy"})


def _env_constants() -> dict[str, str]:
    """``ENV_*`` names bound to a string literal at module level in src/, with import aliases."""
    found: dict[str, str] = {}
    aliases: dict[str, str] = {}
    for _, mod in modules(PKG_ROOT):
        for node in mod.body:
            target = value = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign):
                target, value = node.target, node.value
            if (
                isinstance(target, ast.Name)
                and "ENV" in target.id
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                found[target.id] = value.value
        for node in ast.walk(mod):
            if isinstance(node, ast.ImportFrom):
                aliases.update({a.asname: a.name for a in node.names if a.asname})
    for alias, name in aliases.items():
        if name in found:
            found.setdefault(alias, found[name])
    return found


def _env_name(
    node: ast.expr | None, constants: dict[str, str], local: dict[str, ast.expr]
) -> str | None:
    """The variable name an expression stands for, or None when it is computed at run time.

    An f-string that starts with a known name gives a family: ``f"{ENV_X}_{alias}"``
    is ``"X_<"``, which matches a registry entry ``X_<ALIAS>``. A local name is
    followed to its assignment in the same function.
    """
    if isinstance(node, ast.Name) and node.id in local:
        node = local[node.id]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    if isinstance(node, ast.JoinedStr):
        prefix = ""
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                prefix += part.value
            elif isinstance(part, ast.FormattedValue) and isinstance(part.value, ast.Name):
                known = constants.get(part.value.id)
                if known is None:
                    break
                prefix += known
            else:
                break
        else:
            return prefix
        return f"{prefix}<" if prefix else None
    return None


def _scoped_nodes(tree: ast.AST) -> Iterator[tuple[str, dict[str, ast.expr], ast.AST]]:
    """Every node with its enclosing function and the local assignments of that function."""

    def visit(
        node: ast.AST, scope: tuple[str, ...], local: dict[str, ast.expr]
    ) -> Iterator[tuple[str, dict[str, ast.expr], ast.AST]]:
        for child in ast.iter_child_nodes(node):
            inner_scope, inner_local = scope, local
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                inner_scope = (*scope, child.name)
                inner_local = {
                    n.targets[0].id: n.value
                    for n in ast.walk(child)
                    if isinstance(n, ast.Assign)
                    and len(n.targets) == 1
                    and isinstance(n.targets[0], ast.Name)
                }
            yield ".".join(scope) or "<module>", local, child
            yield from visit(child, inner_scope, inner_local)

    yield from visit(tree, (), {})


def _is_environ(node: ast.AST) -> bool:
    return dotted(node).split(".")[-1] == "environ"


def _env_read_arg(node: ast.AST) -> tuple[str, ast.expr | None] | None:
    """(kind, name expression) when ``node`` reads the environment, else None.

    Reads: ``os.environ.get``/``pop``, ``os.getenv``, an ``envvar=`` keyword,
    a call of an ``_ENV_READ_HELPERS`` function, ``os.environ[X]`` (not as an
    assignment target) and ``X in os.environ`` / ``X not in os.environ``.
    """
    if isinstance(node, ast.Call):
        func = dotted(node.func)
        if any(func == g or func.endswith(f".{g}") for g in _ENV_GETTERS) or (
            func in _ENV_READ_HELPERS
        ):
            return func, node.args[0] if node.args else None
        if keyword(node, "envvar") is not None:
            return "envvar=", keyword(node, "envvar")
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.ctx, ast.Load)
        and _is_environ(node.value)
    ):
        return "os.environ[]", node.slice
    if (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.In | ast.NotIn)
        and _is_environ(node.comparators[0])
    ):
        return "in os.environ", node.left
    return None


def _env_reads(c: Context) -> tuple[dict[str, str], dict[str, str]]:
    """(variable name -> first reader, reader subject -> what) for every read in src/.

    See :func:`_env_read_arg` for what counts as a read. The body of an
    ``_ENV_READ_HELPERS`` function is skipped: its callers name the variable.
    A read whose name is computed at run time goes to the second map under its
    function.
    """
    constants = c.cached("env_constants", _env_constants)
    named: dict[str, str] = {}
    dynamic: dict[str, str] = {}
    for module, mod in c.cached("package_modules", lambda: modules(PKG_ROOT)):
        for function, local, node in _scoped_nodes(mod):
            read = _env_read_arg(node)
            if read is None:
                continue
            kind, arg = read
            subject = f"{module}:{function}"
            name = _env_name(arg, constants, local)
            if name is None and function.split(".")[-1] in _ENV_READ_HELPERS:
                continue
            if name is None:
                dynamic[f"{subject} {kind}"] = "reads a variable named at run time"
            else:
                named.setdefault(name, subject)
    return named, dynamic


def _registered(name: str, registered: set[str]) -> bool:
    """True for a registry name, or for a family read (``X_<``) of an entry ``X_<ALIAS>``."""
    if name in registered:
        return True
    return name.endswith("<") and any(
        "<" in entry and entry.startswith(name[:-1]) for entry in registered
    )


def r_env_registry_complete(c: Context) -> Violations:
    from keboola_agent_cli.env_vars import ENV_VARS

    named, dynamic = _env_reads(c)
    registered = {var.name for var in ENV_VARS}
    found = {
        name: f"read in {where} but not in env_vars.ENV_VARS"
        for name, where in named.items()
        if not _registered(name, registered)
    }
    found.update(dynamic)
    families = {name[:-1] for name in named if name.endswith("<")}
    for var in ENV_VARS:
        if var.library or var.name in named:
            continue
        if "<" in var.name and var.name.split("<")[0] in families:
            continue
        found[f"{var.name} (registered)"] = "in env_vars.ENV_VARS but nothing in src/ reads it"
    return found


def _is_nonzero_exit(node: ast.AST) -> bool:
    """`typer.Exit(code)` or `sys.exit(code)` whose code is not absent or a literal 0."""
    if not isinstance(node, ast.Call) or dotted(node.func) not in ("typer.Exit", "sys.exit"):
        return False
    code = node.args[0] if node.args else keyword(node, "code")
    return not (code is None or (isinstance(code, ast.Constant) and code.value in (0, None)))


def _nodes(stmt: ast.AST) -> Iterator[ast.AST]:
    """Every node of ``stmt`` outside nested functions and classes."""
    yield stmt
    for child in ast.iter_child_nodes(stmt):
        if not isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            yield from _nodes(child)


def _exit_follows(chain: list[tuple[list[ast.stmt], int]]) -> bool:
    """True when a non-zero exit can run after the statement at the end of ``chain``.

    ``chain`` holds, from the function body down, each statement list and the
    index of the statement that contains the output call. At each level the
    later statements of the list are scanned; a direct `return` or `raise`
    there ends the scan, because control does not reach what follows it.
    """
    for stmts, index in reversed(chain):
        for stmt in stmts[index + 1 :]:
            if any(_is_nonzero_exit(n) for n in _nodes(stmt)):
                return True
            if isinstance(stmt, ast.Return | ast.Raise):
                return False
    return False


def _output_sites(
    stmts: list[ast.stmt], chain: list[tuple[list[ast.stmt], int]]
) -> Iterator[tuple[ast.Call, list[tuple[list[ast.stmt], int]]]]:
    """Each `<formatter>.output(...)` call under ``stmts`` with its statement chain."""
    for index, stmt in enumerate(stmts):
        here = [*chain, (stmts, index)]
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        blocks = [
            getattr(stmt, name)
            for name in ("body", "orelse", "finalbody")
            if isinstance(getattr(stmt, name, None), list)
        ]
        blocks += [h.body for h in getattr(stmt, "handlers", [])]
        blocks += [case.body for case in getattr(stmt, "cases", [])]
        if blocks:
            for block in blocks:
                yield from _output_sites(block, here)
            # The test of an `if`/`while`, the items of a `with`: calls outside the blocks.
            headers = [n for n in ast.iter_child_nodes(stmt) if isinstance(n, ast.expr)]
        else:
            headers = [stmt]
        for header in headers:
            for node in _nodes(header):
                # Any receiver (`formatter`, `fmt`, `self.formatter`): the method name decides.
                if isinstance(node, ast.Call) and dotted(node.func).endswith(".output"):
                    yield node, here


def r_ok_then_exit(c: Context) -> Violations:
    found = {}
    for module, tree in c.command_modules:
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for call, chain in _output_sites(func.body, []):
                if keyword(call, "exit_code") is None and _exit_follows(chain):
                    found[f"{module}:{func.name}"] = (
                        "formatter.output() is followed by a non-zero exit; pass exit_code="
                    )
    return found


def r_manifest_fresh(c: Context) -> Violations:
    want = manifest_text(c.tree)
    have = MANIFEST_PATH.read_text(encoding="utf-8") if MANIFEST_PATH.exists() else ""
    if have == want:
        return {}
    return {"docs/cli-manifest.json": f"{_manifest_changes(have, want)}; run 'make cli-manifest'"}


def _manifest_changes(have: str, want: str) -> str:
    """Name the commands whose manifest entry changed, for the failure line."""
    try:
        old = {c["path"]: c for c in json.loads(have)["commands"]}
    except (ValueError, KeyError, TypeError):
        return "missing or not a manifest"
    new = {c["path"]: c for c in json.loads(want)["commands"]}
    changed = sorted(p for p in old.keys() | new.keys() if old.get(p) != new.get(p))
    if not changed:
        return "differs from the live tree (root options or aliases)"
    more = f" and {len(changed) - 3} more" if len(changed) > 3 else ""
    return f"differs from the live tree: {', '.join(changed[:3])}{more}"


def _load_routes() -> list[tuple[str, str, Any]]:
    """Every (method, path template, APIRoute) of the serve app, lazy includes expanded."""
    from keboola_agent_cli.server import create_app

    with tempfile.TemporaryDirectory() as config_dir:
        fastapi_app = create_app(config_dir=config_dir, auth_token="cli-spec-check")
    stack, seen, routes = list(fastapi_app.routes), set(), []
    while stack:
        route = stack.pop()
        if id(route) in seen:
            continue
        seen.add(id(route))
        inner = getattr(route, "original_router", None)
        if inner is not None:
            stack.extend(inner.routes)
            continue
        for method in sorted(getattr(route, "methods", None) or ()):
            if method not in ("HEAD", "OPTIONS"):
                routes.append((method, getattr(route, "path", ""), route))
    return sorted(routes, key=lambda r: (r[1], r[0]))


def _serve_map() -> dict[tuple[str, str], str]:
    from keboola_agent_cli.server._serve_command_map import SERVE_COMMAND_MAP

    return SERVE_COMMAND_MAP


def r_serve_route(c: Context) -> Violations:
    mapped = {cmd for cmd in _serve_map().values() if cmd}
    return {
        lf.name: "no kbagent serve route maps to this command"
        for lf in c.tree.leaves
        if lf.name not in mapped
    }


def r_serve_map(c: Context) -> Violations:
    return {
        f"{method} {path}": f"maps to {cmd!r}, which is not a command"
        for (method, path), cmd in _serve_map().items()
        if cmd and cmd not in c.tree.by_name
    }


def r_route_permission_key(c: Context) -> Violations:
    from keboola_agent_cli.server.dependencies import PERMISSION_DEPENDENCY_MARKER
    from keboola_agent_cli.server.route_permissions import ROUTE_OPERATIONS, is_unguarded

    found = {}
    for method, path, route in c.routes:
        if is_unguarded(method, path):
            continue
        operation = ROUTE_OPERATIONS.get((method, path))
        inline = [
            getattr(getattr(d, "dependency", None), PERMISSION_DEPENDENCY_MARKER, None)
            for d in getattr(route, "dependencies", ()) or ()
        ]
        keys = [k for k in (operation, *inline) if k is not None]
        if not keys:
            found[f"{method} {path}"] = "route resolves to no permission key"
        elif any(k not in OPERATION_REGISTRY for k in keys):
            found[f"{method} {path}"] = f"permission key {keys} is not in OPERATION_REGISTRY"
    return found


def r_dry_run_parity(c: Context) -> Violations:
    from fastapi.dependencies.utils import get_flat_dependant

    from keboola_agent_cli.server.dependencies import _body_declares_dry_run

    commands = _serve_map()
    found = {}
    for method, path, route in c.routes:
        leaf = c.tree.by_name.get(commands.get((method, path), ""))
        if leaf is None or "--dry-run" not in opt_names(leaf):
            continue
        flat = get_flat_dependant(route.dependant)
        in_query = any(p.alias == "dry_run" for p in flat.query_params)
        if not in_query and not _body_declares_dry_run(flat.body_params):
            found[f"{method} {path}"] = f"'{leaf.name}' has --dry-run, the route has no dry_run"
    return found


def r_partial_update_patch(c: Context) -> Violations:
    return {
        f"{method} {path}": "partial update served with PUT; use PATCH"
        for method, path, _ in c.routes
        if method == "PUT" and path in PARTIAL_UPDATE_ROUTES
    }


def _claude_md_signatures() -> list[str]:
    text = CLAUDE_MD.read_text(encoding="utf-8")
    section = re.split(r"(?m)^## All CLI Commands$", text, maxsplit=1)[1]
    block = section.split("```", 2)[1]
    return [ln for ln in block.splitlines() if ln.startswith("kbagent ")]


_FLAG = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]*)")


def r_claude_md_flags(c: Context) -> Violations:
    found = {}
    for line in _claude_md_signatures():
        words = line.split()[1:]
        leaf = None
        for n in range(1, min(4, len(words)) + 1):
            leaf = c.tree.by_name.get(" ".join(words[:n]), leaf)
        if leaf is None:
            continue
        have = opt_names(leaf) | c.tree.root_opts | {"--help"}
        for flag in _FLAG.findall(line):
            if flag not in have:
                found[f"{leaf.name} {flag}"] = "in CLAUDE.md, not an option of the command"
    return found


RULES: tuple[Rule, ...] = (
    Rule("naming.kebab-case", "tree", "command segments and long options are kebab-case", r_kebab),
    Rule(
        "naming.alias-parity",
        "tree",
        "a hidden alias group is the same app as its visible group",
        r_alias_parity,
    ),
    Rule(
        "naming.hidden-leaf-has-canonical",
        "tree",
        "every hidden leaf outside an alias group has a COMMAND_ALIASES entry",
        r_hidden_leaf_canonical,
    ),
    Rule(
        "args.short-flag-one-meaning",
        "tree",
        "one short flag = one long option (SHORT_FLAGS)",
        r_short_one_meaning,
    ),
    Rule(
        "args.no-global-option-shadow",
        "tree",
        "no command option reuses a global option name",
        r_no_global_shadow,
    ),
    Rule(
        "args.long-option-one-shape",
        "tree",
        "one long name = one shape inside its command group (A1)",
        r_long_one_shape,
    ),
    Rule("args.no-bare-id-option", "tree", "no generic --id", r_no_bare_id),
    Rule("args.no-positional-arguments", "tree", "identifiers are options", r_no_positional),
    Rule(
        "args.no-dead-parameter",
        "ast",
        "every declared callback parameter is read",
        r_dead_parameter,
    ),
    Rule(
        "args.list-option-repeatable",
        "tree",
        "no list option that takes only one comma string",
        r_list_option_repeatable,
    ),
    Rule(
        "input.file-option-vocabulary",
        "tree",
        "file input is --file or --<field>-file; flags are skipped",
        r_file_vocabulary,
    ),
    Rule(
        "input.single-parser",
        "ast",
        'no startswith("@") value parsing outside parse_json_arg',
        r_single_parser,
    ),
    Rule(
        "target.project-help-states-default",
        "tree",
        "an optional or repeatable --project help states the omitted default",
        r_project_help_default,
    ),
    Rule(
        "target.no-write-fanout",
        "tree",
        "a write or destructive command with an optional repeatable --project has --all-projects",
        r_no_write_fanout,
    ),
    Rule(
        "output.no-direct-print",
        "ast",
        "no print, typer.echo, click.echo, stream write or new rich Console in commands/",
        r_no_direct_print,
    ),
    Rule(
        "env.registry-complete",
        "ast",
        "every environment variable read in src/ is in env_vars.ENV_VARS, and every entry is read",
        r_env_registry_complete,
    ),
    Rule(
        "output.ok-then-exit-declares-exit-code",
        "ast",
        "formatter.output followed by a non-zero exit passes exit_code=",
        r_ok_then_exit,
    ),
    Rule(
        "errors.forward-retryable",
        "ast",
        "formatter.error(error_code=exc.error_code, ...) passes retryable=",
        r_forward_retryable,
    ),
    Rule(
        "errors.config-error-keeps-code",
        "ast",
        "a ConfigError handler keeps the subclass code",
        r_config_error_keeps_code,
    ),
    Rule(
        "errors.code-has-type-and-exit",
        "ast",
        "every ErrorCode has an explicit (error_type, exit_code) entry that docs/error-codes.md repeats",
        r_code_has_type_and_exit,
        baseline=False,
    ),
    Rule(
        "safety.destructive-has-dry-run",
        "tree",
        "category destructive => --dry-run",
        r_destructive_dry_run,
    ),
    Rule(
        "safety.destructive-has-yes",
        "tree",
        "category destructive => --yes (--force does not count)",
        r_destructive_yes,
    ),
    Rule(
        "safety.write-has-dry-run",
        "tree",
        "category write => --dry-run",
        r_write_dry_run,
    ),
    Rule(
        "safety.destructive-verb-category",
        "tree",
        "a delete-like verb is destructive or admin",
        r_verb_category,
    ),
    Rule(
        "safety.force-meaning-registered",
        "tree",
        "every --force has a FORCE_MEANINGS entry naming its guards",
        r_force_registered,
    ),
    Rule("safety.yes-has-short-y", "tree", "--yes has -y", r_yes_short_y),
    Rule(
        "safety.confirm-via-helper",
        "ast",
        "no direct typer.confirm in commands/ outside _helpers._confirm_or_abort",
        r_confirm_via_helper,
    ),
    Rule("lists.list-has-limit", "tree", "a list leaf has --limit", r_list_limit),
    Rule("help.command-has-help", "tree", "command help is not empty", r_command_help),
    Rule("help.param-has-help", "tree", "every visible parameter has help", r_param_help),
    Rule(
        "help.claude-md-flags-exist",
        "tree",
        "every --flag in a CLAUDE.md signature exists",
        r_claude_md_flags,
    ),
    Rule(
        "help.manifest-fresh",
        "tree",
        "the committed manifest equals the generated one",
        r_manifest_fresh,
        baseline=False,
    ),
    Rule(
        "enforcement.runtime-permission-guard",
        "tree",
        "every leaf reaches a permission check with its own key",
        r_runtime_guard,
    ),
    Rule(
        "enforcement.permission-literal-matches-mount",
        "tree",
        "a group callback checks the key of its mount path",
        r_permission_literal,
    ),
    Rule(
        "serve.command-has-route",
        "tree",
        "a route names the command, or an exception with a reason exists",
        r_serve_route,
    ),
    Rule(
        "serve.map-names-live-command",
        "tree",
        "every SERVE_COMMAND_MAP value is a live leaf",
        r_serve_map,
    ),
    Rule(
        "serve.route-has-permission-key",
        "tree",
        "every route resolves to an operation key in OPERATION_REGISTRY",
        r_route_permission_key,
    ),
    Rule(
        "serve.dry-run-parity",
        "tree",
        "a route has dry_run when its command has --dry-run",
        r_dry_run_parity,
    ),
    Rule(
        "serve.partial-update-uses-patch",
        "tree",
        "a route in the reviewed list of partial updates uses PATCH",
        r_partial_update_patch,
    ),
)

# Rules of the spec that this script does not evaluate: contract cases (run by
# pytest in `make test` and `make cli-spec-check`) and rules that land later.
CONTRACT_RULES: dict[str, str] = {
    "help.doc-examples-parse": "tests/test_doc_examples.py",
    "output.human-snapshots": "tests/test_cli_human_snapshots.py",
    "serve.route-permission": "tests/test_cli_contract.py",
    "output.one-json-document": "tests/test_cli_contract.py",
    "output.bare-group-exits-2": "tests/test_cli_contract.py",
    "output.root-flags-anywhere": "tests/test_cli_contract.py",
    "output.envelope-schema": "tests/test_cli_contract.py",
    "help.dash-h": "tests/test_cli_contract.py",
    "args.repeated-single-value": "tests/test_cli_contract.py",
    "serve.undeclared-dry-run-rejected": "tests/test_server_dry_run_guard.py",
    "safety.no-tty-confirmation": "tests/test_cli_contract.py",
    "output.multi-item-headline": "tests/test_cli_contract.py",
}
DEFERRED_RULES: dict[str, str] = {
    "args.global-option-one-shape": "the GLOBAL_OPTIONS registry is not reviewed yet (A1)",
    "naming.verb-vocabulary": "the verb vocabulary file is not reviewed yet",
    "input.content-option-refuses-at-file": "the @file refusal lands in 1.0.0 (Input)",
    "output.no-warning-after-output": "lands with warnings[] in the --json envelope",
    "errors.fanout-exit": "contract; the default-project change for fan-out reads is 1.0.0",
    "help.context-from-manifest": "kbagent context is generated from the manifest in 1.0.0 (AG1)",
    "change.expired-deprecation": "no DEPRECATIONS entry has a remove_after date yet: "
    "the window of the 1.0.0 hidden aliases starts at M",
    "change.breaking-bullet-on-removal": "release kind; lands with the release mechanics",
    "target.targets-reported": "contract; lands with project_source in 1.0.0",
    "serve.targets-reported": "contract; the Kbagent-Targets header lands in 1.0.0 (V3)",
}

# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if callable(value):
        return "<dynamic>"
    return str(value)


def _param_entry(p: Any) -> dict[str, Any]:
    """One parameter; keys with a false or empty value are left out to keep the file small."""
    choices = getattr(p.type, "choices", None)
    entry: dict[str, Any] = {
        "kind": getattr(p, "param_type_name", ""),
        "name": p.name,
        "opts": list(p.opts),
        "secondary_opts": list(getattr(p, "secondary_opts", [])),
        "type": type_name(p),
        "choices": [getattr(ch, "name", None) or str(ch) for ch in choices or ()],
        "required": bool(p.required),
        "multiple": bool(getattr(p, "multiple", False)),
        "is_flag": bool(getattr(p, "is_flag", False)),
        "hidden": bool(getattr(p, "hidden", False)),
        "envvar": getattr(p, "envvar", None),
        "default": _jsonable(p.default),
        "help": (getattr(p, "help", None) or "").strip(),
    }
    return {k: v for k, v in entry.items() if v not in (False, None, [], "") or k == "default"}


def build_manifest(tree: Tree) -> dict[str, Any]:
    commands: list[dict[str, Any]] = []
    for g in sorted(tree.groups, key=lambda g: g.path):
        commands.append(
            {
                "path": " ".join(g.path),
                "kind": "group",
                "hidden": bool(getattr(g.node, "hidden", False)),
                "help": (g.node.help or "").strip(),
            }
        )
    for lf in sorted(tree.leaves, key=lambda lf: lf.path):
        commands.append(
            {
                "path": lf.name,
                "kind": "command",
                "hidden": lf.hidden,
                "category": lf.category,
                "help": lf.help,
                "params": [_param_entry(p) for p in lf.params],
            }
        )
    commands.sort(key=lambda entry: entry["path"])
    return {
        "_comment": "Generated by 'make cli-manifest' from the live Typer tree. Do not edit.",
        "root": {
            "help": (tree.root.help or "").strip(),
            "params": [_param_entry(p) for p in tree.root.params],
        },
        "aliases": dict(sorted(tree.aliases.items())),
        "commands": commands,
    }


def manifest_text(tree: Tree) -> str:
    """The manifest as JSON, one parameter per line, so a diff names the changed option."""
    manifest = build_manifest(tree)
    params: list[str] = []

    def mark(entries: list[dict[str, Any]]) -> list[str]:
        marks = []
        for entry in entries:
            params.append(json.dumps(entry, ensure_ascii=False))
            marks.append(f"@@param-{len(params) - 1}@@")
        return marks

    manifest["root"]["params"] = mark(manifest["root"]["params"])
    for command in manifest["commands"]:
        if "params" in command:
            command["params"] = mark(command["params"])
    text = json.dumps(manifest, indent=2, ensure_ascii=False)
    text = re.sub(r'"@@param-(\d+)@@"', lambda m: params[int(m.group(1))], text)
    return text + "\n"


# --------------------------------------------------------------------------- #
# Baseline and ratchet
# --------------------------------------------------------------------------- #


def evaluate(tree: Tree) -> dict[str, Violations]:
    ctx = Context(tree)
    return {rule.id: rule.evaluate(ctx) for rule in RULES}


def build_baseline(results: dict[str, Violations], old: dict[str, Any]) -> dict[str, Any]:
    """A new baseline from the current results; keeps blocking flags and exceptions."""
    old_blocking: dict[str, bool] = old.get("blocking", {})
    old_exceptions: dict[str, dict[str, str]] = old.get("exceptions", {})
    exceptions = {
        rid: {s: why for s, why in sorted(subjects.items()) if s in results.get(rid, {})}
        for rid, subjects in sorted(old_exceptions.items())
    }
    violations = {}
    for rule in RULES:
        listed = sorted(set(results[rule.id]) - set(exceptions.get(rule.id, {})))
        if rule.baseline and listed:
            violations[rule.id] = listed
    return {
        "_comment": old.get("_comment", ""),
        "spec_version": old.get("spec_version", 1),
        "blocking": {r.id: bool(old_blocking.get(r.id, False)) for r in RULES},
        "violations": violations,
        "exceptions": {rid: subs for rid, subs in exceptions.items() if subs},
    }


def anchor(rule_id: str) -> str:
    return f"{SPEC_DOC}#{rule_id}"


def ratchet(
    results: dict[str, Violations],
    baseline: dict[str, Any],
    base: dict[str, Any] | None = None,
    allow_new_exceptions: bool = False,
) -> list[str]:
    """Return failure lines (empty = pass); prints NOTE lines for non-blocking rules."""
    known = {r.id: r for r in RULES}
    blocking: dict[str, bool] = baseline.get("blocking", {})
    listed: dict[str, list[str]] = baseline.get("violations", {})
    excepted: dict[str, dict[str, str]] = baseline.get("exceptions", {})
    fails: list[str] = []
    for rid in sorted(set(blocking) | set(listed) | set(excepted)):
        if rid not in known:
            fails.append(f"FAIL unknown   {rid}: the baseline names a rule the check does not have")
    for rid in sorted(set(known) - set(blocking)):
        fails.append(f"FAIL unknown   {rid}: the baseline has no blocking flag for this rule")
    for rid, current in results.items():
        is_blocking = blocking.get(rid, True)
        prefix = "FAIL" if is_blocking else "NOTE"
        out = fails if is_blocking else []
        allowed = set(excepted.get(rid, {}))
        if known[rid].baseline:
            allowed |= set(listed.get(rid, []))
        elif listed.get(rid):
            fails.append(f"FAIL baseline  {rid}: this rule takes no baseline entries")
        lines = [
            f"{prefix} new      {rid}: {s} -- {current[s]}  (rule: {known[rid].text}; "
            f"{anchor(rid)})"
            for s in sorted(set(current) - allowed)
        ] + [
            f"{prefix} stale    {rid}: {s} -- no longer a violation; remove it from the "
            "baseline ('make cli-spec-baseline')"
            for s in sorted(allowed - set(current))
        ]
        out.extend(lines)
        if not is_blocking:
            for line in lines:
                print(line)
    if base is not None:
        fails.extend(_growth(blocking, listed, excepted, base, allow_new_exceptions))
    return fails


def _growth(
    blocking: dict[str, bool],
    listed: dict[str, list[str]],
    excepted: dict[str, dict[str, str]],
    base: dict[str, Any],
    allow_new_exceptions: bool,
) -> list[str]:
    """Failure lines for a baseline that is looser than the base branch's.

    Entries are counted per rule, not compared by subject: renaming a command,
    keeping the old name as a hidden alias (C3) or moving a function to another
    module renames its subject, and that is no growth. Known limit: a fixed
    entry can be swapped for a new violation of the same rule elsewhere.
    """
    old_listed: dict[str, list[str]] = base.get("violations", {})
    old_excepted: dict[str, dict[str, str]] = base.get("exceptions", {})
    old_blocking: dict[str, bool] = base.get("blocking", {})
    fails: list[str] = []
    for rid in sorted(set(blocking) | set(listed) | set(excepted)):
        if not blocking.get(rid, True):
            continue
        old_count, count = len(old_listed.get(rid, [])), len(listed.get(rid, []))
        if count > old_count:
            fails.append(
                f"FAIL grown    {rid}: {old_count} -> {count} entries -- the baseline "
                "gained entries"
            )
        if not allow_new_exceptions:
            for s in sorted(set(excepted.get(rid, {})) - set(old_excepted.get(rid, {}))):
                fails.append(
                    f"FAIL exception {rid}: {s} -- new exception; a reviewer adds the "
                    "'cli-spec-exception' label to the pull request to accept it"
                )
    for rid, was in sorted(old_blocking.items()):
        if rid not in blocking:
            fails.append(f"FAIL removed  {rid}: a rule of the base baseline is missing")
        elif was and not blocking[rid]:
            fails.append(f"FAIL loosened {rid}: changed from blocking to non-blocking")
    return fails


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def report(tree: Tree, results: dict[str, Violations]) -> list[str]:
    """Per-command inventory (#791 deliverable 2): category, flags, violated rules."""
    by_command: dict[str, list[str]] = defaultdict(list)
    for rid, subjects in results.items():
        for subject in subjects:
            for lf in tree.leaves:
                if subject == lf.name or subject.startswith(f"{lf.name} "):
                    by_command[lf.name].append(rid)
    lines = ["command\tcategory\tproject\tdry-run\tyes\tforce\trules"]
    for lf in sorted(tree.leaves, key=lambda lf: lf.path):
        p = param_with(lf, "--project")
        project = (
            "-"
            if p is None
            else "repeatable"
            if getattr(p, "multiple", False)
            else "required"
            if p.required
            else "optional"
        )
        flags = ["yes" if f in opt_names(lf) else "-" for f in ("--dry-run", "--yes", "--force")]
        rules = ",".join(sorted(set(by_command.get(lf.name, [])))) or "-"
        lines.append("\t".join([lf.name, lf.category or "?", project, *flags, rules]))
    return lines


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the CLI against its spec.")
    parser.add_argument("--baseline", type=Path, default=BASELINE_PATH)
    parser.add_argument("--base-baseline", type=Path, help="baseline of the base branch")
    parser.add_argument(
        "--allow-new-exceptions",
        action="store_true",
        help="accept subjects added to 'exceptions' against --base-baseline (reviewed PRs)",
    )
    parser.add_argument("--details", metavar="RULE", help="print every subject of one rule")
    parser.add_argument("--list-rules", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--write-manifest", action="store_true")
    args = parser.parse_args(argv)

    if args.list_rules:
        for rule in RULES:
            print(f"{rule.id}\t{rule.kind}\tscripts/check_cli_spec.py\t{rule.text}")
        for rid, where in sorted(CONTRACT_RULES.items()):
            print(f"{rid}\tcontract\t{where}")
        for rid, why in sorted(DEFERRED_RULES.items()):
            print(f"{rid}\tdeferred\t{why}")
        return 0

    tree = load_tree()
    if args.write_manifest:
        MANIFEST_PATH.write_text(manifest_text(tree), encoding="utf-8", newline="\n")
        print(f"wrote {MANIFEST_PATH.relative_to(REPO_ROOT)}")
        return 0

    results = evaluate(tree)
    if args.details:
        if args.details not in results:
            print(
                f"unknown rule {args.details!r}; see 'python scripts/check_cli_spec.py "
                "--list-rules' for the rule ids",
                file=sys.stderr,
            )
            return 2
        for subject, why in sorted(results[args.details].items()):
            print(f"{subject:70s} {why}")
        return 0
    if args.report:
        print("\n".join(report(tree, results)))
        return 0

    old = _read_json(args.baseline) if args.baseline.exists() else {}
    if args.write_baseline:
        _write_json(args.baseline, build_baseline(results, old))
        print(f"wrote {args.baseline}")
        return 0

    base = _read_json(args.base_baseline) if args.base_baseline else None
    fails = ratchet(results, old, base, args.allow_new_exceptions)
    for line in fails:
        print(line)
    if fails:
        print(
            f"\n{len(fails)} problem(s). Fix the command, or for a fixed violation remove "
            "its baseline line ('make cli-spec-baseline'). An accepted exception goes into "
            f"'exceptions' with a reason. Rule texts: 'python scripts/check_cli_spec.py "
            f"--list-rules'; the rules are specified in {SPEC_DOC}."
        )
        return 1
    counts = ", ".join(f"{rid}={len(v)}" for rid, v in results.items() if v)
    print(f"OK: {len(tree.leaves)} commands, {len(RULES)} rules, no new or stale entries.")
    print(f"Current violations (baseline + exceptions): {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
