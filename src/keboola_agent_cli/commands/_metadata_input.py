"""Helpers for resolving text input from --text, --file, or --stdin, and file input.

Shared by `branch metadata-set` and `project description-set`; the
``--configuration-file`` helpers serve `config update/new/row-create/row-update`. Enforces
mutual exclusivity so the CLI fails fast with a clear error instead of
silently picking one source.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import typer

from ..errors import ConfigError, ErrorCode
from ._helpers import read_file_arg


def resolve_text_input(
    *,
    text: str | None,
    file: Path | None,
    stdin: bool,
) -> str:
    """Resolve a single string value from exactly one of three sources.

    Args:
        text: Inline string value, or None.
        file: Path to a file to read UTF-8 text from (``-`` reads stdin), or None.
        stdin: If True, read the value from standard input.

    Returns:
        The resolved string.

    Raises:
        ConfigError: If zero or more than one source is provided, or if
            the given file cannot be read.
    """
    sources = [text is not None, file is not None, stdin]
    provided = sum(sources)
    if provided == 0:
        raise ConfigError("Specify exactly one of --text, --file, or --stdin.")
    if provided > 1:
        raise ConfigError("--text, --file, and --stdin are mutually exclusive.")

    if text is not None:
        return text
    if file is not None:
        if str(file) == "-":  # the 1.x --<field>-file form reads stdin from `-`
            return read_file_arg(file, label="--file")
        if not file.is_file():
            raise ConfigError(f"Input file not found: {file}")
        return file.read_text(encoding="utf-8")
    return sys.stdin.read()


def configuration_file_text(path: Path) -> str:
    """The text of ``--configuration-file``: a path, or ``-`` for stdin (the 1.x form)."""
    if str(path) == "-":
        return read_file_arg(path, label="--configuration-file")
    return path.read_text(encoding="utf-8")


def row_configuration_file(formatter: Any, configuration: str | None, path: Path) -> dict:
    """The row configuration of ``--configuration-file`` (the 1.x form; ``-`` is stdin)."""
    if configuration:
        formatter.error(
            message="Cannot use both --configuration and --configuration-file.",
            error_code=ErrorCode.VALIDATION_ERROR,
        )
        raise typer.Exit(code=2)
    try:
        return json.loads(configuration_file_text(path))
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        formatter.error(
            message=f"Invalid --configuration-file input: {exc}",
            error_code=ErrorCode.VALIDATION_ERROR,
        )
        raise typer.Exit(code=2) from None
