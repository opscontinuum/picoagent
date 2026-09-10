"""Writing settings back into a TOML file, which the standard library will not do.

``tomllib`` reads and does not write - that is deliberate upstream, and it is the whole reason
this module exists. picoagent's promise is the standard library and nothing else, so
``picoagent setup`` cannot reach for ``tomlkit`` to store the key it just asked for.

What it does is *edit*, never re-emit. A config file is somebody's: it has their comments in it,
their ordering, their spacing, and settings this tool has never heard of. Round-tripping it
through ``tomllib.loads`` and a serialiser would return a file with every comment gone and every
table rearranged, which is a rewrite dressed up as a save. So the text is kept and the smallest
possible piece of it is changed: an existing key's line is replaced in place, a new key is
appended inside the table it belongs to, and a table nothing mentions yet is added at the end.

The safety net is :func:`apply_edits` parsing its own output and comparing it with the document
it meant to produce. A hand-written scanner over a format with multi-line strings, inline
tables, arrays of tables and dotted keys will meet a file it reads wrongly; what must never
happen is that it *writes* one. So a mismatch raises :class:`TomlEditError` and nothing is
written, leaving the user to make the edit themselves - a worse outcome than the edit working,
and a much better one than a silently mangled config.
"""
from __future__ import annotations

import re
import tomllib
from typing import Any

#: A key that needs no quoting. Everything else is written as a quoted basic string, escapes and
#: all, because a provider's name reaches this file: ``[providers.<name>]`` is built from a name
#: a plugin chose, and a name with a quote or a backslash in it must come back out as it went in.
BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")

_HEADER = re.compile(r"^\s*(?P<open>\[\[?)(?P<name>[^\[\]]*)(?P<close>\]\]?)\s*(?:#.*)?$")
_KEY = re.compile(r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z0-9_-]+|\"(?:[^\"\\]|\\.)*\"|'[^']*')[ \t]*=[ \t]*")

#: The escapes TOML's basic strings define by name. Anything else below the space, plus DEL,
#: goes out as ``\uXXXX``; everything at or above the space is written as itself, so a file that
#: was UTF-8 stays UTF-8 and a path with an accent in it is still readable afterwards.
_ESCAPES = {"\\": "\\\\", '"': '\\"', "\b": "\\b", "\t": "\\t",
            "\n": "\\n", "\f": "\\f", "\r": "\\r"}

#: Table paths this module tracks but will never address. A key under ``[[servers]]`` belongs to
#: an array of tables, which has no single ``[name]`` to edit, so it is filed under a path no
#: caller can spell rather than being mistaken for a key of the table above it.
_UNADDRESSABLE = "\x00"


class TomlEditError(Exception):
    """The edit could not be made, or could not be proved to have been made correctly."""


def render_string(value: str) -> str:
    """``value`` as a TOML basic string, quoted and escaped."""
    out = ['"']
    for char in value:
        if char in _ESCAPES:
            out.append(_ESCAPES[char])
        elif char < " " or char == "\x7f":
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def render_key(key: str) -> str:
    """One key, bare when TOML allows it and quoted when it does not."""
    return key if BARE_KEY.match(key) else render_string(key)


def render_value(value: Any) -> str:
    """One value, in the smallest TOML spelling that reads it back unchanged.

    ``bool`` is tested before ``int`` because it is one in Python and is not one in a config
    file: ``True`` written as ``1`` comes back as an integer and stops meaning what it meant.
    A type this does not know is refused rather than guessed at - ``str(value)`` would write
    something that parses and says something else.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return render_string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(render_value(item) for item in value) + "]"
    raise TomlEditError(f"cannot write a {type(value).__name__} to a TOML file")


def render_table(path: tuple[str, ...], values: dict[str, Any]) -> list[str]:
    """A whole ``[a.b]`` table as lines, for a table the file does not have yet."""
    header = "[" + ".".join(render_key(part) for part in path) + "]"
    return [header] + [f"{render_key(key)} = {render_value(value)}" for key, value in values.items()]


def _skip_string(text: str, start: int) -> int:
    """The index just past the single-line string opening at ``start``."""
    quote = text[start]
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == "\\" and quote == '"':
            index += 2
            continue
        if char == quote:
            return index + 1
        index += 1
    return len(text)


def _scan(text: str, depth: int, pending: str | None) -> tuple[int, str | None, int | None]:
    """Read one line as TOML text, tracking what is still open at the end of it.

    Returns the bracket depth (an array or inline table the value has not closed yet), the
    multi-line string delimiter still waiting for its partner, and where a comment starts - the
    first ``#`` that is not inside a string, so a ``#`` in a URL is not read as one.

    This is the whole of the format knowledge here, and it is the part most likely to be wrong
    about a file somebody actually wrote. That is what :func:`apply_edits`'s check is for.
    """
    index, comment_at = 0, None
    while index < len(text):
        if pending:
            found = text.find(pending, index)
            if found < 0:
                return depth, pending, None
            index, pending = found + 3, None
            continue
        char = text[index]
        if char == "#":
            comment_at = index
            break
        if text.startswith('"""', index) or text.startswith("'''", index):
            pending, index = text[index:index + 3], index + 3
            continue
        if char in "\"'":
            index = _skip_string(text, index)
            continue
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth = max(0, depth - 1)
        index += 1
    return depth, pending, comment_at


