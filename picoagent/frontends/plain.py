"""A deliberately simple line-based REPL with no dependencies.

It exists so picoagent works out of the box; anything fancier (scrollback,
syntax highlighting, panes) belongs in a frontend plugin.

Every write here goes out through :func:`~picoagent.core.text.safe_for_stream`, including the
ones nothing strips - the model's deltas, a tool result's preview, a question's prompt. What a
terminal *obeys* is a question about some of these strings; whether a string can become bytes at
all is a question about all of them, and the answer has to be at the write, because that is the
one place every string passes through and the only place that knows which codec the stream has.
"""
from __future__ import annotations

import asyncio
import getpass
import json
import sys
from typing import Any

from ..core.text import safe_for_stream, strip_terminal_controls

_ANSI = {"dim": "\033[2m", "bold": "\033[1m", "red": "\033[31m", "cyan": "\033[36m",
         "yellow": "\033[33m", "off": "\033[0m"}


class PlainFrontend:
    def __init__(self, color: bool = True):
        self.color = color and sys.stdout.isatty()

    # ------------------------------------------------------------------ output
    def _print(self, text: str, *styles: str, end: str = "\n") -> None:
        """Write one styled line. Callers pass text that is already safe to hand a terminal.

        *Stripping* is at the call site rather than here because this method is also how the
        frontend writes its *own* escape codes, and a stripper that ran last would remove them.
        *Encoding* is the opposite: it runs here, last, after the styling, because the question
        is what this stream can carry and the answer must cover every byte leaving. Our own codes
        are ASCII, so passing them through the same round trip costs nothing and means no path
        into ``print`` skips it.
        """
        if self.color and styles:
            text = "".join(_ANSI[s] for s in styles) + text + _ANSI["off"]
        print(safe_for_stream(text, sys.stdout), end=end, flush=True)

    async def emit(self, event: str, payload: dict) -> None:
        if event == "assistant_delta":
            # Unstripped, like every frontend's delta: an escape sequence can arrive split
            # across two chunks, so a per-chunk stripper would be a filter that looks like a
            # defence. Encodable is a per-chunk property, though, so that guard does apply.
            print(safe_for_stream(payload["text"], sys.stdout), end="", flush=True)
        elif event == "thinking_delta":
            self._print(payload["text"], "dim", end="")
        elif event == "assistant_end":
            print()
        elif event == "tool_start":
            call = payload["call"]
            self._print(f"⚙ {call.name} {json.dumps(call.args)[:160]}", "cyan")
        elif event == "tool_result":
            self._print(self._preview(payload["result"].content), "red" if payload["result"].is_error else "dim")
        elif event == "notice":
            self._print(strip_terminal_controls(payload["text"]), "yellow")
        elif event == "error":
            self._print("error: " + strip_terminal_controls(payload["text"]), "red")

    @staticmethod
    def _preview(text: str, lines: int = 8) -> str:
        """First few lines of a tool result, indented, with an ellipsis if cut."""
        all_lines = text.splitlines()
        shown = "\n".join("  " + line for line in all_lines[:lines])
        return shown + ("\n  …" if len(all_lines) > lines else "")

    # ------------------------------------------------------------------ input
    async def _readline(self, prompt: str) -> str:
        """``input()`` without blocking the event loop.

        The prompt is written to the stream by ``input`` itself, and a plugin asking the user
        something chooses its wording, so it is guarded like anything else that is written.
        """
        prompt = safe_for_stream(prompt, sys.stdout)
        return await asyncio.get_running_loop().run_in_executor(None, input, prompt)

    async def _read_secret(self, prompt: str) -> str:
        """A credential typed at the terminal, with the terminal not echoing it.

        ``getpass`` rather than ``input`` for the one kind of answer that must not be left on
        screen behind the person typing it, or in a screen recording, or in a shoulder's view.
        ``getpass`` falls back to an echoing read, warning as it does so, on a terminal that
        cannot turn echo off. That is the right trade here: the answer is still needed, and
        refusing to take it would leave somebody unable to configure the tool at all.
        """
        prompt = safe_for_stream(prompt, sys.stdout)
        return await asyncio.get_running_loop().run_in_executor(None, getpass.getpass, prompt)

    async def ask(self, kind: str, prompt: str, **kw: Any) -> Any:
        if kind == "input" and kw.get("secret"):
            return await self._read_secret(f"{prompt} ")
        if kind == "confirm":
            answer = await self._readline(f"{prompt} [y/N] ")
            return answer.strip().lower() in ("y", "yes")
        if kind == "select":
            options = kw.get("options", [])
            for index, option in enumerate(options, 1):
                print(safe_for_stream(f"  {index}. {option}", sys.stdout))
            answer = await self._readline(f"{prompt} [1-{len(options)}] ")
            try:
                return options[int(answer) - 1]
            except (ValueError, IndexError):
                return None
        return await self._readline(f"{prompt} ")

    async def read_input(self) -> str | None:
        try:
            return await self._readline("\n› ")
        except (EOFError, KeyboardInterrupt):
            return None

    # ------------------------------------------------------------------ main loop
    async def run(self, agent: Any) -> None:
        rt = agent.rt
        self._print(f"picoagent · {rt.provider_name}/{rt.model} · /help for commands · ! runs a shell command", "dim")
        while True:
            text = await self.read_input()
            if text is None or text.strip() in ("/exit", "/quit"):
                return
            if not text.strip():
                continue
            if text.startswith("!"):
                await self._user_shell(agent, text[1:])
                continue
            try:
                await agent.handle_input(text)
            except KeyboardInterrupt:
                rt.abort.set()
                self._print("(aborted)", "yellow")

    async def _user_shell(self, agent: Any, command: str) -> None:
        """``!cmd`` runs a command for the user's eyes only; plugins may intercept via ``user_bash``."""
        event = await agent.rt.events.emit("user_bash", {"command": command, "result": None}, agent.rt)
        if event.get("result") is None:
            proc = await asyncio.create_subprocess_shell(command, cwd=agent.rt.cwd,
                                                         stdout=asyncio.subprocess.PIPE,
                                                         stderr=asyncio.subprocess.STDOUT)
            output, _ = await proc.communicate()
            event["result"] = output.decode(errors="replace") + f"\n[exit {proc.returncode}]"
        self._print(event["result"], "dim")
