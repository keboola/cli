"""Static compliance tests for the kbagent skill frontmatter.

Claude Desktop (and the Agent Skills spec) enforce a hard limit of 1024
characters on the ``description`` field in SKILL.md frontmatter; a longer
description makes the whole skill fail to load with
"field 'description' in SKILL.md must be at most 1024 characters"
(issue #447). These tests keep the frontmatter within every consumer's
limits so the plugin stays loadable in Claude Desktop, Claude Code, and
claude.ai alike.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent
SKILL_MD = REPO_ROOT / "plugins" / "kbagent" / "skills" / "kbagent" / "SKILL.md"

# Hard limit from the Agent Skills spec, enforced by Claude Desktop at
# skill-load time (issue #447).
DESCRIPTION_CHAR_LIMIT = 1024


@pytest.fixture(scope="module")
def frontmatter() -> dict:
    text = SKILL_MD.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match is not None, "SKILL.md is missing YAML frontmatter"
    data = yaml.safe_load(match.group(1))
    assert isinstance(data, dict), "SKILL.md frontmatter must be a YAML mapping"
    return data


class TestSkillFrontmatter:
    def test_skill_file_exists(self) -> None:
        assert SKILL_MD.is_file(), f"SKILL.md missing at {SKILL_MD}"

    def test_name_matches_directory(self, frontmatter: dict) -> None:
        assert frontmatter.get("name") == "kbagent"

    def test_description_present(self, frontmatter: dict) -> None:
        description = frontmatter.get("description", "")
        assert description.strip(), "SKILL.md description must not be empty"

    def test_description_within_claude_desktop_limit(self, frontmatter: dict) -> None:
        description = frontmatter["description"]
        assert len(description) <= DESCRIPTION_CHAR_LIMIT, (
            f"SKILL.md description is {len(description)} characters; Claude "
            f"Desktop rejects skills whose description exceeds "
            f"{DESCRIPTION_CHAR_LIMIT} characters (issue #447). Trim it."
        )
