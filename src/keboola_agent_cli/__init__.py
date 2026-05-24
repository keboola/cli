"""Keboola Agent CLI - AI-friendly interface to Keboola projects."""

from importlib.metadata import PackageNotFoundError, version

from .constants import APP_NAME

try:
    __version__ = version(APP_NAME)
except PackageNotFoundError:
    __version__ = "0.0.0-dev"
