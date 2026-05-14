"""Hint definitions for Kai (Keboola AI Assistant) commands (ping, ask, chat, history)."""

from .. import HintRegistry
from ..models import ClientCall, CommandHint, HintStep, ServiceCall

# ── kai ping ──────────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="kai.ping",
        description="Check Kai server health and MCP connection status",
        steps=[
            HintStep(
                comment="Verify token and check Kai feature flag",
                client=ClientCall(
                    method="verify_token",
                    args={},
                    result_var="token_info",
                    result_hint="TokenInfo",
                ),
                service=ServiceCall(
                    service_class="KaiService",
                    service_module="kai_service",
                    method="ping",
                    args={"alias": "{project}"},
                ),
            ),
        ],
        notes=[
            "Kai commands use KaiClient from the 'kai_client' package, not KeboolaClient.",
            "KaiClient.from_storage_api() auto-discovers the Kai API URL from the stack URL.",
            "The service checks the 'agent-chat' feature flag before calling Kai.",
            "Client hint shows verify_token (feature detection); actual ping uses KaiClient.",
        ],
    )
)

# ── kai ask ───────────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="kai.ask",
        description="Ask Kai a one-shot question about your project",
        steps=[
            HintStep(
                comment="Send a one-shot question to Kai and collect the full response",
                client=ClientCall(
                    method="verify_token",
                    args={},
                    result_var="token_info",
                    result_hint="TokenInfo",
                ),
                service=ServiceCall(
                    service_class="KaiService",
                    service_module="kai_service",
                    method="ask",
                    args={
                        "alias": "{project}",
                        "message": "{message}",
                    },
                ),
            ),
        ],
        notes=[
            "Kai commands use KaiClient from the 'kai_client' package, not KeboolaClient.",
            "KaiClient.chat(message) sends a question and returns (chat_id, response_text).",
            "Service returns {'project_alias', 'chat_id', 'response'}.",
        ],
    )
)

# ── kai chat ──────────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="kai.chat",
        description="Send a message in a Kai chat session (new or continued)",
        steps=[
            HintStep(
                comment="Send a chat message to Kai, optionally continuing an existing session",
                client=ClientCall(
                    method="verify_token",
                    args={},
                    result_var="token_info",
                    result_hint="TokenInfo",
                ),
                service=ServiceCall(
                    service_class="KaiService",
                    service_module="kai_service",
                    method="chat_message",
                    args={
                        "alias": "{project}",
                        "message": "{message}",
                        "chat_id": "{chat_id}",
                    },
                ),
            ),
        ],
        notes=[
            "Kai commands use KaiClient from the 'kai_client' package, not KeboolaClient.",
            "Without --chat-id, starts a new chat session.",
            "With --chat-id, continues an existing conversation.",
            "KaiClient.send_message() returns an async stream of events; service collects text events.",
            "Service returns {'project_alias', 'chat_id', 'response'}.",
        ],
    )
)

# ── kai preflight ─────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="kai.preflight",
        description=(
            "Inspect whether the configured token can use Kai "
            "(master token + AI Agent Chat feature)"
        ),
        steps=[
            HintStep(
                comment=(
                    "Inspect /v2/storage/tokens/verify for isMasterToken and the "
                    "agent-chat feature flag, without raising on failure"
                ),
                client=ClientCall(
                    method="get_project_info",
                    args={},
                    result_var="info",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="KaiService",
                    service_module="kai_service",
                    method="preflight",
                    args={"alias": "{project}"},
                ),
            ),
        ],
        notes=[
            "Unlike ping/ask/chat, preflight NEVER raises KAI_NOT_ENABLED — it "
            "returns a structured payload so callers can render their own UI.",
            "Returns {'ok', 'is_master_token', 'has_agent_chat_feature', "
            "'token_description', 'project_id', 'project_name', 'error'}.",
            "Kai requires the project's master ('owner') Storage API token; "
            "custom tokens fail the is_master_token check.",
        ],
    )
)

# ── kai chat-detail ───────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="kai.chat-detail",
        description="Fetch the full message history of a single Kai chat",
        steps=[
            HintStep(
                comment=(
                    "Fetch the full transcript of one chat (used to restore a "
                    "conversation when continuing it with `kai chat --chat-id`)"
                ),
                client=ClientCall(
                    method="verify_token",
                    args={},
                    result_var="token_info",
                    result_hint="TokenInfo",
                ),
                service=ServiceCall(
                    service_class="KaiService",
                    service_module="kai_service",
                    method="get_chat_detail",
                    args={
                        "alias": "{project}",
                        "chat_id": "{chat_id}",
                    },
                ),
            ),
        ],
        notes=[
            "Kai commands use KaiClient from the 'kai_client' package, not KeboolaClient.",
            "KaiClient.get_chat(chat_id) returns a ChatDetail with full message list.",
            "Service flattens parts[] → plain {role, content} records; tool calls and "
            "other non-text parts are skipped (Kai streaming protocol internals).",
            "Service returns {'project_alias', 'chat_id', 'title', 'created_at', 'messages'}.",
        ],
    )
)

# ── kai history ───────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="kai.history",
        description="List recent Kai chat sessions",
        steps=[
            HintStep(
                comment="Get chat history for the current user",
                client=ClientCall(
                    method="verify_token",
                    args={},
                    result_var="token_info",
                    result_hint="TokenInfo",
                ),
                service=ServiceCall(
                    service_class="KaiService",
                    service_module="kai_service",
                    method="get_history",
                    args={
                        "alias": "{project}",
                        "limit": "{limit}",
                    },
                ),
            ),
        ],
        notes=[
            "Kai commands use KaiClient from the 'kai_client' package, not KeboolaClient.",
            "Service returns {'project_alias', 'chats': [...], 'has_more': bool}.",
            "Each chat has: id, title, created_at, visibility.",
        ],
    )
)
