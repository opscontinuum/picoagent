"""A deliberately simple line-based REPL with no dependencies.

It exists so picoagent works out of the box; anything fancier (scrollback,
syntax highlighting, panes) belongs in a frontend plugin.
"""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

_ANSI = {"dim": "\033[2m", "bold": "\033[1m", "red": "\033[31m", "cyan": "\033[36m",
         "yellow": "\033[33m", "off": "\033[0m"}


class PlainFrontend:
    def __init__(self, color: bool = True):
        self.color = color and sys.stdout.isatty()

    # ------------------------------------------------------------------ output
    def _print(self, text: str, *styles: str, end: str = "\n") -> None:
        if self.color and styles:
            text = "".join(_ANSI[s] for s in styles) + text + _ANSI["off"]
        print(text, end=end, flush=True)

    async def emit(self, event: str, payload: dict) -> None:
        if event == "assistant_delta":
            print(payload["text"], end="", flush=True)
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
            self._print(payload["text"], "yellow")
        elif event == "error":
            self._print("error: " + payload["text"], "red")

    @staticmethod
    def _preview(text: str, lines: int = 8) -> str:
        """First few lines of a tool result, indented, with an ellipsis if cut."""
        all_lines = text.splitlines()
        shown = "\n".join("  " + line for line in all_lines[:lines])
        return shown + ("\n  …" if len(all_lines) > lines else "")

    # ------------------------------------------------------------------ input
    async def _readline(self, prompt: str) -> str:
        """``input()`` without blocking the event loop."""
        return await asyncio.get_running_loop().run_in_executor(None, input, prompt)

    async def ask(self, kind: str, prompt: str, **kw: Any) -> Any:
        if kind == "confirm":
            answer = await self._readline(f"{prompt} [y/N] ")
            return answer.strip().lower() in ("y", "yes")
        if kind == "select":
            options = kw.get("options", [])
            for index, option in enumerate(options, 1):
                print(f"  {index}. {option}")
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
