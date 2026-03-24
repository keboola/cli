"""Git helper functions for sync operations.

All functions are safe to call on machines without git installed -- they
return ``None`` or sensible defaults when the ``git`` binary is missing
or the directory is not a repository.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def is_git_repo(path: Path) -> bool:
    """Return ``True`` if *path* is inside a git working tree."""
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=path,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def get_current_branch(path: Path) -> str | None:
    """Return the current git branch name, or ``None`` on failure.

    Uses ``git rev-parse --abbrev-ref HEAD`` which returns the
    symbolic branch name (e.g. ``main``, ``feature/foo``).
    """
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=path,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    # Detached HEAD returns "HEAD"
    return branch if branch and branch != "HEAD" else None


def get_default_branch(path: Path) -> str:
    """Detect the default branch name (``main`` or ``master``).

    Strategy:
    1. Check ``git config init.defaultBranch`` (user/repo setting).
    2. Check if ``refs/remotes/origin/main`` exists.
    3. Check if ``refs/remotes/origin/master`` exists.
    4. Fall back to ``"main"``.
    """
    # 1. git config
    result = subprocess.run(
        ["git", "config", "init.defaultBranch"],
        cwd=path,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()

    # 2. origin/main
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "refs/remotes/origin/main"],
        cwd=path,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return "main"

    # 3. origin/master
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "refs/remotes/origin/master"],
        cwd=path,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return "master"

    # 4. Default
    return "main"
