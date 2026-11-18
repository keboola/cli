"""One kbagent run at a time per sync directory (#827 follow-up, L1-L4).

Two runs in one sync working tree -- a second ``sync push`` started while the
first still runs, a ``sync pull`` during a push -- read and write the same
manifest, ``pendingRemovals`` journal, quarantine and config files, and each
silently undoes the other's bookkeeping. Every command that reads or writes a
tree takes this lock first, for its whole run, and a second run fails fast
with ``SYNC_DIRECTORY_BUSY`` (retryable: wait, then run it again).

- **The lock (L1, L2).** ``<root>/.keboola/lock/sync.lock``, an OS lock
  (``flock`` on POSIX, ``LockFileEx`` on Windows) through filelock's native
  class -- never its soft (marker file) lock: the kernel releases an OS lock
  when its holder dies, so a crashed run never blocks the next one, and a
  soft lock would not. The file is never deleted (deleting a locked file lets
  a third run lock a new file of the same name). ``lock/`` carries a
  ``.gitignore`` holding ``*``, so it never shows in ``git status``.
- **Ownership (L3).** Per thread, keyed by the resolved root: the thread that
  holds a tree may enter it again (push runs diff, clone runs push, pull runs
  init), any other thread or process fails busy. A forked child starts with
  no ownership.
- **Errors (L4).** Only "held by another run" is ``SYNC_DIRECTORY_BUSY``.
  Any other failure to create, open or lock the file is ``ConfigError``
  naming the file and the OS error -- never reported as busy.

Limits: the OS lock needs a local disk; on a network drive (NFS, SMB) it may
not exclude a run on another machine. A case or mount alias of the root
(``/Repo`` vs ``/repo`` on a case-insensitive disk) is another key in this
process, but the same file to the OS, so it fails busy instead of nesting.
"""

from __future__ import annotations

import contextlib
import errno
import functools
import inspect
import os
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from filelock import Timeout

from ..constants import KEBOOLA_DIR_NAME
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from .manifest import load_manifest
from .self_ignore import IGNORE_FILENAME, SelfIgnoreError, ensure_self_ignore

if sys.platform == "win32":
    from filelock import WindowsFileLock as _NativeFileLock
else:
    from filelock import UnixFileLock as _NativeFileLock

LOCK_DIR = "lock"
LOCK_FILENAME = "sync.lock"


@dataclass
class _Hold:
    """One tree this thread holds: the OS lock and how deep the nesting is."""

    lock: Any
    depth: int


_owner = threading.local()
# The stand-in lock of a directory entered UNLOCKED (no sync tree yet).
_UNLOCKED = object()
# errnos of a tree that cannot be written (PermissionError covers EACCES/EPERM
# and Windows' access denied).
_READ_ONLY = frozenset({errno.EROFS, errno.EACCES, errno.EPERM})


def _holds() -> dict[Path, _Hold]:
    """The trees the calling thread holds, by resolved root."""
    holds = getattr(_owner, "holds", None)
    if holds is None:
        holds = _owner.holds = {}
    return holds


def _forget_after_fork() -> None:
    """A forked child holds no tree: its parent's ownership is not its own (L3)."""
    global _owner
    _owner = threading.local()


if hasattr(os, "register_at_fork"):  # POSIX only
    os.register_at_fork(after_in_child=_forget_after_fork)


def lock_file_path(project_root: Path) -> Path:
    """Where the lock of *project_root* lives."""
    return project_root / KEBOOLA_DIR_NAME / LOCK_DIR / LOCK_FILENAME


def is_directory_busy(exc: BaseException) -> bool:
    """True iff *exc* is the ``SYNC_DIRECTORY_BUSY`` error (L6: never swallowed)."""
    return getattr(exc, "error_code", None) == ErrorCode.SYNC_DIRECTORY_BUSY


def busy_error(root: Path) -> KeboolaApiError:
    """The error of a tree another kbagent run holds (L4)."""
    lock_file = lock_file_path(root)
    return KeboolaApiError(
        message=(
            f"The sync directory {root} is in use by another kbagent run (sync, or config "
            "new/rename writing into it). Nothing was done. Wait for that run to finish, "
            f"then run this command again. Do not delete {lock_file}."
        ),
        error_code=ErrorCode.SYNC_DIRECTORY_BUSY,
        retryable=True,
        details={"directory": str(root), "lock_file": str(lock_file)},
    )


