"""MCP server exposing the Windows-Use GUI agent.

Tools
-----
``windows_use_task``           Run a natural-language GUI task with the Windows-Use agent.
``windows_desktop_state``      Read-only snapshot of the open windows (fast, no LLM).
``windows_accessibility_tree`` Read-only UI Automation tree of the active window.
``windows_screenshot``         Capture the virtual screen as a PNG.
``windows_use_status``         Report the resolved LLM configuration and versions.

Targets the MCP Python SDK 2.x low-level server API.
"""

from __future__ import annotations

import asyncio
import base64
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import mcp_types as types
from mcp.server.lowlevel import Server

from .config import (
    ConfigurationError,
    build_llm,
    fallback_settings,
    resolve_candidates,
    resolve_settings,
)
from .logging_setup import apply_log_level
from .runner import run_agent_task
from .stdout_guard import install_stdout_guard
from .transport import plain_stdio_server

# stdout carries the MCP frames - make sure no library can pollute it.
install_stdout_guard()

# Keep the upstream package's very chatty DEBUG logs off the wire.
apply_log_level()

#: GUI automation is not safe to run concurrently - one task at a time.
_GUI_LOCK = asyncio.Lock()

#: A single dedicated thread for every UI Automation call.
#:
#: COM objects created by ``comtypes`` belong to the apartment of the thread
#: that created them, so all GUI work has to happen on one thread. Using the
#: default thread pool would hand the same session to different threads.
_GUI_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="windows-use")


