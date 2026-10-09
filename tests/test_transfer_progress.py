"""`--progress` on storage uploads/downloads: reporter, CLI wiring, download byte counts."""

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.client import KeboolaClient
from keboola_agent_cli.client._transfer import _CloudDownloader
from keboola_agent_cli.commands._progress import ProgressLog, format_bytes, transfer_progress
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.models import AppConfig, ProjectConfig
from keboola_agent_cli.output import OutputFormatter

TEST_TOKEN = "901-progress-token"
GIB = 1024**3
MIB = 1024**2

runner = CliRunner()


def _args(command: str, *rest: str) -> list[str]:
    return ["--json", "storage", command, "--project", "test", *rest]


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _log(total: int | None, **kwargs) -> tuple[ProgressLog, list[str], _Clock]:
    clock = _Clock()
    lines: list[str] = []
    return ProgressLog("upload big.csv", total, lines.append, clock=clock, **kwargs), lines, clock


class TestFormatBytes:
    def test_units(self) -> None:
        assert format_bytes(512) == "512 B"
        assert format_bytes(1536) == "1.50 KiB"
        assert format_bytes(4.2 * GIB) == "4.20 GiB"


class TestProgressLog:
    def test_line_has_percent_amount_speed_elapsed_eta(self) -> None:
        log, lines, clock = _log(10 * GIB)
        clock.now += 60
        log.update(int(4.2 * GIB), 10 * GIB)
        assert lines == [
            "upload big.csv: 42.0% 4.20/10.00 GiB, 71.68 MiB/s, elapsed 0:01:00, ETA 0:01:22"
        ]

    def test_emits_at_most_once_per_interval(self) -> None:
        log, lines, clock = _log(100 * MIB, interval=10)
        for step in range(1, 26):  # one update per second for 25 s
            clock.now += 1
            log.update(step * MIB, 100 * MIB)
        assert len(lines) == 2  # at t=10 and t=20, not on every callback
        assert "elapsed 0:00:10" in lines[0]
        assert "elapsed 0:00:20" in lines[1]

    def test_speed_uses_trailing_window_not_overall_average(self) -> None:
        log, lines, clock = _log(None, interval=1, window=30)
        # 100 s at 1 MiB/s, then the link speeds up to 10 MiB/s for 60 s.
        for _ in range(100):
            clock.now += 1
            log.update(log._done + MIB, None)
        for _ in range(60):
            clock.now += 1
            log.update(log._done + 10 * MIB, None)
        assert log.window_rate() == pytest.approx(10 * MIB)
        assert "10.00 MiB/s" in lines[-1]
        log.finish()
        # Final line reports the overall average: 700 MiB / 160 s.
        assert lines[-1] == "upload big.csv: 700.00 MiB, avg 4.38 MiB/s, elapsed 0:02:40, done"

    def test_eta_unknown_before_first_sample(self) -> None:
        log, lines, _clock = _log(GIB, interval=0)
        log.update(0, GIB)  # start signal only -- no bytes, no line
        assert lines == []
        log.finish()
        assert lines == ["upload big.csv: 0.0% 0.00/1.00 GiB, avg ?, elapsed 0:00:00, done"]
        log2, lines2, _clock2 = _log(GIB, interval=0)
        log2.update(MIB, GIB)  # same instant as start: no measurable rate yet
        assert lines2[-1].endswith("? /s, elapsed 0:00:00, ETA ?")

    def test_unknown_total_has_no_percent_or_eta(self) -> None:
        log, lines, clock = _log(None, interval=5)
        clock.now += 5
        log.update(50 * MIB, None)
        assert lines == ["upload big.csv: 50.00 MiB, 10.00 MiB/s, elapsed 0:00:05"]

    def test_total_learned_from_callback(self) -> None:
        log, lines, clock = _log(None, interval=5)
        clock.now += 5
        log.update(50 * MIB, 100 * MIB)
        assert lines[0].startswith("upload big.csv: 50.0% 50.00/100.00 MiB, 10.00 MiB/s")
        assert lines[0].endswith("ETA 0:00:05")

    def test_zero_callback_restarts_clock(self) -> None:
        """A download first waits for its export job; that wait is not transfer time."""
        log, lines, clock = _log(None, interval=5)
        clock.now += 300  # export job
        log.update(0, 100 * MIB)
        clock.now += 5
        log.update(50 * MIB, 100 * MIB)
        assert "elapsed 0:00:05" in lines[0]
        assert "10.00 MiB/s" in lines[0]

    def test_final_line_marks_failure(self) -> None:
        log, lines, clock = _log(100 * MIB, interval=999)
        clock.now += 10
        log.update(20 * MIB, 100 * MIB)
        log.finish(failed=True)
        assert lines == [
            "upload big.csv: 20.0% 20.00/100.00 MiB, avg 2.00 MiB/s, elapsed 0:00:10, failed"
        ]


