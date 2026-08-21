"""Unit tests for the wheel build hook (``hatch_build.py``).

Regression coverage for issue #320 ("Windows installation completely broken").
The two bugs are reproduced *without a Windows machine*:

- **Bug 1** -- a bare ``subprocess.check_call(["npm", ...])`` raises
  ``FileNotFoundError`` on Windows because ``npm`` is ``npm.cmd`` (a batch
  launcher ``CreateProcess`` can't resolve from the bare name). We assert the
  hook (a) passes the *resolved* ``shutil.which`` path so the ``.cmd`` runs,
  and (b) catches ``OSError`` so a spawn failure degrades to a UI-less wheel
  instead of killing the build. Both are simulated with mocks, so they run on
  any OS.
- **Bug 2** -- an early ``return`` left ``_ui_dist/`` missing, and hatchling's
  ``force-include`` then failed the whole build. We assert every code path
  leaves ``_ui_dist/`` existing on disk.

Also covers a gap where the CI wheel build always runs inside a real `git`
checkout, so it never exercised the no-``.git`` case (a VCS-url install that
hands hatchling a plain exported source tree). Without a ``.git`` dir,
hatchling's gitignore-based default-exclusion can't run, so the (gitignored)
``_ui_dist/`` the hook just populated got picked up by BOTH the default
package globbing and ``force-include``, and the wheel build aborted with "A
second file is being added to the wheel archive at the same path". See
``TestForceIncludeNoDuplicate`` below for the end-to-end regression test.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from unittest import mock

import pytest

# ``scripts/hatch_build.py`` is not under ``src/``, so it is not importable as a
# normal package. Load it by file path and register it in ``sys.modules`` so
# ``mock.patch("hatch_build.<attr>")`` resolves to *this* instance. The hatchling
# import inside is guarded, so this works in a plain dev venv that has no
# hatchling installed.
_HOOK_PATH = Path(__file__).resolve().parents[1] / "scripts" / "hatch_build.py"
_spec = importlib.util.spec_from_file_location("hatch_build", _HOOK_PATH)
assert _spec is not None and _spec.loader is not None
hatch_build = importlib.util.module_from_spec(_spec)
sys.modules["hatch_build"] = hatch_build
_spec.loader.exec_module(hatch_build)

# The CI wheel-content assertion helper lives in ``scripts/`` -- load it the
# same way so its logic is regression-tested in normal (ubuntu) CI, not only by
# the Windows wheel-build job that calls it as a subprocess.
_HELPER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_wheel_ui.py"
_helper_spec = importlib.util.spec_from_file_location("check_wheel_ui", _HELPER_PATH)
assert _helper_spec is not None and _helper_spec.loader is not None
check_wheel_ui = importlib.util.module_from_spec(_helper_spec)
sys.modules["check_wheel_ui"] = check_wheel_ui
_helper_spec.loader.exec_module(check_wheel_ui)


def _make_repo(root: Path, *, with_frontend: bool = True, with_dist: bool = False) -> Path:
    """Lay out a minimal fake repo tree and return ``root``."""
    (root / "src" / "keboola_agent_cli").mkdir(parents=True)
    if with_frontend:
        (root / "web" / "frontend").mkdir(parents=True)
    if with_dist:
        dist = root / "web" / "frontend" / "dist"
        dist.mkdir(parents=True, exist_ok=True)
        (dist / "index.html").write_text("<html>app</html>", encoding="utf-8")
        (dist / "assets").mkdir()
        (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    return root


def _target(root: Path) -> Path:
    return root / "src" / "keboola_agent_cli" / "_ui_dist"


class TestBundleUiHappyPath:
    def test_prebuilt_dist_is_copied(self, tmp_path: Path) -> None:
        root = _make_repo(tmp_path, with_dist=True)

        hatch_build._bundle_ui(root, log=lambda _msg: None)

        target = _target(root)
        assert (target / "index.html").read_text(encoding="utf-8") == "<html>app</html>"
        assert (target / "assets" / "app.js").exists()

    def test_stale_target_is_cleared_before_copy(self, tmp_path: Path) -> None:
        root = _make_repo(tmp_path, with_dist=True)
        target = _target(root)
        target.mkdir(parents=True)
        (target / "STALE.txt").write_text("from a previous build", encoding="utf-8")

        hatch_build._bundle_ui(root, log=lambda _msg: None)

        assert not (target / "STALE.txt").exists()
        assert (target / "index.html").exists()


class TestBundleUiBug2TargetAlwaysExists:
    """Every early-return path must leave ``_ui_dist/`` existing (issue #320 Bug 2)."""

    def test_no_npm_creates_empty_target(self, tmp_path: Path) -> None:
        root = _make_repo(tmp_path)  # frontend present, no dist
        with mock.patch.object(hatch_build.shutil, "which", return_value=None):
            hatch_build._bundle_ui(root, log=lambda _msg: None)

        target = _target(root)
        assert target.is_dir()
        assert not (target / "index.html").exists()

    def test_no_frontend_dir_creates_empty_target(self, tmp_path: Path) -> None:
        root = _make_repo(tmp_path, with_frontend=False)
        with mock.patch.object(hatch_build.shutil, "which", return_value=None):
            hatch_build._bundle_ui(root, log=lambda _msg: None)

        assert _target(root).is_dir()

    def test_skip_env_var_creates_empty_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _make_repo(tmp_path)
        monkeypatch.setenv(hatch_build.SKIP_UI_BUILD_ENV, "1")
        # npm IS available -- the env var must short-circuit before invoking it.
        with (
            mock.patch.object(hatch_build.shutil, "which", return_value="/usr/bin/npm"),
            mock.patch.object(hatch_build.subprocess, "check_call") as check_call,
        ):
            hatch_build._bundle_ui(root, log=lambda _msg: None)

        check_call.assert_not_called()
        assert _target(root).is_dir()
        assert not (_target(root) / "index.html").exists()

    def test_skip_env_var_wins_over_prebuilt_dist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # "skip UI build" means a CLI-only wheel even when a dist is on disk.
        root = _make_repo(tmp_path, with_dist=True)
        monkeypatch.setenv(hatch_build.SKIP_UI_BUILD_ENV, "1")

        hatch_build._bundle_ui(root, log=lambda _msg: None)

        assert _target(root).is_dir()
        assert not (_target(root) / "index.html").exists()

    def test_build_without_index_html_creates_empty_target(self, tmp_path: Path) -> None:
        # npm "succeeds" but does not produce dist/index.html -> still no crash.
        root = _make_repo(tmp_path)
        with (
            mock.patch.object(hatch_build.shutil, "which", return_value="/usr/bin/npm"),
            mock.patch.object(hatch_build.subprocess, "check_call"),
        ):
            hatch_build._bundle_ui(root, log=lambda _msg: None)

        assert _target(root).is_dir()
        assert not (_target(root) / "index.html").exists()


class TestBundleUiBug1NpmInvocation:
    """The Windows ``npm.cmd`` traps (issue #320 Bug 1)."""

    def test_filenotfounderror_is_caught_not_propagated(self, tmp_path: Path) -> None:
        """A bare ``["npm", ...]`` on Windows raises FileNotFoundError -- must degrade."""
        root = _make_repo(tmp_path)
        with (
            mock.patch.object(
                hatch_build.shutil, "which", return_value="C:\\Program Files\\nodejs\\npm.cmd"
            ),
            mock.patch.object(
                hatch_build.subprocess,
                "check_call",
                side_effect=FileNotFoundError(2, "The system cannot find the file specified"),
            ),
        ):
            # Must NOT raise -- the old code only caught CalledProcessError.
            hatch_build._bundle_ui(root, log=lambda _msg: None)

        assert _target(root).is_dir()

    def test_calledprocesserror_is_caught(self, tmp_path: Path) -> None:
        root = _make_repo(tmp_path)
        with (
            mock.patch.object(hatch_build.shutil, "which", return_value="/usr/bin/npm"),
            mock.patch.object(
                hatch_build.subprocess,
                "check_call",
                side_effect=subprocess.CalledProcessError(1, ["npm", "ci"]),
            ),
        ):
            hatch_build._bundle_ui(root, log=lambda _msg: None)

        assert _target(root).is_dir()

    def test_passes_resolved_npm_path_not_bare_name(self, tmp_path: Path) -> None:
        """Two regressions guarded here: (1) the resolved ``shutil.which`` path
        (``npm.cmd`` on Windows) is handed to subprocess, never the bare string
        ``"npm"``; (2) ``npm ci`` carries ``--ignore-scripts`` (GHSA-pfvh) while
        ``npm run build`` intentionally does NOT."""
        root = _make_repo(tmp_path)
        resolved = "C:\\Program Files\\nodejs\\npm.cmd"
        with (
            mock.patch.object(hatch_build.shutil, "which", return_value=resolved),
            mock.patch.object(hatch_build.subprocess, "check_call") as check_call,
        ):
            hatch_build._bundle_ui(root, log=lambda _msg: None)

        assert check_call.call_count == 2
        ci_cmd = check_call.call_args_list[0].args[0]
        build_cmd = check_call.call_args_list[1].args[0]
        assert ci_cmd[0] == resolved
        assert build_cmd[0] == resolved
        assert ci_cmd[1] == "ci"
        # GHSA-pfvh: dependency lifecycle scripts must NOT run at wheel-build /
        # `git+` install time (install-time RCE surface). Verified non-breaking
        # against the real `tsc -b && vite build`.
        assert "--ignore-scripts" in ci_cmd
        # Intentional asymmetry: `--ignore-scripts` gates the dependency INSTALL
        # (`npm ci`). `npm run build` runs our OWN build script and must NOT
        # carry the flag -- do not "fix" the asymmetry by adding it there.
        assert build_cmd[1:] == ["run", "build"]
        assert "--ignore-scripts" not in build_cmd

    def test_successful_npm_build_is_bundled(self, tmp_path: Path) -> None:
        """When npm produces dist/index.html, it gets copied into _ui_dist/."""
        root = _make_repo(tmp_path)
        dist = root / "web" / "frontend" / "dist"

        def fake_check_call(cmd: list[str], **_kwargs: object) -> int:
            # Simulate ``npm run build`` emitting the SPA on the second call.
            if cmd[1:] == ["run", "build"]:
                dist.mkdir(parents=True, exist_ok=True)
                (dist / "index.html").write_text("<html>built</html>", encoding="utf-8")
            return 0

        with (
            mock.patch.object(hatch_build.shutil, "which", return_value="/usr/bin/npm"),
            mock.patch.object(hatch_build.subprocess, "check_call", side_effect=fake_check_call),
        ):
            hatch_build._bundle_ui(root, log=lambda _msg: None)

        assert (_target(root) / "index.html").read_text(encoding="utf-8") == "<html>built</html>"


class TestEnsureTarget:
    def test_creates_missing_dir(self, tmp_path: Path) -> None:
        target = tmp_path / "deep" / "_ui_dist"
        hatch_build._ensure_target(target)
        assert target.is_dir()

    def test_idempotent_on_existing_dir(self, tmp_path: Path) -> None:
        target = tmp_path / "_ui_dist"
        target.mkdir()
        hatch_build._ensure_target(target)  # must not raise
        assert target.is_dir()


def _fake_wheel(dist_dir: Path, *, with_ui: bool, name: str = "pkg-0.0.1-py3-none-any.whl") -> Path:
    """Write a minimal wheel (zip) with or without the bundled SPA marker."""
    dist_dir.mkdir(parents=True, exist_ok=True)
    wheel = dist_dir / name
    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr("keboola_agent_cli/__init__.py", "")
        if with_ui:
            zf.writestr(check_wheel_ui.UI_MARKER, "<html>app</html>")
    return wheel


class TestCheckWheelUiHelper:
    """The ``scripts/check_wheel_ui.py`` CI assertion helper (issue #320)."""

    def test_wheel_bundles_ui_true(self, tmp_path: Path) -> None:
        wheel = _fake_wheel(tmp_path, with_ui=True)
        assert check_wheel_ui.wheel_bundles_ui(str(wheel)) is True

    def test_wheel_bundles_ui_false(self, tmp_path: Path) -> None:
        wheel = _fake_wheel(tmp_path, with_ui=False)
        assert check_wheel_ui.wheel_bundles_ui(str(wheel)) is False

    def test_expect_ui_passes_on_ui_wheel(self, tmp_path: Path) -> None:
        _fake_wheel(tmp_path, with_ui=True)
        assert check_wheel_ui.main(["--expect-ui", "--dist", str(tmp_path)]) == 0

    def test_expect_ui_fails_on_cli_only_wheel(self, tmp_path: Path) -> None:
        _fake_wheel(tmp_path, with_ui=False)
        assert check_wheel_ui.main(["--expect-ui", "--dist", str(tmp_path)]) == 1

    def test_no_ui_passes_on_cli_only_wheel(self, tmp_path: Path) -> None:
        _fake_wheel(tmp_path, with_ui=False)
        assert check_wheel_ui.main(["--no-ui", "--dist", str(tmp_path)]) == 0

    def test_no_ui_fails_on_ui_wheel(self, tmp_path: Path) -> None:
        _fake_wheel(tmp_path, with_ui=True)
        assert check_wheel_ui.main(["--no-ui", "--dist", str(tmp_path)]) == 1

    def test_missing_wheel_is_an_error(self, tmp_path: Path) -> None:
        assert check_wheel_ui.main(["--expect-ui", "--dist", str(tmp_path)]) == 1


class TestForceIncludeNoDuplicate:
    """Building without a ``.git`` dir must not duplicate ``_ui_dist/``.

    Reproduces the real failure end-to-end (not mocked): a minimal project
    laid out with the actual ``pyproject.toml`` / ``hatch_build.py``, a
    prebuilt SPA dist on disk (so the hook populates ``_ui_dist/`` with a real
    file), and deliberately NO ``.git`` directory -- the exact shape of a
    VCS-url install where the build backend never sees repo history.
    """

    def test_wheel_builds_without_git_directory(self, tmp_path: Path) -> None:
        if shutil.which("uv") is None:
            pytest.skip("uv not on PATH")

        repo_root = Path(__file__).resolve().parents[1]
        project = tmp_path / "project"
        (project / "src" / "keboola_agent_cli").mkdir(parents=True)
        (project / "src" / "keboola_agent_cli" / "__init__.py").write_text("", encoding="utf-8")
        (project / "src" / "keboola_agent_cli" / "py.typed").write_text("", encoding="utf-8")
        (project / "scripts").mkdir()
        shutil.copy(
            repo_root / "scripts" / "hatch_build.py", project / "scripts" / "hatch_build.py"
        )
        shutil.copy(repo_root / "pyproject.toml", project / "pyproject.toml")
        (project / "README.md").write_text("test project", encoding="utf-8")

        dist = project / "web" / "frontend" / "dist"
        dist.mkdir(parents=True)
        (dist / "index.html").write_text("<html>app</html>", encoding="utf-8")

        assert not (project / ".git").exists()

        result = subprocess.run(
            ["uv", "build", "--wheel", "-o", str(tmp_path / "out")],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr

        (wheel,) = list((tmp_path / "out").glob("*.whl"))
        with zipfile.ZipFile(wheel) as zf:
            ui_entries = [n for n in zf.namelist() if n.endswith("_ui_dist/index.html")]
        assert ui_entries == ["keboola_agent_cli/_ui_dist/index.html"]
