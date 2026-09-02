"""The agent loop end-to-end with a scripted provider, plus the plugin loader and trust store."""
import hashlib, json, tempfile, unittest
from pathlib import Path
from helpers import CaptureFrontend, ScriptedProvider, call, make_runtime, run, text, ROOT
from picoagent.core.loop import AgentLoop
from picoagent.plugins import loader
from picoagent.plugins.api import PluginAPI


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _rt(self, turns):
        self.provider = ScriptedProvider(turns)
        return make_runtime(self.tmp, provider=self.provider)

    def test_text_only_turn_ends_loop(self):
        rt = self._rt([[text("hello")]])
        run(AgentLoop(rt).run("hi"))
        self.assertEqual(rt.frontend.text, "hello"); self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual([m.role for m in rt.session.messages()], ["user", "assistant"])

    def test_tool_call_executes_then_model_sees_result(self):
        rt = self._rt([[call("shell", command="echo pong")], [text("done")]])
        run(AgentLoop(rt).run("ping"))
        second = self.provider.calls[1]["messages"]
        self.assertEqual(second[-1].role, "tool"); self.assertIn("pong", second[-1].tool_results[0].content)
        self.assertEqual(rt.frontend.text, "done")

    def test_plugin_can_block_a_tool_call(self):
        rt = self._rt([[call("shell", command="rm -rf /")], [text("ok")]])
        rt.events.on("tool_call", lambda p, c: {"block": True, "reason": "nope"} if "rm" in p["args"]["command"] else None, owner="gate")
        run(AgentLoop(rt).run("x"))
        r = rt.frontend.tool_results()[0]
        self.assertTrue(r.is_error); self.assertIn("nope", r.content)

    def test_plugin_can_rewrite_tool_args(self):
        rt = self._rt([[call("shell", command="echo a")], [text("ok")]])
        rt.events.on("tool_call", lambda p, c: {"args": {"command": "echo rewritten"}})
        run(AgentLoop(rt).run("x"))
        self.assertIn("rewritten", rt.frontend.tool_results()[0].content)

    def test_unknown_tool_returns_error_result(self):
        rt = self._rt([[call("teleport", to="mars")], [text("ok")]])
        run(AgentLoop(rt).run("x"))
        self.assertTrue(rt.frontend.tool_results()[0].is_error)

    def test_before_agent_start_can_rewrite_system_prompt(self):
        rt = self._rt([[text("ok")]])
        rt.events.on("before_agent_start", lambda p, c: {"system_prompt": p["system_prompt"] + "\nEXTRA"})
        run(AgentLoop(rt).run("x"))
        self.assertTrue(self.provider.calls[0]["system"].endswith("EXTRA"))

    def test_skills_are_advertised_in_system_prompt(self):
        rt = self._rt([[text("ok")]])
        d = self.tmp / "skills/foo"; d.mkdir(parents=True); (d / "SKILL.md").write_text("---\ndescription: Foo it\n---\nbody")
        rt.skills.add_dir(self.tmp / "skills", "project")
        run(AgentLoop(rt).run("x"))
        self.assertIn("foo: Foo it", self.provider.calls[0]["system"])

    def test_handle_input_routes_slash_commands_without_calling_model(self):
        rt = self._rt([[text("should not run")]])
        async def cmd(args, rt): return f"got {args}"
        rt.commands.register("echo", cmd)
        run(AgentLoop(rt).handle_input("/echo hi"))
        self.assertEqual(self.provider.calls, []); self.assertIn(("notice", {"text": "got hi"}), rt.frontend.events)

    def test_steer_message_is_delivered_after_tool_batch(self):
        rt = self._rt([[call("shell", command="true")], [text("ok")]])
        rt.queue.append(("steer", "focus on tests"))
        run(AgentLoop(rt).run("x"))
        self.assertEqual(self.provider.calls[1]["messages"][-1].text, "focus on tests")

    def test_provider_error_emits_error_and_stops(self):
        from picoagent.core.types import StreamEvent
        rt = self._rt([[StreamEvent("error", error="boom")]])
        run(AgentLoop(rt).run("x"))
        self.assertIn(("error", {"text": "boom"}), rt.frontend.events)


class PluginLoaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.plug = self.tmp / "myplug"; self.plug.mkdir()
        (self.plug / "plugin.toml").write_text('name="myplug"\nentry="myplug:register"\nskills=["skills"]\n')
        (self.plug / "myplug.py").write_text(
            "def register(api):\n    api.register_command('hello', lambda a, rt: None, 'hi')\n    api.on('x', lambda p, c: {'seen': True})\n")
        (self.plug / "skills/s").mkdir(parents=True); (self.plug / "skills/s/SKILL.md").write_text("---\ndescription: d\n---\nb")
        self.rt = make_runtime(self.tmp)
        self.trust = loader.TrustStore(self.tmp / "home")

    def test_untrusted_plugin_is_refused(self):
        self.assertIsNone(loader.load_plugin(self.plug, self.rt, self.trust))

    def test_trusted_plugin_registers_commands_events_and_skills(self):
        self.trust.trust(loader.Manifest.load(self.plug))
        m = loader.load_plugin(self.plug, self.rt, self.trust)
        self.assertEqual(m.name, "myplug")
        self.assertIsNotNone(self.rt.commands.get("hello"))
        self.assertTrue(run(self.rt.events.emit("x", {})).get("seen"))
        self.assertEqual(self.rt.skills.get("s").source, "plugin:myplug")

    def test_modified_plugin_loses_trust(self):
        self.trust.trust(loader.Manifest.load(self.plug))
        (self.plug / "myplug.py").write_text("def register(api): pass\n")
        self.assertIsNone(loader.load_plugin(self.plug, self.rt, self.trust))

    def test_plugin_tool_overrides_builtin(self):
        api = PluginAPI(self.rt, "p", self.plug)
        class FakeRead:
            name, description, parameters = "read", "fake", {"type": "object", "properties": {}}
            async def execute(self, a, c): ...
        api.register_tool(FakeRead())
        self.assertEqual(self.rt.tools.get("read").description, "fake")

    def test_plugin_config_is_scoped_by_name(self):
        self.rt.cfg["plugins"]["p"] = {"mode": "ask"}
        self.assertEqual(PluginAPI(self.rt, "p", self.plug).plugin_config(), {"mode": "ask"})


