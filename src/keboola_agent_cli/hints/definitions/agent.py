"""Hint definitions for `kbagent agent ...` commands.

Intentionally empty: the agent CRUD/run commands are pure-local (they
read/write ``<config_dir>/agents.json`` and spawn local subprocesses via
the runner). There is no Keboola HTTP API behind them, so the ``client``
mode hints would have to invent a fake ``KeboolaClient.list_agent_tasks``
that doesn't exist.

The ``--hint`` framework falls back to a "No --hint available" message
when a command has no registered hint, which is the right answer here.
For programmatic use, refer to
:class:`keboola_agent_cli.services.agent_service.AgentService` directly
-- its methods mirror the CLI surface one-to-one.
"""

from __future__ import annotations
