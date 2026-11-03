"""Helper for resolving text input from ``--<field>`` or ``--<field>-file``.

Shared by `branch metadata-set`, `project description-set` and `storage
describe-bucket/describe-table`. Enforces mutual exclusivity so the CLI fails
fast with a clear error instead of silently picking one source (spec rule I2).
"""

from __future__ import annotations

from pathlib import Path

from ..errors import ConfigError
from ._helpers import text_input


def resolve_text_input(*, value: str | None, file: Path | None, option: str) -> str:
    """Resolve a single string value from exactly one of ``option`` and ``<option>-file``.

    Args:
        value: Inline string value, or None.
        file: Path to a file to read UTF-8 text from (``-`` reads stdin), or None.
        option: The inline option name, e.g. ``--description``.

    Returns:
        The resolved string.

    Raises:
        ConfigError: If neither or both sources are provided, or if the given
            file cannot be read.
    """
    if value is None and file is None:
        raise ConfigError(f"Specify exactly one of {option} or {option}-file.")
    try:
        return text_input(value, file, label=option) or ""
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
