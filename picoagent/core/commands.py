"""Slash commands (``/help``, ``/model``...). Core registers a handful; plugins add the rest.

A handler is ``async def handler(args: str, runtime) -> str | None``; a returned
string is shown to the user as a notice.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

Handler = Callable[[str, Any], Awaitable[str | None]]


@dataclass
class Command:
    name: str
    handler: Handler
    description: str = ""
    owner: str = "core"


class CommandRegistry:
    def __init__(self) -> None:
        self._commands: dict[str, Command] = {}

    def register(self, name: str, handler: Handler, description: str = "", owner: str = "core") -> None:
        """Add ``/name``; re-registering replaces (plugins can override core commands)."""
        self._commands[name] = Command(name, handler, description, owner)

    def get(self, name: str) -> Command | None:
        return self._commands.get(name)

    def all(self) -> list[Command]:
        return sorted(self._commands.values(), key=lambda c: c.name)

    def parse(self, text: str) -> tuple[Command, str] | None:
        """Return ``(command, args)`` if ``text`` invokes a known command, else ``None``.

        ``/skill:...`` is intentionally not a command; the skill registry handles it.
        """
        stripped = text.strip()
        if not stripped.startswith("/") or stripped.startswith("/skill:"):
            return None
        name, _, args = stripped[1:].partition(" ")
        command = self.get(name)
        return (command, args.strip()) if command else None
