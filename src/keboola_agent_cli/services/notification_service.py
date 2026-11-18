"""Notification subscription discovery, audit, and write service (issue #600, #690).

Fleet-wide queries over the Notification Service's ``/project-subscriptions``,
joined in memory against the project's component configurations so callers can
answer "which flow does this alert belong to, and who does it page?" across
every registered project at once. Since issue #690 it also owns the write
path -- create, delete, and replace-recipient -- so scripts can fix a stale
recipient without a trip through the web UI.

These subscriptions back the Flow Builder's *Notifications* tab (bell icon).
They are NOT stored in a flow's ``configuration`` JSON, so ``flow detail`` /
``config detail`` never showed them -- that was the last unauditable
notification surface in a fleet-wide "are our alert recipients still valid"
sweep.

Two wire-format traps this module exists to absorb, both taken from the
service's public swagger:

- ``filters[].field`` values are dotted paths into the *event payload*
  (``job.component.id``, ``job.configuration.id``, ``branch.id``, ``phase.id``,
  ``durationOvertimePercentage``), not flat keys like ``configurationId``.
- ``recipient`` is discriminated on ``channel``: email carries ``address``,
  webhook carries ``url``. A single ``address`` column has to read both.

``filters`` is optional (only ``event`` and ``recipient`` are required), so a
project-wide catch-all subscription with no filters at all is legal and common
-- every join here degrades to "project-wide, no specific config" rather than
erroring or dropping the row.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx

from ..effective_branch import resolve_branch
from ..errors import ConfigError, ErrorCode, KeboolaApiError, request_outcome_unknown
from ..models import ProjectConfig
from ._notification_filters import (
    check_incoming_filters,
    is_equality,
    merge_filters,
    normalize_filters,
    validate_source_filters,
)
from .base import BaseService

logger = logging.getLogger(__name__)

# Dotted paths into the event payload, used as ``filters[].field`` values.
FILTER_FIELD_COMPONENT_ID = "job.component.id"
FILTER_FIELD_CONFIG_ID = "job.configuration.id"
FILTER_FIELD_BRANCH_ID = "branch.id"
FILTER_FIELD_PHASE_ID = "phase.id"
# Not a payload path: the job's run time over its usual run time, as a ratio
# (0.5 = 50 % longer than usual). The job-queue daemon puts it on the
# *-processing-long events only.
FILTER_FIELD_OVERTIME = "durationOvertimePercentage"
OPERATOR_AT_LEAST = ">="

# The events whose payload carries ``durationOvertimePercentage``. Any other
# event never matches an overtime filter, so ``--overtime-percent`` is refused
# there instead of creating a subscription that silently never fires.
OVERTIME_EVENTS = ("job-processing-long", "phase-job-processing-long")
# kbagent's own minimum, not the service's: the ``> 0`` constraint of the
# ``*ProcessingLongNotification`` data is on the EVENT the job-queue daemon
# sends, so the daemon only reports values above 0. A 0 % threshold would
# match every overtime step -- the same as no threshold -- so it is refused.
# (The UI can store 0; ``--filter 'durationOvertimePercentage>=0'`` also can.)
# The upper bound is technical, not a business rule: the daemon keeps emitting
# past 500 %, but the percent must stay representable once divided by 100.
MIN_OVERTIME_PERCENT = 1
# A plain decimal number. ``float()`` alone would also accept "1_0", " 0.5 "
# and "nan", which the service would never compare as numbers.
_NUMERIC_STRING = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?")
MAX_OVERTIME_PERCENT = 10000

# ``recipient.channel`` values the write path accepts. The CLI layer
# validates against the same tuple (defense in depth, not the primary guard --
# see ``_build_recipient``).
CHANNEL_EMAIL = "email"
CHANNEL_WEBHOOK = "webhook"
VALID_CHANNELS = (CHANNEL_EMAIL, CHANNEL_WEBHOOK)

# For help text and hints only. kbagent never rejects an event name against
# it: the service validates the event against its own registry on create, and
# may add events without a kbagent release.
KNOWN_EVENTS = (
    "job-failed",
    "job-succeeded",
    "job-succeeded-with-warning",
    "job-processing-long",
    "phase-job-failed",
    "phase-job-succeeded",
    "phase-job-succeeded-with-warning",
    "phase-job-processing-long",
)

SCOPE_CONFIG = "config"
SCOPE_PROJECT_WIDE = "project-wide"


def _filter_value(filters: list[Any], field: str) -> str:
    """Return the stringified value of the first EQUALITY filter matching ``field``.

    Filter values are declared as ``string | integer | boolean``, so every
    value is stringified here -- a numeric config ID must not reach the
    output columns (or an ``==`` comparison against a CLI option) as an int.

    Only an equality filter (operator omitted or ``==``) names a value: a
    ``branch.id < 123`` filter is not "branch 123", so it leaves the column
    empty (#989).
    """
    for item in filters:
        if isinstance(item, dict) and item.get("field") == field and is_equality(item):
            value = item.get("value")
            return "" if value is None else str(value)
    return ""


def _recipient_address(recipient: dict[str, Any]) -> str:
    """Return a recipient's destination regardless of channel.

    Email recipients carry ``address``, webhook recipients carry ``url``. An
    unknown future channel yields an empty string rather than raising -- the
    row is still worth showing for its event and filters.
    """
    return str(recipient.get("address") or recipient.get("url") or "")


def _overtime_percent(filters: list[Any]) -> int | float | None:
    """Return the overtime threshold as a percent, or ``None``.

    Only an unambiguous threshold is reported: exactly one
    ``durationOvertimePercentage`` filter whose operator is ``>=`` (an omitted
    operator means ``==`` on the wire) and whose value is a finite number or a
    numeric string. Anything else -- no filter, two filters, an equality
    filter, a boolean, NaN -- is ``None``; the raw ``filters`` stay in the row.
    The percent is an ``int`` when whole (``0.5`` -> ``50``) and keeps its
    fraction otherwise (``0.125`` -> ``12.5``) with no fixed decimal cut-off.
    Never raises: one malformed subscription must not break a fleet audit.
    """
    matches = [
        item
        for item in filters
        if isinstance(item, dict) and item.get("field") == FILTER_FIELD_OVERTIME
    ]
    if len(matches) != 1 or matches[0].get("operator") != OPERATOR_AT_LEAST:
        return None
    value = matches[0].get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, str) and not _NUMERIC_STRING.fullmatch(value):
        return None
    # Decimal of the value's shortest text form: 0.07 reads as exactly 7, and
    # no precision is cut off (1e-09 reads as 1e-07, not 0).
    try:
        exact = Decimal(value if isinstance(value, str) else repr(value)) * 100
    except (ArithmeticError, ValueError):  # decimal Overflow / InvalidOperation
        return None
    if not exact.is_finite() or exact < 0 or not math.isfinite(float(exact)):
        return None
    return int(exact) if exact == exact.to_integral_value() else float(exact)


def _validate_overtime_percent(event: str, overtime_percent: object) -> None:
    """Refuse a threshold the service would store but never match.

    Raises:
        KeboolaApiError: ``INVALID_ARGUMENT`` naming ``--overtime-percent``
            (exit 2 on the CLI, HTTP 400 over ``serve``) for an event outside
            :data:`OVERTIME_EVENTS`, a non-integer (booleans included), or a
            value outside ``MIN_OVERTIME_PERCENT..MAX_OVERTIME_PERCENT``.
    """
    if event not in OVERTIME_EVENTS:
        raise KeboolaApiError(
            message=(
                f"--overtime-percent applies only to {' and '.join(OVERTIME_EVENTS)}; "
                f"event '{event}' carries no run-time overtime, so the subscription "
                "would never fire."
            ),
            error_code=ErrorCode.INVALID_ARGUMENT,
        )
    _validate_overtime_value(overtime_percent)


def _validate_overtime_value(overtime_percent: object) -> None:
    """Refuse a non-integer (booleans included) or out-of-range threshold.

    Raises:
        KeboolaApiError: ``INVALID_ARGUMENT`` naming ``--overtime-percent``.
    """
    if (
        isinstance(overtime_percent, bool)
        or not isinstance(overtime_percent, int)
        or not MIN_OVERTIME_PERCENT <= overtime_percent <= MAX_OVERTIME_PERCENT
    ):
        raise KeboolaApiError(
            message=(
                f"--overtime-percent must be a whole number from {MIN_OVERTIME_PERCENT} "
                f"to {MAX_OVERTIME_PERCENT} "
                "(50 means 50 % longer than usual)."
            ),
            error_code=ErrorCode.INVALID_ARGUMENT,
        )


def _build_recipient(channel: str, address: str) -> dict[str, str]:
    """Build a ``recipient`` body for the given channel.

    Args:
        channel: One of :data:`VALID_CHANNELS`.
        address: Email address (``email``) or callback URL (``webhook``).

    Returns:
        ``{"channel": ..., "address": ...}`` for email, ``{"channel": ...,
        "url": ...}`` for webhook -- mirroring the discriminated shape
        :func:`_recipient_address` reads back.

    Raises:
        ConfigError: ``channel`` is outside :data:`VALID_CHANNELS`. The CLI
            layer validates this too (an explicit membership check against
            ``VALID_CHANNELS`` in the command, which exits 2 with
            ``INVALID_ARGUMENT``); this check is defense in depth for any
            other caller (SDK, tests) that skips it.
    """
    if channel == CHANNEL_EMAIL:
        return {"channel": CHANNEL_EMAIL, "address": address}
    if channel == CHANNEL_WEBHOOK:
        return {"channel": CHANNEL_WEBHOOK, "url": address}
    raise ConfigError(
        f"Unknown notification channel '{channel}'. Valid channels: {', '.join(VALID_CHANNELS)}."
    )


def _build_filters(
    component_id: str | None,
    config_id: str | None,
    branch_id: int | None,
    overtime_percent: int | None = None,
) -> list[dict[str, Any]]:
    """Build ``filters[]`` in a stable component/config/branch/overtime order.

    Each scope argument is included only when provided, and every value is
    stringified -- filter values are declared as ``string | integer |
    boolean`` on the wire, and a numeric config/branch ID must not reach the
    API as a raw int (mirrors the read-side stringification in
    :func:`_filter_value`).

    The overtime threshold is the exception: the service compares it with
    ``>=`` against a ratio, so it goes out as the float ``percent / 100`` with
    an explicit operator -- byte-for-byte what the Keboola UI writes.
    """
    filters: list[dict[str, Any]] = []
    if component_id:
        filters.append({"field": FILTER_FIELD_COMPONENT_ID, "value": str(component_id)})
    if config_id:
        filters.append({"field": FILTER_FIELD_CONFIG_ID, "value": str(config_id)})
    if branch_id is not None:
        filters.append({"field": FILTER_FIELD_BRANCH_ID, "value": str(branch_id)})
    if overtime_percent is not None:
        filters.append(
            {
                "field": FILTER_FIELD_OVERTIME,
                "value": overtime_percent / 100,
                "operator": OPERATOR_AT_LEAST,
            }
        )
    return filters


def _dedicated_options(
    component_id: str | None,
    config_id: str | None,
    branch_id: int | None,
    overtime_percent: int | None,
) -> list[tuple[str, str, bool]]:
    """``(option, field, given)`` for each dedicated option, as ``_build_filters`` reads them."""
    return [
        ("--component-id", FILTER_FIELD_COMPONENT_ID, bool(component_id)),
        ("--config-id", FILTER_FIELD_CONFIG_ID, bool(config_id)),
        ("--branch", FILTER_FIELD_BRANCH_ID, branch_id is not None),
        ("--overtime-percent", FILTER_FIELD_OVERTIME, overtime_percent is not None),
    ]


def _incoming_filters(
    component_id: str | None,
    config_id: str | None,
    branch_id: int | None,
    overtime_percent: int | None,
    filters: list[Any] | None,
) -> list[dict[str, Any]]:
    """Validate and build the filters a caller asks for: dedicated first, then generic.

    Raises:
        KeboolaApiError: ``INVALID_ARGUMENT`` for a malformed filter, a dedicated
            option and a filter on the same field, or a repeated field+operator.
    """
    # An explicit empty ID is not "omitted": it would silently skip the filter
    # (and the conflict check below), so it is refused.
    for option, value in (("--component-id", component_id), ("--config-id", config_id)):
        if value is not None and not str(value).strip():
            raise KeboolaApiError(
                message=f"{option} is empty. Give an ID, or leave the option out.",
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
    # Type and range before _build_filters divides it (a string would raise
    # TypeError there). The event check needs the event, so callers run it.
    if overtime_percent is not None:
        _validate_overtime_value(overtime_percent)
    generic = normalize_filters(filters)
    check_incoming_filters(
        _dedicated_options(component_id, config_id, branch_id, overtime_percent), generic
    )
    return _build_filters(component_id, config_id, branch_id, overtime_percent) + generic


_OUTCOME_UNKNOWN_HINT = (
    "The subscription may have been created anyway: do not re-run this command; "
    "check `kbagent notification list` first."
)


def _validate_expires_at(expires_at: str | None) -> None:
    """Refuse an explicit empty expiry: it is not "omitted" (create) or "inherit" (clone)."""
    if expires_at is not None and not expires_at.strip():
        raise KeboolaApiError(
            message="--expires-at is empty. Give an ISO-8601 time, or leave the option out.",
            error_code=ErrorCode.INVALID_ARGUMENT,
        )


def _create_or_explain(
    client: Any,
    event: str,
    recipient: dict[str, Any],
    filters: list[dict[str, Any]] | None,
    expires_at: str | None,
) -> dict[str, Any]:
    """POST a subscription; an unknown outcome says not to repeat it.

    The HTTP layer never repeats this POST after a 5xx or a read timeout. When
    the answer was lost the subscription may exist, so the error tells the
    caller to look before running the command again, and is not retryable.
    """
    try:
        return client.create_project_subscription(event, recipient, filters, expires_at)
    except (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError) as exc:
        # Transport failures the HTTP layer does not wrap: the request may have
        # reached the service, so the outcome is unknown, like a lost 5xx.
        raise KeboolaApiError(
            message=(
                f"The connection failed while creating the subscription ({type(exc).__name__}). "
                f"{_OUTCOME_UNKNOWN_HINT}"
            ),
            error_code=ErrorCode.CONNECTION_ERROR,
            retryable=False,
            details={"outcome_unknown": True},
        ) from exc
    except KeboolaApiError as exc:
        if not request_outcome_unknown(exc):
            raise
        raise KeboolaApiError(
            message=f"{exc.message} {_OUTCOME_UNKNOWN_HINT}",
            status_code=exc.status_code,
            error_code=exc.error_code,
            retryable=False,
            details=exc.details,
        ) from exc


def _extract_subscription_fields(sub: dict[str, Any]) -> dict[str, Any]:
    """Project a raw subscription dict into the canonical audit row.

    ``config_name`` is deliberately left unset -- the service joins it
    separately once the project's configurations have been fetched.
    """
    raw_filters = sub.get("filters") or []
    filters = raw_filters if isinstance(raw_filters, list) else []
    recipient = sub.get("recipient") or {}
    if not isinstance(recipient, dict):
        recipient = {}

    config_id = _filter_value(filters, FILTER_FIELD_CONFIG_ID)

    return {
        "subscription_id": str(sub.get("id", "")),
        "event": str(sub.get("event", "")),
        "component_id": _filter_value(filters, FILTER_FIELD_COMPONENT_ID),
        "config_id": config_id,
        "branch_id": _filter_value(filters, FILTER_FIELD_BRANCH_ID),
        "phase_id": _filter_value(filters, FILTER_FIELD_PHASE_ID),
        "channel": str(recipient.get("channel", "")),
        "address": _recipient_address(recipient),
        "expires_at": str(sub.get("expiresAt", "") or ""),
        # A subscription can filter on fields with no dedicated key (anything
        # the service adds later; overtime_percent summarises only one
        # unambiguous threshold), so the raw list rides along for --json.
        "filters": filters,
        "overtime_percent": _overtime_percent(filters),
        "scope": SCOPE_CONFIG if config_id else SCOPE_PROJECT_WIDE,
    }


def _clone_warnings(
    filters: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> list[str]:
    """Warnings about a clone's planned filters that the caller may not expect."""
    warnings: list[str] = []
    fields = {item["field"] for item in filters}
    if FILTER_FIELD_BRANCH_ID not in fields:
        warnings.append(
            "The new subscription has no branch.id filter; the Notification Service "
            "will add the default branch's id."
        )
    overridden = {item["field"] for item in incoming}
    # The flow changes whether --component-id / --config-id or a --filter on
    # the same field overrode it; ``incoming`` holds both.
    flow_changed = bool(overridden & {FILTER_FIELD_COMPONENT_ID, FILTER_FIELD_CONFIG_ID})
    if flow_changed and FILTER_FIELD_PHASE_ID in fields and FILTER_FIELD_PHASE_ID not in overridden:
        warnings.append(
            "The source's phase.id filter was copied, but that phase may not exist in "
            "the new flow. Override it with --filter 'phase.id=PHASE_ID' if needed."
        )
    return warnings


@dataclass
class ConfigNameIndex:
    """Lookup tables for resolving a subscription's parent configuration name.

    ``by_pair`` is the authoritative index. ``names_by_config_id`` exists only
    for the fallback path -- a subscription filtering on
    ``job.configuration.id`` with no component filter -- and holds every name
    seen for a config ID so an ambiguous match can be detected rather than
    guessed at.
    """

    by_pair: dict[tuple[str, str], str] = field(default_factory=dict)
    names_by_config_id: dict[str, list[str]] = field(default_factory=dict)


def _build_config_indexes(components: list[dict[str, Any]]) -> ConfigNameIndex:
    """Index a ``list_components_with_configs`` payload for name resolution."""
    index = ConfigNameIndex()

    for comp in components:
        comp_id = str(comp.get("id", ""))
        for cfg in comp.get("configurations", []) or []:
            cfg_id = str(cfg.get("id", ""))
            name = str(cfg.get("name", "") or "")
            index.by_pair[(comp_id, cfg_id)] = name
            index.names_by_config_id.setdefault(cfg_id, []).append(name)

    return index


def _resolve_config_name(row: dict[str, Any], index: ConfigNameIndex) -> str:
    """Resolve a row's parent configuration name, or "" when not determinable.

    Exact ``(component, config)`` match wins. When the subscription filters
    only on a config ID, fall back to a config-ID lookup -- but only when it
    is unambiguous. Two components sharing a config ID must not be guessed
    between: a wrong flow name in an alert audit is worse than a blank one.
    """
    config_id = row["config_id"]
    if not config_id:
        return ""

    component_id = row["component_id"]
    if component_id:
        return index.by_pair.get((component_id, config_id), "")

    candidates = index.names_by_config_id.get(config_id, [])
    return candidates[0] if len(candidates) == 1 else ""


def _matches_scope(
    row: dict[str, Any],
    component_id: str | None,
    config_id: str | None,
) -> bool:
    """Strict AND match of a row against the component / config filters."""
    if component_id is not None and row["component_id"] != component_id:
        return False
    return config_id is None or row["config_id"] == config_id


def _could_also_fire(
    row: dict[str, Any],
    component_id: str | None,
    config_id: str | None,
) -> bool:
    """Would a DROPPED row still fire for the scope being audited?

    True only when the mismatch comes from an ABSENT constraint on the row --
    that is the genuinely ambiguous case the exclusion counter warns about. A
    subscription carrying its own explicit filter for a *different* component
    can never fire for the audited one, so counting it would be noise in
    exactly the incident-response workflow this command exists for.
    """
    if component_id is not None and row["component_id"] and row["component_id"] != component_id:
        return False
    return not (config_id is not None and row["config_id"] and row["config_id"] != config_id)


class NotificationService(BaseService):
    """Audit and write queries over Notification Service subscriptions.

    ``list_subscriptions`` / ``get_subscription_detail`` are read-only. Since
    issue #690, ``create_subscription`` / ``delete_subscription`` /
    ``replace_subscription_recipient`` add a write path; since #989,
    ``clone_subscription`` too.
    """

    # ------------------------------------------------------------------
    # list
    # ------------------------------------------------------------------

    def list_subscriptions(
        self,
        aliases: list[str] | None = None,
        event: str | None = None,
        component_id: str | None = None,
        config_id: str | None = None,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """List notification subscriptions across one, many, or all projects.

        Args:
            aliases: Project aliases to query. ``None`` / empty means every
                registered project.
            event: Event-name filter. Sent as ``?event=`` AND applied
                client-side, because the service ignores the parameter -- see
                the comment in :meth:`_fetch_project_subscriptions`.
            component_id: Client-side filter on the subscription's
                ``job.component.id`` filter value (the API has no such filter).
            config_id: Client-side filter on ``job.configuration.id``.
            branch_id: Branch used to resolve configuration names. Subscriptions
                themselves are project-level; a branch-scoped one carries a
                ``branch.id`` filter, surfaced as the ``branch_id`` column.

        Returns:
            ``{"subscriptions": [...], "errors": [...],
            "project_wide_excluded": int}``. The counter reports how many
            filter-less catch-all subscriptions a ``component_id`` /
            ``config_id`` filter dropped -- those also fire for the config
            being audited, so silently omitting them would answer the
            "who gets paged" question wrongly.
        """
        projects = self.resolve_projects(aliases)

        def worker(alias: str, project: ProjectConfig) -> tuple[Any, ...]:
            return self._fetch_project_subscriptions(
                alias,
                project,
                event=event,
                component_id=component_id,
                config_id=config_id,
                branch_id=branch_id,
            )

        successes, errors = self._run_parallel(projects, worker)

        subscriptions: list[dict[str, Any]] = []
        project_wide_excluded = 0
        for result in successes:
            subscriptions.extend(result[1])
            project_wide_excluded += result[2]

        subscriptions.sort(
            key=lambda s: (
                s.get("project_alias", ""),
                s.get("event", ""),
                (s.get("config_name") or "").lower(),
                s.get("subscription_id", ""),
            )
        )
        errors.sort(key=lambda e: e.get("project_alias", ""))

        return {
            "subscriptions": subscriptions,
            "errors": errors,
            "project_wide_excluded": project_wide_excluded,
        }

    # ------------------------------------------------------------------
    # detail
    # ------------------------------------------------------------------

    def get_subscription_detail(
        self,
        alias: str,
        subscription_id: str,
        branch_id: int | None = None,
    ) -> dict[str, Any]:
        """Return the full audit row for a single subscription.

        Raises:
            ConfigError: alias not registered.
            KeboolaApiError: subscription not found or permission denied.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch = resolve_branch(self._config_store, alias, branch_id, role="source")

        client = self._client_factory(project.stack_url, project.token)
        try:
            row = _extract_subscription_fields(client.get_project_subscription(subscription_id))
            row["project_alias"] = alias
            self._stamp_config_names(client, [row], effective_branch)
            return row
        finally:
            client.close()

    # ------------------------------------------------------------------
    # write path (issue #690)
    # ------------------------------------------------------------------

    def create_subscription(
        self,
        alias: str,
        event: str,
        channel: str,
        address: str,
        component_id: str | None = None,
        config_id: str | None = None,
        branch_id: int | None = None,
        expires_at: str | None = None,
        overtime_percent: int | None = None,
        filters: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Create a notification subscription and return its audit row.

        Args:
            alias: Registered project alias.
            event: Kebab-case event name (see ``KNOWN_EVENTS`` for hints).
                kbagent does not validate it; the service checks it against
                its own event registry.
            channel: ``"email"`` or ``"webhook"`` -- see :data:`VALID_CHANNELS`.
            address: Email address (``email``) or callback URL (``webhook``).
            component_id: Optional ``job.component.id`` filter.
            config_id: Optional ``job.configuration.id`` filter.
            branch_id: Optional ``branch.id`` filter.
            expires_at: Optional ISO-8601 expiry, sent on the wire as
                ``expiresAt``.
            overtime_percent: Optional whole-percent threshold for the
                ``*-processing-long`` events (50 = the job runs 50 % longer
                than usual), sent as a ``durationOvertimePercentage >=``
                filter. ``None`` adds no filter.
            filters: Extra ``{field, operator?, value}`` filters (any event
                payload field), appended after the dedicated ones. ``None``
                and ``[]`` add none. See :func:`normalize_filters`.

        Returns:
            The same shape as :meth:`get_subscription_detail`: the created
            subscription projected through ``_extract_subscription_fields``,
            with ``project_alias`` and a best-effort ``config_name`` stamped.

        Raises:
            ConfigError: ``alias`` not registered, or ``channel`` invalid.
            KeboolaApiError: ``INVALID_ARGUMENT`` for an unusable
                ``overtime_percent`` (see :func:`_validate_overtime_percent`)
                or ``filters`` (see :func:`_incoming_filters`), or the API
                rejected the subscription.
        """
        if overtime_percent is not None:
            _validate_overtime_percent(event, overtime_percent)
        _validate_expires_at(expires_at)
        wire_filters = _incoming_filters(
            component_id, config_id, branch_id, overtime_percent, filters
        )
        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch = resolve_branch(self._config_store, alias, branch_id, role="source")

        recipient = _build_recipient(channel, address)

        client = self._client_factory(project.stack_url, project.token)
        try:
            created = _create_or_explain(client, event, recipient, wire_filters or None, expires_at)
            row = _extract_subscription_fields(created)
            row["project_alias"] = alias
            self._stamp_config_names(client, [row], effective_branch)
            return row
        finally:
            client.close()

    def delete_subscription(self, alias: str, subscription_id: str) -> dict[str, Any]:
        """Delete a notification subscription.

        Args:
            alias: Registered project alias.
            subscription_id: Numeric-string subscription ID.

        Returns:
            ``{"project_alias": alias, "subscription_id": str(subscription_id),
            "deleted": True}``.

        Raises:
            ConfigError: ``alias`` not registered.
            KeboolaApiError: the subscription was not found (404), or the API
                rejected the delete -- propagated verbatim, never swallowed.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]

        client = self._client_factory(project.stack_url, project.token)
        try:
            client.delete_project_subscription(subscription_id)
            return {
                "project_alias": alias,
                "subscription_id": str(subscription_id),
                "deleted": True,
            }
        finally:
            client.close()

    def replace_subscription_recipient(
        self,
        alias: str,
        subscription_id: str,
        new_address: str,
        new_channel: str | None = None,
    ) -> dict[str, Any]:
        """Swap a subscription's recipient by creating a new one, then deleting the old.

        CREATE-then-DELETE, deliberately the reverse of the delete-then-recreate
        sketch in the originating issue. The Notification Service has no
        in-place recipient PATCH -- creating always mints a fresh ID -- so the
        two steps can never be one atomic operation, and the ordering decides
        which failure mode you get. Creating first means a failed second step
        (the delete) leaves the OLD subscription lingering alongside the new
        one: a duplicate alert, recoverable with a follow-up
        :meth:`delete_subscription` call. Deleting first would instead risk
        losing the alert entirely if the create then failed -- unrecoverable,
        and silently so. A failed delete therefore does NOT raise -- ANY
        exception, not just :class:`KeboolaApiError` (a raw transport failure
        such as ``httpx.RemoteProtocolError`` is just as possible, and letting
        it escape here would lose ``new_subscription_id`` from the response
        entirely, inviting a retry that mints a THIRD subscription): it is
        reported via ``old_deleted: False`` plus a warning naming the old
        subscription id, leaving the caller to decide whether to retry.

        The old subscription's ``event``, raw ``filters``, and ``expiresAt``
        are carried over VERBATIM (no rebuild) -- only the recipient changes.
        ``new_channel=None`` keeps the old subscription's channel; if that
        channel is empty or unknown and no explicit ``new_channel`` is given,
        this raises rather than guess at a destination.

        Args:
            alias: Registered project alias.
            subscription_id: The subscription whose recipient is being replaced.
            new_address: New email address or webhook URL.
            new_channel: Optional channel override. ``None`` keeps the old
                subscription's channel.

        Returns:
            ``{"old_subscription_id", "new_subscription_id", "old_address",
            "old_deleted": bool, "warnings": [str, ...], **new_audit_row}`` --
            the new subscription's audit row (same shape as
            :meth:`get_subscription_detail`) merged with the replace-specific
            keys above. ``new_subscription_id`` is ALWAYS the fresh id the API
            minted -- a caller must not keep using the old one.

        Raises:
            ConfigError: ``alias`` not registered, ``new_channel`` invalid, or
                the old recipient's channel is empty/unknown with no
                ``new_channel`` override.
            KeboolaApiError: fetching the old subscription or creating the new
                one failed. A failure to delete the old one is caught (any
                exception, not just this one) and reported instead -- see
                above.
        """
        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch = resolve_branch(self._config_store, alias, None, role="source")

        client = self._client_factory(project.stack_url, project.token)
        try:
            old = client.get_project_subscription(subscription_id)
            old_recipient = old.get("recipient") or {}
            if not isinstance(old_recipient, dict):
                old_recipient = {}
            old_address = _recipient_address(old_recipient)
            channel = new_channel or str(old_recipient.get("channel", ""))
            if not channel:
                raise ConfigError(
                    "The old subscription's recipient channel is empty or "
                    "unknown; pass an explicit channel to replace it."
                )

            recipient = _build_recipient(channel, new_address)
            created = client.create_project_subscription(
                str(old.get("event", "")),
                recipient,
                old.get("filters") or None,
                old.get("expiresAt"),
            )

            warnings: list[str] = []
            try:
                client.delete_project_subscription(subscription_id)
                old_deleted = True
            except Exception as exc:
                # Deliberately broad: the new subscription already exists at
                # this point, so ANY failure here -- a KeboolaApiError, or a
                # raw transport failure (httpx.ReadError / WriteError /
                # RemoteProtocolError / ProxyError, none of which
                # ``_do_request`` wraps) -- must be swallowed the same way.
                # Letting one of those escape would lose ``new_subscription_id``
                # from the response entirely, and a caller's retry would then
                # mint a THIRD subscription -- exactly what create-first
                # ordering exists to avoid.
                old_deleted = False
                logger.warning(
                    "Failed to delete old notification subscription %s after "
                    "replacing its recipient: %s",
                    subscription_id,
                    exc,
                )
                warnings.append(
                    f"Old subscription {subscription_id} was not deleted "
                    f"(a duplicate now exists alongside the new one): {exc}"
                )

            row = _extract_subscription_fields(created)
            row["project_alias"] = alias
            self._stamp_config_names(client, [row], effective_branch)

            return {
                "old_subscription_id": str(subscription_id),
                "new_subscription_id": row["subscription_id"],
                "old_address": old_address,
                "old_deleted": old_deleted,
                "warnings": warnings,
                **row,
            }
        finally:
            client.close()

    def clone_subscription(
        self,
        alias: str,
        subscription_id: str,
        component_id: str | None = None,
        config_id: str | None = None,
        branch_id: int | None = None,
        overtime_percent: int | None = None,
        filters: list[Any] | None = None,
        address: str | None = None,
        channel: str | None = None,
        expires_at: str | None = None,
        clear_expires_at: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Create a new subscription from an existing one, with overrides (#989).

        The source is read once and never changed. Its event and project are
        kept. Filters use grouped replacement (:func:`merge_filters`): every
        field an override names loses all its source filters and gets the new
        ones; every other source filter is copied verbatim. Without
        ``branch_id`` the source's ``branch.id`` filters are kept as they are --
        the active branch never changes the scope (it is read only for the
        config-name join).

        Recipient: untouched, the source's raw recipient dict is reused;
        ``address`` alone keeps the source channel; ``channel`` needs
        ``address``. Expiry: inherited, replaced by ``expires_at``, or dropped
        by ``clear_expires_at`` (the two exclude each other).

        ``dry_run`` reads the source and returns the planned body; nothing is
        created. A clone without any override duplicates the alert -- the
        caller asked for exactly that. Running a clone twice creates two.

        Returns:
            ``source_subscription_id``, ``dry_run``, ``filters_replaced``
            (``[{field, old, new}]``), ``filters_added``, ``filters_kept``,
            ``warnings``, and either ``planned`` (dry run) or
            ``new_subscription_id`` plus the new subscription's audit row.

        Raises:
            ConfigError: ``alias`` not registered, or the source channel is
                empty/unknown and ``address`` is given without ``channel``.
            KeboolaApiError: ``INVALID_ARGUMENT`` for conflicting or malformed
                overrides, a source whose filters cannot be copied, or an
                overtime threshold on an event that has none; any API error.
        """
        if channel is not None and address is None:
            raise KeboolaApiError(
                message="--channel needs --address: give the new recipient's address too.",
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
        if channel is not None and channel not in VALID_CHANNELS:
            raise KeboolaApiError(
                message=f"Invalid --channel '{channel}'. Valid channels: {', '.join(VALID_CHANNELS)}.",
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
        _validate_expires_at(expires_at)
        if expires_at is not None and clear_expires_at:
            raise KeboolaApiError(
                message="--expires-at and --clear-expires-at exclude each other.",
                error_code=ErrorCode.INVALID_ARGUMENT,
            )
        incoming = _incoming_filters(component_id, config_id, branch_id, overtime_percent, filters)

        projects = self.resolve_projects([alias])
        project = projects[alias]
        effective_branch = resolve_branch(self._config_store, alias, None, role="source")

        client = self._client_factory(project.stack_url, project.token)
        try:
            source = client.get_project_subscription(subscription_id)
            event = str(source.get("event", ""))
            if overtime_percent is not None:
                _validate_overtime_percent(event, overtime_percent)
            source_filters = validate_source_filters(str(subscription_id), source)
            merge = merge_filters(source_filters, incoming)

            raw_recipient = source.get("recipient")
            if address is None:
                if not isinstance(raw_recipient, dict):
                    raise KeboolaApiError(
                        message=(
                            f"Subscription {subscription_id} has no recipient kbagent can "
                            "copy; pass --address (and --channel)."
                        ),
                        error_code=ErrorCode.INVALID_ARGUMENT,
                    )
                recipient: dict[str, Any] = raw_recipient
            else:
                source_channel = (
                    raw_recipient.get("channel", "") if isinstance(raw_recipient, dict) else ""
                )
                new_channel = channel or str(source_channel)
                if not new_channel:
                    raise ConfigError(
                        "The source subscription's recipient channel is empty or "
                        "unknown; pass --channel with --address."
                    )
                recipient = _build_recipient(new_channel, address)

            if clear_expires_at:
                new_expires = None
            elif expires_at is not None:
                new_expires = expires_at
            else:
                new_expires = source.get("expiresAt")
            warnings = _clone_warnings(merge.filters, incoming)
            report: dict[str, Any] = {
                "project_alias": alias,
                "source_subscription_id": str(subscription_id),
                "dry_run": dry_run,
                "filters_replaced": merge.replaced,
                "filters_added": merge.added,
                "filters_kept": merge.kept,
                "warnings": warnings,
            }
            if dry_run:
                report["planned"] = {
                    "event": event,
                    "recipient": recipient,
                    "filters": merge.filters,
                    "expiresAt": new_expires,
                }
                return report

            created = _create_or_explain(
                client,
                event,
                recipient,
                merge.filters or None,
                new_expires,
            )
            row = _extract_subscription_fields(created)
            row["project_alias"] = alias
            if not self._stamp_config_names(client, [row], effective_branch):
                warnings.append(
                    "The configuration name of the new subscription could not be read; "
                    "the subscription was created."
                )
            return {**row, **report, "new_subscription_id": row["subscription_id"]}
        finally:
            client.close()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _fetch_project_subscriptions(
        self,
        alias: str,
        project: ProjectConfig,
        event: str | None,
        component_id: str | None,
        config_id: str | None,
        branch_id: int | None,
    ) -> tuple[Any, ...]:
        """Fetch, filter and name-join subscriptions for a single project.

        Returns one of TWO shapes, per the ``BaseService._run_parallel``
        convention: ``(alias, rows, excluded, True)`` on success, or the
        2-tuple ``(alias, error_dict)`` on failure. The arity difference IS
        the discriminator -- ``_run_parallel`` sorts on ``len(result) == 2``
        -- so neither shape may grow or shrink independently of the other.
        """
        effective_branch = resolve_branch(self._config_store, alias, branch_id, role="source")

        client = self._client_factory(project.stack_url, project.token)
        try:
            raw = client.list_project_subscriptions(event=event)
            rows = [_extract_subscription_fields(sub) for sub in raw]

            # The live service IGNORES ``?event=`` -- verified against a real
            # stack, where a filtered request answers 200 with every
            # subscription in the project. The parameter is still sent (the
            # swagger documents it, and a server-side fix would then cost
            # nothing), but the narrowing has to happen here or --event
            # answers "who gets paged on failure" with a superset that
            # includes success recipients.
            if event:
                rows = [row for row in rows if row["event"] == event]

            # Count only rows the filter actually DROPPED. A subscription
            # filtering on job.component.id alone is labelled project-wide
            # (scope keys off the config filter), but --component-id KEEPS
            # it -- warning that it was hidden would contradict the table
            # the user is looking at.
            excluded = 0
            if component_id is not None or config_id is not None:
                kept: list[dict[str, Any]] = []
                for row in rows:
                    if _matches_scope(row, component_id, config_id):
                        kept.append(row)
                    elif row["scope"] == SCOPE_PROJECT_WIDE and _could_also_fire(
                        row, component_id, config_id
                    ):
                        excluded += 1
                rows = kept

            for row in rows:
                row["project_alias"] = alias
            self._stamp_config_names(client, rows, effective_branch)

            # Success shape: 4-tuple. See the arity contract in the docstring.
            return (alias, rows, excluded, True)
        except KeboolaApiError as exc:
            # Failure shape: 2-tuple, which is how _run_parallel detects it.
            return (
                alias,
                {
                    "project_alias": alias,
                    "error_code": exc.error_code,
                    "message": exc.message,
                },
            )
        except Exception as exc:
            # Failure shape: 2-tuple, which is how _run_parallel detects it.
            return (
                alias,
                {
                    "project_alias": alias,
                    "error_code": "UNEXPECTED_ERROR",
                    "message": str(exc),
                },
            )
        finally:
            client.close()

    @staticmethod
    def _stamp_config_names(
        client: Any,
        rows: list[dict[str, Any]],
        branch_id: int | None,
    ) -> bool:
        """Stamp ``config_name`` on every row, in place; ``False`` when the lookup failed.

        The single ``list_components_with_configs`` round-trip is skipped
        entirely when no row is config-scoped: its payload is proportional to
        the whole project, and a project full of catch-all subscriptions would
        otherwise pay for a join that can only ever produce blanks.

        Which listing call is used depends on what the rows actually need.
        ``list_components_with_configs`` sends ``include=configuration,rows``
        -- every config body and row in the project -- which is a steep price
        for a display name, and it is paid once per project in the fan-out.
        When every config-scoped row names its component (the common case:
        a Notifications-tab subscription filters on both), one cheap
        per-component listing answers the same question. Only a subscription
        filtering on a bare config ID needs the whole-project view, because
        resolving that ID means searching every component.

        A failing lookup is swallowed: ``config_name`` is a display nicety,
        and losing it must never hide the recipient the audit came for.
        """
        for row in rows:
            row["config_name"] = ""

        scoped = [row for row in rows if row["config_id"]]
        if not scoped:
            return True

        index = ConfigNameIndex()
        try:
            if any(not row["component_id"] for row in scoped):
                index = _build_config_indexes(
                    client.list_components_with_configs(branch_id=branch_id)
                )
            else:
                for component_id in sorted({row["component_id"] for row in scoped}):
                    for cfg in client.list_component_configs(component_id, branch_id=branch_id):
                        index.by_pair[(component_id, str(cfg.get("id", "")))] = str(
                            cfg.get("name", "") or ""
                        )
        except Exception as exc:
            logger.debug("Config-name join failed for notification subscriptions: %s", exc)
            return False

        for row in rows:
            row["config_name"] = _resolve_config_name(row, index)
        return True


__all__ = [
    "CHANNEL_EMAIL",
    "CHANNEL_WEBHOOK",
    "FILTER_FIELD_BRANCH_ID",
    "FILTER_FIELD_COMPONENT_ID",
    "FILTER_FIELD_CONFIG_ID",
    "FILTER_FIELD_PHASE_ID",
    "KNOWN_EVENTS",
    "SCOPE_CONFIG",
    "SCOPE_PROJECT_WIDE",
    "VALID_CHANNELS",
    "NotificationService",
]
