"""``PluginAPI`` - the one object a plugin receives, and the only surface it should depend on.

A plugin is a module exposing ``def register(api: PluginAPI) -> None``. Inside it you
subscribe to events, register tools/commands/providers, or swap the frontend. Every
``register_*`` call with an existing name **replaces** the previous registration,
which is how plugins override built-ins.

Handlers, commands and tools registered here are tagged with the plugin's name so
they can be listed and, later, unloaded.

One thing to know before reading settings: :meth:`PluginAPI.plugin_config` answers with the
user's config layers, not with a repository's. A repository's ``[plugins.<name>]`` values are
kept apart and taken only when a plugin names them. See :class:`picoagent.core.config.PluginConfig`.

One thing to know before spawning anything: :func:`minimal_env` is the environment a child gets,
and :meth:`PluginAPI.exec` uses it. See its docstring for the rule and for the one exception the
tree makes.
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Callable

from ..core.config import PluginConfig, plugin_config
from ..core.events import warn_if_unpublished
from ..core.loop import Runtime
from ..core.skills import Skill
from ..core.tools import kill_process_tree, own_process_group

#: Seconds the child of a timed-out :meth:`PluginAPI.exec` gets to exit on its own before it is
#: killed. Long enough for a command interrupted mid-write to undo what it started, short enough
#: that the plugin waiting on the call still gets an answer this turn.
CHILD_EXIT_GRACE: float = 5.0

#: The exit code a timed-out :meth:`PluginAPI.exec` reports. 124 is what ``timeout(1)`` uses, so a
#: plugin that passes the code on says the same thing as the shell the user would have typed.
TIMED_OUT_EXIT_CODE: int = 124

#: What a child process needs to *be a runnable program*, on either platform, and nothing that
#: identifies you to a service. Each name earns its place by breaking something when absent:
#:
#: * ``PATH`` - passing ``env`` makes it the search path ``subprocess`` uses to find the command
#:   itself, so a child without it cannot be started at all unless it was named absolutely.
#: * ``HOME`` / ``USERPROFILE`` - where a runtime keeps its caches and its own config. A node or
#:   python child without one writes to ``/`` or refuses to start.
#: * ``SystemRoot`` and its neighbours - Windows platform, not preference. Winsock loads its
#:   provider DLLs by way of ``SystemRoot``, so a child that opens a socket fails without it, and
#:   the failure names a DLL rather than an environment variable.
#: * ``LANG`` / ``LC_*`` - the pipe is UTF-8 by contract; a child that falls back to ASCII
#:   mangles every non-ASCII character in a tool result.
#: * ``TZ``, ``TMPDIR`` / ``TEMP`` / ``TMP`` - a timestamp and a scratch directory.
#:
#: Everything else, including every credential and every ``*_TOKEN`` a shell exports, is left
#: behind. This is an allowlist for the same reason ``credential-guard``'s is: a denylist of
#: secret-shaped names cannot be complete, and ``DATABASE_URL`` is the standing proof.
MINIMAL_ENV_NAMES: frozenset[str] = frozenset({
    # POSIX
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR",
    # Windows: a child cannot start, resolve a command, or open a socket without these.
    "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "SYSTEMDRIVE", "TEMP", "TMP", "USERPROFILE",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "OS",
})


def minimal_env(extra: dict[str, str] | None = None, pass_env: Sequence[str] = (),
                base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment picoagent gives a child process: :data:`MINIMAL_ENV_NAMES`, then
    ``pass_env`` names lifted from ``base``, then ``extra`` written over the top.

    The rule this exists to hold: **a child picoagent spawns starts from a minimal environment,
    and anything more is named by the user, one variable at a time.** Inheriting ``os.environ``
    is the opposite default and it is how a credential reaches a program the model influences -
    an MCP server runs arguments the model chose, and a plugin's subprocess writes its output
    into a tool result that goes back to the model on the next turn.

    ``base`` is ``os.environ`` unless a caller passes its own, which is what lets a test check
    the Windows half of the answer from Linux. A name is looked up exactly first and
    case-insensitively second, because Windows spells ``SystemRoot`` several ways and POSIX
    treats two spellings as two variables.
    """
    source = os.environ if base is None else base
    by_upper: dict[str, tuple[str, str]] = {}
    for key, value in source.items():
        by_upper.setdefault(key.upper(), (key, value))

    built: dict[str, str] = {}
    for name in (*MINIMAL_ENV_NAMES, *pass_env):
        if name in source:
            built[name] = source[name]
        elif name.upper() in by_upper:
            key, value = by_upper[name.upper()]
            built[key] = value
    built.update({str(key): str(value) for key, value in (extra or {}).items()})
    return built


