"""Kai (Keboola AI Assistant) service — bridge between CLI and cloud Kai API.

Provides sync wrappers around the async kai-client library, with feature
detection (agent-chat flag) and project resolution via BaseService.
"""

import asyncio
import logging
from typing import Any

from kai_client import KaiClient, KaiError

from ..auth.sentinel import require_static_token
from ..constants import KAI_FEATURE_FLAG, KAI_REQUEST_TIMEOUT, KAI_STREAM_TIMEOUT
from ..errors import ConfigError, ErrorCode, KeboolaApiError
from .base import BaseService

logger = logging.getLogger(__name__)


class KaiService(BaseService):
    """Business logic for Kai AI Assistant integration.

    All public methods are synchronous — they wrap the async KaiClient
    via asyncio.run() so Typer commands can call them directly.
    """

    # ------------------------------------------------------------------
    # Project resolution
    # ------------------------------------------------------------------

    def resolve_alias(self, alias: str | None) -> str:
        """Resolve a project alias, falling back to the default project.

        Args:
            alias: Explicit alias, or None for default.

        Returns:
            Resolved alias string.

        Raises:
            ConfigError: If no projects configured or alias not found.
        """
        if alias:
            # Validate it exists
            self.resolve_projects([alias])
            return alias
        # Fall back to default (first project)
        projects = self.resolve_projects()
        if not projects:
            raise ConfigError("No projects configured. Run 'kbagent project add' first.")
        return next(iter(projects))

    # ------------------------------------------------------------------
    # Feature detection
    # ------------------------------------------------------------------

    def _check_kai_enabled(self, alias: str) -> None:
        """Raise KeboolaApiError if Kai is not enabled for the project.

        Calls verify_token to check owner.features for the agent-chat flag.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        client = self._client_factory(project.stack_url, project.token)
        try:
            token_info = client.verify_token()
        finally:
            client.close()

        if KAI_FEATURE_FLAG not in token_info.features:
            raise KeboolaApiError(
                message=(
                    f"Kai is not enabled for project '{alias}'. "
                    "Enable the 'AI Agent Chat' feature in project settings and use "
                    "the project's master Storage API token (the auto-generated "
                    "'owner' token, not a custom one) — custom tokens cannot access Kai."
                ),
                status_code=0,
                error_code=ErrorCode.KAI_NOT_ENABLED,
            )

    # ------------------------------------------------------------------
    # Async helpers
    # ------------------------------------------------------------------

    async def _create_kai_client(self, alias: str) -> KaiClient:
        """Create a KaiClient with auto-discovered URL for the given project."""
        projects = self.resolve_projects([alias])
        project = projects[alias]
        require_static_token(project.token, feature="kbagent kai")
        return await KaiClient.from_storage_api(
            storage_api_token=project.token,
            storage_api_url=project.stack_url,
            timeout=KAI_REQUEST_TIMEOUT,
            stream_timeout=KAI_STREAM_TIMEOUT,
        )

    # ------------------------------------------------------------------
    # Public methods (sync wrappers)
    # ------------------------------------------------------------------

    def ping(self, alias: str) -> dict[str, Any]:
        """Check Kai server health for a project.

        Returns:
            Dict with timestamp and server info.
        """
        self._check_kai_enabled(alias)

        async def _ping() -> dict[str, Any]:
            client = await self._create_kai_client(alias)
            async with client:
                ping_resp = await client.ping()
                info_resp = await client.info()

            return {
                "project_alias": alias,
                "timestamp": ping_resp.timestamp.isoformat(),
                "app_name": info_resp.app_name,
                "app_version": info_resp.app_version,
                "server_version": info_resp.server_version,
                "mcp_status": (
                    info_resp.connected_mcp.get("status", "unknown")
                    if isinstance(info_resp.connected_mcp, dict)
                    else "unknown"
                ),
            }

        try:
            return asyncio.run(_ping())
        except KaiError as exc:
            raise KeboolaApiError(
                message=f"Kai ping failed: {exc.message}",
                status_code=0,
                error_code=ErrorCode.KAI_ERROR,
            ) from exc

    def ask(self, alias: str, message: str) -> dict[str, Any]:
        """Send a one-shot question to Kai and collect the full text response.

        Args:
            alias: Project alias.
            message: The question to ask.

        Returns:
            Dict with chat_id and response text.
        """
        self._check_kai_enabled(alias)

        async def _ask() -> dict[str, Any]:
            client = await self._create_kai_client(alias)
            async with client:
                chat_id, response_text = await client.chat(message)

            return {
                "project_alias": alias,
                "chat_id": chat_id,
                "response": response_text,
            }

        try:
            return asyncio.run(_ask())
        except KaiError as exc:
            raise KeboolaApiError(
                message=f"Kai ask failed: {exc.message}",
                status_code=0,
                error_code=ErrorCode.KAI_ERROR,
            ) from exc

    def chat_message(self, alias: str, message: str, chat_id: str | None = None) -> dict[str, Any]:
        """Send a message in a chat session and collect the response.

        Args:
            alias: Project alias.
            message: The message to send.
            chat_id: Optional existing chat ID to continue.

        Returns:
            Dict with chat_id and response text.
        """
        self._check_kai_enabled(alias)

        async def _chat() -> dict[str, Any]:
            client = await self._create_kai_client(alias)
            async with client:
                cid = chat_id or client.new_chat_id()
                response_parts: list[str] = []
                async for event in client.send_message(cid, message):
                    if event.type == "text":
                        text = getattr(event, "text", None)
                        if text is not None:
                            response_parts.append(text)

                return {
                    "project_alias": alias,
                    "chat_id": cid,
                    "response": "".join(response_parts),
                }

        try:
            return asyncio.run(_chat())
        except KaiError as exc:
            raise KeboolaApiError(
                message=f"Kai chat failed: {exc.message}",
                status_code=0,
                error_code=ErrorCode.KAI_ERROR,
            ) from exc

    def preflight(self, alias: str) -> dict[str, Any]:
        """Inspect the configured token for Kai readiness without raising.

        Unlike ``ping``/``ask``/``chat`` which call ``_check_kai_enabled`` and
        raise ``KAI_NOT_ENABLED`` when the agent-chat feature flag is missing,
        this method returns a structured payload describing token state so the
        UI can render a single, informative warning instead of an error
        cascade. Detects both required conditions:

        - ``isMasterToken == True`` on /v2/storage/tokens/verify (the
          auto-generated 'owner' token; custom tokens cannot access Kai)
        - ``agent-chat`` in ``owner.features``

        Returns:
            Dict with keys: ``project_alias``, ``ok`` (bool, both conditions
            true), ``is_master_token``, ``has_agent_chat_feature``,
            ``token_description``, ``project_id``, ``project_name``, and
            ``error`` (None on success, string describing the failure).
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        result: dict[str, Any] = {
            "project_alias": alias,
            "ok": False,
            "is_master_token": False,
            "has_agent_chat_feature": False,
            "token_description": None,
            "project_id": None,
            "project_name": None,
            "error": None,
        }
        client = self._client_factory(project.stack_url, project.token)
        try:
            raw = client.get_project_info()
        except KeboolaApiError as exc:
            result["error"] = exc.message
            return result
        finally:
            client.close()

        owner = raw.get("owner", {}) or {}
        features = owner.get("features", []) or []
        result["is_master_token"] = bool(raw.get("isMasterToken", False))
        result["has_agent_chat_feature"] = KAI_FEATURE_FLAG in features
        result["token_description"] = raw.get("description")
        result["project_id"] = owner.get("id")
        result["project_name"] = owner.get("name")
        result["ok"] = result["is_master_token"] and result["has_agent_chat_feature"]
        if not result["ok"]:
            reasons: list[str] = []
            if not result["is_master_token"]:
                reasons.append("the configured token is not the project's master ('owner') token")
            if not result["has_agent_chat_feature"]:
                reasons.append("the project lacks the 'AI Agent Chat' feature flag")
            result["error"] = "; ".join(reasons)
        return result

    def get_chat_detail(self, alias: str, chat_id: str) -> dict[str, Any]:
        """Fetch full message history for a single chat.

        Args:
            alias: Project alias.
            chat_id: Kai chat ID (UUID).

        Returns:
            Dict with chat_id, title, created_at and a flat list of messages.
            Each message is ``{"role": str, "content": str}`` where ``content``
            is the concatenated text of all ``text`` parts (tool calls and
            other non-text parts are skipped — they are an implementation
            detail of Kai's streaming protocol, not user-facing content).
        """
        self._check_kai_enabled(alias)

        async def _detail() -> dict[str, Any]:
            client = await self._create_kai_client(alias)
            async with client:
                chat_detail = await client.get_chat(chat_id)

            messages: list[dict[str, Any]] = []
            for msg in chat_detail.messages:
                parts = msg.parts or []
                text_segments = [
                    p.get("text", "")
                    for p in parts
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                content = "".join(text_segments)
                if not content and msg.role == "user":
                    # User messages occasionally arrive with text wrapped in a
                    # different shape (legacy chats). Surface the raw parts
                    # JSON dump as a fallback so the user at least sees
                    # something — better than a silent empty bubble.
                    continue
                messages.append(
                    {
                        "id": msg.id,
                        "role": msg.role,
                        "content": content,
                        "created_at": msg.created_at.isoformat() if msg.created_at else None,
                    }
                )

            return {
                "project_alias": alias,
                "chat_id": chat_detail.id,
                "title": chat_detail.title,
                "created_at": chat_detail.created_at.isoformat()
                if chat_detail.created_at
                else None,
                "messages": messages,
            }

        try:
            return asyncio.run(_detail())
        except KaiError as exc:
            raise KeboolaApiError(
                message=f"Kai chat detail failed: {exc.message}",
                status_code=0,
                error_code=ErrorCode.KAI_ERROR,
            ) from exc

    def get_history(self, alias: str, limit: int = 10) -> dict[str, Any]:
        """Get chat history for the current user.

        Args:
            alias: Project alias.
            limit: Max number of chats to return.

        Returns:
            Dict with list of chat summaries.
        """
        self._check_kai_enabled(alias)

        async def _history() -> dict[str, Any]:
            client = await self._create_kai_client(alias)
            async with client:
                history = await client.get_history(limit=limit)

            return {
                "project_alias": alias,
                "chats": [
                    {
                        "id": chat.id,
                        "title": chat.title or "(untitled)",
                        "created_at": chat.created_at.isoformat() if chat.created_at else None,
                        "visibility": chat.visibility,
                    }
                    for chat in history.chats
                ],
                "has_more": history.has_more,
            }

        try:
            return asyncio.run(_history())
        except KaiError as exc:
            raise KeboolaApiError(
                message=f"Kai history failed: {exc.message}",
                status_code=0,
                error_code=ErrorCode.KAI_ERROR,
            ) from exc
