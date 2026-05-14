"""Hatchling custom build hook that bundles the built React SPA into the wheel.

End-users install this package via:

- ``uv tool install git+https://github.com/padak/keboola_agent_cli`` --
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

Wired in via ``[tool.hatch.build.hooks.custom]`` in pyproject.toml.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "build-ui"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        repo_root = Path(self.root).resolve()
        dist = repo_root / "web" / "frontend" / "dist"
        target = repo_root / "src" / "keboola_agent_cli" / "_ui_dist"
        frontend_dir = repo_root / "web" / "frontend"

        # Always start from a clean slate so stale assets from a previous
        # build don't leak into the new wheel. We rebuild target each time.
        if target.exists():
            shutil.rmtree(target)

        if not (dist / "index.html").exists():
            # No prebuilt dist. Try to build it iff npm is present AND the
            # source tree exists (true for `uv tool install git+...` and
            # local development; false if someone uploaded an sdist that
            # excluded `web/`).
            if frontend_dir.exists() and shutil.which("npm"):
                self._log("no prebuilt dist found; running npm build")
                try:
                    subprocess.check_call(
                        ["npm", "ci", "--prefer-offline", "--no-audit", "--no-fund"],
                        cwd=frontend_dir,
                    )
                    subprocess.check_call(["npm", "run", "build"], cwd=frontend_dir)
                except subprocess.CalledProcessError as exc:
                    self._log(
                        f"WARNING: npm build failed ({exc}); wheel will not bundle "
                        "the UI. `kbagent serve --ui` will fail until the user "
                        "rebuilds the SPA manually.",
                    )
                    return
            else:
                why = "no `npm` on PATH" if frontend_dir.exists() else "no web/frontend/ dir"
                self._log(
                    f"WARNING: no prebuilt SPA and {why}; wheel will not bundle "
                    "the UI. `kbagent serve --ui` will fail until the user "
                    "rebuilds the SPA manually.",
                )
                return

        if not (dist / "index.html").exists():
            self._log("WARNING: build did not produce dist/index.html; skipping UI bundle.")
            return

        self._log(f"copying {dist} -> {target}")
        shutil.copytree(dist, target)

    def _log(self, message: str) -> None:
        # Hatchling's BuilderInterface exposes ``app`` for nicely-formatted
        # output but it's not always present on the hook context, so we
        # fall back to a plain print prefixed with our hook id so the user
        # can grep for it in noisy build logs.
        print(f"[hatch:build-ui] {message}")
