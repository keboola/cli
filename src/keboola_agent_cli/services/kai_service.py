"""Kai (Keboola AI Assistant) service — bridge between CLI and cloud Kai API.

Provides sync wrappers around the async kai-client library, with feature
detection (agent-chat flag) and project resolution via BaseService.
"""

import asyncio
import logging
from typing import Any

from kai_client import KaiClient, KaiError

from ..constants import KAI_FEATURE_FLAG, KAI_REQUEST_TIMEOUT, KAI_STREAM_TIMEOUT
from ..errors import ConfigError, KeboolaApiError
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
                    "Enable the 'AI Agent Chat' feature in project settings."
                ),
                status_code=0,
                error_code="KAI_NOT_ENABLED",
            )

    # ------------------------------------------------------------------
    # Async helpers
    # ------------------------------------------------------------------

    async def _create_kai_client(self, alias: str) -> KaiClient:
        """Create a KaiClient with auto-discovered URL for the given project."""
        projects = self.resolve_projects([alias])
        project = projects[alias]
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
                error_code="KAI_ERROR",
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
                error_code="KAI_ERROR",
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
                        response_parts.append(event.text)  # type: ignore[attr-defined]

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
                error_code="KAI_ERROR",
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
                error_code="KAI_ERROR",
            ) from exc
