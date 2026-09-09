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
* The parent's ``tool_call`` gate, borrowed for the duration of each call. See below.

Why the child does not get the parent's event bus
-------------------------------------------------
A ``Runtime`` builds its own :class:`EventBus`, so a child made this way starts with the parent's
tools and none of the parent's handlers. Every safety gate in this codebase is a ``tool_call``
handler (permission-gate, credential-guard, es-doctor), so a child inherited ``shell`` while
leaving behind the thing that asks before ``rm -rf``. The child's ``SilentFrontend`` answers
``False`` to every confirmation, which never helped, because with an empty bus nothing asked.

Assigning ``child.events = parent.events`` closes that in one line and opens worse holes. Every
event the child raises would drive the parent's handlers, unmarked and indistinguishable from the
parent's own: this tool's cap handler would see the *parent's* ``turn_end`` and abort the parent
at turn N, a ``context`` handler would compact a history it was not looking at, and the rules
plugin would deliver a rule into the parent's conversation from the child's ``turn_end``.
Handlers here hold per-session state and a reference to the parent runtime; a second concurrent
session driving all of their events is not something any of them was written for.

Forwarding one event is a smaller claim than sharing the bus, but it is the same hazard in
miniature, and it caught the rules plugin: see "Forwarded to everyone, and marked" below for
which half of that plugin fired for the child, and what a stateful handler has to do about it.

So the child gets one subscription instead: :meth:`AgentTool._gate_through_parent` re-emits the
child's ``tool_call`` payload on the parent's bus and adopts the verdict, so a block, a reason, or
a rewrite of the arguments lands on the child's call exactly as it would on the parent's. Nothing
else crosses. ``tool_call`` is the only event core lets a handler stop before an effect happens
(``AgentLoop._execute_tools`` is the one place a ``block`` turns into a refusal), which is why
forwarding it is enough for gates and why forwarding it is a set of one rather than a list of
plugin names somebody has to keep current.

**What this does not cover.** A plugin that enforces somewhere other than ``tool_call`` is not
enforcing on the child. A ``tool_result`` redactor does not scrub the child's tool output, a
``context`` handler does not see the child's history, and a ``session_start`` check does not run
for the child. That failure is silent: the child proceeds and nothing reports the gap. If you
write a gate, put it on ``tool_call`` or the ``agent`` tool is a way around it. If a future event
becomes blockable, add it to :data:`FORWARDED_EVENTS` and to the test that pins that set, which
is where a reader who assumed more was covered finds out that it is not.

Forwarded to everyone, and marked: which default is the safe one
---------------------------------------------------------------
Forwarding is unconditional. Every ``tool_call`` handler on the parent's bus is offered the
child's call, and the forwarded payload carries ``delegated: True`` (plus ``delegated_by``, the
tool that ran the child) so a handler that cares can tell whose call it is. The alternative was
the mirror image: forward only to handlers that opt in, so the default is that a child's call is
invisible. Both defaults are wrong for somebody; the question is who, and how loudly.

Decide it on the plugin whose author never thought about delegation at all.

* **Unconditional forwarding (what this does).** A gate written with no notion of child agents
  still gates the child, because a gate is a pure function of the call: it answers "may this
  proceed, and with what arguments", and that answer does not depend on which loop produced the
  call. A *stateful* handler that never checks the mark mutates parent-session state on child
  activity. That is a real bug, and this file shipped with it: the rules plugin queued a rule
  delivery for a file only the child ever read, spent the parent's once-per-session marker on it,
  and injected the text into the parent's conversation at the parent's next ``turn_end``. The
  damage is bounded, though. It is context and bookkeeping, it lands in the transcript where a
  human can see it, and it cannot authorise an action.
* **Opt-in forwarding.** A stateful plugin is correct without changing a line, and
  permission-gate, credential-guard and es-doctor stop gating the child until somebody edits
  each of them. The child keeps ``write``, ``edit`` and ``shell`` and loses the thing that asks
  before ``rm -rf``. Nothing reports it. That is precisely the finding this forwarding exists to
  close, re-opened as a default, and every gate written after today starts out inside it.

Enforcement fails closed; state fails loudly. A plugin that ignores the mark delivers a rule into
the wrong conversation, which is a bug with a transcript attached. A gate that is never offered
the call permits a deletion nobody was asked about, which is the vulnerability. So the child's
call goes to everyone, and telling a child's call from a parent's is one dictionary lookup.

**What a plugin has to do about it.** Nothing, if its ``tool_call`` handler only reads the call
and returns a verdict; that is most of them. If the handler writes state that something else on
the parent's bus later reads (a counter, a once-per-session set, a queue drained at
``turn_end``), open it with::

    if event.get("delegated"):
        return None       # a child's call: gate it if you gate, but do not remember it

