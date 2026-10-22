"""Updater contract tests for the 1.x updater and the crossing (CLI-51, #791 section 4.4).

* UP1 on a client of the new major: before M ``/releases/latest`` names the
  previous major, so the client reads the release list filtered by its own
  major. Pre-releases only with ``--beta``.
* The cache file of the running major (``version_cache_v<N>.json``).
* Breaking-change row 28: the first run after an automatic update to a higher
  major refuses one write or destructive command with ``UPDATED_ACROSS_MAJOR``
  (exit 6). A read gets a ``warnings[]`` entry. Not after ``update --major``.
* C5: the post-update banner lists the ``BREAKING:`` notes of every crossed
  version. ``--json`` adds ``UPDATED_ACROSS_BREAKING``.

GitHub is faked with pytest-httpx. Every registered answer is used once, so a
test also pins the requests of a run. The running version is patched, so the
same code is tested as 0.x, 1.x and 2.x.
"""

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from keboola_agent_cli import (
    auto_update,
    changelog,
    update_crossing,
    update_notice,
    update_runner,
)
from keboola_agent_cli.auto_update import _get_cache_path as real_get_cache_path
from keboola_agent_cli.auto_update import version_cache_filename
from keboola_agent_cli.cli import app
from keboola_agent_cli.permissions import OPERATION_REGISTRY, SERVE_ONLY_OPERATIONS
from keboola_agent_cli.services import version_service
from keboola_agent_cli.services.version_service import fetch_gated_release

MIGRATION_GUIDE_URL = "https://github.com/keboola/cli/blob/main/docs/migrate-to-1.0.md"
API = "https://api.github.com/repos/keboola/cli"
LATEST_URL = f"{API}/releases/latest"
LIST_URL = f"{API}/releases?per_page=100"
NEXT_TAG_URL = f"{API}/releases/tags/v2.0.0"
T0 = datetime(2026, 11, 2, 14, 5, tzinfo=UTC)
NOW = T0 + timedelta(days=5)  # after T0, before M: /releases/latest names 0.x
INSTALL = ("uv", "tool", "install", "--force", "--reinstall", "keboola-cli @ fake")


