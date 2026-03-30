"""Keboola Agent CLI - AI-friendly interface to Keboola projects."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("keboola-agent-cli")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"
