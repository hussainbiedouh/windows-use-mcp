"""Keeps stdout clean for the MCP stdio transport.

On a stdio MCP server, stdout carries the JSON-RPC frames. Any stray
``print()`` from a library would corrupt the protocol stream and break the
client connection.

The upstream ``windows-use`` package prints to stdout from its UI Automation
watchdog error handlers (``windows_use/agent/watchdog/event_handlers.py``),
which fire whenever a focus/structure/property callback raises.

The guard below redirects every ``print()`` to stderr. It deliberately leaves
``sys.stdout`` untouched: the MCP transport writes to ``sys.stdout.buffer``
rather than calling ``print()``, so the protocol channel stays intact.
"""

from __future__ import annotations

import builtins
import sys

_installed = False


def install_stdout_guard() -> None:
    """Route ``print()`` output to stderr. Safe to call more than once."""
    global _installed
    if _installed:
        return

    real_print = builtins.print

    def guarded_print(*args, **kwargs):
        kwargs.setdefault("file", sys.stderr)
        try:
            return real_print(*args, **kwargs)
        except Exception:  # noqa: BLE001 - a broken stderr must not kill the server
            return None

    builtins.print = guarded_print
    _installed = True
