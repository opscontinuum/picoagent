"""Shared test fixtures. Keeps individual test files short and readable."""
from __future__ import annotations
import asyncio, logging, os, sys
from pathlib import Path

logging.disable(logging.CRITICAL)   # plugin-failure tests log on purpose; keep output clean

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from picoagent.core.config import load_config      # noqa: E402
from picoagent.core.loop import Runtime            # noqa: E402
from picoagent.core.session import Session         # noqa: E402
from picoagent.core.tools import BUILTIN_TOOLS, ToolContext  # noqa: E402
from picoagent.core.types import StreamEvent, ToolCall       # noqa: E402


def run(coro):
    """Run a coroutine to completion (tests stay synchronous and readable)."""
    return asyncio.run(coro)


class CaptureFrontend:
    """Frontend that records every event and answers every question with `answer`."""
    def __init__(self, answer=True):
        self.events: list[tuple[str, dict]] = []
        self.text = ""
        self.answer = answer

    async def emit(self, event, payload):
        self.events.append((event, payload))
        if event == "assistant_delta":
            self.text += payload["text"]

    async def ask(self, kind, prompt, **kw):
        return self.answer

    async def read_input(self):
        return None

    async def run(self, agent):
        pass

    def tool_results(self):
        return [p["result"] for e, p in self.events if e == "tool_result"]


class ScriptedProvider:
    """A provider that replays a list of turns. Each turn is a list of StreamEvents.
    Lets loop tests assert exact behaviour without any network."""
    name = "scripted"

    def __init__(self, turns: list[list[StreamEvent]]):
        self.turns = turns
        self.calls: list[dict] = []

    async def stream(self, *, system, messages, tools, model, max_tokens, thinking, temperature=None):
        self.calls.append({"system": system, "messages": messages, "tools": tools})
        turn = self.turns[min(len(self.calls) - 1, len(self.turns) - 1)]
        for ev in turn:
            yield ev
        yield StreamEvent("done", usage={"input": 1, "output": 1})


def text(t: str) -> StreamEvent:
    return StreamEvent("text", text=t)


def call(name: str, **args) -> StreamEvent:
    return StreamEvent("tool_call", tool_call=ToolCall(f"id-{name}-{len(args)}", name, args))


def make_runtime(tmp: Path, provider=None, frontend=None) -> Runtime:
    """A Runtime wired like the CLI does it, but pointing at a temp dir and a fake provider."""
    os.environ["PICOAGENT_HOME"] = str(tmp / "home")
    cfg = load_config(tmp, {"model": "test", "provider": "scripted"})
    rt = Runtime(cfg, tmp, Session(tmp / "session.jsonl", tmp))
    for t in BUILTIN_TOOLS:
        rt.tools.register(t())
    if provider:
        rt.providers.register(provider)
    rt.frontend = frontend or CaptureFrontend()
    return rt


def tool_ctx(tmp: Path, **cfg_overrides) -> ToolContext:
    cfg = {"tool_output_max_bytes": 50_000, "tool_output_max_lines": 2000, "shell_timeout": 10, **cfg_overrides}
    return ToolContext(cwd=tmp, config=cfg, tool_call_id="t1", abort=asyncio.Event())
