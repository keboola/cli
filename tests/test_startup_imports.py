"""Startup import guardrail (issue #801).

`kbagent` registers its command groups lazily (see ``LAZY_COMMANDS`` in
``cli.py``) and builds services on first use, so one invocation imports only the
command it runs. Nothing fails loudly when that regresses -- a stray top-level
import in ``cli.py``, ``telemetry.py`` or ``__init__.py`` just makes every
command slower again. These tests pin it down deterministically: each one runs
a fresh interpreter (``sys.modules`` of the test process is polluted by every
other test) and inspects which modules got imported.

The budget counts only kbagent's OWN modules -- third-party and stdlib counts
differ by Python version and OS, kbagent's do not. It ratchets DOWN only: when
a change lowers the count, lower ``KBAGENT_MODULE_BUDGET`` with it. Raising it
needs a reason in the PR (and usually a lazy import instead).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from keboola_agent_cli.cli import LAZY_COMMANDS

# kbagent-owned modules imported by `import keboola_agent_cli.cli`.
KBAGENT_MODULE_BUDGET = 34

# Heavy optional dependencies no single command's startup path may pay for
# unless that command needs them (prompt_toolkit: interactive pickers / REPL;
# fastapi + uvicorn: `serve`; jsonschema: flow / config validation).
HEAVY_THIRD_PARTY = ("prompt_toolkit", "fastapi", "uvicorn", "starlette", "jsonschema")

_MARKER = "@@MODULES@@"


def _loaded_modules(tmp_path: Path, code: str) -> set[str]:
    """Run ``code`` in a fresh interpreter and return its final ``sys.modules`` keys."""
    script = f"import json, sys\n{code}\nprint({_MARKER!r} + json.dumps(sorted(sys.modules)))\n"
    env = {
        **os.environ,
        # Keep the child hermetic: no auto-update check, no telemetry, and a
        # throwaway config dir instead of the developer's real one.
        "KBAGENT_AUTO_UPDATE": "false",
        "KBAGENT_DISABLE_TELEMETRY": "1",
        "KBAGENT_CONFIG_DIR": str(tmp_path),
        "COLUMNS": "120",
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith(_MARKER)]
    assert lines, (
        f"child produced no module list.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return set(json.loads(lines[-1][len(_MARKER) :]))


def _command_modules(modules: set[str]) -> set[str]:
    """Modules of the root command registry (``commands/<module>.py``) that got imported."""
    registered = {entry.target.split(":")[0] for entry in LAZY_COMMANDS}
    return {name for name in registered if f"keboola_agent_cli.commands.{name}" in modules}


def _kbagent_modules(modules: set[str]) -> set[str]:
    return {m for m in modules if m == "keboola_agent_cli" or m.startswith("keboola_agent_cli.")}


def test_importing_cli_loads_no_command_module(tmp_path: Path) -> None:
    modules = _loaded_modules(tmp_path, "import keboola_agent_cli.cli")

    assert _command_modules(modules) == set()
    assert not any(m.startswith("keboola_agent_cli.commands.") for m in modules)
    assert "keboola_agent_cli.lib" not in modules, "the SDK facade must stay lazy in __init__.py"
    assert not {m.split(".")[0] for m in modules} & set(HEAVY_THIRD_PARTY)


def test_importing_cli_stays_within_module_budget(tmp_path: Path) -> None:
    own = _kbagent_modules(_loaded_modules(tmp_path, "import keboola_agent_cli.cli"))

    assert len(own) <= KBAGENT_MODULE_BUDGET, (
        f"`import keboola_agent_cli.cli` now loads {len(own)} kbagent modules "
        f"(budget {KBAGENT_MODULE_BUDGET}). Import the new dependency lazily "
        f"inside the function that needs it. Loaded:\n" + "\n".join(sorted(own))
    )


def test_job_list_help_imports_only_the_job_group(tmp_path: Path) -> None:
    """The acceptance case from #801: one real invocation, end to end."""
    modules = _loaded_modules(
        tmp_path,
        "from keboola_agent_cli.cli import app\n"
        "try:\n"
        "    app(['job', 'list', '--help'])\n"
        "except SystemExit:\n"
        "    pass",
    )

    assert _command_modules(modules) == {"job"}
    assert not {m.split(".")[0] for m in modules} & set(HEAVY_THIRD_PARTY)
    # Services are built on first use; `--help` uses none.
    services = {m for m in modules if m.startswith("keboola_agent_cli.services.")}
    assert "keboola_agent_cli.services.job_service" not in services


@pytest.mark.parametrize("alias", ["sl", "mr"])
def test_hidden_alias_imports_only_its_group(tmp_path: Path, alias: str) -> None:
    modules = _loaded_modules(
        tmp_path,
        "from keboola_agent_cli.cli import app\n"
        "try:\n"
        f"    app([{alias!r}, '--help'])\n"
        "except SystemExit:\n"
        "    pass",
    )

    expected = {"sl": {"semantic_layer"}, "mr": {"merge_request"}}[alias]
    assert _command_modules(modules) == expected


def test_root_help_still_loads_every_command(tmp_path: Path) -> None:
    """`kbagent --help` needs every command's help text, so it resolves them all."""
    modules = _loaded_modules(
        tmp_path,
        "from keboola_agent_cli.cli import app\ntry:\n    app(['--help'])\nexcept SystemExit:\n    pass",
    )

    assert _command_modules(modules) == {entry.target.split(":")[0] for entry in LAZY_COMMANDS}
