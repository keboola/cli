"""Tests for scripts/release_latest.py: the GitHub "latest" flag across a major change.

T0 of major N is ``published_at`` of the stable release vN.0.0, M is T0 + 28 days.
The failure modes worth pinning are the two that move clients at the wrong time:

- a 0.x release stops being latest before M (clients that follow ``releases/latest``
  would stay on an older 0.x, and from the patch on they would see no fix), or
- a 1.x release becomes latest before M (every client older than the 0.x patch
  installs 1.x within the hour).

The decision functions are pure and tested directly; ``classify`` / ``promote`` are
tested through a fake reader with the same methods as ``GitHubReader``; ``main`` is
tested once against a fake HTTP layer, so the 404 handling and the server clock are
covered too.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.error
from datetime import UTC, datetime, timedelta
from email.message import Message
from email.utils import format_datetime
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release_latest.py"
_SPEC = importlib.util.spec_from_file_location("_release_latest_under_test", SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
rl = importlib.util.module_from_spec(_SPEC)
sys.modules["_release_latest_under_test"] = rl
_SPEC.loader.exec_module(rl)

T0 = datetime(2026, 11, 2, 14, 5, tzinfo=UTC)
M = T0 + timedelta(days=28)
BEFORE_T0 = T0 - timedelta(days=3)
BETWEEN = T0 + timedelta(days=10)
JUST_BEFORE_M = M - timedelta(seconds=1)
AFTER_M = M + timedelta(hours=1)


def decide(version: str, now: datetime, **overrides) -> bool:
    """decide_make_latest with the 1.0.0 timeline: v1.0.0 published at T0 (if now >= T0)."""
    published = now >= T0
    kwargs = {
        "prerelease": False,
        "own_major_start": T0 if version.startswith("1.") and published else None,
        "next_major_start": T0 if version.startswith("0.") and published else None,
        "current_latest": None,
        "now": now,
    }
    kwargs.update(overrides)
    return rl.decide_make_latest(version, **kwargs)


class TestDecideMakeLatest:
    @pytest.mark.parametrize("now", [BEFORE_T0, BETWEEN, JUST_BEFORE_M])
    def test_zero_x_release_before_m_is_latest(self, now: datetime) -> None:
        assert decide("0.105.1", now) is True

    @pytest.mark.parametrize("now", [M, AFTER_M])
    def test_zero_x_release_from_m_is_not_latest(self, now: datetime) -> None:
        assert decide("0.105.3", now) is False

    def test_v1_0_0_while_it_is_being_created_is_not_latest(self) -> None:
        """The release v1.0.0 does not exist yet when its own job decides the flag."""
        assert decide("1.0.0", BEFORE_T0, own_major_start=None) is False

    def test_v1_0_0_rerun_after_publication_is_not_latest(self) -> None:
        assert decide("1.0.0", T0 + timedelta(minutes=5)) is False

    @pytest.mark.parametrize("now", [BETWEEN, JUST_BEFORE_M])
    def test_one_x_release_before_m_is_not_latest(self, now: datetime) -> None:
        assert decide("1.0.1", now) is False

    @pytest.mark.parametrize("now", [M, AFTER_M])
    def test_one_x_release_from_m_is_latest(self, now: datetime) -> None:
        assert decide("1.1.0", now) is True

    def test_pre_release_is_never_latest(self) -> None:
        assert decide("0.106.0b1", BEFORE_T0, prerelease=True) is False
        assert decide("1.2.0rc1", AFTER_M, prerelease=True) is False

    def test_post_release_counts_as_stable(self) -> None:
        assert decide("0.105.1.post1", BEFORE_T0) is True

    def test_never_moves_the_flag_back_to_a_lower_version(self) -> None:
        """A re-run of an older tag must not take the flag from a newer release."""
        assert decide("0.105.0", BEFORE_T0, current_latest=(0, 105, 1, 0)) is False

    def test_equal_or_lower_current_latest_keeps_true(self) -> None:
        assert decide("0.105.1", BEFORE_T0, current_latest=(0, 105, 1, 0)) is True
        assert decide("0.105.1", BEFORE_T0, current_latest=(0, 105, 0, 0)) is True

    def test_one_x_after_m_is_latest_while_a_zero_x_is_still_latest(self) -> None:
        """At M, before promote-major ran, the current latest is still 0.x."""
        assert decide("1.2.0", AFTER_M, current_latest=(0, 105, 3, 0)) is True

    def test_next_major_generalizes(self) -> None:
        """1.x behaves like 0.x once v2.0.0 exists."""
        start2 = datetime(2027, 6, 1, tzinfo=UTC)
        kwargs = {"own_major_start": T0, "current_latest": None, "prerelease": False}
        assert rl.decide_make_latest(
            "1.9.0", next_major_start=start2, now=start2 + timedelta(days=1), **kwargs
        )
        assert not rl.decide_make_latest(
            "1.9.1", next_major_start=start2, now=start2 + timedelta(days=28), **kwargs
        )

    def test_rejects_a_non_stable_version_without_the_prerelease_flag(self) -> None:
        with pytest.raises(ValueError):
            decide("0.106.0b1", BEFORE_T0)


def release(tag: str, published_at: datetime) -> rl.Release:
    return rl.Release(tag=tag, published_at=published_at)


ZERO_X = [
    release("v0.105.3", T0 + timedelta(days=12)),
    release("v0.105.2", T0 - timedelta(days=20)),
]
ONE_X = [
    release("v1.1.0", T0 + timedelta(days=20)),
    release("v1.0.1", T0 + timedelta(days=4)),
    release("v1.0.0", T0),
]


ONE_START = {1: T0}


class TestPromotionTarget:
    def test_nothing_before_t0(self) -> None:
        assert rl.promotion_target(ZERO_X, {}, "v0.105.3", BEFORE_T0) is None

    @pytest.mark.parametrize("now", [T0, BETWEEN, JUST_BEFORE_M])
    def test_nothing_before_m(self, now: datetime) -> None:
        assert rl.promotion_target(ONE_X + ZERO_X, ONE_START, "v0.105.3", now) is None

    def test_newest_one_x_at_m(self) -> None:
        assert rl.promotion_target(ONE_X + ZERO_X, ONE_START, "v0.105.3", M) == "v1.1.0"

    def test_picks_the_highest_version_not_the_newest_publication(self) -> None:
        releases = [release("v1.0.2", T0 + timedelta(days=21)), *ONE_X]
        assert rl.promotion_target(releases, ONE_START, "v0.105.3", AFTER_M) == "v1.1.0"

    def test_idempotent_when_the_target_is_already_latest(self) -> None:
        assert rl.promotion_target(ONE_X + ZERO_X, ONE_START, "v1.1.0", AFTER_M) is None

    def test_corrects_a_lower_one_x_latest(self) -> None:
        """A release decided before M but published after M got make_latest=false."""
        releases = [release("v1.2.0", M + timedelta(minutes=2)), *ONE_X]
        assert rl.promotion_target(releases, ONE_START, "v1.1.0", AFTER_M) == "v1.2.0"

    def test_start_release_need_not_be_in_the_list(self) -> None:
        """T0 comes from the tag endpoint, so v1.0.0 may be older than the list page."""
        assert rl.promotion_target(ONE_X[:2] + ZERO_X, ONE_START, "v0.105.3", AFTER_M) == "v1.1.0"

    def test_unpublished_start_gives_no_target(self) -> None:
        assert rl.promotion_target(ONE_X + ZERO_X, {1: None}, "v0.105.3", AFTER_M) is None

    def test_ignores_tags_that_are_not_stable_versions(self) -> None:
        releases = [release("v1.3.0b1", AFTER_M), release("nightly", AFTER_M), *ONE_X]
        assert rl.promotion_target(releases, ONE_START, "v0.105.3", AFTER_M) == "v1.1.0"


class FakeReader:
    """Same interface as GitHubReader, backed by dicts."""

    def __init__(
        self,
        now: datetime,
        starts: dict[int, datetime] | None = None,
        latest: str | None = None,
        releases: list[rl.Release] | None = None,
    ) -> None:
        self.now = now
        self.starts = starts or {}
        self.latest = latest
        self.releases = releases or []
        self.start_calls: list[int] = []

    def server_now(self) -> datetime:
        return self.now

    def major_start(self, major: int) -> datetime | None:
        self.start_calls.append(major)
        return self.starts.get(major)

    def latest_tag(self) -> str | None:
        return self.latest

    def stable_releases(self) -> list[rl.Release]:
        return self.releases


class TestClassify:
    def test_zero_x_before_t0(self) -> None:
        reader = FakeReader(BEFORE_T0, latest="v0.105.0")
        result = rl.classify(reader, "0.105.1", prerelease=False)  # type: ignore[arg-type]
        assert result == {"make_latest": "true", "old_line": "false"}
        assert reader.start_calls == [1]  # major 0 has no own start

    def test_release_branch_fix_between_t0_and_m(self) -> None:
        reader = FakeReader(BETWEEN, starts={1: T0}, latest="v0.105.2")
        result = rl.classify(reader, "0.105.3", prerelease=False)  # type: ignore[arg-type]
        assert result == {"make_latest": "true", "old_line": "true"}

    def test_release_branch_fix_after_m(self) -> None:
        reader = FakeReader(AFTER_M, starts={1: T0}, latest="v1.1.0")
        result = rl.classify(reader, "0.105.4", prerelease=False)  # type: ignore[arg-type]
        assert result == {"make_latest": "false", "old_line": "true"}

    def test_v1_0_0(self) -> None:
        reader = FakeReader(BEFORE_T0, latest="v0.105.2")
        result = rl.classify(reader, "1.0.0", prerelease=False)  # type: ignore[arg-type]
        assert result == {"make_latest": "false", "old_line": "false"}
        assert reader.start_calls == [1, 2]

    def test_pre_release(self) -> None:
        reader = FakeReader(BEFORE_T0, latest="v0.105.2")
        result = rl.classify(reader, "0.106.0b1", prerelease=True)  # type: ignore[arg-type]
        assert result["make_latest"] == "false"

    def test_no_release_at_all(self) -> None:
        reader = FakeReader(BEFORE_T0, latest=None)
        result = rl.classify(reader, "0.1.0", prerelease=False)  # type: ignore[arg-type]
        assert result["make_latest"] == "true"


class TestPromote:
    def test_empty_tag_before_m(self) -> None:
        reader = FakeReader(BETWEEN, starts=ONE_START, latest="v0.105.3", releases=ONE_X + ZERO_X)
        assert rl.promote(reader) == {"tag": ""}  # type: ignore[arg-type]

    def test_tag_at_m(self) -> None:
        reader = FakeReader(M, starts=ONE_START, latest="v0.105.3", releases=ONE_X + ZERO_X)
        assert rl.promote(reader) == {"tag": "v1.1.0"}  # type: ignore[arg-type]
        assert reader.start_calls == [1]

    def test_reads_the_start_of_each_major_from_the_highest_down(self) -> None:
        """Before M of 2.x, the newest 1.x stays the target; one call per major."""
        start2 = AFTER_M + timedelta(days=10)
        releases = [release("v2.0.0", start2), *ONE_X]
        reader = FakeReader(
            start2 + timedelta(days=1),
            starts={1: T0, 2: start2},
            latest="v1.1.0",
            releases=releases,
        )
        assert rl.promote(reader) == {"tag": ""}  # type: ignore[arg-type]
        assert reader.start_calls == [2, 1]

    def test_before_t0_reads_no_start(self) -> None:
        reader = FakeReader(BEFORE_T0, latest="v0.105.2", releases=ZERO_X)
        assert rl.promote(reader) == {"tag": ""}  # type: ignore[arg-type]
        assert reader.start_calls == []


# --- the HTTP layer ---------------------------------------------------------


def _headers(now: datetime) -> Message:
    message = Message()
    message["Date"] = format_datetime(now, usegmt=True)
    return message


class _Response(io.BytesIO):
    def __init__(self, body: object, now: datetime) -> None:
        super().__init__(json.dumps(body).encode())
        self.headers = _headers(now)


def _gh_release(tag: str, published_at: datetime, *, draft: bool = False, pre: bool = False):
    return {
        "tag_name": tag,
        "published_at": published_at.isoformat().replace("+00:00", "Z"),
        "draft": draft,
        "prerelease": pre,
    }


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the retry delays instead of waiting."""
    delays: list[float] = []
    monkeypatch.setattr(rl.time, "sleep", delays.append)
    return delays


