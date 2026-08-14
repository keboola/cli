"""Tests for changelog summarisation and rendering.

Covers two new surfaces:

* ``changelog.headline`` -- first-sentence extraction with version-number and
  abbreviation guards, max-char truncation, and dangling-backtick cleanup.
* ``commands/changelog`` -- default one-line summary vs ``--full``, the
  ``(+N more)`` indicator, the footer hint, and BREAKING prefix styling.

Renderer tests drive ``_format_changelog_human`` with a synthetic entries dict
(not the live ``CHANGELOG``) so they stay green as real release notes change.
"""

from __future__ import annotations

import io
import json

from rich.console import Console
from typer.testing import CliRunner

from keboola_agent_cli.changelog import CHANGELOG, format_whats_new, headline
from keboola_agent_cli.cli import app
from keboola_agent_cli.commands.changelog import (
    _PREFIX_RE,
    _format_changelog_human,
    _styled_note,
)
from keboola_agent_cli.constants import CHANGELOG_HEADLINE_MAX_CHARS, ENV_SKIP_UPDATE


def _render(entries: dict[str, list[str]], *, full: bool) -> str:
    """Render entries to plain text through a captured Rich console."""
    buf = io.StringIO()
    console = Console(file=buf, width=80, no_color=True)
    _format_changelog_human(console, {"entries": entries}, full=full)
    return buf.getvalue()


# A multi-version fixture: 1.2.0 has three notes (so a summary hides two),
# 1.1.0 has a single short one (so a summary hides nothing).
_ENTRIES: dict[str, list[str]] = {
    "1.2.0": [
        "New: alpha thing. Detail about alpha that stays hidden.",
        "Fix: beta thing.",
        "Internal: gamma thing.",
    ],
    "1.1.0": ["Change: single short note."],
}


class TestHeadline:
    def test_first_sentence_only(self) -> None:
        note = "New: a thing happened. And then a second thing. And a third."
        assert headline(note) == "New: a thing happened."

    def test_short_note_returned_verbatim(self) -> None:
        assert headline("Fix: small thing.") == "Fix: small thing."

    def test_version_number_period_is_not_a_boundary(self) -> None:
        note = "Bumped to 0.57.0 for everyone. Details follow."
        assert headline(note) == "Bumped to 0.57.0 for everyone."

    def test_abbreviation_period_is_not_a_boundary(self) -> None:
        note = "Holds a dimension (e.g. a Chart of Accounts) as one record. More."
        assert headline(note) == "Holds a dimension (e.g. a Chart of Accounts) as one record."

    def test_digit_guard_is_period_only(self) -> None:
        # The digit guard targets version-number periods only -- a digit before
        # "!" or "?" is a genuine sentence end and must still split.
        assert headline("Exit code 5! Details follow.") == "Exit code 5!"
        assert headline("Ready in v2? Yes, fully ready.") == "Ready in v2?"

    def test_long_first_sentence_truncated_on_word_boundary(self) -> None:
        note = "Word " * 100  # no terminator, far over the cap
        out = headline(note)
        assert out.endswith("…")
        # cap + " …" (space + ellipsis) is the worst case
        assert len(out) <= CHANGELOG_HEADLINE_MAX_CHARS + 2
        assert not out.endswith("Wor …")  # cut on a whole word, not mid-token

    def test_dangling_backtick_dropped_on_truncation(self) -> None:
        # Opening backtick whose closer lands in the dropped tail.
        note = "uses `unterminated then a bunch of normal words follow afterwards ok"
        out = headline(note, max_chars=40)
        assert "`" not in out
        assert out.endswith("…")


class TestRendererSummary:
    def test_shows_headline_and_more_count(self) -> None:
        out = _render(_ENTRIES, full=False)
        assert "New: alpha thing." in out
        assert "(+2 more)" in out  # 3 notes -> 2 hidden
        assert "Detail about alpha" not in out  # detail hidden
        assert "Fix: beta thing." not in out  # sibling notes hidden
        assert "--full" in out  # footer hint present

    def test_single_short_note_has_no_more_and_no_footer(self) -> None:
        out = _render({"1.1.0": ["Change: single short note."]}, full=False)
        assert "(+" not in out
        assert "--full" not in out  # nothing hidden -> no hint

    def test_truncated_single_note_triggers_footer(self) -> None:
        long_note = "Change: " + "word " * 100  # one note, but truncated
        out = _render({"1.0.0": [long_note]}, full=False)
        assert "(+" not in out  # still only one note
        assert "--full" in out  # but detail was hidden -> hint


