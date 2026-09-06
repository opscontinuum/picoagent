"""Behavioural tests for the ``agent`` tool shipped by the agents plugin.

Everything runs offline through ScriptedProvider: the parent registers it, the child shares
the parent's provider registry, so one script drives both and ``provider.calls`` is a complete
record of who asked the model for what.
"""
import tempfile, unittest
from pathlib import Path
from helpers import CaptureFrontend, ScriptedProvider, call, make_runtime, run, text, ROOT
from picoagent.core.loop import AgentLoop
from picoagent.core.tools import ToolContext
from picoagent.plugins import loader

PLUGINS = ROOT / "examples/plugins"


class AgentToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.provider = None

    def _rt(self, turns):
        self.provider = ScriptedProvider(turns)
        rt = make_runtime(self.tmp, provider=self.provider, frontend=CaptureFrontend())
        loader.load_plugin(PLUGINS / "agents", rt, loader.TrustStore(rt.cwd / "home"), allow_untrusted=True)
        return rt

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

    # ------------------------------------------------------------------ recursion guard
    def test_child_cannot_see_the_agent_tool(self):
        rt = self._rt([[text("done")]])
        self._execute(rt, {"prompt": "look around"})
        offered = {spec.name for spec in self.provider.calls[0]["tools"]}
        self.assertNotIn("agent", offered)
        self.assertEqual(offered, {"read", "write", "edit", "shell"})

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