class FakeGitHub:
    """urlopen replacement: ``routes`` maps a repo path to a JSON body, others answer 404."""

    def __init__(self) -> None:
        self.routes: dict[str, object] = {}
        self.now = BEFORE_T0
        self.requests: list[tuple[str, dict[str, str]]] = []

    def urlopen(self, request, timeout):
        path = request.full_url.split("/repos/keboola/cli/", 1)[1]
        self.requests.append((path, request.headers))
        if path in self.routes:
            return _Response(self.routes[path], self.now)
        raise urllib.error.HTTPError(
            request.full_url, 404, "Not Found", _headers(self.now), io.BytesIO(b"{}")
        )


@pytest.fixture
def fake_github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    github = FakeGitHub()
    monkeypatch.setattr(rl.urllib.request, "urlopen", github.urlopen)
    monkeypatch.setenv("GITHUB_REPOSITORY", "keboola/cli")
    monkeypatch.setenv("GH_TOKEN", "test-token")
    return github


class TestMain:
    def test_classify_before_t0_answers_404_as_not_published(
        self, fake_github, capsys: pytest.CaptureFixture[str]
    ) -> None:
        routes = fake_github.routes
        routes["releases/latest"] = _gh_release("v0.105.0", BEFORE_T0 - timedelta(days=5))

        assert rl.main(["classify", "--version", "0.105.1"]) == 0

        assert capsys.readouterr().out.splitlines() == ["make_latest=true", "old_line=false"]
        paths = [path for path, _ in fake_github.requests]
        assert paths == ["releases/tags/v1.0.0", "releases/latest"]
        assert fake_github.requests[0][1]["Authorization"] == "Bearer test-token"

    def test_classify_uses_the_server_clock(self, fake_github, capsys) -> None:
        """'Now' comes from the Date header, not from the runner clock."""
        routes = fake_github.routes
        routes["releases/tags/v1.0.0"] = _gh_release("v1.0.0", T0)
        routes["releases/latest"] = _gh_release("v0.105.3", T0 - timedelta(days=1))
        fake_github.now = AFTER_M

        rl.main(["classify", "--version", "0.105.4"])

        assert capsys.readouterr().out.splitlines() == ["make_latest=false", "old_line=true"]

    def test_a_draft_or_pre_release_start_does_not_count(self, fake_github, capsys) -> None:
        routes = fake_github.routes
        routes["releases/tags/v1.0.0"] = _gh_release("v1.0.0", T0, pre=True)
        fake_github.now = AFTER_M

        rl.main(["classify", "--version", "0.105.4"])

        assert capsys.readouterr().out.splitlines() == ["make_latest=true", "old_line=false"]

    def test_any_other_http_error_fails(self, monkeypatch, fake_github) -> None:
        def broken(request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, 500, "boom", _headers(BEFORE_T0), io.BytesIO(b"")
            )

        monkeypatch.setattr(rl.urllib.request, "urlopen", broken)
        with pytest.raises(urllib.error.HTTPError):
            rl.main(["classify", "--version", "0.105.1"])

    def test_promote_filters_drafts_and_pre_releases(self, fake_github, capsys) -> None:
        routes = fake_github.routes
        routes["releases?per_page=100"] = [
            _gh_release("v1.2.0", AFTER_M, draft=True),
            _gh_release("v1.1.1b1", AFTER_M, pre=True),
            _gh_release("v1.1.0", T0 + timedelta(days=20)),
            _gh_release("v1.0.0", T0),
            _gh_release("v0.105.3", T0 + timedelta(days=12)),
        ]
        routes["releases/latest"] = _gh_release("v0.105.3", T0 + timedelta(days=12))
        routes["releases/tags/v1.0.0"] = _gh_release("v1.0.0", T0)
        fake_github.now = AFTER_M

        rl.main(["promote"])

        assert capsys.readouterr().out.splitlines() == ["tag=v1.1.0"]

    def test_promote_reads_t0_from_the_tag_when_v1_0_0_is_off_the_list(
        self, fake_github, capsys
    ) -> None:
        routes = fake_github.routes
        routes["releases?per_page=100"] = [_gh_release("v1.7.0", AFTER_M)]
        routes["releases/latest"] = _gh_release("v0.105.3", T0 + timedelta(days=12))
        routes["releases/tags/v1.0.0"] = _gh_release("v1.0.0", T0)
        fake_github.now = AFTER_M

        rl.main(["promote"])

        assert capsys.readouterr().out.splitlines() == ["tag=v1.7.0"]
        assert ("releases/tags/v1.0.0") in [path for path, _ in fake_github.requests]


