"""The Frontend protocol - how the core talks to a user interface.

The core never prints. It emits *semantic* events and asks questions; whatever is
registered as the frontend decides how to show them. That keeps the loop testable
and lets a plugin swap the plain REPL for a full TUI, a JSON-RPC bridge, or an
HTTP server without touching core code.

Events the core emits (payload keys in brackets):

    user_message     [text, kind]      a user-role message going to the model. ``kind`` is
                                       ``typed`` (the person's own prompt), ``queued`` (text a
                                       plugin sent for delivery) or ``injected`` (text a plugin
                                       added at ``before_agent_start``); the last two speak in
                                       the user's voice, so they are announced rather than
                                       reaching the model unseen
    assistant_start  []                a model reply is starting
    assistant_delta  [text]            streamed text
    thinking_delta   [text]            streamed reasoning (if the provider exposes it)
    assistant_end    [message]         the full Message
    tool_start       [call]            a ToolCall is about to run
    tool_result      [call, result]    its ToolResult (also emitted for blocked calls)
    notice           [text, source?]   informational text. ``source`` is ``command`` when the text
                                       is a slash command's output, which is the answer in a
                                       ``-p`` run; without it the notice is commentary about the
                                       session and headless runs keep it off stdout. It reads as
                                       the string ``"command"``, but the dispatcher stamps
                                       :data:`~picoagent.core.commands.COMMAND_SOURCE` and a
                                       frontend that routes on it should compare with ``is``:
                                       a plugin's payload can carry the word, not the object
    error            [text]            something went wrong
    plugin_skipped   [name, reason, root, urgent, text]
                                       one plugin did not load, at startup; ``urgent`` marks
                                       one the user approved that is now not running
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Frontend(Protocol):
    async def emit(self, event: str, payload: dict) -> None:
        """Render one event."""

    async def ask(self, kind: str, prompt: str, **kw: Any) -> Any:
        """Ask the user something. ``kind`` is ``confirm`` (-> bool), ``select`` (``options=[...]`` -> choice)
        or ``input`` (-> str). ``input`` also takes ``secret=True``, which asks a frontend not to
        echo what is typed; a frontend that cannot honour it still has to answer the question.
        Headless frontends should return a safe default (False/None)."""

    async def read_input(self) -> str | None:
        """Next line from the user, or ``None`` to end the session."""

    async def run(self, agent: Any) -> None:
        """Drive an interactive session: read input, hand it to ``agent.handle_input``, repeat."""
