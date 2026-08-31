"""Documentation Q&A service — ask the Keboola docs natural-language questions.

Bridges the CLI to the AI Service /docs/question endpoint. Resolves the
target project (stack URL + token) from the config store the same way
ComponentService does, then queries the AI Service and normalizes the
response into the CLI's snake_case output contract.
"""

import logging
from typing import Any

from ..config_store import ConfigStore
from ..models import DocsAnswer
from .base import BaseService, ClientFactory
from .component_service import AiClientFactory, default_ai_client_factory

logger = logging.getLogger(__name__)


class DocsService(BaseService):
    """Business logic for the `kbagent docs` command group.

    Uses AiServiceClient (via injected factory) to answer natural-language
    questions grounded in the official Keboola documentation.
    """

    def __init__(
        self,
        config_store: ConfigStore,
        client_factory: ClientFactory | None = None,
        ai_client_factory: AiClientFactory | None = None,
    ) -> None:
        super().__init__(config_store, client_factory)
        self._ai_client_factory = ai_client_factory or default_ai_client_factory

    def ask_docs(self, alias: str | None, query: str) -> dict[str, Any]:
        """Ask the Keboola documentation a natural language question.

        Args:
            alias: Project alias used to derive the stack URL and token.
                None resolves via the shared default-project cascade
                (KBAGENT_PROJECT env > ``project use`` pin > sole project).
            query: Natural language question about the Keboola platform.

        Returns:
            Dict with keys:
                - "query": the question as asked
                - "text": Markdown answer text
                - "source_urls": list of documentation URLs the answer
                  is grounded in

        Raises:
            ConfigError: If the alias is unknown, or no default project can
                be resolved.
            KeboolaApiError: If the AI Service call fails.
        """
        resolved_alias, _source = self.resolve_pinned_alias(explicit=alias)
        project = self.resolve_projects([resolved_alias])[resolved_alias]

        ai_client = self._ai_client_factory(project.stack_url, project.token)
        try:
            raw = ai_client.docs_question(query)
        finally:
            ai_client.close()

        answer = DocsAnswer(**raw)
        return {
            "query": query,
            "text": answer.text,
            "source_urls": answer.source_urls,
        }
