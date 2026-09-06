"""``agent`` - run a child agent in this process and return only what it answered.

Why a tool when a skill already does this
-----------------------------------------
The ``delegate`` skill tells the model to run ``picoagent -p '<prompt>'`` through ``shell``, and
that works. It also asks the model to build a correct shell command: quote a multi-line prompt,
escape what is inside it, pick a timeout. Small models get that wrong often enough to matter, and
a malformed command is spent tokens with nothing to show. A typed tool has two named parameters
and a schema the provider validates, so calling it correctly is most of the difficulty removed.

The second reason is a bound the skill can only ask for. ``AgentLoop._turns`` runs until the model
answers without calling a tool; there is no turn cap in core. A child that keeps re-reading the
same file never terminates on its own, and from ``shell`` the only stop is the process timeout,
which kills the run and discards everything it printed. Here the parent subscribes to the child's
``turn_end`` and sets ``runtime.abort``, so a capped child still returns the text it produced.

What the child gets
-------------------
* The parent's provider registry, provider, model and sampling settings, so it answers like the
  parent would. No second process, no re-read config, no API key handling of its own.
* A session in a temporary directory. The parent's log must not grow by the child's turns, which
  is the whole point of delegating: the noise stays over there.
* The parent's cwd, so relative paths mean the same thing in both.
* Every tool the parent currently has active, except this one. That is the recursion guard: one
  level deep by construction, with no depth counter that could be miscounted or reset.

The child holds ``write``, ``edit`` and ``shell`` like the parent does, and its frontend answers
every question with the safe default because nobody is watching it. Delegating does not launder an
action: ask the user before delegating something you would have asked about doing yourself, and
never build a delegated prompt out of file, log or web text.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from picoagent.core.loop import AgentLoop, Runtime
from picoagent.core.session import Session
from picoagent.core.tools import ToolContext
from picoagent.core.types import ToolResult

#: Model rounds a child may take before the parent stops it. High enough for a real search across
#: a tree (read, grep, read again, answer), low enough that a livelocked child costs seconds.
DEFAULT_MAX_TURNS = 12


class SilentFrontend:
    """The child's user interface: there isn't one.

    The tool result is the whole of what the parent is meant to see, so the child's streamed text
    and tool traffic go nowhere. Questions get the same safe default a headless run gives, because
    nobody is present to answer them.
    """

    async def emit(self, event: str, payload: dict) -> None:
        return None

    async def ask(self, kind: str, prompt: str, **kw: Any) -> Any:
        return False if kind == "confirm" else None

    async def read_input(self) -> str | None:
        return None

    async def run(self, agent: Any) -> None:
        return None


class AgentTool:
    """Runs one child agent per call and reports its final assistant text."""

    name = "agent"
    description = (
        "Delegate one self-contained task to a child agent and get back only its final answer. "
        "The child has your tools except this one (delegation does not nest), your working "
        "directory, and its own conversation, so its file reads and dead ends never enter yours. "
        "It cannot ask a follow-up question: put everything it needs in the prompt and say what "
        "shape of answer you want. Worth it when the answer is small and deriving it is large; "
        "not worth it for two tool calls, or for work whose intermediate steps you need to see.")
    parameters = {"type": "object", "properties": {
        "prompt": {"type": "string",
                   "description": "The child's whole task. Name files by path, spell out terms, "
                                  "restate context it cannot see, and state the answer format."},
        "max_turns": {"type": "integer",
                      "description": f"Model rounds the child may take (default {DEFAULT_MAX_TURNS}). "
                                     "Raise it for a wide search, lower it to fail fast."}},
        "required": ["prompt"]}

    def __init__(self, api: Any) -> None:
        # The tool needs the parent Runtime and ToolContext does not carry one, so keep the API
        # object the plugin was registered with; api.rt is that runtime.
        self.api = api
        self.default_max_turns = self._configured_cap(api)

    @staticmethod
    def _configured_cap(api: Any) -> int:
        """``[plugins.agents] max_turns`` in config.toml, ignoring an unusable value."""
        configured = api.plugin_config().get("max_turns", DEFAULT_MAX_TURNS)
        try:
            cap = int(configured)
        except (TypeError, ValueError):
            return DEFAULT_MAX_TURNS
        return cap if cap > 0 else DEFAULT_MAX_TURNS

    # ------------------------------------------------------------------ execution
    async def execute(self, args: dict, ctx: ToolContext) -> ToolResult:
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return ToolResult(ctx.tool_call_id, "agent needs a non-empty 'prompt': the child's whole "
                                                "task, in one string.", is_error=True)
        try:
            cap = self._requested_cap(args)
        except ValueError as exc:
            return ToolResult(ctx.tool_call_id, str(exc), is_error=True)

        # The temporary directory holds the child's session file. It goes away with the run; the
        # entries we read the answer from are in memory, so cleanup order does not matter.
        with tempfile.TemporaryDirectory(prefix="picoagent-agent-") as scratch:
            child = self._child_runtime(Path(scratch))
            progress = self._bound_turns(child, cap, ctx)
            await AgentLoop(child).run(prompt)
            answer = self._final_text(child)
            details = {"turns": progress["turns"], "model": child.model, "stopped": progress["stopped"]}

        if not answer:
            return ToolResult(ctx.tool_call_id,
                              f"The child agent produced no text in {progress['turns']} turn(s). "
                              "It may have spent them all on tool calls; try a smaller task or a "
                              "higher max_turns.", is_error=True, details=details)
        if progress["stopped"]:
            # Say the answer was cut short. A truncated answer read as a complete one is the way
            # this tool would mislead the parent.
            answer += f"\n\n[child stopped: {progress['stopped']}. This answer may be incomplete.]"
        return ToolResult(ctx.tool_call_id, answer, details=details)

    def _requested_cap(self, args: dict) -> int:
        """The effective turn cap. Raises ``ValueError`` with a message meant for the model."""
        requested = args.get("max_turns")
        if requested is None:
            return self.default_max_turns
        try:
            cap = int(requested)
        except (TypeError, ValueError):
            raise ValueError(f"max_turns must be a whole number, got {requested!r}")
        if cap < 1:
            raise ValueError(f"max_turns must be at least 1, got {cap}")
        return cap

    # ------------------------------------------------------------------ the child
    def _child_runtime(self, scratch: Path) -> Runtime:
        """A Runtime that answers like the parent but logs nowhere near it."""
        parent = self.api.rt
        child = Runtime(parent.cfg, parent.cwd, Session(scratch / "session.jsonl", parent.cwd))
        child.providers = parent.providers          # shared object: one client, one set of keys
        child.provider_name, child.model = parent.provider_name, parent.model
        child.thinking, child.temperature = parent.thinking, parent.temperature
        child.frontend = SilentFrontend()
        # active(), not names(): a plugin that narrowed the parent's tools (plan mode, read-only)
        # narrowed them for a reason, and a child must not be the way around it.
        for tool in parent.tools.active():
            if tool.name != self.name:
                child.tools.register(tool, owner="agents")
        return child

    def _bound_turns(self, child: Runtime, cap: int, ctx: ToolContext) -> dict:
        """Stop the child at ``cap`` model rounds, or as soon as the parent's run is cancelled.

        ``turn_end`` is the only place with both the turn number and whether the model is still
        calling tools. Setting ``runtime.abort`` there ends ``_turns`` at its next check, which is
        how a child gets a cap that core does not give it.
        """
        progress: dict[str, Any] = {"turns": 0, "stopped": ""}

        async def stop_when_done(payload: dict, runtime: Runtime) -> None:
            progress["turns"] = payload["turn"]
            if ctx.abort.is_set():
                progress["stopped"] = "the parent run was cancelled"
                runtime.abort.set()
            elif payload.get("tool_results") and payload["turn"] >= cap:
                progress["stopped"] = f"turn cap of {cap} reached"
                runtime.abort.set()

        child.events.on("turn_end", stop_when_done, owner="agents")
        return progress

    @staticmethod
    def _final_text(child: Runtime) -> str:
        """The child's last assistant text, or ``""`` if it never said anything.

        Read from the session rather than collected from the frontend: an aborted child still has
        its assistant message appended, so a capped run reports what it managed to say.
        """
        for message in reversed(child.session.messages()):
            if message.role == "assistant" and message.text.strip():
                return message.text.strip()
        return ""
