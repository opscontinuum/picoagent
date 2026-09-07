"""The plugin-facing surface, checked against what the docs promise about it.

Almost nothing in ``picoagent/`` calls these, and that is the expected state: a plugin
architecture's API is used by plugins, not by the core. What is *not* expected is a promise
nobody has ever run. ``docs/plugin-authoring.md`` shows a plugin author eight calls, and until
this file existed no test made any of them, so the examples in the docs were assertions about
the code rather than facts derived from it.

Each test here is written the way the docs write it, so a change that breaks the documented
call breaks the test, and a doc example that was never true fails on the day it is checked
rather than on the day somebody follows it.

``CORE_EVENTS`` gets the same treatment from the other direction: it is a list of names with
no reader, so nothing was stopping it drifting from the events the core emits or from the
table in ``docs/events-reference.md``. Both comparisons are made below, which is what turns a
comment into an invariant.
"""
from __future__ import annotations

import asyncio
import gc
import logging
import os
import re
import sys
import time
import unittest
import warnings
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from helpers import CaptureFrontend, ROOT, ScriptedProvider, call, make_runtime, run, text, temp_dir
from picoagent import __version__
from picoagent.core.events import CORE_EVENTS
from picoagent.core.loop import AgentLoop
from picoagent.core.skills import Skill
from picoagent.core.tools import is_windows
from picoagent.plugins.api import PluginAPI
from picoagent.plugins import loader
from picoagent.plugins.manifest import Manifest, unmet_requirements


@contextmanager
def capture_logs(logger_name: str):
    """``assertLogs`` with ``helpers``' blanket ``logging.disable`` lifted for the duration."""
    logging.disable(logging.NOTSET)
    try:
        case = unittest.TestCase()
        with case.assertLogs(logger_name, level="WARNING") as captured:
            yield captured
    finally:
        logging.disable(logging.CRITICAL)


