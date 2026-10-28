"""Refresh the config-derived fields of a ``pyproject.toml`` after a push.

Pull renders ``pyproject.toml`` for a Python transformation or app from the
config: ``[project] name`` from the config's name and, for an app,
``[tool.keboola] config_id`` from its id. Push and clone never refreshed them,
so after a rename (or the create that assigns the id) the file kept the old
values and its recorded hash described a file a fresh pull would not write.

Only those owned fields change. Everything else is the user's -- other
tables, comments, a custom ``requires-python``, the line endings -- and stays
byte for byte, which is why this edits lines instead of re-rendering the file.
No file is created: pull writes none for a config without packages.
"""

from __future__ import annotations

import re
from pathlib import Path

from .code_extraction import (
    PYPROJECT_FILENAME,
    PYTHON_APP_COMPONENTS,
    owned_companions,
    pyproject_name,
    toml_string,
)

# A table header (``[a.b]``) or an array-of-tables header (``[[a]]``, no table
# this module edits), with an optional trailing comment.
_HEADER = re.compile(r"^\s*(\[\[?)\s*([^\[\]]+?)\s*\]\]?\s*(#.*)?$")
# ``key = "value"`` or ``key = 'value'``, keeping the indent and anything after
# the value (a comment). A value of any other shape is left alone.
_VALUE = r"^(\s*{key}\s*=\s*)(\"(?:[^\"\\]|\\.)*\"|'[^']*')(.*)$"


def refresh_pyproject_identity(
    component_id: str, config_dir: Path, *, name: str, config_id: str
) -> None:
    """Point the owned fields of *config_dir*'s ``pyproject.toml`` at the config.

    *name* is the config's name; *config_id* its id, or ``""`` when the file
    must not get one (the promote path keeps the source tree's own ids, and a
    config not created yet has none). ``[tool.keboola]`` is an app's only.
    """
    path = config_dir / PYPROJECT_FILENAME
    if PYPROJECT_FILENAME not in owned_companions(component_id) or not path.is_file():
        return
    original = path.read_bytes()
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError:
        return
    lines = text.splitlines(keepends=True)
    if name:
        _set_value(lines, "project", "name", pyproject_name(name), add=False)
    if config_id and component_id in PYTHON_APP_COMPONENTS:
        newline = "\r\n" if "\r\n" in text else "\n"
        if not _set_value(lines, "tool.keboola", "config_id", config_id, add=True):
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += newline
            lines += [
                newline,
                f"[tool.keboola]{newline}",
                f"component_id = {toml_string(component_id)}{newline}",
                f"config_id = {toml_string(config_id)}{newline}",
            ]
    updated = "".join(lines).encode("utf-8")
    if updated != original:
        path.write_bytes(updated)


def _set_value(lines: list[str], table: str, key: str, value: str, *, add: bool) -> bool:
    """Set *key* in *table* in place; with *add*, append it to the table when absent.

    Returns False when the table does not exist (nothing changed).
    """
    pattern = re.compile(_VALUE.format(key=re.escape(key)))
    start: int | None = None
    end = len(lines)
    for i, line in enumerate(lines):
        header = _HEADER.match(line.rstrip("\r\n"))
        if header is None:
            continue
        if start is not None:
            end = i
            break
        if header.group(1) == "[" and header.group(2) == table:
            start = i
    if start is None:
        return False
    for i in range(start + 1, end):
        body = lines[i].rstrip("\r\n")
        match = pattern.match(body)
        if match:
            lines[i] = match.group(1) + toml_string(value) + match.group(3) + lines[i][len(body) :]
            return True
    if add:
        last = max(
            (
                i
                for i in range(start, end)
                if lines[i].strip() and not lines[i].lstrip().startswith("#")
            ),
            default=start,
        )
        ending = lines[last][len(lines[last].rstrip("\r\n")) :] or "\n"
        if not lines[last].endswith(("\n", "\r")):
            lines[last] += ending
        lines.insert(last + 1, f"{key} = {toml_string(value)}{ending}")
    return True
