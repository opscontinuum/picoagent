"""Tools: the protocol, the registry, and the four built-ins the model gets by default.

Design notes
------------
* A tool is any object with ``name``, ``description``, ``parameters`` (JSON schema) and an
  async ``execute(args, ctx) -> ToolResult``. No base class is required.
* Tools **never raise** for expected failures (missing file, non-zero exit); they return a
  ``ToolResult`` with ``is_error=True`` so the model can recover. Unexpected exceptions are
  caught by the loop and reported the same way.
* Output is truncated (default 50 KB / 2000 lines) and the full text spilled to a temp file,
  because an unbounded tool result is the fastest way to blow the context window.
* Tools that mutate files take a per-file lock so parallel tool calls can't lose updates.
* Plugins replace a built-in by registering a tool with the same name (last write wins).
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import signal
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .types import ToolResult, ToolSpec

log = logging.getLogger("picoagent.tools")


@dataclass
class ToolContext:
    """Everything a tool may need at execution time."""
    cwd: Path                 # project root; relative paths resolve against it
    config: dict              # effective config (truncation limits, timeouts...)
    tool_call_id: str         # echoed back in the ToolResult
    abort: asyncio.Event      # set when the user cancels; long tools should poll it
    ui: Any = None            # Frontend for ask()/emit(), or None in headless mode
    extra: dict = field(default_factory=dict)   # scratch space for plugins


@runtime_checkable
class Tool(Protocol):
    """Structural interface every tool satisfies."""
    name: str
    description: str
    parameters: dict[str, Any]

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult: ...


# --------------------------------------------------------------------------- helpers

def truncate(text: str, max_bytes: int, max_lines: int, keep: str = "head") -> tuple[str, bool]:
    """Cut ``text`` to the limits, keeping the ``head`` or the ``tail``.

    Returns ``(text, was_truncated)``. Use ``head`` for file reads and search results
    (the beginning matters) and ``tail`` for command output (the end matters).
    """
    lines = text.splitlines(keepends=True)
    if len(lines) <= max_lines and len(text.encode()) <= max_bytes:
        return text, False
    selected = lines[:max_lines] if keep == "head" else lines[-max_lines:]
    out = "".join(selected)
    raw = out.encode()
    if len(raw) > max_bytes:
        raw = raw[:max_bytes] if keep == "head" else raw[-max_bytes:]
        out = raw.decode(errors="ignore")
    return out, True


def spill_to_tempfile(text: str) -> str:
    """Write ``text`` to a temp file and return its path (so the model can grep the full output)."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", prefix="picoagent-", delete=False) as fh:
        fh.write(text)
        return fh.name


class PathRefused(Exception):
    """A model-supplied path fell outside the project while confinement was on."""


def resolve_path(ctx: ToolContext, raw: str) -> Path:
    """Turn a model-supplied path into an absolute one.

    Strips a leading ``@`` (some models copy it from ``@file`` mentions) and expands ``~``.

    By default any path resolves, including absolute ones and ``..`` traversal. That is not an
    oversight: a coding agent legitimately edits sibling repositories, ``~/.config``, and files
    outside whatever directory it happens to have started in, and confining it would break
    ordinary work. The boundary around an agent is the tools it is given - see
    docs/security/trust-boundaries.md - with permission-gate for protected paths.

    Deployments that need the harder rule can set ``confine_to_project = true``, which refuses
    anything resolving outside ``ctx.cwd``. Off by default because turning it on breaks real
    workflows; available because some environments must have it.
    """
    path = Path(os.path.expanduser(raw.lstrip("@")))
    resolved = (path if path.is_absolute() else ctx.cwd / path)
    if not ctx.config.get("confine_to_project"):
        return resolved
    root = ctx.cwd.resolve()
    candidate = resolved.resolve() if resolved.exists() else Path(os.path.normpath(resolved))
    if candidate != root and root not in candidate.parents:
        raise PathRefused(f"{candidate} is outside the project ({root}) and confine_to_project is on")
    return candidate


_file_locks: dict[str, asyncio.Lock] = {}


def file_lock(path: Path) -> asyncio.Lock:
    """One lock per file so concurrent ``edit``/``write`` calls on the same path serialise.

    Existing files are keyed by their real path so symlink aliases share a lock.
    """
    key = str(path.resolve()) if path.exists() else str(path.absolute())
    return _file_locks.setdefault(key, asyncio.Lock())


