"""Tools: the protocol, the registry, and the six built-ins the model gets by default.

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
import re
import signal
import tempfile
from collections.abc import Callable, Iterator, Mapping
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


#: Directory names no search descends into.
#:
#: A walk that goes into ``.git`` or ``node_modules`` spends nearly all of its time there and
#: answers with files nobody asked about: a match inside a vendored dependency reads exactly
#: like a match in the code being edited, and the model has no way to tell them apart. The list
#: is a module-level constant rather than a parameter because it is a fact about repositories
#: rather than about one call, and because a name that turns out to be wrong here has to be
#: visible to be fixed.
#:
#: Only names that never hold hand-written source are on it. ``build``, ``dist`` and ``target``
#: were considered and deliberately left off: each is generated output in one ecosystem and a
#: source directory in another, and a source tree skipped in silence is a worse failure than a
#: slow search. Narrowing beyond this list is the caller's job, through ``path`` or ``glob``.
IGNORED_DIRECTORIES: frozenset[str] = frozenset({
    ".git", ".hg", ".svn", ".bzr",                       # version control metadata
    "node_modules", ".venv", "venv", "site-packages",    # installed dependencies
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".eggs",
    ".cache", ".next", ".nuxt", ".terraform",
    ".idea", ".vscode",                                  # editor state
})

#: How much of a file is examined for the NUL byte that says "not text". A binary file that
#: begins with a text-shaped header - an ELF interpreter path, a PNG's chunk names - still has
#: one well inside the first block, and reading more than a block to decide costs the whole file.
_BINARY_SNIFF_BYTES = 8192

#: The most matches ``grep`` collects before it stops walking.
#:
#: The output limits in the config would cut the *text* either way, but only after every file
#: had been read: a pattern like ``.`` matches every line of every file in the tree, and the
#: work of finding a hundred thousand matches is spent whether or not they are shown. The cap
#: is on the search, so a too-broad pattern costs a moment rather than a minute, and the result
#: says the cap was reached so the model narrows the pattern instead of trusting the count.
GREP_MATCH_LIMIT = 200

#: How much of one matching line is quoted back. A minified bundle is one line of 400 KB, and
#: a single such match would otherwise fill the whole tool result on its own.
GREP_MAX_LINE_CHARS = 300

#: Files larger than this are not searched. Every candidate is read into memory to be decoded,
#: and a repository with a database dump or a packed asset in it should cost a skipped file
#: rather than the session's memory. The skip is named in the tool description, because a model
#: that does not know a file was left out reads "no matches" as "not there".
GREP_MAX_FILE_BYTES = 2_000_000


def _glob_matcher(pattern: str) -> re.Pattern[str]:
    """Compile a glob pattern into a regex matched against ``/``-separated relative paths.

    Neither obvious alternative does this correctly. ``fnmatch`` has no notion of a directory
    separator - its ``*`` matches ``/`` too, so ``*.py`` would match ``pkg/b.py`` and, worse,
    ``**/*.py`` would *not* match a file sitting at the root, because its two stars insist on
    the slash between them. ``Path.glob`` has the semantics right but owns the walk, so it
    descends into ``node_modules`` before anything can be filtered out of its results and
    leaves nowhere to poll an abort.

    Translating once here and matching against the paths our own walk produces gives both.
    ``**/`` is any number of directories including none, ``*`` and ``?`` stop at a ``/``, and a
    character class is handed to the regex engine, which spells one the same way glob does.

    Raises :class:`re.error` for a pattern that cannot be compiled, which the tool turns into an
    error result rather than letting it escape as an exception.
    """
    parts: list[str] = []
    index, end = 0, len(pattern)
    while index < end:
        if pattern.startswith("**/", index):
            parts.append("(?:[^/]+/)*")
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            parts.append("[^/]")
            index += 1
        elif pattern[index] == "[":
            close = pattern.find("]", index + 1)
            if close < 0:                       # an unclosed bracket is a literal one, as in a shell
                parts.append(re.escape("["))
                index += 1
            else:
                body = pattern[index + 1:close]
                parts.append("[" + ("^" + body[1:] if body.startswith("!") else body) + "]")
                index = close + 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(parts))


def _walk_files(root: Path, abort: asyncio.Event) -> Iterator[tuple[Path, str]]:
    """Every file under ``root`` as ``(absolute path, path relative to root)``, noise skipped.

    The relative spelling comes back with the absolute one because both are wanted at every
    call site and recomputing it per file is the walk's second-largest cost after the reads.

    ``dirnames`` is edited in place rather than filtered afterwards: that is the documented way
    to tell ``os.walk`` not to descend, and it is the difference between skipping ``.git`` and
    reading it and then throwing the results away. Symlinked directories are not followed, so a
    link pointing at its own parent cannot make this run forever.

    ``abort`` is polled once per directory rather than once per file, which is often enough to
    stop a large tree promptly and rare enough to cost nothing. A partial answer is the right
    answer for a cancelled search; the caller says so in the result.
    """
    for parent, dirnames, filenames in os.walk(root, followlinks=False):
        if abort.is_set():
            return
        dirnames[:] = [name for name in dirnames if name not in IGNORED_DIRECTORIES]
        relative = Path(parent).relative_to(root)
        for name in filenames:
            yield Path(parent) / name, (relative / name).as_posix()


def _reported_path(root: Path, project_root: Path) -> Callable[[str], str]:
    """Map a search-root-relative path to the spelling ``read`` and ``edit`` will accept.

    A search rooted at ``pkg`` matches its patterns against ``b.py`` - the short spelling is
    what the model asked about, and what it should be able to write a pattern against. But
    every path this tool reports is one the model's *next* call hands straight back, and
    ``read b.py`` resolves against the project root, where there is no such file. Reporting the
    short spelling therefore costs a turn every time ``path`` is used, on a call that looked
    like it succeeded.

    So the two spellings are kept apart on purpose: the pattern still matches relative to the
    search root, and the answer comes back relative to the project root, which is where the
    caller will resolve it. A root outside the project has no relative spelling at all, so
    those come back absolute - still directly usable, just longer.
    """
    try:
        prefix = root.relative_to(project_root)
    except ValueError:
        return lambda relative: str(root / relative)
    if prefix == Path("."):
        return lambda relative: relative
    return lambda relative: (prefix / relative).as_posix()


def _search_root(ctx: ToolContext, raw: str | None) -> Path:
    """The directory ``glob`` and ``grep`` walk: the model's ``path``, or the project root.

    Resolved through :func:`resolve_path_inside_project`, one rule stricter than ``read`` and
    ``write`` use, because a search root is almost always the model's own construction rather
    than something a user typed. ``path="../.."`` is nobody's request, and a search that quietly
    walked the parent of the work tree - reading the sibling repository, reporting its files as
    though they were this one's - is worse than one that says no. An absolute path is still
    allowed, so searching the sibling repository on purpose still works.

    Raises :class:`PathRefused`, which both tools turn into an error result.
    """
    return resolve_path_inside_project(ctx, raw or ".")


def _read_source_text(path: Path) -> str | None:
    """A file's text, or ``None`` for anything a search must not quote back.

    Three skips, each of which would otherwise put something wrong in the transcript.

    A **binary** file decoded with ``errors="replace"`` becomes pages of replacement characters
    that match nothing and read as corruption, so a NUL byte in the first block is taken as the
    answer - the same test ``grep`` itself uses, and wrong only for the UTF-16 text nobody keeps
    source in. A file that is not **UTF-8** is decoded strictly and skipped rather than guessed
    at, because a guessed encoding puts characters in the result that are not in the file. And
    an **unreadable** file - permissions, a dangling symlink, a device node, a file deleted
    between the walk and the read - is one file's problem: the ``OSError`` stops here so that
    the other thousand files still get searched.

    Returning ``None`` rather than raising keeps the skip decision in one place; a caller that
    sees ``None`` has nothing to decide.
    """
    try:
        if path.stat().st_size > GREP_MAX_FILE_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:_BINARY_SNIFF_BYTES]:
        return None
    try:
        return raw.decode()
    except UnicodeDecodeError:
        return None


class GlobTool:
    """Find files by path pattern, without the model having to compose a `find` command."""
    name = "glob"
    description = ("Find files by path pattern. Returns matching file paths, one per line, "
                   "sorted, relative to the project root. '**/' matches any number of "
                   "directories including none ('**/*.py' finds every Python file); '*' and '?' "
                   "stop at a '/'. Dependency, build-cache and version-control directories "
                   "(.git, node_modules, __pycache__, .venv, ...) are never searched. "
                   "Output is truncated at ~2000 lines / 50KB - narrow the pattern if it is.")
    parameters = {"type": "object", "properties": {
        "pattern": {"type": "string", "description": "glob pattern, e.g. '**/*.py' or 'src/**/test_*.py'"},
        "path": {"type": "string",
                 "description": "directory to search from (default: the project root); "
                                "results stay relative to the project root"}},
        "required": ["pattern"]}

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        """Resolve the root, compile the pattern, walk, and hand back the sorted matches.

        The walk runs in a worker thread. That is not about speed: ``ctx.abort`` is set by
        another task on this event loop, and a walk that blocks the loop for thirty seconds is a
        walk during which nothing can set the event it is meant to be polling. Off the loop, the
        poll inside :func:`_walk_files` can actually see it, and the frontend keeps painting.

        Finding nothing is an answer, not a failure: the pattern the model guessed was wrong,
        which is information it can act on, and an ``is_error`` result invites it to retry the
        same call instead of writing a different pattern.
        """
        try:
            root = _search_root(ctx, args.get("path"))
        except PathRefused as exc:
            return tool_result(ctx, str(exc), is_error=True)
        if not root.is_dir():
            return tool_result(ctx, f"Not a directory: {root}", is_error=True)
        pattern = args["pattern"]
        try:
            matcher = _glob_matcher(pattern)
        except re.error as exc:
            return tool_result(ctx, f"Cannot use {pattern!r} as a glob pattern: {exc}", is_error=True)

        matches = await asyncio.to_thread(self._matching_paths, root, matcher, ctx.abort,
                                          _reported_path(root, ctx.cwd))
        body = "\n".join(matches) or f"No files match {pattern!r} under {root}"
        # A cancelled walk reports what it found, and says the tree was not finished. Without
        # that line an abort at the first directory is indistinguishable from a pattern that
        # genuinely matches nothing, and the model draws the wrong conclusion from an empty list.
        if ctx.abort.is_set():
            body += "\n[search aborted before the whole tree was walked]"
        return tool_result(ctx, body, path=str(root), count=len(matches))

    @staticmethod
    def _matching_paths(root: Path, matcher: re.Pattern[str], abort: asyncio.Event,
                        report: Callable[[str], str]) -> list[str]:
        """The relative paths under ``root`` the pattern matches, in path order.

        Path order rather than most-recently-modified. Both are defensible and the trade is
        real: mtime order puts the file the model just edited first, which survives a
        truncation that path order would push it out of. Path order wins on the two properties
        that turned out to matter more here. It is *stable* - the same call twice gives the same
        list, so a model comparing a result against the one it got two turns ago is comparing
        like with like, and a truncated result is a prefix it can page past rather than a
        reshuffle. And it groups a directory's files together, so the listing shows the shape of
        the tree, which is most of what a model asks ``glob`` for in the first place. mtime also
        costs a ``stat`` per file, which path order does not spend at all.
        """
        return sorted(report(relative) for _, relative in _walk_files(root, abort)
                      if matcher.fullmatch(relative))


class GrepTool:
    """Find file *contents* by regular expression: the search `glob` cannot do."""
    name = "grep"
    description = ("Search file contents with a Python regular expression. Returns one "
                   "'path:line:text' line per match, in path order, with paths relative to the "
                   "project root and line numbers counted from 1. Narrow the search with "
                   "path (a directory) and glob (a path pattern such as '**/*.py'). Dependency "
                   "and version-control directories are never searched, and binary files, "
                   "non-UTF-8 files and files over 2MB are skipped. Stops after "
                   f"{GREP_MATCH_LIMIT} matches and says so - narrow the pattern if it does.")
    parameters = {"type": "object", "properties": {
        "pattern": {"type": "string", "description": "Python regular expression, e.g. 'def \\w+_tool'"},
        "path": {"type": "string",
                 "description": "directory to search from (default: the project root); "
                                "results stay relative to the project root"},
        "glob": {"type": "string", "description": "only search files whose path matches this glob"},
        "case_insensitive": {"type": "boolean", "description": "match without regard to case"}},
        "required": ["pattern"]}

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        """Compile both patterns, then search off the event loop.

        The two patterns are compiled before anything is walked, so a typo in either costs no
        file reads and the message names which one was wrong. A regular expression the model got
        wrong is an ordinary event - it is composing them blind, from a description of code it
        has not read - so it comes back as an error result it can correct on the next turn, never
        as the ``re.error`` that would otherwise be raised from inside the walk.
        """
        try:
            root = _search_root(ctx, args.get("path"))
        except PathRefused as exc:
            return tool_result(ctx, str(exc), is_error=True)
        if not root.is_dir():
            return tool_result(ctx, f"Not a directory: {root}", is_error=True)
        pattern = args["pattern"]
        try:
            matcher = re.compile(pattern, re.IGNORECASE if args.get("case_insensitive") else 0)
        except re.error as exc:
            return tool_result(ctx, f"Invalid regular expression {pattern!r}: {exc}", is_error=True)
        selector = args.get("glob")
        try:
            selecting = _glob_matcher(selector) if selector else None
        except re.error as exc:
            return tool_result(ctx, f"Cannot use {selector!r} as a glob pattern: {exc}", is_error=True)

        hits, capped = await asyncio.to_thread(self._hits, root, matcher, selecting, ctx.abort,
                                               _reported_path(root, ctx.cwd))
        body = self._report(hits, capped, f"No matches for {pattern!r} under {root}", ctx)
        return ToolResult(ctx.tool_call_id, body, details={"path": str(root), "matches": len(hits)})

    @staticmethod
    def _hits(root: Path, matcher: re.Pattern[str], selecting: re.Pattern[str] | None,
              abort: asyncio.Event, report: Callable[[str], str]) -> tuple[list[str], bool]:
        """Every matching line as ``path:line:text``, and whether the cap stopped the search.

        One flat line per match rather than a per-file heading with its matches under it. The
        heading form is prettier and reads better in a terminal, and it breaks in the one place
        this output has to survive: a truncation. Output here is cut to the session's limits
        from the head, and a cut through a grouped listing leaves line numbers whose filename
        has already scrolled away - references to nothing. Every line of the flat form carries
        its own path, so a result cut anywhere is still entirely usable, and it is the shape
        ``grep -n`` has printed for forty years, which the model has read a great deal of.
        """
        hits: list[str] = []
        for path, relative in _walk_files(root, abort):
            if selecting is not None and not selecting.fullmatch(relative):
                continue
            text = _read_source_text(path)
            if text is None:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if not matcher.search(line):
                    continue
                shown = line if len(line) <= GREP_MAX_LINE_CHARS else line[:GREP_MAX_LINE_CHARS] + " ..."
                hits.append(f"{report(relative)}:{number}:{shown}")
                if len(hits) >= GREP_MATCH_LIMIT:
                    return hits, True
        return hits, False

    @staticmethod
    def _report(hits: list[str], capped: bool, empty: str, ctx: ToolContext) -> str:
        """The matches, cut to the session's limits, with a footer saying what is missing and why.

        Cut first and append after, the way :meth:`ReadTool._window` does, so the footer is the
        one line that cannot itself be truncated away - a note about a truncation that got
        truncated is worse than no note, because the model then reads a partial result as a
        complete one and stops looking.

        Only one footer, and the output limit outranks the match cap when both applied: it is
        the tighter of the two and its advice - narrow the pattern - is the same either way.

        Finding nothing takes the same path, with ``empty`` standing in for the list, because a
        search that matched nothing and one that was cancelled before it got anywhere have to be
        told apart, and the abort footer is what tells them apart. Nothing found is an answer
        rather than an error: it means the pattern was wrong, which is something the model can
        act on, while ``is_error`` invites it to retry the same call unchanged.
        """
        body, cut = truncate("\n".join(hits) or empty, ctx.config["tool_output_max_bytes"],
                             ctx.config["tool_output_max_lines"])
        if cut:
            body += f"\n[truncated: {len(hits)} matches; narrow the pattern, or set path/glob]"
        elif capped:
            body += (f"\n[stopped at {GREP_MATCH_LIMIT} matches; there are more - "
                     "narrow the pattern, or set path/glob]")
        if ctx.abort.is_set():
            body += "\n[search aborted before the whole tree was walked]"
        return body


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


#: Environment variables a model-composed command is allowed to see.
#:
#: The built-in shell used to hand every command ``{**os.environ, "PICOAGENT": "1"}``, so ``env``
#: - one of the first things a model runs when it wants to know where it is - returned the user's
#: API keys as a tool result. Every tool result is appended to the session log and replayed to the
#: model on the next turn, so one such command wrote a credential to a file on disk and put it in
#: the next prompt. DISA ASD V6R4 records that as V-222444 (sensitive data in the application
#: logs); making the log owner-only answered who may read it, not what is in it.
#:
#: An allowlist rather than a denylist of secret-shaped names, because a denylist can never be
#: complete: ``OPENROUTER_KEY``, ``GH_PAT``, ``PRIVATE_KEY``, ``AWS_ACCESS_KEY_ID`` and
#: ``DATABASE_URL`` all sail through one, and the site that invented the name is the site whose
#: key leaks. Everything here is a path, a locale, a terminal setting or an identity the command
#: could ask the operating system for anyway - nothing here carries a secret, which is the
#: property to preserve when adding to it.
#:
#: The list is what an ordinary build needs and no wider: ``npm test``, ``cargo build``,
#: ``pytest`` and ``git`` all want ``PATH`` and ``HOME``, and each toolchain wants the variable
#: saying where it was installed. Deliberately absent, each because the value is a credential or
#: carries one: ``PICOAGENT_API_KEY`` and ``OPENAI_API_KEY`` (the documented way to supply the
#: model key - see ``provider.py``), ``SSH_AUTH_SOCK`` (a live agent socket), ``HTTP_PROXY`` and
#: ``HTTPS_PROXY`` (routinely ``http://user:pass@proxy``), and every ``AWS_``/``GH_``/
#: ``GITHUB_``/``NPM_`` variable. A site that needs one of those in a command names it in
#: ``shell_env_allow``, which is a decision with somebody's name on it.
SHELL_ENV_ALLOWLIST: frozenset[str] = frozenset({
    # POSIX: where things are, who you are, and how to print
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ",
    "TMPDIR", "PWD", "DISPLAY",
    # Windows: the equivalents, plus what its shell needs to resolve a command at all
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "APPDATA",
    "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "SYSTEMDRIVE",
    "NUMBER_OF_PROCESSORS", "OS", "PROCESSOR_ARCHITECTURE", "USERNAME", "COMPUTERNAME",
    # The interpreter's own variables: these decide which Python runs and what it imports, and
    # picoagent is a Python harness whose commands run `python` and `pip` constantly. Stripping
    # them makes the tool's interpreter quietly disagree with the user's terminal.
    "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV",
})
# An entry earns its place by being *needed* - by this interpreter, or for a default install of
# a toolchain to run at all - not by being harmless. "It is only a path" is a denylist judgment
# in allowlist clothing: it re-opens per-name secret-or-not classification, which is the failure
# an allowlist exists to end. GOPATH, CARGO_HOME, JAVA_HOME, NVM_DIR and the rest were here once
# on that argument; every one is only load-bearing for a relocated toolchain, whose owner names
# it in `shell_env_allow` (USER_ONLY) and gets exactly what they asked for.

#: The one value of ``shell_env`` that passes the whole environment through. Only this exact
#: string opens the door, so a typo, a value of the wrong type, and a value that arrived from
#: somewhere unexpected all fail closed onto the allowlist.
SHELL_ENV_INHERIT = "inherit"


def shell_env(base_env: Mapping[str, str], config: Mapping[str, Any]) -> dict[str, str]:
    """The environment a model-composed command runs with, plus the ``PICOAGENT`` marker.

    ``config["shell_env"]`` is ``"allowlist"`` - the default, and the answer for any value this
    does not recognise - or ``"inherit"``, which passes the whole environment through for the
    user who genuinely wants that. A config that has never heard of the setting gets the safe
    one, so an embedder assembling a config dict by hand is covered too.

    ``config["shell_env_allow"]`` adds names to :data:`SHELL_ENV_ALLOWLIST`, matched
    case-insensitively like the list itself. Its shape is checked here rather than trusted: this
    runs on whatever the config layering produced, and a ``TypeError`` raised inside a tool is a
    failure the model is told about and the user is not. A value that is not a list of strings
    names no variable, so it adds none.

    Both settings are in :data:`picoagent.core.config.USER_ONLY`. A cloned repository naming
    ``DATABASE_URL`` here would put it in the environment of the first command the model ran,
    which is the hole ``[plugins.credential-guard] extra_allow_env`` was closed for.
    """
    if config.get("shell_env") == SHELL_ENV_INHERIT:
        return {**base_env, "PICOAGENT": "1"}
    extra = config.get("shell_env_allow")
    named = ({name.upper() for name in extra if isinstance(name, str)}
             if isinstance(extra, list) else set())
    allowed = SHELL_ENV_ALLOWLIST | named
    env = {name: value for name, value in base_env.items() if name.upper() in allowed}
    env["PICOAGENT"] = "1"
    return env


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

    The command sees :func:`shell_env`, not the user's whole environment. The description says
    so, because a variable that is simply absent looks to a model like a variable set to the
    empty string, and it will otherwise report the build as broken rather than say what it
    could not see.
    """
    name = "shell"
    description = ("Run a shell command in the project directory (bash/sh on Linux and macOS, "
                   "PowerShell on Windows - detected automatically, not the same dialect on both). "
                   "Returns stdout+stderr and exit code. Use timeout (seconds) for long commands. "
                   "Output is truncated at 50KB / 2000 lines (the full output is saved to a temp "
                   "file whose path is reported). The environment is an allowlist - PATH, HOME, "
                   "locale and the Python toolchain paths - so API keys, tokens and other credentials the "
                   "user exported are not visible to the command, by design. If one is genuinely "
                   "needed, say which variable it is instead of trying to read it.")
    parameters = {"type": "object", "properties": {"command": {"type": "string"},
                  "timeout": {"type": "integer"}}, "required": ["command"]}

    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        timeout = int(args.get("timeout") or ctx.config["shell_timeout"])
        proc = await spawn_shell(args["command"], ctx.cwd, shell_env(os.environ, ctx.config))
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


BUILTIN_TOOLS: list[type] = [ReadTool, WriteTool, EditTool, GlobTool, GrepTool, ShellTool]


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
