"""permission-gate - the smallest useful safety layer, as a plugin.

What it does
------------
* Asks the user before running shell commands that match a "dangerous" pattern.
* Refuses to read or write protected paths (secrets, keys, ``.git`` internals). The path is
  resolved through ``picoagent.core.tools.resolve_tool_path`` first, so the gate decides about
  the file the tool will open rather than about the way the model spelled it.
* Adds ``/yolo [ask|yolo|readonly]`` to switch modes mid-session.
* Tells the model (via a system-prompt section) that some actions may be blocked.

Configuration (``[plugins.permission-gate]`` in config.toml)::

    mode = "ask"                              # ask | yolo | readonly   - your config only
    dangerous = ["\\brm\\s+-rf", "\\bsudo\\b"]  # regexes matched against bash commands - your config only
    protected = [".env", "**/*.pem"]           # fnmatch patterns, matched against the resolved path

A repository's ``.picoagent/config.toml`` may add ``protected`` patterns, which only ever
refuses more. It may not set ``mode`` or ``dangerous``: those are how the gate decides to ask,
and a gate a repository can open is not a gate. See ``docs/security/trust-boundaries.md``.
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

from picoagent.core.tools import resolve_tool_path

DEFAULT_DANGEROUS = [r"\brm\s+-[a-z]*r[a-z]*f", r"\bsudo\b", r"git\s+push\s+.*--force",
                     r"curl[^|]*\|\s*(ba)?sh", r"\bmkfs\b", r"\bdd\s+if="]
DEFAULT_PROTECTED = [".env", ".env.*", "**/*.pem", "**/id_rsa*", ".git/**"]
MUTATING_TOOLS = {"write", "edit"}
PROMPT_NOTE = ("# Safety\nSome shell commands need user confirmation and some paths are protected. "
               "If a tool call comes back blocked, explain why and propose an alternative.")


def _spellings(path: Path) -> list[str]:
    """Every way a pattern could name ``path``: the whole path, and each trailing run of it.

    Patterns are written the way a person thinks about a repository - ``.env``, ``**/*.pem``,
    ``.git/**`` - and a resolved path is absolute, so matching only the absolute form would
    quietly retire every relative pattern in the default list and in every user's config. So
    ``/home/u/proj/.git/hooks/pre-commit`` is offered as itself, then ``proj/.git/hooks/...``,
    ``.git/hooks/pre-commit``, ``hooks/pre-commit`` and finally ``pre-commit``. ``.git/**``
    matches the third, ``.env`` and ``*.pem`` match the last - the basename check this
    replaces - and the absolute spelling the gate used to miss matches the first.

    It also means ``.git/**`` now covers a ``.git`` directory anywhere the agent can reach,
    not only the one directly under the project. That is the direction to be wrong in: this
    list only ever refuses, and a repository may add to it but never shorten it.
    """
    parts = path.parts
    return [path.as_posix()] + ["/".join(parts[i:]) for i in range(1, len(parts))]


class PermissionGate:
    """Holds the mode and the pattern lists; one instance per session."""

    def __init__(self, api):
        """Read the policy, and read it from the right layer.

        ``mode`` and ``dangerous`` decide whether this gate asks at all. A cloned repository that
        set ``mode = "yolo"`` or ``dangerous = []`` would switch off the confirmation the user
        installed this plugin for, before the user had read a line of the repository. They come
        from the user's own config only - ``api.plugin_config()`` does not carry a repository's
        values - and a repository that tried is named at session start.

        ``protected`` is the one setting a repository has something useful to say about: it knows
        that its secrets live in ``config/keys/``. It is taken as an *addition*, never a
        replacement, so the answer to "can a repository change this list" stays "only upward".
        A repository can widen what is refused; it cannot narrow it.

        Nothing here defends against ``protected = 5``. It does not have to: ``from_project``
        takes the ``[]`` below as the shape the value must have and hands back ``[]`` when the
        repository wrote something else. Wrapping it in ``list()`` here is what turned a bad
        value into a ``TypeError`` in this constructor, and a constructor that raises takes the
        whole gate with it - the repository could not empty the list, so it deleted the gate.
        """
        cfg = api.plugin_config()
        api.warn_about_project_config("protected")
        self.api = api
        self.mode: str = cfg.get("mode", "ask")
        self.dangerous = [re.compile(p) for p in cfg.get("dangerous", DEFAULT_DANGEROUS)]
        self.protected: list[str] = (list(cfg.get("protected", DEFAULT_PROTECTED))
                                     + cfg.from_project("protected", []))

    # ------------------------------------------------------------------ policy
    def is_protected(self, path: Path) -> bool:
        """True if the file at ``path`` matches a protected pattern.

        ``path`` is resolved - what :func:`resolve_tool_path` says the tool will open - and not
        the string the model wrote. Matching the string was a bypass by respelling:
        ``.git/hooks/pre-commit`` was refused while ``<project>/.git/hooks/pre-commit``,
        ``./.git/config`` and ``@.git/config`` all named the same files and were allowed, so a
        commit hook could be planted through the gate that exists to stop exactly that.
        """
        return any(fnmatch.fnmatch(spelling, pattern)
                   for spelling in _spellings(path) for pattern in self.protected)

    def is_dangerous(self, command: str) -> bool:
        return any(pattern.search(command) for pattern in self.dangerous)

    # ------------------------------------------------------------------ handlers
    async def on_tool_call(self, event: dict, rt) -> dict | None:
        """``tool_call`` handler: return a block dict to stop the call, ``None`` to allow it."""
        name, args = event["name"], event["args"]
        if self.mode == "readonly" and name in MUTATING_TOOLS | {"shell"}:
            return self._block("read-only mode (/yolo to change)")
        raw = args.get("path")
        if name in MUTATING_TOOLS | {"read"} and isinstance(raw, str) and raw:
            # rt.cfg is the dictionary the tool will get as ctx.config, so the gate and the
            # tool resolve the same string the same way - the point of the seam. An empty or
            # non-string path is left alone: it resolves to the project directory, which every
            # directory pattern would then match, and the tool answers a missing argument
            # itself with a message the model can act on.
            if self.is_protected(resolve_tool_path(raw, rt.cfg).path):
                return self._block(f"{raw} is protected")
        if name == "shell" and self.mode == "ask" and self.is_dangerous(args.get("command", "")):
            ui = self.api.ui
            allowed = await ui.ask("confirm", f"Run dangerous command?\n  {args['command']}") if ui else False
            if not allowed:
                return self._block("user declined")
        return None

    async def on_yolo(self, argstr: str, rt) -> str:
        """``/yolo`` toggles ask<->yolo; ``/yolo readonly`` (or any mode name) sets it explicitly."""
        requested = argstr.strip()
        self.mode = requested or ("yolo" if self.mode == "ask" else "ask")
        return f"permission mode: {self.mode}"

    @staticmethod
    def _block(reason: str) -> dict:
        return {"block": True, "reason": reason}


def register(api):
    api.declare_required("this session's only check on destructive commands and protected paths")
    gate = PermissionGate(api)
    api.on("tool_call", gate.on_tool_call)
    api.register_command("yolo", gate.on_yolo, "toggle permission mode (ask|yolo|readonly)")
    api.register_system_prompt_section("permission-gate", lambda: PROMPT_NOTE)