class PluginAPI:
    """Everything a plugin may do, on one object, grouped by the banner comments below.

    It is wide on purpose, and the width has been argued rather than left alone. Split into a
    protocol per group - registration, runtime control, config, process execution - a plugin
    that registers one tool would no longer carry methods it never calls, which is what
    interface segregation asks for. It would also cost the two things this object is for.

    The first is that nothing here is *implemented* by anybody else. Plugins are callers, not
    implementers: there is one concrete class, the tests drive the real one over a real
    ``Runtime``, and no fake exists to keep in step. The cost a wide interface usually imposes
    falls on implementers, and there are none. What a caller depends on is the attribute it
    names, so a plugin calling ``api.register_tool`` is not coupled to ``api.exec`` in any sense
    that a split would relieve.

    The second is that this is the documented surface for people writing plugins, and
    ``docs/plugin-authoring.md`` teaches it as one object: everything you need is on ``api``.
    Assembling four protocols before writing a five-line plugin is worse for them, and keeping
    the flat spelling working *on top of* a split means a delegating facade - more code, an
    extra hop for every call, and every shipped plugin rewritten to gain nothing measurable.

    What the grouping is really about is reading, and that is what the banner comments answer.
    The methods that would form the split are the methods that sit together.

    What the object does earn, past being a pass-through: every registration is tagged with the
    plugin's name without the plugin having to know its own, ``emit`` namespaces the event,
    ``on`` warns about a name nobody publishes, ``send_message`` refuses an unknown delivery,
    and ``exec`` supplies the minimal environment and the process-group cleanup. A plugin
    reaching ``rt`` directly gets none of that, which is the argument for the facade existing
    at all and is unrelated to how wide it is.
    """

    def __init__(self, rt: Runtime, name: str, root: Path):
        self.rt, self.name, self.root = rt, name, root
        self.required_reason: str | None = None

    def declare_required(self, reason: str) -> None:
        """Say that this session should not run without this plugin.

        The loader's default is to catch whatever ``register()`` raises, note the plugin as
        skipped, and carry on. That is right for a plugin that adds a convenience and wrong for
        one that provides a control: a session missing its permission gate looks exactly like a
        session whose permission gate allowed everything. The loader cannot tell those plugins
        apart, so the plugin says so, and a failure after this call stops startup instead.

        Call it as the first statement in ``register()``. A failure before it - a syntax error,
        a missing import - is still a plain skip, because nothing has said otherwise yet. That
        is the honest limit of a declaration made in code the declaration is meant to protect.
        """
        self.required_reason = reason

    # ------------------------------------------------------------------ events
    def on(self, event: str, handler: Callable) -> None:
        """Subscribe to a lifecycle event. See ``docs/events-reference.md``.

        A name that is neither a core event nor namespaced ``<plugin>:<event>`` is warned about
        and then subscribed anyway - see :func:`~picoagent.core.events.warn_if_unpublished` for
        why it is a warning and not a refusal.
        """
        warn_if_unpublished(event, self.name)
        self.rt.events.on(event, handler, owner=self.name)

    async def emit(self, event: str, payload: dict) -> dict:
        """Publish a plugin-specific event as ``"<plugin>:<event>"`` for other plugins."""
        return await self.rt.events.emit(f"{self.name}:{event}", payload, self.rt)

    # ------------------------------------------------------------------ registration
    def register_tool(self, tool: Any) -> None:
        """Expose a tool to the model. Same name as a built-in => override it."""
        self.rt.tools.register(tool, owner=self.name)

    def unregister_tool(self, name: str) -> None:
        """Take a tool out of the registry. Unknown name => nothing happens.

        Stronger than :meth:`set_active_tools`, which only narrows what the *model* is offered:
        a hidden tool is still registered, so another plugin reaches it through
        ``rt.tools.get(name)`` and runs it. This one removes it for everybody, which is what a
        plugin that must not merely shadow a tool - it must not leave it reachable - needs.
        """
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

    def remove_system_prompt_section(self, section: str) -> None:
        """Take a named block out of the system prompt. Unknown name => nothing happens.

        The other half of :meth:`register_system_prompt_section`, and here for the reason
        :meth:`unregister_tool` is: a plugin that adds something needs a way to take it back,
        and a surface that only registers makes a plugin reach past this object for the undo.

        Not the same as rendering an empty string. ``build()`` skips an empty section, so the
        prompt reads alike either way today, but a blanked section is still registered: it is
        still called every turn, still there for a plugin walking the sections, and still the
        entry another plugin's replacement would land on.
        """
        self.rt.prompt.remove_section(section)

    def register_skill(self, skill: Skill) -> None:
        self.rt.skills.add(skill)

    # ------------------------------------------------------------------ runtime control
    def set_active_tools(self, names: list[str] | None) -> None:
        """Limit the tools the model sees (``None`` = all). Enables plan/read-only modes
        and deferred tool loading."""
        self.rt.tools.set_active(names)

    def get_active_tools(self) -> list[str]:
        """The names currently offered to the model; :meth:`all_tools` is everything registered.

        The read half of :meth:`set_active_tools`, and worth using rather than assuming: the
        setter replaces the whole list, so a plugin that only wants ``shell`` gone subtracts
        from this rather than naming the tools it happens to know about, and another plugin's
        narrowing survives.
        """
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
        ``next_turn``: its own message, immediately before the user's next prompt.

        Every kind has a draining site in :class:`~picoagent.core.loop.AgentLoop`, so queued text
        always reaches the model. A ``steer`` is the one with a timing caveat: queued from the
        final ``turn_end`` there is no tool batch left to follow, and it is carried to the next
        prompt with the ``next_turn`` text. Queue it only when the guidance still reads sensibly
        one prompt later.
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

    async def exec(self, cmd: str, *args: str, timeout: float = 60,
                   env: dict[str, str] | None = None) -> tuple[int, str]:
        """Run a subprocess in the project dir; returns ``(exit_code, combined_output)``.

        The child starts from :func:`minimal_env`, not from ``os.environ``, and ``env`` is
        merged over that: ``await api.exec("gh", "pr", "list", env={"GH_TOKEN": token})`` is how
        a command gets a credential, and the call site is then the place that says which one.

        The same rule as an MCP server, for the same reason rather than by analogy. The output
        of this command is a plugin's to do what it likes with, and what plugins do with it is
        put it in a tool result or a notice, both of which reach the model and the session log.
        A ``git status`` or a ``docker ps`` needs ``PATH`` and ``HOME`` and nothing else; a
        command that genuinely needs more is a command whose author can name what.

        Running out of ``timeout`` is a result rather than an exception: the child's whole tree
        is ended and reaped, and you get ``(124, "timed out after <n>s")``. Nothing of what it
        printed survives - the read was cancelled with it - so there is nothing truthful to put
        in place of that sentence.
        """
        proc = await asyncio.create_subprocess_exec(cmd, *args, cwd=self.rt.cwd,
                                                    env=minimal_env(env),
                                                    stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT,
                                                    # its own group, so the cleanup below reaches
                                                    # the children a `sh -c` child started too
                                                    **own_process_group())
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            # `wait_for` cancels the read and nothing else: without this the child runs on with
            # nobody left to wait for it, which is an orphan the session cannot report or reap.
            await kill_process_tree(proc, grace=CHILD_EXIT_GRACE)
            return TIMED_OUT_EXIT_CODE, f"timed out after {timeout}s"
        except asyncio.CancelledError:
            # An aborted run leaves the same orphan, and the child is now in a group of its own,
            # so the terminal's Ctrl-C no longer reaches it either. Clean up, then let the
            # cancellation carry on being one.
            await kill_process_tree(proc, grace=CHILD_EXIT_GRACE)
            raise
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

    def plugin_config(self) -> PluginConfig:
        """Settings from ``[plugins.<this plugin>]`` in config.toml (empty if absent).

        Reads as a dict of the **user's** layers. A repository's ``[plugins.<this plugin>]``
        values sit beside them and are reachable by name through ``.from_project(key)`` or
        ``.with_project(*keys)``, and ``.source(key)`` says which layer a value came from.
        Both readers take the default you pass as the shape the repository's value must have,
        so a mistyped project value costs you the value and not your ``register()``.
        See :class:`picoagent.core.config.PluginConfig` for why that is the default direction.
        """
        return plugin_config(self.rt.cfg, self.name)

    def warn_about_project_config(self, *accepted: str) -> None:
        """At session start, name the ``[plugins.<this plugin>]`` keys this repository set and
        this plugin did not take. Pass the keys you deliberately accept from a repository.

        Refusing a value and saying nothing leaves two people confused: whoever wrote the project
        config wonders why it did nothing, and whoever cloned the repository never learns it tried
        to move an endpoint or start a process. Both need to see the same line.

        An *accepted* key that arrived with the wrong type is reported here too. It is refused
        deeper down, by :class:`~picoagent.core.config.PluginConfig`, and a plugin author who
        never thinks about types still gets the announcement: the message is collected in the
        config layer and read back at session start, after ``register()`` has done its reading.
        """
        ignored = [key for key in self.plugin_config().project_keys() if key not in accepted]

        async def announce(event: dict, rt) -> None:
            if not rt.frontend:
                return
            lines = []
            if ignored:
                lines.append(f"{self.name}: ignored {', '.join(ignored)} from this repository's "
                             ".picoagent/config.toml - these are read from your own config only.")
            lines += [f"{self.name}: {message}" for message in self.plugin_config().refusals]
            if lines:
                await rt.frontend.emit(
                    "notice", {"text": "\n".join(lines + ["See docs/security/trust-boundaries.md"])})
        self.on("session_start", announce)
