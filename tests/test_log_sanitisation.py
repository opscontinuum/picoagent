"""What a plugin's exception can write to the terminal on its way through the log.

``picoagent.core.text`` already strips terminal control sequences out of every notice, error and
exception picoagent renders itself. Logging was the hole beside it: ``log.exception`` renders the
traceback, and the last line of a traceback is the exception's class name and ``str(exc)`` -
neither of which the call site touches, however carefully it wraps its own arguments. Three call
sites do it on purpose, because a plugin failing must not end the session: an event handler that
raised (``events.py``), a slash command that raised and a tool that raised (``loop.py``). All
three are plugin code, so all three render text an attacker chose, straight to stderr.

The fix is a formatter rather than three edits, because the traceback is rendered by the logging
machinery and not by the caller. These tests assert both halves: the formatter cleans what it is
given, and ``main`` actually installs it - an uninstalled formatter is the same as no formatter.
"""
from __future__ import annotations

import io
import logging
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from helpers import ROOT, temp_dir  # noqa: F401  (puts picoagent on sys.path)
from picoagent import cli

#: Hides the rest of the line, then retitles the window: what an exception message can do today.
ESCAPES = "\x1b[2K\x1b]0;pwned\x07"


class SafeLogFormatterTests(unittest.TestCase):
    """The formatter itself, given the two things a plugin gets to write."""

    def _formatted(self, record: logging.LogRecord) -> str:
        return cli.SafeLogFormatter("%(name)s: %(message)s").format(record)

    def _record(self, message: str, exc_info=None) -> logging.LogRecord:
        return logging.LogRecord("picoagent.test", logging.ERROR, __file__, 1, message, (), exc_info)

    def test_the_message_is_stripped(self):
        formatted = self._formatted(self._record(f"plugin said {ESCAPES}hello"))
        self.assertNotIn("\x1b", formatted)
        self.assertIn("hello", formatted)

    def test_an_exception_message_in_the_traceback_is_stripped(self):
        try:
            raise ValueError(f"{ESCAPES}the plugin's own words")
        except ValueError as exc:
            record = self._record("handler failed", (type(exc), exc, exc.__traceback__))
        formatted = self._formatted(record)
        self.assertNotIn("\x1b", formatted)
        self.assertIn("the plugin's own words", formatted)

    def test_an_exception_class_name_in_the_traceback_is_stripped(self):
        """``type()`` takes any string as a name, so the name is the plugin's words too."""
        hostile = type(f"{ESCAPES}Boom", (Exception,), {})
        try:
            raise hostile("failed")
        except Exception as exc:  # noqa: BLE001 - the class under test is the point
            record = self._record("tool failed", (type(exc), exc, exc.__traceback__))
        self.assertNotIn("\x1b", self._formatted(record))

    def test_a_message_that_cannot_be_rendered_is_reported_rather_than_raised(self):
        class Unprintable:
            def __str__(self):
                raise RuntimeError("no")

        record = logging.LogRecord("picoagent.test", logging.ERROR, __file__, 1,
                                   "plugin said %s", (Unprintable(),), None)
        self.assertIn("could not be rendered", self._formatted(record))

    def test_an_ordinary_line_is_unchanged(self):
        self.assertEqual(self._formatted(self._record("loaded 3 plugins")),
                         "picoagent.test: loaded 3 plugins")


class FormatterIsInstalledTests(unittest.TestCase):
    """``main`` configures logging once, and that is the only place this can be wired."""

    def setUp(self):
        self.tmp = temp_dir()
        self._old_home = os.environ.get("PICOAGENT_HOME")
        self._old_cwd = Path.cwd()
        os.environ["PICOAGENT_HOME"] = str(self.tmp / "home")
        os.chdir(self.tmp)
        root = logging.getLogger()
        self._saved = root.handlers[:]
        root.handlers.clear()

    def tearDown(self):
        root = logging.getLogger()
        root.handlers[:] = self._saved
        os.chdir(self._old_cwd)
        if self._old_home is None:
            os.environ.pop("PICOAGENT_HOME", None)
        else:
            os.environ["PICOAGENT_HOME"] = self._old_home

    def test_main_installs_it_on_the_root_handler(self):
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["plugin", "list"])   # any subcommand; what is under test is the wiring
        formatters = [handler.formatter for handler in logging.getLogger().handlers]
        self.assertTrue(formatters, "main configured no logging handler")
        self.assertTrue(all(isinstance(formatter, cli.SafeLogFormatter) for formatter in formatters),
                        f"logging still renders plugin text verbatim: {formatters}")


if __name__ == "__main__":
    unittest.main()
