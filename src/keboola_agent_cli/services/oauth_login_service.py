"""Business logic for `kbagent project login` (browser OAuth + PKCE).

Orchestrates the interactive login: loopback callback server, browser
hand-off, code exchange, Storage-token minting, token verification, and
config persistence. The protocol-level pieces live in ``oauth.py``; the
silent refresh of an already-logged-in project happens in
``BaseService.resolve_projects()`` via ``oauth.ensure_fresh_oauth_token``.
"""

import logging
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..constants import (
    OAUTH_CALLBACK_PORTS,
    OAUTH_LOGIN_TIMEOUT_SECONDS,
    OAUTH_SAPI_TOKEN_LIFETIME_SECONDS,
)
from ..errors import ConfigError, mask_token
from ..models import OAuthCredentials, ProjectConfig, normalize_stack_url
from ..oauth import (
    OAuthCallbackServer,
    build_authorize_url,
    exchange_code,
    generate_pkce_pair,
    generate_state,
    mint_storage_token,
    resolve_oauth_client_id,
)
from .base import BaseService
from .org_service import slugify

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoginOutcome:
    """Result of a completed browser login."""

    alias: str
    project_name: str
    project_id: int | None
    stack_url: str
    masked_token: str
    token_expires_at: float
    re_authenticated: bool

    def as_dict(self) -> dict[str, Any]:
        """Shape consumed by OutputFormatter (JSON mode emits this verbatim)."""
        return {
            "alias": self.alias,
            "project_name": self.project_name,
            "project_id": self.project_id,
            "stack_url": self.stack_url,
            "token": self.masked_token,
            "token_expires_at": self.token_expires_at,
            "re_authenticated": self.re_authenticated,
            "auth_type": "oauth",
        }