class TestRetry:
    """A single 5xx/429/network error must not fail a release; repeated ones must."""

    def _flaky(self, monkeypatch, fake_github, failures: list[BaseException]) -> None:
        real = fake_github.urlopen

        def urlopen(request, timeout):
            if failures:
                raise failures.pop(0)
            return real(request, timeout)

        monkeypatch.setattr(rl.urllib.request, "urlopen", urlopen)

    @staticmethod
    def _http(code: int) -> urllib.error.HTTPError:
        return urllib.error.HTTPError("u", code, "x", _headers(BEFORE_T0), io.BytesIO(b""))

    def test_recovers_after_a_503(self, monkeypatch, fake_github, capsys, no_sleep) -> None:
        fake_github.routes["releases/latest"] = _gh_release("v0.105.0", BEFORE_T0)
        self._flaky(monkeypatch, fake_github, [self._http(503)])

        assert rl.main(["classify", "--version", "0.105.1"]) == 0

        assert capsys.readouterr().out.splitlines() == ["make_latest=true", "old_line=false"]
        assert no_sleep == [2]

    def test_recovers_after_a_429_and_a_network_error(
        self, monkeypatch, fake_github, capsys, no_sleep
    ) -> None:
        self._flaky(monkeypatch, fake_github, [self._http(429), urllib.error.URLError("reset")])

        rl.main(["classify", "--version", "0.105.1"])

        assert capsys.readouterr().out.splitlines() == ["make_latest=true", "old_line=false"]
        assert no_sleep == [2, 4]

    def test_fails_after_repeated_503(self, monkeypatch, fake_github, no_sleep) -> None:
        self._flaky(monkeypatch, fake_github, [self._http(503) for _ in range(4)])

        with pytest.raises(urllib.error.HTTPError) as exc:
            rl.main(["classify", "--version", "0.105.1"])

        assert exc.value.code == 503
        assert no_sleep == [2, 4, 8]

    def test_does_not_retry_a_403(self, monkeypatch, fake_github, no_sleep) -> None:
        self._flaky(monkeypatch, fake_github, [self._http(403)])

        with pytest.raises(urllib.error.HTTPError):
            rl.main(["classify", "--version", "0.105.1"])

        assert no_sleep == []

    def test_promote_before_t0_prints_an_empty_tag(self, fake_github, capsys) -> None:
        routes = fake_github.routes
        routes["releases?per_page=100"] = [_gh_release("v0.105.0", BEFORE_T0)]
        routes["releases/latest"] = _gh_release("v0.105.0", BEFORE_T0)

        rl.main(["promote"])

        assert capsys.readouterr().out.splitlines() == ["tag="]
