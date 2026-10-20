"""Helpers that run the Windows-Use agent and normalise its output.

Everything in this module is synchronous and is meant to be executed on a
worker thread - the agent blocks on UI Automation calls, COM and subprocesses.

The upstream package is imported lazily inside the functions: importing it
costs ~15 s (it eagerly imports every LLM provider SDK), and that delay must
not sit in front of the MCP handshake.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from .logging_setup import apply_log_level

#: Max characters kept per tool result in the trace.
TOOL_RESULT_CHARS = 600

#: Browser name -> the value of the upstream ``Browser`` enum.
BROWSER_ALIASES: dict[str, str] = {
    "edge": "msedge",
    "msedge": "msedge",
    "chrome": "chrome",
    "firefox": "firefox",
}

MODES = ("normal", "flash")


def resolve_browser(name: str | None) -> str:
    """Map a user supplied browser name onto the upstream ``Browser`` value."""
    if not name:
        return BROWSER_ALIASES["edge"]
    key = name.strip().lower()
    if key in BROWSER_ALIASES:
        return BROWSER_ALIASES[key]
    raise ValueError(f"Unknown browser '{name}'. Use one of: {', '.join(sorted(BROWSER_ALIASES))}.")


def _truncate(value: Any, limit: int = TOOL_RESULT_CHARS) -> str:
    text = value if isinstance(value, str) else str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [{len(text) - limit} more chars]"


class AgentTrace:
    """Collects the events emitted by an agent run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.steps: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.answer: str | None = None

    def __call__(self, event: Any) -> None:
        """Collect one agent event. ``event.type`` is a str enum upstream."""
        data = event.data
        kind = getattr(event.type, "value", event.type)
        with self._lock:
            if kind == "thought":
                self.steps.append(
                    {
                        "step": data.get("step"),
                        "kind": "thought",
                        "text": _truncate(data.get("thought", "")),
                    }
                )
            elif kind == "tool_call":
                self.steps.append(
                    {
                        "step": data.get("step"),
                        "kind": "tool_call",
                        "tool": data.get("tool_name"),
                        "params": data.get("tool_params"),
                    }
                )
            elif kind == "tool_result":
                self.steps.append(
                    {
                        "step": data.get("step"),
                        "kind": "tool_result",
                        "tool": data.get("tool_name"),
                        "ok": data.get("is_success"),
                        "content": _truncate(data.get("content", "")),
                    }
                )
            elif kind == "done":
                self.answer = data.get("content")
            elif kind == "error":
                detail = _truncate(data.get("error", "unknown error"))
                error_type = data.get("error_type")
                if error_type:
                    detail = f"[{error_type}] {detail}"
                self.errors.append(detail)

    def render(self) -> str:
        """Human readable transcript of the run."""
        lines: list[str] = []
        for step in self.steps:
            kind = step["kind"]
            if kind == "thought":
                lines.append(f"[step {step['step']}] thought: {step['text']}")
            elif kind == "tool_call":
                params = json.dumps(step["params"], ensure_ascii=False, default=str)
                lines.append(f"[step {step['step']}] -> {step['tool']}({params})")
            elif kind == "tool_result":
                status = "ok" if step["ok"] else "failed"
                lines.append(
                    f"[step {step['step']}] <- {step['tool']} [{status}]: {step['content']}"
                )
        for err in self.errors:
            lines.append(f"[error] {err}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "errors": self.errors,
            "answer": self.answer,
        }


def run_agent_task(
    task: str,
    llm,
    *,
    max_steps: int = 25,
    use_vision: bool = False,
    instructions: list[str] | None = None,
    browser: str | None = None,
    mode: str = "normal",
    experimental: bool = False,
    auto_minimize: bool = False,
    max_consecutive_failures: int = 3,
) -> dict[str, Any]:
    """Run a single GUI task and return a structured result.

    The agent is created inside this function on purpose: UI Automation binds to
    the COM apartment of the thread that initialises it, so the agent must be
    built and used on the same worker thread.
    """
    from windows_use import Agent
    from windows_use.agent.desktop.views import Browser

    # The upstream package re-enables DEBUG on import - clamp it again.
    apply_log_level()

    browser_value = resolve_browser(browser)
    browser_enum = Browser(browser_value)

    trace = AgentTrace()
    agent = Agent(
        llm=llm,
        mode=mode if mode in MODES else "normal",
        browser=browser_enum,
        use_vision=use_vision,
        instructions=list(instructions or []),
        max_steps=max_steps,
        max_consecutive_failures=max_consecutive_failures,
        event_subscriber=trace,
        log_to_console=False,
        experimental=experimental,
        auto_minimize=auto_minimize,
    )

    # ``Agent.invoke`` swallows exceptions and returns None in that case.
    result = agent.invoke(task=task)

    if result is None:
        return {
            "ok": False,
            "answer": trace.answer,
            "error": "; ".join(trace.errors) or "The agent terminated without a result.",
            "transcript": trace.render(),
            "steps": trace.steps,
        }

    return {
        "ok": bool(result.is_done),
        "answer": result.content if result.content is not None else trace.answer,
        "error": result.error,
        "transcript": trace.render(),
        "steps": trace.steps,
    }
