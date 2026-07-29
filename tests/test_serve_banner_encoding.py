"""Tests for the encoding-safe startup banner in ``kbagent serve`` (issue #522).

On Windows with a non-UTF-8 console codepage (cp1250 on Czech/Polish/Hungarian
locales, and any legacy single-byte encoding) the box-drawing glyphs ``├─`` /
``└─`` in the startup banner cannot be encoded. ``sys.stdout.write`` then raises
``UnicodeEncodeError`` -- and because that happens *before* ``uvicorn.run``, the
server never binds the port. The banner is cosmetic, so it must degrade to ASCII
instead of crashing startup.

These tests cover the two helpers that implement the fix:

* ``_encode_safe(text, encoding)`` -- pure transliteration decision.
* ``_write_banner(text)`` -- the I/O wrapper with a belt-and-braces fallback.

plus an AST-based regression guard that keeps the ASCII fallback table in sync
with whatever glyphs the live banner literals actually use.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from keboola_agent_cli.commands import serve
from keboola_agent_cli.commands.serve import (
    _BANNER_ASCII_FALLBACK,
    _encode_safe,
    _write_banner,
)

# The exact glyphs the banner uses; cp1250 (and every other legacy single-byte
# console codepage) cannot encode any of them.
_BOX_GLYPHS = ("├", "└", "─")


class _Cp1250Stdout:
    """A stdout stand-in that behaves like a real cp1250 Windows console.

    Writing a character cp1250 cannot represent raises ``UnicodeEncodeError``,
    exactly as the Windows console does -- so a test that drives ``_write_banner``
    through this object reproduces issue #522 faithfully rather than approximating
    it.
    """

    encoding = "cp1250"

    def __init__(self) -> None:
        self._chunks: list[str] = []

    def write(self, s: str) -> int:
        s.encode(self.encoding)  # raises UnicodeEncodeError on ├ └ ─
        self._chunks.append(s)
        return len(s)

    def flush(self) -> None:  # pragma: no cover - trivial
        pass

    def getvalue(self) -> str:
        return "".join(self._chunks)


class TestFakeConsoleFidelity:
    """Guard the guard: prove the fake actually reproduces the crash, so the
    passing ``_write_banner`` test below is meaningful and not vacuous."""

    def test_naive_write_of_glyphs_crashes(self) -> None:
        fake = _Cp1250Stdout()
        with pytest.raises(UnicodeEncodeError):
            fake.write("  ├─ open: http://x\n")

    def test_plain_ascii_write_is_fine(self) -> None:
        fake = _Cp1250Stdout()
        fake.write("  |- open: http://x\n")
        assert "open" in fake.getvalue()


class TestEncodeSafe:
    def test_utf8_keeps_unicode_glyphs(self) -> None:
        banner = "  ├─ open\n  └─ token\n"
        # A UTF-8 console can render the glyphs, so nothing is transliterated.
        assert _encode_safe(banner, "utf-8") == banner

    def test_cp1250_transliterates_to_ascii(self) -> None:
        out = _encode_safe("  ├─ open\n  └─ token\n", "cp1250")
        assert out == "  |- open\n  `- token\n"
        for glyph in _BOX_GLYPHS:
            assert glyph not in out

    def test_none_encoding_defaults_to_utf8(self) -> None:
        # An exotic stream can report ``encoding is None``; we default to UTF-8
        # (the modern norm) and keep the glyphs rather than needlessly degrading.
        banner = "  ├─ open\n"
        assert _encode_safe(banner, None) == banner

    def test_unknown_codec_degrades_to_ascii(self) -> None:
        # A bogus/unknown codec name raises LookupError from str.encode; that
        # must degrade to ASCII, not propagate.
        out = _encode_safe("  ├─ open\n", "definitely-not-a-codec")
        assert out == "  |- open\n"

    def test_ascii_only_text_is_unchanged_on_cp1250(self) -> None:
        text = "  kbagent serve\n  token: abc123\n"
        assert _encode_safe(text, "cp1250") == text

    @pytest.mark.parametrize("encoding", ["cp1250", "cp1252", "latin-1", "ascii"])
    def test_result_is_always_encodable_on_legacy_consoles(self, encoding: str) -> None:
        # Property: whatever legacy single-byte console we target, the result
        # of _encode_safe must round-trip cleanly (no UnicodeEncodeError).
        banner = "  ├─ open\n  ├─ docs\n  └─ token\n"
        _encode_safe(banner, encoding).encode(encoding)  # must not raise


class TestWriteBanner:
    def test_does_not_crash_on_cp1250_and_emits_ascii(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _Cp1250Stdout()
        monkeypatch.setattr(sys, "stdout", fake)
        _write_banner("  ├─ open: http://127.0.0.1:8001/\n  └─ token: secret\n")
        out = fake.getvalue()
        assert "|- open" in out
        assert "`- token" in out
        for glyph in _BOX_GLYPHS:
            assert glyph not in out

    def test_preserves_unicode_on_utf8_console(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A real UTF-8 console keeps the glyphs verbatim -- the fix must not
        # degrade output on the common (modern) case. (io.StringIO can't stand
        # in here: it's a slotted C type with no settable ``encoding`` attr.)
        class _Utf8Stdout:
            encoding = "utf-8"

            def __init__(self) -> None:
                self._chunks: list[str] = []

            def write(self, s: str) -> int:
                s.encode(self.encoding)  # utf-8 encodes everything -> never raises
                self._chunks.append(s)
                return len(s)

            def flush(self) -> None:
                pass

            def getvalue(self) -> str:
                return "".join(self._chunks)

        buf = _Utf8Stdout()
        monkeypatch.setattr(sys, "stdout", buf)
        _write_banner("  ├─ open\n  └─ token\n")
        assert buf.getvalue() == "  ├─ open\n  └─ token\n"

    def test_belt_and_braces_survives_liar_encoding(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Pathological stream: claims UTF-8 (so _encode_safe keeps the glyphs)
        # but its write() still rejects them. The try/except must catch the
        # UnicodeEncodeError and re-emit lossily rather than crash startup.
        class _LiarStdout:
            encoding = "utf-8"

            def __init__(self) -> None:
                self.calls = 0
                self.buf: list[str] = []

            def write(self, s: str) -> int:
                self.calls += 1
                if self.calls == 1:
                    # First (optimistic) write rejects the glyphs.
                    raise UnicodeEncodeError("cp1250", s, 0, 1, "mock")
                self.buf.append(s)
                return len(s)

            def flush(self) -> None:
                pass

        liar = _LiarStdout()
        monkeypatch.setattr(sys, "stdout", liar)
        _write_banner("  ├─ open\n")  # must not raise
        assert liar.calls == 2  # optimistic write, then lossy fallback write


def _static_banner_text(node: ast.AST) -> str:
    """Flatten a str/f-string literal AST node to its static text.

    ``banner = ("...\n" f"  ├─ {x}\n" ...)`` parses to a ``Constant`` (pure
    string) or ``JoinedStr`` (f-string). We keep the literal ``Constant`` parts
    (which carry the box glyphs) and drop the ``{expr}`` interpolations, which
    are runtime values with no bearing on the fallback table.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            # FormattedValue ({expr}) contributes only runtime data -> skip.
        return "".join(parts)
    return ""


