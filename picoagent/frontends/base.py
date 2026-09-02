"""The Frontend protocol - how the core talks to a user interface.

The core never prints. It emits *semantic* events and asks questions; whatever is
registered as the frontend decides how to show them. That keeps the loop testable
and lets a plugin swap the plain REPL for a full TUI, a JSON-RPC bridge, or an
HTTP server without touching core code.

Events the core emits (payload keys in brackets):

    user_message     [text]            the prompt that was just submitted
    assistant_start  []                a model reply is starting
    assistant_delta  [text]            streamed text
    thinking_delta   [text]            streamed reasoning (if the provider exposes it)
    assistant_end    [message]         the full Message
    tool_start       [call]            a ToolCall is about to run
    tool_result      [call, result]    its ToolResult (also emitted for blocked calls)
    notice           [text]            informational text (command output etc.)
    error            [text]            something went wrong
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Frontend(Protocol):
    async def emit(self, event: str, payload: dict) -> None:
        """Render one event."""

    async def ask(self, kind: str, prompt: str, **kw: Any) -> Any:
        """Ask the user something. ``kind`` is ``confirm`` (-> bool), ``select`` (``options=[...]`` -> choice)
        or ``input`` (-> str). Headless frontends should return a safe default (False/None)."""

    async def read_input(self) -> str | None:
        """Next line from the user, or ``None`` to end the session."""

    async def run(self, agent: Any) -> None:
        """Drive an interactive session: read input, hand it to ``agent.handle_input``, repeat."""
