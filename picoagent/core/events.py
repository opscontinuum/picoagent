"""The event bus: how plugins observe and shape the agent's behaviour.

Why this exists
---------------
Every interesting moment in the agent's life (a prompt arrives, a tool is about
to run, the model finished a turn...) is published here as a named event with a
dictionary payload. Plugins subscribe with ``api.on(event, handler)``. This is
the *only* coupling between core and plugins, which is what keeps the core small.

The three things a handler can do
---------------------------------
1. **Observe** – return ``None``; nothing changes.
2. **Patch**   – return a ``dict``; its keys are merged into the payload and later
   handlers see the merged version. This is how a plugin rewrites tool arguments
   or a system prompt.
3. **Stop**    – return ``{"block": True, ...}`` (for blockable events such as
   ``tool_call``) or ``{"action": "handled"}`` (for ``input``). No further handlers
   run and the loop reacts accordingly.

Handlers run in registration order, i.e. plugin load order. A handler that raises
is logged and skipped so one broken plugin cannot take down the session.

See ``docs/events-reference.md`` for every event and its payload.
"""
from __future__ import annotations

import inspect
import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable

log = logging.getLogger("picoagent.events")

# A handler receives (payload, runtime) and may return a patch dict.
Handler = Callable[[dict, Any], "Awaitable[dict | None] | dict | None"]

#: Events the core itself emits. Plugins may emit their own, namespaced "plugin:event".
CORE_EVENTS = frozenset({
    "session_start", "session_end",
    "input", "before_agent_start", "context",
    "turn_start", "turn_end", "agent_end", "agent_settled",
    "message_update", "message_end",
    "tool_call", "tool_result", "tool_execution_start", "tool_execution_end",
    "user_bash", "provider_error", "model_select",
})


class EventBus:
    """Ordered, fault-tolerant publish/subscribe with payload patching."""

    def __init__(self) -> None:
        # event name -> list of (owner, handler). Owner is the plugin name, used for cleanup.
        self._handlers: dict[str, list[tuple[str, Handler]]] = defaultdict(list)

    def on(self, event: str, handler: Handler, *, owner: str = "core") -> None:
        """Subscribe ``handler`` to ``event``. ``owner`` lets a plugin's handlers be removed later."""
        self._handlers[event].append((owner, handler))

    def off_owner(self, owner: str) -> None:
        """Remove every handler registered by ``owner`` (used when a plugin is unloaded)."""
        for event in self._handlers:
            self._handlers[event] = [(o, h) for o, h in self._handlers[event] if o != owner]

    def listeners(self, event: str) -> int:
        """Number of handlers subscribed to ``event`` (handy for tests and diagnostics)."""
        return len(self._handlers.get(event, []))

    async def emit(self, event: str, payload: dict, runtime: Any = None) -> dict:
        """Run all handlers for ``event`` and return the final, possibly patched, payload.

        The returned dict is the same object that was passed in, mutated in place,
        so callers can inspect ``payload["block"]`` or ``payload["action"]`` afterwards.
        """
        for owner, handler in list(self._handlers.get(event, [])):
            patch = await self._call(owner, event, handler, payload, runtime)
            if not isinstance(patch, dict):
                continue
            payload.update(patch)
            if payload.get("block"):
                payload.setdefault("blocked_by", owner)
                break
            if payload.get("action") == "handled":
                break
        return payload

    @staticmethod
    async def _call(owner: str, event: str, handler: Handler, payload: dict, runtime: Any):
        """Invoke one handler, awaiting it if needed, and swallow (but log) its exceptions."""
        try:
            result = handler(payload, runtime)
            if inspect.isawaitable(result):
                result = await result
            return result
        except Exception as exc:  # noqa: BLE001 – plugin code is untrusted, keep the loop alive
            log.exception("handler from '%s' for event '%s' failed: %s", owner, event, exc)
            return None
