"""permission-gate - the smallest useful safety layer, as a plugin.

What it does
------------
* Asks the user before running shell commands that match a "dangerous" pattern.
* Refuses to read or write protected paths (secrets, keys, ``.git`` internals).
* Adds ``/yolo [ask|yolo|readonly]`` to switch modes mid-session.
* Tells the model (via a system-prompt section) that some actions may be blocked.

Configuration (``[plugins.permission-gate]`` in config.toml)::

    mode = "ask"                              # ask | yolo | readonly
    dangerous = ["\\brm\\s+-rf", "\\bsudo\\b"]  # regexes matched against bash commands
    protected = [".env", "**/*.pem"]           # fnmatch patterns (full path or basename)
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
        cfg = api.plugin_config()
        self.api = api
        self.mode: str = cfg.get("mode", "ask")
        self.dangerous = [re.compile(p) for p in cfg.get("dangerous", DEFAULT_DANGEROUS)]
        self.protected: list[str] = cfg.get("protected", DEFAULT_PROTECTED)

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
    gate = PermissionGate(api)
    api.on("tool_call", gate.on_tool_call)
    api.register_command("yolo", gate.on_yolo, "toggle permission mode (ask|yolo|readonly)")
    api.register_system_prompt_section("permission-gate", lambda: PROMPT_NOTE)
