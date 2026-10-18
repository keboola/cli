"""Updater contract tests for the major gate (CLI-40, #791 section 4.3, UP1, UP2, C9).

GitHub is faked with pytest-httpx: ``/releases/latest``, the release-by-tag
answer, the release list and the ``Date`` header. Every registered answer is
used once, so a test also pins how many requests a run makes. The local clock
is a fake too. No test installs anything: the install and the deferred helper
are mocks, and the tests assert which of them was reached.

Names: T0 is the ``published_at`` of ``v1.0.0``; M is T0 + 28 days.
"""

import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from typer.testing import CliRunner

from keboola_agent_cli import auto_update, update_notice
from keboola_agent_cli.cli import app
from keboola_agent_cli.constants import MIGRATION_GUIDE_URL
from keboola_agent_cli.output import OutputFormatter, success_json
from keboola_agent_cli.services import version_service
from keboola_agent_cli.services.version_service import VersionService, fetch_gated_release

RUNNING = "0.105.2"
API = "https://api.github.com/repos/keboola/cli"
LATEST_URL = f"{API}/releases/latest"
LIST_URL = f"{API}/releases?per_page=100"
TAG_URL = f"{API}/releases/tags/v1.0.0"
T0 = datetime(2026, 11, 2, 14, 5, tzinfo=UTC)
M = T0 + timedelta(days=28)
INSTALL = ("uv", "tool", "install", "--force", "--reinstall", "keboola-cli @ fake")


