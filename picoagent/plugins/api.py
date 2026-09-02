"""``PluginAPI`` - the one object a plugin receives, and the only surface it should depend on.

A plugin is a module exposing ``def register(api: PluginAPI) -> None``. Inside it you
subscribe to events, register tools/commands/providers, or swap the frontend. Every
``register_*`` call with an existing name **replaces** the previous registration,
which is how plugins override built-ins.

Handlers, commands and tools registered here are tagged with the plugin's name so
they can be listed and, later, unloaded.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from ..core.loop import Runtime
from ..core.skills import Skill


class PluginAPI:
    def __init__(self, rt: Runtime, name: str, root: Path):
        self.rt, self.name, self.root = rt, name, root

    # ------------------------------------------------------------------ events
    def on(self, event: str, handler: Callable) -> None:
        """Subscribe to a lifecycle event. See ``docs/events-reference.md``."""
        self.rt.events.on(event, handler, owner=self.name)

    async def emit(self, event: str, payload: dict) -> dict:
        """Publish a plugin-specific event as ``"<plugin>:<event>"`` for other plugins."""
        return await self.rt.events.emit(f"{self.name}:{event}", payload, self.rt)

    # ------------------------------------------------------------------ registration
    def register_tool(self, tool: Any) -> None:
        """Expose a tool to the model. Same name as a built-in => override it."""
        self.rt.tools.register(tool, owner=self.name)

    def unregister_tool(self, name: str) -> None:
        self.rt.tools.unregister(name)

    def register_command(self, name: str, handler: Callable, description: str = "") -> None:
        """Add ``/name``. ``handler(args: str, runtime) -> str | None``."""
        self.rt.commands.register(name, handler, description, owner=self.name)

    def register_provider(self, provider: Any) -> None:
        """Add a model provider; select it with ``--provider <name>`` or ``api.set_model``."""
        self.rt.providers.register(provider)

    def register_frontend(self, frontend: Any) -> None:
        """Replace the user interface (e.g. with a TUI or an RPC server)."""
        self.rt.frontend = frontend

    def register_system_prompt_section(self, section: str, render: Callable[[], str]) -> None:
        """Add or replace a named block of the system prompt. ``render`` runs every turn."""
        self.rt.prompt.set_section(section, render)

    def register_skill(self, skill: Skill) -> None:
        self.rt.skills.add(skill)

    # ------------------------------------------------------------------ runtime control
    def set_active_tools(self, names: list[str] | None) -> None:
        """Limit the tools the model sees (``None`` = all). Enables plan/read-only modes
        and deferred tool loading."""
        self.rt.tools.set_active(names)

    def get_active_tools(self) -> list[str]:
        return [t.name for t in self.rt.tools.active()]

    def all_tools(self) -> list[str]:
        return self.rt.tools.names()

    async def set_model(self, model: str, provider: str | None = None) -> None:
        """Switch model (and optionally provider) for subsequent turns; emits ``model_select``."""
        previous = self.rt.model
        self.rt.model = model
        if provider:
            self.rt.provider_name = provider
        await self.rt.events.emit("model_select", {"model": model, "previous": previous,
                                                   "provider": self.rt.provider_name}, self.rt)

    def set_thinking(self, level: str) -> None:
        """``off`` | ``low`` | ``medium`` | ``high``."""
        self.rt.thinking = level

    def send_message(self, text: str, deliver_as: str = "steer") -> None:
        """Queue a user-role message for the model.

        ``steer``: delivered after the current tool batch (mid-run nudge).
        ``follow_up``: a new prompt once the agent finishes.
        ``next_turn``: bundled with the user's next prompt.
        """
        if deliver_as not in ("steer", "follow_up", "next_turn"):
            raise ValueError(f"unknown deliver_as {deliver_as!r}")
        self.rt.queue.append((deliver_as, text))

    def append_entry(self, custom_type: str, data: Any) -> None:
        """Persist plugin state in the session log (survives restarts, never sent to the model)."""
        self.rt.session.append_custom(custom_type, data)

    def entries(self, custom_type: str):
        """Iterate previously persisted entries of ``custom_type`` on the active branch."""
        return self.rt.session.custom(custom_type)

    async def exec(self, cmd: str, *args: str, timeout: float = 60) -> tuple[int, str]:
        """Run a subprocess in the project dir; returns ``(exit_code, combined_output)``."""
        proc = await asyncio.create_subprocess_exec(cmd, *args, cwd=self.rt.cwd,
                                                    stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        output, _ = await asyncio.wait_for(proc.communicate(), timeout)
        return proc.returncode or 0, output.decode(errors="replace")

    # ------------------------------------------------------------------ context
    @property
    def cwd(self) -> Path:
        return self.rt.cwd

    @property
    def config(self) -> dict:
        """The whole effective config. Prefer :meth:`plugin_config` for your own settings."""
        return self.rt.cfg

    @property
    def session(self):
        return self.rt.session

    @property
    def ui(self):
        """The active frontend: ``await api.ui.ask("confirm", "...")``, ``await api.ui.emit("notice", {...})``.
        May be a headless frontend that answers ``False``/``None``."""
        return self.rt.frontend

    @property
    def model(self) -> str:
        return self.rt.model

    def is_idle(self) -> bool:
        return self.rt.is_idle()

    def abort(self) -> None:
        """Cancel the current run at the next safe point."""
        self.rt.abort.set()

    def plugin_config(self) -> dict:
        """Settings from ``[plugins.<this plugin>]`` in config.toml (empty dict if absent)."""
        return self.rt.cfg.get("plugins", {}).get(self.name, {})
