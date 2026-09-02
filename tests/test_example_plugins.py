"""Behavioural tests for the example plugins, loaded through the real loader."""
import tempfile, unittest
from pathlib import Path
from helpers import CaptureFrontend, ScriptedProvider, call, make_runtime, run, text, ROOT
from picoagent.core.loop import AgentLoop
from picoagent.core.types import Message
from picoagent.plugins import loader

PLUGINS = ROOT / "examples/plugins"


def load(rt, name):
    return loader.load_plugin(PLUGINS / name, rt, loader.TrustStore(rt.cwd / "home"), allow_untrusted=True)


class PermissionGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _rt(self, turns, answer=True):
        rt = make_runtime(self.tmp, provider=ScriptedProvider(turns), frontend=CaptureFrontend(answer=answer))
        load(rt, "permission-gate")
        return rt

    def test_dangerous_command_is_blocked_when_user_declines(self):
        rt = self._rt([[call("shell", command="rm -rf build")], [text("ok")]], answer=False)
        run(AgentLoop(rt).run("clean"))
        self.assertIn("declined", rt.frontend.tool_results()[0].content)

    def test_dangerous_command_runs_when_user_confirms(self):
        rt = self._rt([[call("shell", command="rm -rf /tmp/does-not-exist-xyz")], [text("ok")]], answer=True)
        run(AgentLoop(rt).run("clean"))
        self.assertFalse(rt.frontend.tool_results()[0].is_error)

    def test_harmless_command_never_asks(self):
        rt = self._rt([[call("shell", command="echo hi")], [text("ok")]], answer=False)
        run(AgentLoop(rt).run("x"))
        self.assertIn("hi", rt.frontend.tool_results()[0].content)

    def test_protected_paths_cannot_be_read_or_written(self):
        (self.tmp / ".env").write_text("SECRET=1")
        rt = self._rt([[call("read", path=".env"), call("write", path=".env", content="x")], [text("ok")]])
        run(AgentLoop(rt).run("x"))
        self.assertTrue(all(r.is_error for r in rt.frontend.tool_results()))
        self.assertEqual((self.tmp / ".env").read_text(), "SECRET=1")

    def test_yolo_command_toggles_mode_and_skips_prompts(self):
        rt = self._rt([[call("shell", command="rm -rf /tmp/nope-xyz")], [text("ok")]], answer=False)
        run(AgentLoop(rt).handle_input("/yolo"))
        run(AgentLoop(rt).run("x"))
        self.assertFalse(rt.frontend.tool_results()[0].is_error)

    def test_readonly_mode_blocks_writes_and_shell(self):
        rt = self._rt([[call("shell", command="echo hi")], [text("ok")]])
        run(AgentLoop(rt).handle_input("/yolo readonly"))
        run(AgentLoop(rt).run("x"))
        self.assertIn("read-only", rt.frontend.tool_results()[0].content)

    def test_bundled_skill_is_registered(self):
        rt = self._rt([[text("ok")]])
        self.assertEqual(rt.skills.get("safe-refactor").source, "plugin:permission-gate")


class CompactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_compact_command_summarises_and_keeps_recent(self):
        provider = ScriptedProvider([[text("SUMMARY OF OLD STUFF")]])
        rt = make_runtime(self.tmp, provider=provider)
        rt.cfg["plugins"]["compaction"] = {"keep_recent": 2}
        load(rt, "compaction")
        for t in "abcde":
            rt.session.append_message(Message(role="user", text=t))
        run(AgentLoop(rt).handle_input("/compact"))
        texts = [m.text for m in rt.session.messages()]
        self.assertEqual(texts[0], "[Conversation summary]\nSUMMARY OF OLD STUFF")
        self.assertEqual(texts[1:], ["d", "e"])
        self.assertIn("compress", provider.calls[0]["system"].lower())

    def test_nothing_to_compact_when_history_is_short(self):
        rt = make_runtime(self.tmp, provider=ScriptedProvider([[text("x")]]))
        load(rt, "compaction")
        run(AgentLoop(rt).handle_input("/compact"))
        self.assertIn(("notice", {"text": "nothing to compact"}), rt.frontend.events)

    def test_context_overflow_error_triggers_compaction_and_retry(self):
        from picoagent.core.types import StreamEvent
        provider = ScriptedProvider([[StreamEvent("error", error="HTTP 400: context length exceeded")],
                                     [text("SUMMARY")], [text("recovered")]])
        rt = make_runtime(self.tmp, provider=provider)
        rt.cfg["plugins"]["compaction"] = {"keep_recent": 1}
        load(rt, "compaction")
        for t in "abc":
            rt.session.append_message(Message(role="user", text=t))
        run(AgentLoop(rt).run("d"))
        self.assertEqual(rt.frontend.text, "recovered")
        self.assertEqual(len(provider.calls), 3)


if __name__ == "__main__":
    unittest.main()
