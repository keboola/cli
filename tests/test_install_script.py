"""Tests for the version resolution and fallbacks of install.sh, offline.

install.sh takes the version from PyPI (``info.version``, the highest stable
version), not from the GitHub ``releases/latest`` redirect: during the migration
window of a new major, ``releases/latest`` still names the previous major on
purpose, and a new install must get the new major. The fallback chain is the
GitHub wheel of that version, then the same version from PyPI, then the source
build of that tag -- never the default branch.

The real script runs under ``sh`` with stub ``curl`` and ``uv`` first on PATH, so
nothing reaches the network and nothing is installed. The stubs log their
arguments; the tests assert on those logs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"
WHEEL_PREFIX = "https://github.com/keboola/cli/releases/download"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("sh") is None,
    reason="install.sh is a POSIX shell script",
)

# curl stub: answers the PyPI JSON from $FAKE_PYPI_JSON (missing file = failure),
# answers a wheel HEAD request per $FAKE_WHEEL_HEAD (ok|fail), fails anything else.
CURL_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_LOG_DIR/curl.log"
for arg in "$@"; do url="$arg"; done
case "$url" in
  https://pypi.org/pypi/keboola-cli/json)
    [ -f "$FAKE_PYPI_JSON" ] || exit 22
    cat "$FAKE_PYPI_JSON" ;;
  *.whl)
    [ "$FAKE_WHEEL_HEAD" = ok ] || exit 22 ;;
  *) exit 22 ;;
esac
"""

# uv stub: logs the install spec (the last argument); fails when it contains any of
# the |-separated substrings in $FAKE_UV_FAIL.
UV_STUB = """#!/bin/sh
for arg in "$@"; do spec="$arg"; done
printf '%s\\n' "$spec" >> "$STUB_LOG_DIR/uv.log"
old_ifs=$IFS; IFS='|'
for pattern in $FAKE_UV_FAIL; do
  case "$spec" in *"$pattern"*) exit 1 ;; esac
done
IFS=$old_ifs
exit 0
"""


def pypi_json(version: str, description: str = "") -> str:
    """A compact PyPI JSON document, key order as PyPI sends it (info first)."""
    return json.dumps(
        {
            "info": {
                "description": description,
                "name": "keboola-cli",
                "project_urls": {"Homepage": "https://github.com/keboola/cli"},
                "requires_python": ">=3.12",
                "version": version,
            },
            "last_serial": 1,
            "releases": {"0.99.1": [{"python_version": "py3"}], version: []},
            "urls": [{"python_version": "py3", "filename": f"keboola_cli-{version}.whl"}],
        },
        separators=(",", ":"),
    )


class Run:
    def __init__(self, proc: subprocess.CompletedProcess[str], log_dir: Path) -> None:
        self.proc = proc
        self.log_dir = log_dir

    def _lines(self, name: str) -> list[str]:
        path = self.log_dir / name
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    @property
    def uv_specs(self) -> list[str]:
        return self._lines("uv.log")

    @property
    def curl_calls(self) -> list[str]:
        return self._lines("curl.log")

    @property
    def output(self) -> str:
        return self.proc.stdout + self.proc.stderr


@pytest.fixture
def run_install(tmp_path: Path):
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name, body in (("curl", CURL_STUB), ("uv", UV_STUB)):
        stub = stubs / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(0o755)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    pypi_file = tmp_path / "pypi.json"

    def _run(pypi: str | None, *, wheel_head: str = "ok", uv_fail: str = "", **env: str) -> Run:
        if pypi is None:
            pypi_file.unlink(missing_ok=True)
        else:
            pypi_file.write_text(pypi, encoding="utf-8")
        proc = subprocess.run(
            ["sh", str(INSTALL_SH)],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            env={
                # Only the stubs and the system tools: the real uv/kbagent of the
                # machine running the tests are never on this PATH.
                "PATH": f"{stubs}:/usr/bin:/bin",
                "HOME": str(tmp_path / "home"),
                "NO_COLOR": "1",
                "STUB_LOG_DIR": str(log_dir),
                "FAKE_PYPI_JSON": str(pypi_file),
                "FAKE_WHEEL_HEAD": wheel_head,
                "FAKE_UV_FAIL": uv_fail,
                **env,
            },
        )
        return Run(proc, log_dir)

    return _run


