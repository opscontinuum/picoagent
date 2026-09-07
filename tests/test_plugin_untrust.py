"""Withdrawing an approval, including for a plugin that is no longer on disk.

A separate file from ``test_plugin_add_consent.py``: that one asserts the ordering of consent
against installation, and this one asserts the other half of the same control. An approval that
can be given and not taken back is not a decision the user holds; it is a decision they made
once. The case that forces the design is a record whose directory has been deleted, because the
record outlives the directory: it goes on standing for that path, so whatever arrives there next
reads as a plugin the user once vetted rather than as one they have never seen. Anything that
resolves its argument by reading ``plugin.toml`` cannot clear such a record - there is no
manifest left to read - so the tests drive both spellings: the directory that is gone, and the
name it was filed under.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from helpers import ROOT, make_runtime  # noqa: F401  (ROOT puts picoagent on sys.path)
from picoagent import cli
from picoagent.plugins import loader

PLUGIN_TOML = """\
name = "withdraw-probe"
version = "0.1.0"
entry = "withdraw_probe:register"
description = "a plugin the user changes their mind about"
"""

REQUIRED_TOML = PLUGIN_TOML + """\
required = true
required_reason = "it is the only check on destructive commands"
"""


class PluginUntrustTests(unittest.TestCase):
    """Drive ``plugin_command(untrust)`` against a trust store in a temp home."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.project = self.tmp / "project"
        self.project.mkdir(parents=True)

        self._old_home = os.environ.get("PICOAGENT_HOME")
        self._old_cwd = Path.cwd()
        os.environ["PICOAGENT_HOME"] = str(self.home)
        os.chdir(self.project)

    def tearDown(self):
        os.chdir(self._old_cwd)
        if self._old_home is None:
            os.environ.pop("PICOAGENT_HOME", None)
        else:
            os.environ["PICOAGENT_HOME"] = self._old_home

    # ---------------------------------------------------------------- harness
    def _plugin(self, name: str = "withdraw-probe", toml: str = PLUGIN_TOML) -> Path:
        """An installed plugin under the user's own plugin directory, approved."""
        root = self.home / "plugins" / name
        root.mkdir(parents=True)
        (root / "plugin.toml").write_text(toml.replace("withdraw-probe", name))
        (root / "withdraw_probe.py").write_text("def register(api):\n    pass\n")
        self._store().trust(loader.Manifest.load(root))
        return root

    def _delete(self, root: Path) -> None:
        """Remove the plugin directory, leaving only the approval behind."""
        for path in sorted(root.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        root.rmdir()

    def _store(self) -> loader.TrustStore:
        return loader.TrustStore(self.home)

    def _run(self, pcmd: str, spec: str | None = None) -> tuple[int, str]:
        buffer = io.StringIO()
        args = argparse.Namespace(pcmd=pcmd, spec=spec, project=False, yes=False)
        with redirect_stdout(buffer):
            code = cli.plugin_command(args)
        return code, buffer.getvalue()

    def _roots(self) -> list[str]:
        return [record.get("root") for record in self._store().data.values()]

    # ---------------------------------------------------------------- the plugin is there
    def test_withdrawing_removes_the_record(self):
        root = self._plugin()
        code, _ = self._run("untrust", str(root))
        self.assertEqual(code, 0)
        self.assertEqual(self._store().data, {})

    def test_withdrawing_says_what_it_removed_and_from_where(self):
        """Silent withdrawal is the same failure as a silent skip: the user cannot check it."""
        root = self._plugin()
        _, out = self._run("untrust", str(root))
        self.assertIn("withdraw-probe", out)
        self.assertIn(str(root), out)
        self.assertIn(str(self.home / "trust.json"), out)

    def test_the_plugin_directory_is_left_alone(self):
        """Withdrawing an approval is not uninstalling: only the decision is taken back."""
        root = self._plugin()
        self._run("untrust", str(root))
        self.assertTrue((root / "plugin.toml").exists())

    def test_the_plugin_is_untrusted_afterwards(self):
        root = self._plugin()
        self._run("untrust", str(root))
        self.assertEqual(self._store().status(loader.Manifest.load(root)), "new")

    # ---------------------------------------------------------------- the plugin is gone
    def test_a_record_whose_directory_was_deleted_can_be_withdrawn_by_path(self):
        root = self._plugin()
        self._delete(root)
        code, out = self._run("untrust", str(root))
        self.assertEqual(code, 0, out)
        self.assertEqual(self._store().data, {})

    def test_a_record_whose_directory_was_deleted_can_be_withdrawn_by_name(self):
        """The name is what a user still has after the directory is gone."""
        root = self._plugin()
        self._delete(root)
        code, out = self._run("untrust", "withdraw-probe")
        self.assertEqual(code, 0, out)
        self.assertEqual(self._store().data, {})

    def test_withdrawing_a_required_record_lets_the_session_start_again(self):
        """The refusal for a required plugin that is gone points at this command, so this is the
        test that the pointer is true. It also pins what makes that refusal a stop rather than a
        wedge: `plugin_command` builds a config and a trust store and nothing else. If it ever
        loaded plugins, the withdrawal below would raise before it could withdraw anything.
        """
        root = self._plugin(toml=REQUIRED_TOML)
        self._delete(root)
        with self.assertRaises(loader.RequiredPluginMissing):
            loader.load_all(make_runtime(self.tmp))      # a runtime over this same home
        code, out = self._run("untrust", "withdraw-probe")
        self.assertEqual(code, 0, out)
        loader.load_all(make_runtime(self.tmp))          # no refusal left to raise

    def test_the_refusal_that_names_this_command_names_it_by_the_right_spelling(self):
        """A recovery line that does not parse as a command is a lockout written politely."""
        root = self._plugin(toml=REQUIRED_TOML)
        self._delete(root)
        with self.assertRaises(loader.RequiredPluginMissing) as caught:
            loader.load_all(make_runtime(self.tmp))
        self.assertIn("picoagent plugin untrust withdraw-probe", str(caught.exception))
        code, _ = self._run("untrust", "withdraw-probe")
        self.assertEqual(code, 0)

    def test_withdrawing_makes_the_next_thing_at_that_path_a_fresh_decision(self):
        """What a left-behind record still does, and what withdrawing it stops.

        The record is a record of a directory, and the directory outlives the plugin that was in
        it. Reinstall there and the old approval answers for the new arrival; withdraw first and
        the user is asked again, which is the point of holding the decision.
        """
        root = self._plugin()
        contents = (root / "plugin.toml").read_text(), (root / "withdraw_probe.py").read_text()
        self._delete(root)
        self._run("untrust", "withdraw-probe")
        root.mkdir(parents=True)
        (root / "plugin.toml").write_text(contents[0])
        (root / "withdraw_probe.py").write_text(contents[1])
        self.assertEqual(self._store().status(loader.Manifest.load(root)), "new")

    # ---------------------------------------------------------------- nothing to withdraw
    def test_an_unknown_name_is_refused_rather_than_reported_as_done(self):
        self._plugin()
        code, out = self._run("untrust", "never-approved")
        self.assertEqual(code, 1)
        self.assertIn("never-approved", out)
        self.assertEqual(len(self._store().data), 1, "an unrelated approval was removed")

    def test_an_empty_store_is_refused(self):
        code, out = self._run("untrust", "withdraw-probe")
        self.assertEqual(code, 1)
        self.assertNotIn("withdrew", out.lower())

    def test_a_name_covering_two_checkouts_is_refused_rather_than_guessed(self):
        """Two directories can hold one plugin name, and only the user knows which they mean."""
        first = self._plugin()
        second = self.tmp / "elsewhere" / "withdraw-probe"
        second.mkdir(parents=True)
        (second / "plugin.toml").write_text(PLUGIN_TOML)
        (second / "withdraw_probe.py").write_text("def register(api):\n    pass\n")
        self._store().trust(loader.Manifest.load(second))

        code, out = self._run("untrust", "withdraw-probe")
        self.assertEqual(code, 1)
        self.assertIn(str(first), out)
        self.assertIn(str(second), out)
        self.assertEqual(len(self._store().data), 2, "an ambiguous name removed something")

        code, _ = self._run("untrust", str(second))
        self.assertEqual(code, 0)
        self.assertEqual(self._roots(), [loader.TrustStore.key(first)])

    def test_untrust_with_no_argument_is_a_usage_error(self):
        """2, the code argparse uses, because naming nothing is a usage mistake and not a refusal."""
        self._plugin()
        code, out = self._run("untrust")
        self.assertEqual(code, 2)
        self.assertIn("untrust", out)
        self.assertEqual(len(self._store().data), 1)

    # ---------------------------------------------------------------- finding the identifier
    def test_list_shows_an_approval_whose_directory_is_gone(self):
        """A record you cannot see is one you cannot pass to untrust."""
        root = self._plugin()
        self._delete(root)
        _, out = self._run("list")
        self.assertIn("withdraw-probe", out)
        self.assertIn(str(root), out)

    def test_list_still_shows_installed_plugins(self):
        root = self._plugin()
        _, out = self._run("list")
        self.assertIn("withdraw-probe", out)
        self.assertIn("trusted", out)
        self.assertIn(str(root), out)

    # ---------------------------------------------------------------- the file on disk
    def test_the_store_stays_valid_json_after_a_withdrawal(self):
        self._plugin("keeper")
        root = self._plugin()
        self._run("untrust", str(root))
        data = json.loads((self.home / "trust.json").read_text())
        self.assertEqual([record["name"] for record in data.values()], ["keeper"])


if __name__ == "__main__":
    unittest.main()
