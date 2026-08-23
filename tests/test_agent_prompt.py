"""Static regression tests for the keboola-expert subagent prompt.

These are prompt-compliance tests. They do NOT exercise a real LLM;
they verify that the pilot agent prompt, slash command, plugin-level
CLAUDE.md, and plugin.json contain the content required by the design.
If someone trims the prompt and drops a non-negotiable rule or a known
gotcha, these tests fail fast.

Scenario-based behavioral tests against a real LLM are out of scope
for this suite.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
PLUGIN_DIR = REPO_ROOT / "plugins" / "kbagent"
AGENT_FILE = PLUGIN_DIR / "agents" / "keboola-expert.md"
COMMAND_FILE = PLUGIN_DIR / "commands" / "keboola.md"
PLUGIN_CLAUDE_MD = PLUGIN_DIR / ".claude-plugin" / "CLAUDE.md"
PLUGIN_JSON = PLUGIN_DIR / ".claude-plugin" / "plugin.json"

# ~20k tokens ~= 80 kB in typical English markdown (~4 chars/token). The budget
# stays under that so the static prompt never crowds out the task.
#
# History: 60 kB -> 62 000 B in v0.48.0 (the `feature` matrix row); 62 000 B ->
# 70 000 B in v0.88.0. That earlier bump left the file with under 100 bytes of
# headroom, which stopped being a budget and became a tripwire: the next PR to
# touch the prompt paid for an unrelated trim before it could add its own line.
# 70 000 B is a deliberate owner decision to buy that room back, and it still
# sits ~12% under the 80 kB reference point.
#
# It is NOT a licence to grow the file. The standing instruction is unchanged:
# exhaustive per-command detail belongs in `AGENT_CONTEXT` (loaded on demand),
# and the real answer to sustained growth is splitting keboola-expert into
# per-domain specialists, not another bump. Trim before you add.
#
# This is the SINGLE SOURCE OF TRUTH for the budget. Prose elsewhere that
# hardcodes its own figure instead of quoting this one drifts silently:
# CONTRIBUTING.md and kbagent-pr-reviewer.md both said "60 KB" long after
# v0.48.0 moved the ceiling to 62 000 B, and both still said "62 000 B"
# after v0.88.0 moved it again to 70 000 B, until each was caught by hand.
# test_documented_budget_matches_enforced_budget below is the gate that
# keeps them honest going forward.
PROMPT_BYTE_BUDGET = 70_000

# Docs that state the prompt budget in prose. Each must quote it as
# "<PROMPT_BYTE_BUDGET> B" (space-grouped, e.g. "70 000 B") next to the
# word "budget" -- see _BUDGET_MENTION_RE below.
BUDGET_DOC_SITES = [
    "CONTRIBUTING.md",
    "plugins/kbagent/agents/kbagent-pr-reviewer.md",
]

# Matches a budget figure immediately followed by "budget" (optionally
# "prompt budget"), in either the enforced byte form ("70 000 B" / "70000 B")
# or the deprecated kilobyte form ("60 KB"). Grouping punctuation (spaces or
# commas) inside the number is tolerated so "70 000", "70,000" and "70000"
# all match -- only the digits are compared against PROMPT_BYTE_BUDGET.
_BUDGET_MENTION_RE = re.compile(
    r"(?P<number>\d[\d ,]*\d|\d)\s*(?P<unit>KB|B)\s+(?:prompt\s+)?budget"
)


@pytest.fixture(scope="module")
def agent_body() -> str:
    return AGENT_FILE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------
# 1. Agent file exists and has correct frontmatter
# ---------------------------------------------------------------------


class TestPilotAgentFile:
    def test_agent_file_exists(self) -> None:
        assert AGENT_FILE.is_file(), f"Pilot agent file missing at {AGENT_FILE}"

    def test_agent_frontmatter_fields(self, agent_body: str) -> None:
        """Frontmatter must include name, description, tools, model, color."""
        assert agent_body.startswith("---\n"), "Missing YAML frontmatter"
        # Extract frontmatter block (between first two '---' lines)
        end = agent_body.index("\n---\n", 4)
        fm = agent_body[4:end]
        for field in ("name:", "description:", "tools:", "model:", "color:"):
            assert field in fm, f"Frontmatter missing '{field}'"

    def test_agent_name_matches_slash_command_delegation(self, agent_body: str) -> None:
        """The agent's `name:` must match what the /keboola slash command spawns."""
        fm_end = agent_body.index("\n---\n", 4)
        fm = agent_body[4:fm_end]
        name_line = next(line for line in fm.splitlines() if line.startswith("name:"))
        name = name_line.split(":", 1)[1].strip()
        assert name == "keboola-expert"

        cmd = COMMAND_FILE.read_text(encoding="utf-8")
        assert "keboola-expert" in cmd, "Slash command doesn't reference the agent"

    def test_agent_prompt_under_token_budget(self, agent_body: str) -> None:
        size = len(agent_body.encode("utf-8"))
        assert size < PROMPT_BYTE_BUDGET, (
            f"Agent prompt is {size} bytes (~{size // 4} tokens); "
            f"budget is {PROMPT_BYTE_BUDGET} bytes. Trim or split into specialists."
        )

    def test_documented_budget_matches_enforced_budget(self) -> None:
        """Every doc site's budget figure must match PROMPT_BYTE_BUDGET.

        A doc site that hardcodes its own figure instead of quoting this
        module's constant is a silent-drift surface: CONTRIBUTING.md and
        kbagent-pr-reviewer.md both sat on a stale figure for a full release
        span after the constant moved, until someone caught it by hand. The
        expected string is derived from PROMPT_BYTE_BUDGET rather than a
        second hardcoded literal, so a legitimate future budget change (the
        constant and the docs moving together) never fails this test --
        only a doc site left behind does.

        _BUDGET_MENTION_RE also matches the deprecated "KB" form, so a site
        that regresses to kilobytes is caught even though its digits happen
        to equal PROMPT_BYTE_BUDGET's byte figure (which would never
        legitimately occur, but the point is to catch the wrong *unit* too).
        """
        expected_bytes = str(PROMPT_BYTE_BUDGET)
        for relative_path in BUDGET_DOC_SITES:
            text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
            mentions = _BUDGET_MENTION_RE.findall(text)
            assert mentions, (
                f"{relative_path} does not mention the prompt budget in the "
                f"expected '<number> B budget' form. It must quote the "
                f"enforced value (PROMPT_BYTE_BUDGET in {Path(__file__).name}), "
                f"not its own figure."
            )
            for number, unit in mentions:
                digits = re.sub(r"[ ,]", "", number)
                assert unit == "B" and digits == expected_bytes, (
                    f"{relative_path} states the prompt budget as "
                    f"'{number} {unit}', but PROMPT_BYTE_BUDGET is "
                    f"{expected_bytes} B ({Path(__file__).name} is the single "
                    f"source of truth). Update the doc to match."
                )


