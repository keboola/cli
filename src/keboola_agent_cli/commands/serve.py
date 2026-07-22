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
from datetime import UTC, datetime
from pathlib import Path

import typer

from ..constants import ENV_CONVERSATION_ID
from ..errors import ConfigError

logger = logging.getLogger(__name__)

ENV_AUTH_TOKEN = "KBAGENT_SERVE_TOKEN"
ENV_UI_DIST = "KBAGENT_UI_DIST"


def _default_conversation_id() -> str:
    """Generate a conversation ID for a fresh ``kbagent serve`` session.

    Format: ``serve-<UTC-timestamp>-<8 hex>``. The timestamp prefix makes
    log scanning by start time trivial; the hex suffix disambiguates
    rapid restarts in the same minute. The ``serve-`` prefix is how
    observability dashboards filter "human-driven session" vs other
    integrations.

    Reads ``KBAGENT_CONVERSATION_ID`` and reuses it when present so a
    caller that pre-set the var (CI, supervisor scripts, debugging across
    a restart) keeps a stable session ID across kbagent serve restarts.
    """
    existing = os.environ.get(ENV_CONVERSATION_ID, "").strip()
    if existing:
        return existing
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"serve-{stamp}-{secrets.token_hex(4)}"


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


# Box-drawing glyphs used by the startup banner, mapped to ASCII so the banner
# degrades to `|-` / `` `- `` on a console that can't render Unicode -- the same
# fallback install.sh uses (Unicode only under a UTF-8 locale, ASCII otherwise).
_BANNER_ASCII_FALLBACK = str.maketrans({"├": "|", "└": "`", "─": "-"})


def _encode_safe(text: str, encoding: str | None) -> str:
    """Return ``text`` with box-drawing glyphs stripped to ASCII if unencodable.

    On Windows with a non-UTF-8 console codepage (cp1250 on Czech/Polish/
    Hungarian locales, and any other legacy single-byte encoding) the ``├─`` /
    ``└─`` glyphs in the startup banner cannot be encoded, so ``sys.stdout.write``
    raises ``UnicodeEncodeError`` -- which crashed ``kbagent serve`` before
    uvicorn ever bound the port (issue #522). When ``encoding`` can represent the
    banner we keep the Unicode glyphs; otherwise we transliterate to ASCII.

    ``encoding`` is ``sys.stdout.encoding`` at the call site (``None`` on an
    exotic stream); an unknown codec name (``LookupError``) also degrades to
    ASCII rather than propagating.
    """
    enc = encoding or "utf-8"
    try:
        text.encode(enc)
    except (UnicodeEncodeError, LookupError):
        return text.translate(_BANNER_ASCII_FALLBACK)
    return text


def _write_banner(text: str) -> None:
    """Write the startup banner without ever letting a glyph crash startup.

    Two layers of defense (issue #522): first transliterate to ASCII when
    stdout's encoding can't represent the banner, then wrap the write in a
    belt-and-braces ``try/except`` so even an unforeseen un-encodable character
    re-emits with lossy replacement instead of aborting the server. A degraded
    banner is always better than a server that won't start.
    """
    encoding = getattr(sys.stdout, "encoding", None)
    safe = _encode_safe(text, encoding)
    try:
        sys.stdout.write(safe)
    except UnicodeEncodeError:
        enc = encoding or "ascii"
        sys.stdout.write(safe.encode(enc, "replace").decode(enc, "replace"))
    sys.stdout.flush()


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
        from ..server.app import _resolve_cors_origins
    except ModuleNotFoundError as exc:  # pragma: no cover
        missing = exc.name or "server extras"
        typer.echo(
            f"\nkbagent serve requires the optional 'server' extras "
            f"(missing: {missing}).\n\n"
            "Reinstall with the [server] extras:\n\n"
            "  # If you installed via uv tool install (recommended for end users):\n"
            "  uv tool install --force --with 'keboola-cli[server]' \\\n"
            "    git+https://github.com/keboola/cli\n\n"
            "  # If you have a local checkout (development):\n"
            "  uv pip install -e '.[server]'\n",
            err=True,
        )
        raise typer.Exit(code=1) from None

    auth_token = os.environ.get(ENV_AUTH_TOKEN) or secrets.token_urlsafe(32)
    os.environ[ENV_AUTH_TOKEN] = auth_token

    # Generate a stable conversation ID for this serve session and export it
    # to env so child processes (MCP subprocess, AI agent CLI invocations,
    # scheduled `kbagent http` calls) inherit it and emit X-Conversation-ID
    # on every Keboola API request. Otherwise observability shows
    # "Conversation ID not set" in `kbagent doctor`.
    conversation_id = _default_conversation_id()
    os.environ[ENV_CONVERSATION_ID] = conversation_id

    cors = list(cors_origin) if cors_origin else None
    # Validate --cors-origin up front so an unsafe value (wildcard with
    # credentials, GHSA-5mh2) is a clean usage error -- and so we do NOT
    # mis-attribute an unrelated create_app ConfigError to --cors-origin.
    try:
        _resolve_cors_origins(cors)
    except ConfigError as exc:
        raise typer.BadParameter(str(exc), param_hint="--cors-origin") from None

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
        banner = (
            "\n"
            "  kbagent serve  (single-process UI mode)\n"
            f"  ├─ open:      http://{host}:{port}/\n"
            f"  ├─ api docs:  http://{host}:{port}/docs\n"
            f"  ├─ ui dist:   {resolved_ui_dist}\n"
            f"  ├─ conv id:   {conversation_id}\n"
            f"  └─ token:     {auth_token}\n"
            "\n"
            "  Browser is auto-authenticated via an HttpOnly kbagent_session cookie\n"
            "  set on GET /. Token never enters the JS heap or the URL.\n"
            f"  For curl / scripts: Authorization: Bearer {auth_token}\n"
            "\n"
            "  For `kbagent http` in another terminal (bash/zsh):\n"
            f"    export KBAGENT_SERVE_URL=http://{host}:{port}\n"
            f"    export KBAGENT_SERVE_TOKEN={auth_token}\n"
            f"    export KBAGENT_CONVERSATION_ID={conversation_id}\n"
            "\n"
        )
    else:
        banner = (
            "\n"
            "  kbagent serve\n"
            f"  ├─ host:      http://{host}:{port}\n"
            f"  ├─ docs:      http://{host}:{port}/docs\n"
            f"  ├─ openapi:   http://{host}:{port}/openapi.json\n"
            f"  ├─ conv id:   {conversation_id}\n"
            f"  └─ token:     {auth_token}\n"
            "\n"
            f"  Set {ENV_AUTH_TOKEN}={auth_token} for the Node BFF.\n"
            "  Tip: `kbagent serve --ui` mounts the React SPA on the same port.\n"
            "\n"
            "  For `kbagent http` in another terminal (bash/zsh):\n"
            f"    export KBAGENT_SERVE_URL=http://{host}:{port}\n"
            f"    export KBAGENT_SERVE_TOKEN={auth_token}\n"
            f"    export KBAGENT_CONVERSATION_ID={conversation_id}\n"
            "\n"
        )
    _write_banner(banner)

    uvicorn.run(
        app,
        host=host,
        port=port,
        reload=reload,
        log_level=log_level,
    )
