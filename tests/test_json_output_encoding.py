"""Tests for UTF-8 machine output on non-UTF-8 consoles (issue #546).

``kbagent --json <anything>`` crashed on Windows whenever the payload carried a
non-ASCII character -- an arrow in a flow name, an accented config name, an
emoji -- because ``sys.stdout`` inherited the console codepage (cp1250 on
Czech/Polish/Hungarian Windows 11) and ``sys.stdout.write`` raised
``UnicodeEncodeError``. ``--json`` exists for machine consumption, so it must
not depend on which terminal happens to be attached.

Contrast with issue #522 (``tests/test_serve_banner_encoding.py``): there the
un-encodable text was a decorative banner and the right answer was to
transliterate to ASCII. Here it is *data*, so the fix forces UTF-8 instead --
first by reconfiguring the stream, then by writing UTF-8 bytes to its binary
buffer for streams that cannot be reconfigured.
"""

from __future__ import annotations

import io
import json

import pytest

from keboola_agent_cli.output import (
    OutputFormatter,
    _stdout_is_utf8,
    force_utf8_stdout,
    write_machine_output,
)

# The character from the issue report: an arrow inside a flow name. cp1250
# (like every other legacy single-byte console codepage) cannot encode it.
ARROW = "→"


class _Cp1250Stdout:
    """A stdout stand-in that behaves like a real cp1250 Windows console.

    Writing a character cp1250 cannot represent raises ``UnicodeEncodeError``,
    exactly as the Windows console does. Exposes a ``buffer`` (every real
    ``TextIOWrapper`` has one) but no ``reconfigure``, so it exercises the
    byte-level fallback path.
    """

    encoding = "cp1250"

    def __init__(self) -> None:
        self._chunks: list[str] = []
        self.buffer = io.BytesIO()
        self.flush_calls = 0

    def write(self, s: str) -> int:
        s.encode(self.encoding)  # raises UnicodeEncodeError on non-cp1250 chars
        self._chunks.append(s)
        return len(s)

    def flush(self) -> None:
        self.flush_calls += 1

    def isatty(self) -> bool:
        return False

    def written_text(self) -> str:
        """Everything that reached the stream, whichever layer carried it."""
        return "".join(self._chunks) + self.buffer.getvalue().decode("utf-8")


class _ReconfigurableCp1250Stdout(_Cp1250Stdout):
    """cp1250 console that supports ``reconfigure`` -- the real Windows case.

    After the fix flips it to UTF-8 the text layer encodes everything itself and
    the binary buffer is never touched.
    """

    def reconfigure(self, *, encoding: str) -> None:
        self.encoding = encoding


class _UnreconfigurableStdout(_Cp1250Stdout):
    """Stream whose ``reconfigure`` fails (detached / exotic stream)."""

    def reconfigure(self, *, encoding: str) -> None:
        raise ValueError("underlying buffer has been detached")


class _NoBufferStdout:
    """cp1250 stream with neither ``reconfigure`` nor ``buffer``."""

    encoding = "cp1250"

    def write(self, s: str) -> int:
        s.encode(self.encoding)
        return len(s)

    def flush(self) -> None:  # pragma: no cover - trivial
        pass


class TestFakeConsoleFidelity:
    """Guard the guard: prove the fakes reproduce the crash, so the passing
    tests below are meaningful and not vacuous."""

    def test_naive_write_of_arrow_crashes(self) -> None:
        fake = _Cp1250Stdout()
        with pytest.raises(UnicodeEncodeError):
            fake.write(f'{{"name": "extract {ARROW} load"}}\n')

    def test_plain_ascii_write_is_fine(self) -> None:
        fake = _Cp1250Stdout()
        fake.write('{"name": "extract -> load"}\n')
        assert "extract" in fake.written_text()


