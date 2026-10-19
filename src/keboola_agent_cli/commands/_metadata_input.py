"""Helper for resolving text input from --text, --file, or --stdin.

Shared by `branch metadata-set` and `project description-set`. Enforces
mutual exclusivity so the CLI fails fast with a clear error instead of
silently picking one source.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..errors import ConfigError


def resolve_text_input(
    *,
    text: str | None,
    file: Path | None,
    stdin: bool,
) -> str:
    """Resolve a single string value from exactly one of three sources.

    Args:
        text: Inline string value, or None.
        file: Path to a file to read UTF-8 text from, or None.
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
        if not file.is_file():
            raise ConfigError(f"Input file not found: {file}")
        try:
            return file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ConfigError(f"--file: cannot read file {file}: {exc}") from None
    return sys.stdin.read()
