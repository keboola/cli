"""`--set` path guard for `config update` / `config row-update` (issue #593).

Split out of ``config_service.py``: that module sits at its hard file-size
ceiling, and this guard is a self-contained, framework-free validation unit
with no dependency on ``ConfigService`` state.
"""

from __future__ import annotations

from typing import Any

from ..constants import CONFIG_SET_GUARD_HINTS, CONFIG_SET_GUARDED_PREFIXES
from ..errors import ErrorCode, KeboolaApiError


def validate_set_paths(set_paths: list[tuple[str, Any]] | None) -> None:
    """Reject ``--set`` paths whose first segment is an API-level sibling of
    ``configuration`` (issue #593 Part A).

    ``config update --set`` and ``config row-update --set`` are documented
    as editing ``configuration.*`` only. ``_resolve_configuration`` /
    ``_resolve_row_configuration`` used to apply every ``--set`` path
    unconditionally onto ``current_detail.get("configuration", {})`` --
    but keys like ``state`` or ``name`` are SIBLINGS of ``configuration``
    in the Storage API config-detail response, not children of it. So
    e.g. ``--set 'state.x=y'`` silently created ``configuration.state.x``
    instead of touching the real ``state`` field: exit 0, a bumped
    version, a plausible-looking ``--dry-run`` diff, and the runtime
    state left untouched.

    Call this BEFORE any network call so the rejection also covers
    ``--dry-run`` -- a usage mistake should never be allowed to look like
    a successful preview. ``_resolve_configuration`` and
    ``_resolve_row_configuration`` both call this as their first line.

    This is a plain, side-effect-free validation function (no CLI/typer
    dependency, per the service layer's framework-free contract) so the
    command layer can call it directly too, ahead of ``service.update_config``
    / ``service.update_config_row``, and map the raised error to a usage
    exit code before anything (including a client) is constructed.

    Args:
        set_paths: The parsed ``(path, value)`` pairs from one or more
            ``--set PATH=VALUE`` flags, or ``None``/empty if none given.

    Raises:
        KeboolaApiError: ``error_code=ErrorCode.INVALID_ARGUMENT`` naming
            the offending path, its first segment, and the real tool/flag
            to use instead -- callers should map this to a usage-error
            exit code (see ``commands/config.py``'s existing ``--set
            PATH=VALUE`` format check for the pattern to mirror).
    """
    if not set_paths:
        return
    for path, _value in set_paths:
        # Bracket syntax is the same class of usage mistake as a sibling path,
        # so it is rejected in the same place, with the same exit code, before
        # the same boundary (any network call). set_nested_value raises on it
        # too, but that ValueError surfaces mid-request as an unhandled crash
        # with no structured output -- exactly the silent-ish failure this
        # guard exists to prevent.
        if "[" in path or "]" in path:
            raise KeboolaApiError(
                status_code=400,
                error_code=ErrorCode.INVALID_ARGUMENT,
                message=(
                    f"--set '{path}' uses bracket syntax, which is not "
                    f"supported. Use dot-separated integer segments over an "
                    f"existing list instead, e.g. 'files.0' rather than "
                    f"'files[0]'."
                ),
            )
        first_segment = path.split(".", 1)[0]
        if first_segment not in CONFIG_SET_GUARDED_PREFIXES:
            continue
        hint = CONFIG_SET_GUARD_HINTS.get(first_segment, "not settable via --set")
        raise KeboolaApiError(
            status_code=400,
            error_code=ErrorCode.INVALID_ARGUMENT,
            message=(
                f"--set '{path}' targets '{first_segment}', which is a "
                f"top-level API field, not part of 'configuration'. --set "
                f"only edits configuration.* -- use {hint} instead. If the "
                f"component genuinely has a 'configuration.{first_segment}' "
                f"key, pass the full body via --configuration JSON|@file|- "
                f"instead."
            ),
        )
