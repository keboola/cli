"""PyInstaller entry point for the frozen `kbagent` binary.

PyInstaller runs the entry script as top-level `__main__`, so the package's own
``src/keboola_agent_cli/__main__.py`` (which uses a relative import
``from .cli import app``) cannot be used directly — it raises
``ImportError: attempted relative import with no known parent package``.

This launcher uses an absolute import instead. Verified to produce a working
no-Python binary (`env -i kbagent --version` → `kbagent vX.Y.Z`).

Calls ``run`` (the telemetry-emitting wrapper), not ``app`` directly, so the
standalone binary posts usage telemetry exactly like the pip/uv console script.
"""

from keboola_agent_cli.cli import run

run()
