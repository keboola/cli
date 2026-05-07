"""Tests for the interactive REPL module."""

import os
import stat
from pathlib import Path
from unittest.mock import MagicMock

import platformdirs
import pytest
import typer.main

from keboola_agent_cli.commands.repl import (
    KbagentCompleter,
    _build_command_tree,
    _get_history_path,
)


class TestBuildCommandTree:
    """Tests for command tree introspection."""

    def test_builds_tree_from_app(self) -> None:
        """_build_command_tree produces entries for known commands."""
        from keboola_agent_cli.cli import app

        click_app = typer.main.get_command(app)
        tree = _build_command_tree(click_app)

        # Should contain top-level groups and their subcommands
        assert "project list" in tree
        assert "config list" in tree
        assert "job list" in tree
        assert "branch create" in tree
        assert "workspace create" in tree

    def test_tree_contains_help_text(self) -> None:
        """Command tree entries have non-empty help text."""
        from keboola_agent_cli.cli import app

        click_app = typer.main.get_command(app)
        tree = _build_command_tree(click_app)

        # At least some commands should have help text
        non_empty = [v for v in tree.values() if v]
        assert len(non_empty) > 10

    def test_tree_includes_top_level_commands(self) -> None:
        """Top-level commands like 'doctor', 'context' are in the tree."""
        from keboola_agent_cli.cli import app

        click_app = typer.main.get_command(app)
        tree = _build_command_tree(click_app)

        assert "doctor" in tree
        assert "context" in tree
        assert "version" in tree
        assert "repl" in tree


class TestKbagentCompleter:
    """Tests for the REPL tab completer."""

    def test_completes_partial_input(self) -> None:
        """Completer returns matching commands for partial input."""
        tree = {
            "project list": "List projects",
            "project add": "Add a project",
            "config list": "List configs",
        }
        completer = KbagentCompleter(tree)

        doc = MagicMock()
        doc.text_before_cursor = "project"
        completions = list(completer.get_completions(doc, None))

        # Should match both project commands
        assert len(completions) == 2
        texts = {c.text for c in completions}
        # Completions are suffixes: " add" and " list" after "project"
        assert " add" in texts
        assert " list" in texts

    def test_completes_empty_input(self) -> None:
        """Completer returns all commands for empty input."""
        tree = {"project list": "List", "config list": "List"}
        completer = KbagentCompleter(tree)

        doc = MagicMock()
        doc.text_before_cursor = ""
        completions = list(completer.get_completions(doc, None))

        assert len(completions) == 2

    def test_no_matches_for_unknown(self) -> None:
        """Completer returns nothing for unrecognized input."""
        tree = {"project list": "List"}
        completer = KbagentCompleter(tree)

        doc = MagicMock()
        doc.text_before_cursor = "unknown"
        completions = list(completer.get_completions(doc, None))

        assert len(completions) == 0


class TestReplCli:
    """Tests for REPL via CLI runner."""

    def test_repl_command_with_json_mode(self) -> None:
        """REPL in --json mode outputs a message instead of entering interactive mode."""
        from typer.testing import CliRunner

        from keboola_agent_cli.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["--json", "repl"])

        assert result.exit_code == 0
        assert "REPL mode is for interactive use" in result.output

    def test_no_args_non_tty_shows_help(self) -> None:
        """Running kbagent without args in non-TTY mode shows help."""
        from typer.testing import CliRunner

        from keboola_agent_cli.cli import app

        runner = CliRunner()
        result = runner.invoke(app, [])

        assert result.exit_code == 0
        assert "Commands" in result.output or "kbagent" in result.output


