"""Hatchling custom build hook that bundles the built React SPA into the wheel.

End-users install this package via:

- ``uv tool install git+https://github.com/keboola/cli`` --
  uv clones the repo, runs ``hatchling`` to produce a wheel, installs it,
  then deletes the clone. The user does NOT have a checkout on disk.
- ``pip install keboola-agent-cli`` (PyPI) -- prebuilt wheel.

For ``kbagent serve --ui`` to work after either install path, the wheel
must already carry the SPA. This hook arranges that by:

1. **Discovering** an existing build at ``web/frontend/dist`` (e.g. the
   maintainer ran ``make web-build`` before ``uv build``).
2. **Building it on the fly** if missing AND ``npm`` is available --
   covers the ``uv tool install git+...`` happy path on machines that
   already have Node 20+ for other reasons.
3. **Copying** the dist into ``src/keboola_agent_cli/_ui_dist/`` so
   hatchling's normal package collection picks it up (the dir is in
   ``.gitignore`` to avoid checking in generated assets).

If neither a prebuilt dist nor ``npm`` is available, the hook logs a
warning and lets the wheel build proceed without the UI. The CLI will
still work; only ``kbagent serve --ui`` will fail with a "no UI bundled"
error pointing the user at install instructions.

**Cross-platform note (issue #320).** Two Windows-specific traps are
handled here:

- ``npm`` on Windows is a batch launcher (``npm.cmd``). A bare
  ``subprocess.check_call(["npm", ...])`` cannot find it and raises
  ``FileNotFoundError`` (a subclass of ``OSError``, *not*
  ``CalledProcessError``). We pass the full path returned by
  ``shutil.which("npm")`` -- which is ``...\\npm.cmd`` on Windows, and
  ``CreateProcess`` happily runs a ``.cmd`` via the system shell even with
  ``shell=False`` -- and we widen the ``except`` to ``OSError`` so a failed
  invocation degrades to a UI-less wheel instead of killing the build.
- hatchling's ``force-include`` (see pyproject.toml) fails the whole build
  if its source path is missing. Every code path here therefore guarantees
  ``_ui_dist/`` exists on return (empty is fine -- hatchling includes zero
  files from it and the runtime UI detector keys on ``index.html``).

Set ``KBAGENT_SKIP_UI_BUILD=1`` to skip the on-the-fly npm build and ship a
CLI-only wheel deliberately (fast builds; CI exercising the no-UI path).

Wired in via ``[tool.hatch.build.targets.wheel.hooks.custom]`` in pyproject.toml
(``path = "scripts/hatch_build.py"``). hatchling resolves the hook by that
explicit path, so this file does not need to sit at the repo root.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hatchling.builders.hooks.plugin.interface import BuildHookInterface
else:
    # hatchling is only present in the build environment (uv/pip provision it
    # from ``[build-system].requires``). Fall back to ``object`` at runtime so
    # the pure helpers below (``_bundle_ui`` / ``_ensure_target``) stay
    # importable for unit tests in a plain dev venv that has no hatchling.
    try:
        from hatchling.builders.hooks.plugin.interface import BuildHookInterface
    except ModuleNotFoundError:  # pragma: no cover - exercised only without hatchling
        BuildHookInterface = object

# Set to "1" to ship a CLI-only wheel: skip bundling the SPA entirely, even if
# a prebuilt ``web/frontend/dist`` exists. The wheel still builds -- an empty
# ``_ui_dist/`` placeholder is created so hatchling's force-include resolves,
# and ``kbagent serve --ui`` surfaces the friendly "no UI bundled" error.
# Useful for fast CLI-only builds and for exercising the no-UI path in CI
# without uninstalling Node.
SKIP_UI_BUILD_ENV = "KBAGENT_SKIP_UI_BUILD"


def _ensure_target(target: Path) -> None:
    """Guarantee the force-include source dir exists so the wheel build works.

    hatchling's ``force-include`` fails the *entire* wheel build if its source
    path is missing (issue #320, Bug 2). An empty directory satisfies it --
    hatchling includes zero files from it, and the runtime UI detector keys on
    ``index.html`` (absent here), so ``kbagent serve --ui`` degrades to a
    friendly "no UI bundled" error rather than a build-time crash.
    """
    target.mkdir(parents=True, exist_ok=True)


def _bundle_ui(repo_root: Path, log: Callable[[str], None] = print) -> None:
    """Populate ``src/keboola_agent_cli/_ui_dist/`` for wheel inclusion.

    Extracted from :class:`CustomBuildHook` so it can be unit-tested without a
    full hatchling build context. ``log`` is injected for the same reason.

    Postcondition: ``_ui_dist/`` always exists on return (see
    :func:`_ensure_target`).
    """
    dist = repo_root / "web" / "frontend" / "dist"
    target = repo_root / "src" / "keboola_agent_cli" / "_ui_dist"
    frontend_dir = repo_root / "web" / "frontend"

    # Always start from a clean slate so stale assets from a previous build
    # don't leak into the new wheel. We rebuild target each time.
    if target.exists():
        shutil.rmtree(target)

    # 1) Explicit opt-out: ship a CLI-only wheel. Checked first so it wins even
    #    over a prebuilt dist -- "skip UI build" means "no UI in this wheel".
    if os.environ.get(SKIP_UI_BUILD_ENV) == "1":
        log(f"{SKIP_UI_BUILD_ENV}=1 set; skipping SPA bundle (CLI-only wheel).")
        _ensure_target(target)
        return

    # 2) Prebuilt dist on disk -- the maintainer ran ``make web-build`` first.
    if (dist / "index.html").exists():
        log(f"copying {dist} -> {target}")
        shutil.copytree(dist, target)
        return

    # 3) No prebuilt dist. Build it iff the source tree exists AND npm is on
    #    PATH. ``shutil.which`` returns the resolved path -- on Windows that is
    #    ``...\\npm.cmd``; passing the full path lets CreateProcess run the
    #    batch launcher even with shell=False (a bare "npm" raises WinError 2).
    npm = shutil.which("npm")
    if not (frontend_dir.exists() and npm):
        why = "no `npm` on PATH" if frontend_dir.exists() else "no web/frontend/ dir"
        log(
            f"WARNING: no prebuilt SPA and {why}; wheel will not bundle the UI. "
            "`kbagent serve --ui` will fail until the user rebuilds the SPA manually."
        )
        _ensure_target(target)
        return

    log("no prebuilt dist found; running npm build")
    try:
        subprocess.check_call(
            [npm, "ci", "--prefer-offline", "--no-audit", "--no-fund"],
            cwd=frontend_dir,
        )
        subprocess.check_call([npm, "run", "build"], cwd=frontend_dir)
    except (subprocess.CalledProcessError, OSError) as exc:
        # OSError covers FileNotFoundError/PermissionError from the spawn
        # itself (the Windows ``npm.cmd`` trap); CalledProcessError covers a
        # non-zero npm exit. Either way: degrade to a UI-less wheel.
        log(
            f"WARNING: npm build failed ({exc}); wheel will not bundle the UI. "
            "`kbagent serve --ui` will fail until the user rebuilds the SPA manually."
        )
        _ensure_target(target)
        return

    if not (dist / "index.html").exists():
        log("WARNING: build did not produce dist/index.html; skipping UI bundle.")
        _ensure_target(target)
        return

    log(f"copying {dist} -> {target}")
    shutil.copytree(dist, target)


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "build-ui"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        _bundle_ui(Path(self.root).resolve(), self._log)

    def _log(self, message: str) -> None:
        # Hatchling's BuilderInterface exposes ``app`` for nicely-formatted
        # output but it's not always present on the hook context, so we
        # fall back to a plain print prefixed with our hook id so the user
        # can grep for it in noisy build logs.
        print(f"[hatch:build-ui] {message}")
