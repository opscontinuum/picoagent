"""Headless frontends for scripting and CI.

* ``PrintFrontend()``          - ``picoagent -p "..."``: streams the answer to stdout, and
  everything that is not the answer to stderr, so a caller can redirect one and read the other.
* ``PrintFrontend(json=True)`` - ``--json``: one JSON object per event on stdout, so other
  programs can consume the full trace (tool calls, results, errors).

Questions are answered with a safe default (``False``/``None``) because nobody is there.

Every write goes out through :func:`~picoagent.core.text.safe_for_stream`, the JSON one
included. ``json.dumps`` already escapes to ASCII, so there the guard is a no-op and the record
is unchanged; it is there so that "each of these writes is encodable" is a property of the
method rather than of an argument about one branch, and so a later ``ensure_ascii=False`` cannot
quietly make a crash reachable again.

A known limit, in the same shape as the model's own deltas: a plugin may emit ``assistant_delta``
and reach the ``-p`` answer on stdout, unstripped. That is not the leak it looks like. A plugin
is in-process code with the user's privileges - it can call ``sys.stdout.write`` itself, and a
per-chunk stripper is defeated by splitting one escape sequence across two emits, which is what
makes the model's deltas unstripped in the first place. The forgery that *is* worth blocking is
the one a filter can actually block: a plugin claiming its notice is a command's answer, which
``_attested`` below rewrites, because there the claim is a field a program reads rather than a
byte a terminal obeys. Crashing the write is not part of the limit: an ``assistant_delta`` no
codec can encode killed the session, and it goes through the guard like everything else.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, is_dataclass
from typing import Any

from ..core.commands import COMMAND_SOURCE
from ..core.text import safe_for_stream, strip_terminal_controls


def _serialise(obj: Any):
    return asdict(obj) if is_dataclass(obj) else str(obj)


def _attested(payload: dict) -> dict:
    """A copy of a ``notice`` payload whose ``source`` is the dispatcher's own or absent.

    Only :data:`~picoagent.core.commands.COMMAND_SOURCE` proves a notice is a command's output,
    and only the dispatcher has it. Everything else that emits a notice - a plugin, the startup
    path - builds its own payload, so the word ``"command"`` sitting in that key says nothing
    about who put it there. Rewriting the key rather than only branching on it keeps ``--json``
    honest as well: there the claim is a field a program reads rather than a choice of stream,
    and a forgery left in place would be believed by the consumer instead of by the shell.
    """
    if payload.get("source") is COMMAND_SOURCE:
        return payload
    return {key: value for key, value in payload.items() if key != "source"}


class PrintFrontend:
    def __init__(self, json_mode: bool = False):
        self.json_mode = json_mode

    async def emit(self, event: str, payload: dict) -> None:
        if event == "notice":
            payload = _attested(payload)
        if self.json_mode:
            # Not sanitised, unlike the two branches below, because this stream is data rather
            # than a terminal: `json.dumps` escapes every character a terminal would obey (it is
            # ASCII-only by default, so the C1 range goes too), and what a program reads back is
            # the text as it was. Stripping here would edit a record instead of a display. A
            # consumer that echoes a field to its own terminal owns that step, the same way it
            # owns every other rendering decision `--json` hands it.
            record = json.dumps({"event": event, **payload}, default=_serialise)
            sys.stdout.write(safe_for_stream(record, sys.stdout) + "\n")
            sys.stdout.flush()
        elif event == "assistant_delta":
            sys.stdout.write(safe_for_stream(payload["text"], sys.stdout)); sys.stdout.flush()
        elif event == "assistant_end":
            sys.stdout.write("\n")
        elif event == "notice":
            # One event name carries two different things, so the channel is chosen per notice.
            # A slash command's whole output is a notice, and it is what `-p "/model list"` was
            # asked to produce, so it goes to stdout with the answer; without that, every command
            # printed nothing at all in non-JSON mode. Anything else is commentary about the
            # session (a startup warning, a plugin's own advisory) and on stdout it lands inside
            # the bytes the caller captured and parsed, so it goes to stderr with the rest of the
            # diagnostics, where a person still reads it and a pipe does not. Unmarked means
            # commentary, and _attested has already dropped a `source` that only claims to be
            # the dispatcher's, so the branch below reads a key core is the only writer of.
            stream = sys.stdout if payload.get("source") else sys.stderr
            stream.write(safe_for_stream(strip_terminal_controls(payload["text"]), stream) + "\n")
            stream.flush()
        elif event == "error":
            text = safe_for_stream(strip_terminal_controls(payload["text"]), sys.stderr)
            sys.stderr.write(text + "\n")

    async def ask(self, kind: str, prompt: str, **kw: Any) -> Any:
        return False if kind == "confirm" else None

    async def read_input(self) -> str | None:
        return None

    async def run(self, agent: Any) -> None:
        """Nothing to drive: the CLI submits the single prompt itself."""
