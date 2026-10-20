"""Log level control for the upstream ``windows_use`` package.

The upstream package sets its own logger to ``DEBUG`` at module import time
and dumps the full window list on every desktop capture. That import happens
lazily (see ``runner.py``), so the level has to be re-applied after each
import rather than only at server start-up.
"""

from __future__ import annotations

import logging
import os

ENV_LOG_LEVEL = "WINDOWS_USE_LOG_LEVEL"
DEFAULT_LEVEL = "WARNING"

#: Loggers the upstream package creates.
NOISY_LOGGERS = (
    "windows_use",
    "windows_use.agent.desktop.service",
    "windows_use.agent.tree.service",
)


def apply_log_level() -> None:
    """Force the upstream loggers to the configured level."""
    name = os.environ.get(ENV_LOG_LEVEL, DEFAULT_LEVEL).upper()
    level = getattr(logging, name, None)
    if not isinstance(level, int):
        level = logging.WARNING
    for logger_name in NOISY_LOGGERS:
        logging.getLogger(logger_name).setLevel(level)