def _key_path(spec: str) -> tuple[str, ...] | None:
    """``providers."my.name"`` as ``("providers", "my.name")``, or ``None`` if it is not a key.

    Parsed by ``tomllib`` rather than by a split on ``.``, because the quoting rules for a dotted
    key are the quoting rules for a string and re-implementing them is how ``[providers."a.b"]``
    becomes two tables. Handing it a document with one key and reading the shape back is exact
    and costs one parse of one short line.
    """
    try:
        parsed: Any = tomllib.loads(f"{spec} = 0")
    except Exception:  # noqa: BLE001 - anything that will not parse is simply not a key path
        return None
    path: list[str] = []
    while isinstance(parsed, dict) and len(parsed) == 1:
        key, parsed = next(iter(parsed.items()))
        path.append(key)
        if not isinstance(parsed, dict):
            return tuple(path)
    return None


def _map(lines: list[str]) -> tuple[dict[tuple, list[int]], dict[tuple, tuple[int, int, int | None]]]:
    """Where every table's lines are, and which line each of its keys is written on.

    A table's region runs from just after its header to just before the next one, so a key added
    to it lands among its own keys rather than at the end of the file under whatever table
    happens to be last. A key's region is a range because a value can span lines - an array
    written one element per line, a ``\"\"\"`` block - and replacing only the first of those lines
    would leave the rest behind as text belonging to nothing.
    """
    regions: dict[tuple, list[int]] = {(): [0, 0]}
    entries: dict[tuple, tuple[int, int, int | None]] = {}
    current: tuple = ()
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        header = _HEADER.match(line)
        if header:
            regions[current][1] = index
            path = _key_path(header.group("name").strip())
            if path is None or header.group("open") != "[" or header.group("close") != "]":
                path = (_UNADDRESSABLE, str(index))
            current = path
            regions[current] = [index + 1, index + 1]
            index += 1
            continue
        match = _KEY.match(line)
        if match is None:
            index += 1
            continue
        depth, pending, comment_at = _scan(line[match.end():], 0, None)
        last = index
        while (pending is not None or depth > 0) and last + 1 < len(lines):
            last += 1
            depth, pending, _ = _scan(lines[last], depth, pending)
        key = _key_path(match.group("key"))
        if key is not None and len(key) == 1:
            offset = None if comment_at is None else comment_at + match.end()
            entries[(current, key[0])] = (index, last, offset)
        index = last + 1
    regions[current][1] = len(lines)
    return regions, entries


def _insertion_point(lines: list[str], region: list[int]) -> int:
    """Where a new key goes in ``region``: after its last setting, before the trailing chatter.

    Blank lines at the end of a table are separation, and a comment block at the end of one is
    almost always about the table *below* it, since that is where a person writes the note
    explaining the next section. Inserting above both keeps a new key with the keys it belongs
    to and leaves the note attached to whatever it was introducing.
    """
    start, end = region
    last = start
    for index in range(start, min(end, len(lines))):
        stripped = lines[index].strip()
        if stripped and not stripped.startswith("#"):
            last = index + 1
    return last


def _expected(document: dict, edits: dict[tuple[str, ...], dict[str, Any]]) -> dict:
    """The document the edits are meant to produce, as ``tomllib`` would read it back."""
    result = {key: value for key, value in document.items()}
    for path, values in edits.items():
        node = result
        for part in path:
            child = node.get(part)
            if child is None:
                child = {}
            elif not isinstance(child, dict):
                raise TomlEditError(f"{'.'.join(path)} cannot be written: {part} is not a table")
            else:
                child = dict(child)
            node[part] = child
            node = child
        node.update(values)
    return result


def apply_edits(text: str, edits: dict[tuple[str, ...], dict[str, Any]]) -> str:
    """``text`` with ``edits`` applied, and every byte it does not change left alone.

    ``edits`` maps a table path to the keys to set in it; ``()`` is the top level, so
    ``{(): {"model": "gpt-4o"}, ("providers", "openai"): {"api_key": "sk-..."}}`` is one call.
    A path is a tuple rather than a dotted string because a provider may be named with a dot in
    it, and splitting the name back apart is the bug that would put the key in the wrong table.

    Raises :class:`TomlEditError` when ``text`` does not parse, when a value has a type TOML
    cannot hold, or when the result does not read back as the document the edits describe. The
    last of those is the important one: it is the check that turns "the scanner misread this
    file" from silent corruption of somebody's config into a refusal with the file untouched.
    """
    if not edits:
        return text
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise TomlEditError(f"the file is not valid TOML, so nothing can be added to it: {exc}") from None
    expected = _expected(document, edits)

    lines = text.split("\n")
    regions, entries = _map(lines)
    changes: list[tuple[int, int, list[str]]] = []
    appended: list[list[str]] = []
    for path, values in edits.items():
        if path not in regions:
            appended.append(render_table(path, values))
            continue
        fresh: list[str] = []
        for key, value in values.items():
            rendered = f"{render_key(key)} = {render_value(value)}"
            spot = entries.get((path, key))
            if spot is None:
                fresh.append(rendered)
                continue
            start, last, comment_at = spot
            indent = _KEY.match(lines[start]).group("indent")     # type: ignore[union-attr]
            trailing = "" if comment_at is None else "  " + lines[start][comment_at:].strip()
            changes.append((start, last + 1, [indent + rendered + trailing]))
        if fresh:
            at = _insertion_point(lines, regions[path])
            changes.append((at, at, fresh))

    for start, end, replacement in sorted(changes, key=lambda change: change[0], reverse=True):
        lines[start:end] = replacement
    if appended:
        while lines and not lines[-1].strip():
            lines.pop()
        for block in appended:
            lines.extend(["", *block])
        lines.append("")

    result = "\n".join(lines)
    try:
        written = tomllib.loads(result)
    except tomllib.TOMLDecodeError as exc:
        raise TomlEditError(f"the edit would not have parsed ({exc}); the file is unchanged") from None
    if written != expected:
        raise TomlEditError("the edit did not produce the settings it was meant to; the file is "
                            "unchanged. Add them by hand, or move the file aside and re-run")
    return result
