"""The OAuth workflow doc must tell an agent what the wizard and the command really do.

Two claims in the first version of ``oauth-workflow.md`` were wrong:

- It told the agent to wait until ``authorization.oauth_api.version`` goes
  ``3`` -> ``4``. That field is the OAuth Broker API version: the wizard always
  writes ``3`` (help.keboola.com, "OAuth" in the common interface), so the
  check never passes and an agent reports a failure after a success. The
  configuration ``version`` goes up when the wizard saves the credentials.
- Its model answer said "Opened ... in your browser", but agents run with
  ``--json``, which never opens a browser (``browser_opened: false``).
"""

from __future__ import annotations

from pathlib import Path

SKILL_DIR = Path(__file__).parent.parent / "plugins/kbagent/skills/kbagent"
WORKFLOW = SKILL_DIR / "references/oauth-workflow.md"


def _section(heading: str) -> str:
    """The body of one ``## `` section of the workflow doc."""
    text = WORKFLOW.read_text(encoding="utf-8")
    parts = text.split(f"## {heading}\n", 1)
    assert len(parts) == 2, f"section {heading!r} is gone"
    return parts[1].split("\n## ", 1)[0]


def _verify_step() -> str:
    steps = _section("Steps")
    parts = steps.split("\n3. ", 1)
    assert len(parts) == 2, "the verify step (3.) is gone"
    return parts[1]


def test_verify_step_uses_the_config_version_and_the_credentials_id() -> None:
    verify = _verify_step()
    assert "configuration `version`" in verify
    assert "oauth_api.id" in verify


def test_no_doc_expects_oauth_api_version_to_change() -> None:
    assert "-> `4`" not in WORKFLOW.read_text(encoding="utf-8")
    skill = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "verify `oauth_api.version`" not in skill


def test_model_answer_does_not_claim_an_open() -> None:
    reporting = _section("Reporting the link in an answer")
    assert "browser_opened" in reporting
    assert "Opened the" not in reporting
