"""Text picoagent did not write, made safe to put in front of a person.

A notice's text comes from a plugin, a tool result, a repository's config file or a remote MCP
server. An exception's message - and its class name, which ``type()`` will take as any string -
comes from whoever raised it, and plugin code raises freely. None of those are trusted, and a
terminal reads some of what they can contain as *commands* rather than as characters: an ANSI
sequence can rewrite a line the user already read, blank the screen, hide the text that follows,
or retitle the window. That turns an informational line into a way to lie about what the session
did, which is worth more to an attacker than any single line it could print honestly.

Three jobs, because they are three different risks:

* :func:`strip_terminal_controls` removes what a terminal obeys. It changes nothing else, so a
  multi-line ``/model list`` still arrives with its lines and its indentation.
* :func:`safe_for_display` adds a ceiling, for the text that is picoagent's own sentence about
  something that went wrong rather than an answer the user asked for.
* :func:`safe_for_stream` answers a different question - not "will the terminal obey this" but
  "can this be turned into bytes at all". A ``str`` in Python is not always text: a lone
  surrogate (``json.loads('"\\ud800"')`` makes one out of an MCP server's reply) is a ``str``
  that no codec can encode, UTF-8 included, so the write raises ``UnicodeEncodeError`` and the
  session ends in a traceback. A stream that is not UTF-8 - an ASCII-only console, a Windows
  code page - widens that from one hostile character to most of Unicode. It is the last thing a
  frontend does before handing a string to a stream, and it is the only one of the three that
  every write goes through, including the ones nothing strips.

What is deliberately *not* covered: characters that mislead a reader without instructing the
terminal, such as the bidirectional overrides (U+202E and friends) that reverse the order text
is displayed in. Those are a rendering question that reaches every string picoagent shows,
including the model's own reply, and answering it here would only look like it had been answered.
"""
from __future__ import annotations

import re
from typing import Any

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
#: 2. CSI, in both its two-character (``ESC [``) and single-byte (``\x9b``) forms: parameters,
#:    then intermediate bytes, then a final byte that is optional so a truncated sequence at the
#:    end of a message is still consumed. The intermediates are what make ``ESC [ 3 1 SP h``
#:    consume the ``h``, which reads like a letter stolen from the following text and is instead
#:    the same parse a terminal makes - ``h`` is a final byte, so the ``h`` was never going to be
#:    displayed. Dropping the intermediates to keep it would leave the tail of every genuine
#:    sequence that has one (``ESC [ 0 SP q``, the cursor style) in the text as `` q``.
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


def safe_for_stream(text: str, stream: Any) -> str:
    """``text`` with every character ``stream`` cannot encode replaced by its ``\\uXXXX`` escape.

    The escaping is a round trip through the stream's own codec with ``backslashreplace``, which
    is deliberately narrower than ``repr``. ``repr`` would answer this and the terminal-control
    question in one line, at the price of quoting the whole string and escaping every newline in
    it - and the notice channel exists to deliver a multi-line ``/model list`` as lines. A round
    trip touches only what the codec cannot represent: a listing stays a listing, ``café`` stays
    ``café`` on a UTF-8 terminal, and the character that would have raised arrives as the six
    printable characters that name it, which is more than it deserves and more than a person gets
    from a traceback.

    Against the stream's codec, not UTF-8, because the crash is a property of the pair. A lone
    surrogate defeats every codec; ``日本語`` defeats an ASCII console and not a UTF-8 one. Asking
    the stream what it can encode covers both, and re-decoding with the same codec is exact:
    ``encode`` produced those bytes from that codec a moment ago. A stream that names a codec
    Python cannot round trip is not a reason to raise from the one function whose purpose is that
    the write does not, so the fallback is ASCII, which every stream that could accept the write
    at all can carry.
    """
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        return text.encode(encoding, "backslashreplace").decode(encoding)
    except (LookupError, UnicodeError):
        return text.encode("ascii", "backslashreplace").decode("ascii")


def describe_exception(exc: BaseException, limit: int = MAX_MESSAGE_CHARS) -> str:
    """``"TypeName: message"``, where both halves are treated as text the raiser chose.

    The message half is the obvious one. The name half is untrusted for a less obvious reason:
    ``type(name, bases, ns)`` accepts any string, so a plugin can raise an exception whose class
    is called whatever a picoagent line looks like - and a metaclass can make ``__name__`` a
    property, which turns *reading the name* into running the raiser's code. Both halves are
    therefore read inside their own guard. A single guard around one f-string was not one: the
    name was read in the fallback as well, so a name that raised raised again from the line that
    exists to survive it, and the exception left the function whose whole point is that nothing
    does. Every caller is a catch whose job is that a failure does not end the session, and an
    exception escaping *from the report* undoes exactly that.

    Three details carry that promise, and none of them is style:

    * ``BaseException``, not ``Exception``. The raiser chooses what to raise, and a
      ``KeyboardInterrupt`` from a property is not the user asking to stop.
    * ``str(exc)`` rather than ``{exc}`` in an f-string. Formatting calls ``__format__``, which a
      raiser can define separately from ``__str__``; ``str()`` calls ``__str__`` and is required
      by the interpreter to hand back a ``str``.
    * ``"".join`` rather than an f-string to put the halves together. What ``str()`` returns may
      be an instance of a ``str`` subclass, which can define ``__format__`` again; ``join`` reads
      the characters and calls nothing, and what it returns is a plain ``str``, so every step
      after this one - the substitution, the length, the slice - is arithmetic on text.
    """
    try:
        name = str(type(exc).__name__)
    except BaseException:  # noqa: BLE001 - reading the name runs the raiser's code
        name = "<its name could not be read>"
    try:
        message = str(exc)
    except BaseException:  # noqa: BLE001 - the raiser's __str__ is as untrusted as its output
        message = "<its message could not be rendered>"
    return safe_for_display("".join((name, ": ", message)), limit)
