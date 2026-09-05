"""The agent loop - the heart of picoagent.

One user prompt runs like this::

    handle_input(text)
      ├─ slash command?            -> run it, done
      ├─ event: input              -> plugins may transform or fully handle it
      ├─ /skill:name expansion
      └─ run(prompt)
           ├─ event: before_agent_start   (system prompt is final after this)
           └─ repeat:
                ├─ event: turn_start
                ├─ event: context            (plugins may rewrite/compact the history)
                ├─ provider.stream(...)      -> frontend deltas, message_update events
                ├─ no tool calls?            -> stop
                ├─ event: tool_call (each)   -> block / rewrite, then execute in parallel
                ├─ event: tool_result (each)
                └─ event: turn_end
           ├─ event: agent_end
           ├─ queued follow-up prompts?      -> run them
           └─ event: agent_settled

:class:`Runtime` is the bag of registries everything shares; :class:`AgentLoop`
holds the control flow and nothing else.
"""
from __future__ import annotations

import asyncio
import copy
import logging
from pathlib import Path
from typing import Any

from .commands import CommandRegistry
from .context import SystemPromptBuilder
from .events import EventBus
from .provider import ProviderRegistry
from .session import Session
from .skills import SkillRegistry
from .tools import ToolContext, ToolRegistry
from .types import Message, ToolCall, ToolResult

log = logging.getLogger("picoagent.loop")


class Runtime:
    """Shared state for one session: registries, config, model selection, queues."""

    def __init__(self, cfg: dict, cwd: Path, session: Session):
        self.cfg, self.cwd, self.session = cfg, cwd, session
        self.events = EventBus()
        self.tools = ToolRegistry()
        self.skills = SkillRegistry()
        self.commands = CommandRegistry()
        self.providers = ProviderRegistry()
        self.prompt = SystemPromptBuilder(cfg, cwd)
        self.frontend: Any = None                 # set by the CLI or a frontend plugin
        self.model: str = cfg["model"]
        self.provider_name: str = cfg["provider"]
        self.thinking: str = cfg["thinking"]
        self.temperature: float | None = cfg["temperature"]
        self.abort = asyncio.Event()              # set to cancel the current run
        # Messages queued by plugins/frontends: (deliver_as, text) where deliver_as is
        # "steer" (after the current tool batch), "follow_up" (after the agent finishes)
        # or "next_turn" (bundled with the next user prompt).
        self.queue: list[tuple[str, str]] = []
        self._busy = False

    def is_idle(self) -> bool:
        return not self._busy

    def take_queued(self, deliver_as: str) -> list[str]:
        """Remove and return queued messages of one delivery kind."""
        taken = [text for kind, text in self.queue if kind == deliver_as]
        self.queue = [(kind, text) for kind, text in self.queue if kind != deliver_as]
        return taken