# ---------------------------------------------------------------------
# 2. Non-negotiable rules are all present (plan §6.1 + §14.8)
# ---------------------------------------------------------------------


NON_NEGOTIABLE_RULES = [
    # rule 1: fresh fetch
    ("FRESH FETCH", "must re-fetch before write"),
    # rule 2: dry-run first
    ("DRY-RUN FIRST", "must dry-run before apply"),
    # rule 3: never chain update + run
    ("NEVER chain `config update` + `job run`", "no implicit job run"),
    # rule 4: the MCP passthrough is gone (removed in v0.85.0)
    ("THERE IS NO MCP PASSTHROUGH", "SKILL.md rule 8"),
    # rule 5: prefer CLI over REST
    ("PREFER CLI OVER REST", "no raw keboola.com REST"),
    # rule 6: version gate
    ("VERSION GATE", "refuse on outdated CLI"),
    # rule 7: always --json
    ("ALWAYS USE `--json`", "parseable output"),
    # rule 8: token discipline
    ("TOKEN DISCIPLINE", "never read config.json for token"),
]


class TestNonNegotiableRules:
    @pytest.mark.parametrize("needle,why", NON_NEGOTIABLE_RULES)
    def test_rule_present(self, agent_body: str, needle: str, why: str) -> None:
        assert needle in agent_body, f"Non-negotiable rule missing ({why}): '{needle}'"


# ---------------------------------------------------------------------
# 3. Inline gotchas from observed failure modes
# ---------------------------------------------------------------------


INLINE_GOTCHAS = [
    # Conditional flows: validate-before-push + INVALID_FLOW_DEFINITION (since 0.57.0)
    ("INVALID_FLOW_DEFINITION", "conditional-flow validation error code"),
    # Snowflake transformation scaffolding (config new --push accepts it)
    ("keboola.snowflake-transformation", "scaffolding a SQL transformation config"),
    # Primary keys on new output tables crash first run (nullable default)
    ("Primary keys on new output tables", "nullable first-run crash"),
    # source vs destination swap in output mappings
    ("`source` vs `destination`", "output mapping swap bug"),
    # Linked buckets scope semantics
    ("Linked buckets", "in.c-X only in source project"),
    # Google Sheets OAuth not exportable
    ("Google Sheets Writer OAuth", "manual re-auth required"),
    # Storage table rename does not exist as a CLI or API primitive
    ("`kbagent storage rename-table`", "does not exist -- no such command"),
    # Synced column_metadata can be empty even when Keboola has metadata
    ("`column_metadata: {}`", "synced file is not authoritative"),
]