# --------------------------------------------------------------------------- built-in tools

class ReadTool:
    """Read a text file (numbered lines) or list a directory."""
    name = "read"
    description = ("Read a file. Returns numbered lines. Use offset/limit for large files. "
                   "Output is truncated at ~2000 lines / 50KB.")
    parameters = {"type": "object", "properties": {
        "path": {"type": "string"},
        "offset": {"type": "integer", "description": "1-based first line"},
        "limit": {"type": "integer", "description": "number of lines"}}, "required": ["path"]}

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            path = resolve_path(ctx, args["path"])
        except PathRefused as exc:
            return ToolResult(ctx.tool_call_id, str(exc), is_error=True)
        if not path.exists():
            return ToolResult(ctx.tool_call_id, f"File not found: {path}", is_error=True)
        if path.is_dir():
            listing = sorted(f"{p.name}/" if p.is_dir() else p.name for p in path.iterdir())
            return ToolResult(ctx.tool_call_id, "\n".join(listing) or "(empty directory)")
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError as exc:
            return ToolResult(ctx.tool_call_id, f"Cannot read {path}: {exc}", is_error=True)

        first = max(1, int(args.get("offset") or 1))
        count = int(args.get("limit") or 0)
        window = lines[first - 1: first - 1 + count] if count else lines[first - 1:]
        numbered = "\n".join(f"{first + i:6d}\t{line}" for i, line in enumerate(window))
        body, truncated = truncate(numbered, ctx.config["tool_output_max_bytes"], ctx.config["tool_output_max_lines"])
        if truncated:
            body += f"\n[truncated: file has {len(lines)} lines; use offset/limit to read the rest]"
        return ToolResult(ctx.tool_call_id, body or "(empty file)", details={"path": str(path), "lines": len(lines)})


class WriteTool:
    """Create or overwrite a whole file."""
    name = "write"
    description = "Create or overwrite a file with the given content. Creates parent directories."
    parameters = {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                  "required": ["path", "content"]}

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            path = resolve_path(ctx, args["path"])
        except PathRefused as exc:
            return ToolResult(ctx.tool_call_id, str(exc), is_error=True)
        async with file_lock(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args["content"])
        return ToolResult(ctx.tool_call_id, f"Wrote {len(args['content'])} chars to {path}", details={"path": str(path)})


class EditTool:
    """Exact-string replacement. The safest edit primitive: no regex, no fuzzy matching."""
    name = "edit"
    description = ("Exact-string replacement in a file. old_text must occur exactly once "
                   "(include more surrounding lines to disambiguate) unless replace_all is true.")
    parameters = {"type": "object", "properties": {
        "path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"},
        "replace_all": {"type": "boolean"}}, "required": ["path", "old_text", "new_text"]}

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        try:
            path = resolve_path(ctx, args["path"])
        except PathRefused as exc:
            return ToolResult(ctx.tool_call_id, str(exc), is_error=True)
        if not path.exists():
            return ToolResult(ctx.tool_call_id, f"File not found: {path}", is_error=True)
        old, new, replace_all = args["old_text"], args["new_text"], bool(args.get("replace_all"))
        async with file_lock(path):
            source = path.read_text()
            occurrences = source.count(old)
            if occurrences == 0:
                return ToolResult(ctx.tool_call_id, "old_text not found (whitespace must match exactly)", is_error=True)
            if occurrences > 1 and not replace_all:
                return ToolResult(ctx.tool_call_id,
                                  f"old_text occurs {occurrences} times; add context or set replace_all", is_error=True)
            path.write_text(source.replace(old, new) if replace_all else source.replace(old, new, 1))
        return ToolResult(ctx.tool_call_id, f"Edited {path} ({occurrences} replacement{'s' if occurrences > 1 else ''})",
                          details={"path": str(path), "old": old, "new": new})


def is_windows() -> bool:
    return platform.system() == "Windows"