class TrustChangeTests(unittest.TestCase):
    """A plugin that changed after approval must be distinguishable from one never approved:
    the first is code the user vetted being replaced, and needs a different response."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.plug = self.tmp / "myplug"
        self.plug.mkdir()
        (self.plug / "plugin.toml").write_text('name="myplug"\nentry="myplug:register"\n')
        (self.plug / "myplug.py").write_text("def register(api): pass\n")
        self.trust = loader.TrustStore(self.tmp / "home")
        self.manifest = loader.Manifest.load(self.plug)

    def test_status_is_new_before_approval(self):
        self.assertEqual(self.trust.status(self.manifest), "new")

    def test_status_is_trusted_after_approval(self):
        self.trust.trust(self.manifest)
        self.assertEqual(self.trust.status(self.manifest), "trusted")

    def test_status_is_changed_when_the_entry_module_is_edited(self):
        self.trust.trust(self.manifest)
        (self.plug / "myplug.py").write_text("def register(api): pass  # edited\n")
        self.assertEqual(self.trust.status(self.manifest), "changed")

    def test_describe_change_names_the_file_that_moved(self):
        self.trust.trust(self.manifest)
        (self.plug / "myplug.py").write_text("def register(api): pass  # edited\n")
        described = "\n".join(self.trust.describe_change(self.manifest))
        self.assertIn("myplug.py: modified", described)
        self.assertNotIn("plugin.toml", described)

    def test_a_legacy_bare_fingerprint_record_still_loads(self):
        """Older versions stored a plain string; those users shouldn't be locked out."""
        home = self.tmp / "home"
        home.mkdir(parents=True, exist_ok=True)
        (home / "trust.json").write_text(json.dumps({"myplug": loader.plugin_fingerprint(self.manifest)}))
        store = loader.TrustStore(home)
        self.assertTrue(store.is_trusted(self.manifest))
        self.assertEqual(store.status(self.manifest), "trusted")

    def test_a_legacy_record_says_it_cannot_detail_the_change(self):
        home = self.tmp / "home"
        home.mkdir(parents=True, exist_ok=True)
        (home / "trust.json").write_text(json.dumps({"myplug": "stale-fingerprint"}))
        store = loader.TrustStore(home)
        self.assertEqual(store.status(self.manifest), "changed")
        self.assertIn("cannot say which file", "\n".join(store.describe_change(self.manifest)))

    def test_fingerprint_is_unchanged_from_the_previous_scheme(self):
        """Existing trust.json files must keep matching, or every user is re-prompted."""
        expected = hashlib.sha256((self.plug / "plugin.toml").read_bytes())
        expected.update((self.plug / "myplug.py").read_bytes())
        self.assertEqual(loader.plugin_fingerprint(self.manifest), expected.hexdigest())


class LoadReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.plug = self.tmp / "myplug"
        self.plug.mkdir()
        (self.plug / "plugin.toml").write_text('name="myplug"\nentry="myplug:register"\n')
        (self.plug / "myplug.py").write_text("def register(api): pass\n")
        self.rt = make_runtime(self.tmp)
        self.rt.cfg["plugins"]["enabled"] = [str(self.plug)]

    def test_an_untrusted_plugin_is_reported_as_new(self):
        report = loader.load_all(self.rt)
        self.assertEqual([(n, r) for n, r, _ in report.skipped], [("myplug", "new")])
        self.assertEqual(report.loaded, [])

    def test_a_changed_plugin_is_reported_as_changed_and_needs_review(self):
        loader.TrustStore(Path(self.rt.cfg["_user_dir"])).trust(loader.Manifest.load(self.plug))
        (self.plug / "myplug.py").write_text("def register(api): pass  # edited\n")
        report = loader.load_all(self.rt)
        self.assertEqual([(n, r) for n, r, _ in report.skipped], [("myplug", "changed")])
        self.assertEqual(len(report.needs_review()), 1)

    def test_a_trusted_plugin_loads_and_is_not_skipped(self):
        loader.TrustStore(Path(self.rt.cfg["_user_dir"])).trust(loader.Manifest.load(self.plug))
        report = loader.load_all(self.rt)
        self.assertEqual([m.name for m in report.loaded], ["myplug"])
        self.assertEqual(report.skipped, [])


if __name__ == "__main__":
    unittest.main()