class OAuthLoginService(BaseService):
    """Browser-based OAuth login for Keboola projects.

    Inherits config_store + client_factory injection from BaseService;
    the client_factory is used to verify the freshly minted Storage token
    and read the project identity (name/id/org) for persistence.
    """

    def login(
        self,
        stack_url: str,
        *,
        alias: str | None = None,
        port: int | None = None,
        open_browser: bool = True,
        timeout: float = OAUTH_LOGIN_TIMEOUT_SECONDS,
        on_authorize_url: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Run the full Authorization Code + PKCE login flow.

        Blocks until the user completes (or abandons) the browser login.

        Args:
            stack_url: Keboola stack URL (bare host / base URL / deep-link
                are all accepted, same forgiveness as `project add`).
            alias: Alias to register the project under. Defaults to the
                slugified project name reported by the stack. Re-login into
                a project already registered for this stack UPDATES that
                entry instead of creating a duplicate.
            port: Explicit loopback callback port. Defaults to the first
                free port from OAUTH_CALLBACK_PORTS (all of which must be
                whitelisted on the registered OAuth client).
            open_browser: When False, skip launching the browser -- the
                user opens the authorize URL manually (SSH/headless-with-
                local-browser scenarios).
            timeout: Seconds to wait for the browser redirect.
            on_authorize_url: Callback invoked with the authorize URL right
                before waiting, so the command layer can display it.

        Returns:
            ``LoginOutcome.as_dict()`` payload.

        Raises:
            KeboolaApiError: OAuth/token-mint/verification failures
                (ErrorCode.OAUTH_ERROR for protocol failures).
            ConfigError: Alias collision with a different project.
        """
        stack_url = normalize_stack_url(stack_url)
        client_id = resolve_oauth_client_id()
        pkce = generate_pkce_pair()
        state = generate_state()
        candidate_ports = (port,) if port is not None else OAUTH_CALLBACK_PORTS

        with OAuthCallbackServer(candidate_ports) as server:
            authorize_url = build_authorize_url(
                stack_url,
                client_id=client_id,
                redirect_uri=server.redirect_uri,
                state=state,
                code_challenge=pkce.challenge,
            )
            if on_authorize_url is not None:
                on_authorize_url(authorize_url)
            if open_browser:
                # Best-effort: a False return (no browser available) is fine,
                # the URL was already printed via on_authorize_url.
                webbrowser.open(authorize_url)
            code = server.wait_for_code(state, timeout)
            redirect_uri = server.redirect_uri

        tokens = exchange_code(
            stack_url,
            client_id=client_id,
            code=code,
            code_verifier=pkce.verifier,
            redirect_uri=redirect_uri,
        )
        sapi_token = mint_storage_token(stack_url, access_token=tokens.access_token)
        token_expires_at = time.time() + OAUTH_SAPI_TOKEN_LIFETIME_SECONDS

        # Verify the minted token and learn which project the user selected
        # on the Connection consent screen.
        client = self._client_factory(stack_url, sapi_token)
        try:
            token_info = client.verify_token()
        finally:
            client.close()

        credentials = OAuthCredentials(
            client_id=client_id,
            refresh_token=tokens.refresh_token,
            token_expires_at=token_expires_at,
        )
        outcome = self._persist(
            alias=alias,
            stack_url=stack_url,
            sapi_token=sapi_token,
            credentials=credentials,
            token_info=token_info,
        )
        return outcome.as_dict()

    def _persist(
        self,
        *,
        alias: str | None,
        stack_url: str,
        sapi_token: str,
        credentials: OAuthCredentials,
        token_info: Any,
    ) -> LoginOutcome:
        """Save (or update on re-login) the project entry.

        Idempotency: logging into a project that is already registered for
        the same stack updates that entry in place (rotated refresh token,
        fresh minted token) -- a second login must never error with
        "alias already exists" or create a duplicate.
        """
        config = self._config_store.load()

        # Re-login detection: same stack + same project id -> update in place.
        existing_alias = next(
            (
                existing
                for existing, proj in config.projects.items()
                if not proj.ephemeral
                and proj.stack_url == stack_url
                and proj.project_id is not None
                and proj.project_id == token_info.project_id
            ),
            None,
        )

        project = ProjectConfig(
            stack_url=stack_url,
            token=sapi_token,
            project_name=token_info.project_name,
            project_id=token_info.project_id,
            org_id=token_info.org_id,
            org_name=token_info.org_name,
            oauth=credentials,
        )

        if existing_alias is not None and (alias is None or alias == existing_alias):
            # Preserve fields login does not own (e.g. active_branch_id).
            project.active_branch_id = config.projects[existing_alias].active_branch_id
            self._config_store.edit_project(
                existing_alias,
                token=sapi_token,
                oauth=credentials,
                project_name=token_info.project_name,
                project_id=token_info.project_id,
                org_id=token_info.org_id,
                org_name=token_info.org_name,
            )
            chosen_alias = existing_alias
            re_authenticated = True
        else:
            chosen_alias = alias or slugify(token_info.project_name)
            collision = config.projects.get(chosen_alias)
            if collision is not None and (
                collision.stack_url != stack_url or collision.project_id != token_info.project_id
            ):
                raise ConfigError(
                    f"Alias '{chosen_alias}' already points to a different project "
                    f"({collision.project_name or collision.stack_url}). Pass "
                    "--project <other-alias> to register this login separately."
                )
            if collision is not None:
                self._config_store.edit_project(chosen_alias, token=sapi_token, oauth=credentials)
                re_authenticated = True
            else:
                self._config_store.add_project(chosen_alias, project)
                re_authenticated = False

        logger.debug(
            "OAuth login persisted for project '%s' (id=%s, token=%s)",
            chosen_alias,
            token_info.project_id,
            mask_token(sapi_token),
        )
        return LoginOutcome(
            alias=chosen_alias,
            project_name=token_info.project_name,
            project_id=token_info.project_id,
            stack_url=stack_url,
            masked_token=mask_token(sapi_token),
            token_expires_at=credentials.token_expires_at or 0.0,
            re_authenticated=re_authenticated,
        )
