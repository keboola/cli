"""Every `kbagent ...` example in the agent-facing documents parses against the tree.

Agents copy these examples as they are. An example with a command path that
does not exist, or with a long option that the command does not have, fails
with exit 2. A root option (`--json`) after the subcommand fails the same way.

Scanned: CLAUDE.md and plugins/kbagent/**/*.md (lines in fenced code blocks and
inline code spans that start with `kbagent `), and AGENT_CONTEXT (indented
lines and inline code spans). A line that ends with `\\` is joined with the next
one. Quoted values are removed before the scan. The check stops at the first
token that is not a plain command word or an option, so placeholders (`<cmd>`,
`NAME`, `list|detail`) pass.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import typer.main

from keboola_agent_cli.cli import app
from keboola_agent_cli.commands._project_ref import target_project_param
from keboola_agent_cli.commands.context import AGENT_CONTEXT
from keboola_agent_cli.commands.repl import _is_group

REPO_ROOT = Path(__file__).resolve().parent.parent

# Intentional non-commands: the text says they do not exist.
ALLOWED = {
    "kbagent tool list",  # removed in v0.85.0
    "kbagent tool call",  # removed in v0.85.0
    "kbagent storage rename-table",  # "does not exist"
    "kbagent flow migrate",  # "does not exist"
    "kbagent storage retype",  # made-up command in a review example
}

_WORD = re.compile(r"[a-z][a-z0-9-]*")
_LONG = re.compile(r"(?<![\w-])--[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")


def _opts(cmd: Any) -> set[str]:
    return {o for p in cmd.params for o in (*p.opts, *p.secondary_opts)} | {"--help"}


def _cut(example: str) -> str:
    """Cut at a comment, redirect, `&&` or `;`, and at a pipe outside `[...]` / `(...)`."""
    example = re.split(r"\s+(?:#|\\$|2?>)|\s*(?:&&|;)", example)[0]
    depth = 0
    for i, char in enumerate(example):
        if char in "[(":
            depth += 1
        elif char in "])":
            depth = max(depth - 1, 0)
        elif char == "|" and depth == 0 and example[i - 1 : i].isspace():
            return example[:i].strip()
    return example.strip()


def _examples(text: str, *, markdown: bool) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    fence = False
    lines = text.splitlines()
    for n, line in enumerate(lines, 1):
        if markdown and line.lstrip().startswith("```"):
            fence = not fence
            continue
        found = [s for s in re.findall(r"`([^`]*)`", line) if s.startswith("kbagent ")]
        if fence or (not markdown and line.startswith(" ")):
            m = re.match(r"\s*(?:\$\s*)?(?:[A-Z_]+=\S+\s+)*(?:uv run\s+)?(kbagent\s.*)", line)
            if m:
                cmd, nxt = m.group(1), n
                # Join shell continuation lines.
                while cmd.rstrip().endswith("\\") and nxt < len(lines):
                    cmd, nxt = cmd.rstrip()[:-1] + " " + lines[nxt].strip(), nxt + 1
                found.append(cmd)
        for ex in found:
            # Cut at a shell operator or a comment.
            out.append((n, _cut(ex)))
    return out


def _problem(example: str, root: Any) -> str | None:
    ctx = type(root).context_class(root)
    tokens = _QUOTED.sub(" VALUE ", example).split()[1:]
    i = 0
    root_opts = _opts(root)
    root_valued = {o for p in root.params if not getattr(p, "is_flag", False) for o in p.opts}
    while i < len(tokens) and tokens[i].startswith("-"):
        name = tokens[i].split("=")[0]
        if name not in root_opts:
            return f"no root option {name}"
        i += 2 if name in root_valued and "=" not in tokens[i] else 1
    node = root
    path: list[str] = []
    while _is_group(node) and i < len(tokens):
        if tokens[i].startswith("-") and tokens[i] != "--help":
            name = " ".join(path) or "kbagent"
            return f"option {tokens[i]} before the subcommand of {name!r}"
        if not _WORD.fullmatch(tokens[i]):
            return None  # placeholder or pseudo-syntax
        sub = node.get_command(ctx, tokens[i])
        if sub is None:
            return f"no command {' '.join([*path, tokens[i]])!r}"
        node, path, i = sub, [*path, tokens[i]], i + 1
    if _is_group(node):
        return None
    have = _opts(node)
    for token in tokens[i:]:
        for opt in _LONG.findall(token):
            if opt not in have:
                return f"{' '.join(path)} has no option {opt}"
    return None


def test_agent_facing_examples_parse() -> None:
    root = typer.main.get_command(app)
    sources = [(REPO_ROOT / "CLAUDE.md", True)]
    sources += [(p, True) for p in sorted((REPO_ROOT / "plugins" / "kbagent").rglob("*.md"))]
    problems = []
    for path, markdown in sources:
        for n, ex in _examples(path.read_text(encoding="utf-8"), markdown=markdown):
            if ex not in ALLOWED and (why := _problem(ex, root)):
                problems.append(f"{path.relative_to(REPO_ROOT)}:{n}: {why}: {ex}")
    for n, ex in _examples(AGENT_CONTEXT, markdown=False):
        if ex not in ALLOWED and (why := _problem(ex, root)):
            problems.append(f"AGENT_CONTEXT:{n}: {why}: {ex}")
    assert not problems, "\n".join(problems)


def test_checker_finds_a_wrong_example() -> None:
    root = typer.main.get_command(app)
    assert _problem("kbagent project list --bogus", root) == "project list has no option --bogus"
    # The root flags are accepted after the subcommand too (O7).
    assert _problem("kbagent project list --json", root) is None
    assert _problem("kbagent no-such-group list", root) == "no command 'no-such-group'"
    assert _problem("kbagent --json project list", root) is None
    assert _problem("kbagent config <cmd> --project P", root) is None


def test_checker_reads_bracketed_and_grouped_options() -> None:
    root = typer.main.get_command(app)
    assert _problem("kbagent agent test [--bogus]", root) == "agent test has no option --bogus"
    assert _problem("kbagent agent test (--bogus|--stream)", root) == (
        "agent test has no option --bogus"
    )
    assert _problem("kbagent agent test [--stream] (--type T | --name N)", root) is None


def test_checker_joins_continuation_lines() -> None:
    root = typer.main.get_command(app)
    text = "```bash\nkbagent project list \\\n  --bogus\n```\n"
    [(n, ex)] = _examples(text, markdown=True)
    assert n == 2
    assert _problem(ex, root) == "project list has no option --bogus"


def test_checker_rejects_an_option_before_the_subcommand() -> None:
    root = typer.main.get_command(app)
    assert _problem("kbagent storage --project X buckets", root) == (
        "option --project before the subcommand of 'storage'"
    )
    assert _problem("kbagent storage --help", root) is None


def test_checker_ignores_quoted_values() -> None:
    root = typer.main.get_command(app)
    assert _problem("kbagent agent test --prompt 'a --zz'", root) is None
    assert _problem('kbagent agent test --prompt "a --zz" --bogus', root) == (
        "agent test has no option --bogus"
    )


def test_checker_reads_options_after_a_spaced_alternation() -> None:
    root = typer.main.get_command(app)
    [(_, ex)] = _examples("```\nkbagent agent test (--type T | --bogus X)\n```\n", markdown=True)
    assert _problem(ex, root) == "agent test has no option --bogus"
    [(_, ex)] = _examples("```\nkbagent --json project list | jq .\n```\n", markdown=True)
    assert ex == "kbagent --json project list"


# --------------------------------------------------------------------------- #
# Agents pass --project on every command (docs/migrate-to-1.0.md, "Plugin"):
# the active project is shared by every terminal, agent conversation and
# `kbagent serve` of one configuration directory.
# --------------------------------------------------------------------------- #

# Options with which a command chooses its projects itself.
_PROJECT_CHOICE = {"--project", "-p", "--all-projects", "--from-csv"}


def _command_and_options(example: str, root: Any) -> tuple[Any, list[str]] | None:
    """The leaf command of ``example`` and the tokens after its path, or None."""
    ctx = type(root).context_class(root)
    tokens = _QUOTED.sub(" VALUE ", example).split()[1:]
    root_valued = {o for p in root.params if not getattr(p, "is_flag", False) for o in p.opts}
    i = 0
    while i < len(tokens) and tokens[i].startswith("-"):
        i += 2 if tokens[i] in root_valued else 1
    node = root
    while _is_group(node) and i < len(tokens):
        if not _WORD.fullmatch(tokens[i]):
            return None
        node = node.get_command(ctx, tokens[i])
        if node is None:
            return None
        i += 1
    if _is_group(node):
        return None
    return node, re.sub(r"[\[\]()|]", " ", " ".join(tokens[i:])).split()


def _leaves_out_the_project(example: str, root: Any) -> bool:
    found = _command_and_options(example, root)
    if found is None:
        return False
    command, rest = found
    if target_project_param(command) is None:
        return False
    # A command name in prose, a list of command names and a --help call are
    # not examples to run.
    if "--help" in rest or not any(token.startswith("-") for token in rest):
        return False
    if command.operation == "project.refresh" and "--all" in rest:
        return False  # the deprecated name of --all-projects, the form 0.x knows
    return not any(token.split("=")[0] in _PROJECT_CHOICE for token in rest)


def test_agent_examples_name_the_project() -> None:
    """An example in the plugin documents that shows options also shows --project.

    A heading is skipped: other documents link to its anchor.
    """
    root = typer.main.get_command(app)
    missing = []
    for path in sorted((REPO_ROOT / "plugins" / "kbagent").rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        for n, ex in _examples(text, markdown=True):
            if not lines[n - 1].startswith("#") and _leaves_out_the_project(ex, root):
                missing.append(f"{path.relative_to(REPO_ROOT)}:{n}: {ex}")
    assert not missing, "\n".join(missing)


def test_checker_finds_an_example_without_the_project() -> None:
    root = typer.main.get_command(app)
    assert _leaves_out_the_project("kbagent --json config update --set a=b", root)
    assert not _leaves_out_the_project("kbagent config update --project P --set a=b", root)
    assert not _leaves_out_the_project("kbagent config list --all-projects", root)
    assert not _leaves_out_the_project("kbagent config update", root)  # a command name
    assert not _leaves_out_the_project("kbagent config update --help", root)
    assert not _leaves_out_the_project("kbagent project add --alias x", root)  # a new alias
