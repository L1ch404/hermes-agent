"""
Java log manager — reads console output from a rotating log file.
"""

from __future__ import annotations

import os
import tempfile
import time


class LogManager:
    """Manage log file creation and reading."""

    def __init__(self, base_dir: str | None = None):
        self._base_dir = base_dir or os.path.join(tempfile.gettempdir(), "jolink-logs")
        os.makedirs(self._base_dir, exist_ok=True)
        self._current_file: str | None = None

    def create(self, main_class: str) -> str:
        """Create a new log file and return its path."""
        ts = int(time.time())
        self._current_file = os.path.join(
            self._base_dir, f"{main_class}-{ts}.log"
        )
        return self._current_file

    @property
    def path(self) -> str | None:
        return self._current_file

    def tail(self, n: int = 50) -> dict:
        """Return the last N lines of the current log."""
        if not self._current_file:
            return {"error": "No log file created"}
        try:
            with open(self._current_file, "r") as f:
                lines = f.readlines()
            return {
                "lines": lines[-n:] if n > 0 else lines,
                "total_lines": len(lines),
                "log_file": self._current_file,
            }
        except FileNotFoundError:
            return {"error": f"Log file not found: {self._current_file}"}
