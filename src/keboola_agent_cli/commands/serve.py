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
    """Find the built React SPA across install layouts.

    Search order (first match wins):
    1) ``$KBAGENT_UI_DIST`` env var -- explicit override, never auto-overridden.
    2) ``keboola_agent_cli/_ui_dist`` *inside the installed package* --
       populated at wheel build time by the ``hatch_build.py`` hook. This
       is what ``uv tool install git+...`` and ``pip install`` from PyPI
       both produce when Node is available (or a maintainer pre-built it).
    3) ``<repo>/web/frontend/dist`` resolved relative to this source file --
       editable installs (``uv pip install -e .``) plus local clones.
    4) ``<cwd>/web/frontend/dist`` -- last-resort for unusual layouts.

    Returns ``None`` if no candidate exists; the caller then surfaces a
    "no UI bundled" error with rebuild instructions tailored to the
    likely install method.
    """
    env = os.environ.get(ENV_UI_DIST)
    if env:
        p = Path(env).expanduser().resolve()
        return p if (p / "index.html").exists() else None

    # commands/serve.py -> commands -> keboola_agent_cli/ -> _ui_dist
    bundled = Path(__file__).resolve().parent.parent / "_ui_dist"
    if (bundled / "index.html").exists():
        return bundled

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
            "both the API and the UI. ``GET /`` sets an HttpOnly `kbagent_session` "
            "cookie (SameSite=Strict, Path=/) so the browser boots already "
            "authenticated -- no Node BFF, no paste step, no token in the JS "
            "heap or URL. Run `make web-build` once to produce the dist/ folder."
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
    # Probe the optional server extras up front so users get a friendly
    # install hint instead of a Python traceback. We catch BOTH imports
    # (uvicorn AND the FastAPI surface that ``..server`` pulls in) because
    # a partial install (one extra present, the other not) was producing
    # the legacy "ModuleNotFoundError: No module named 'fastapi'" raw
    # traceback in 0.40.0 -- the previous guard only watched uvicorn.
    try:
        import uvicorn

        from ..server import create_app
    except ModuleNotFoundError as exc:  # pragma: no cover
        missing = exc.name or "server extras"
        typer.echo(
            f"\nkbagent serve requires the optional 'server' extras "
            f"(missing: {missing}).\n\n"
            "Reinstall with the [server] extras:\n\n"
            "  # If you installed via uv tool install (recommended for end users):\n"
            "  uv tool install --force --with 'keboola-agent-cli[server]' \\\n"
            "    git+https://github.com/padak/keboola_agent_cli\n\n"
            "  # If you have a local checkout (development):\n"
            "  uv pip install -e '.[server]'\n",
            err=True,
        )
        raise typer.Exit(code=1) from None

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
            bundled = Path(__file__).resolve().parent.parent / "_ui_dist"
            repo_dist = Path(__file__).resolve().parents[3] / "web" / "frontend" / "dist"
            cwd_dist = Path.cwd() / "web" / "frontend" / "dist"
            typer.echo(
                "--ui: no built React SPA found.\n"
                "\n"
                "  Searched these locations (none had index.html):\n"
                f"    1. $KBAGENT_UI_DIST    {os.environ.get(ENV_UI_DIST, '(unset)')}\n"
                f"    2. installed package   {bundled}\n"
                f"    3. repo checkout       {repo_dist}\n"
                f"    4. cwd                 {cwd_dist}\n"
                "\n"
                "  How to fix:\n"
                "    - From git checkout: `make web-build` then re-run.\n"
                "    - From `uv tool install git+...`: re-install with Node 20+\n"
                "      on PATH; the build hook will compile the SPA automatically.\n"
                "    - Or pin a path: `kbagent serve --ui-dist /path/to/dist`.",
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
            "  Browser is auto-authenticated via an HttpOnly kbagent_session cookie\n"
            "  set on GET /. Token never enters the JS heap or the URL.\n"
            f"  For curl / scripts: Authorization: Bearer {auth_token}\n"
            "\n"
            "  For `kbagent http` in another terminal (bash/zsh):\n"
            f"    export KBAGENT_SERVE_URL=http://{host}:{port}\n"
            f"    export KBAGENT_SERVE_TOKEN={auth_token}\n"
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
            "  For `kbagent http` in another terminal (bash/zsh):\n"
            f"    export KBAGENT_SERVE_URL=http://{host}:{port}\n"
            f"    export KBAGENT_SERVE_TOKEN={auth_token}\n"
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
