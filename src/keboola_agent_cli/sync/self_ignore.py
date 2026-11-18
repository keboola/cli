"""A ``.gitignore`` holding ``*`` that keeps a ``.keboola/`` folder out of git.

Shared by the removal quarantine (``.keboola/removing/``) and the directory
lock (``.keboola/lock/``): their contents are local, per-run state that must
never show in ``git status`` or be committed.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from pathlib import Path

IGNORE_FILENAME = ".gitignore"
_IGNORE_RULE = b"*\n"


class SelfIgnoreError(Exception):
    """The ignore file is not a regular file, or could not be written."""


def ensure_self_ignore(ignore: Path) -> None:
    """Make *ignore* a regular file holding ``*``, never through a link (K3).

    A link (dangling or not) or anything but a regular file raises
    :class:`SelfIgnoreError`: writing through it could create or change a file
    outside the project. An existing file is never written into -- it may be a
    hard link to a file elsewhere: a fresh file is created exclusively next to
    it (no link followed) and renamed over it, so only the directory entry
    changes.
    """
    if ignore.is_symlink() or (ignore.exists() and not ignore.is_file()):
        raise SelfIgnoreError(f"{ignore.name} in it is not a regular file")
    if ignore.is_file() and ignore.read_bytes() == _IGNORE_RULE:
        return
    temp = ignore.with_name(f"{ignore.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(temp, flags, 0o644)
        with os.fdopen(fd, "wb") as handle:
            handle.write(_IGNORE_RULE)
        os.replace(temp, ignore)
    except OSError as exc:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise SelfIgnoreError(f"{ignore.name} could not be written ({exc})") from exc
