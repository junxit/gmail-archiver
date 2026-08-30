"""Gmail Archiver - A tool to backup and restore Gmail emails with metadata."""
from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth is pyproject.toml, so the CLI's --version cannot
    # drift from the packaged version.
    __version__ = version("gmail-archiver")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0+unknown"
