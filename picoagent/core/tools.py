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

def missing_required_args(tool: Any, args: dict) -> list[str]:
    """Declared-required arguments the caller did not supply.

    Reads the tool's own ``parameters['required']``, so a tool that gains a required
    argument is covered without touching this, and the message can never drift from the
    schema it describes. Plugin tools are covered for free.
    """
    required = (getattr(tool, "parameters", None) or {}).get("required") or []
    return [name for name in required if name not in args]


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


def tool_result(ctx: ToolContext, text: str, is_error: bool = False, **details: Any) -> ToolResult:
    """A tool's last line: cut ``text`` to the session's limits, say when it cut, and wrap it.

    Reach for this at the end of every ``execute`` that returns text whose length something
    other than the tool decides - a cluster's answer, a document, a search result. Unbounded
    output is the fastest way to break a session, and the limits live in the config so a
    deployment sets them once for every tool rather than each tool inventing a cap.

    It takes ``ctx`` rather than the two limits because the result also carries
    ``ctx.tool_call_id``: the id and the limits are the whole of what this needs, and they
    arrive together. Keyword arguments become ``ToolResult.details``, which UIs and plugins
    read and the model never sees, so structured facts about the call go there (``path=``,
    ``exit_code=``) and ``text`` stays what the model is meant to read.

    It keeps the *head*, which is right for a document or a listing. A tool whose output
    matters at the end - a command's - calls :func:`truncate` with ``keep="tail"`` itself, the
    way :class:`ShellTool` does, because it has a footer to add after the cut.
    """
    body, cut = truncate(text, ctx.config["tool_output_max_bytes"], ctx.config["tool_output_max_lines"])
    return ToolResult(ctx.tool_call_id, body + ("\n[truncated]" if cut else ""), is_error=is_error,
                      details=details)


