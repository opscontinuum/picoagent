"""Headless frontends for scripting and CI.

* ``PrintFrontend()``          - ``picoagent -p "..."``: streams the answer to stdout.
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
            # A slash command's whole output is a notice; without this, `-p "/model list"`
            # (and every other command) printed nothing at all in non-JSON mode.
            sys.stdout.write(payload["text"] + "\n"); sys.stdout.flush()
        elif event == "error":
            sys.stderr.write(payload["text"] + "\n")

    async def ask(self, kind: str, prompt: str, **kw: Any) -> Any:
        return False if kind == "confirm" else None

    async def read_input(self) -> str | None:
        return None

    async def run(self, agent: Any) -> None:
        """Nothing to drive: the CLI submits the single prompt itself."""