@contextmanager
def expect_no_logs(logger_name: str):
    """The inverse: fail if anything at WARNING or above is logged inside the block."""
    logging.disable(logging.NOTSET)
    records: list[logging.LogRecord] = []

    class _Sink(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger, sink = logging.getLogger(logger_name), _Sink(level=logging.WARNING)
    logger.addHandler(sink)
    try:
        yield records
    finally:
        logger.removeHandler(sink)
        logging.disable(logging.CRITICAL)
    if records:
        raise AssertionError(f"unexpected warnings: {[r.getMessage() for r in records]}")


# --------------------------------------------------------------------------- CORE_EVENTS

EMIT_CALL = re.compile(r"""events\.emit\(\s*["']([a-z_]+)["']""")
DOC_ROW = re.compile(r"^\|\s*`([a-z_]+)`\s*\|")


def _emitted_by_core() -> set[str]:
    """Every literal event name the core publishes on the bus."""
    names: set[str] = set()
    for source in sorted((ROOT / "picoagent").rglob("*.py")):
        names |= set(EMIT_CALL.findall(source.read_text()))
    return names


def _documented_events() -> set[str]:
    """The event names in the first table of ``docs/events-reference.md``.

    Stops at the second heading, because the table below it lists things emitted to the
    frontend rather than to the bus, and those are deliberately not core events.
    """
    body = (ROOT / "docs" / "events-reference.md").read_text()
    first_section = body.split("\n## ", 1)[0]
    return {match.group(1) for line in first_section.splitlines() if (match := DOC_ROW.match(line))}


class CoreEventInventory(unittest.TestCase):
    """``CORE_EVENTS`` is documentation-as-code; these are the two readers it never had."""

    def test_the_scan_finds_events_at_all(self):
        """A regex that silently matched nothing would make the two tests below vacuous."""
        self.assertGreater(len(_emitted_by_core()), 10)
        self.assertGreater(len(_documented_events()), 10)

    def test_core_events_lists_exactly_what_the_core_emits(self):
        emitted = _emitted_by_core()
        self.assertEqual(CORE_EVENTS - emitted, set(), "CORE_EVENTS names an event nothing emits")
        self.assertEqual(emitted - CORE_EVENTS, set(), "the core emits an event CORE_EVENTS omits")

    def test_core_events_matches_the_events_reference_table(self):
        documented = _documented_events()
        self.assertEqual(CORE_EVENTS - documented, set(), "an event exists that the reference omits")
        self.assertEqual(documented - CORE_EVENTS, set(), "the reference documents an event that does not exist")


class UnknownEventNames(unittest.TestCase):
    """A misspelled event name used to be a plugin that loads, reports success, and never runs.

    ``api.on("tool_calls", handler)`` subscribes to a name nothing publishes. Nothing refuses
    it, nothing fires, and there is no other signal: the author sees a loaded plugin that does
    nothing. The name is checked against ``CORE_EVENTS`` at the one place plugin authors call.
    """

    def setUp(self):
        self.tmp = temp_dir()
        self.rt = make_runtime(self.tmp)
        self.api = PluginAPI(self.rt, "probe", self.tmp)

    def test_a_misspelled_core_event_is_named_in_a_warning(self):
        with capture_logs("picoagent.events") as captured:
            self.api.on("tool_calls", lambda payload, rt: None)
        message = "\n".join(captured.output)
        self.assertIn("tool_calls", message)
        self.assertIn("probe", message)

    def test_a_real_core_event_is_silent(self):
        with expect_no_logs("picoagent.events"):
            self.api.on("tool_call", lambda payload, rt: None)

    def test_another_plugins_namespaced_event_is_silent(self):
        """``api.emit`` publishes as ``"<plugin>:<event>"``, so a colon means somebody else's."""
        with expect_no_logs("picoagent.events"):
            self.api.on("mcp:server_ready", lambda payload, rt: None)

    def test_the_handler_is_subscribed_either_way(self):
        """A warning, not a refusal: the core does not get to decide a plugin is wrong."""
        with capture_logs("picoagent.events"):
            self.api.on("tool_calls", lambda payload, rt: None)
        self.assertEqual(self.rt.events.listeners("tool_calls"), 1)


# --------------------------------------------------------------------------- documented API

class DocumentedPluginApi(unittest.TestCase):
    """One test per call in ``docs/plugin-authoring.md``, written the way the docs write it."""

    def setUp(self):
        self.tmp = temp_dir()

    def _api(self, provider=None, frontend=None) -> PluginAPI:
        self.rt = make_runtime(self.tmp, provider=provider, frontend=frontend)
        return PluginAPI(self.rt, "probe", self.tmp)

    # -- api.register_frontend(MyTUI())  "replace the REPL"
    def test_register_frontend_replaces_the_frontend_api_ui_answers_with(self):
        api = self._api()
        replacement = CaptureFrontend()
        api.register_frontend(replacement)
        self.assertIs(api.ui, replacement)
        self.assertIs(self.rt.frontend, replacement)

    # -- api.register_skill(skill)
    def test_register_skill_makes_slash_skill_expand_and_reach_the_model(self):
        provider = ScriptedProvider([[text("deployed")]])
        api = self._api(provider=provider)
        api.register_skill(Skill(name="deploy", description="Run the deployment checklist",
                                 path=self.tmp / "SKILL.md", body="1. run the tests\nTarget: $ARGUMENTS"))
        run(AgentLoop(self.rt).handle_input("/skill:deploy prod"))
        sent = provider.calls[0]["messages"][0].text
        self.assertIn("run the tests", sent)
        self.assertIn("prod", sent, "$ARGUMENTS was not substituted")

    def test_register_skill_puts_the_description_in_the_system_prompt(self):
        provider = ScriptedProvider([[text("ok")]])
        api = self._api(provider=provider)
        api.register_skill(Skill(name="deploy", description="Run the deployment checklist",
                                 path=self.tmp / "SKILL.md", body="body"))
        run(AgentLoop(self.rt).run("hi"))
        self.assertIn("Run the deployment checklist", provider.calls[0]["system"])

    # -- api.set_active_tools(["read", "shell"])  "read-only mode; None restores all"
    def test_set_active_tools_narrows_what_the_model_is_offered(self):
        provider = ScriptedProvider([[text("ok")]])
        api = self._api(provider=provider)
        api.set_active_tools(["read"])
        run(AgentLoop(self.rt).run("hi"))
        self.assertEqual([spec.name for spec in provider.calls[0]["tools"]], ["read"])

    def test_set_active_tools_none_restores_all_of_them(self):
        provider = ScriptedProvider([[text("ok")], [text("ok")]])
        api = self._api(provider=provider)
        api.set_active_tools(["read"])
        api.set_active_tools(None)
        run(AgentLoop(self.rt).run("hi"))
        self.assertEqual(sorted(spec.name for spec in provider.calls[0]["tools"]),
                         sorted(api.all_tools()))

    def test_set_active_tools_does_not_unregister_the_tools_it_hides(self):
        """``all_tools`` still names them, which is what makes the narrowing reversible."""
        api = self._api()
        api.set_active_tools(["read"])
        self.assertIn("shell", api.all_tools())

    # -- api.get_active_tools()
    def test_get_active_tools_reads_back_what_set_active_tools_set(self):
        api = self._api()
        api.set_active_tools(["read", "shell"])
        self.assertEqual(sorted(api.get_active_tools()), ["read", "shell"])

    def test_get_active_tools_is_every_tool_when_nothing_was_narrowed(self):
        api = self._api()
        self.assertEqual(sorted(api.get_active_tools()), sorted(api.all_tools()))

    def test_get_active_tools_lets_a_plugin_narrow_without_clobbering_another(self):
        """The reason the getter exists: subtract from the current set, not from every tool."""
        api = self._api()
        api.set_active_tools(["read", "write", "shell"])
        api.set_active_tools([name for name in api.get_active_tools() if name != "shell"])
        self.assertEqual(sorted(api.get_active_tools()), ["read", "write"])

    # -- api.unregister_tool(name)
    def test_unregister_tool_removes_it_from_the_registry_entirely(self):
        api = self._api()
        api.unregister_tool("shell")
        self.assertNotIn("shell", api.all_tools())
        self.assertIsNone(self.rt.tools.get("shell"))

    def test_unregister_tool_is_harder_than_hiding_it(self):
        """``set_active_tools`` hides a tool from the model; this removes it from the process.

        Worth the distinction because another plugin can reach a hidden tool through
        ``rt.tools.get`` and run it, and cannot reach an unregistered one.
        """
        api = self._api()
        api.set_active_tools(["read"])
        self.assertIsNotNone(self.rt.tools.get("shell"))
        api.unregister_tool("shell")
        self.assertIsNone(self.rt.tools.get("shell"))

    def test_unregister_tool_is_silent_about_a_name_that_was_never_registered(self):
        api = self._api()
        api.unregister_tool("no-such-tool")

    # -- await api.set_model("gpt-4.1", provider="openai")
    def test_set_model_emits_model_select_with_the_documented_payload(self):
        api = self._api()
        seen: list[dict] = []
        api.on("model_select", lambda payload, rt: seen.append(dict(payload)))
        run(api.set_model("gpt-4.1", provider="openai"))
        self.assertEqual(seen[0]["model"], "gpt-4.1")
        self.assertEqual(seen[0]["previous"], "test")
        self.assertEqual(seen[0]["provider"], "openai")

    def test_set_model_changes_the_model_for_subsequent_turns(self):
        provider = ScriptedProvider([[text("ok")]])
        api = self._api(provider=provider)
        run(api.set_model("gpt-4.1"))
        self.assertEqual(api.model, "gpt-4.1")
        run(AgentLoop(self.rt).run("hi"))
        self.assertEqual(self.rt.model, "gpt-4.1")

    def test_set_model_without_a_provider_keeps_the_current_one(self):
        api = self._api()
        before = self.rt.provider_name
        run(api.set_model("gpt-4.1"))
        self.assertEqual(self.rt.provider_name, before)

    # -- api.set_thinking("high")
    def test_set_thinking_reaches_the_provider_call(self):
        provider = ScriptedProvider([[text("ok")]])
        api = self._api(provider=provider)
        api.set_thinking("high")
        run(AgentLoop(self.rt).run("hi"))
        self.assertEqual(self.rt.thinking, "high")

    # -- code, output = await api.exec("git", "status")
    def test_exec_returns_the_exit_code_and_the_output(self):
        api = self._api()
        code, output = run(api.exec("python3", "-c", "print('hello')"))
        self.assertEqual(code, 0)
        self.assertIn("hello", output)

    def test_exec_reports_a_non_zero_exit_rather_than_raising(self):
        api = self._api()
        code, _ = run(api.exec("python3", "-c", "raise SystemExit(3)"))
        self.assertEqual(code, 3)

    def test_exec_combines_stderr_into_the_same_string(self):
        api = self._api()
        _, output = run(api.exec("python3", "-c", "import sys; sys.stderr.write('to-stderr')"))
        self.assertIn("to-stderr", output)

    def test_exec_runs_in_the_project_directory(self):
        api = self._api()
        _, output = run(api.exec("python3", "-c", "import os; print(os.getcwd())"))
        self.assertEqual(Path(output.strip()).resolve(), self.tmp.resolve())

    def test_exec_does_not_hand_the_child_a_credential_from_the_environment(self):
        """The documented rule, and the reason ``api.exec`` exists rather than ``create_subprocess``."""
        import os
        os.environ["PICOAGENT_PROBE_TOKEN"] = "sekrit"
        try:
            api = self._api()
            _, output = run(api.exec("python3", "-c",
                                     "import os; print(os.environ.get('PICOAGENT_PROBE_TOKEN', 'ABSENT'))"))
        finally:
            del os.environ["PICOAGENT_PROBE_TOKEN"]
        self.assertIn("ABSENT", output)
        self.assertNotIn("sekrit", output)

    def test_exec_passes_a_named_variable_through_env(self):
        api = self._api()
        _, output = run(api.exec("python3", "-c", "import os; print(os.environ['GH_TOKEN'])",
                                 env={"GH_TOKEN": "named-by-the-call-site"}))
        self.assertIn("named-by-the-call-site", output)

    # -- api.register_system_prompt_section(...) and its inverse
    def test_register_system_prompt_section_adds_a_block_to_the_prompt(self):
        api = self._api()
        api.register_system_prompt_section("mine", lambda: "# House rules\nno force pushes")
        self.assertIn("no force pushes", self.rt.prompt.build())

    def test_a_section_renders_every_turn_rather_than_once(self):
        api = self._api()
        counter = iter(range(10))
        api.register_system_prompt_section("mine", lambda: f"tick {next(counter)}")
        self.assertIn("tick 0", self.rt.prompt.build())
        self.assertIn("tick 1", self.rt.prompt.build())

    def test_remove_system_prompt_section_takes_a_block_back_out_of_the_prompt(self):
        api = self._api()
        api.register_system_prompt_section("mine", lambda: "# House rules\nno force pushes")
        api.remove_system_prompt_section("mine")
        self.assertNotIn("no force pushes", self.rt.prompt.build())

    def test_remove_system_prompt_section_can_drop_a_built_in_one(self):
        api = self._api()
        self.assertIn("# Environment", self.rt.prompt.build())
        api.remove_system_prompt_section("env")
        self.assertNotIn("# Environment", self.rt.prompt.build())

    def test_remove_system_prompt_section_forgets_the_name_rather_than_blanking_it(self):
        """Not the same as ``set_section(name, lambda: "")``: ``build()`` skips an empty section,
        so the two render alike, but the blanked one is still registered and still runs every
        turn, and a plugin reading ``rt.prompt.sections`` still finds it there."""
        api = self._api()
        api.register_system_prompt_section("mine", lambda: "# House rules")
        api.remove_system_prompt_section("mine")
        self.assertNotIn("mine", self.rt.prompt.sections)

    def test_remove_system_prompt_section_is_silent_about_a_name_that_was_never_set(self):
        """The same answer ``unregister_tool`` gives: removing what is not there is not an error,
        so a plugin can undo its own registration without first proving it made one."""
        api = self._api()
        before = self.rt.prompt.build()
        api.remove_system_prompt_section("never-registered")
        self.assertEqual(self.rt.prompt.build(), before)


# --------------------------------------------------------------------------- manifest fields

class ShippedManifestFields(unittest.TestCase):
    """``Manifest`` fields nothing in the core reads, checked against the manifests that ship."""

    def setUp(self):
        self.manifests = [Manifest.load(path.parent)
                          for path in sorted(ROOT.glob("examples/plugins/*/plugin.toml"))]

    def test_there_are_manifests_to_check(self):
        self.assertGreater(len(self.manifests), 5)

    def test_requires_is_parsed_off_the_manifests_that_set_it(self):
        """Eight shipped plugins declare ``requires``; nothing enforces it, so this is the
        only thing standing between the field and silently becoming unparsed."""
        declared = [m for m in self.manifests if m.requires]
        self.assertGreater(len(declared), 0, "no shipped manifest sets 'requires'")
        for manifest in declared:
            with self.subTest(plugin=manifest.name):
                self.assertTrue(all(isinstance(entry, str) for entry in manifest.requires))

    def test_a_requires_value_of_the_wrong_shape_costs_the_value_not_the_plugin(self):
        root = temp_dir()
        (root / "plugin.toml").write_text('name = "p"\nentry = "p:register"\nrequires = 3\n')
        self.assertEqual(Manifest.load(root).requires, [])


# --------------------------------------------------------------------------- requires

class RequiresIsCheckedAndWarnedAbout(unittest.TestCase):
    """``requires`` shipped as "informational for now": documented, written by eight manifests,
    read by nothing. Read now - and answered with a warning rather than a refusal, because a
    field nobody's declaration was ever tested against is no ground to withhold a plugin on."""

    def _manifest(self, requires: str) -> Manifest:
        root = temp_dir()
        (root / "plugin.toml").write_text(f'name = "p"\nentry = "p:register"\n{requires}\n')
        return Manifest.load(root)

    def test_a_constraint_the_running_version_meets_says_nothing(self):
        manifest = self._manifest('requires = ["picoagent>=0.1"]')
        self.assertEqual(unmet_requirements(manifest, "0.1.0"), [])

    def test_a_constraint_the_running_version_misses_names_both_versions(self):
        manifest = self._manifest('requires = ["picoagent>=0.3"]')
        complaint = "\n".join(unmet_requirements(manifest, "0.1.0"))
        self.assertIn("picoagent>=0.3", complaint)
        self.assertIn("0.1.0", complaint)

    def test_a_bare_name_asks_for_no_particular_version(self):
        self.assertEqual(unmet_requirements(self._manifest('requires = ["picoagent"]'), "0.1.0"), [])

    def test_a_constraint_that_cannot_be_read_is_reported_rather_than_raised(self):
        manifest = self._manifest('requires = ["picoagent ~ 0.1"]')
        complaint = "\n".join(unmet_requirements(manifest, "0.1.0"))
        self.assertIn("picoagent ~ 0.1", complaint)

    def test_a_requirement_naming_something_else_is_reported_rather_than_guessed_at(self):
        """``requires`` is about picoagent's version; a package a plugin imports is ``python_deps``.
        Read as a picoagent constraint it would silently pass, which is worse than saying so."""
        manifest = self._manifest('requires = ["requests>=2"]')
        self.assertIn("requests>=2", "\n".join(unmet_requirements(manifest, "0.1.0")))

    def test_every_shipped_manifest_is_satisfied_by_the_running_picoagent(self):
        """The eight declarations that were never read by anything, now that something reads them."""
        for path in sorted(ROOT.glob("examples/plugins/*/plugin.toml")):
            manifest = Manifest.load(path.parent)
            with self.subTest(plugin=manifest.name):
                self.assertEqual(unmet_requirements(manifest, __version__), [])


class RequiresAtLoadTime(unittest.TestCase):
    """A mismatch is a line on the way past, not a door. The loader has one refusal already -
    the trust check - and that one the user can answer; a version mismatch they cannot."""

    def setUp(self):
        self.tmp = temp_dir()
        self.plug = self.tmp / "myplug"
        self.plug.mkdir()
        (self.plug / "myplug.py").write_text(
            "def register(api):\n    api.register_command('hello', lambda a, rt: None, 'hi')\n")
        self.rt = make_runtime(self.tmp)
        self.trust = loader.TrustStore(self.tmp / "home")

    def _write_manifest(self, requires: str) -> None:
        (self.plug / "plugin.toml").write_text(
            f'name = "myplug"\nentry = "myplug:register"\nrequires = {requires}\n')

    def _load(self):
        return loader.load_plugin(self.plug, self.rt, self.trust, allow_untrusted=True)

    def test_a_mismatch_warns_with_the_plugin_name_the_ask_and_what_is_running(self):
        self._write_manifest('["picoagent>=99.0"]')
        with capture_logs("picoagent.plugins") as captured:
            self._load()
        warning = "\n".join(captured.output)
        self.assertIn("myplug", warning)
        self.assertIn("picoagent>=99.0", warning)
        self.assertIn(__version__, warning)

    def test_a_mismatched_plugin_loads_anyway(self):
        self._write_manifest('["picoagent>=99.0"]')
        with capture_logs("picoagent.plugins"):
            self._load()
        self.assertIsNotNone(self.rt.commands.get("hello"))

    def test_a_satisfied_requirement_is_loaded_without_a_word(self):
        self._write_manifest('["picoagent>=0.1"]')
        with expect_no_logs("picoagent.plugins"):
            self._load()




# --------------------------------------------------------------------------- api.exec's children

def _alive(pid: int) -> bool:
    """Whether ``pid`` still names a live process. POSIX only: ``os.kill`` on Windows terminates
    the process for any signal it does not recognise, signal 0 included, so the tests that call
    this are skipped there rather than asking a question that kills the answer."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:      # alive, and owned by somebody else after a pid was reused
        return True
    return True


def _gone_within(pid: int, seconds: float = 5.0) -> bool:
    """Poll rather than assert once: a signalled process exits on the kernel's schedule, and a
    grandchild is reaped by init after that, so a single check races the thing it is measuring."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.02)
    return not _alive(pid)


@unittest.skipIf(is_windows(), "pid liveness cannot be asked for on Windows without killing it")
class TimedOutExecLeavesNothingBehind(unittest.TestCase):
    """``api.exec``'s timeout path, which is the documented way for a plugin to run a command.

    ``asyncio.wait_for`` cancels the ``communicate()`` and nothing else, so the child of a call
    that timed out used to keep running with nobody left to wait for it: an orphan the session
    cannot report, cannot kill and cannot reap.
    """

    def setUp(self):
        self.tmp = temp_dir()
        self.pidfile = self.tmp / "child.pid"

    def _timed_out(self, *argv: str, timeout: float = 0.4) -> tuple[int, str]:
        api = PluginAPI(make_runtime(self.tmp), "probe", self.tmp)
        return run(api.exec(*argv, timeout=timeout))

    def _sleeper(self, preamble: str = "") -> tuple[str, ...]:
        """A child that announces its pid and then outlives any timeout a test would use."""
        return (sys.executable, "-c", f"import os, time\n{preamble}\n"
                f"open({str(self.pidfile)!r}, 'w').write(str(os.getpid()))\n"
                "time.sleep(30)\n")

    def _announced_pid(self) -> int:
        for _ in range(200):
            if self.pidfile.exists() and self.pidfile.read_text().strip():
                return int(self.pidfile.read_text())
            time.sleep(0.02)
        raise AssertionError("the child never wrote its pid")

    def test_a_timed_out_call_answers_rather_than_raising(self):
        """The documented return shape is ``(exit_code, output)``; a timeout is a result too."""
        code, output = self._timed_out(*self._sleeper())
        self.assertNotEqual(code, 0)
        self.assertIn("timed out", output)

    def test_the_child_is_dead_afterwards(self):
        self._timed_out(*self._sleeper())
        pid = self._announced_pid()
        self.assertTrue(_gone_within(pid), f"pid {pid} outlived the call that started it")

    def test_the_child_is_reaped_and_not_merely_signalled(self):
        """An unreaped child is a zombie and a ``ResourceWarning``, which is why the audit that
        first wrote this test deleted it again rather than leave the noise in the suite."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self._timed_out(*self._sleeper())
            gc.collect()
        leaked = [str(w.message) for w in caught if issubclass(w.category, ResourceWarning)]
        self.assertEqual(leaked, [])

    def test_a_child_that_ignores_sigterm_is_killed_anyway(self):
        """Asking first is a courtesy, not a condition: a child that declines still exits."""
        sleeper = self._sleeper("import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN)")
        with patch("picoagent.plugins.api.CHILD_EXIT_GRACE", 0.3):
            self._timed_out(*sleeper)
        pid = self._announced_pid()
        self.assertTrue(_gone_within(pid), f"pid {pid} survived SIGTERM and was never killed")

    def test_an_aborted_call_kills_its_child_too(self):
        """A run cancelled part-way (Ctrl-C, ``api.abort``) leaves the same orphan a timeout
        would, and the child leads its own group now, so the terminal's own signal no longer
        reaches it either. The cleanup runs, and the cancellation stays a cancellation."""
        async def start_then_cancel() -> None:
            api = PluginAPI(make_runtime(self.tmp), "probe", self.tmp)
            call = asyncio.ensure_future(api.exec(*self._sleeper(), timeout=30))
            for _ in range(200):
                if self.pidfile.exists() and self.pidfile.read_text().strip():
                    break
                await asyncio.sleep(0.02)
            call.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await call

        run(start_then_cancel())
        pid = self._announced_pid()
        self.assertTrue(_gone_within(pid), f"pid {pid} outlived the call that was cancelled")

    def test_a_grandchild_does_not_survive_the_timeout_either(self):
        """``api.exec("sh", "-c", ...)`` is a documented call, and a shell's own children are
        not the direct child: killing only that one reparents the rest and loses them."""
        self._timed_out("sh", "-c", f"sleep 30 & echo $! > {self.pidfile}; wait")
        pid = self._announced_pid()
        self.assertTrue(_gone_within(pid), f"grandchild {pid} outlived the shell that started it")


if __name__ == "__main__":
    unittest.main()
