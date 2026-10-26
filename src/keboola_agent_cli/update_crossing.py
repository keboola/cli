"""The runs after an update (#791 section 4.4, spec C5, breaking-change row 28).

An update is known to the run that follows it in two ways: the
``KBAGENT_UPDATED_FROM`` variable that a POSIX update sets before it runs the
same command again, and the marker of a finished deferred update, whose
``from_version`` names the old version (claimed by one process only, see
:func:`.update_runner.collect_finished_deferred_update`).
:func:`note_finished_update` handles both:

* ``UPDATED_ACROSS_BREAKING``, a ``warnings[]`` entry of that one run, when the
  crossed versions carry ``BREAKING:`` changelog notes.
* an automatic update to a higher major (not ``kbagent update --major``)
  writes the state file ``crossing_pending.json``. While it exists, every read
  gets an ``UPDATED_ACROSS_MAJOR`` warning (:func:`announce_pending_crossing`).
  The first write or destructive command claims the file with an atomic move
  and is refused (:func:`refusal_for`). Of several processes at most one wins
  the claim. Every later command runs.

The protection fails open: a state file that cannot be written, read, parsed
or claimed never refuses anything.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

from . import update_notice, update_runner
from .changelog import crossed_breaking_notes
from .constants import CROSSING_PENDING_FILENAME, migration_guide_url
from .errors import ErrorCode
from .permissions import OPERATION_REGISTRY

logger = logging.getLogger(__name__)

# Risk classes of OPERATION_REGISTRY that the first command after a major crossing refuses.
# Admin-class commands (`project add`, `serve`, `update`, ...) are not refused.
REFUSED_CATEGORIES = frozenset({"write", "destructive"})


def _major(version: str) -> int | None:
    try:
        return Version(version).major
    except InvalidVersion:
        return None


def _state_path() -> Path:
    return update_runner.state_dir() / CROSSING_PENDING_FILENAME


def _write_state(payload: dict[str, Any]) -> None:
    """Write the state file in one atomic step. A failure only logs (fail open)."""
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
                tmp_file.write(json.dumps(payload))
            os.replace(tmp_path, path)
        except OSError:
            tmp_path.unlink(missing_ok=True)
            raise
    except OSError:
        logger.debug("Could not write the major-crossing state", exc_info=True)


def _valid_state(data: object) -> dict[str, str] | None:
    if not isinstance(data, dict):
        return None
    from_version, to_version = data.get("from_version"), data.get("to_version")
    if not isinstance(from_version, str) or not isinstance(to_version, str):
        return None
    if _major(to_version) is None:
        return None
    return {"from_version": from_version, "to_version": to_version}


def _guide_url(state: dict[str, str]) -> str:
    """The migration guide of the major that ``state`` crossed into (parsed by :func:`_valid_state`)."""
    return migration_guide_url(Version(state["to_version"]).major)


def _read_state() -> dict[str, str] | None:
    try:
        return _valid_state(json.loads(_state_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def _message(state: dict[str, str]) -> str:
    return (
        f"kbagent was updated from v{state['from_version']} to v{state['to_version']}, a new "
        f"major version. Read what changed: {_guide_url(state)}."
    )


def note_finished_update(from_version: str, to_version: str, *, major_flag: bool = False) -> None:
    """Record an update that finished before this run (see the module docstring).

    Args:
        from_version: The version that ran before the update.
        to_version: The running version.
        major_flag: The update was started with ``kbagent update --major``:
            the user chose the new major, so nothing is refused.
    """
    crossed = crossed_breaking_notes(from_version, to_version)
    if crossed:
        versions = list(crossed)
        update_notice.add_entry(
            {
                "code": ErrorCode.UPDATED_ACROSS_BREAKING.value,
                "message": (
                    f"kbagent was updated from v{from_version} to v{to_version}. "
                    f"These versions have BREAKING changes: {', '.join(versions)}."
                ),
                "hint": "kbagent changelog --full",
                "from_version": from_version,
                "to_version": to_version,
                "versions": versions,
            }
        )
    old_major, new_major = _major(from_version), _major(to_version)
    if major_flag or old_major is None or new_major is None or old_major >= new_major:
        return
    _write_state(
        {"from_version": from_version, "to_version": to_version, "recorded_at": time.time()}
    )


def announce_pending_crossing(*, json_mode: bool) -> None:
    """Warn while a major crossing waits for its first write or destructive command.

    ``--json``: an ``UPDATED_ACROSS_MAJOR`` entry in ``warnings[]``, which a
    refused command removes. Human mode: one line on stderr
    (:func:`crossing_text`), printed at once for a top-level command (never
    refused) and otherwise held until the group callback knows that the
    command is not refused (:func:`print_held_warning`). Nothing when the
    state file is missing or cannot be read.
    """
    state = _read_state()
    if state is None:
        return
    update_notice.add_entry(
        {
            "code": ErrorCode.UPDATED_ACROSS_MAJOR.value,
            "message": _message(state),
            "hint": _guide_url(state),
            **state,
            "url": _guide_url(state),
        }
    )
    if json_mode:
        return
    if _first_positional(sys.argv[1:]) in OPERATION_REGISTRY:
        sys.stderr.write(crossing_text(state))
    else:
        update_notice.hold_text(crossing_text(state))


def print_held_warning() -> None:
    """Print the held human warning line of a command that is not refused."""
    text = update_notice.take_held_text()
    if text is not None:
        sys.stderr.write(text)


# Root options that take the next token as their value.
_ROOT_VALUE_OPTIONS = frozenset({"--config-dir", "--conversation-id"})


def _first_positional(args: list[str]) -> str | None:
    """The top-level command or group of ``args``, after the root options."""
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg in _ROOT_VALUE_OPTIONS:
            skip = True
        elif not arg.startswith("-"):
            return arg
    return None


def crossing_text(state: dict[str, str]) -> str:
    """The human warning line while a major crossing is pending."""
    return (
        f"Warning: {_message(state)} Reads run with this warning until the first write or "
        "destructive command. That command is refused once, then every command runs.\n"
    )


def refusal_for(operation: str) -> dict[str, Any] | None:
    """The refusal of ``operation`` after a major crossing, or None.

    Only a write or destructive operation of :data:`OPERATION_REGISTRY` is
    refused, and only by the process that claims the state file. A failed
    claim (another process won, or the directory does not allow the move) or
    an unreadable file refuses nothing. Returns the ``message`` and the
    ``details`` of the error.
    """
    if OPERATION_REGISTRY.get(operation) not in REFUSED_CATEGORIES:
        return None
    path = _state_path()
    if update_runner.claim_file(path) is not update_runner.ClaimResult.CLAIMED:
        return None
    state = _valid_state(update_runner.read_claimed_json(path))
    update_notice.take_entry(ErrorCode.UPDATED_ACROSS_MAJOR)
    update_notice.take_held_text()
    if state is None:
        return None
    new_major = _major(state["to_version"])
    return {
        "message": (
            f"kbagent was updated from v{state['from_version']} to v{state['to_version']}, "
            f"a new major version. '{operation}' did not run: kbagent {new_major}.x changes "
            f"defaults that this command can depend on. Read what changed: "
            f"{_guide_url(state)}. Run the command again to proceed."
        ),
        "details": {**state, "url": _guide_url(state)},
    }
