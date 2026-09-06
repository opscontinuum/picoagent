"""complete - Tab completion for the plain REPL, sourced from the live registries.

Why a plugin can do this at all: ``PlainFrontend._readline`` calls the builtin ``input()``,
and on POSIX ``input()`` routes through GNU readline as soon as the ``readline`` module has
been imported into the process. Nothing in picoagent imports it, so the import below *is*
the mechanism. Tab stops being a literal tab character in your prompt and becomes a
completion request, with no change to the frontend.

What completes is decided from the whole line buffer, not the word alone, because ``/model
gpt`` and a bare ``gpt`` want different answers:

* ``/mo``          -> slash commands from ``rt.commands``, plus ``/skill:<name>`` entries
* ``/skill:es-``   -> skill names from ``rt.skills``
* ``/model <part>`` -> cached model ids, and the literal ``list``
* anything else    -> paths under the runtime cwd

Model ids are the awkward case. The only way to learn them is ``provider.list_models()``,
an HTTP round trip, and a Tab press must never block on the network. So the ids are cached
on disk per provider and refreshed at most once per session, after the first agent turn has
settled - a moment where the provider has demonstrably already been reached, so the refresh
adds a follow-up request rather than the first one. Tab itself only ever reads the file.

Configuration (``[plugins.complete]`` in config.toml)::

    refresh_models = true   # false: only complete model ids you have already used
    paths = true            # false: leave bare words alone entirely
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

try:
    import readline
except ImportError:                 # Windows has no readline in the stdlib; see install().
    readline = None                 # type: ignore[assignment]

log = logging.getLogger("picoagent.plugins.complete")

CACHE_FILE = "complete-models.json"
# Whitespace only, deliberately. readline's default delimiters include "/", ":" and "-",
# which would hand the completer "model" out of "/model" and "es-doctor" out of
# "/skill:es-doctor" - so the completer could not tell a command from a skill from a path.
WORD_DELIMS = " \t\n"


class ModelCache:
    """Model ids per provider, stored under the picoagent home directory.

    The session log was the other candidate (``api.append_entry``), but a fresh session
    starts with an empty log, so the cache would be cold exactly when someone opens
    picoagent to switch models. A file outside the session survives that.
    """

    def __init__(self, api: Any):
        self.api = api
        self.path = Path(api.config["_user_dir"]) / CACHE_FILE

    def read(self) -> list[str]:
        """Cached ids for the active provider, with the currently selected model folded in."""
        cached = self._load().get(self.api.rt.provider_name, [])
        return sorted({*cached, self.api.model})

    def write(self, names: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {**self._load(), self.api.rt.provider_name: sorted(set(names))}
        self.path.write_text(json.dumps(data, indent=2))

    def _load(self) -> dict[str, list[str]]:
        """A missing or corrupt cache is a cold cache, not an error worth surfacing."""
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}


class Completer:
    """Turns ``(word, line)`` into candidates. The routing is pure, so it tests without a terminal."""

    def __init__(self, api: Any, cache: ModelCache, line_buffer: Callable[[], str] | None = None):
        self.api, self.cache = api, cache
        self.paths_enabled: bool = api.plugin_config().get("paths", True)
        self._line_buffer = line_buffer or _current_line
        self._matches: list[str] = []

    # ------------------------------------------------------------------ readline protocol
    def complete(self, word: str, state: int) -> str | None:
        """Return the ``state``-th candidate for ``word``, then ``None`` once they run out.

        The whole computation sits inside the try because readline discards any exception
        raised in a completer and then shows nothing at all, which is a miserable thing to
        diagnose from the other side of a prompt. Failing closed to "no completions" looks
        identical to the user and leaves a debug log line behind.
        """
        try:
            if state == 0:
                self._matches = self.candidates(word, self._line_buffer())
        except Exception:               # noqa: BLE001 - see the docstring
            log.debug("completion failed for %r", word, exc_info=True)
            self._matches = []
        return self._matches[state] if 0 <= state < len(self._matches) else None

    # ------------------------------------------------------------------ routing
    def candidates(self, word: str, line: str) -> list[str]:
        """Pick a source from the shape of the word and the command the line starts with."""
        if word.startswith("/skill:"):
            return self._skills(word)
        if word.startswith("/"):
            return self._commands(word) + self._skills(word)
        if line.strip().split(" ")[0] == "/model":
            return self._models(word)
        return self._paths(word) if self.paths_enabled else []

    def _commands(self, word: str) -> list[str]:
        names = [f"/{command.name}" for command in self.api.rt.commands.all()]
        return [name for name in names if name.startswith(word)]

    def _skills(self, word: str) -> list[str]:
        return sorted(f"/skill:{skill.name}" for skill in self.api.rt.skills.all()
                      if f"/skill:{skill.name}".startswith(word))

    def _models(self, word: str) -> list[str]:
        """``list`` comes first because it is how you fill the cache when it is empty."""
        return [name for name in ["list", *self.cache.read()] if name.startswith(word)]

    def _paths(self, word: str) -> list[str]:
        """Complete a word against the filesystem, relative to the runtime cwd.

        An empty word returns nothing rather than dumping a directory listing into a chat
        prompt, and a word that names nothing on disk returns nothing too - which is what
        keeps Tab quiet while you are typing prose. Dotfiles appear only once you have
        typed the dot, the same bargain a shell makes.
        """
        if not word:
            return []
        head, separator, tail = word.rpartition("/")
        directory = Path(os.path.expanduser(head + separator)) if separator else Path(".")
        if not directory.is_absolute():
            directory = self.api.cwd / directory
        if not directory.is_dir():
            return []
        dotfiles_wanted = tail.startswith(".")
        names = sorted(entry.name for entry in directory.iterdir()
                       if entry.name.startswith(tail)
                       and (dotfiles_wanted or not entry.name.startswith(".")))
        return [head + separator + name + ("/" if (directory / name).is_dir() else "") for name in names]


class ModelRefresh:
    """One ``list_models()`` call per session, once the agent has gone quiet."""

    def __init__(self, api: Any, cache: ModelCache):
        self.api, self.cache, self.done = api, cache, False
        self._task: asyncio.Task | None = None

    async def on_settled(self, event: dict, rt: Any) -> None:
        """``agent_settled`` handler: schedule the fetch, never await it.

        Awaiting here would hold the REPL prompt hostage to an HTTP timeout, so the fetch
        runs as a background task and the next prompt appears immediately. ``list_models``
        is optional on the Provider protocol, hence the ``hasattr`` check rather than a
        try/except around a missing attribute.
        """
        provider = rt.providers.get(rt.provider_name)
        if self.done or not hasattr(provider, "list_models"):
            return
        self.done = True
        self._task = asyncio.create_task(self._fetch(provider))

    async def _fetch(self, provider: Any) -> None:
        try:
            self.cache.write(await provider.list_models())
        except Exception:               # noqa: BLE001 - a cold cache is not worth an error banner
            log.debug("model list refresh failed", exc_info=True)


def tab_binding() -> str:
    """The init-file line that binds Tab, for whichever readline is underneath.

    macOS ships a ``readline`` module backed by libedit, which does not understand GNU's
    ``tab: complete`` and silently leaves Tab unbound when handed it. libedit announces
    itself in ``readline.__doc__``, which is the only reliable way to tell the two apart
    at runtime.
    """
    return "bind ^I rl_complete" if "libedit" in (getattr(readline, "__doc__", "") or "") else "tab: complete"


def _current_line() -> str:
    return readline.get_line_buffer() if readline else ""


def install(api: Any) -> Completer | None:
    """Bind the completer to readline, or return ``None`` when there is no readline to bind.

    Windows has no ``readline`` in the standard library. The plugin loads there and does
    nothing, because a missing Tab key is a far smaller problem than a session that refuses
    to start over a convenience feature.
    """
    if readline is None:
        log.debug("readline is unavailable on this platform; Tab completion stays off")
        return None
    completer = Completer(api, ModelCache(api))
    readline.set_completer_delims(WORD_DELIMS)
    readline.set_completer(completer.complete)
    readline.parse_and_bind(tab_binding())
    return completer


def register(api: Any) -> None:
    completer = install(api)
    if completer and api.plugin_config().get("refresh_models", True):
        api.on("agent_settled", ModelRefresh(api, completer.cache).on_settled)