class TestTransferProgressContext:
    def test_default_without_tty_reports_nothing(self, capsys) -> None:
        formatter = OutputFormatter(json_mode=False)
        with transfer_progress(formatter, label="x", total_bytes=10, enabled=False) as cb:
            assert cb is None
        assert capsys.readouterr().err == ""

    def test_enabled_without_tty_writes_lines_to_stderr_only(self, capsys) -> None:
        formatter = OutputFormatter(json_mode=True)
        with transfer_progress(formatter, label="upload a.csv", total_bytes=10, enabled=True) as cb:
            assert cb is not None
            cb(10, 10)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.strip().startswith("upload a.csv: 100.0% 10/10 B, avg ")
        assert captured.err.strip().endswith("done")

    def test_enabled_reports_failure_and_reraises(self, capsys) -> None:
        formatter = OutputFormatter(json_mode=True)
        with (
            pytest.raises(RuntimeError),
            transfer_progress(formatter, label="dl", total_bytes=None, enabled=True),
        ):
            raise RuntimeError("boom")
        assert capsys.readouterr().err.strip().endswith("failed")


def _store(tmp_path: Path) -> ConfigStore:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    store = ConfigStore(config_dir=config_dir)
    store.save(
        AppConfig(
            projects={
                "test": ProjectConfig(stack_url="https://connection.keboola.com", token=TEST_TOKEN)
            }
        )
    )
    return store


class TestProgressFlagCli:
    def _invoke(self, tmp_path: Path, args: list[str], method: str, result: dict):
        def _transfer(**kwargs):
            on_progress = kwargs["on_progress"]
            assert on_progress is not None
            on_progress(0, 2 * MIB)
            on_progress(MIB, 2 * MIB)
            on_progress(2 * MIB, 2 * MIB)
            return result

        with (
            patch("keboola_agent_cli.cli.ConfigStore", return_value=_store(tmp_path)),
            patch("keboola_agent_cli.cli.StorageService") as svc_cls,
        ):
            getattr(svc_cls.return_value, method).side_effect = _transfer
            return runner.invoke(app, args)

    def test_upload_table_progress_json(self, tmp_path: Path) -> None:
        csv_file = tmp_path / "big.csv"
        csv_file.write_text("id\n1\n")
        out = self._invoke(
            tmp_path,
            _args("upload-table", "--table-id", "in.c-b.t", "--file", str(csv_file), "--progress"),
            "upload_table",
            {"project_alias": "test", "table_id": "in.c-b.t", "incremental": False},
        )
        assert out.exit_code == 0, out.output
        assert json.loads(out.stdout)["data"]["table_id"] == "in.c-b.t"
        assert "upload big.csv: 100.0% 2.00/2.00 MiB" in out.stderr
        assert out.stderr.strip().endswith("done")

    def test_download_table_progress_json(self, tmp_path: Path) -> None:
        out_file = tmp_path / "t.csv"
        out = self._invoke(
            tmp_path,
            _args(
                "download-table", "--table-id", "in.c-b.t", "--output", str(out_file), "--progress"
            ),
            "download_table",
            {"project_alias": "test", "table_id": "in.c-b.t", "file_size_bytes": 2},
        )
        assert out.exit_code == 0, out.output
        assert json.loads(out.stdout)["data"]["file_size_bytes"] == 2
        assert "download in.c-b.t: 100.0% 2.00/2.00 MiB" in out.stderr

    def test_json_without_flag_passes_no_callback(self, tmp_path: Path) -> None:
        with (
            patch("keboola_agent_cli.cli.ConfigStore", return_value=_store(tmp_path)),
            patch("keboola_agent_cli.cli.StorageService") as svc_cls,
        ):
            svc_cls.return_value.download_table.return_value = {"file_size_bytes": 1}
            out = runner.invoke(
                app,
                _args(
                    "download-table", "--table-id", "in.c-b.t", "--output", str(tmp_path / "t.csv")
                ),
            )
        assert out.exit_code == 0, out.output
        assert svc_cls.return_value.download_table.call_args.kwargs["on_progress"] is None
        assert "download in.c-b.t" not in out.stderr


