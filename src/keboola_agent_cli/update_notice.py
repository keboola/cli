"""The deprecation notice of a running major once the next major is out (#791 section 4.3).

The notice starts when the client first sees a published, stable release
``v<N+1>.0.0``. Human mode prints it as one stderr line; ``--json`` adds a
``DEPRECATED_VERSION`` entry to the top-level ``warnings[]`` of the envelope.

The startup auto-update runs before the ``OutputFormatter`` exists, so the
notice is kept here, at module level, and the envelope builders in
:mod:`keboola_agent_cli.output` read it from :func:`pending_warnings`. This
module imports nothing from the rest of the package except constants, so the
updater and the output layer can both import it.
"""

import enum
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .constants import MIGRATION_GUIDE_URL

DEPRECATED_VERSION_CODE = "DEPRECATED_VERSION"
UPDATE_MAJOR_HINT = "kbagent update --major"


class NoticeMode(enum.Enum):
    """Which of the three text forms applies to this installation."""

    AUTO_UPDATE = "auto_update"  # the startup check installs the next major at M
    AUTO_UPDATE_OFF = "auto_update_off"  # KBAGENT_AUTO_UPDATE=false
    FROZEN = "frozen"  # a native binary; its package manager upgrades it


@dataclass(frozen=True)
class DeprecationNotice:
    """The human text and the ``warnings[]`` entry of one notice."""

    text: str
    entry: dict[str, Any]


_current: DeprecationNotice | None = None
_printed = False


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_notice(
    *,
    current_version: str,
    next_version: str,
    published_ts: float | None,
    migration_ts: float | None,
    now: float,
    mode: NoticeMode,
    channel_command: str | None = None,
) -> DeprecationNotice:
    """Build the notice text and its ``warnings[]`` entry.

    Args:
        current_version: The running version, e.g. ``0.105.2``.
        next_version: The next major release, e.g. ``1.0.0``.
        published_ts: Its ``published_at``; None when the date is unknown
            (the release answered 404 after it was seen).
        migration_ts: M, or None when the date is unknown.
        now: Current time, for the form used at and after M.
        mode: Which text form applies.
        channel_command: The package manager's upgrade command (frozen binary).
    """
    current_parts = current_version.split(".")
    current_major = current_parts[0]
    next_major = next_version.split(".")[0]
    short = ".".join(current_parts[:2])
    parts = [f"kbagent {short} is deprecated."]
    if published_ts is not None:
        released = datetime.fromtimestamp(published_ts, UTC)
        parts.append(f"kbagent {next_version} was released on {released:%Y-%m-%d %H:%M} UTC.")
    else:
        parts.append(f"kbagent {next_version} was released, but its release date is unknown now.")
    if mode is NoticeMode.FROZEN:
        command = channel_command or "upgrade kbagent with the package manager that installed it"
        parts.append(f"Your package manager installs {next_major}.x now: {command.rstrip('.')}.")
    elif mode is NoticeMode.AUTO_UPDATE_OFF:
        parts.append(f"This installation stays on {current_major}.x; run {UPDATE_MAJOR_HINT}.")
    elif migration_ts is None:
        parts.append(
            f"This installation stays on {current_major}.x until the release date is known."
        )
    elif now >= migration_ts:
        parts.append(
            f"This installation updates itself to {next_major}.x once every kbagent process "
            "has exited; restart a running kbagent serve."
        )
    else:
        migration = datetime.fromtimestamp(migration_ts, UTC)
        parts.append(
            f"This installation updates itself to {next_major}.x on "
            f"{migration:%Y-%m-%d} at {migration:%H:%M} UTC."
        )
    parts.append(f"What changes: {MIGRATION_GUIDE_URL}.")
    if mode is NoticeMode.AUTO_UPDATE:
        parts.append(f"Update now: {UPDATE_MAJOR_HINT}.")
    # A `keboola-cli<N` pin alone does not hold a self-updating install: the
    # updater reinstalls from an exact wheel URL and `uv tool` keeps no pin.
    if mode is NoticeMode.AUTO_UPDATE:
        parts.append(
            f"To stay on {current_major}.x: set KBAGENT_AUTO_UPDATE=false "
            f"(and pin keboola-cli<{next_major} in CI)."
        )
    elif mode is NoticeMode.AUTO_UPDATE_OFF:
        parts.append(f"In CI, also pin keboola-cli<{next_major} for the install.")
    text = " ".join(parts)
    entry: dict[str, Any] = {
        "code": DEPRECATED_VERSION_CODE,
        "message": text,
        "hint": UPDATE_MAJOR_HINT,
        "current_version": current_version,
        "next_version": next_version,
        "released_at": _iso(published_ts),
        "migration_at": _iso(migration_ts),
        "url": MIGRATION_GUIDE_URL,
    }
    return DeprecationNotice(text=text, entry=entry)


def register(notice: DeprecationNotice | None) -> None:
    """Keep ``notice`` for the envelope builders; a later call replaces it."""
    global _current
    if notice is not None:
        _current = notice


def current() -> DeprecationNotice | None:
    return _current


def pending_warnings() -> list[dict[str, Any]]:
    """The ``warnings[]`` entries the startup check left for the ``--json`` envelope."""
    return [dict(_current.entry)] if _current is not None else []


def print_once() -> bool:
    """Write the notice to stderr unless it was written in this process already.

    Returns:
        True when a line was written.
    """
    global _printed
    if _current is None or _printed:
        return False
    sys.stderr.write(f"Warning: {_current.text}\n")
    _printed = True
    return True


def reset() -> None:
    """Forget the notice and whether it was printed (for tests)."""
    global _current, _printed
    _current = None
    _printed = False