Of the shipped plugins only ``rules`` needed that; see
:meth:`rules.RuleEngine.on_tool_call`, which is the only ``tool_call`` handler in
``examples/plugins`` that keeps state. permission-gate, credential-guard and es-doctor were left
untouched on purpose: they are verdict-only, and needing no edit is the property this default is
chosen for.

**A third-party plugin that has not been updated** behaves exactly as it did before the mark
existed. If it is a gate, it works, and it now also covers delegated calls. If it is stateful, it
still counts the child's work as the parent's: it is not blocked, not warned, and nothing in the
run says so. The mark is opt-in *for the plugin that needs it*, which is the cost of this default
and is stated here rather than discovered later.

Only the verdict crosses back. The parent's handlers run on a copy, and of what they leave on it
the child's event takes :data:`VERDICT_KEYS` and nothing else, so a parent handler cannot use a
forwarded call to smuggle payload keys into the child's loop, and the ``delegated`` mark itself
never reaches the child's bus.

The child holds ``write``, ``edit`` and ``shell`` like the parent does. Its own frontend answers
every question with the safe default, so a tool that asks for itself gets ``no``; a parent gate
that asks reaches the parent's real frontend, because it holds the parent's API object.
Delegating does not launder an action: ask the user before delegating something you would have
asked about doing yourself, and never build a delegated prompt out of file, log or web text.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from picoagent.core.loop import AgentLoop, Runtime
from picoagent.core.session import Session
from picoagent.core.tools import ToolContext, truncate
from picoagent.core.types import ToolResult

#: Child events replayed on the parent's bus. One entry, because ``tool_call`` is the only event
#: whose handlers can stop an action before it happens. Anything added here must be an event where
#: a handler's verdict still means something when the child, not the parent, produced it.
FORWARDED_EVENTS = ("tool_call",)

#: Payload key set on every forwarded call, so a handler can tell a child's call from the
#: parent's. Set on the copy the parent's handlers see; never merged back onto the child's event.
DELEGATED_KEY = "delegated"

#: What a forwarded emit is allowed to bring back. A gate's whole answer is "may this proceed"
#: (``block``, ``reason``, ``blocked_by``) and "with what arguments" (``args``); anything else a
#: parent handler leaves on the payload is state of the parent's run and stays there.
VERDICT_KEYS = ("block", "reason", "blocked_by", "args")

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
        # The session's limits apply here as everywhere: a child is bounded by max_tokens on an
        # ordinary day, but max_tokens is the user's setting, and the parent's context is what an
        # oversized answer breaks. Cut first and append the footers after, so the sentence that
        # says the answer is incomplete cannot itself be what the cut removed.
        answer, was_truncated = truncate(answer, ctx.config["tool_output_max_bytes"],
                                         ctx.config["tool_output_max_lines"])
        if was_truncated:
            answer += "\n[truncated]"
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
        for event in FORWARDED_EVENTS:
            child.events.on(event, self._gate_through_parent, owner="agents")
        return child

    async def _gate_through_parent(self, payload: dict, child: Runtime) -> dict:
        """Put the child's ``tool_call`` to the parent's handlers and return their verdict.

        The parent's bus is emitted on with the parent's runtime, not the child's, for two
        reasons. Handlers were registered against the parent and read state through it, so
        ``rt.cfg`` and ``rt.frontend`` mean what they meant when the plugin was loaded; a gate
        that asks a human reaches the human. And a handler that responds to something alarming by
        setting ``rt.abort`` stops the parent, which :meth:`_bound_turns` already watches through
        ``ctx.abort``, so the child stops at its next turn rather than running on inside a session
        that has been cancelled.

        The copy the parent's handlers see is marked ``delegated``, because they are about to be
        asked about a call their own runtime did not make. Every gate may ignore the mark and be
        right; a handler that keeps state must read it or it books the child's work against the
        parent's session. The module docstring argues why that is the default.

        Only :data:`VERDICT_KEYS` come back. ``EventBus.emit`` returns the whole merged payload,
        including the mark and anything a parent handler bolted on, and merging all of it into the
        child's event would make a forwarded call a channel into the child's loop. Copying also
        means a handler that raises midway leaves no half-verdict on the child's event.
        """
        parent = self.api.rt
        forwarded = dict(payload, **{DELEGATED_KEY: True, "delegated_by": self.name})
        verdict = await parent.events.emit("tool_call", forwarded, parent)
        return {key: verdict[key] for key in VERDICT_KEYS if key in verdict}

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
