"""Tests for the complete plugin. Offline, stdlib only, no terminal and no model."""
import importlib
import json
import sys
import unittest
from pathlib import Path

from helpers import ScriptedProvider, make_runtime, text, ROOT, temp_dir
from picoagent.core.skills import Skill
from picoagent.plugins import loader
from picoagent.plugins.api import PluginAPI

PLUGIN = ROOT / "examples/plugins/complete"


def import_plugin():
    """Import the entry module the way the loader does, fresh each time.

    A fresh exec matters for the no-readline test: the ``import readline`` at module top
    level only runs once per module object, so a cached module would keep the readline it
    already found.
    """
    spec = importlib.util.spec_from_file_location("picoagent_plugin_complete", PLUGIN / "complete.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class CompleterTests(unittest.TestCase):
    """Candidate generation, driven directly so no pty or readline binding is involved."""

    def setUp(self):
        self.tmp = temp_dir()
        self.rt = make_runtime(self.tmp, provider=ScriptedProvider([[text("ok")]]))
        self.module = import_plugin()
        self.api = PluginAPI(self.rt, "complete", PLUGIN)

    def completer(self, line_buffer=""):
        return self.module.Completer(self.api, self.module.ModelCache(self.api), lambda: line_buffer)

    async def _noop(self, args, rt):
        return None

    def register_commands(self, *names):
        for name in names:
            self.rt.commands.register(name, self._noop, f"the {name} command")

    def register_skills(self, *names):
        for name in names:
            self.rt.skills.add(Skill(name=name, description="", path=self.tmp / name / "SKILL.md", body=""))

    # ------------------------------------------------------------------ slash commands
    def test_slash_prefix_completes_registered_commands(self):
        self.register_commands("model", "model-info", "help", "new")
        self.assertEqual(self.completer().candidates("/mo", "/mo"), ["/model", "/model-info"])

    def test_command_list_comes_from_the_registry_not_a_hardcoded_list(self):
        self.register_commands("help")
        self.rt.commands.register("teleport", self._noop, "added after the completer was built")
        self.assertIn("/teleport", self.completer().candidates("/t", "/t"))

    def test_bare_slash_offers_commands_and_skills_together(self):
        self.register_commands("help")
        self.register_skills("deploy")
        self.assertEqual(self.completer().candidates("/", "/"), ["/help", "/skill:deploy"])

    # ------------------------------------------------------------------ skills
    def test_skill_prefix_completes_registered_skills(self):
        self.register_skills("es-doctor", "es-admin", "deploy")
        self.assertEqual(self.completer().candidates("/skill:es-", "/skill:es-"),
                         ["/skill:es-admin", "/skill:es-doctor"])

    def test_skill_prefix_never_offers_commands(self):
        self.register_commands("skill-runner")
        self.register_skills("deploy")
        self.assertEqual(self.completer().candidates("/skill:", "/skill:"), ["/skill:deploy"])

    # ------------------------------------------------------------------ paths
    def test_partial_filename_completes_against_the_runtime_cwd(self):
        (self.tmp / "partial-notes.md").write_text("x")
        (self.tmp / "partial-notes.bak").write_text("x")
        (self.tmp / "unrelated.md").write_text("x")
        self.assertEqual(self.completer().candidates("partial", "read partial"),
                         ["partial-notes.bak", "partial-notes.md"])

    def test_directories_are_marked_and_can_be_descended(self):
        (self.tmp / "sub").mkdir()
        (self.tmp / "sub" / "file.txt").write_text("x")
        self.assertEqual(self.completer().candidates("su", "su"), ["sub/"])
        self.assertEqual(self.completer().candidates("sub/fi", "sub/fi"), ["sub/file.txt"])

    def test_dotfiles_stay_hidden_until_the_dot_is_typed(self):
        (self.tmp / ".hidden").write_text("x")
        (self.tmp / "visible").write_text("x")
        self.assertEqual(self.completer().candidates("", "look at "), [])
        self.assertEqual(self.completer().candidates("v", "v"), ["visible"])
        self.assertEqual(self.completer().candidates(".h", ".h"), [".hidden"])

    def test_prose_word_that_names_nothing_completes_to_nothing(self):
        (self.tmp / "notes.md").write_text("x")
        self.assertEqual(self.completer().candidates("please", "please explain"), [])

    # ------------------------------------------------------------------ models
    def test_model_argument_completes_from_the_cache_without_touching_the_network(self):
        cache = self.module.ModelCache(self.api)
        cache.write(["gpt-4o-mini", "gpt-4o", "llama3"])
        self.rt.providers.register(NoNetworkProvider())
        self.assertEqual(self.completer("/model gpt").candidates("gpt", "/model gpt"),
                         ["gpt-4o", "gpt-4o-mini"])

    def test_model_cache_is_kept_per_provider(self):
        self.module.ModelCache(self.api).write(["gpt-4o"])
        self.rt.provider_name = "other"
        self.assertNotIn("gpt-4o", self.module.ModelCache(self.api).read())

    def test_current_model_is_always_completable_even_with_no_cache(self):
        self.rt.model = "some-local-build"
        candidates = self.completer("/model some").candidates("some", "/model some")
        self.assertEqual(candidates, ["some-local-build"])

    # ------------------------------------------------------------------ failure behaviour
    def test_completer_returns_nothing_rather_than_raising_on_garbage(self):
        completer = self.completer("\x00\x00 /// ::")
        for word in ["\x00", "/skill:\x00/\x00", "//////", "~" * 400, "\\", "/model \x00"]:
            self.assertIsNone(completer.complete(word, 0), word)

    def test_an_exception_inside_the_completer_degrades_to_no_completions(self):
        def explode():
            raise RuntimeError("line buffer is on fire")

        completer = self.module.Completer(self.api, self.module.ModelCache(self.api), explode)
        self.assertIsNone(completer.complete("/mo", 0))

    def test_state_past_the_last_candidate_terminates_the_completion(self):
        self.register_commands("help")
        completer = self.completer("/h")
        self.assertEqual(completer.complete("/h", 0), "/help")
        self.assertIsNone(completer.complete("/h", 1))


class NoNetworkProvider:
    """Fails loudly if anything reaches for the network during completion."""
    name = "scripted"

    async def list_models(self):
        raise AssertionError("completion must never call list_models")

    async def stream(self, **kw):
        raise AssertionError("completion must never call stream")
        yield


class TabBindingTests(unittest.TestCase):
    """The libedit branch cannot be exercised on Linux, so drive it through the docstring."""

    def setUp(self):
        self.module = import_plugin()
        self.original = self.module.readline.__doc__

    def tearDown(self):
        self.module.readline.__doc__ = self.original

    def test_gnu_readline_gets_the_gnu_syntax(self):
        self.module.readline.__doc__ = "command line editing using GNU readline"
        self.assertEqual(self.module.tab_binding(), "tab: complete")

    def test_libedit_gets_the_libedit_syntax(self):
        self.module.readline.__doc__ = "command line editing using libedit readline"
        self.assertEqual(self.module.tab_binding(), "bind ^I rl_complete")


class NoReadlineTests(unittest.TestCase):
    """Windows has no readline. The plugin must still load, and then do nothing."""

    def setUp(self):
        self.tmp = temp_dir()
        # None in sys.modules is what makes `import readline` raise a real ImportError.
        self.saved = sys.modules.get("readline")
        sys.modules["readline"] = None

    def tearDown(self):
        if self.saved is None:
            sys.modules.pop("readline", None)
        else:
            sys.modules["readline"] = self.saved

    def test_plugin_loads_through_the_real_loader_without_readline(self):
        rt = make_runtime(self.tmp, provider=ScriptedProvider([[text("ok")]]))
        manifest = loader.load_plugin(PLUGIN, rt, loader.TrustStore(self.tmp / "home"), allow_untrusted=True)
        self.assertEqual(manifest.name, "complete")

    def test_install_reports_that_it_did_nothing(self):
        rt = make_runtime(self.tmp, provider=ScriptedProvider([[text("ok")]]))
        module = import_plugin()
        self.assertIsNone(module.readline)
        self.assertIsNone(module.install(PluginAPI(rt, "complete", PLUGIN)))

    def test_no_event_handler_is_registered_when_there_is_nothing_to_complete(self):
        rt = make_runtime(self.tmp, provider=ScriptedProvider([[text("ok")]]))
        module = import_plugin()
        module.register(PluginAPI(rt, "complete", PLUGIN))
        self.assertEqual(rt.events.listeners("agent_settled"), 0)


class RefreshTests(unittest.TestCase):
    """The one network call the plugin makes, and where it is allowed to happen."""

    def setUp(self):
        self.tmp = temp_dir()
        self.rt = make_runtime(self.tmp, provider=ListingProvider())
        self.module = import_plugin()
        self.api = PluginAPI(self.rt, "complete", PLUGIN)

    def test_settled_agent_refreshes_the_cache_in_the_background(self):
        import asyncio

        cache = self.module.ModelCache(self.api)
        refresh = self.module.ModelRefresh(self.api, cache)

        async def drive():
            await refresh.on_settled({}, self.rt)
            await refresh._task

        asyncio.run(drive())
        self.assertEqual(json.loads(cache.path.read_text())["scripted"], ["fake-large", "fake-small"])

    def test_refresh_happens_at_most_once_per_session(self):
        import asyncio

        provider = self.rt.providers.get("scripted")
        refresh = self.module.ModelRefresh(self.api, self.module.ModelCache(self.api))

        async def drive():
            for _ in range(3):
                await refresh.on_settled({}, self.rt)
                if refresh._task:
                    await refresh._task

        asyncio.run(drive())
        self.assertEqual(provider.list_calls, 1)


class ListingProvider(ScriptedProvider):
    name = "scripted"

    def __init__(self):
        super().__init__([[text("ok")]])
        self.list_calls = 0

    async def list_models(self):
        self.list_calls += 1
        return ["fake-small", "fake-large"]


if __name__ == "__main__":
    unittest.main()
