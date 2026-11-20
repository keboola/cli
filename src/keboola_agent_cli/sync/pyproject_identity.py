"""Refresh the config-derived fields of a ``pyproject.toml`` after a push.

Pull renders ``pyproject.toml`` for a Python transformation or app from the
config: ``[project] name`` from the config's name and, for an app,
``[tool.keboola] config_id`` from its id. Push and clone never refreshed them,
so after a rename (or the create that assigns the id) the file kept the old
values and its recorded hash described a file a fresh pull would not write.

Only those owned fields change. Everything else is the user's -- other
tables, comments, a custom ``requires-python``, the line endings -- and stays
byte for byte, which is why this edits lines instead of re-rendering the file.
No file is created: pull writes none for a config without packages. A file the
line edit cannot change safely is left alone (the push then warns).
"""

from __future__ import annotations

import copy
import hashlib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
_STRING_VALUE = r"(\"(?:[^\"\\]|\\.)*\"|'[^']*')(.*)$"


def _value_pattern(key: str) -> re.Pattern[str]:
    return re.compile(rf"^(\s*{re.escape(key)}\s*=\s*)" + _STRING_VALUE)


@dataclass(frozen=True)
class PyprojectRefresh:
    """What :func:`refresh_pyproject` did to a ``pyproject.toml``.

    Attributes:
        refreshed: The owned fields now hold the config's values (or the
            component owns no ``pyproject.toml``, or there is none). False:
            the file was left as it was.
        file_hash: SHA-256 of the bytes the file holds as this call knows
            them -- the bytes it wrote, or the bytes it read when it wrote
            nothing. Empty when there is no file it owns, or it could not be
            read. Never a fresh read after the write: a push stamps this hash,
            so an edit saved after the write stays visible to ``sync diff``
            (#827).
        edited: *expected_hash* was given and the file no longer had it: it
            was edited after push read it. Nothing was written.
    """

    refreshed: bool
    file_hash: str = ""
    edited: bool = False


def refresh_pyproject_identity(
    component_id: str, config_dir: Path, *, name: str, config_id: str
) -> bool:
    """True iff :func:`refresh_pyproject` left the owned fields pointing at the config."""
    return refresh_pyproject(component_id, config_dir, name=name, config_id=config_id).refreshed


def refresh_pyproject(
    component_id: str,
    config_dir: Path,
    *,
    name: str,
    config_id: str,
    expected_hash: str | None = None,
) -> PyprojectRefresh:
    """Point the owned fields of *config_dir*'s ``pyproject.toml`` at the config.

    *name* is the config's name; *config_id* its id, or ``""`` when the file
    must not get one (the promote path keeps the source tree's own ids, and a
    config not created yet has none). ``[tool.keboola]`` is an app's only.

    The line edit covers the layout pull writes. Any other valid TOML spelling
    (a multi-line string, a quoted key, an inline ``keboola = {...}`` table, a
    number) could turn into invalid TOML or a second definition, so the result
    is parsed and must equal the file's own content with only the owned values
    changed. When it does not -- or the file is no valid TOML to begin with,
    or cannot be read or written -- the file is left as it was and ``refreshed`` is False: the
    remote write already happened, so the caller warns instead of failing.

    *expected_hash* is the hash of the file push read and sent. A file that
    no longer has it was edited during the push: it is left as it is
    (``edited``), so the edit is neither overwritten nor taken for synced.
    """
    path = config_dir / PYPROJECT_FILENAME
    if PYPROJECT_FILENAME not in owned_companions(component_id) or not path.is_file():
        return PyprojectRefresh(refreshed=True)
    try:
        original = path.read_bytes()
    except OSError:
        return PyprojectRefresh(refreshed=False)
    read_hash = _sha256(original)
    if expected_hash is not None and read_hash != expected_hash:
        return PyprojectRefresh(refreshed=False, edited=True)
    try:
        text = original.decode("utf-8")
        before = _parse(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return PyprojectRefresh(refreshed=False, file_hash=read_hash)
    expected = _expected(before, component_id, name=name, config_id=config_id)
    if expected is None:
        return PyprojectRefresh(refreshed=False, file_hash=read_hash)
    if expected == before:
        return PyprojectRefresh(refreshed=True, file_hash=read_hash)
    lines = text.splitlines(keepends=True)
    project = before.get("project")
    if isinstance(project, dict) and expected["project"].get("name") != project.get("name"):
        _set_value(lines, "project", "name", expected["project"]["name"], add=False)
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
    updated = "".join(lines)
    try:
        if _parse(updated) != expected:
            return PyprojectRefresh(refreshed=False, file_hash=read_hash)
    except tomllib.TOMLDecodeError:
        return PyprojectRefresh(refreshed=False, file_hash=read_hash)
    generated = updated.encode("utf-8")
    try:
        path.write_bytes(generated)
    except OSError:
        # A read-only file: the remote write already happened, so the push
        # warns and still stamps its baseline. Opening for write fails before
        # any byte is written, so the file is as it was.
        return PyprojectRefresh(refreshed=False, file_hash=read_hash)
    return PyprojectRefresh(refreshed=True, file_hash=_sha256(generated))


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _parse(text: str) -> dict[str, Any]:
    """Parse TOML with floats kept as their source text.

    The before/after comparison must hold for any valid file, and a float
    ``nan`` never equals itself: an unrelated ``value = nan`` refused the refresh.
    """
    return tomllib.loads(text, parse_float=str)


def _expected(
    before: dict[str, Any], component_id: str, *, name: str, config_id: str
) -> dict[str, Any] | None:
    """*before* with the owned values set, or ``None`` when its shape has no place for them."""
    expected = copy.deepcopy(before)
    project = expected.get("project")
    if name and isinstance(project, dict) and "name" in project:
        project["name"] = pyproject_name(name)
    if config_id and component_id in PYTHON_APP_COMPONENTS:
        tool = expected.setdefault("tool", {})
        if not isinstance(tool, dict):
            return None
        table = tool.get("keboola")
        if table is None:
            tool["keboola"] = {"component_id": component_id, "config_id": config_id}
        elif isinstance(table, dict):
            table["config_id"] = config_id
        else:
            return None
    return expected


def _set_value(lines: list[str], table: str, key: str, value: str, *, add: bool) -> bool:
    """Set *key* in *table* in place; with *add*, append it to the table when absent.

    Returns False when the table does not exist (nothing changed).
    """
    pattern = _value_pattern(key)
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
