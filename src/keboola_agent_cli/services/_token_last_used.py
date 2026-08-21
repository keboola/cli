"""Deriving a Storage token's last-used timestamp from its event feed (issue #622).

The Storage API's token payloads carry no ``lastUsed`` field -- only the Manage
API's PAT response does -- so "is this token still in use?" has to be derived
from ``GET /v2/storage/tokens/{id}/events``. That derivation is shared by the
CLI (``TokenService.list_tokens``) and the importable SDK
(``keboola_agent_cli.Client.list_tokens``), so it lives here rather than in
either of them.

The feed itself is already narrowed to actions the token *performed* by
``KeboolaClient.list_token_events``; what is left for this module is the part
the API genuinely cannot answer on its own -- telling a token that was never
used apart from one whose history has simply aged out of retention.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any, Protocol

from ..constants import TOKEN_EVENTS_RETENTION_DAYS

logger = logging.getLogger(__name__)

# Ordering of `lastUsedStatus` when sorting dormant-first. Reading order is
# cleanup order, so states you can act on come first: "never" is a proven
# mis-provisioning, "unknown"/"error" are unresolved and want a human's eye, and
# actually-used tokens sort last among themselves, longest-idle leading.
_STATUS_RANK: dict[str, int] = {"never": 0, "unknown": 1, "error": 2, "used": 3}


class SupportsTokenEvents(Protocol):
    """The one client method this module needs (keeps it testable and decoupled)."""

    def list_token_events(self, token_id: str, *, limit: int = 1) -> list[dict[str, Any]]: ...


def parse_api_timestamp(value: Any) -> datetime | None:
    """Parse a Storage API timestamp (ISO 8601, ``+HHMM`` offset) or give up."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        logger.debug("Unparseable API timestamp: %r", value)
        return None


def derive_last_used(events: list[dict[str, Any]], created: Any) -> dict[str, Any]:
    """Derive ``lastUsed`` / ``lastUsedEvent`` / ``lastUsedStatus`` for one token.

    The feed sorts newest-first, so when it has anything at all its first entry
    is the answer.

    An empty feed is the interesting case, because it carries two meanings the
    API cannot separate: never used, or unused for longer than events are kept.
    ``created`` breaks the tie -- a token minted INSIDE the retention window
    with no events provably never ran, while an older one may simply have
    outlived its own history. A token with no usable creation date cannot clear
    that bar and degrades to ``unknown`` rather than claiming the stronger
    answer, because "never used" is the finding people act on by revoking.
    """
    if events:
        newest = events[0]
        return {
            "lastUsed": newest.get("created"),
            "lastUsedEvent": newest.get("event"),
            "lastUsedStatus": "used",
        }
    created_at = parse_api_timestamp(created)
    horizon = datetime.now(UTC) - timedelta(days=TOKEN_EVENTS_RETENTION_DAYS)
    provably_never = created_at is not None and created_at >= horizon
    return {
        "lastUsed": None,
        "lastUsedEvent": None,
        "lastUsedStatus": "never" if provably_never else "unknown",
    }


def token_error_entry(token_id: str, exc: Exception) -> dict[str, str]:
    """One accumulated per-token failure from the fan-out."""
    error_code = getattr(exc, "error_code", None)
    return {
        "token_id": token_id,
        "error_code": str(getattr(error_code, "value", error_code) or "UNKNOWN_ERROR"),
        "message": str(exc),
    }


def dormancy_rank(token: dict[str, Any]) -> tuple[int, float]:
    """Sort key placing the most-likely-revocable tokens first.

    Used tokens fall back on their timestamp so the longest-idle one leads that
    group; an unparseable timestamp sorts as "oldest" rather than being quietly
    pushed to the end where nobody would look at it.
    """
    status = str(token.get("lastUsedStatus", "used"))
    rank = _STATUS_RANK.get(status, len(_STATUS_RANK))
    moment = parse_api_timestamp(token.get("lastUsed"))
    return (rank, moment.timestamp() if moment else float("-inf"))


def enrich_tokens(
    client: SupportsTokenEvents,
    tokens: list[dict[str, Any]],
    *,
    max_workers: int,
) -> list[dict[str, str]]:
    """Add the three last-used fields to every token, in place, in parallel.

    One request per token -- 25 tokens on a busy project is 25 requests, and
    doing them serially is what makes people give up on token hygiene in the
    first place.

    A per-token failure degrades to ``lastUsedStatus == "error"`` plus an entry
    in the returned list rather than aborting the run: one unreadable token
    must not cost the caller the whole audit.

    Returns the accumulated per-token errors (empty when all succeeded).
    """
    errors: list[dict[str, str]] = []
    if not tokens:
        return errors
    lock = Lock()

    def fetch(token: dict[str, Any]) -> None:
        token_id = str(token.get("id", ""))
        try:
            events = client.list_token_events(token_id)
        except Exception as exc:
            logger.debug("Last-used lookup failed for token %s", token_id, exc_info=True)
            token.update(lastUsed=None, lastUsedEvent=None, lastUsedStatus="error")
            with lock:
                errors.append(token_error_entry(token_id, exc))
            return
        token.update(**derive_last_used(events, token.get("created")))

    with ThreadPoolExecutor(max_workers=min(len(tokens), max_workers)) as pool:
        for future in as_completed([pool.submit(fetch, token) for token in tokens]):
            future.result()  # propagate anything the worker did not handle itself
    return errors
