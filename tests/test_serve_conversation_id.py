"""Tests for the conversation-ID auto-generation in ``kbagent serve``.

Without ``KBAGENT_CONVERSATION_ID`` set, every API call out of kbagent
skips the ``X-Conversation-ID`` header and ``kbagent doctor`` warns about
it. The serve command now generates a per-session ID (or reuses one from
the env) and exports it so child processes inherit it; the doctor check
then passes for the lifetime of the serve session.
"""

from __future__ import annotations

import re

from keboola_agent_cli.commands.serve import _default_conversation_id
from keboola_agent_cli.constants import ENV_CONVERSATION_ID


class TestDefaultConversationId:
    def test_generates_fresh_id_when_env_unset(self, monkeypatch) -> None:
        monkeypatch.delenv(ENV_CONVERSATION_ID, raising=False)
        cid = _default_conversation_id()
        # ``serve-<UTC stamp 16 chars>-<8 hex>`` -- pin the shape so future
        # refactors don't accidentally drop the prefix observability uses.
        assert re.fullmatch(r"serve-\d{8}T\d{6}Z-[0-9a-f]{8}", cid), cid

    def test_each_fresh_call_is_unique(self, monkeypatch) -> None:
        monkeypatch.delenv(ENV_CONVERSATION_ID, raising=False)
        a = _default_conversation_id()
        b = _default_conversation_id()
        # Even in the same second the hex suffix disambiguates -- two
        # back-to-back kbagent serve restarts must not share a conversation.
        assert a != b

    def test_reuses_preset_env_var(self, monkeypatch) -> None:
        """A caller (CI, supervisor script, debugging session that restarts
        the serve process) can pre-set the var to keep a stable session
        ID across restarts. The function must defer to it rather than
        overwriting with a fresh value.
        """
        monkeypatch.setenv(ENV_CONVERSATION_ID, "supervisor-conv-42")
        assert _default_conversation_id() == "supervisor-conv-42"

    def test_empty_env_falls_back_to_generated(self, monkeypatch) -> None:
        """An empty-string env var (common when shells export but do not
        assign) must NOT be treated as "preset"; we still generate.
        """
        monkeypatch.setenv(ENV_CONVERSATION_ID, "   ")
        cid = _default_conversation_id()
        assert cid.startswith("serve-")
        assert cid != "   "
