"""Hint definitions for encrypt commands."""

from .. import HintRegistry
from ..models import ClientCall, CommandHint, HintStep, ServiceCall

# ── encrypt values ─────────────────────────────────────────────────

HintRegistry.register(
    CommandHint(
        cli_command="encrypt.values",
        description="Encrypt configuration values",
        steps=[
            HintStep(
                comment="Encrypt values via Encryption API",
                client=ClientCall(
                    method="encrypt_values",
                    args={"component_id": "{component_id}", "data": "{input}"},
                    result_var="encrypted",
                    result_hint="dict",
                ),
                service=ServiceCall(
                    service_class="EncryptService",
                    service_module="encrypt_service",
                    method="encrypt",
                    args={
                        "alias": "{project}",
                        "component_id": "{component_id}",
                        "input_data": "{input}",
                    },
                ),
            ),
        ],
        notes=[
            "Uses the Encryption API (encryption.keboola.com).",
            "Values must be prefixed with '#' to be encrypted.",
            "Already-encrypted values (KBC:: prefix) are passed through.",
        ],
    )
)
