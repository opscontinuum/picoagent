"""permission-gate - the smallest useful safety layer, as a plugin.

What it does
------------
* Asks the user before running shell commands that match a "dangerous" pattern.
* Refuses to read or write protected paths (secrets, keys, ``.git`` internals).
* Adds ``/yolo [ask|yolo|readonly]`` to switch modes mid-session.
* Tells the model (via a system-prompt section) that some actions may be blocked.

Configuration (``[plugins.permission-gate]`` in config.toml)::

    mode = "ask"                              # ask | yolo | readonly   - your config only
    dangerous = ["\\brm\\s+-rf", "\\bsudo\\b"]  # regexes matched against bash commands - your config only
    protected = [".env", "**/*.pem"]           # fnmatch patterns (full path or basename)

A repository's ``.picoagent/config.toml`` may add ``protected`` patterns, which only ever
refuses more. It may not set ``mode`` or ``dangerous``: those are how the gate decides to ask,
and a gate a repository can open is not a gate. See ``docs/security/trust-boundaries.md``.
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

DEFAULT_DANGEROUS = [r"\brm\s+-[a-z]*r[a-z]*f", r"\bsudo\b", r"git\s+push\s+.*--force",
                     r"curl[^|]*\|\s*(ba)?sh", r"\bmkfs\b", r"\bdd\s+if="]
DEFAULT_PROTECTED = [".env", ".env.*", "**/*.pem", "**/id_rsa*", ".git/**"]
MUTATING_TOOLS = {"write", "edit"}
PROMPT_NOTE = ("# Safety\nSome shell commands need user confirmation and some paths are protected. "
               "If a tool call comes back blocked, explain why and propose an alternative.")


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
    def is_protected(self, path: str) -> bool:
        """True if ``path`` (as given, or its basename) matches a protected pattern."""
        return any(fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(Path(path).name, pattern)
                   for pattern in self.protected)

    def is_dangerous(self, command: str) -> bool:
        return any(pattern.search(command) for pattern in self.dangerous)

    # ------------------------------------------------------------------ handlers
    async def on_tool_call(self, event: dict, rt) -> dict | None:
        """``tool_call`` handler: return a block dict to stop the call, ``None`` to allow it."""
        name, args = event["name"], event["args"]
        if self.mode == "readonly" and name in MUTATING_TOOLS | {"shell"}:
            return self._block("read-only mode (/yolo to change)")
        if name in MUTATING_TOOLS | {"read"} and self.is_protected(args.get("path", "")):
            return self._block(f"{args.get('path')} is protected")
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
