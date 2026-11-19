"""The flow checks of `flow new` / `flow update` for a generic configuration write.

`config new --push`, `config update` and `config clone` (and their REST routes,
which `kbagent http` reaches too) write a configuration of any component. For
`keboola.flow` they now refuse the definitions `flow new` / `flow update`
refuse -- before, they wrote a flow the job queue would not run.
"""

from __future__ import annotations

from typing import Any

from ..errors import ErrorCode, KeboolaApiError
from .flow_validation import validate_conditional_flow

FLOW_COMPONENT_ID = "keboola.flow"


def check_flow_configuration(component_id: str, configuration: dict[str, Any] | None) -> None:
    """Raise ``INVALID_FLOW_DEFINITION`` when a keboola.flow body would be broken.

    The semantic checks of ``validate_conditional_flow`` (no schema fetch: the
    `flow` commands add the live schema), on the body the write leaves. Every
    body is checked, ``{}`` too ("no phases"); the callers skip the check only
    where nothing is written to the body: `config new` without
    ``--configuration`` (the empty flow `flow new` makes without a definition)
    and a metadata-only `config update`. Other components are not checked.
    """
    if component_id != FLOW_COMPONENT_ID or not isinstance(configuration, dict):
        return
    errors = validate_conditional_flow(
        configuration.get("phases", []), configuration.get("tasks", [])
    )
    if errors:
        raise KeboolaApiError(
            message=(
                "Flow definition is invalid: "
                + "; ".join(errors)
                + ". Nothing was written. Check it with `kbagent flow validate` and "
                "write flows with `kbagent flow new` / `flow update`."
            ),
            status_code=400,
            error_code=ErrorCode.INVALID_FLOW_DEFINITION,
            retryable=False,
        )