def _live_banner_literals() -> list[str]:
    """Extract every ``banner = (...)`` literal from serve_command's source.

    Parsing the real module (not a copy pasted into the test) means this guard
    tracks whatever glyphs the shipping banner actually uses -- add a new one
    without extending _BANNER_ASCII_FALLBACK and the round-trip below fails.
    """
    src = Path(serve.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "serve_command"
    )
    literals = [
        _static_banner_text(node.value)
        for node in ast.walk(fn)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "banner" for t in node.targets)
    ]
    return [text for text in literals if text]


class TestLiveBannerRegression:
    """The fallback table must cover every glyph the real banner emits."""

    def test_both_banners_were_found(self) -> None:
        # Sanity: serve_command builds two banners (UI and API-only). If a
        # refactor renames the variable, catch it here rather than silently
        # testing nothing.
        literals = _live_banner_literals()
        assert len(literals) == 2, f"expected 2 banner literals, found {len(literals)}"

    @pytest.mark.parametrize("encoding", ["cp1250", "cp1252", "latin-1"])
    def test_live_banners_survive_legacy_consoles(self, encoding: str) -> None:
        for banner in _live_banner_literals():
            safe = _encode_safe(banner, encoding)
            safe.encode(encoding)  # must not raise -> banner can't crash serve

    def test_every_non_ascii_glyph_is_in_fallback_table(self) -> None:
        # Directly assert the invariant: any non-ASCII character appearing in a
        # live banner literal has an ASCII mapping. This is the actionable
        # failure ("add glyph X to _BANNER_ASCII_FALLBACK") for a future editor.
        # str.maketrans keys the table by code point (int), not by character,
        # so membership is tested with ord(ch).
        for banner in _live_banner_literals():
            for ch in banner:
                if ord(ch) > 127:
                    assert ord(ch) in _BANNER_ASCII_FALLBACK, (
                        f"banner glyph {ch!r} (U+{ord(ch):04X}) has no ASCII "
                        f"fallback; add it to _BANNER_ASCII_FALLBACK in serve.py"
                    )