class AgentLoop:
    def __init__(self, rt: Runtime):
        self.rt = rt

    # ------------------------------------------------------------------ input routing
    async def handle_input(self, text: str, images: list[dict] | None = None) -> None:
        """Entry point for anything the user typed (or a frontend/plugin sent on their behalf)."""
        rt = self.rt
        parsed = rt.commands.parse(text)
        if parsed:
            command, args = parsed
            notice = await command.handler(args, rt)
            if notice:
                await rt.frontend.emit("notice", {"text": notice})
            return

        event = await rt.events.emit("input", {"text": text, "images": images or [], "action": "continue"}, rt)
        if event.get("action") == "handled":
            return

        prompt = rt.skills.expand(event["text"])
        await self.run(prompt if prompt is not None else event["text"], event["images"])

    # ------------------------------------------------------------------ one prompt
    async def run(self, prompt: str, images: list[dict] | None = None) -> None:
        """Run the model until it stops calling tools, then drain follow-ups."""
        rt = self.rt
        rt._busy = True
        rt.abort.clear()
        try:
            system = await self._prepare(prompt, images or [])
            await self._turns(system)
            await rt.events.emit("agent_end", {}, rt)
            follow_ups = rt.take_queued("follow_up")
            if follow_ups:
                await self.run("\n".join(follow_ups))   # recursion ends when the queue is empty
                return
            await rt.events.emit("agent_settled", {}, rt)
        finally:
            rt._busy = False

    async def _prepare(self, prompt: str, images: list[dict]) -> str:
        """Build the system prompt, let plugins adjust it, and record the user message."""
        rt = self.rt
        system = rt.prompt.build()
        skills = rt.skills.prompt_section()
        if skills:
            system += "\n\n" + skills
        event = await rt.events.emit("before_agent_start",
                                     {"prompt": prompt, "system_prompt": system, "message": None}, rt)
        if event.get("message"):
            rt.session.append_message(Message(role="user", text=event["message"], meta={"custom_type": "injected"}))
        rt.session.append_message(Message(role="user", text=prompt, images=images))
        await rt.frontend.emit("user_message", {"text": prompt})
        return event["system_prompt"]

    async def _turns(self, system: str) -> None:
        """Alternate model calls and tool batches until the model answers without tools."""
        rt = self.rt
        turn = 0
        while not rt.abort.is_set():
            turn += 1
            await rt.events.emit("turn_start", {"turn": turn}, rt)
            assistant = await self._model_turn(system)
            if assistant is None:                       # provider error already reported
                return
            if not assistant.tool_calls:
                await rt.events.emit("turn_end", {"turn": turn, "message": assistant}, rt)
                return
            results = await self._execute_tools(assistant.tool_calls)
            rt.session.append_message(Message(role="tool", tool_results=results))
            await rt.events.emit("turn_end", {"turn": turn, "message": assistant, "tool_results": results}, rt)
            steer = rt.take_queued("steer")
            if steer:
                rt.session.append_message(Message(role="user", text="\n".join(steer)))

    # ------------------------------------------------------------------ model call
    async def _model_turn(self, system: str) -> Message | None:
        """Stream one assistant message. Returns ``None`` on an unrecoverable provider error."""
        rt = self.rt
        context = await rt.events.emit("context", {"messages": copy.deepcopy(rt.session.messages()),
                                                   "system_prompt": system}, rt)
        provider = rt.providers.get(rt.provider_name)
        message = Message(role="assistant", meta={"model": rt.model})

        await rt.frontend.emit("assistant_start", {})
        async for chunk in provider.stream(system=context["system_prompt"], messages=context["messages"],
                                           tools=rt.tools.specs(), model=rt.model,
                                           max_tokens=rt.cfg["max_tokens"], thinking=rt.thinking,
                                           temperature=rt.temperature):
            if rt.abort.is_set():
                break
            if chunk.type == "text":
                message.text += chunk.text
                await rt.frontend.emit("assistant_delta", {"text": chunk.text})
                await rt.events.emit("message_update", {"delta": chunk.text}, rt)
            elif chunk.type == "thinking":
                await rt.frontend.emit("thinking_delta", {"text": chunk.text})
            elif chunk.type == "tool_call" and chunk.tool_call:
                message.tool_calls.append(chunk.tool_call)
            elif chunk.type == "done":
                message.meta["usage"] = chunk.usage
            elif chunk.type == "error":
                return await self._on_provider_error(chunk.error, system)

        await rt.frontend.emit("assistant_end", {"message": message})
        rt.session.append_message(message)
        await rt.events.emit("message_end", {"message": message}, rt)
        return message

    async def _on_provider_error(self, error: str, system: str) -> Message | None:
        """Report the error; a plugin (e.g. compaction) may fix things and ask for a retry."""
        rt = self.rt
        await rt.frontend.emit("error", {"text": error})
        event = await rt.events.emit("provider_error", {"error": error, "retry": False}, rt)
        return await self._model_turn(system) if event.get("retry") else None

    # ------------------------------------------------------------------ tools
    async def _execute_tools(self, calls: list[ToolCall]) -> list[ToolResult]:
        """Preflight every call through ``tool_call`` (sequentially, so plugins see a stable
        order), then execute the survivors - concurrently if configured."""
        rt = self.rt
        plan: list[tuple[ToolCall, ToolResult | None]] = []
        for call in calls:
            event = await rt.events.emit("tool_call", {"name": call.name, "args": call.args,
                                                       "id": call.id, "block": False}, rt)
            if event.get("block"):
                reason = event.get("reason") or f"blocked by {event.get('blocked_by', 'plugin')}"
                plan.append((call, ToolResult(call.id, f"Blocked: {reason}", is_error=True)))
            else:
                call.args = event["args"]
                plan.append((call, None))

        if rt.cfg.get("parallel_tools", True):
            return list(await asyncio.gather(*(self._execute_one(c, r) for c, r in plan)))
        return [await self._execute_one(c, r) for c, r in plan]

    async def _execute_one(self, call: ToolCall, blocked: ToolResult | None) -> ToolResult:
        """Run a single tool call (or report it as blocked) and let plugins patch the result."""
        rt = self.rt
        if blocked:
            await rt.frontend.emit("tool_result", {"call": call, "result": blocked})
            return blocked

        await rt.frontend.emit("tool_start", {"call": call})
        await rt.events.emit("tool_execution_start", {"call": call}, rt)
        result = await self._invoke(call)

        patched = await rt.events.emit("tool_result", {"name": call.name, "args": call.args, "content": result.content,
                                                       "is_error": result.is_error, "details": result.details}, rt)
        result.content, result.is_error, result.details = patched["content"], patched["is_error"], patched["details"]
        await rt.events.emit("tool_execution_end", {"call": call, "result": result}, rt)
        await rt.frontend.emit("tool_result", {"call": call, "result": result})
        return result

    async def _invoke(self, call: ToolCall) -> ToolResult:
        """Look the tool up and execute it, converting any exception into an error result."""
        rt = self.rt
        tool = rt.tools.get(call.name)
        if tool is None or not rt.tools.is_active(tool):
            return ToolResult(call.id, f"Unknown or inactive tool '{call.name}'", is_error=True)
        ctx = ToolContext(cwd=rt.cwd, config=rt.cfg, tool_call_id=call.id, abort=rt.abort, ui=rt.frontend)
        try:
            return await tool.execute(call.args, ctx)
        except Exception as exc:  # noqa: BLE001 - a tool bug must not kill the session
            log.exception("tool %s failed", call.name)
            return ToolResult(call.id, f"{type(exc).__name__}: {exc}", is_error=True)
