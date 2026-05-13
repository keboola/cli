"""kbagent serve - launch the FastAPI HTTP server.

Wraps all kbagent services as REST endpoints. Print the bearer token to
stdout on startup so the Node.js BFF (or any other client) can read it.

With ``--ui`` the same process additionally serves the built React SPA
from ``web/frontend/dist`` and pre-injects the bearer token into the
HTML, so end-users don't need to spawn a Node BFF or paste tokens.
"""

from __future__ import annotations

import logging
import os
import secrets
import sys
from pathlib import Path

import typer

logger = logging.getLogger(__name__)

ENV_AUTH_TOKEN = "KBAGENT_SERVE_TOKEN"
ENV_UI_DIST = "KBAGENT_UI_DIST"


def _autodetect_ui_dist() -> Path | None:
    """Find ``web/frontend/dist`` for an editable install or local checkout.

    Search order:
    1) ``$KBAGENT_UI_DIST`` env var (explicit override).
    2) ``<repo>/web/frontend/dist`` resolved relative to *this* file. Works
       for ``uv pip install -e .`` checkouts where the package lives at
       ``<repo>/src/keboola_agent_cli/`` and the SPA build is sibling at
       ``<repo>/web/frontend/dist``.
    3) ``<cwd>/web/frontend/dist`` -- helpful when running from a clone.

    Returns ``None`` if no candidate exists; the caller then surfaces a
    "run `make web-build` first" error.
    """
    env = os.environ.get(ENV_UI_DIST)
    if env:
        p = Path(env).expanduser().resolve()
        return p if (p / "index.html").exists() else None

    # commands/serve.py -> commands -> keboola_agent_cli -> src -> repo
    repo_dist = Path(__file__).resolve().parents[3] / "web" / "frontend" / "dist"
    if (repo_dist / "index.html").exists():
        return repo_dist

    cwd_dist = Path.cwd() / "web" / "frontend" / "dist"
    if (cwd_dist / "index.html").exists():
        return cwd_dist
    return None


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
    ui: bool = typer.Option(
        False,
        "--ui",
        help=(
            "Mount the built React SPA at / so a single uvicorn process serves "
            "both the API and the UI. The bearer token is injected into "
            "index.html so the browser boots already authenticated -- no Node "
            "BFF needed. Run `make web-build` once to produce the dist/ folder."
        ),
    ),
    ui_dist: str | None = typer.Option(
        None,
        "--ui-dist",
        help=(
            "Override the path to the built React dist/ directory. Defaults to "
            "<repo>/web/frontend/dist relative to the package, or $KBAGENT_UI_DIST. "
            "Implies --ui."
        ),
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

    # Resolve --ui/--ui-dist. Either flag opts in; --ui-dist additionally
    # pins the path. If --ui is on but no dist found, abort with the
    # "run make web-build" hint instead of silently launching API-only.
    resolved_ui_dist: str | None = None
    if ui or ui_dist:
        candidate = Path(ui_dist).expanduser().resolve() if ui_dist else _autodetect_ui_dist()
        if candidate is None or not (candidate / "index.html").exists():
            typer.echo(
                "--ui: no built React dist/ found.\n"
                "  Run `make web-build` (or set --ui-dist / $KBAGENT_UI_DIST).\n"
                f"  Searched: $KBAGENT_UI_DIST, "
                f"{Path(__file__).resolve().parents[3] / 'web/frontend/dist'}, "
                f"{Path.cwd() / 'web/frontend/dist'}",
                err=True,
            )
            raise typer.Exit(code=1) from None
        resolved_ui_dist = str(candidate)

    app = create_app(
        config_dir=config_dir,
        auth_token=auth_token,
        cors_origins=cors,
        serve_url=serve_url,
        ui_dist=resolved_ui_dist,
    )

    if resolved_ui_dist:
        # UI mode: the user opens the browser directly; there is no BFF to
        # paste the token into. The token is still printed so scripted
        # callers / curl one-liners keep working.
        sys.stdout.write(
            "\n"
            "  kbagent serve  (single-process UI mode)\n"
            f"  ├─ open:      http://{host}:{port}/\n"
            f"  ├─ api docs:  http://{host}:{port}/docs\n"
            f"  ├─ ui dist:   {resolved_ui_dist}\n"
            f"  └─ token:     {auth_token}\n"
            "\n"
            "  Browser is auto-authenticated via injected window.__KBAGENT_TOKEN.\n"
            f"  For curl / scripts: Authorization: Bearer {auth_token}\n"
            "\n"
        )
    else:
        sys.stdout.write(
            "\n"
            "  kbagent serve\n"
            f"  ├─ host:      http://{host}:{port}\n"
            f"  ├─ docs:      http://{host}:{port}/docs\n"
            f"  ├─ openapi:   http://{host}:{port}/openapi.json\n"
            f"  └─ token:     {auth_token}\n"
            "\n"
            f"  Set {ENV_AUTH_TOKEN}={auth_token} for the Node BFF.\n"
            "  Tip: `kbagent serve --ui` mounts the React SPA on the same port.\n"
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