def _client() -> KeboolaClient:
    return KeboolaClient(stack_url="https://connection.keboola.com", token=TEST_TOKEN)


def _assert_monotonic(calls: list[tuple[int, int | None]], total_bytes: int) -> None:
    done = [c[0] for c in calls]
    assert done == sorted(done)
    assert done[0] == 0
    assert done[-1] == total_bytes


def _no_auth(_url: str) -> dict[str, str]:
    return {}


class _PlainDownloader(_CloudDownloader):
    """Real streaming, but slice URLs used verbatim (no cloud credentials)."""

    def resolve_base_url(self, file_detail: dict[str, Any]) -> str:
        return ""

    def resolve_slice_url(self, base_url: str, entry_url: str, file_detail: dict[str, Any]) -> str:
        return entry_url


class TestDownloadProgressClient:
    def test_download_file_reports_network_bytes(self, tmp_path: Path, httpx_mock) -> None:
        body = b"x" * (3 * MIB + 17)
        url = "https://storage.example.com/file.csv?sig=1"
        httpx_mock.add_response(url=url, content=body)
        calls: list[tuple[int, int | None]] = []
        out = tmp_path / "f.csv"
        written = _client().download_file(url, str(out), lambda d, t: calls.append((d, t)))
        assert written == len(body)
        assert out.read_bytes() == body
        _assert_monotonic(calls, len(body))
        assert {t for _d, t in calls} == {len(body)}  # Content-Length is the total

    def test_download_file_counts_compressed_bytes(self, tmp_path: Path, httpx_mock) -> None:
        import gzip

        raw = b"a,b\n" * 200_000
        body = gzip.compress(raw)
        url = "https://storage.example.com/file.csv.gz"
        httpx_mock.add_response(url=url, content=body)
        calls: list[tuple[int, int | None]] = []
        _client().download_file(url, str(tmp_path / "f.csv"), lambda d, t: calls.append((d, t)))
        _assert_monotonic(calls, len(body))
        assert (tmp_path / "f.csv").read_bytes() == raw

    @pytest.mark.parametrize("to_dir", [False, True])
    def test_sliced_download_sums_across_slices(
        self, tmp_path: Path, httpx_mock, to_dir: bool
    ) -> None:
        slices = [b"1" * (MIB + 5), b"2" * 300, b"3" * (2 * MIB)]
        manifest = {
            "entries": [
                {"url": f"https://slices.example.com/part-{i}", "meta": {"content_length": len(s)}}
                for i, s in enumerate(slices)
            ]
        }
        httpx_mock.add_response(
            url="https://storage.example.com/manifest", content=json.dumps(manifest).encode()
        )
        for i, payload in enumerate(slices):
            httpx_mock.add_response(url=f"https://slices.example.com/part-{i}", content=payload)
        downloader = _PlainDownloader("azure", _no_auth)
        detail = {"isSliced": True, "url": "https://storage.example.com/manifest"}
        calls: list[tuple[int, int | None]] = []
        total = sum(len(s) for s in slices)
        with patch(
            "keboola_agent_cli.client._CloudDownloader.create", return_value=downloader
        ) as _create:
            client = _client()
            if to_dir:
                client.download_sliced_file_to_dir(
                    detail, str(tmp_path / "d"), lambda d, t: calls.append((d, t))
                )
            else:
                client.download_sliced_file(
                    detail, str(tmp_path / "f"), lambda d, t: calls.append((d, t))
                )
        assert _create.called
        _assert_monotonic(calls, total)
        assert {t for _d, t in calls} == {total}

    def test_sliced_total_unknown_without_manifest_sizes(self, tmp_path: Path, httpx_mock) -> None:
        manifest = {"entries": [{"url": "https://slices.example.com/p0"}]}
        httpx_mock.add_response(
            url="https://storage.example.com/manifest", content=json.dumps(manifest).encode()
        )
        httpx_mock.add_response(url="https://slices.example.com/p0", content=b"abc")
        downloader = _PlainDownloader("azure", _no_auth)
        calls: list[tuple[int, int | None]] = []
        with patch("keboola_agent_cli.client._CloudDownloader.create", return_value=downloader):
            _client().download_sliced_file(
                {"isSliced": True, "url": "https://storage.example.com/manifest"},
                str(tmp_path / "f"),
                lambda d, t: calls.append((d, t)),
            )
        _assert_monotonic(calls, 3)
        assert {t for _d, t in calls} == {None}
