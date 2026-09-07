"""Slash commands (``/help``, ``/model``...). Core registers a handful; plugins add the rest.

A handler is ``async def handler(args: str, runtime) -> str | None``; a returned
string is shown to the user as a notice.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

Handler = Callable[[str, Any], Awaitable[str | None]]


class _CommandSource(str):
    """The value the dispatcher stamps on the notice that carries a command's return value.

    It subclasses ``str`` so that every reader that already exists keeps working: a frontend
    plugin comparing ``payload["source"] == "command"`` still matches, and a ``--json`` run
    still renders the same field. What the subclass adds is *identity*. :data:`COMMAND_SOURCE`
    is the only instance, so a frontend deciding whether a notice may take stdout asks
    ``is COMMAND_SOURCE`` and a plugin that writes the word ``"command"`` into a payload it
    built itself produces a plain ``str`` that fails the test.

    This does not contain a plugin that goes looking, and it is not meant to: plugin code runs
    in this process and can import this name like any other, the same limit
    ``docs/security/trust-boundaries.md`` records for every other boundary around plugin code.
    What it ends is the forgery that costs nothing - the payload copied out of a docstring, the
    dict handed through from somewhere else - so that displacing the answer of a ``-p`` run
    takes a deliberate reach past the documented API rather than one extra key.
    """
    __slots__ = ()


#: Marks the ``notice`` that carries a slash command's output. That notice is what ``-p "/model
#: list"`` was asked to produce, so it goes to stdout with the answer; every other notice is
#: commentary about the session and goes to stderr. See :mod:`picoagent.frontends.base`.
COMMAND_SOURCE = _CommandSource("command")


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
