"""Keboola Scheduler Service API client with retry, timeouts, and token masking.

This module communicates with the Keboola Scheduler Service, which registers
cron triggers for ``keboola.scheduler`` Storage configurations. Writing the
Storage config alone is not enough -- the service must be told to (re)load it
via ``POST /schedules`` before the cron trigger fires. Derives the Scheduler
Service URL from the Storage API stack URL by replacing 'connection.' with
'scheduler.' in the hostname.

Inherits shared retry/error logic from BaseHttpClient.
"""

import logging
from typing import Any
from urllib.parse import quote

from .http_base import BaseHttpClient

logger = logging.getLogger(__name__)


class SchedulerClient(BaseHttpClient):
    """HTTP client for the Keboola Scheduler Service API.

    Provides schedule activation (register/refresh a ``keboola.scheduler``
    config with the service) and removal of the service-side registration,
    with built-in retry logic (exponential backoff for 429/5xx), timeouts,
    and automatic token masking in error messages.

    Inherits _do_request() and _raise_api_error() from BaseHttpClient.
    """

    def __init__(self, stack_url: str, token: str) -> None:
        self._stack_url = stack_url.rstrip("/")
        scheduler_base_url = self._derive_service_url(self._stack_url, "scheduler")
        headers = {
            "X-StorageApi-Token": token,
        }
        super().__init__(
            base_url=scheduler_base_url,
            token=token,
            headers=headers,
        )

    def __enter__(self) -> "SchedulerClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def activate_schedule(self, configuration_id: str) -> dict[str, Any]:
        """Register (or refresh) a schedule with the Scheduler Service.

        The service loads the referenced ``keboola.scheduler`` Storage
        configuration and applies its current state -- an ``enabled`` config
        gets a live cron trigger, a ``disabled`` one is deregistered. Call
        this after every create/update of the config; the operation is
        idempotent.

        Args:
            configuration_id: ID of the ``keboola.scheduler`` Storage
                configuration (not the target flow ID).

        Returns:
            Dict with the registered schedule as returned by the service.

        Raises:
            KeboolaApiError: On API errors (403 when the token lacks the
                schedule-activation privilege, etc.).
        """
        payload = {"configurationId": configuration_id}
        response = self._do_request("POST", "/schedules", json=payload)
        return response.json()

    def remove_schedule(self, configuration_id: str) -> None:
        """Deregister a schedule from the Scheduler Service.

        Removes the service-side registration for the given
        ``keboola.scheduler`` Storage configuration so its cron trigger stops
        firing. The Storage configuration itself is untouched.

        Args:
            configuration_id: ID of the ``keboola.scheduler`` Storage
                configuration.

        Raises:
            KeboolaApiError: On API errors (404 when the service has no
                registration for this configuration, etc.).
        """
        encoded_id = quote(configuration_id, safe="")
        self._do_request("DELETE", f"/configurations/{encoded_id}")
