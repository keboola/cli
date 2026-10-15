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
            out.append((n, re.split(r"\s+(?:#|\\$|2?>|\|)|\s*(?:&&|;)", ex)[0].strip()))
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
    assert _problem("kbagent project list --json", root) == "project list has no option --json"
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