def spill_to_tempfile(text: str) -> str:
    """Write ``text`` to a temp file and return its path (so the model can grep the full output)."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", prefix="picoagent-", delete=False) as fh:
        fh.write(text)
        return fh.name


class PathRefused(Exception):
    """A model-supplied path fell outside the project while confinement was on."""


@dataclass(frozen=True)
class ResolvedPath:
    """Which file a model-supplied path names, and whether a tool will open it.

    ``path`` is always absolute and always symlink-resolved: it is the file the OS reaches if
    the call goes ahead, and it is filled in even when ``refusal`` says the tool will not make
    that call. A guard asking "which file is this?" must never be handed ``None``, because
    ``None`` reads as *no file here* - the same answer an unrelated argument gives - and a guard
    that reads it that way lets the call through to a tool that then opens something.
    """
    path: Path
    refusal: str | None = None


def _project_root(config: dict, cwd: Path | None) -> Path:
    """The directory a relative path is resolved against: the session's, not the process's.

    They differ under ``-C``, and every path decision has to be taken against the same one, so
    the fallback for a config nobody built with ``load_config`` is written once here.
    """
    return Path(cwd) if cwd is not None else Path(config.get("_cwd") or Path.cwd())


def _model_path(raw: str) -> Path:
    """What a model-supplied string names before it is joined to anything.

    A leading ``@`` is stripped (models copy it from ``@file`` mentions) and ``~`` expanded.
    Anything deciding *about* such a string - is it absolute? - has to ask here rather than of
    the raw text, or it decides about a path nothing will open: ``@../..`` is not relative to
    the tool that opens it, whatever ``Path("@../..").is_absolute()`` says.
    """
    return Path(os.path.expanduser(raw.lstrip("@")))


def resolve_tool_path(raw: str, config: dict, cwd: Path | None = None) -> ResolvedPath:
    """Where a model-supplied path lands: the seam a tool and a guard must both resolve through.

    A ``tool_call`` guard decides about a path *before* the tool touches it, and the only way
    that decision can be about the same file is for both to compute it here. Every guard that
    resolved a path itself drifted from this function and the drift was a bypass: an unstripped
    ``@`` prefix, a relative path resolved against the process directory rather than the
    session's (they differ under ``-C``), a symlink nobody followed. See
    docs/plugin-authoring.md, "Gating a path argument".

    Guards have a runtime, not a :class:`ToolContext` - that is built per call inside the loop,
    after the guards have already answered - so this takes the two things a runtime carries:
    ``rt.cfg`` (the same dictionary the tool receives as ``ctx.config``) and, optionally, the
    session directory. ``cwd`` defaults to the ``_cwd`` that ``load_config`` records, so
    ``resolve_tool_path(raw, rt.cfg)`` is the whole call from a guard. It falls back to the
    process directory only for a config nobody built with ``load_config``.

    An expected failure is a value here, not an exception: guards outnumber tools and a guard
    that has to wrap this in ``try`` is a guard that will one day catch the wrong thing, or
    nothing. Tools keep the exception through :func:`resolve_path`.

    Refused, for this function, means *the tool will not open this*: outside the project while
    ``confine_to_project`` is on, or a path the OS cannot resolve at all (a symlink loop).
    """
    root = _project_root(config, cwd)
    path = _model_path(raw)
    absolute = path if path.is_absolute() else root / path
    try:
        # Non-strict resolve() follows every symlink component that exists and appends the rest,
        # so a file that does not exist yet still names the real directory it would be created
        # in. The textual normpath this replaced could not see a link, which is how a write
        # through `repo-symlink/new-file` landed outside a project confinement was holding.
        real, project = absolute.resolve(), root.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        # A symlink loop (RuntimeError on CPython, ELOOP elsewhere) or a path the OS will not
        # parse. Nothing can be opened through it, so it is refused - and the caller still gets
        # the best name available rather than a None it would read as "no file".
        return ResolvedPath(Path(os.path.normpath(absolute)), f"{absolute} cannot be resolved: {exc}")
    if config.get("confine_to_project") and real != project and project not in real.parents:
        return ResolvedPath(real, f"{real} is outside the project ({project}) "
                                  "and confine_to_project is on")
    return ResolvedPath(real)


def resolve_path(ctx: ToolContext, raw: str) -> Path:
    """Turn a model-supplied path into the absolute path this tool will open.

    Strips a leading ``@`` (some models copy it from ``@file`` mentions), expands ``~``, and
    resolves symlinks, so what comes back is the file the OS actually reaches.

    By default any path resolves, including absolute ones and ``..`` traversal. That is not an
    oversight: a coding agent legitimately edits sibling repositories, ``~/.config``, and files
    outside whatever directory it happens to have started in, and confining it would break
    ordinary work. The boundary around an agent is the tools it is given - see
    docs/security/trust-boundaries.md - with permission-gate for protected paths.

    Deployments that need the harder rule can set ``confine_to_project = true``, which refuses
    anything resolving outside ``ctx.cwd``. Off by default because turning it on breaks real
    workflows; available because some environments must have it.

    This is :func:`resolve_tool_path` with the refusal raised instead of returned, because a
    tool that forgets to check gets an exception the loop turns into an error result, while a
    guard that forgets to check would silently allow. Same decision, taken in one place.
    """
    resolved = resolve_tool_path(raw, ctx.config, ctx.cwd)
    if resolved.refusal:
        raise PathRefused(resolved.refusal)
    return resolved.path


def resolve_tool_path_inside_project(raw: str, config: dict, cwd: Path | None = None) -> ResolvedPath:
    """:func:`resolve_tool_path`, with one more rule: a *relative* path must land in the project.

    Two different rules, easily read as one:

    * ``confine_to_project`` is the security boundary. It is off by default, it applies to every
      spelling of a path, and :func:`resolve_tool_path` enforces it.
    * This adds a **usability** rule on top, for a tool whose path argument is normally the
      model's own construction: a relative path that climbs out of the project (``../..``, or a
      symlink that leads there) is refused, while an absolute one is allowed. An absolute path
      is a place someone named on purpose - the sibling repository, the terraform tree next
      door - and a tool that refused it would be unusable for the work it exists for.

    It is not a security boundary and must not be sold as one: anything that can write ``../..``
    can write ``/etc``, so this stops a mistake, not an attacker. What stops an attacker is
    ``confine_to_project``, and that is still the rule underneath.

    The escape is judged on the *resolved* path, after ``@`` stripping, ``~`` expansion and
    symlink following, because those are what decide which file gets opened. One of the two
    plugin copies this replaces judged it on the text instead, and was wrong twice for it:
    ``@../..`` passed where ``../..`` did not, and a symlink inside the project pointing out of
    it passed as a child of it.
    """
    resolved = resolve_tool_path(raw, config, cwd)
    if resolved.refusal or _model_path(raw).is_absolute():
        return resolved
    root = _project_root(config, cwd).resolve()
    if resolved.path == root or root in resolved.path.parents:
        return resolved
    return ResolvedPath(resolved.path,
                        f"{raw!r} resolves to {resolved.path}, which is outside the project "
                        f"directory ({root}); pass an absolute path if you meant to go there")


def resolve_path_inside_project(ctx: ToolContext, raw: str) -> Path:
    """:func:`resolve_tool_path_inside_project` with the refusal raised instead of returned.

    The same split as :func:`resolve_path` and for the same reason: a tool that forgets to check
    gets an exception the loop turns into an error result, while a guard that forgets to check
    would silently allow. Catch :class:`PathRefused` at the call site and return it as an error
    result - a refused path is an expected failure, not a bug.
    """
    resolved = resolve_tool_path_inside_project(raw, ctx.config, ctx.cwd)
    if resolved.refusal:
        raise PathRefused(resolved.refusal)
    return resolved.path


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
        """Resolve the path, then hand it to whichever of the two jobs this tool does.

        One tool, two operations, because that is what a model reaching for ``read`` wants: it
        does not know yet whether the path it copied out of an error message is a file or the
        directory above it, and making it guess costs a wasted turn. They share the path
        resolution and nothing else, so each has its own function below.
        """
        try:
            path = resolve_path(ctx, args["path"])
        except PathRefused as exc:
            return ToolResult(ctx.tool_call_id, str(exc), is_error=True)
        if not path.exists():
            return ToolResult(ctx.tool_call_id, f"File not found: {path}", is_error=True)
        if path.is_dir():
            return self._listing(path, ctx)
        return self._window(path, args, ctx)

    @staticmethod
    def _listing(path: Path, ctx: ToolContext) -> ToolResult:
        """A directory: its entries by name, subdirectories marked with a trailing slash."""
        listing = sorted(f"{p.name}/" if p.is_dir() else p.name for p in path.iterdir())
        return ToolResult(ctx.tool_call_id, "\n".join(listing) or "(empty directory)")

    @staticmethod
    def _window(path: Path, args: dict, ctx: ToolContext) -> ToolResult:
        """A file: the ``offset``/``limit`` window, numbered, cut to the output limits.

        The line numbers count from the file's first line, not the window's, so a number the
        model reads here is the one it can pass back as ``offset`` or quote in an ``edit``.
        """
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


def own_process_group() -> dict[str, Any]:
    """The spawn keyword that makes a child lead a process group of its own, per platform.

    Every child :func:`kill_process_tree` may be asked to end must be spawned with this. The
    kill signals the *group*, which is how it reaches the grandchildren a shell started; a
    child left in picoagent's own group would make ``os.killpg`` signal picoagent instead,
    and the terminal it was started from with it.
    """
    if is_windows():
        import subprocess  # local: CREATE_NEW_PROCESS_GROUP only exists on the Windows build
        # getattr, not a direct attribute access: the constant is only defined by the subprocess
        # module when the *real* interpreter is Windows, independent of the is_windows() check
        # above - this keeps the branch exercisable by mocking is_windows() in tests on any OS.
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


async def spawn_shell(command: str, cwd: Path, env: dict) -> asyncio.subprocess.Process:
    """Start ``command`` in the platform's real shell: PowerShell on Windows, ``/bin/sh``
    elsewhere. ``cmd.exe`` (the default for ``create_subprocess_shell`` on Windows) doesn't
    understand ``$VAR``, POSIX pipes, or most commands a model generates, so Windows needs an
    explicit PowerShell invocation rather than the plain cross-platform shell call.
    """
    if is_windows():
        return await asyncio.create_subprocess_exec(
            "powershell", "-NoProfile", "-NonInteractive", "-Command", command, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env,
            **own_process_group(),
        )
    return await asyncio.create_subprocess_shell(
        command, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env=env, **own_process_group(),
    )


#: How long a killed tree gets to actually go, before it is left un-reaped and reported. Only a
#: process the kernel cannot interrupt reaches this, and the caller still has to answer somebody.
_REAP_TIMEOUT = 5.0


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> bool:
    """Send ``sig`` to the group ``proc`` leads; ``False`` if the group is already gone.

    The answer is what tells an escalation apart from a pointless second signal: a tree that
    has left needs no SIGKILL, and asking again would only race a reused pid.
    """
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        return False
    return True


async def _exited(proc: asyncio.subprocess.Process, seconds: float) -> bool:
    """Wait up to ``seconds`` for ``proc`` to exit *and be reaped*; ``False`` if it is still there."""
    try:
        await asyncio.wait_for(proc.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return False
    return True


async def kill_process_tree(proc: asyncio.subprocess.Process, grace: float = 0.0) -> None:
    """End the whole process tree and reap it, so a timed-out command can't leak children.

    ``proc`` must have been spawned with :func:`own_process_group`, because the signals go to
    the group: killing the direct child alone reparents its own children to init, where nothing
    in this session can see or stop them.

    POSIX: signal the process group ``start_new_session`` made ``proc`` the leader of - SIGKILL,
    or SIGTERM first when ``grace`` asks for it. Windows: process groups work differently and there's no ``os.killpg`` at all, so this
    shells out to ``taskkill /T`` (kill the tree) instead - the standard way to do this from
    pure stdlib on Windows.

    ``grace`` seconds, on POSIX, buys the tree a SIGTERM first, and SIGKILL follows only if it
    is still there afterwards: a child holding a lock or half a written file gets its chance to
    undo that, and a child that ignores SIGTERM still does not survive. Windows is offered no
    such choice because it has none to make - ``TerminateProcess`` is what both ``terminate()``
    and ``kill()`` call there, and ``taskkill`` without ``/F`` posts ``WM_CLOSE``, which a
    console child has no message loop to receive.

    The reap is bounded by :data:`_REAP_TIMEOUT` rather than awaited forever. A process wedged in
    an uninterruptible kernel wait outlives SIGKILL itself, and the caller - a tool result, a
    plugin's ``api.exec`` - has to answer somebody. One warned-about un-reaped child beats a
    session that never returns.
    """
    if is_windows():
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/F", "/T", "/PID", str(proc.pid),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await killer.wait()
    else:
        asked = grace > 0 and _signal_group(proc, signal.SIGTERM)
        if not (asked and await _exited(proc, grace)):
            _signal_group(proc, signal.SIGKILL)
    if proc.returncode is None and not await _exited(proc, _REAP_TIMEOUT):
        log.warning("process %s did not exit after being killed; leaving it un-reaped", proc.pid)


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
