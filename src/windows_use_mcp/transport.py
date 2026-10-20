"""A plain, non-intrusive stdio transport for the MCP server.

The MCP Python SDK 2.x ``stdio_server`` performs a "stream claim" on Windows:
it duplicates fd 0/1, redirects them to /dev/null and stderr and rebinds the
Windows ``STD_*`` handles. That hardening is invisible to the SDK's own
client but breaks the way Bun (OpenCode) spawns a child process over pipes -
the client ends up waiting for a reply that never arrives.

This module implements the same wire format (newline delimited JSON-RPC) using
nothing but fd 0 and fd 1, which every MCP client understands.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
import mcp_types as types
from mcp.shared._context_streams import create_context_streams
from mcp.shared.message import SessionMessage


def _write_frame(payload: str) -> None:
    """Write one JSON-RPC frame to stdout and flush it."""
    sys.stdout.buffer.write(payload.encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


@asynccontextmanager
async def plain_stdio_server() -> AsyncIterator[tuple[Any, Any]]:
    """Serve MCP over the process's stdin/stdout using plain file descriptors."""
    read_stream_writer, read_stream = create_context_streams[SessionMessage | Exception](0)
    write_stream, write_stream_reader = create_context_streams[SessionMessage](0)

    loop = asyncio.get_running_loop()

    async def stdin_reader() -> None:
        try:
            async with read_stream_writer:
                while True:
                    line = await loop.run_in_executor(None, sys.stdin.buffer.readline)
                    if not line:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        message = types.jsonrpc_message_adapter.validate_json(line, by_name=False)
                    except Exception as exc:  # noqa: BLE001 - surfaced to the client
                        await read_stream_writer.send(exc)
                        continue
                    await read_stream_writer.send(SessionMessage(message))
        except anyio.ClosedResourceError:
            await anyio.lowlevel.checkpoint()

    async def stdout_writer() -> None:
        try:
            async with write_stream_reader:
                async for session_message in write_stream_reader:
                    payload = session_message.message.model_dump_json(
                        by_alias=True, exclude_unset=True
                    )
                    await loop.run_in_executor(None, _write_frame, payload)
        except anyio.ClosedResourceError:
            await anyio.lowlevel.checkpoint()

    async with anyio.create_task_group() as tg:
        tg.start_soon(stdin_reader)
        tg.start_soon(stdout_writer)
        yield read_stream, write_stream
