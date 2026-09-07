"""Headless frontends for scripting and CI.

* ``PrintFrontend()``          - ``picoagent -p "..."``: streams the answer to stdout, and
  everything that is not the answer to stderr, so a caller can redirect one and read the other.
* ``PrintFrontend(json=True)`` - ``--json``: one JSON object per event on stdout, so other
  programs can consume the full trace (tool calls, results, errors).

Questions are answered with a safe default (``False``/``None``) because nobody is there.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, is_dataclass
from typing import Any


def _serialise(obj: Any):
    return asdict(obj) if is_dataclass(obj) else str(obj)


class PrintFrontend:
    def __init__(self, json_mode: bool = False):
        self.json_mode = json_mode

    async def emit(self, event: str, payload: dict) -> None:
        if self.json_mode:
            sys.stdout.write(json.dumps({"event": event, **payload}, default=_serialise) + "\n")
            sys.stdout.flush()
        elif event == "assistant_delta":
            sys.stdout.write(payload["text"]); sys.stdout.flush()
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
            # commentary: a plugin has to say a notice is the answer before it can displace one.
            stream = sys.stdout if payload.get("source") == "command" else sys.stderr
            stream.write(payload["text"] + "\n"); stream.flush()
        elif event == "error":
            sys.stderr.write(payload["text"] + "\n")

    async def ask(self, kind: str, prompt: str, **kw: Any) -> Any:
        return False if kind == "confirm" else None

    async def read_input(self) -> str | None:
        return None

    async def run(self, agent: Any) -> None:
        """Nothing to drive: the CLI submits the single prompt itself."""