class TestReplFirewallPropagation:
    """REPL must forward --deny-writes / --deny-destructive to every inner command.

    Dropping the flags when re-invoking inside the REPL would silently
    restore write access for a user who started the session with a firewall.
    """

    def test_deny_writes_appended_to_inner_argv(self, monkeypatch) -> None:
        """When deny_writes=True, the REPL rebuilds argv with --deny-writes."""
        from keboola_agent_cli.commands import repl as repl_module

        captured: list[list[str]] = []

        class _FakeClickApp:
            def __call__(self, argv, standalone_mode=False):
                captured.append(list(argv))
                raise SystemExit(0)

        monkeypatch.setattr(repl_module.typer.main, "get_command", lambda _app: _FakeClickApp())

        class _FakeSession:
            def __init__(self, **_kwargs):
                self._replies = iter(["project list", ""])

            def prompt(self, _text):
                try:
                    return next(self._replies)
                except StopIteration as exc:
                    raise EOFError from exc

        monkeypatch.setattr(repl_module, "PromptSession", _FakeSession)

        repl_module._run_repl(
            json_mode=True,
            verbose=False,
            no_color=True,
            config_dir=None,
            deny_writes=True,
            deny_destructive=False,
        )

        # Every captured invocation must carry --deny-writes in the rebuilt argv.
        assert captured, "REPL did not dispatch any commands"
        for argv in captured:
            assert "--deny-writes" in argv, (
                f"REPL dropped --deny-writes when rebuilding argv: {argv}"
            )

    def test_deny_destructive_appended_to_inner_argv(self, monkeypatch) -> None:
        """When deny_destructive=True, the REPL rebuilds argv with --deny-destructive."""
        from keboola_agent_cli.commands import repl as repl_module

        captured: list[list[str]] = []

        class _FakeClickApp:
            def __call__(self, argv, standalone_mode=False):
                captured.append(list(argv))
                raise SystemExit(0)

        monkeypatch.setattr(repl_module.typer.main, "get_command", lambda _app: _FakeClickApp())

        class _FakeSession:
            def __init__(self, **_kwargs):
                self._replies = iter(["storage delete-table --project p --table-id t"])

            def prompt(self, _text):
                try:
                    return next(self._replies)
                except StopIteration as exc:
                    raise EOFError from exc

        monkeypatch.setattr(repl_module, "PromptSession", _FakeSession)

        repl_module._run_repl(
            json_mode=False,
            verbose=False,
            no_color=True,
            config_dir=None,
            deny_writes=False,
            deny_destructive=True,
        )

        assert captured
        for argv in captured:
            assert "--deny-destructive" in argv, (
                f"REPL dropped --deny-destructive when rebuilding argv: {argv}"
            )

    def test_no_duplicate_when_user_retypes_flag(self, monkeypatch) -> None:
        """If user types the flag inside the REPL, we must not double it."""
        from keboola_agent_cli.commands import repl as repl_module

        captured: list[list[str]] = []

        class _FakeClickApp:
            def __call__(self, argv, standalone_mode=False):
                captured.append(list(argv))
                raise SystemExit(0)

        monkeypatch.setattr(repl_module.typer.main, "get_command", lambda _app: _FakeClickApp())

        class _FakeSession:
            def __init__(self, **_kwargs):
                self._replies = iter(["--deny-writes project list"])

            def prompt(self, _text):
                try:
                    return next(self._replies)
                except StopIteration as exc:
                    raise EOFError from exc

        monkeypatch.setattr(repl_module, "PromptSession", _FakeSession)

        repl_module._run_repl(
            json_mode=False,
            verbose=False,
            no_color=True,
            config_dir=None,
            deny_writes=True,
            deny_destructive=False,
        )

        assert captured
        argv = captured[0]
        assert argv.count("--deny-writes") == 1, (
            f"REPL duplicated --deny-writes when user also typed it: {argv}"
        )


class TestHistoryPathPermissions:
    """Issue #269 sec-04 -- REPL history file must be 0600 to avoid token leaks."""

    def test_creates_history_file_with_0600(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fresh history file is created with mode 0600."""
        monkeypatch.setattr(platformdirs, "user_config_dir", lambda *_a, **_kw: str(tmp_path))
        history = _get_history_path()
        assert history.exists()
        mode = stat.S_IMODE(history.stat().st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    def test_tightens_permissive_existing_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pre-existing world-readable history file is chmod'd to 0600."""
        monkeypatch.setattr(platformdirs, "user_config_dir", lambda *_a, **_kw: str(tmp_path))
        history = tmp_path / "repl_history"
        history.write_text("legacy entry\n")
        os.chmod(history, 0o644)
        assert stat.S_IMODE(history.stat().st_mode) == 0o644

        result = _get_history_path()
        assert result == history
        mode = stat.S_IMODE(history.stat().st_mode)
        assert mode == 0o600