def wheel(version: str) -> str:
    return f"{WHEEL_PREFIX}/v{version}/keboola_cli-{version}-py3-none-any.whl"


class TestVersionFromPyPI:
    def test_installs_the_github_wheel_of_the_pypi_version(self, run_install) -> None:
        run = run_install(pypi_json("1.0.0"))

        assert run.proc.returncode == 0, run.output
        assert run.uv_specs == [f"keboola-cli[server] @ {wheel('1.0.0')}"]

    def test_never_reads_the_releases_latest_redirect(self, run_install) -> None:
        run = run_install(pypi_json("1.0.0"))

        assert run.curl_calls, run.output
        assert not any("releases/latest" in call for call in run.curl_calls)

    def test_a_quoted_version_key_in_the_description_does_not_match(self, run_install) -> None:
        """The README in `description` is a JSON string, so its quotes are escaped."""
        description = 'Config example: {"version": "9.9.9"}'
        run = run_install(pypi_json("0.105.2", description=description))

        assert run.uv_specs == [f"keboola-cli[server] @ {wheel('0.105.2')}"]

    def test_post_release_version_is_accepted(self, run_install) -> None:
        run = run_install(pypi_json("0.105.2.post1"))

        assert run.uv_specs == [f"keboola-cli[server] @ {wheel('0.105.2.post1')}"]

    def test_cli_only_install_drops_the_server_extras(self, run_install) -> None:
        run = run_install(pypi_json("1.0.0"), KBAGENT_NO_SERVER="1")

        assert run.uv_specs == [f"keboola-cli @ {wheel('1.0.0')}"]


class TestFallbacks:
    def test_missing_wheel_installs_the_same_version_from_pypi(self, run_install) -> None:
        run = run_install(pypi_json("1.0.0"), wheel_head="fail")

        assert run.proc.returncode == 0, run.output
        assert run.uv_specs == ["keboola-cli[server]==1.0.0"]

    def test_failed_wheel_install_installs_the_same_version_from_pypi(self, run_install) -> None:
        run = run_install(pypi_json("1.0.0"), uv_fail=".whl")

        assert run.uv_specs == [
            f"keboola-cli[server] @ {wheel('1.0.0')}",
            "keboola-cli[server]==1.0.0",
        ]

    def test_source_build_is_pinned_to_the_resolved_tag(self, run_install) -> None:
        run = run_install(pypi_json("1.0.0"), uv_fail=".whl|==1.0.0")

        assert run.proc.returncode == 0, run.output
        assert run.uv_specs[-1] == "keboola-cli[server] @ git+https://github.com/keboola/cli@v1.0.0"

    def test_unreachable_pypi_installs_the_newest_stable_from_pypi(self, run_install) -> None:
        run = run_install(None)

        assert run.proc.returncode == 0, run.output
        assert run.uv_specs == ["keboola-cli[server]"]

    def test_unresolved_version_never_builds_the_default_branch(self, run_install) -> None:
        run = run_install(None, uv_fail="keboola-cli")

        assert run.proc.returncode == 1
        assert run.uv_specs == ["keboola-cli[server]"]
        assert "Install failed" in run.output

    @pytest.mark.parametrize("bad", ["1.0.0; rm -rf ~", "latest", "1.0.0b1", ""])
    def test_a_version_that_is_not_plain_x_y_z_is_rejected(self, run_install, bad: str) -> None:
        run = run_install(pypi_json(bad))

        assert run.uv_specs == ["keboola-cli[server]"]
        assert not any(".whl" in call for call in run.curl_calls)


def test_install_script_is_valid_posix_sh() -> None:
    proc = subprocess.run(
        ["sh", "-n", str(INSTALL_SH)], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert os.access(INSTALL_SH, os.R_OK)
