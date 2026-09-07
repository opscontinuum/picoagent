"""A ``plugin.toml`` nobody can read, and the three CLI verbs that walk over one.

``Manifest.load`` reads a file that arrives with a clone: ``.picoagent/plugins/<name>/plugin.toml``
inside a repository is content, not a decision the user made. Session startup already treats it
that way - ``load_all`` catches around every load, so one broken manifest costs its own plugin and
nothing else. The CLI did not: ``plugin list`` caught nothing at all, and ``plugin add`` and
``plugin trust`` caught two exception types out of the several a hostile file can produce, so a
repository shipping thirteen bytes of UTF-16 ended ``picoagent plugin list`` in a traceback out of
``pathlib.read_text``.

What these tests hold to: reading a manifest fails as one documented exception, and a hostile one
never stops the user seeing, adding or trusting the *other* plugins.
"""
from __future__ import annotations

import argparse
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from helpers import ROOT, make_runtime  # noqa: F401  (ROOT puts picoagent on sys.path)
from picoagent import cli
from picoagent.plugins import loader
from picoagent.plugins.manifest import Manifest, ManifestError

GOOD_TOML = """\
name = "healthy"
version = "0.2.0"
entry = "healthy:register"
description = "the plugin the user still wants to see"
"""

#: A file re-saved by an editor that writes UTF-16, and the same shape an attacker would commit.
NOT_UTF8 = b'\xff\xfename = "evil"\n'


class _PluginDirs(unittest.TestCase):
    """A user plugin directory under a temp ``PICOAGENT_HOME``, with the CWD in a temp project."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.project = self.tmp / "project"
        (self.home / "plugins").mkdir(parents=True)
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

    def _plugin(self, name: str, toml: str | bytes) -> Path:
        root = self.home / "plugins" / name
        root.mkdir(parents=True, exist_ok=True)
        path = root / "plugin.toml"
        path.write_bytes(toml) if isinstance(toml, bytes) else path.write_text(toml)
        return root

    def _run(self, pcmd: str, spec: str | None = None) -> tuple[int, str]:
        buffer = io.StringIO()
        args = argparse.Namespace(pcmd=pcmd, spec=spec, project=False, yes=True)
        with redirect_stdout(buffer):
            code = cli.plugin_command(args)
        return code, buffer.getvalue()


class ManifestLoadTests(_PluginDirs):
    """One exception type for "this manifest is unreadable", whatever made it unreadable."""

    def test_a_manifest_that_is_not_utf8_raises_manifest_error(self):
        root = self._plugin("evil", NOT_UTF8)
        with self.assertRaises(ManifestError) as caught:
            Manifest.load(root)
        self.assertIn(str(root / "plugin.toml"), str(caught.exception))

    def test_a_manifest_that_is_not_toml_raises_manifest_error(self):
        root = self._plugin("broken", 'name = "evil\n')
        with self.assertRaises(ManifestError) as caught:
            Manifest.load(root)
        self.assertIn(str(root / "plugin.toml"), str(caught.exception))

    def test_a_manifest_missing_a_required_key_raises_manifest_error(self):
        root = self._plugin("nameless", 'entry = "x:register"\n')
        with self.assertRaises(ManifestError) as caught:
            Manifest.load(root)
        self.assertIn("name", str(caught.exception))

    def test_a_manifest_whose_name_is_not_a_string_raises_manifest_error(self):
        root = self._plugin("numeric", 'name = 7\nentry = "x:register"\n')
        with self.assertRaises(ManifestError):
            Manifest.load(root)

    def test_a_missing_manifest_raises_manifest_error(self):
        root = self.home / "plugins" / "absent"
        root.mkdir()
        with self.assertRaises(ManifestError) as caught:
            Manifest.load(root)
        self.assertIn("plugin.toml", str(caught.exception))

    def test_a_manifest_that_parses_is_untouched(self):
        root = self._plugin("healthy", GOOD_TOML)
        self.assertEqual(Manifest.load(root).name, "healthy")


class PluginListTests(_PluginDirs):
    """A hostile manifest must not stop the user listing the plugins around it."""

    def test_listing_survives_a_manifest_that_is_not_utf8(self):
        self._plugin("evil", NOT_UTF8)
        code, out = self._run("list")
        self.assertEqual(code, 0, out)

    def test_the_other_plugins_are_still_listed(self):
        self._plugin("evil", NOT_UTF8)
        self._plugin("healthy", GOOD_TOML)
        _, out = self._run("list")
        self.assertIn("healthy", out)

    def test_the_unreadable_one_is_named_rather_than_skipped_silently(self):
        """A directory that vanishes from the listing reads as a plugin that is not installed."""
        self._plugin("evil", NOT_UTF8)
        _, out = self._run("list")
        self.assertIn("evil", out)
        self.assertIn("UNREADABLE", out)


class PluginTrustTests(_PluginDirs):
    """``plugin trust`` on an unreadable directory: a refusal, not a traceback."""

    def test_trusting_an_unreadable_manifest_is_refused_readably(self):
        root = self._plugin("evil", NOT_UTF8)
        code, out = self._run("trust", str(root))
        self.assertEqual(code, 1)
        self.assertIn("not a plugin directory", out)

    def test_nothing_is_approved(self):
        root = self._plugin("evil", NOT_UTF8)
        self._run("trust", str(root))
        self.assertEqual(loader.TrustStore(self.home).data, {})


class PluginAddTests(_PluginDirs):
    """``plugin add`` resolves a source and reads its manifest; the read may fail any way."""

    def test_adding_an_unreadable_manifest_is_refused_readably(self):
        root = self._plugin("evil", NOT_UTF8)
        code, out = self._run("add", str(root))
        self.assertEqual(code, 1)
        self.assertIn("is not a plugin", out)


class SessionStartupTests(_PluginDirs):
    """The loader's own behaviour is unchanged: one broken manifest costs its own plugin only."""

    def test_a_broken_manifest_does_not_stop_the_other_plugins_loading(self):
        self._plugin("evil", NOT_UTF8)
        healthy = self._plugin("healthy", GOOD_TOML)
        (healthy / "healthy.py").write_text("def register(api):\n    pass\n")
        rt = make_runtime(self.project)
        rt.cfg["_user_dir"] = str(self.home)
        report = loader.load_all(rt, allow_untrusted=True)
        self.assertIn("healthy", [manifest.name for manifest in report.loaded])


if __name__ == "__main__":
    unittest.main()
