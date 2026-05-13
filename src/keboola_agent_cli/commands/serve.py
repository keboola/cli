"""kbagent serve - launch the FastAPI HTTP server.

Wraps all kbagent services as REST endpoints. Print the bearer token to
stdout on startup so the Node.js BFF (or any other client) can read it.
"""

from __future__ import annotations

import logging
import os
import secrets
import sys

import typer

logger = logging.getLogger(__name__)

ENV_AUTH_TOKEN = "KBAGENT_SERVE_TOKEN"


def serve_command(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind host. Default 127.0.0.1 (localhost-only).",
    ),
    port: int = typer.Option(
        8001,
        "--port",
        help="Bind port. Default 8001 to leave 8000 for the Node BFF.",
    ),
    reload: bool = typer.Option(
        False,
        "--reload",
        help="Auto-reload on code changes (uvicorn --reload).",
    ),
    log_level: str = typer.Option(
        "info",
        "--log-level",
        help="uvicorn log level: critical, error, warning, info, debug, trace.",
    ),
    cors_origin: list[str] = typer.Option(
        None,
        "--cors-origin",
        help="Add a CORS origin (repeatable). Default: localhost:5173 / 8000.",
    ),
    config_dir: str | None = typer.Option(
        None,
        "--config-dir",
        help="Override config directory path (matches kbagent --config-dir).",
    ),
) -> None:
    """Launch the kbagent HTTP API server.

    The server prints a bearer token on startup. Set ``KBAGENT_SERVE_TOKEN``
    in advance to pin a value (useful for the Node BFF dev workflow).
    """
    try:
        import uvicorn
    except ImportError:  # pragma: no cover
        typer.echo(
            "FastAPI/uvicorn not installed. Reinstall kbagent with the 'server' extra:\n"
            "  uv pip install -e '.[server]'",
            err=True,
        )
        raise typer.Exit(code=1) from None

    from ..server import create_app

    auth_token = os.environ.get(ENV_AUTH_TOKEN) or secrets.token_urlsafe(32)
    os.environ[ENV_AUTH_TOKEN] = auth_token

    cors = list(cors_origin) if cors_origin else None

    serve_url = f"http://{host}:{port}"
    app = create_app(
        config_dir=config_dir,
        auth_token=auth_token,
        cors_origins=cors,
        serve_url=serve_url,
    )

    sys.stdout.write(
        "\n"
        "  kbagent serve\n"
        f"  ├─ host:      http://{host}:{port}\n"
        f"  ├─ docs:      http://{host}:{port}/docs\n"
        f"  ├─ openapi:   http://{host}:{port}/openapi.json\n"
        f"  └─ token:     {auth_token}\n"
        "\n"
        f"  Set {ENV_AUTH_TOKEN}={auth_token} for the Node BFF.\n"
        "\n"
    )
    sys.stdout.flush()

    uvicorn.run(
        app,
        host=host,
        port=port,
        reload=reload,
        log_level=log_level,
    )
