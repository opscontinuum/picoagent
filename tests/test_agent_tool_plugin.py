"""Behavioural tests for the ``agent`` tool shipped by the agents plugin.

Everything runs offline through ScriptedProvider: the parent registers it, the child shares
the parent's provider registry, so one script drives both and ``provider.calls`` is a complete
record of who asked the model for what.
"""
import importlib.util, sys, unittest
from pathlib import Path
from helpers import CaptureFrontend, ScriptedProvider, call, make_runtime, run, text, ROOT, temp_dir
from picoagent.core.loop import AgentLoop
from picoagent.core.tools import ToolContext
from picoagent.plugins import loader

PLUGINS = ROOT / "examples/plugins"

# The module under its own name, so a test can read the constants the tool documents itself with.
_spec = importlib.util.spec_from_file_location("picoagent_plugin_agents", PLUGINS / "agents/agent_tool.py")
agent_tool = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = agent_tool
_spec.loader.exec_module(agent_tool)


class AgentToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir()
        self.provider = None

    def _rt(self, turns, answer=True):
        self.provider = ScriptedProvider(turns)
        rt = make_runtime(self.tmp, provider=self.provider, frontend=CaptureFrontend(answer=answer))
        self._load(rt, "agents")
        return rt

    @staticmethod
    def _load(rt, plugin):
        loader.load_plugin(PLUGINS / plugin, rt, loader.TrustStore(rt.cwd / "home"), allow_untrusted=True)

    @staticmethod
    def _ctx(rt) -> ToolContext:
        """The context the loop would build for a tool call."""
        return ToolContext(cwd=rt.cwd, config=rt.cfg, tool_call_id="t1", abort=rt.abort, ui=rt.frontend)

    def _execute(self, rt, args):
        return run(rt.tools.get("agent").execute(args, self._ctx(rt)))

    # ------------------------------------------------------------------ wiring
    def test_plugin_registers_the_tool(self):
        rt = self._rt([[text("ok")]])
        self.assertIn("agent", rt.tools.names())

    def test_schema_requires_a_prompt(self):
        rt = self._rt([[text("ok")]])
        self.assertEqual(rt.tools.get("agent").parameters["required"], ["prompt"])

    # ------------------------------------------------------------------ the answer
    def test_child_answer_comes_back_as_the_tool_result(self):
        rt = self._rt([[text("CHILD ANSWER")]])
        result = self._execute(rt, {"prompt": "what is in this directory"})
        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "CHILD ANSWER")

    def test_a_long_child_answer_is_cut_to_the_sessions_limits(self):
        """The answer used to come back whole, whatever its size - the one tool in the tree
        that returned unbounded text. max_tokens bounds a child on an ordinary day; the
        session's own limits are what the parent's context is entitled to."""
        rt = self._rt([[text("A" * 200)]])
        rt.cfg["tool_output_max_bytes"] = 64
        result = self._execute(rt, {"prompt": "say a lot"})
        self.assertIn("[truncated]", result.content)
        self.assertLess(len(result.content), 200)

    def test_parent_session_is_not_extended_by_the_child(self):
        rt = self._rt([[text("CHILD ANSWER")]])
        before = len(rt.session.entries)
        self._execute(rt, {"prompt": "investigate"})
        self.assertEqual(len(rt.session.entries), before)

    def test_parent_loop_sees_only_the_child_final_text(self):
        rt = self._rt([[call("agent", prompt="what is here")], [text("CHILD ANSWER")],
                       [text("the child said: CHILD ANSWER")]])
        run(AgentLoop(rt).run("ask a child"))
        self.assertEqual(rt.frontend.tool_results()[0].content, "CHILD ANSWER")
        self.assertEqual(rt.frontend.text, "the child said: CHILD ANSWER")

    # ------------------------------------------------------------------ parent gates
    @staticmethod
    def _parent_gate(rt, block: set[str] | None = None, patch: dict | None = None) -> list[dict]:
        """A parent-side ``tool_call`` handler shaped like permission-gate's, with a record.

        Records ``(name, runtime)`` for every call it is offered, so a test can assert both that
        the child's calls reached the parent's bus and which runtime the handler was handed.
        """
        seen: list[dict] = []

        async def gate(event, runtime):
            seen.append({"name": event["name"], "args": dict(event["args"]), "runtime": runtime,
                         "delegated": event.get("delegated"), "by": event.get("delegated_by")})
            if block and event["name"] in block:
                return {"block": True, "reason": "gate says no"}
            return patch
        rt.events.on("tool_call", gate, owner="test-gate")
        return seen

    def test_parent_gate_blocks_a_child_tool_call(self):
        victim = self.tmp / "victim.txt"
        rt = self._rt([[call("write", path=str(victim), content="child was here")], [text("done")]])
        seen = self._parent_gate(rt, block={"write"})
        result = self._execute(rt, {"prompt": "write a file", "max_turns": 3})
        self.assertEqual([entry["name"] for entry in seen], ["write"])
        self.assertFalse(victim.exists())
        self.assertEqual(result.content, "done")

    def test_blocked_child_call_is_reported_to_the_child(self):
        rt = self._rt([[call("write", path=str(self.tmp / "v.txt"), content="x")], [text("done")]])
        self._parent_gate(rt, block={"write"})
        self._execute(rt, {"prompt": "write a file", "max_turns": 3})
        tool_messages = [m for m in self.provider.calls[1]["messages"] if m.role == "tool"]
        self.assertIn("Blocked: gate says no", tool_messages[0].tool_results[0].content)

    def test_the_gate_is_handed_the_parent_runtime(self):
        """Handlers read config, frontend and session through the runtime they are given."""
        rt = self._rt([[call("read", path="README.md")], [text("done")]])
        seen = self._parent_gate(rt)
        self._execute(rt, {"prompt": "read something", "max_turns": 3})
        self.assertIs(seen[0]["runtime"], rt)

    def test_the_gate_can_rewrite_a_child_call_arguments(self):
        """A patch is a verdict too: permission-gate blocks, others redirect or clamp."""
        target = self.tmp / "allowed.txt"
        target.write_text("allowed content")
        rt = self._rt([[call("read", path="/etc/shadow")], [text("done")]])
        self._parent_gate(rt, patch={"args": {"path": str(target)}})
        self._execute(rt, {"prompt": "read the file", "max_turns": 3})
        tool_messages = [m for m in self.provider.calls[1]["messages"] if m.role == "tool"]
        self.assertIn("allowed content", tool_messages[0].tool_results[0].content)

    def test_no_child_call_reaches_the_parent_gate_when_the_child_makes_none(self):
        rt = self._rt([[text("nothing to do")]])
        seen = self._parent_gate(rt)
        self._execute(rt, {"prompt": "think only"})
        self.assertEqual(seen, [])

    def test_the_shipped_permission_gate_refuses_a_child_rm_rf(self):
        """The finding, end to end: ask mode, the user declines, the child's command does not run."""
        rt = self._rt([[call("shell", command="rm -rf build")], [text("I could not delete it")]],
                      answer=False)
        self._load(rt, "permission-gate")
        result = self._execute(rt, {"prompt": "clean the build", "max_turns": 3})
        tool_messages = [m for m in self.provider.calls[1]["messages"] if m.role == "tool"]
        self.assertIn("Blocked: user declined", tool_messages[0].tool_results[0].content)
        self.assertEqual(result.content, "I could not delete it")

    def test_the_shipped_permission_gate_lets_a_safe_child_command_through(self):
        """The gate is a gate, not a wall: an undangerous command still runs for the child."""
        rt = self._rt([[call("shell", command="echo hello")], [text("it said hello")]], answer=False)
        self._load(rt, "permission-gate")
        self._execute(rt, {"prompt": "say hello", "max_turns": 3})
        tool_messages = [m for m in self.provider.calls[1]["messages"] if m.role == "tool"]
        self.assertIn("hello", tool_messages[0].tool_results[0].content)
        self.assertNotIn("Blocked", tool_messages[0].tool_results[0].content)

    def test_the_shipped_credential_guard_blocks_a_child_reading_the_key_store(self):
        """The second shipped gate, on a call the child made: the key never reaches the child."""
        secrets = self.tmp / "home" / "credentials"
        secrets.parent.mkdir(parents=True, exist_ok=True)
        secrets.write_text("openai=sk-not-a-real-key\n")
        rt = self._rt([[call("read", path=str(secrets))], [text("I could not read it")]])
        self._load(rt, "credential-guard")
        self._execute(rt, {"prompt": "read the stored keys", "max_turns": 3})
        blocked = [m for m in self.provider.calls[1]["messages"] if m.role == "tool"][0].tool_results[0]
        self.assertIn("Blocked:", blocked.content)
        self.assertNotIn("sk-not-a-real-key", blocked.content)

    # ------------------------------------------------------------------ whose call is it
    def test_a_forwarded_child_call_is_marked_delegated(self):
        """The mark is the whole of what a stateful handler has to go on, so it is pinned here.

        A gate may ignore it and stay correct. A handler that writes session state cannot: without
        this key it books the child's work against the parent's run.
        """
        rt = self._rt([[call("read", path="README.md")], [text("done")]])
        seen = self._parent_gate(rt)
        self._execute(rt, {"prompt": "read something", "max_turns": 3})
        self.assertEqual([entry["delegated"] for entry in seen], [True])
        self.assertEqual([entry["by"] for entry in seen], ["agent"])

    def test_a_call_the_parent_made_itself_is_not_marked(self):
        """Otherwise the mark would say nothing: everything would look delegated."""
        rt = self._rt([[call("read", path="README.md")], [text("done")]])
        seen = self._parent_gate(rt)
        run(AgentLoop(rt).run("read it yourself"))
        self.assertEqual([entry["delegated"] for entry in seen], [None])

    def test_a_gate_that_ignores_the_mark_still_blocks_the_child(self):
        """The point of the default: a handler written before delegation existed keeps working."""
        victim = self.tmp / "victim.txt"
        rt = self._rt([[call("write", path=str(victim), content="child was here")], [text("done")]])
        seen = self._parent_gate(rt, block={"write"})     # never looks at event["delegated"]
        self._execute(rt, {"prompt": "write a file", "max_turns": 3})
        self.assertEqual([entry["name"] for entry in seen], ["write"])
        self.assertFalse(victim.exists())

    def test_only_the_verdict_crosses_back_to_the_child(self):
        """A forwarded call is not a channel into the child's loop: the mark and any key a parent
        handler bolted on stay on the parent's side."""
        rt = self._rt([[text("ok")]])

        async def chatty(event, runtime):
            return {"block": True, "reason": "no", "args": {"path": "safe.txt"},
                    "parent_bookkeeping": 1, "delegated": "tampered"}
        rt.events.on("tool_call", chatty, owner="test-gate")
        verdict = run(rt.tools.get("agent")._gate_through_parent(
            {"name": "read", "args": {"path": "/etc/shadow"}, "id": "i", "block": False}, rt))
        self.assertEqual(set(verdict), set(agent_tool.VERDICT_KEYS))
        self.assertNotIn(agent_tool.DELEGATED_KEY, verdict)
        self.assertEqual(verdict["blocked_by"], "test-gate")

    def test_the_verdict_keys_are_the_ones_the_loop_acts_on(self):
        """Pins the set. Anything added here has to be something the child's loop reads."""
        self.assertEqual(agent_tool.VERDICT_KEYS, ("block", "reason", "blocked_by", "args"))

    # ------------------------------------------------------------------ what does not cross
    def test_only_tool_call_is_forwarded(self):
        """Pins the forwarded set. Widening it is a decision, not a side effect of an edit."""
        self.assertEqual(agent_tool.FORWARDED_EVENTS, ("tool_call",))

    def test_child_turns_do_not_drive_parent_turn_handlers(self):
        """The rules plugin keeps once-per-session state on ``turn_end`` and queues text into the
        parent's conversation from it. A child's turns must not spend that."""
        rt = self._rt([[call("read", path="README.md")], [text("done")]])
        stateful = []

        async def record(payload, runtime):
            stateful.append(payload["turn"])
        rt.events.on("turn_end", record, owner="test-rules")
        self._execute(rt, {"prompt": "read something", "max_turns": 3})
        self.assertEqual(stateful, [])
        self.assertEqual(rt.queue, [])

    def test_the_child_turn_cap_does_not_land_on_the_parent(self):
        rt = self._rt([[text("still working"), call("read", path="README.md")]])
        self._execute(rt, {"prompt": "loop forever", "max_turns": 1})
        self.assertEqual(rt.events.listeners("turn_end"), 0)
        self.assertFalse(rt.abort.is_set())

    # ------------------------------------------------------------------ recursion guard
    def test_child_cannot_see_the_agent_tool(self):
        rt = self._rt([[text("done")]])
        self._execute(rt, {"prompt": "look around"})
        offered = {spec.name for spec in self.provider.calls[0]["tools"]}
        self.assertNotIn("agent", offered)
        self.assertEqual(offered, {"read", "write", "edit", "shell"})

    def test_a_narrowed_parent_tool_set_is_inherited_narrowed(self):
        """Plan mode and read-only narrow the parent for a reason; the child stays narrowed."""
        rt = self._rt([[text("done")]])
        rt.tools.set_active(["read", "agent"])
        self._execute(rt, {"prompt": "look around"})
        offered = {spec.name for spec in self.provider.calls[0]["tools"]}
        self.assertEqual(offered, {"read"})

    # ------------------------------------------------------------------ the turn cap
    def test_max_turns_stops_a_child_that_never_stops_calling_tools(self):
        rt = self._rt([[text("still working"), call("read", path="README.md")]])
        result = self._execute(rt, {"prompt": "loop forever", "max_turns": 2})
        self.assertEqual(len(self.provider.calls), 2)
        self.assertIn("still working", result.content)
        self.assertIn("turn cap", result.content)

    def test_default_cap_applies_when_max_turns_is_omitted(self):
        rt = self._rt([[text("still working"), call("read", path="README.md")]])
        self._execute(rt, {"prompt": "loop forever"})
        self.assertEqual(len(self.provider.calls), rt.tools.get("agent").default_max_turns)

    # ------------------------------------------------------------------ bad input
    def test_missing_prompt_is_an_error_result(self):
        rt = self._rt([[text("ok")]])
        result = self._execute(rt, {})
        self.assertTrue(result.is_error)
        self.assertIn("prompt", result.content)
        self.assertEqual(self.provider.calls, [])

    def test_unusable_max_turns_is_an_error_result(self):
        rt = self._rt([[text("ok")]])
        for bad in (0, -3, "many"):
            result = self._execute(rt, {"prompt": "x", "max_turns": bad})
            self.assertTrue(result.is_error)
            self.assertIn("max_turns", result.content)

    def test_child_that_produces_no_text_is_an_error_result(self):
        rt = self._rt([[call("read", path="missing.txt")]])
        result = self._execute(rt, {"prompt": "x", "max_turns": 1})
        self.assertTrue(result.is_error)
        self.assertIn("no text", result.content)


if __name__ == "__main__":
    unittest.main()