class TestStdoutIsUtf8:
    @pytest.mark.parametrize("encoding", ["utf-8", "UTF-8", "utf8", "UTF_8", "utf-8-sig"])
    def test_utf8_aliases_detected(self, encoding: str, monkeypatch: pytest.MonkeyPatch) -> None:
        stream = _Cp1250Stdout()
        stream.encoding = encoding
        monkeypatch.setattr("sys.stdout", stream)
        assert _stdout_is_utf8() is True

    @pytest.mark.parametrize("encoding", ["cp1250", "cp437", "latin-1", "ascii"])
    def test_legacy_codepages_rejected(
        self, encoding: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stream = _Cp1250Stdout()
        stream.encoding = encoding
        monkeypatch.setattr("sys.stdout", stream)
        assert _stdout_is_utf8() is False

    def test_missing_encoding_attribute_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdout", io.StringIO())  # no .encoding
        assert _stdout_is_utf8() is False


class TestForceUtf8Stdout:
    def test_reconfigures_legacy_codepage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stream = _ReconfigurableCp1250Stdout()
        monkeypatch.setattr("sys.stdout", stream)
        force_utf8_stdout()
        assert stream.encoding == "utf-8"

    def test_utf8_stream_left_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stream = _ReconfigurableCp1250Stdout()
        stream.encoding = "utf-8"
        monkeypatch.setattr("sys.stdout", stream)
        force_utf8_stdout()
        assert stream.encoding == "utf-8"

    def test_stream_without_reconfigure_is_tolerated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stream = _Cp1250Stdout()
        monkeypatch.setattr("sys.stdout", stream)
        force_utf8_stdout()  # must not raise
        assert stream.encoding == "cp1250"

    def test_failing_reconfigure_is_tolerated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stream = _UnreconfigurableStdout()
        monkeypatch.setattr("sys.stdout", stream)
        force_utf8_stdout()  # must not raise
        assert stream.encoding == "cp1250"


class TestWriteMachineOutput:
    def test_reconfigured_stream_writes_through_text_layer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stream = _ReconfigurableCp1250Stdout()
        monkeypatch.setattr("sys.stdout", stream)

        payload = f'{{"name": "extract {ARROW} load"}}\n'
        write_machine_output(payload)

        assert stream.written_text() == payload
        assert stream.buffer.getvalue() == b""  # binary fallback not needed

    def test_unreconfigurable_stream_falls_back_to_utf8_bytes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stream = _Cp1250Stdout()
        monkeypatch.setattr("sys.stdout", stream)

        payload = f'{{"name": "extract {ARROW} load"}}\n'
        write_machine_output(payload)

        assert stream.buffer.getvalue() == payload.encode("utf-8")
        assert stream.written_text() == payload

    def test_ascii_payload_needs_no_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stream = _Cp1250Stdout()
        monkeypatch.setattr("sys.stdout", stream)

        write_machine_output('{"name": "plain"}\n')

        assert stream.buffer.getvalue() == b""
        assert stream.written_text() == '{"name": "plain"}\n'

    def test_stream_without_binary_layer_surfaces_the_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Nothing can encode the text here; a silent drop or a mangled payload
        # would be worse than the original error for a machine consumer.
        monkeypatch.setattr("sys.stdout", _NoBufferStdout())
        with pytest.raises(UnicodeEncodeError):
            write_machine_output(f'{{"name": "{ARROW}"}}\n')


class TestJsonModeOnLegacyConsole:
    """End-to-end regression for issue #546 through ``OutputFormatter``."""

    def test_output_survives_non_ascii(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stream = _Cp1250Stdout()
        monkeypatch.setattr("sys.stdout", stream)

        formatter = OutputFormatter(json_mode=True, no_color=True)
        formatter.output({"flows": [{"name": f"extract {ARROW} load"}]})

        parsed = json.loads(stream.written_text())
        assert parsed["status"] == "ok"
        assert parsed["data"]["flows"][0]["name"] == f"extract {ARROW} load"

    def test_error_survives_non_ascii(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stream = _Cp1250Stdout()
        monkeypatch.setattr("sys.stdout", stream)

        formatter = OutputFormatter(json_mode=True, no_color=True)
        formatter.error(f"flow 'extract {ARROW} load' not found", error_code="NOT_FOUND")

        parsed = json.loads(stream.written_text())
        assert parsed["status"] == "error"
        assert ARROW in parsed["error"]["message"]

    def test_success_survives_non_ascii(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stream = _Cp1250Stdout()
        monkeypatch.setattr("sys.stdout", stream)

        formatter = OutputFormatter(json_mode=True, no_color=True)
        formatter.success(f"created flow 'extract {ARROW} load'")

        parsed = json.loads(stream.written_text())
        assert ARROW in parsed["data"]["message"]

    def test_utf8_console_output_is_byte_identical(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Modern terminals must be completely unaffected: same text, no escapes.
        stream = _ReconfigurableCp1250Stdout()
        stream.encoding = "utf-8"
        monkeypatch.setattr("sys.stdout", stream)

        formatter = OutputFormatter(json_mode=True, no_color=True)
        formatter.output({"name": f"extract {ARROW} load"})

        assert ARROW in stream.written_text()
        assert "\\u2192" not in stream.written_text()