async def spawn_shell(command: str, cwd: Path, env: dict) -> asyncio.subprocess.Process:
    """Start ``command`` in the platform's real shell: PowerShell on Windows, ``/bin/sh``
    elsewhere. ``cmd.exe`` (the default for ``create_subprocess_shell`` on Windows) doesn't
    understand ``$VAR``, POSIX pipes, or most commands a model generates, so Windows needs an
    explicit PowerShell invocation rather than the plain cross-platform shell call.
    """
    if is_windows():
        import subprocess  # local: CREATE_NEW_PROCESS_GROUP only exists on the Windows build
        # getattr, not a direct attribute access: the constant is only defined by the subprocess
        # module when the *real* interpreter is Windows, independent of the is_windows() check
        # above - this keeps the branch exercisable by mocking is_windows() in tests on any OS.
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return await asyncio.create_subprocess_exec(
            "powershell", "-NoProfile", "-NonInteractive", "-Command", command, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env,
            creationflags=creation_flags,  # lets kill_process_tree reach the whole tree
        )
    return await asyncio.create_subprocess_shell(
        command, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env=env, start_new_session=True,  # own process group so we can kill children on timeout
    )


async def kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the whole process tree and reap it, so a timed-out command can't leak children.

    POSIX: SIGKILL the process group ``start_new_session`` made ``proc`` the leader of.
    Windows: process groups work differently and there's no ``os.killpg`` at all, so this
    shells out to ``taskkill /T`` (kill the tree) instead - the standard way to do this from
    pure stdlib on Windows.
    """
    if is_windows():
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/F", "/T", "/PID", str(proc.pid),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await killer.wait()
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        log.warning("timed-out command did not exit after being killed")


class ShellTool:
    """Run a shell command in the project directory and capture its output.

    Dispatches to the platform's actual shell: ``/bin/sh`` on Linux/macOS, PowerShell on
    Windows (auto-detected via ``platform.system()``) - not the same dialect everywhere, so
    the model should write commands appropriate to what it's told the platform is (see the
    ``env`` system-prompt section).
    """
    name = "shell"
    description = ("Run a shell command in the project directory (bash/sh on Linux and macOS, "
                   "PowerShell on Windows - detected automatically, not the same dialect on both). "
                   "Returns stdout+stderr and exit code. Use timeout (seconds) for long commands. "
                   "Output is truncated at 50KB / 2000 lines (the full output is saved to a temp "
                   "file whose path is reported).")
    parameters = {"type": "object", "properties": {"command": {"type": "string"},
                  "timeout": {"type": "integer"}}, "required": ["command"]}

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        timeout = int(args.get("timeout") or ctx.config["shell_timeout"])
        proc = await spawn_shell(args["command"], ctx.cwd, {**os.environ, "PICOAGENT": "1"})
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await kill_process_tree(proc)
            return ToolResult(ctx.tool_call_id, f"Command timed out after {timeout}s", is_error=True)

        output = stdout.decode(errors="replace")
        body, truncated = truncate(output, ctx.config["tool_output_max_bytes"],
                                   ctx.config["tool_output_max_lines"], keep="tail")
        if truncated:
            body += f"\n[output truncated; full output: {spill_to_tempfile(output)}]"
        body += f"\n[exit code {proc.returncode}]"
        return ToolResult(ctx.tool_call_id, body, is_error=proc.returncode != 0,
                          details={"exit_code": proc.returncode})


BUILTIN_TOOLS: list[type] = [ReadTool, WriteTool, EditTool, ShellTool]


# --------------------------------------------------------------------------- registry

class ToolRegistry:
    """Holds every known tool and the subset currently exposed to the model.

    ``set_active`` lets plugins implement deferred/dynamic loading (register many tools,
    expose few) or a read-only mode (``["read"]``).
    """

    def __init__(self) -> None:
        self._all: dict[str, Tool] = {}
        self._active: list[str] | None = None   # None means "everything registered"

    def register(self, tool: Tool, *, owner: str = "core") -> None:
        """Add ``tool``; a same-named tool is replaced (that's how plugins override built-ins)."""
        if tool.name in self._all and owner != "core":
            log.info("tool '%s' overridden by plugin '%s'", tool.name, owner)
        self._all[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._all.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._all.get(name)

    def names(self) -> list[str]:
        return list(self._all)

    def set_active(self, names: list[str] | None) -> None:
        """Restrict the model-visible tools to ``names`` (unknown names ignored); ``None`` resets."""
        self._active = None if names is None else [n for n in names if n in self._all]

    def is_active(self, tool: Tool) -> bool:
        return tool in self.active()

    def active(self) -> list[Tool]:
        names = self._active if self._active is not None else list(self._all)
        return [self._all[n] for n in names]

    def specs(self) -> list[ToolSpec]:
        """What gets sent to the provider."""
        return [ToolSpec(t.name, t.description, t.parameters) for t in self.active()]