async def _run_on_gui_thread(func, *args, **kwargs):
    """Run a blocking GUI function on the dedicated GUI thread."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_GUI_EXECUTOR, lambda: func(*args, **kwargs))


DEFAULT_MAX_STEPS = 25
DEFAULT_TIMEOUT_SECONDS = 900

SERVER_NAME = "windows-use"
SERVER_VERSION = "1.0.0"


def _text(payload: str) -> list[types.ContentBlock]:
    return [types.TextContent(type="text", text=payload)]


def _error(message: str, **extra: Any) -> list[types.ContentBlock]:
    data = {"error": message, **extra}
    return _text(json.dumps(data, indent=2, ensure_ascii=False, default=str))


TOOL_DEFS: list[types.Tool] = [
    types.Tool(
        name="windows_use_task",
        title="Control the Windows desktop",
        description=(
            "Control the Windows desktop at the GUI level. Give a natural-language task "
            "(e.g. 'Open Notepad and write a poem', 'Close the Excel window that is "
            "minimized') and the agent will click, type, scroll, run PowerShell and "
            "navigate the UI until the task is done. Returns the agent's final answer "
            "plus a transcript of the steps it took. This drives the real mouse and "
            "keyboard of the logged-in user session."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The GUI task to perform, in plain English.",
                },
                "max_steps": {
                    "type": "integer",
                    "description": (
                        "Maximum number of agent steps before giving up. "
                        f"Default {DEFAULT_MAX_STEPS}."
                    ),
                    "default": DEFAULT_MAX_STEPS,
                    "minimum": 1,
                    "maximum": 200,
                },
                "use_vision": {
                    "type": "boolean",
                    "description": (
                        "Send screenshots of the screen to the model. Slower and more "
                        "expensive, but far more reliable for canvas-like or unusual UIs."
                    ),
                    "default": False,
                },
                "instructions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Extra system instructions for the agent.",
                },
                "browser": {
                    "type": "string",
                    "description": "Target browser for web tasks: edge, chrome or firefox.",
                    "enum": ["edge", "chrome", "firefox"],
                    "default": "edge",
                },
                "mode": {
                    "type": "string",
                    "description": "'normal' (full context) or 'flash' (lighter, faster).",
                    "enum": ["normal", "flash"],
                    "default": "normal",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": (
                        "Wall-clock limit for the task. The agent keeps running in the "
                        f"background if it is hit. Default {DEFAULT_TIMEOUT_SECONDS}."
                    ),
                    "default": DEFAULT_TIMEOUT_SECONDS,
                    "minimum": 30,
                },
                "provider": {
                    "type": "string",
                    "description": "Override the configured LLM provider for this call.",
                },
                "model": {
                    "type": "string",
                    "description": "Override the configured LLM model for this call.",
                },
            },
            "required": ["task"],
        },
        annotations=types.ToolAnnotations(
            destructiveHint=True,
            openWorldHint=True,
            title="Control the Windows desktop",
        ),
    ),
    types.Tool(
        name="windows_desktop_state",
        title="List open Windows windows",
        description=(
            "List the windows that are currently open on the Windows desktop, plus the "
            "active window. Read-only and fast - use it to find out what is running "
            "before delegating a task."
        ),
        input_schema={"type": "object", "properties": {}},
        annotations=types.ToolAnnotations(readOnlyHint=True, title="List open Windows windows"),
    ),
    types.Tool(
        name="windows_accessibility_tree",
        title="Dump the UI Automation tree",
        description=(
            "Dump the UI Automation tree of the active window as text (id, window, "
            "control type, name, coordinates). Read-only. Use it to locate the exact "
            "element ids the agent refers to."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "max_rows": {
                    "type": "integer",
                    "description": "Maximum number of element rows to return.",
                    "default": 200,
                    "minimum": 1,
                    "maximum": 2000,
                }
            },
        },
        annotations=types.ToolAnnotations(readOnlyHint=True, title="Dump the UI Automation tree"),
    ),
    types.Tool(
        name="windows_screenshot",
        title="Capture the screen",
        description="Capture the current screen (all monitors) and return it as a PNG image.",
        input_schema={"type": "object", "properties": {}},
        annotations=types.ToolAnnotations(readOnlyHint=True, title="Capture the screen"),
    ),
    types.Tool(
        name="windows_use_status",
        title="Windows-Use server status",
        description=(
            "Report the Windows-Use installation version and the LLM configuration the "
            "server resolved (the API key is never printed, only whether one is set)."
        ),
        input_schema={"type": "object", "properties": {}},
        annotations=types.ToolAnnotations(readOnlyHint=True, title="Windows-Use server status"),
    ),
]


async def _on_list_tools(
    ctx: Any, params: types.PaginatedRequestParams | None
) -> types.ListToolsResult:
    """Return the tool catalogue."""
    return types.ListToolsResult(tools=TOOL_DEFS)


async def _on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
    """Dispatch a tool call."""
    name = params.name
    args = params.arguments or {}

    try:
        if name == "windows_use_task":
            content = await _run_task(args)
        elif name == "windows_desktop_state":
            content = await _desktop_state()
        elif name == "windows_accessibility_tree":
            content = await _accessibility_tree(args)
        elif name == "windows_screenshot":
            content = await _screenshot()
        elif name == "windows_use_status":
            content = await _status()
        else:
            content = _error(f"Unknown tool: {name}", tool=name)
    except ConfigurationError as exc:
        content = _error(str(exc), tool=name)
    except Exception as exc:  # noqa: BLE001 - surface the failure to the client
        content = _error(f"{type(exc).__name__}: {exc}", tool=name)

    return types.CallToolResult(content=content)


async def _run_task(args: dict) -> list[types.ContentBlock]:
    task = (args.get("task") or "").strip()
    if not task:
        return _error("The 'task' argument is required and must not be empty.")

    timeout = int(args.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    max_steps = int(args.get("max_steps") or DEFAULT_MAX_STEPS)

    candidates = resolve_candidates(
        provider=args.get("provider"),
        model=args.get("model"),
    )

    base_kwargs = {
        "task": task,
        "max_steps": max_steps,
        "use_vision": bool(args.get("use_vision", False)),
        "instructions": args.get("instructions") or [],
        "browser": args.get("browser"),
        "mode": args.get("mode") or "normal",
    }

    attempted: list[dict[str, Any]] = []
    result: dict[str, Any] | None = None
    settings = candidates[0]

    async with _GUI_LOCK:
        for index, candidate in enumerate(candidates):
            settings = candidate
            try:
                llm = build_llm(candidate)
            except Exception as exc:  # noqa: BLE001
                attempted.append(
                    {**candidate.public_dict(), "error": f"{type(exc).__name__}: {exc}"}
                )
                continue

            try:
                result = await asyncio.wait_for(
                    _run_on_gui_thread(run_agent_task, llm=llm, **base_kwargs),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                return _error(
                    f"The Windows-Use agent did not finish within {timeout} seconds. "
                    "The task may still be running in the background.",
                    tool="windows_use_task",
                    timeout_seconds=timeout,
                )

            attempted.append({**candidate.public_dict(), "ok": result["ok"]})

            # A run that never managed a single successful tool call means the
            # provider could not serve the request (quota, rate limit, network).
            # In that case fall through to the next configured provider.
            if result["ok"] or _had_successful_tool(result) or index == len(candidates) - 1:
                break

    if result is None:
        return _error(
            "No LLM provider could be started. Check the MCP server configuration.",
            tool="windows_use_task",
            attempted=attempted,
        )

    payload = {
        "task": task,
        "ok": result["ok"],
        "answer": result["answer"],
        "error": result["error"],
        "llm": settings.public_dict(),
        "attempted": attempted,
        "transcript": result["transcript"],
    }
    return _text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def _had_successful_tool(result: dict[str, Any]) -> bool:
    """True when at least one tool call succeeded during the run."""
    return any(
        step.get("kind") == "tool_result" and step.get("ok") for step in result.get("steps", [])
    )


async def _desktop_state() -> list[types.ContentBlock]:
    def _collect() -> dict[str, Any]:
        from windows_use.agent.desktop.service import Desktop

        apply_log_level()
        desktop = Desktop(use_vision=False, use_accessibility=False)
        state = desktop.get_state()
        return {
            "active_window": state.active_window_to_string(),
            "windows": state.windows_to_string(),
            "window_count": len(state.windows),
            "active_desktop": state.active_desktop_to_string(),
            "desktops": state.desktops_to_string(),
        }

    data = await _run_on_gui_thread(_collect)
    return _text(json.dumps(data, indent=2, ensure_ascii=False, default=str))


async def _accessibility_tree(args: dict) -> list[types.ContentBlock]:
    max_rows = int(args.get("max_rows") or 200)

    def _collect() -> dict[str, Any]:
        from windows_use.agent.desktop.service import Desktop

        apply_log_level()
        desktop = Desktop(use_vision=False, use_accessibility=True)
        state = desktop.get_state()
        tree_state = state.tree_state
        if tree_state is None:
            return {"ok": False, "error": "The accessibility tree could not be captured."}
        body = tree_state.interactive_elements_to_string()
        rows = body.splitlines()
        if len(rows) > max_rows + 1:
            rows = rows[: max_rows + 1] + [f"... truncated at {max_rows} rows"]
        return {
            "ok": True,
            "active_window": state.active_window.name if state.active_window else None,
            "element_count": len(tree_state.interactive_nodes or []),
            "tree": "\n".join(rows),
        }

    data = await _run_on_gui_thread(_collect)
    return _text(json.dumps(data, indent=2, ensure_ascii=False, default=str))


async def _screenshot() -> list[types.ContentBlock]:
    def _capture() -> bytes:
        from windows_use.agent.desktop.service import Desktop

        apply_log_level()
        desktop = Desktop(use_vision=False, use_accessibility=False)
        png = desktop.get_screenshot(as_bytes=True)
        return png if isinstance(png, bytes) else b""

    png = await _run_on_gui_thread(_capture)
    if not png:
        return _error("The screenshot could not be captured.", tool="windows_screenshot")
    return [
        types.ImageContent(
            type="image", data=base64.b64encode(png).decode("ascii"), mimeType="image/png"
        )
    ]


async def _status() -> list[types.ContentBlock]:
    from importlib import metadata

    def _versions() -> dict[str, Any]:
        versions: dict[str, Any] = {}
        for pkg in ("windows-use", "windows-use-mcp"):
            try:
                versions[pkg] = metadata.version(pkg)
            except metadata.PackageNotFoundError:
                versions[pkg] = None
        return versions

    versions = await asyncio.to_thread(_versions)

    try:
        llm_info: dict[str, Any] = resolve_settings().public_dict()
        llm_info["error"] = None
    except ConfigurationError as exc:
        llm_info = {"error": str(exc)}

    try:
        fallbacks = [s.public_dict() for s in fallback_settings()]
    except ConfigurationError as exc:
        fallbacks = [{"error": str(exc)}]

    payload = {"versions": versions, "llm": llm_info, "fallbacks": fallbacks}
    return _text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


server = Server(
    SERVER_NAME,
    version=SERVER_VERSION,
    instructions=(
        "Windows-Use gives you GUI-level control of the Windows desktop. Use "
        "windows_use_task for anything that needs clicking, typing or launching apps; "
        "windows_desktop_state and windows_accessibility_tree are read-only helpers for "
        "discovering what is on screen."
    ),
    on_list_tools=_on_list_tools,
    on_call_tool=_on_call_tool,
)


async def main() -> None:
    """Run the MCP server over stdio."""
    async with plain_stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