class TestInlineGotchas:
    @pytest.mark.parametrize("needle,why", INLINE_GOTCHAS)
    def test_gotcha_present(self, agent_body: str, needle: str, why: str) -> None:
        assert needle in agent_body, (
            f"Inline gotcha missing ({why}): expected to find '{needle}' in agent prompt"
        )


# ---------------------------------------------------------------------
# 4. Tool selection matrix key rows (plan §6.6)
# ---------------------------------------------------------------------


TOOL_MATRIX_ROWS = [
    "kbagent flow validate",
    "conditional flow",
    "kbagent config new --component-id keboola.snowflake-transformation",
    "kbagent job run",
    "kbagent config list",
    "kbagent config search",
    "kbagent sync pull",
    "kbagent sync push",
    "kbagent workspace query",
]


class TestToolSelectionMatrix:
    @pytest.mark.parametrize("command", TOOL_MATRIX_ROWS)
    def test_command_in_matrix(self, agent_body: str, command: str) -> None:
        assert command in agent_body, f"Tool matrix missing reference to: {command}"


# ---------------------------------------------------------------------
# 5. Output contract (plan §14.6 + per-step constraint design)
# ---------------------------------------------------------------------


class TestOutputContract:
    def test_verification_payload_schema_documented(self, agent_body: str) -> None:
        """Must document the verification payload JSON schema for writes."""
        for field in (
            '"status"',
            '"resource"',
            '"diff_summary"',
            '"fresh_fetch_ts"',
            '"dry_run_ts"',
            '"apply_ts"',
            '"post_apply_verification"',
            '"commands_executed"',
            '"next_step"',
        ):
            assert field in agent_body, f"Output contract missing field: {field}"

    def test_refusal_format_documented(self, agent_body: str) -> None:
        assert "## Refusal" in agent_body
        assert "repair_path" in agent_body

    def test_self_check_section_present(self, agent_body: str) -> None:
        assert "ANTI-DRIFT SELF-CHECK" in agent_body


# ---------------------------------------------------------------------
# 6. Slash command
# ---------------------------------------------------------------------


class TestSlashCommand:
    def test_command_file_exists(self) -> None:
        assert COMMAND_FILE.is_file()

    def test_command_frontmatter(self) -> None:
        body = COMMAND_FILE.read_text(encoding="utf-8")
        assert body.startswith("---\n")
        assert "description:" in body
        assert "allowed-tools:" in body
        assert "Task" in body  # must allow Task tool for delegation

    def test_empty_args_produces_guidance(self) -> None:
        body = COMMAND_FILE.read_text(encoding="utf-8")
        assert "$ARGUMENTS" in body
        assert "empty" in body.lower() or "What Keboola task" in body

    def test_dry_run_only_handoff_explicit(self) -> None:
        body = COMMAND_FILE.read_text(encoding="utf-8")
        # Main agent must not auto-apply when subagent returns dry_run_only
        assert "dry_run_only" in body
        assert "Do NOT auto-apply" in body or "do NOT auto-apply" in body


# ---------------------------------------------------------------------
# 7. Plugin-level CLAUDE.md
# ---------------------------------------------------------------------


class TestPluginClaudeMd:
    def test_plugin_claude_md_exists(self) -> None:
        assert PLUGIN_CLAUDE_MD.is_file()

    def test_delegation_strategy_documented(self) -> None:
        body = PLUGIN_CLAUDE_MD.read_text(encoding="utf-8")
        assert "keboola-expert" in body
        assert "Task(" in body or "Task tool" in body
        assert "/keboola" in body


# ---------------------------------------------------------------------
# 8. Observed-failure-mode coverage
# ---------------------------------------------------------------------


class TestObservedFailureModesCovered:
    """The prompt must cover failure modes seen in past internal sessions."""

    def test_flow_file_full_replace_warned(self, agent_body: str) -> None:
        # --file is full-replace; omitting fields silently drops them.
        assert "--file" in agent_body
        assert "full replace" in agent_body.lower() or "full-replace" in agent_body.lower()

    def test_column_metadata_staleness_addressed(self, agent_body: str) -> None:
        # Agent must not trust a synced file's `column_metadata: {}` for
        # write-path decisions -- the sync may not have fetched metadata.
        assert "synced" in agent_body.lower()
        assert "column_metadata" in agent_body

    def test_storage_retype_path_present(self, agent_body: str) -> None:
        # The manual retype pattern must be sketched so the agent does
        # not fall back to REST when rethinking column types.
        assert "retype" in agent_body.lower() or "Retype table" in agent_body
