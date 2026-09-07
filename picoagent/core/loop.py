"""The agent loop - the heart of picoagent.

One user prompt runs like this::

    handle_input(text)
      ├─ slash command?            -> run it (failures are reported, never fatal), done
      ├─ event: input              -> plugins may transform or fully handle it
      ├─ /skill:name expansion
      ├─ queued "next_turn" text   -> carried in ahead of the prompt
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

from .commands import COMMAND_SOURCE, Command, CommandRegistry
from .context import SystemPromptBuilder
from .events import EventBus
from .provider import ProviderRegistry
from .session import Session
from .skills import SkillRegistry
from .text import describe_exception
from .tools import ToolContext, ToolRegistry, missing_required_args
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
        # or "next_turn" (carried in ahead of the next user prompt). Every kind has a
        # draining site in AgentLoop; a kind without one accumulates here unseen forever.
        self.queue: list[tuple[str, str]] = []
        self._busy = False

    def is_idle(self) -> bool:
        return not self._busy

    def take_queued(self, *deliver_as: str) -> list[str]:
        """Remove and return queued messages of the named delivery kinds, in queue order.

        Several kinds at once because one draining site can serve more than one of them, and
        two filtered passes would reorder text a plugin queued as one sequence.
        """
        taken = [text for kind, text in self.queue if kind in deliver_as]
        self.queue = [(kind, text) for kind, text in self.queue if kind not in deliver_as]
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
            await self._run_command(*parsed)
            return

        event = await rt.events.emit("input", {"text": text, "images": images or [], "action": "continue"}, rt)
        if event.get("action") == "handled":
            return

        # The prompt is where messages queued between runs come in: "next_turn" by its own
        # contract, and a "steer" whose run ended before a tool batch could carry it (see
        # _turns). Draining both here is what stops either from sitting in the queue unsent.
        carried = rt.take_queued("next_turn", "steer")
        prompt = rt.skills.expand(event["text"])
        await self.run(prompt if prompt is not None else event["text"], event["images"], carried)

    async def _run_command(self, command: Command, args: str) -> None:
        """Run one slash command and show what it returned, reporting any failure to the user.

        Commands were the last plugin call-in without a catch: events and tools already have
        one, so a handler that raised unwound through here into ``PlainFrontend.run``, which
        stops at ``KeyboardInterrupt`` only. One broken ``/command`` ended the REPL and took
        the session with it. Reporting it leaves the user at a prompt with the rest of the
        session intact, which is the same trade ``_invoke`` makes for a tool that raises.

        Showing the output belongs inside that catch, because a handler is as likely to return
        the wrong type as to raise. A returned dict reached ``PlainFrontend._print``, which on a
        colour terminal concatenates the text onto its escape codes, and the resulting TypeError
        unwound to exactly where a raise used to. The type is checked here rather than left to
        the frontend so the message names the plugin's mistake instead of describing string
        concatenation, and so every frontend is handed the ``str`` its contract promises.

        ``KeyboardInterrupt`` and ``CancelledError`` do not derive from ``Exception`` and so keep
        travelling: the user asking to stop, and the loop being torn down, are not plugin bugs.

        What the exception *says* is the plugin's too, so it goes through
        :func:`~picoagent.core.text.describe_exception` rather than into an f-string. A
        ``__str__`` returning escape sequences writes over the report it is quoted in, and one
        returning five megabytes buries the session that survived the failure.
        """
        try:
            notice = await command.handler(args, self.rt)
            if notice is None:
                return
            if not isinstance(notice, str):
                raise TypeError(f"handler returned {type(notice).__name__}, expected str or None")
            await self._tell_user("notice", {"text": notice, "source": COMMAND_SOURCE})
        except Exception as exc:  # noqa: BLE001 - plugin code is untrusted; the session outlives it
            log.exception("command /%s failed", command.name)
            await self._tell_user("error", {"text": f"/{command.name} failed: {describe_exception(exc)}"})

    async def _tell_user(self, event: str, payload: dict) -> None:
        """Emit to the frontend if one is attached. For paths that are not allowed to raise.

        ``rt.frontend`` is ``None`` until the CLI or a frontend plugin sets it, and an embedder
        that drives commands without a UI never sets it. Reporting a contained failure straight
        through it raised ``AttributeError`` out of the one branch whose job is keeping the
        session alive, so the containment became the thing it was preventing.

        The streaming path deliberately does not use this. There a missing frontend means the
        answer is going nowhere, which is the caller's bug and should say so loudly rather than
        run a whole model turn into silence.
        """
        frontend = self.rt.frontend
        if frontend is None:
            log.warning("no frontend attached, dropping %s: %s", event, payload.get("text", ""))
            return
        await frontend.emit(event, payload)

    # ------------------------------------------------------------------ one prompt
    async def run(self, prompt: str, images: list[dict] | None = None,
                  carried: list[str] | None = None) -> None:
        """Run the model until it stops calling tools, then drain follow-ups.

        ``carried`` is text queued before this prompt existed; it is delivered as its own
        user-role messages ahead of the prompt, so the prompt the user typed stays theirs.
        """
        rt = self.rt
        rt._busy = True
        rt.abort.clear()
        try:
            system = await self._prepare(prompt, images or [], carried or [])
            await self._turns(system)
            await rt.events.emit("agent_end", {}, rt)
            follow_ups = rt.take_queued("follow_up")
            if follow_ups:
                await self.run("\n".join(follow_ups))   # recursion ends when the queue is empty
                return
            await rt.events.emit("agent_settled", {}, rt)
        finally:
            rt._busy = False

    async def _prepare(self, prompt: str, images: list[dict], carried: list[str]) -> str:
        """Build the system prompt, let plugins adjust it, and record the messages this prompt sends.

        Every user-role message that goes to the model is announced, not only the one the user
        typed. Text a plugin injected or queued spoke to the model in the user's voice while the
        transcript showed nothing, so a person reading along could not tell which of their
        instructions were theirs. ``kind`` says whose words these are, and a frontend that does
        not care ignores the field exactly as it ignored the extra messages.
        """
        rt = self.rt
        system = rt.prompt.build()
        skills = rt.skills.prompt_section()
        if skills:
            system += "\n\n" + skills
        event = await rt.events.emit("before_agent_start",
                                     {"prompt": prompt, "system_prompt": system, "message": None}, rt)
        if event.get("message"):
            rt.session.append_message(Message(role="user", text=event["message"], meta={"custom_type": "injected"}))
            await rt.frontend.emit("user_message", {"text": event["message"], "kind": "injected"})
        for queued in carried:
            rt.session.append_message(Message(role="user", text=queued, meta={"custom_type": "queued"}))
            await rt.frontend.emit("user_message", {"text": queued, "kind": "queued"})
        rt.session.append_message(Message(role="user", text=prompt, images=images))
        await rt.frontend.emit("user_message", {"text": prompt, "kind": "typed"})
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
                # The final turn. A steer queued from this turn_end has no batch left to follow,
                # so it stays queued and handle_input carries it in at the next prompt. It is
                # late either way; the top of a prompt beats surfacing inside a later run's tool
                # batch, where the model reads it with no idea which turn it belonged to.
                await rt.events.emit("turn_end", {"turn": turn, "message": assistant}, rt)
                return
            results = await self._execute_tools(assistant.tool_calls)
            rt.session.append_message(Message(role="tool", tool_results=results))
            await rt.events.emit("turn_end", {"turn": turn, "message": assistant, "tool_results": results}, rt)
            steer = rt.take_queued("steer")
            if steer:
                steered = "\n".join(steer)
                rt.session.append_message(Message(role="user", text=steered))
                await rt.frontend.emit("user_message", {"text": steered, "kind": "queued"})

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
        # A model omitting an argument is an expected failure, so it is a value. Letting it
        # reach the tool produced `KeyError: 'path'`, which says nothing about what to fix,
        # so small models reissued the same malformed call until the turn cap stopped them.
        missing = missing_required_args(tool, call.args)
        if missing:
            required = ", ".join(tool.parameters.get("required", []))
            return ToolResult(call.id, f"{call.name}: missing required argument(s): "
                                       f"{', '.join(missing)}. This tool requires: {required}.",
                              is_error=True)
        ctx = ToolContext(cwd=rt.cwd, config=rt.cfg, tool_call_id=call.id, abort=rt.abort, ui=rt.frontend)
        try:
            return await tool.execute(call.args, ctx)
        except Exception as exc:  # noqa: BLE001 - a tool bug must not kill the session
            log.exception("tool %s failed", call.name)
            # A tool is plugin code, so the exception's message and its class name are both its
            # words. This result is rendered in the REPL and replayed to the model next turn,
            # and neither reader benefits from an escape sequence or a megabyte of one.
            return ToolResult(call.id, describe_exception(exc), is_error=True)