class TestRendererFull:
    def test_shows_every_note(self) -> None:
        out = _render(_ENTRIES, full=True)
        assert "Detail about alpha that stays hidden." in out
        assert "Fix: beta thing." in out
        assert "Internal: gamma thing." in out
        assert "(+2 more)" not in out
        assert "--full" not in out  # no footer hint in full mode


class TestBreakingPrefix:
    def test_prefix_regex_matches_breaking_with_decoration(self) -> None:
        m = _PREFIX_RE.match("BREAKING (flow): orchestrator dropped.")
        assert m is not None
        assert m.group(1).lower() == "breaking"

    def test_breaking_prefix_styled_red(self) -> None:
        text = _styled_note("BREAKING: a thing changed.")
        assert any("red" in str(span.style) for span in text.spans)


class TestFormatWhatsNew:
    def test_uses_headline_not_full_blob(self, monkeypatch) -> None:
        from keboola_agent_cli import changelog as cl

        long_tail = "x" * 500
        monkeypatch.setattr(
            cl, "CHANGELOG", {"9.9.9": [f"New: short headline. Long detail {long_tail}"]}
        )
        out = format_whats_new("9.9.8", "9.9.9")
        assert "What's new in v9.9.9" in out
        assert "New: short headline." in out
        assert long_tail not in out  # detail is summarised away


class TestChangelogCliIntegration:
    """End-to-end against the live CHANGELOG -- assertions stay data-agnostic."""

    def test_summary_is_shorter_than_full(self, monkeypatch) -> None:
        monkeypatch.setenv(ENV_SKIP_UPDATE, "1")
        runner = CliRunner()
        summary = runner.invoke(app, ["changelog", "-n", "3"])
        full = runner.invoke(app, ["changelog", "-n", "3", "--full"])
        assert summary.exit_code == 0, summary.output
        assert full.exit_code == 0, full.output
        assert len(summary.output) < len(full.output)

    def test_v_is_an_alias_for_full(self, monkeypatch) -> None:
        monkeypatch.setenv(ENV_SKIP_UPDATE, "1")
        runner = CliRunner()
        short_flag = runner.invoke(app, ["changelog", "-n", "2", "-v"])
        long_flag = runner.invoke(app, ["changelog", "-n", "2", "--full"])
        assert short_flag.output == long_flag.output

    def test_json_payload_has_no_full_key(self, monkeypatch) -> None:
        monkeypatch.setenv(ENV_SKIP_UPDATE, "1")
        result = CliRunner().invoke(app, ["--json", "changelog", "-n", "1"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert list(data.keys()) == ["entries"]


class TestLiveChangelogHeadlines:
    """The real ``CHANGELOG``, unlike the renderer tests above.

    Those drive synthetic entries on purpose, so they stay green as release
    notes change. This one is deliberately the opposite: it checks the
    authoring contract the module docstring states -- every note must lead with
    a self-contained first sentence, because that sentence is what
    ``kbagent changelog`` and the post-update "What's new" banner show.

    A first sentence over the cap is not merely shortened; the cut lands
    wherever the character budget runs out, which is typically mid-clause and
    before the point of the change. Two 0.84.0 notes shipped to main that way
    -- "... from a SOURCE project (dev) to a …" -- and nothing failed, because
    every other check treats a note as an opaque string.

    Scope is the newest version only -- the one being written right now, whose
    notes are still editable. Roughly 40% of the historical entries are cut the
    same way; rewriting already-published release notes to satisfy a test is
    not worth it, so this guards the entries an author can still fix.
    """

    def test_newest_release_notes_are_not_truncated(self) -> None:
        version = next(iter(CHANGELOG))
        truncated = [
            headline(note) for note in CHANGELOG[version] if headline(note).rstrip().endswith("…")
        ]

        assert not truncated, (
            f"These v{version} notes' first sentence exceeds "
            f"{CHANGELOG_HEADLINE_MAX_CHARS} chars, so `kbagent changelog` and the "
            '"What\'s new" banner show it cut off mid-clause. Lead with a short, '
            "self-contained sentence and move the detail into later sentences:\n  "
            + "\n  ".join(truncated)
        )
