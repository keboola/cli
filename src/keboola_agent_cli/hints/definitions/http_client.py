"""Hint definitions for ``kbagent http`` self-call commands.

These four commands are a thin shell over ``HttpForwarderService.request()``
which in turn opens an ``httpx.Client`` against the running ``kbagent serve``.
Unlike other CLI commands, they do NOT use any KeboolaClient — there is no
Storage/Manage/AI Service involved. The ``client`` hint therefore renders a
raw ``httpx`` snippet (the actual transport layer); the ``service`` hint
renders the ``HttpForwarderService`` call (the kbagent-flavoured wrapper that
also handles env-var resolution, body parsing, and error mapping).

Both modes target the same use case: an AI agent inside a scheduled task
that wants to talk to the live serve without forking another ``kbagent`` CLI
tree (and inheriting potentially stale config). The renderer for
``client_type="kbagent_serve"`` lives in ``hints/renderer.py``.
"""

from .. import HintRegistry
from ..models import ClientCall, CommandHint, HintStep, ServiceCall

_NOTES = [
    "Requires KBAGENT_SERVE_URL + KBAGENT_SERVE_TOKEN env vars. The scheduler "
    "auto-injects both into every AI-agent / cli_command subprocess; outside "
    "that context export them yourself or run via `kbagent http` directly.",
    "Prefer this over forking `kbagent <cmd>` from a scheduled task — the "
    "HTTP path always reads the OPERATOR'S live config (the running serve), "
    "not the global ~/.config/keboola-agent-cli/ that a fresh subprocess "
    "would pick up.",
    "Browse the OpenAPI to discover endpoints: `kbagent http get /openapi.json`.",
]


def _http_hint(verb: str, *, with_body: bool, description: str) -> CommandHint:
    """Build one CommandHint for a single HTTP verb.

    The args dict gets ``path`` + ``timeout`` always, plus ``body`` only for
    verbs that carry a request body (POST / PATCH). GET / DELETE pass through
    with no body — matching the CLI surface in ``commands/http_client.py``.
    """
    client_args: dict[str, str] = {"path": "{path}", "timeout": "{timeout}"}
    service_args: dict[str, str] = {
        "method": f'"{verb.upper()}"',
        "path": "{path}",
        "timeout": "{timeout}",
    }
    if with_body:
        client_args["body"] = "{body}"
        service_args["body"] = "{body}"

    return CommandHint(
        cli_command=f"http.{verb}",
        description=description,
        steps=[
            HintStep(
                comment=f"{verb.upper()} an endpoint on the running kbagent serve",
                client=ClientCall(
                    method=verb,
                    args=client_args,
                    client_type="kbagent_serve",
                    result_var="result",
                    result_hint="dict | list | str",
                ),
                service=ServiceCall(
                    service_class="HttpForwarderService",
                    service_module="http_forwarder_service",
                    method="request",
                    args=service_args,
                ),
            ),
        ],
        notes=_NOTES,
    )


# ── http get ──────────────────────────────────────────────────────

HintRegistry.register(
    _http_hint(
        "get",
        with_body=False,
        description="GET an endpoint on the running kbagent serve",
    )
)


# ── http post ─────────────────────────────────────────────────────

HintRegistry.register(
    _http_hint(
        "post",
        with_body=True,
        description="POST to an endpoint on the running kbagent serve",
    )
)


# ── http patch ────────────────────────────────────────────────────

HintRegistry.register(
    _http_hint(
        "patch",
        with_body=True,
        description="PATCH an endpoint on the running kbagent serve",
    )
)


# ── http delete ───────────────────────────────────────────────────

HintRegistry.register(
    _http_hint(
        "delete",
        with_body=False,
        description="DELETE an endpoint on the running kbagent serve",
    )
)