def _release(tag: str, *, release_id: int = 1, prerelease: bool = False, published: datetime = T0):
    return {
        "id": release_id,
        "tag_name": tag,
        "draft": False,
        "prerelease": prerelease,
        "published_at": published.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


V0 = _release("v0.106.0")
V1 = _release("v1.0.1", release_id=102)
V1_TAG = _release("v1.0.0", release_id=101)
ZERO_X = [V0, _release("v0.105.2"), _release("v0.105.1")]
WITH_ONE_X = [V1, V1_TAG, *ZERO_X]


def _date(when: datetime | None) -> dict[str, str]:
    return {} if when is None else {"Date": format_datetime(when, usegmt=True)}


def _answer(httpx_mock, url: str, value: dict | list | int, now: datetime | None) -> None:
    if isinstance(value, int):
        httpx_mock.add_response(url=url, status_code=value, headers=_date(now))
    else:
        httpx_mock.add_response(url=url, json=value, headers=_date(now))


def _github(
    httpx_mock,
    *,
    now: datetime | None,
    latest: dict | int | None = V0,
    tag: dict | int | None = None,
    releases: list[dict] | int | None = None,
) -> None:
    """Register one answer each for ``/releases/latest``, the ``v1.0.0`` tag and the list."""
    for url, value in ((LATEST_URL, latest), (TAG_URL, tag), (LIST_URL, releases)):
        if value is not None:
            _answer(httpx_mock, url, value, now)


@pytest.fixture(autouse=True)
def _running_0x(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run as 0.105.2, as a uv tool install (not frozen), with a fixed install command."""
    for module in (version_service, auto_update):
        monkeypatch.setattr(module, "__version__", RUNNING)
        monkeypatch.setattr(module, "detect_frozen_distribution", lambda: None)
    monkeypatch.setattr(version_service, "build_kbagent_upgrade_command", lambda **_: list(INSTALL))
    monkeypatch.delenv("KBAGENT_AUTO_UPDATE", raising=False)


@pytest.fixture
def local_clock(monkeypatch: pytest.MonkeyPatch):
    """Set the local clock of the updater; returns a setter."""

    def set_clock(when: datetime) -> None:
        fake = SimpleNamespace(time=lambda: when.timestamp())
        monkeypatch.setattr(version_service, "time", fake)
        monkeypatch.setattr(auto_update, "time", fake)

    return set_clock


def _cache(**keys: object) -> dict:
    return {"last_check": 0.0, "latest_version": RUNNING, **keys}


def _cached_next_major(
    *, release_id: int = 101, published: datetime | None = T0, checked: datetime
) -> dict:
    return {
        "next_major_version": "1.0.0",
        "next_major_release_id": release_id,
        "next_major_published_at": (
            published.strftime("%Y-%m-%dT%H:%M:%SZ") if published is not None else None
        ),
        "checked_at": checked.timestamp(),
    }


@pytest.fixture
def startup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Run ``maybe_auto_update`` with every way to install replaced by a mock."""
    mocks = SimpleNamespace(
        perform=MagicMock(return_value=auto_update.UpdateResult(auto_update.UpdateOutcome.SUCCESS)),
        reexec=MagicMock(),
        defer=MagicMock(return_value=True),
    )
    monkeypatch.setattr(auto_update, "_perform_update", mocks.perform)
    monkeypatch.setattr(auto_update, "_re_exec", mocks.reexec)
    monkeypatch.setattr(auto_update, "request_deferred_update", mocks.defer)
    monkeypatch.setattr(auto_update, "should_defer", lambda: False)  # POSIX
    monkeypatch.setattr(auto_update, "_should_skip_all", lambda: False)
    monkeypatch.setattr(auto_update, "_should_skip_kbagent_stage", lambda: False)
    monkeypatch.setattr(auto_update, "report_finished_deferred_update", lambda: None)
    cache_file = tmp_path / "version_cache.json"

    def run(*, cache: dict | None = None, json_mode: bool = False) -> SimpleNamespace:
        if cache is not None:
            cache_file.write_text(json.dumps(cache))
        monkeypatch.setattr(auto_update, "_AUTO_UPDATE_RAN", False)
        auto_update.maybe_auto_update(json_mode=json_mode)
        mocks.cache = json.loads(cache_file.read_text()) if cache_file.exists() else None
        return mocks

    return run


def _installed_nothing(mocks: SimpleNamespace) -> bool:
    return not (mocks.perform.called or mocks.defer.called or mocks.reexec.called)


def _urls(httpx_mock) -> list[str]:
    return [str(request.url) for request in httpx_mock.get_requests()]


class TestNoNextMajor:
    """Before ``v1.0.0`` exists the updater behaves as today and prints nothing about 1.x."""

    def test_installs_the_newest_0x_inline_and_re_execs(
        self, httpx_mock, local_clock, startup, capsys
    ) -> None:
        local_clock(T0)
        _github(httpx_mock, now=T0, tag=404)

        mocks = startup()

        assert mocks.perform.call_args.args == ("0.106.0",)
        mocks.reexec.assert_called_once()
        mocks.defer.assert_not_called()
        assert "deprecated" not in capsys.readouterr().err
        assert update_notice.pending_warnings() == []
        assert "next_major_version" not in mocks.cache
        # The 404 is remembered, so the tag is not read again for 24 hours.
        assert mocks.cache["checked_at"] == T0.timestamp()
        assert _urls(httpx_mock) == [LATEST_URL, TAG_URL]

    def test_within_24_hours_only_releases_latest_is_read(self, httpx_mock, local_clock) -> None:
        local_clock(T0)
        _github(httpx_mock, now=T0)
        cache = _cache(checked_at=(T0 - timedelta(hours=23)).timestamp())

        gated = fetch_gated_release(cache=cache)

        assert gated.latest_version == "0.106.0"
        assert gated.next_major is None
        assert _urls(httpx_mock) == [LATEST_URL]


class TestBeforeMigration:
    """From T0 to M the client stays on 0.x and shows the notice."""

    NOW = T0 + timedelta(days=5)

    def test_stays_on_0x(self, httpx_mock, local_clock, startup) -> None:
        local_clock(self.NOW)
        _github(httpx_mock, now=self.NOW, tag=V1_TAG)

        mocks = startup()

        assert mocks.perform.call_args.args == ("0.106.0",)
        mocks.defer.assert_not_called()
        assert mocks.cache["latest_version"] == "0.106.0"
        assert mocks.cache["next_major_version"] == "1.0.0"
        assert mocks.cache["next_major_release_id"] == 101
        assert mocks.cache["next_major_published_at"] == "2026-11-02T14:05:00Z"
        assert mocks.cache["checked_at"] == self.NOW.timestamp()
        assert _urls(httpx_mock) == [LATEST_URL, TAG_URL]

    def test_human_notice_on_stderr(self, httpx_mock, local_clock, startup, capsys) -> None:
        local_clock(self.NOW)
        _github(httpx_mock, now=self.NOW, tag=V1_TAG)

        startup()

        err = capsys.readouterr().err
        line = next(line for line in err.splitlines() if "deprecated" in line)
        assert line == (
            "Warning: kbagent 0.105 is deprecated. kbagent 1.0.0 was released on "
            "2026-11-02 14:05 UTC. This installation updates itself to 1.x on 2026-11-30 at "
            f"14:05 UTC. What changes: {MIGRATION_GUIDE_URL}. Update now: kbagent update "
            "--major. To stay on 0.x: set KBAGENT_AUTO_UPDATE=false (and pin keboola-cli<1 "
            "in CI)."
        )

    def test_json_notice_in_warnings_not_on_stderr(
        self, httpx_mock, local_clock, startup, capsys
    ) -> None:
        local_clock(self.NOW)
        _github(httpx_mock, now=self.NOW, tag=V1_TAG)

        startup(json_mode=True)

        assert "deprecated" not in capsys.readouterr().err
        envelope = json.loads(success_json({"x": 1}))
        (entry,) = envelope["warnings"]
        assert entry["code"] == "DEPRECATED_VERSION"
        assert entry["hint"] == "kbagent update --major"
        assert entry["current_version"] == RUNNING
        assert entry["next_version"] == "1.0.0"
        assert entry["released_at"] == "2026-11-02T14:05:00Z"
        assert entry["migration_at"] == "2026-11-30T14:05:00Z"
        assert entry["url"] == MIGRATION_GUIDE_URL
        assert entry["message"].startswith("kbagent 0.105 is deprecated.")

    def test_human_notice_once_per_24_hours(self, httpx_mock, local_clock, startup, capsys) -> None:
        local_clock(self.NOW)
        shown = {"notice_shown_at": (self.NOW - timedelta(hours=2)).timestamp()}
        cache = _cache(**_cached_next_major(checked=self.NOW - timedelta(hours=1)), **shown)
        _github(httpx_mock, now=self.NOW)

        startup(cache=cache)

        assert "deprecated" not in capsys.readouterr().err

    def test_human_notice_every_run_in_the_last_72_hours(
        self, httpx_mock, local_clock, startup, capsys
    ) -> None:
        now = M - timedelta(hours=10)
        local_clock(now)
        shown = {"notice_shown_at": (now - timedelta(minutes=5)).timestamp()}
        cache = _cache(**_cached_next_major(checked=now - timedelta(hours=1)), **shown)
        _github(httpx_mock, now=now)

        startup(cache=cache)

        assert "deprecated" in capsys.readouterr().err

    def test_tag_is_read_at_most_once_per_24_hours(self, httpx_mock, local_clock) -> None:
        local_clock(self.NOW)
        cache = _cache(**_cached_next_major(checked=self.NOW - timedelta(hours=23)))
        _github(httpx_mock, now=self.NOW)

        gated = fetch_gated_release(cache=cache)

        assert gated.latest_version == "0.106.0"
        assert gated.next_major is not None and gated.next_major.release_id == 101
        assert _urls(httpx_mock) == [LATEST_URL]


class TestAtMigration:
    """At or after M the next run crosses through the deferred helper, on every OS."""

    def test_crosses_through_the_deferred_helper(self, httpx_mock, local_clock, startup) -> None:
        now = M + timedelta(seconds=1)
        local_clock(now)
        # `promote-major` has marked 1.0.1 as latest: two requests, no list.
        _github(httpx_mock, now=now, latest=V1, tag=V1_TAG)

        mocks = startup()

        mocks.defer.assert_called_once()
        request = mocks.defer.call_args.args[0]
        assert request.target_version == "1.0.1"
        assert request.from_version == RUNNING
        assert request.major_flag is False
        # This command finishes on 0.x: nothing inline, no re-exec.
        mocks.perform.assert_not_called()
        mocks.reexec.assert_not_called()
        # Clients older than the gate read this file: never a 1.x version there.
        assert mocks.cache["latest_version"] == RUNNING
        assert _urls(httpx_mock) == [LATEST_URL, TAG_URL]

    def test_before_promote_major_the_list_names_the_target(
        self, httpx_mock, local_clock, startup
    ) -> None:
        now = M + timedelta(minutes=5)
        local_clock(now)
        _github(httpx_mock, now=now, latest=V0, tag=V1_TAG, releases=WITH_ONE_X)

        mocks = startup()

        assert mocks.defer.call_args.args[0].target_version == "1.0.1"
        assert _urls(httpx_mock) == [LATEST_URL, TAG_URL, LIST_URL]

    def test_reads_the_release_once_more_before_the_crossing(
        self, httpx_mock, local_clock, startup
    ) -> None:
        now = M + timedelta(hours=1)
        local_clock(now)
        cache = _cache(**_cached_next_major(checked=now - timedelta(hours=2)))
        _github(httpx_mock, now=now, latest=V1, tag=V1_TAG)

        mocks = startup(cache=cache)

        assert _urls(httpx_mock) == [LATEST_URL, TAG_URL]
        mocks.defer.assert_called_once()

    def test_failed_final_check_holds_the_crossing(self, httpx_mock, local_clock, startup) -> None:
        now = M + timedelta(hours=1)
        local_clock(now)
        cache = _cache(**_cached_next_major(checked=now - timedelta(hours=2)))
        _github(httpx_mock, now=now, latest=V1, tag=403, releases=WITH_ONE_X)

        mocks = startup(cache=cache)

        mocks.defer.assert_not_called()
        assert mocks.perform.call_args.args == ("0.106.0",)

    def test_missing_date_header_holds_the_crossing(self, httpx_mock, local_clock, startup):
        local_clock(M + timedelta(days=3))  # the local clock alone never crosses
        _github(httpx_mock, now=None, latest=V1, tag=V1_TAG, releases=WITH_ONE_X)

        mocks = startup(json_mode=True)

        mocks.defer.assert_not_called()
        assert mocks.perform.call_args.args == ("0.106.0",)
        # The notice still shows, built with the local clock.
        (entry,) = update_notice.pending_warnings()
        assert "once every kbagent process has exited" in entry["message"]

    def test_utc_date_without_zone_is_utc(self, httpx_mock, local_clock) -> None:
        local_clock(T0)
        header = (M + timedelta(minutes=1)).strftime("%a, %d %b %Y %H:%M:%S -0000")
        httpx_mock.add_response(url=LATEST_URL, json=V1, headers={"Date": header})
        httpx_mock.add_response(url=TAG_URL, json=V1_TAG, headers={"Date": header})

        assert fetch_gated_release().latest_version == "1.0.1"


class TestMajorFlag:
    """``kbagent update --major`` crosses before M, and only through the deferred helper."""

    NOW = T0 + timedelta(days=1)

    @pytest.fixture
    def installer(self, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
        mocks = SimpleNamespace(defer=MagicMock(return_value=True), run=MagicMock())
        monkeypatch.setattr(version_service, "request_deferred_update", mocks.defer)
        monkeypatch.setattr(version_service, "run_install", mocks.run)
        monkeypatch.setattr(version_service, "should_defer", lambda: False)  # POSIX
        return mocks

    def test_major_crosses_at_once(self, httpx_mock, local_clock, installer) -> None:
        local_clock(self.NOW)
        _github(httpx_mock, now=self.NOW, tag=V1_TAG, releases=WITH_ONE_X)

        result = VersionService().self_update(allow_major=True)

        request = installer.defer.call_args.args[0]
        assert request.target_version == "1.0.1"
        assert request.major_flag is True
        installer.run.assert_not_called()
        assert result["kbagent"]["deferred"] is True

    def test_major_reads_the_tag_despite_a_remembered_404(
        self, httpx_mock, local_clock, installer
    ) -> None:
        """The startup hook remembered a 404 two hours ago; v1.0.0 came out one hour ago."""
        published = self.NOW - timedelta(hours=1)
        local_clock(self.NOW)
        (Path(auto_update._get_cache_path())).write_text(
            json.dumps(_cache(checked_at=(self.NOW - timedelta(hours=2)).timestamp()))
        )
        tag = _release("v1.0.0", release_id=101, published=published)
        _github(httpx_mock, now=self.NOW, tag=tag, releases=WITH_ONE_X)

        result = VersionService().self_update(allow_major=True)

        assert installer.defer.call_args.args[0].target_version == "1.0.1"
        assert result["kbagent"]["deferred"] is True

    def test_update_without_major_stays_on_0x(self, httpx_mock, local_clock, installer) -> None:
        local_clock(self.NOW)
        _github(httpx_mock, now=self.NOW, tag=V1_TAG)

        VersionService().self_update()

        installer.defer.assert_not_called()
        assert installer.run.call_args.args[0] == INSTALL
        assert update_notice.current() is not None  # `update` prints the notice

    @pytest.mark.parametrize("when", [T0 + timedelta(days=1), M + timedelta(days=1)])
    def test_beta_alone_never_crosses(self, httpx_mock, local_clock, when: datetime) -> None:
        local_clock(when)
        betas = [_release("v1.1.0b1", prerelease=True), _release("v0.107.0b1", prerelease=True)]
        _github(httpx_mock, now=when, latest=None, tag=V1_TAG, releases=betas + WITH_ONE_X)

        gated = fetch_gated_release(include_prerelease=True)

        assert gated.latest_version == "0.107.0b1"
        assert _urls(httpx_mock) == [LIST_URL, TAG_URL]

    def test_beta_with_major_may_take_a_1x_prerelease(self, httpx_mock, local_clock) -> None:
        local_clock(self.NOW)
        releases = [_release("v1.1.0b1", prerelease=True), *WITH_ONE_X]
        _github(httpx_mock, now=self.NOW, latest=None, tag=V1_TAG, releases=releases)

        gated = fetch_gated_release(include_prerelease=True, allow_major=True)

        assert gated.latest_version == "1.1.0b1"


class TestOffline:
    """A failed fetch installs nothing; HTTP 403 and 429 count as offline."""

    @pytest.mark.parametrize("failure", [403, 429, 503, "network"])
    def test_installs_nothing(self, httpx_mock, local_clock, startup, failure) -> None:
        local_clock(M + timedelta(days=1))
        if failure == "network":
            httpx_mock.add_exception(httpx.ConnectError("offline"), url=LATEST_URL)
        else:
            httpx_mock.add_response(url=LATEST_URL, status_code=failure)

        mocks = startup()

        assert _installed_nothing(mocks)

    def test_notice_uses_the_cached_date(self, httpx_mock, local_clock, startup) -> None:
        now = T0 + timedelta(days=3)
        local_clock(now)
        httpx_mock.add_response(url=LATEST_URL, status_code=403)
        cache = _cache(**_cached_next_major(checked=now - timedelta(days=2)))

        mocks = startup(cache=cache, json_mode=True)

        assert _installed_nothing(mocks)
        (entry,) = update_notice.pending_warnings()
        assert entry["migration_at"] == "2026-11-30T14:05:00Z"
        assert mocks.cache["next_major_published_at"] == "2026-11-02T14:05:00Z"

    def test_unsupported_proxy_scheme_is_offline(self, monkeypatch, startup) -> None:
        """httpx raises ValueError for the scheme before any request (no traceback)."""
        monkeypatch.setenv("HTTPS_PROXY", "ftp://127.0.0.1:1")

        assert fetch_gated_release().latest_version is None
        assert _installed_nothing(startup())


class TestCachedHigherMajor:
    """A 1.x ``latest_version`` written by a client older than the gate is ignored."""

    def test_is_never_installed(self, httpx_mock, local_clock, startup) -> None:
        now = T0 + timedelta(days=1)
        local_clock(now)
        _github(httpx_mock, now=now, latest=_release("v0.105.2"), tag=404)
        fresh = {"last_check": now.timestamp(), "latest_version": "1.0.0"}

        mocks = startup(cache=fresh)

        # The fresh cache did not short-circuit the lookup, and 1.0.0 was not installed.
        assert _urls(httpx_mock)[0] == LATEST_URL
        assert _installed_nothing(mocks)
        assert mocks.cache["latest_version"] == RUNNING


class TestSameMajorBeyondTheFirstPage:
    def test_newest_0x_after_40_releases_of_1x(self, httpx_mock, local_clock) -> None:
        """``/releases/latest`` names 1.x and the crossing is held: the list finds 0.x."""
        now = T0 + timedelta(days=2)
        local_clock(now)
        one_x = [_release(f"v1.{n}.0", release_id=200 + n) for n in range(40, 0, -1)]
        _github(httpx_mock, now=now, latest=one_x[0], tag=V1_TAG, releases=[*one_x, *ZERO_X])

        gated = fetch_gated_release()

        assert gated.latest_version == "0.106.0"
        assert gated.same_major_version == "0.106.0"
        assert _urls(httpx_mock) == [LATEST_URL, TAG_URL, LIST_URL]


class TestMissingOrChangedRelease:
    def test_404_after_t0_holds_with_an_unknown_date(
        self, httpx_mock, local_clock, startup
    ) -> None:
        now = M + timedelta(days=2)
        local_clock(now)
        cache = _cache(**_cached_next_major(checked=now - timedelta(days=2)))
        _github(httpx_mock, now=now, tag=404)

        mocks = startup(cache=cache, json_mode=True)

        mocks.defer.assert_not_called()
        assert mocks.perform.call_args.args == ("0.106.0",)
        (entry,) = update_notice.pending_warnings()
        assert entry["migration_at"] is None
        assert entry["released_at"] is None
        assert "release date is unknown" in entry["message"]
        assert mocks.cache["next_major_published_at"] is None

    def test_republished_release_restarts_the_28_days(
        self, httpx_mock, local_clock, startup
    ) -> None:
        now = M + timedelta(days=2)  # past the first M
        local_clock(now)
        cache = _cache(**_cached_next_major(checked=now - timedelta(days=2)))
        republished = _release("v1.0.0", release_id=202, published=T0 + timedelta(days=20))
        _github(httpx_mock, now=now, tag=republished)

        mocks = startup(cache=cache, json_mode=True)

        mocks.defer.assert_not_called()
        assert mocks.cache["next_major_release_id"] == 202
        (entry,) = update_notice.pending_warnings()
        assert entry["migration_at"] == "2026-12-20T14:05:00Z"

    def test_new_id_with_an_earlier_date_never_shortens_the_window(self, httpx_mock, local_clock):
        now = T0 + timedelta(days=10)
        local_clock(now)
        cache = _cache(**_cached_next_major(checked=now - timedelta(days=2)))
        earlier = _release("v1.0.0", release_id=202, published=T0 - timedelta(days=20))
        _github(httpx_mock, now=now, tag=earlier)

        gated = fetch_gated_release(cache=cache)

        assert gated.next_major is not None
        assert gated.next_major.published_at == "2026-11-02T14:05:00Z"


class TestLocalClockDoesNotMoveM:
    """'Now' is the ``Date`` header of the GitHub response."""

    def test_local_clock_ahead_does_not_cross(self, httpx_mock, local_clock) -> None:
        local_clock(M + timedelta(days=30))
        _github(httpx_mock, now=T0 + timedelta(days=5), tag=V1_TAG)

        assert fetch_gated_release().latest_version == "0.106.0"

    def test_local_clock_behind_still_crosses_at_m(self, httpx_mock, local_clock) -> None:
        local_clock(T0 - timedelta(days=30))
        _github(httpx_mock, now=M + timedelta(minutes=1), latest=V1, tag=V1_TAG)

        assert fetch_gated_release().latest_version == "1.0.1"


class TestCommands:
    """``version``/``update``/``doctor`` show the notice; ``--beta`` never writes the cache."""

    NOW = T0 + timedelta(days=5)

    @pytest.fixture(autouse=True)
    def _no_startup_check(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(auto_update, "_AUTO_UPDATE_RAN", True)
        monkeypatch.setenv("KBAGENT_CONFIG_DIR", str(tmp_path / "cfg"))

    def test_json_version_carries_the_warning(self, httpx_mock, local_clock) -> None:
        local_clock(self.NOW)
        _github(httpx_mock, now=self.NOW, tag=V1_TAG)

        result = CliRunner().invoke(app, ["--json", "version"])

        envelope = json.loads(result.stdout)
        assert envelope["data"]["kbagent"]["latest_version"] == "0.106.0"
        assert [w["code"] for w in envelope["warnings"]] == ["DEPRECATED_VERSION"]

    def test_human_version_prints_the_notice(self, httpx_mock, local_clock) -> None:
        local_clock(self.NOW)
        _github(httpx_mock, now=self.NOW, tag=V1_TAG)

        result = CliRunner().invoke(app, ["version"])

        assert "kbagent 0.105 is deprecated" in result.stderr

    def test_auto_update_off_form(self, httpx_mock, local_clock, monkeypatch) -> None:
        monkeypatch.setenv("KBAGENT_AUTO_UPDATE", "false")
        local_clock(self.NOW)
        _github(httpx_mock, now=self.NOW, tag=V1_TAG)

        result = CliRunner().invoke(app, ["version"])

        assert "This installation stays on 0.x; run kbagent update --major." in result.stderr
        assert "In CI, also pin keboola-cli<1 for the install." in result.stderr
        assert "set KBAGENT_AUTO_UPDATE=false" not in result.stderr

    def test_version_beta_does_not_write_the_cache(self, httpx_mock, local_clock, tmp_path) -> None:
        local_clock(T0 - timedelta(days=5))
        _github(httpx_mock, now=T0 - timedelta(days=5), latest=None, tag=404, releases=ZERO_X)

        VersionService().get_versions(include_prerelease=True)

        assert not (tmp_path / "version_cache.json").exists()

    def test_doctor_shows_the_cached_notice(self, local_clock, tmp_path, monkeypatch) -> None:
        local_clock(self.NOW)
        cache = _cache(**_cached_next_major(checked=self.NOW))
        (tmp_path / "version_cache.json").write_text(json.dumps(cache))
        monkeypatch.setattr(
            "keboola_agent_cli.services.doctor_service.DoctorService.run_checks",
            lambda self: {"checks": [], "summary": {}},
        )

        result = CliRunner().invoke(app, ["--json", "doctor"])

        assert [w["code"] for w in json.loads(result.stdout)["warnings"]] == ["DEPRECATED_VERSION"]

    def test_update_major_run_shows_no_notice(self, httpx_mock, local_clock, monkeypatch) -> None:
        monkeypatch.setattr(version_service, "request_deferred_update", lambda _: True)
        monkeypatch.setattr(version_service, "should_defer", lambda: False)
        local_clock(self.NOW)
        cache = _cache(**_cached_next_major(checked=self.NOW))
        (Path(auto_update._get_cache_path())).write_text(json.dumps(cache))
        _github(httpx_mock, now=self.NOW, tag=V1_TAG, releases=WITH_ONE_X)

        result = CliRunner().invoke(app, ["--json", "update", "--major"])

        envelope = json.loads(result.stdout)
        assert envelope["data"]["kbagent"]["deferred"] is True
        assert "warnings" not in envelope

    @pytest.mark.parametrize("command", [["version"], ["update"]])
    def test_unsupported_proxy_scheme_gives_no_traceback(self, monkeypatch, command) -> None:
        monkeypatch.setenv("HTTPS_PROXY", "ftp://127.0.0.1:1")

        result = CliRunner().invoke(app, ["--json", *command])

        assert result.exception is None, result.exception
        assert result.exit_code == 0
        assert json.loads(result.stdout)["data"]["kbagent"]["latest_version"] is None


class TestEnvelopePlacement:
    """The notice in the error envelope, and for every envelope of a process (REPL)."""

    @pytest.fixture(autouse=True)
    def _notice(self) -> None:
        update_notice.register(
            update_notice.build_notice(
                current_version=RUNNING,
                next_version="1.0.0",
                published_ts=T0.timestamp(),
                migration_ts=M.timestamp(),
                now=(T0 + timedelta(days=1)).timestamp(),
                mode=update_notice.NoticeMode.AUTO_UPDATE,
            )
        )

    def test_error_envelope_lists_the_notice_before_command_warnings(self, capsys) -> None:
        formatter = OutputFormatter(json_mode=True, no_color=True)
        formatter.warning("own warning")
        formatter.error("boom", error_code="CONFIG_ERROR")

        envelope = json.loads(capsys.readouterr().out)
        assert envelope["status"] == "error"
        assert [w["code"] for w in envelope["warnings"]] == ["DEPRECATED_VERSION", "WARNING"]
        assert list(envelope) == ["status", "warnings", "error"]

    def test_every_envelope_of_a_repl_session_carries_it(self, capsys) -> None:
        # The REPL runs each line with a new formatter in the same process; the
        # startup check ran once, so its notice stays for the whole session.
        for _ in range(2):
            OutputFormatter(json_mode=True, no_color=True).output({"n": 1})

        out = capsys.readouterr().out
        decoder, envelopes, pos = json.JSONDecoder(), [], 0
        while pos < len(out.strip()):
            envelope, end = decoder.raw_decode(out, pos)
            envelopes.append(envelope)
            pos = end + 1
        assert len(envelopes) == 2
        for envelope in envelopes:
            assert [w["code"] for w in envelope["warnings"]] == ["DEPRECATED_VERSION"]


def test_frozen_form_names_the_channel_command() -> None:
    notice = update_notice.build_notice(
        current_version=RUNNING,
        next_version="1.0.0",
        published_ts=T0.timestamp(),
        migration_ts=M.timestamp(),
        now=(T0 + timedelta(days=1)).timestamp(),
        mode=update_notice.NoticeMode.FROZEN,
        channel_command="upgrade it with: brew upgrade kbagent.",
    )

    assert "Your package manager installs 1.x now: upgrade it with: brew upgrade kbagent." in (
        notice.text
    )
    assert ".." not in notice.text
    assert "Update now" not in notice.text
    assert "KBAGENT_AUTO_UPDATE" not in notice.text
