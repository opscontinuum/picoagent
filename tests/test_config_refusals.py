"""Config files picoagent cannot use, and settings picoagent does not read.

Two failures live here. The first is a file that will not parse: a repository's
``.picoagent/config.toml`` is read before the user has looked at anything, so a broken one is
somebody else's mistake arriving as a traceback in their terminal, for a file they did not write
and cannot be expected to debug from tomllib frames. The second is the opposite shape - a key
picoagent offers in ``DEFAULTS`` that nothing consumes, so setting it looks like a choice and is
not one.

The asymmetry the first set asserts: a repository's config may be ignored, because a repository you
cloned must not be able to deny you your own tool. Your own config may not, because ignoring it
runs the session under settings you did not choose.
"""
from __future__ import annotations

import logging
import os
import re
import unittest
from pathlib import Path

from helpers import ROOT, temp_dir  # noqa: F401  - puts the package on sys.path
from picoagent.core import config


class _ConfigDirs(unittest.TestCase):
    """A user dir and a project dir, with ``PICOAGENT_HOME`` pointed at the first."""

    def setUp(self):
        self.home = temp_dir()
        self.proj = temp_dir()
        (self.proj / ".picoagent").mkdir()
        (self.home / "config.toml").write_text('model = "user-model"\nmax_tokens = 1234\n')
        self._prev = os.environ.get("PICOAGENT_HOME")
        os.environ["PICOAGENT_HOME"] = str(self.home)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("PICOAGENT_HOME", None)
        else:
            os.environ["PICOAGENT_HOME"] = self._prev

    @property
    def project_config(self) -> Path:
        return self.proj / ".picoagent" / "config.toml"


class BrokenProjectConfigTests(_ConfigDirs):
    """A repository's config is content you cloned. Unusable, it is dropped and announced."""

    def test_invalid_toml_in_a_repository_does_not_end_the_session(self):
        self.project_config.write_text('model = "llama3\n')
        self.assertEqual(config.load_config(self.proj)["model"], "user-model")

    def test_the_users_own_settings_survive_a_broken_repository_config(self):
        """The repository loses its say; the user keeps theirs. Nothing else is disturbed."""
        self.project_config.write_text("[[[not toml")
        cfg = config.load_config(self.proj)
        self.assertEqual(cfg["max_tokens"], 1234)
        self.assertEqual(cfg["plugins"]["enabled"], [])

    def test_the_refusal_names_the_file_what_is_wrong_and_what_happens_next(self):
        self.project_config.write_text('model = "llama3\n')
        said = config.load_config(self.proj)[config.UNREADABLE_PROJECT_CONFIG_KEY]
        self.assertIn(str(self.project_config), said)
        self.assertIn("TOML", said)
        self.assertIn("your own", said)

    def test_the_refusal_is_logged_where_a_default_run_prints_it(self):
        """``logging.basicConfig(level=WARNING)`` in ``main`` is what puts this on stderr."""
        self.project_config.write_text('model = "llama3\n')
        logging.disable(logging.NOTSET)
        try:
            with self.assertLogs("picoagent.config", level="WARNING") as caught:
                config.load_config(self.proj)
        finally:
            logging.disable(logging.CRITICAL)
        self.assertIn(str(self.project_config), "\n".join(caught.output))

    def test_a_directory_where_the_config_should_be_is_refused_the_same_way(self):
        self.project_config.mkdir()
        cfg = config.load_config(self.proj)
        self.assertEqual(cfg["model"], "user-model")
        self.assertIn(str(self.project_config), cfg[config.UNREADABLE_PROJECT_CONFIG_KEY])

    @unittest.skipIf(os.geteuid() == 0, "root reads unreadable files")
    def test_a_config_with_no_read_permission_is_refused_the_same_way(self):
        self.project_config.write_text('model = "llama3"\n')
        self.project_config.chmod(0o000)
        try:
            cfg = config.load_config(self.proj)
        finally:
            self.project_config.chmod(0o600)
        self.assertEqual(cfg["model"], "user-model")
        self.assertIn(str(self.project_config), cfg[config.UNREADABLE_PROJECT_CONFIG_KEY])

    def test_a_repository_config_that_is_not_utf8_does_not_end_the_session(self):
        """``tomllib`` decodes the bytes itself, so a non-UTF-8 file fails before any parsing."""
        self.project_config.write_bytes(b'\xff\xfemodel = "x"\n')
        cfg = config.load_config(self.proj)
        self.assertEqual(cfg["model"], "user-model")
        self.assertIn(str(self.project_config), cfg[config.UNREADABLE_PROJECT_CONFIG_KEY])

    def test_a_repository_config_saved_as_utf16_does_not_end_the_session(self):
        """What a Windows editor writes when somebody re-saves the file. An accident, not an attack."""
        self.project_config.write_bytes('model = "x"\n'.encode("utf-16"))
        cfg = config.load_config(self.proj)
        self.assertEqual(cfg["model"], "user-model")
        self.assertIn(str(self.project_config), cfg[config.UNREADABLE_PROJECT_CONFIG_KEY])

    def test_a_repository_config_of_nested_arrays_does_not_end_the_session(self):
        """A recursive-descent parser runs out of stack before it runs out of input."""
        self.project_config.write_text("v = " + "[" * 8000 + "]" * 8000)
        cfg = config.load_config(self.proj)
        self.assertEqual(cfg["model"], "user-model")
        self.assertIn(str(self.project_config), cfg[config.UNREADABLE_PROJECT_CONFIG_KEY])

    def test_a_repository_config_of_nested_inline_tables_does_not_end_the_session(self):
        self.project_config.write_text("v = " + "{a = " * 2000 + "1" + "}" * 2000)
        cfg = config.load_config(self.proj)
        self.assertEqual(cfg["model"], "user-model")
        self.assertIn(str(self.project_config), cfg[config.UNREADABLE_PROJECT_CONFIG_KEY])

    def test_a_repository_config_that_parses_is_untouched(self):
        """The refusal must not cost the working case - project config is the whole feature."""
        self.project_config.write_text('model = "proj-model"\n')
        cfg = config.load_config(self.proj)
        self.assertEqual(cfg["model"], "proj-model")
        self.assertIsNone(cfg[config.UNREADABLE_PROJECT_CONFIG_KEY])

    def test_no_repository_config_at_all_is_not_a_refusal(self):
        self.assertIsNone(config.load_config(self.proj)[config.UNREADABLE_PROJECT_CONFIG_KEY])


