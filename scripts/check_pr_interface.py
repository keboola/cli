#!/usr/bin/env python3
"""CI gate: a pull request describes its interface changes (spec rule C10).

A pull request that can change the kbagent interface (the CLI, the JSON
output, the error codes, the REST API of `kbagent serve`) has a section
`## Interface changes` in its description. This script finds the interface
items that the pull request changes and checks that the section describes
each one. The reviewer checks the assessment in the section (spec C10).

The items come from generated, committed files, so the result does not depend
on how the author describes the change:

  - docs/cli-manifest.json: commands, aliases and options (`make cli-manifest`).
  - docs/error-codes.md: the error codes, with exit code and error type.
  - docs/web-server-endpoints.md: the REST routes of `kbagent serve`.
  - src/keboola_agent_cli/env_vars.py: the names in the ENV_VARS registry.

The base files come from `git show REF:path`, the head files from the working
tree. A file that the base does not have counts as empty, so all its items are
added. Each item has a class: `breaking` and `additive` follow spec C1, `help`
is a change of a help text or a summary only, and `note` is a change that is
neither (a hidden flag, a category that the spec does not rank).

The section fails the check when:

  1. the pull request changes `src/keboola_agent_cli/` (not `_ui_dist/`), one
     of the files above or `scripts/cli_spec_baseline.json`, and the
     description has no `## Interface changes` section.
  2. no line of the section names a detected item (a command by its
     path, an option by its long flag on a line that also names its command,
     an error code, a route path or a variable name, each in backticks).
  3. a breaking item has no line that names it, contains `BREAKING` and names
     `C3` or `C4`.
  4. an added or breaking item of a command whose category is not `read` has
     no line that names the command and gives its R1 class (`converges`,
     `guarded` or `duplicates`).
  5. the section is empty although the pull request needs it, or says only
     "None" although items were detected.
  6. the section still contains a `TODO` placeholder of the skeleton.

The script uses only the standard library and does not import the package,
so CI runs it without `uv sync`.

Usage (from the repository root):
    python3 scripts/check_pr_interface.py --base origin/main --body-file body.md
    gh pr view 123 --json body --jq .body | python3 scripts/check_pr_interface.py --body-file -
    python3 scripts/check_pr_interface.py --base origin/main --skeleton
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SPEC_LINK = "docs/cli-interface-spec.md#C10"

MANIFEST = "docs/cli-manifest.json"
ERROR_CODES = "docs/error-codes.md"
ENDPOINTS = "docs/web-server-endpoints.md"
ENV_VARS = "src/keboola_agent_cli/env_vars.py"
PACKAGE_DIR = "src/keboola_agent_cli/"
UI_DIST_DIR = "src/keboola_agent_cli/_ui_dist/"
INTERFACE_FILES = frozenset({MANIFEST, ERROR_CODES, ENDPOINTS, "scripts/cli_spec_baseline.json"})

ROOT_PATH = "kbagent"
CATEGORY_RANK = {"read": 0, "write": 1, "destructive": 2, "admin": 3}
CLASS_RANK = {"help": 0, "note": 1, "additive": 2, "breaking": 3}
R1_WORDS = ("converges", "guarded", "duplicates")

_HEADING_2 = re.compile(r"^##\s+interface changes\s*#*\s*$", re.IGNORECASE)
_HEADING_1_OR_2 = re.compile(r"^#{1,2}\s")
_FENCE = re.compile(r"^\s*(```|~~~)")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_UNIT_START = re.compile(r"^\s*([-*+]\s|\d+[.)]\s|\||#)")
_SPAN = re.compile(r"`([^`\n]+)`")
_SPAN_TOKEN_SPLIT = re.compile(r"[\s/|=,\[\]()]+")
_BREAKING = re.compile(r"\bBREAKING\b")
_C3_C4 = re.compile(r"\bC[34]\b")
_R1 = re.compile(r"\b(" + "|".join(R1_WORDS) + r")\b", re.IGNORECASE)
_NONE_ONLY = re.compile(r"^[\s\-*+.]*(none)?[\s.]*$", re.IGNORECASE)
_ERROR_ROW = re.compile(r"^\| `([A-Z][A-Z0-9_]*)` \| (\d+) \| `([a-z_]+)` \| (.*?) \|\s*$")
_ROUTE_ROW = re.compile(r"^\| `([A-Z]+)` \| `(/[^`]*)` \| (.*?) \|\s*$")


@dataclass(frozen=True)
class Item:
    """One interface item that the pull request changes."""

    kind: str  # command, alias, option, error code, route, environment variable
    subject: str  # how the item is named in messages: `workspace create --read-only`
    changes: tuple[str, ...]  # what changed, in words
    cls: str  # breaking | additive | help | note
    command: str = ""  # the command path of a command, alias or option item
    token: str = ""  # the name to find in backticks: flag, code, route path, variable
    method: str = ""  # the HTTP method of a route
    category: str = ""  # the head category of the command, "" when it has none
    rules: tuple[str, ...] = ()  # spec rules to consider, for the skeleton


def _item(kind: str, subject: str, changes: list[tuple[str, str]], **kw: Any) -> Item:
    """Fold (change, class) pairs into one item. The item takes the highest class."""
    cls = max((c for _, c in changes), key=CLASS_RANK.__getitem__)
    return Item(kind, subject, tuple(text for text, _ in changes), cls, **kw)


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #


def parse_manifest(text: str | None) -> dict[str, Any]:
    """`{"commands": {path: entry}, "aliases": {alias: path}}`. The root is path `kbagent`."""
    if not text:
        return {"commands": {}, "aliases": {}}
    data = json.loads(text)
    commands = {c["path"]: c for c in data.get("commands", [])}
    root = data.get("root") or {}
    commands[ROOT_PATH] = {
        "path": ROOT_PATH,
        "kind": "root",
        "hidden": False,
        "help": root.get("help", ""),
        "params": root.get("params", []),
    }
    return {"commands": commands, "aliases": dict(data.get("aliases", {}))}


def _param_key(param: dict[str, Any]) -> str:
    """The first long option name, else the parameter name."""
    for opt in param.get("opts", []):
        if opt.startswith("--"):
            return opt
    return str(param["name"])


def _param_token(param: dict[str, Any]) -> str:
    """The name to find in backticks: the long flag, or the metavar of an argument."""
    key = _param_key(param)
    return key if key.startswith("--") else key.upper()


def _category_change(old: str | None, new: str | None) -> tuple[str, str]:
    text = f"category {old} -> {new}"
    if old in CATEGORY_RANK and new in CATEGORY_RANK:
        raised = CATEGORY_RANK[new] > CATEGORY_RANK[old]
        return text, "breaking" if raised else "additive"
    return text, "note"


def _command_changes(old: dict[str, Any], new: dict[str, Any]) -> list[tuple[str, str]]:
    changes: list[tuple[str, str]] = []
    if old.get("kind") != new.get("kind"):
        changes.append((f"kind {old.get('kind')} -> {new.get('kind')}", "breaking"))
    if old.get("category") != new.get("category"):
        changes.append(_category_change(old.get("category"), new.get("category")))
    if bool(old.get("hidden")) != bool(new.get("hidden")):
        changes.append((f"hidden {bool(old.get('hidden'))} -> {bool(new.get('hidden'))}", "note"))
    if old.get("help") != new.get("help"):
        changes.append(("help changed", "help"))
    return changes


def _names(param: dict[str, Any]) -> set[str]:
    return set(param.get("opts", [])) | set(param.get("secondary_opts", []))


def _option_changes(old: dict[str, Any], new: dict[str, Any]) -> list[tuple[str, str]]:
    changes: list[tuple[str, str]] = []
    removed = sorted(_names(old) - _names(new))
    added = sorted(_names(new) - _names(old))
    if removed:
        changes.append((f"name removed: {', '.join(removed)}", "breaking"))
    if added:
        changes.append((f"name added: {', '.join(added)}", "additive"))
    for field in ("type", "is_flag", "default"):
        if old.get(field) != new.get(field):
            changes.append((f"{field} {old.get(field)!r} -> {new.get(field)!r}", "breaking"))
    old_req, new_req = bool(old.get("required")), bool(new.get("required"))
    if old_req != new_req:
        changes.append((f"required {old_req} -> {new_req}", "breaking" if new_req else "additive"))
    old_multi, new_multi = bool(old.get("multiple")), bool(new.get("multiple"))
    if old_multi != new_multi:
        multi_cls = "additive" if new_multi else "breaking"
        changes.append((f"repeatable {old_multi} -> {new_multi}", multi_cls))
    old_choices, new_choices = set(old.get("choices") or []), set(new.get("choices") or [])
    if old_choices - new_choices:
        changes.append(
            (f"value removed: {', '.join(sorted(old_choices - new_choices))}", "breaking")
        )
    if new_choices - old_choices and old_choices:
        changes.append((f"value added: {', '.join(sorted(new_choices - old_choices))}", "additive"))
    if old.get("envvar") != new.get("envvar"):
        env_cls = "breaking" if old.get("envvar") else "additive"
        changes.append((f"envvar {old.get('envvar')} -> {new.get('envvar')}", env_cls))
    if old.get("help") != new.get("help"):
        changes.append(("help changed", "help"))
    return changes


def _option_items(path: str, old: dict[str, Any], new: dict[str, Any], category: str) -> list[Item]:
    old_params = {_param_key(p): p for p in old.get("params", [])}
    new_params = {_param_key(p): p for p in new.get("params", [])}
    items: list[Item] = []
    for key in sorted(old_params.keys() | new_params.keys()):
        before, after = old_params.get(key), new_params.get(key)
        if before is None and after is not None:
            required = bool(after.get("required"))
            changes = [
                (
                    "option added, required" if required else "option added",
                    "breaking" if required else "additive",
                )
            ]
            rules = ("A1", "A2", "A7", "H7")
        elif after is None and before is not None:
            changes, rules = [("option removed", "breaking")], ("C1", "C3", "C4")
        elif before is not None and after is not None:
            changes, rules = _option_changes(before, after), ("A1", "A7", "H7")
        else:  # pragma: no cover - a key comes from one of the two maps
            continue
        if not changes:
            continue
        param = after or before
        assert param is not None
        token = _param_token(param)
        subject = f"{path} {token}"
        items.append(
            _item(
                "option",
                subject,
                changes,
                command=path,
                token=token,
                category=category,
                rules=rules,
            )
        )
    return items


def diff_manifest(base: dict[str, Any], head: dict[str, Any]) -> list[Item]:
    """The command, alias and option items between two parsed manifests."""
    items: list[Item] = []
    old_cmds, new_cmds = base["commands"], head["commands"]
    for path in sorted(old_cmds.keys() | new_cmds.keys()):
        old, new = old_cmds.get(path), new_cmds.get(path)
        if old is None and new is not None:
            if path == ROOT_PATH:
                items.extend(_option_items(path, {}, new, ""))
                continue
            items.append(
                _item(
                    "command",
                    path,
                    [("command added", "additive")],
                    command=path,
                    category=new.get("category") or "",
                    rules=("N1", "N2", "N3", "H1", "R1"),
                )
            )
        elif new is None and old is not None:
            if path == ROOT_PATH:
                continue
            items.append(
                _item(
                    "command",
                    path,
                    [("command removed", "breaking")],
                    command=path,
                    rules=("C1", "C3", "C4"),
                )
            )
        elif old is not None and new is not None:
            category = new.get("category") or ""
            changes = _command_changes(old, new)
            if changes:
                items.append(
                    _item(
                        "command",
                        path,
                        changes,
                        command=path,
                        category=category,
                        rules=("H1", "H4", "S1"),
                    )
                )
            items.extend(_option_items(path, old, new, category))
    old_alias, new_alias = base["aliases"], head["aliases"]
    for alias in sorted(old_alias.keys() | new_alias.keys()):
        before, after = old_alias.get(alias), new_alias.get(alias)
        if before == after:
            continue
        if before is None:
            change = (f"alias added for {after}", "additive")
        elif after is None:
            change = (f"alias removed (was {before})", "breaking")
        else:
            change = (f"alias target {before} -> {after}", "breaking")
        items.append(_item("alias", alias, [change], command=alias, rules=("N4",)))
    return items


def parse_error_codes(text: str | None) -> dict[str, tuple[str, str, str]]:
    """`{CODE: (exit, error_type, description)}` from the tables of docs/error-codes.md."""
    rows: dict[str, tuple[str, str, str]] = {}
    for line in (text or "").splitlines():
        match = _ERROR_ROW.match(line)
        if match:
            rows[match[1]] = (match[2], match[3], match[4].strip())
    return rows


def diff_error_codes(
    base: dict[str, tuple[str, str, str]], head: dict[str, tuple[str, str, str]]
) -> list[Item]:
    items: list[Item] = []
    for code in sorted(base.keys() | head.keys()):
        old, new = base.get(code), head.get(code)
        changes: list[tuple[str, str]] = []
        if old is None:
            changes.append(("error code added", "additive"))
        elif new is None:
            changes.append(("error code removed", "breaking"))
        else:
            if old[0] != new[0]:
                changes.append((f"exit code {old[0]} -> {new[0]}", "breaking"))
            if old[1] != new[1]:
                changes.append((f"error type {old[1]} -> {new[1]}", "breaking"))
            if old[2] != new[2]:
                changes.append(("description changed", "help"))
        if changes:
            items.append(_item("error code", code, changes, token=code, rules=("E1", "E2", "E8")))
    return items


def parse_routes(text: str | None) -> dict[tuple[str, str], str]:
    """`{(METHOD, /path): summary}` from the tables of docs/web-server-endpoints.md."""
    rows: dict[tuple[str, str], str] = {}
    for line in (text or "").splitlines():
        match = _ROUTE_ROW.match(line)
        if match:
            rows[(match[1], match[2])] = match[3].strip()
    return rows


def diff_routes(base: dict[tuple[str, str], str], head: dict[tuple[str, str], str]) -> list[Item]:
    items: list[Item] = []
    for method, path in sorted(base.keys() | head.keys()):
        old, new = base.get((method, path)), head.get((method, path))
        if old == new:
            continue
        if old is None:
            change = ("route added", "additive")
        elif new is None:
            change = ("route removed", "breaking")
        else:
            change = ("summary changed", "help")
        items.append(
            _item(
                "route",
                f"{method} {path}",
                [change],
                token=path,
                method=method,
                rules=("V1", "V7", "V9"),
            )
        )
    return items


def parse_env_vars(text: str | None) -> set[str]:
    """The `name` of each `EnvVar(...)` in the `ENV_VARS` tuple of env_vars.py."""
    if not text:
        return set()
    names: set[str] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if not (isinstance(target, ast.Name) and target.id == "ENV_VARS"):
            continue
        for call in ast.walk(value):
            if not isinstance(call, ast.Call):
                continue
            args = [*call.args[:1], *(k.value for k in call.keywords if k.arg == "name")]
            for arg in args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    names.add(arg.value)
    return names


def diff_env_vars(base: set[str], head: set[str]) -> list[Item]:
    items = [
        _item(
            "environment variable",
            name,
            [("variable added", "additive")],
            token=name,
            rules=("EV1", "EV2", "EV3"),
        )
        for name in sorted(head - base)
    ]
    items += [
        _item(
            "environment variable",
            name,
            [("variable removed", "breaking")],
            token=name,
            rules=("C1", "C3", "C4"),
        )
        for name in sorted(base - head)
    ]
    return items


def detect_items(
    base_files: dict[str, str | None], head_files: dict[str, str | None]
) -> list[Item]:
    """Every interface item between two sets of source texts (path -> text, None = missing)."""
    return [
        *diff_manifest(
            parse_manifest(base_files.get(MANIFEST)), parse_manifest(head_files.get(MANIFEST))
        ),
        *diff_error_codes(
            parse_error_codes(base_files.get(ERROR_CODES)),
            parse_error_codes(head_files.get(ERROR_CODES)),
        ),
        *diff_routes(
            parse_routes(base_files.get(ENDPOINTS)), parse_routes(head_files.get(ENDPOINTS))
        ),
        *diff_env_vars(
            parse_env_vars(base_files.get(ENV_VARS)), parse_env_vars(head_files.get(ENV_VARS))
        ),
    ]


def needs_section(changed_files: Iterable[str]) -> bool:
    """True when a changed file can change the interface (rule 1)."""
    for path in changed_files:
        if path in INTERFACE_FILES:
            return True
        if path.startswith(PACKAGE_DIR) and not path.startswith(UI_DIST_DIR):
            return True
    return False


# --------------------------------------------------------------------------- #
# The section
# --------------------------------------------------------------------------- #


def extract_section(body: str) -> str | None:
    """The text after `## Interface changes` up to the next level-1 or level-2 heading."""
    lines = _HTML_COMMENT.sub("", body.replace("\r\n", "\n")).split("\n")
    in_fence = False
    start: int | None = None
    for index, line in enumerate(lines):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if start is None:
            if _HEADING_2.match(line):
                start = index + 1
        elif _HEADING_1_OR_2.match(line):
            return "\n".join(lines[start:index])
    return None if start is None else "\n".join(lines[start:])


def logical_lines(section: str) -> list[str]:
    """Bullets, table rows and paragraphs. A wrapped continuation joins its line."""
    units: list[str] = []
    current: list[str] = []
    for line in section.split("\n"):
        if not line.strip():
            if current:
                units.append(" ".join(current))
            current = []
        elif _UNIT_START.match(line) or not current:
            if current:
                units.append(" ".join(current))
            current = [line.strip()]
        else:
            current.append(line.strip())
    if current:
        units.append(" ".join(current))
    return units


def _spans(line: str) -> list[str]:
    return [s.strip() for s in _SPAN.findall(line)]


def names_command(line: str, path: str) -> bool:
    """A backtick span names the command path (`kbagent` may lead it)."""
    for span in _spans(line):
        if path == ROOT_PATH:
            if span == ROOT_PATH or span.startswith(ROOT_PATH + " -"):
                return True
            continue
        text = span.removeprefix(ROOT_PATH + " ").strip()
        if text == path:
            return True
        rest = text[len(path) + 1 :] if text.startswith(path + " ") else ""
        if rest and not rest[0].islower():
            return True
    return False


def names_token(line: str, token: str) -> bool:
    """A backtick span holds the token as a whole word (`--a/--no-a`, `cmd --a X`)."""
    return any(token in _SPAN_TOKEN_SPLIT.split(span) for span in _spans(line))


def names_item(line: str, item: Item) -> bool:
    """The mention rule of rule 2."""
    if item.kind in ("command", "alias"):
        return names_command(line, item.command)
    if item.kind == "option":
        return names_token(line, item.token) and names_command(line, item.command)
    if item.kind == "route":
        spans = _spans(line)
        return item.token in spans or f"{item.method} {item.token}" in spans
    return item.token in _spans(line)


def _describe(item: Item) -> str:
    return f"`{item.subject}` ({item.kind}: {'; '.join(item.changes)})"


def _name_hint(item: Item) -> str:
    if item.kind == "option":
        return f"`{item.command}` and `{item.token}`"
    if item.kind in ("command", "alias"):
        return f"`{item.command}`"
    return f"`{item.token}`"


def check_section(section: str | None, items: list[Item], needed: bool) -> list[str]:
    """The problems of the section, one message per problem. [] means it passes."""
    if section is None:
        if needed or items:
            return [
                (
                    "The description has no `## Interface changes` section, and the pull request "
                    "changes files that can change the interface. Add the section: list each "
                    "change, or write `None.` when nothing in the interface changes."
                )
            ]
        return []
    if not section.strip():
        if not (needed or items):
            return []
        return [
            (
                "The `## Interface changes` section is empty. List each interface change, or "
                "write `None.` when nothing in the interface changes."
            )
        ]
    problems: list[str] = []
    lines = logical_lines(section)
    if items and _NONE_ONLY.match(section):
        problems.append(
            f"The `## Interface changes` section says only `None`, but {len(items)} interface "
            "item(s) changed. Describe each of them."
        )
    if "TODO" in section:
        problems.append(
            "The `## Interface changes` section still contains `TODO`. Replace each `TODO` "
            "of the skeleton with the real text."
        )
    r1_done: set[str] = set()
    for item in items:
        naming = [line for line in lines if names_item(line, item)]
        if not naming:
            problems.append(
                f"{_describe(item)} is not described. Add a line that names {_name_hint(item)} "
                "in backticks and says what changed."
            )
        if item.cls == "breaking" and not any(
            _BREAKING.search(line) and _C3_C4.search(line) for line in naming
        ):
            problems.append(
                f"{_describe(item)} is breaking (spec C1). Add `BREAKING` and `C3` (deprecation "
                "window) or `C4` (ships at once) to the line that names it."
            )
        needs_r1 = item.cls in ("additive", "breaking") and item.category not in ("", "read")
        if needs_r1 and item.command not in r1_done:
            r1_done.add(item.command)
            if not any(names_command(line, item.command) and _R1.search(line) for line in lines):
                problems.append(
                    f"`{item.command}` is a `{item.category}` command and {_describe(item)} "
                    "changes it. Add its repeatability class (spec R1: `converges`, `guarded` "
                    f"or `duplicates`) to a line that names `{item.command}`."
                )
    return problems


def skeleton(items: list[Item]) -> str:
    """A ready-to-fill `## Interface changes` section for the detected items."""
    out = [
        "## Interface changes",
        "",
        (
            f"<!-- Spec rule C10 ({SPEC_LINK}). For each item: what changed, the spec rules it "
            "touches, additive or breaking (C1), and the commands, options, keys or codes you "
            "compared it with (A1, N2, R6, E8, O5, H7). Say that it matches them, or why it "
            "differs. Replace every TODO. -->"
        ),
        "",
    ]
    if not items:
        out.append("None.")
        return "\n".join(out) + "\n"
    for item in items:
        name = f"`{item.subject}`"
        parts = [f"- {name}: {'; '.join(item.changes)}."]
        if item.cls == "breaking":
            parts.append(
                "BREAKING (C1). TODO: C3 (deprecation window) or C4 (ships at once), and why."
            )
        else:
            parts.append(f"Class: {item.cls}.")
        if item.cls in ("additive", "breaking") and item.category not in ("", "read"):
            parts.append("R1: TODO converges, guarded or duplicates.")
        parts.append(f"Rules: {', '.join(item.rules)}.")
        parts.append(
            "Consistency: TODO name the commands or options compared with, and say that it matches or why it differs."
        )
        out.append(" ".join(parts))
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# git and main
# --------------------------------------------------------------------------- #


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _base_text(root: Path, ref: str, path: str) -> str | None:
    result = _git(root, "show", f"{ref}:{path}")
    return result.stdout if result.returncode == 0 else None


def _head_text(root: Path, path: str) -> str | None:
    file = root / path
    return file.read_text(encoding="utf-8") if file.is_file() else None


def changed_files(root: Path, ref: str) -> list[str]:
    """Files that differ from REF in the working tree, plus untracked files."""
    tracked = _git(root, "diff", "--name-only", ref).stdout.split()
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "--full-name").stdout
    return sorted(set(tracked) | set(untracked.split()))


def _read_body(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check the `## Interface changes` section of a pull request (spec C10)."
    )
    parser.add_argument("--base", default="origin/main", help="git ref of the base tree")
    parser.add_argument("--body-file", help="the pull request description (- reads stdin)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="check the description (default)")
    mode.add_argument("--skeleton", action="store_true", help="print a section to fill in")
    args = parser.parse_args(argv)

    toplevel = _git(Path.cwd(), "rev-parse", "--show-toplevel")
    if toplevel.returncode != 0:
        print("::error::Run the check inside the git repository of kbagent.")
        return 2
    root = Path(toplevel.stdout.strip())
    if _git(root, "rev-parse", "--verify", "--quiet", f"{args.base}^{{commit}}").returncode != 0:
        print(f"::error::Base ref {args.base!r} is not a commit. Fetch it first.")
        return 2
    sources = (MANIFEST, ERROR_CODES, ENDPOINTS, ENV_VARS)
    items = detect_items(
        {p: _base_text(root, args.base, p) for p in sources},
        {p: _head_text(root, p) for p in sources},
    )
    if args.skeleton:
        print(skeleton(items), end="")
        return 0
    if not args.body_file:
        parser.error("--check needs --body-file (use - for stdin)")

    section = extract_section(_read_body(args.body_file))
    problems = check_section(section, items, needs_section(changed_files(root, args.base)))
    if not problems:
        print(f"Interface changes: OK ({len(items)} interface item(s) detected and described).")
        return 0
    for problem in problems:
        print(f"::error title=Interface changes::{problem}")
    print(
        f"\n{len(problems)} problem(s) in the `## Interface changes` section of the pull "
        f"request description. Spec: {SPEC_LINK}. Edit the description. The check then runs again."
    )
    print("\nA section for the detected items (`make pr-interface-skeleton`):\n")
    print(skeleton(items), end="")
    return 1


if __name__ == "__main__":
    sys.exit(main())