def _release(tag: str, *, prerelease: bool = False) -> dict:
    return {
        "id": 1,
        "tag_name": tag,
        "draft": False,
        "prerelease": prerelease,
        "published_at": T0.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


LATEST_0X = _release("v0.106.0")
ZERO_X = [LATEST_0X, _release("v0.105.2")]


def _running(monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    for module in (version_service, auto_update):
        monkeypatch.setattr(module, "__version__", version)


@pytest.fixture(autouse=True)
def _uv_tool_install(monkeypatch: pytest.MonkeyPatch) -> None:
    for module in (version_service, auto_update):
        monkeypatch.setattr(module, "detect_frozen_distribution", lambda: None)
    monkeypatch.setattr(version_service, "build_kbagent_upgrade_command", lambda **_: list(INSTALL))
    clock = SimpleNamespace(time=lambda: NOW.timestamp())
    monkeypatch.setattr(version_service, "time", clock)
    monkeypatch.setattr(auto_update, "time", clock)


def _github(httpx_mock, *, latest: dict | None = LATEST_0X, releases: list | None = None):
    """``/releases/latest``, ``v2.0.0`` (404: no next major) and the release list."""
    date = {"Date": format_datetime(NOW, usegmt=True)}
    if latest is not None:
        httpx_mock.add_response(url=LATEST_URL, json=latest, headers=date)
    httpx_mock.add_response(url=NEXT_TAG_URL, status_code=404, headers=date)
    if releases is not None:
        httpx_mock.add_response(url=LIST_URL, json=releases, headers=date)


def _urls(httpx_mock) -> list[str]:
    return [str(request.url) for request in httpx_mock.get_requests()]


class TestOwnMajorBeforeM:
    """UP1: a 1.x client finds 1.x fixes while ``/releases/latest`` names 0.x."""

    def test_startup_installs_a_1x_fix(self, httpx_mock, monkeypatch: pytest.MonkeyPatch) -> None:
        _running(monkeypatch, "1.0.0")
        _github(httpx_mock, releases=[_release("v1.0.1"), _release("v1.0.0"), *ZERO_X])
        perform = MagicMock(
            return_value=auto_update.UpdateResult(auto_update.UpdateOutcome.SUCCESS)
        )
        defer = MagicMock()
        monkeypatch.setattr(auto_update, "_perform_update", perform)
        monkeypatch.setattr(auto_update, "_re_exec", MagicMock())
        monkeypatch.setattr(auto_update, "request_deferred_update", defer)
        monkeypatch.setattr(auto_update, "should_defer", lambda: False)
        monkeypatch.setattr(auto_update, "_should_skip_all", lambda: False)
        monkeypatch.setattr(auto_update, "_should_skip_kbagent_stage", lambda: False)
        monkeypatch.setattr(auto_update, "_AUTO_UPDATE_RAN", False)

        auto_update.maybe_auto_update()

        # Same major: the inline install, not the deferred helper of a crossing.
        assert perform.call_args.args == ("1.0.1",)
        defer.assert_not_called()
        assert json.loads(auto_update._get_cache_path().read_text())["latest_version"] == "1.0.1"
        assert _urls(httpx_mock) == [LATEST_URL, NEXT_TAG_URL, LIST_URL]

    def test_the_list_is_filtered_by_the_running_major(self, httpx_mock, monkeypatch) -> None:
        _running(monkeypatch, "1.2.0")
        _github(httpx_mock, releases=[_release("v1.2.3"), _release("v1.2.0"), *ZERO_X])

        gated = fetch_gated_release()

        assert gated.latest_version == "1.2.3"
        assert gated.same_major_version == "1.2.3"

    def test_up_to_date_1x_installs_nothing(self, httpx_mock, monkeypatch) -> None:
        _running(monkeypatch, "1.0.1")
        _github(httpx_mock, releases=[_release("v1.0.1"), _release("v1.0.0"), *ZERO_X])

        plan = version_service.prepare_update_plan()

        assert plan.kbagent.up_to_date is True
        assert plan.kbagent.command is None

    def test_only_the_first_page_is_read(self, httpx_mock, monkeypatch) -> None:
        """The running major is the newest one, so an older page cannot hold it."""
        _running(monkeypatch, "1.0.0b1")
        full_page = [_release(f"v0.{n}.0") for n in range(200, 100, -1)]
        _github(httpx_mock, releases=full_page)

        gated = fetch_gated_release()

        assert gated.latest_version == "1.0.0b1"
        assert _urls(httpx_mock) == [LATEST_URL, NEXT_TAG_URL, LIST_URL]

    def test_0x_client_still_reads_only_releases_latest(self, httpx_mock, monkeypatch) -> None:
        _running(monkeypatch, "0.105.2")
        date = {"Date": format_datetime(NOW, usegmt=True)}
        httpx_mock.add_response(url=LATEST_URL, json=LATEST_0X, headers=date)
        httpx_mock.add_response(url=f"{API}/releases/tags/v1.0.0", status_code=404, headers=date)

        gated = fetch_gated_release()

        assert gated.latest_version == "0.106.0"
        assert LIST_URL not in _urls(httpx_mock)


PRE = (_release("v1.0.0rc1", prerelease=True), _release("v1.0.0b1", prerelease=True))


class TestPreReleases:
    """``1.0.0b1`` and ``1.0.0rc1`` are GitHub pre-releases before ``v1.0.0``."""

    def test_beta_finds_a_newer_1x_prerelease(self, httpx_mock, monkeypatch) -> None:
        _running(monkeypatch, "1.0.0b1")
        _github(httpx_mock, latest=None, releases=[*PRE, *ZERO_X])

        gated = fetch_gated_release(include_prerelease=True)

        assert gated.latest_version == "1.0.0rc1"

    def test_beta_finds_the_stable_1x_release(self, httpx_mock, monkeypatch) -> None:
        _running(monkeypatch, "1.0.0rc1")
        _github(httpx_mock, latest=None, releases=[_release("v1.0.0"), *PRE, *ZERO_X])

        gated = fetch_gated_release(include_prerelease=True)

        assert gated.latest_version == "1.0.0"

    def test_without_beta_never_a_prerelease(self, httpx_mock, monkeypatch) -> None:
        _running(monkeypatch, "1.0.0b1")
        _github(httpx_mock, releases=[*PRE, *ZERO_X])

        plan = version_service.prepare_update_plan()

        assert plan.kbagent.latest_version == "1.0.0b1"
        assert plan.kbagent.up_to_date is True
        assert plan.kbagent.command is None

    def test_older_stable_release_never_downgrades(self, httpx_mock, monkeypatch) -> None:
        _running(monkeypatch, "1.1.0b1")
        _github(httpx_mock, releases=[_release("v1.1.0b1", prerelease=True), _release("v1.0.0")])

        plan = version_service.prepare_update_plan()

        assert plan.kbagent.latest_version == "1.0.0"
        assert plan.kbagent.up_to_date is True
        assert plan.kbagent.command is None

    def test_without_beta_the_stable_1x_release(self, httpx_mock, monkeypatch) -> None:
        _running(monkeypatch, "1.0.0b1")
        stable = _release("v1.0.0")
        later_pre = _release("v1.0.1rc1", prerelease=True)
        _github(httpx_mock, releases=[later_pre, stable, *PRE, *ZERO_X])

        gated = fetch_gated_release()

        assert gated.latest_version == "1.0.0"


class TestCacheFile:
    """1.x keeps its version check in ``version_cache_v1.json``."""

    @pytest.mark.parametrize(
        ("version", "name"),
        [
            ("0.105.2", "version_cache.json"),
            ("1.0.0b1", "version_cache_v1.json"),
            ("1.4.2", "version_cache_v1.json"),
            ("2.0.0", "version_cache_v2.json"),
            ("0.0.0-dev", "version_cache.json"),
        ],
    )
    def test_file_name_per_major(self, version: str, name: str) -> None:
        assert version_cache_filename(version) == name

    def test_1x_never_writes_the_0x_cache(self, monkeypatch, tmp_path: Path) -> None:
        _running(monkeypatch, "1.0.0")
        monkeypatch.setattr(auto_update, "_get_cache_path", real_get_cache_path)
        monkeypatch.setattr(auto_update.platformdirs, "user_config_dir", lambda _: str(tmp_path))
        zero_x_cache = tmp_path / "version_cache.json"
        zero_x_cache.write_text(json.dumps({"last_check": 0.0, "latest_version": "0.106.0"}))

        auto_update._write_cache("1.0.1")

        assert json.loads(zero_x_cache.read_text())["latest_version"] == "0.106.0"
        written = json.loads((tmp_path / "version_cache_v1.json").read_text())
        assert written["latest_version"] == "1.0.1"


# --- Row 28 and C5: the first run after an update ----------------------------

WRITE = ["config", "delete", "--component-id", "c", "--config-id", "1"]
READ = ["project", "list"]


@pytest.fixture
def first_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Run one kbagent process: a fresh startup check and fresh module state.

    ``KBAGENT_AUTO_UPDATE=false`` stops the startup check after it has
    collected the previous update, so no test reads GitHub.
    """
    monkeypatch.setenv("KBAGENT_AUTO_UPDATE", "false")
    monkeypatch.setenv("KBAGENT_CONFIG_DIR", str(tmp_path / "cfg"))
    _running(monkeypatch, "1.0.0")

    def run(args: list[str]):
        update_notice.reset()
        monkeypatch.setattr(auto_update, "_AUTO_UPDATE_RAN", False)
        monkeypatch.setattr(sys, "argv", ["kbagent", *args])  # CliRunner leaves it alone
        return CliRunner().invoke(app, args)

    return run


def _deferred_update(
    tmp_path: Path, from_version: str, target: str = "1.0.0", **marker: object
) -> None:
    """The files of a finished deferred update (``state_dir`` is ``tmp_path``)."""
    (tmp_path / "pending_update.json").write_text(
        json.dumps(
            {
                "requested_at": NOW.timestamp(),
                "pid": 1,
                "from_version": from_version,
                "target_version": target,
                "recovery_command": None,
                **marker,
            }
        )
    )
    (tmp_path / "pending_update.exit").write_text("0")


def _codes(envelope: dict) -> list[str]:
    return [warning["code"] for warning in envelope.get("warnings", [])]


def _refused(result) -> bool:
    if result.exit_code != 6:
        return False
    return json.loads(result.stdout)["error"]["code"] == "UPDATED_ACROSS_MAJOR"


class TestCrossingRefusesOnce:
    """Each way a 1.x client learns about the crossing refuses one write, then proceeds."""

    def test_patched_updater_marker(self, first_run, tmp_path: Path) -> None:
        _deferred_update(tmp_path, "0.105.2", major_flag=False)

        first = first_run(["--json", *WRITE])
        second = first_run(["--json", *WRITE])

        assert _refused(first)
        error = json.loads(first.stdout)["error"]
        assert error["details"] == {
            "from_version": "0.105.2",
            "to_version": "1.0.0",
            "url": MIGRATION_GUIDE_URL,
        }
        assert error["retryable"] is True
        assert "Run the command again to proceed." in error["message"]
        assert not _refused(second)

    def test_updated_from_env_of_an_older_client(self, first_run, monkeypatch) -> None:
        """A 0.x client older than the gate installs 1.x and runs the command again."""
        monkeypatch.setenv("KBAGENT_UPDATED_FROM", "0.99.0")

        first = first_run(["--json", *WRITE])
        second = first_run(["--json", *WRITE])

        assert _refused(first)
        assert not _refused(second)

    def test_deferred_update_of_an_older_client(self, first_run, tmp_path: Path) -> None:
        """A marker without ``major_flag``: written by a client older than the gate."""
        _deferred_update(tmp_path, "0.98.0")

        first = first_run(["--json", *WRITE])
        second = first_run(["--json", *WRITE])

        assert _refused(first)
        assert not _refused(second)

    def test_update_major_does_not_refuse(self, first_run, tmp_path: Path) -> None:
        _deferred_update(tmp_path, "0.105.2", major_flag=True)

        result = first_run(["--json", *WRITE])

        assert not _refused(result)
        assert "UPDATED_ACROSS_MAJOR" not in result.stdout

    def test_reads_warn_until_the_first_write(self, first_run, tmp_path: Path) -> None:
        """Protection lasts until the first write, across processes, with no expiry."""
        _deferred_update(tmp_path, "0.105.2", major_flag=False)

        reads = [first_run(["--json", *READ]) for _ in range(2)]
        write = first_run(["--json", *WRITE])
        later_write = first_run(["--json", *WRITE])
        later_read = first_run(["--json", *READ])

        for read in reads:
            assert read.exit_code == 0
            warning = next(
                w
                for w in json.loads(read.stdout)["warnings"]
                if w["code"] == "UPDATED_ACROSS_MAJOR"
            )
            assert warning["url"] == MIGRATION_GUIDE_URL
            assert warning["from_version"] == "0.105.2"
        assert _refused(write)
        assert not _refused(later_write)
        assert "UPDATED_ACROSS_MAJOR" not in later_read.stdout
        assert not (tmp_path / "crossing_pending.json").exists()

    def test_admin_command_is_not_refused(self, first_run, monkeypatch) -> None:
        """``project add`` is admin-class: not refused, and the next write still is."""
        monkeypatch.setenv("KBAGENT_UPDATED_FROM", "0.105.2")

        admin = first_run(["--json", "project", "add"])
        write = first_run(["--json", *WRITE])

        assert not _refused(admin)
        assert _refused(write)

    def test_refused_write_carries_no_duplicate_warning(self, first_run, monkeypatch) -> None:
        monkeypatch.setenv("KBAGENT_UPDATED_FROM", "0.105.2")

        result = first_run(["--json", *WRITE])

        assert "UPDATED_ACROSS_MAJOR" not in _codes(json.loads(result.stdout))

    def test_same_major_update_does_not_refuse(self, first_run, monkeypatch) -> None:
        _running(monkeypatch, "1.0.1")
        monkeypatch.setenv("KBAGENT_UPDATED_FROM", "1.0.0")

        assert not _refused(first_run(["--json", *WRITE]))

    def test_generic_over_the_major(self, first_run, monkeypatch) -> None:
        _running(monkeypatch, "2.0.0")
        monkeypatch.setenv("KBAGENT_UPDATED_FROM", "1.9.3")

        assert _refused(first_run(["--json", *WRITE]))

    def test_human_mode(self, first_run, monkeypatch) -> None:
        """A read prints the warning line, the refused write prints only the error."""
        monkeypatch.setenv("KBAGENT_UPDATED_FROM", "0.105.2")
        warning = "That command is refused once, then every command runs."

        group_read = first_run(READ)
        top_level_read = first_run(["changelog", "-L", "1"])
        write = first_run(WRITE)

        assert warning in group_read.stderr
        assert warning in top_level_read.stderr
        assert write.exit_code == 6
        assert "did not run" in write.stderr
        assert warning not in write.stderr

    def test_policy_denial_keeps_the_refusal(self, first_run, monkeypatch, tmp_path) -> None:
        """A write that the policy denies does not use up the one refusal."""
        monkeypatch.setenv("KBAGENT_UPDATED_FROM", "0.105.2")

        denied = first_run(["--json", "--deny-writes", *WRITE])
        write = first_run(["--json", *WRITE])

        assert denied.exit_code == 6
        assert json.loads(denied.stdout)["error"]["code"] == "PERMISSION_DENIED"
        assert _refused(write)
        assert not (tmp_path / "crossing_pending.json").exists()

    def test_failed_deferred_update_does_not_count(self, first_run, tmp_path: Path) -> None:
        _deferred_update(tmp_path, "0.105.2")
        (tmp_path / "pending_update.exit").write_text("1")

        assert not _refused(first_run(["--json", *WRITE]))


class TestStateFile:
    """``crossing_pending.json``: one claim, fail open, nothing after ``--major``."""

    def test_update_major_writes_no_state(self, first_run, tmp_path: Path) -> None:
        _deferred_update(tmp_path, "0.105.2", major_flag=True)

        result = first_run(["--json", *READ])

        assert "UPDATED_ACROSS_MAJOR" not in result.stdout
        assert not (tmp_path / "crossing_pending.json").exists()

    @pytest.mark.parametrize(
        "content",
        [
            "{not json",
            "",
            '{"from_version": 1}',
            '{"from_version": "0.105.2", "to_version": "not-a-version"}',
        ],
    )
    def test_corrupt_state_refuses_nothing(self, first_run, tmp_path: Path, content) -> None:
        (tmp_path / "crossing_pending.json").write_text(content)

        read = first_run(["--json", *READ])
        writes = [first_run(["--json", *WRITE]) for _ in range(2)]

        assert "UPDATED_ACROSS_MAJOR" not in read.stdout
        assert not any(_refused(write) for write in writes)
        assert not (tmp_path / "crossing_pending.json").exists()

    def test_unwritable_state_dir_refuses_nothing(self, first_run, monkeypatch) -> None:
        def no_temp_file(*_: object, **__: object) -> None:
            raise PermissionError("read-only")

        monkeypatch.setattr(update_crossing.tempfile, "mkstemp", no_temp_file)
        monkeypatch.setenv("KBAGENT_UPDATED_FROM", "0.105.2")

        assert not _refused(first_run(["--json", *WRITE]))

    def test_unremovable_state_never_refuses(self, first_run, monkeypatch, tmp_path) -> None:
        """A claim that cannot move the file refuses nothing, on every run."""
        monkeypatch.setenv("KBAGENT_UPDATED_FROM", "0.105.2")
        first_run(["--json", *READ])
        assert (tmp_path / "crossing_pending.json").exists()

        def no_move(*_: object) -> None:
            raise PermissionError("read-only")

        monkeypatch.setattr(update_runner.os, "replace", no_move)
        results = [first_run(["--json", *WRITE]) for _ in range(3)]

        assert not any(_refused(result) for result in results)

    def test_unclaimable_marker_reports_but_records_nothing(
        self, first_run, monkeypatch, tmp_path
    ) -> None:
        """A marker that cannot be moved: the report shows, no refusal state is written."""
        _deferred_update(tmp_path, "0.105.2", major_flag=False)

        def no_move(*_: object) -> None:
            raise PermissionError("read-only")

        monkeypatch.setattr(update_runner.os, "replace", no_move)
        result = first_run(["--json", *WRITE])

        assert "Updated kbagent to v1.0.0" in result.stderr
        assert not _refused(result)
        assert not (tmp_path / "crossing_pending.json").exists()

    def test_concurrent_claims_refuse_at_most_once(self, tmp_path: Path) -> None:
        """Eight processes claim one state file at the same moment. At most one wins.

        On POSIX exactly one wins. On Windows a concurrent move or the read of
        the moved copy can fail with a sharing violation: then nobody is
        refused (fail open).
        """
        state = tmp_path / "crossing_pending.json"
        state.write_text(json.dumps({"from_version": "0.105.2", "to_version": "1.0.0"}))
        script = (
            "import sys, time\n"
            "from pathlib import Path\n"
            "from keboola_agent_cli import update_crossing, update_runner\n"
            "update_runner.state_dir = lambda: Path(sys.argv[1])\n"
            "while time.time() < float(sys.argv[2]):\n"
            "    pass\n"
            "print(update_crossing.refusal_for('config.delete') is not None)\n"
        )
        start = str(time.time() + 2)
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(tmp_path), start],
                stdout=subprocess.PIPE,
                text=True,
            )
            for _ in range(8)
        ]
        answers = [process.communicate(timeout=60)[0].strip() for process in processes]

        winners = answers.count("True")
        if sys.platform == "win32":
            assert winners <= 1, answers
        else:
            assert winners == 1, answers
            assert not state.exists()

    def test_old_claimed_copies_are_removed(self, first_run, tmp_path: Path) -> None:
        """A crash between claim and delete leaves a copy. Startup removes it after a day."""
        old_copy = tmp_path / "crossing_pending.json.123.claimed"
        old_marker = tmp_path / "pending_update.json.456.claimed"
        fresh_copy = tmp_path / "crossing_pending.json.789.claimed"
        for path in (old_copy, old_marker, fresh_copy):
            path.write_text("{}")
        two_days_ago = time.time() - 2 * 86_400
        for path in (old_copy, old_marker):
            os.utime(path, (two_days_ago, two_days_ago))

        first_run(["--json", *READ])

        assert not old_copy.exists()
        assert not old_marker.exists()
        assert fresh_copy.exists()

    def test_concurrent_marker_collection_records_once(self, tmp_path: Path) -> None:
        """Two processes collect one finished deferred update: one claims it."""
        _deferred_update(tmp_path, "0.105.2")

        first = update_runner.collect_finished_deferred_update()
        _deferred_update(tmp_path, "0.105.2")  # the other process still saw the files
        (tmp_path / "pending_update.json").unlink()  # ... but the first one moved it
        second = update_runner.collect_finished_deferred_update()

        assert first is not None and first.claimed
        assert second is None


def _argv(operation: str) -> list[str]:
    return operation.split(".")


@pytest.mark.parametrize(
    "operation",
    sorted(
        op
        for op, category in OPERATION_REGISTRY.items()
        if category in ("write", "destructive") and op not in SERVE_ONLY_OPERATIONS
    ),
)
def test_every_write_and_destructive_command_is_refused(first_run, monkeypatch, operation) -> None:
    """The refusal comes from the group callback, before the command parses its options."""
    monkeypatch.setenv("KBAGENT_UPDATED_FROM", "0.105.2")

    result = first_run(["--json", *_argv(operation)])

    assert _refused(result), (operation, result.exit_code, result.stdout)


class TestBreakingBanner:
    """C5: an update across several versions shows each ``BREAKING:`` note."""

    @pytest.fixture(autouse=True)
    def _changelog(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            changelog,
            "CHANGELOG",
            {
                "1.0.1": ["Fix: a fix. Detail."],
                "1.0.0": ["BREAKING: defaults changed. Detail.", "New: a command."],
                "0.106.0": ["BREAKING (#9): an option was removed. Detail.", "Fix: other."],
                "0.105.3": ["Fix: no breaking note here."],
                "0.105.2": ["BREAKING: already installed before the update."],
            },
        )

    def test_banner_lists_every_crossed_version(self) -> None:
        text = changelog.format_whats_new("0.105.2", "1.0.1")

        assert "What's new in v1.0.1:" in text
        assert "Breaking changes since v0.105.2:" in text
        assert "v1.0.0: BREAKING: defaults changed." in text
        assert "v0.106.0: BREAKING (#9): an option was removed." in text
        assert "already installed" not in text
        assert "Detail" not in text

    def test_one_version_update_shows_only_its_notes(self) -> None:
        text = changelog.format_whats_new("1.0.0", "1.0.1")

        assert text == "  What's new in v1.0.1:\n    - Fix: a fix.\n"

    def test_human_banner_after_the_crossing(self, first_run, monkeypatch) -> None:
        _running(monkeypatch, "1.0.1")
        monkeypatch.setenv("KBAGENT_UPDATED_FROM", "0.105.2")

        result = first_run(READ)

        assert "v0.106.0: BREAKING (#9): an option was removed." in result.stderr
        assert "v1.0.0: BREAKING: defaults changed." in result.stderr

    def test_json_first_command_adds_the_warning(self, first_run, monkeypatch) -> None:
        _running(monkeypatch, "1.0.1")
        monkeypatch.setenv("KBAGENT_UPDATED_FROM", "0.105.2")

        first = first_run(["--json", *READ])
        second = first_run(["--json", *READ])

        entry = next(
            w
            for w in json.loads(first.stdout)["warnings"]
            if w["code"] == "UPDATED_ACROSS_BREAKING"
        )
        assert entry["versions"] == ["1.0.0", "0.106.0"]
        assert entry["hint"] == "kbagent changelog --full"
        # The breaking-notes warning is for the first command only.
        assert _codes(json.loads(second.stdout)) == ["UPDATED_ACROSS_MAJOR"]

    def test_deferred_update_adds_the_warning(self, first_run, monkeypatch, tmp_path) -> None:
        _running(monkeypatch, "1.0.1")
        _deferred_update(tmp_path, "0.105.2", target="1.0.1", major_flag=True)

        result = first_run(["--json", *READ])

        assert _codes(json.loads(result.stdout)) == ["UPDATED_ACROSS_BREAKING"]

    def test_same_major_update_without_breaking_notes_adds_nothing(
        self, first_run, monkeypatch
    ) -> None:
        _running(monkeypatch, "1.0.1")
        monkeypatch.setenv("KBAGENT_UPDATED_FROM", "1.0.0")

        result = first_run(["--json", *READ])

        assert "warnings" not in json.loads(result.stdout)


class TestMigrationGuidePerMajor:
    """The notice and the crossing link the guide of the major they are about, not of 1.0."""

    GUIDE_2 = "https://github.com/keboola/cli/blob/main/docs/migrate-to-2.0.md"

    def test_notice_of_a_1x_install_links_the_2_0_guide(self) -> None:
        notice = update_notice.build_notice(
            current_version="1.4.2",
            next_version="2.0.0",
            published_ts=T0.timestamp(),
            migration_ts=(T0 + timedelta(days=28)).timestamp(),
            now=NOW.timestamp(),
            mode=update_notice.NoticeMode.AUTO_UPDATE,
        )

        assert notice.entry["url"] == self.GUIDE_2
        assert f"What changes: {self.GUIDE_2}." in notice.text

    def test_crossing_to_2x_links_the_2_0_guide(self) -> None:
        text = update_crossing.crossing_text({"from_version": "1.4.2", "to_version": "2.0.0"})

        assert f"Read what changed: {self.GUIDE_2}." in text