class BrokenUserConfigTests(_ConfigDirs):
    """Your own config is a decision you made. Running without it is not picoagent's call."""

    def test_a_broken_user_config_stops_the_session(self):
        (self.home / "config.toml").write_text('model = "mine\n')
        with self.assertRaises(SystemExit) as caught:
            config.load_config(self.proj)
        self.assertIn(str(self.home / "config.toml"), str(caught.exception))

    def test_the_message_is_one_readable_line_not_a_traceback(self):
        (self.home / "config.toml").write_text("[[[not toml")
        with self.assertRaises(SystemExit) as caught:
            config.load_config(self.proj)
        message = str(caught.exception)
        self.assertNotIn("\n", message)
        self.assertFalse(re.search(r"tomllib|_parser|picoagent\.core", message), message)

    def test_the_message_says_what_the_user_can_do_about_it(self):
        (self.home / "config.toml").write_text("[[[not toml")
        with self.assertRaises(SystemExit) as caught:
            config.load_config(self.proj)
        self.assertIn("fix it", str(caught.exception))

    def test_a_user_config_that_is_not_utf8_stops_the_session_readably(self):
        (self.home / "config.toml").write_bytes(b'\xff\xfemodel = "x"\n')
        with self.assertRaises(SystemExit) as caught:
            config.load_config(self.proj)
        self.assertIn(str(self.home / "config.toml"), str(caught.exception))

    def test_a_user_config_of_nested_arrays_stops_the_session_readably(self):
        (self.home / "config.toml").write_text("v = " + "[" * 8000 + "]" * 8000)
        with self.assertRaises(SystemExit) as caught:
            config.load_config(self.proj)
        self.assertIn(str(self.home / "config.toml"), str(caught.exception))

    def test_a_broken_endpoint_file_stops_the_session_too(self):
        """Endpoints hold one credential each and live in the user dir; same author, same rule."""
        (self.home / "endpoints").mkdir()
        (self.home / "endpoints" / "github.toml").write_text("[[[not toml")
        with self.assertRaises(SystemExit) as caught:
            config.load_config(self.proj)
        self.assertIn("github.toml", str(caught.exception))


class AdvertisedSettingsTests(unittest.TestCase):
    """Every key in ``DEFAULTS`` is a promise that setting it does something."""

    def test_every_default_key_is_read_somewhere_in_the_package(self):
        """A key nothing consumes is worse than an absent one: it advertises a choice that
        does not exist, and the user only finds out by watching it have no effect."""
        package = Path(config.__file__).resolve().parent.parent
        sources = [path for path in package.rglob("*.py")
                   if path.name != "config.py" and "__pycache__" not in path.parts]
        texts = {path: path.read_text() for path in sources}
        unread = [key for key in config.DEFAULTS
                  if not any(re.search(rf'''["']{re.escape(key)}["']''', text)
                             for text in texts.values())]
        self.assertEqual(unread, [], "DEFAULTS keys nothing in picoagent/ reads")


if __name__ == "__main__":
    unittest.main()