def _not_writable(exc: BaseException) -> bool:
    """True iff *exc* says the tree cannot be written (read-only mount, no permission)."""
    cause = exc.__cause__ if isinstance(exc, SelfIgnoreError) else exc
    return isinstance(cause, PermissionError) or getattr(cause, "errno", None) in _READ_ONLY


def _acquire(root: Path, *, reader: bool = False) -> Any:
    """Take the OS lock of *root* without waiting; busy or ``ConfigError`` otherwise.

    A *reader* (status, diff, branch-status, a clone's source) on a tree it
    cannot write gets no lock instead (PR #1016 review NB-2): no kbagent
    writer can change a tree that cannot be written, so the read is safe. A
    lock another run holds stays busy; a writer keeps the ``ConfigError``.
    """
    lock_dir = root / KEBOOLA_DIR_NAME / LOCK_DIR
    lock_file = lock_dir / LOCK_FILENAME
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
        # A linked .keboola/ or lock/ would put the lock (and the .gitignore
        # written next to it) outside the project. The root is resolved again
        # now that it exists (a Windows short name expands only then).
        if lock_dir.resolve() != root.resolve() / KEBOOLA_DIR_NAME / LOCK_DIR:
            raise ConfigError(f"The sync lock directory {lock_dir} resolves outside {root}.")
        ensure_self_ignore(lock_dir / IGNORE_FILENAME)
        lock = _NativeFileLock(
            str(lock_file),
            timeout=0,
            blocking=False,
            preserve_lock_file=True,
            fallback_to_soft=False,
        )
        lock.acquire()
    except Timeout:
        raise busy_error(root) from None
    except (OSError, SelfIgnoreError) as exc:
        if reader and _not_writable(exc):
            return _UNLOCKED
        raise ConfigError(
            f"Could not lock the sync directory {root} ({lock_file}): {exc}. The lock needs a "
            "writable .keboola/lock/ folder on a local disk."
        ) from exc
    return lock


# What a directory without ``.keboola/`` (no sync tree yet) gets (L5):
# CREATE takes the lock and creates the folder (init, pull: they make the tree);
# REFUSE raises the "no manifest" error before the block runs (status, diff,
# push, branch-*: they need a tree, and a body that ran unlocked would not be
# serialized with a first pull starting there meanwhile);
# UNLOCKED runs the block without the OS lock (the scaffold placement of a
# direct SDK call into a plain directory, which then reads no manifest). The
# decision holds for the thread's whole block. `config new --output-dir` and
# `config rename --directory` decide before they start instead: a folder that
# is no sync tree then is never read or written as one (#827 round 2).
CREATE = "create"
REFUSE = "refuse"
UNLOCKED = "unlocked"


@contextlib.contextmanager
def sync_dir_lock(
    project_root: Path | str, *, outside: str = CREATE, reader: bool = False
) -> Iterator[None]:
    """Hold the lock of *project_root* for the ``with`` block (L1-L4).

    The thread that already holds the tree -- or entered it UNLOCKED -- just
    nests. *outside* decides for a directory that has no ``.keboola/`` yet:
    :data:`CREATE`, :data:`REFUSE` or :data:`UNLOCKED`. A *reader* runs
    unlocked on a tree it cannot write (see :func:`_acquire`).
    """
    root = Path(project_root).resolve()
    holds = _holds()
    hold = holds.get(root)
    if hold is not None:
        hold.depth += 1
        try:
            yield
        finally:
            hold.depth -= 1
        return
    no_tree = outside != CREATE and not (root / KEBOOLA_DIR_NAME).is_dir()
    if no_tree and outside == REFUSE:
        load_manifest(root)  # raises the "no manifest" FileNotFoundError
    lock: Any = _UNLOCKED if no_tree and outside == UNLOCKED else _acquire(root, reader=reader)
    holds[root] = _Hold(lock=lock, depth=1)
    try:
        yield
    finally:
        del holds[root]
        if lock is not _UNLOCKED:
            lock.release()


def locks_tree[F: Callable[..., Any]](*, outside: str, reader: bool = False) -> Callable[[F], F]:
    """Run a method under the lock of its ``project_root`` argument (L5)."""

    def decorate(method: F) -> F:
        signature = inspect.signature(method)

        @functools.wraps(method)
        def locked(*args: Any, **kwargs: Any) -> Any:
            root = signature.bind(*args, **kwargs).arguments["project_root"]
            with sync_dir_lock(root, outside=outside, reader=reader):
                return method(*args, **kwargs)

        return cast(F, locked)

    return decorate
