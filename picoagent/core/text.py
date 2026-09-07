"""Text picoagent did not write, made safe to put in front of a person.

A notice's text comes from a plugin, a tool result, a repository's config file or a remote MCP
server. An exception's message - and its class name, which ``type()`` will take as any string -
comes from whoever raised it, and plugin code raises freely. None of those are trusted, and a
terminal reads some of what they can contain as *commands* rather than as characters: an ANSI
sequence can rewrite a line the user already read, blank the screen, hide the text that follows,
or retitle the window. That turns an informational line into a way to lie about what the session
did, which is worth more to an attacker than any single line it could print honestly.

Two jobs, because they are two different risks:

* :func:`strip_terminal_controls` removes what a terminal obeys. It changes nothing else, so a
  multi-line ``/model list`` still arrives with its lines and its indentation.
* :func:`safe_for_display` adds a ceiling, for the text that is picoagent's own sentence about
  something that went wrong rather than an answer the user asked for.

What is deliberately *not* covered: characters that mislead a reader without instructing the
terminal, such as the bidirectional overrides (U+202E and friends) that reverse the order text
is displayed in. Those are a rendering question that reaches every string picoagent shows,
including the model's own reply, and answering it here would only look like it had been answered.
"""
from __future__ import annotations

import re

#: Longest sentence picoagent will build out of text it did not write. Generous next to a real
#: message - the provider already caps an HTTP error body at 300 characters, and the longest
#: honest exception here names a path or two - and small enough that a five-megabyte ``__str__``
#: cannot push the transcript out of the scrollback, out of the session log, or into the model's
#: context on the next turn. A tool's *own* output has its own, much larger limit; this is not
#: that, it is the bound on a report about a failure.
MAX_MESSAGE_CHARS = 2000

#: Everything a terminal treats as an instruction, in the order the alternatives must be tried.
#:
#: 1. The string-family sequences (OSC, DCS, PM, APC) carry a body, so the body goes with them
#:    rather than being left behind as text. An unterminated one would swallow the rest of the
#:    input the way it swallows the rest of a terminal, so the body stops at a newline: an
#:    attacker costs themselves their own line and no more of the message.
#: 2. CSI, in both its two-character (``ESC [``) and single-byte (``\x9b``) forms, with the final
#:    byte optional so a truncated sequence at the end of a message is still consumed.
#: 3. Any other escape sequence of two characters. ``ESC c`` alone re-initialises the terminal.
#: 4. What is left of C0 and C1. Tab and newline are not here, on purpose; carriage return is,
#:    because on its own it puts the cursor back on column 0 and lets the next text overwrite a
#:    line the user has already read.
_TERMINAL_CONTROLS = re.compile(
    r"(?:\x1b[\]P^_]|[\x90\x9d\x9e\x9f])[^\x1b\x07\x9c\n]*(?:\x07|\x1b\\|\x9c)?"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]?"
    r"|\x1b[@-~]"
    r"|[\x00-\x08\x0b-\x1f\x7f-\x9f]"
)


def strip_terminal_controls(text: str) -> str:
    """``text`` with every sequence a terminal obeys removed, and nothing else changed.

    Tabs and newlines survive. They are what a notice legitimately contains: the split that puts
    a slash command's output on stdout exists so that ``-p "/model list"`` delivers a listing,
    and a listing is lines.
    """
    return _TERMINAL_CONTROLS.sub("", text)


def safe_for_display(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    """:func:`strip_terminal_controls`, then a ceiling of ``limit`` characters.

    Stripping first is not an ordering preference: cutting first leaves whatever sat across the
    boundary as two halves, and half of an escape sequence is still an escape.
    """
    cleaned = strip_terminal_controls(text)
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[:limit]}… [{len(cleaned) - limit} more characters truncated]"


def describe_exception(exc: BaseException, limit: int = MAX_MESSAGE_CHARS) -> str:
    """``"TypeName: message"``, where both halves are treated as text the raiser chose.

    The message half is the obvious one. The name half is untrusted for a less obvious reason:
    ``type(name, bases, ns)`` accepts any string, so a plugin can raise an exception whose class
    is called whatever a picoagent line looks like.

    A ``__str__`` that raises is reported rather than allowed out. Every caller here is a catch
    whose whole job is that a failure does not end the session, and an exception escaping *from
    the report* would undo exactly that.
    """
    try:
        described = f"{type(exc).__name__}: {exc}"
    except Exception:  # noqa: BLE001 - the raiser's __str__ is as untrusted as its output
        described = f"{type(exc).__name__}: <its message could not be rendered>"
    return safe_for_display(described, limit)
