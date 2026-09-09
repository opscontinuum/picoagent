"""`picoagent plugin add` must obtain consent *before* it installs anything.

A new file rather than an addition to ``test_plugin_upgrade.py``: that one exercises git spec
parsing and fast-forwarding against real repositories and never drives the CLI. What is under
test here is the ordering of two calls inside ``plugin_command``, which needs a different
fixture (a plugin directory, a ``TrustStore`` in a temp home, recorders around the install and
the prompt) and states a different property.

The property: ``python_deps`` are pip-installed from a manifest nobody has approved yet. A
source distribution executes its build script during install, so consent collected afterwards
is consent to something that already ran. Ordering is the whole security control, so it is
asserted directly - a call log, not a "did it install" boolean.
"""
from __future__ import annotations

import argparse
import io
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from helpers import ROOT, temp_dir  # noqa: F401  (puts picoagent on sys.path)
from picoagent import cli
from picoagent.plugins import loader

PLUGIN_TOML = """\
name = "consent-probe"
version = "0.1.0"
entry = "consent_probe:register"
description = "a plugin whose dependency must not be installed unasked"
python_deps = ["evil-sdist==1.0"]
"""


class PluginAddConsentTests(unittest.TestCase):
    """Drive ``plugin_command(add)`` with recorders in place of pip and the prompt."""

    def setUp(self):
        self.tmp = temp_dir()
        self.home = self.tmp / "home"
        self.project = self.tmp / "project"
        self.project.mkdir(parents=True)
        self.plugin = self.tmp / "consent-probe"
        self.plugin.mkdir()
        (self.plugin / "plugin.toml").write_text(PLUGIN_TOML)
        (self.plugin / "consent_probe.py").write_text("def register(api):\n    pass\n")

        self._old_home = os.environ.get("PICOAGENT_HOME")
        self._old_cwd = Path.cwd()
        os.environ["PICOAGENT_HOME"] = str(self.home)
        os.chdir(self.project)

        self.calls: list[str] = []          # the ordering this test exists for
        self.shown_at_prompt = ""           # stdout as it stood when consent was asked

    def tearDown(self):
        os.chdir(self._old_cwd)
        if self._old_home is None:
            os.environ.pop("PICOAGENT_HOME", None)
        else:
            os.environ["PICOAGENT_HOME"] = self._old_home

    # ---------------------------------------------------------------- harness
    def _add(self, answer: str | None, yes: bool = False) -> tuple[int, str]:
        """Run ``plugin add`` on the fixture. ``answer`` is what the user types, ``None`` for
        a prompt that must never happen."""
        buffer = io.StringIO()

        def record_install(manifest):
            self.calls.append(f"install:{','.join(manifest.python_deps)}")

        def record_prompt(question):
            self.calls.append("consent")
            self.shown_at_prompt = buffer.getvalue()
            if answer is None:
                raise AssertionError(f"prompted when it should not have: {question!r}")
            return answer

        args = argparse.Namespace(pcmd="add", spec=str(self.plugin), project=False, yes=yes)
        with mock.patch.object(cli.loader, "install_deps", record_install), \
                mock.patch("builtins.input", record_prompt), redirect_stdout(buffer):
            code = cli.plugin_command(args)
        return code, buffer.getvalue()

    def _trust_store(self) -> loader.TrustStore:
        return loader.TrustStore(self.home)

    # ---------------------------------------------------------------- ordering
    def test_consent_is_asked_before_anything_is_installed(self):
        self._add("y")
        self.assertEqual(self.calls[0], "consent",
                         f"pip ran before the user was asked: {self.calls}")

    def test_the_dependency_is_shown_before_consent_is_asked(self):
        """A user cannot consent to installing a package they were never shown."""
        self._add("y")
        self.assertIn("evil-sdist==1.0", self.shown_at_prompt)

    def test_declining_installs_nothing(self):
        code, out = self._add("n")
        self.assertNotIn("install:evil-sdist==1.0", self.calls, "pip ran after the user said no")
        self.assertEqual(code, 1)
        self.assertNotIn("Enable it by adding", out, "a declined plugin must not be advertised")

    def test_declining_records_no_trust(self):
        self._add("n")
        self.assertEqual(self._trust_store().data, {})

    def test_accepting_installs_and_trusts(self):
        code, _ = self._add("y")
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, ["consent", "install:evil-sdist==1.0"])
        self.assertIn("consent-probe", self._trust_store().data)

    def test_yes_stays_non_interactive(self):
        code, _ = self._add(None, yes=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, ["install:evil-sdist==1.0"])
        self.assertIn("consent-probe", self._trust_store().data)

    def test_an_already_trusted_plugin_is_not_re_prompted(self):
        """Preserved semantics: ``trust_command`` treats an unchanged fingerprint as a no-op,
        and ``add`` must not turn a re-run into a new question."""
        self._trust_store().trust(loader.Manifest.load(self.plugin))
        code, _ = self._add(None)
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, ["install:evil-sdist==1.0"])


if __name__ == "__main__":
    unittest.main()
