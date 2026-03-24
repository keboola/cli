"""Tests for the interactive REPL module."""

from unittest.mock import MagicMock

import typer.main

from keboola_agent_cli.commands.repl import KbagentCompleter, _build_command_tree


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
