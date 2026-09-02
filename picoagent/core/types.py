"""Plain data types shared by every module. No behaviour lives here.

The message model is deliberately provider-neutral: each provider maps these
types to its own wire format (OpenAI ``messages``, Gemini ``contents``...).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Role = Literal["user", "assistant", "tool"]


def new_id() -> str:
    """Short random id for session entries and synthetic tool-call ids."""
    return uuid.uuid4().hex[:12]


@dataclass
class ToolCall:
    """The model asked us to run ``name`` with ``args``. ``id`` ties the result back to the call."""
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class ToolResult:
    """What a tool produced. ``content`` goes to the model; ``details`` is for UIs and plugins only."""
    tool_call_id: str
    content: str
    is_error: bool = False
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class Message:
    """One turn in the conversation.

    * ``user`` messages carry ``text`` and optional ``images`` (base64 dicts with ``media_type``/``data``).
    * ``assistant`` messages carry ``text`` and any ``tool_calls`` the model made.
    * ``tool`` messages carry the ``tool_results`` for a preceding assistant message.
    ``meta`` holds bookkeeping such as token usage or a plugin's ``custom_type``.
    """
    role: Role
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        """JSON-serialisable form used by the session log."""
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Message":
        """Inverse of :meth:`to_dict`. Tolerates missing optional keys from older logs."""
        return Message(
            role=d["role"],
            text=d.get("text", ""),
            tool_calls=[ToolCall(**c) for c in d.get("tool_calls", [])],
            tool_results=[ToolResult(**r) for r in d.get("tool_results", [])],
            images=d.get("images", []),
            meta=d.get("meta", {}),
            ts=d.get("ts", 0.0),
        )


@dataclass
class ToolSpec:
    """What the model is told about a tool: name, description and a JSON-schema ``parameters`` object."""
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class StreamEvent:
    """One chunk from a streaming provider.

    ``text`` for visible output, ``thinking`` for reasoning traces, ``tool_call`` once a
    complete call has been assembled, ``done`` (with ``usage``) at the end, or ``error``.
    """
    type: Literal["text", "thinking", "tool_call", "done", "error"]
    text: str = ""
    tool_call: ToolCall | None = None
    usage: dict[str, int] = field(default_factory=dict)
    error: str = ""
