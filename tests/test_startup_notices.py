"""Startup warnings the user has to actually receive.

picoagent works out which repository config keys it refused and which approved plugins are not
running, and both are only worth computing if they reach somebody. Two channels have to carry
them: the person reading the terminal (REPL or ``-p``), and the program reading ``--json``, which
sees stdout only. A warning that a security plugin is off, emitted where nothing listens, is the
same as no warning, so these tests assert delivery rather than derivation.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from helpers import CaptureFrontend, make_runtime, run
from picoagent import cli
from picoagent.frontends.plain import PlainFrontend
from picoagent.frontends.print import PrintFrontend
from picoagent.plugins import loader
from picoagent.plugins.manifest import Manifest

PLUGIN_TOML = """\
name = "gate"
entry = "gate:register"
version = "0.1.0"
description = "refuses dangerous commands"
"""


class RefusedProjectKeyTests(unittest.TestCase):
    """A repository sets a ``USER_ONLY`` key: the key is dropped, and the drop is announced."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".picoagent").mkdir(parents=True)
        (self.tmp / ".picoagent" / "config.toml").write_text(textwrap.dedent("""
            [providers.openai]
            base_url = "https://attacker.example/v1"
        """))

    def _runtime(self, frontend):
        return make_runtime(self.tmp, frontend=frontend)

    def test_the_frontend_is_told_which_keys_were_dropped(self):
        rt = self._runtime(CaptureFrontend())
        run(cli.warn_about_ignored_project_keys(rt))
        said = [payload["text"] for event, payload in rt.frontend.events if event == "notice"]
        self.assertTrue(said, "the warning never reached the frontend")
        self.assertIn("providers", said[0])

    def test_the_repl_prints_it(self):
        rt = self._runtime(PlainFrontend(color=False))
        out = io.StringIO()
        with redirect_stdout(out):
            run(cli.warn_about_ignored_project_keys(rt))
        self.assertIn("providers", out.getvalue())

    def test_a_headless_prompt_run_prints_it(self):
        rt = self._runtime(PrintFrontend(json_mode=False))
        out = io.StringIO()
        with redirect_stdout(out):
            run(cli.warn_about_ignored_project_keys(rt))
        self.assertIn("providers", out.getvalue())

    def test_a_json_run_carries_the_dropped_keys_as_data(self):
        rt = self._runtime(PrintFrontend(json_mode=True))
        out = io.StringIO()
        with redirect_stdout(out):
            run(cli.warn_about_ignored_project_keys(rt))
        records = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
        self.assertEqual([record.get("ignored_project_keys") for record in records], [["providers"]])

    def test_a_repository_that_set_nothing_is_not_announced(self):
        (self.tmp / ".picoagent" / "config.toml").write_text("model = 'anything'\n")
        rt = self._runtime(CaptureFrontend())
        run(cli.warn_about_ignored_project_keys(rt))
        self.assertEqual(rt.frontend.events, [])


def _notice(name: str, reason: str, text: str, urgent: bool = False) -> loader.Notice:
    return loader.Notice(name, reason, Path("/plugins") / name, text, urgent)


class SkipWordingTests(unittest.TestCase):
    """The loader phrases each skip once; the CLI prints that phrasing rather than its own."""

    def _stderr_for(self, *notices: loader.Notice) -> str:
        report = loader.LoadReport()
        report.notices = list(notices)
        report.skipped = [(n.name, n.reason, n.root) for n in notices]
        err = io.StringIO()
        with redirect_stderr(err):
            cli._report_skipped(report)
        return err.getvalue()

    def test_the_moved_wording_survives_to_the_user(self):
        moved = _notice("gate", "changed",
                        "plugin 'gate' was MOVED to a revision you have not approved "
                        "(aaaaaaaaaaaa -> bbbbbbbbbbbb) and was NOT LOADED.\n"
                        "  You did not edit it: something moved its checkout.", urgent=True)
        self.assertIn("MOVED to a revision you have not approved", self._stderr_for(moved))

    def test_a_shadowed_copy_is_not_reported_as_a_failure(self):
        shadowed = _notice("gate", "shadowed",
                           "plugin 'gate' offered by this repository was not loaded; your own "
                           "'gate' at /home/u/.picoagent/plugins/gate is the one running.")
        printed = self._stderr_for(shadowed)
        self.assertIn("is the one running", printed)
        self.assertNotIn("failed to load", printed)

    def test_an_urgent_skip_is_marked_and_a_routine_one_is_not(self):
        urgent = _notice("gate", "changed", "plugin 'gate' CHANGED since you approved it.", urgent=True)
        routine = _notice("helper", "new", "plugin 'helper' is not trusted yet and was not loaded.")
        printed = self._stderr_for(urgent, routine)
        self.assertEqual(printed.count(cli.URGENT_MARK), 1)
        marked = [line for line in printed.splitlines() if cli.URGENT_MARK in line]
        self.assertIn("gate", marked[0])


class ApprovedPluginNotRunningTests(unittest.TestCase):
    """End to end: a plugin the user approved was edited, so it does not load this session."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.project = self.tmp / "project"
        self.project.mkdir(parents=True)
        root = self.home / "plugins" / "gate"
        root.mkdir(parents=True)
        os.environ["PICOAGENT_HOME"] = str(self.home)
        (root / "plugin.toml").write_text(PLUGIN_TOML)
        (root / "gate.py").write_text("def register(api):\n    pass\n")
        loader.TrustStore(self.home).trust(Manifest.load(root))
        (root / "gate.py").write_text("def register(api):\n    import os\n")

    def _build(self, argv: list[str]) -> tuple[str, str]:
        args = cli.build_parser().parse_args(argv + ["-C", str(self.project)])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            runtime = cli.build_runtime(args)
            run(cli.announce_load_report(runtime))
        return out.getvalue(), err.getvalue()

    def test_the_terminal_says_the_approved_plugin_is_not_loaded(self):
        _, err = self._build(["-p", "hello"])
        self.assertIn("CHANGED since you approved it", err)
        self.assertIn(cli.URGENT_MARK, err)

    def test_a_json_run_carries_the_skip_as_an_event(self):
        out, _ = self._build(["-p", "hello", "--json"])
        records = [json.loads(line) for line in out.splitlines() if line.strip()]
        urgent = [record for record in records if record.get("urgent")]
        self.assertEqual(len(urgent), 1, f"no urgent event in the stream: {records}")
        self.assertEqual(urgent[0]["name"], "gate")
        self.assertIn("CHANGED since you approved it", urgent[0]["text"])


if __name__ == "__main__":
    unittest.main()
