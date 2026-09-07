"""Text picoagent did not write, on its way to a terminal.

A notice's text comes from a plugin, a tool result, a repository's config file or a remote MCP
server; an exception's message and its class name come from whoever raised it, and plugin code
raises freely. A terminal reads some of those bytes as commands rather than as characters: an
ANSI sequence can rewrite a line the user already read, blank the screen, hide what follows, or
retitle the window, which turns a notice into a way to lie about what the session did. Length is
the other half - a message with no bound is a way to push the transcript out of the scrollback.

These tests fix what survives the trip and what does not. Newlines and tabs survive, because a
multi-line notice is what ``/model list`` produces and is the reason the notice channel exists.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from helpers import CaptureFrontend, make_runtime, run
from picoagent.core.loop import AgentLoop
from picoagent.core.text import (MAX_MESSAGE_CHARS, describe_exception, safe_for_display,
                                 strip_terminal_controls)
from picoagent.core.types import ToolResult
from picoagent.frontends.plain import PlainFrontend
from picoagent.frontends.print import PrintFrontend

ESC = "\x1b"


class StripTerminalControlsTests(unittest.TestCase):
    """The sanitiser on its own: what a terminal may still be told."""

    def test_ordinary_text_is_untouched(self):
        self.assertEqual(strip_terminal_controls("plugin 'gate' is not trusted yet"),
                         "plugin 'gate' is not trusted yet")

    def test_newlines_and_tabs_survive(self):
        """A multi-line listing is exactly what the notice channel was built to carry."""
        self.assertEqual(strip_terminal_controls("models:\n\tgpt-4o\n\tllama3.2"),
                         "models:\n\tgpt-4o\n\tllama3.2")

    def test_non_ascii_survives(self):
        self.assertEqual(strip_terminal_controls("café · 日本語 · ✓"), "café · 日本語 · ✓")

    def test_a_colour_sequence_goes_and_its_text_stays(self):
        self.assertEqual(strip_terminal_controls(f"{ESC}[31mdanger{ESC}[0m"), "danger")

    def test_the_sequence_that_blanks_the_screen_goes(self):
        self.assertEqual(strip_terminal_controls(f"all clear{ESC}[2J{ESC}[H"), "all clear")

    def test_a_full_terminal_reset_goes(self):
        """``ESC c`` is one character past the escape and re-initialises the whole terminal."""
        self.assertEqual(strip_terminal_controls(f"done{ESC}c"), "done")

    def test_a_carriage_return_goes(self):
        """Alone it puts the cursor back on column 0, so the next text overwrites what was read."""
        self.assertEqual(strip_terminal_controls("read this\rnow read that"), "read thisnow read that")

    def test_a_window_title_sequence_goes_with_its_body(self):
        self.assertEqual(strip_terminal_controls(f"ok{ESC}]0;pwned\x07 done"), "ok done")

    def test_an_unterminated_title_sequence_costs_its_line_and_no_more(self):
        """A string sequence with no terminator would swallow a terminal; here it stops at the line."""
        self.assertEqual(strip_terminal_controls(f"ok{ESC}]0;pwned\nstill here"), "ok\nstill here")

    def test_the_single_byte_c1_forms_go_too(self):
        """``\\x9b`` is CSI and ``\\x9d`` is OSC on a terminal in 8-bit mode."""
        self.assertEqual(strip_terminal_controls("a\x9b31mb"), "ab")
        self.assertEqual(strip_terminal_controls("a\x9d0;x\x07b"), "ab")

    def test_the_remaining_c0_controls_go(self):
        self.assertEqual(strip_terminal_controls("a\x00b\x07c\x08d\x0be\x0cf\x7fg"), "abcdefg")

    def test_nothing_a_terminal_reads_as_a_command_is_left(self):
        hostile = f"{ESC}[2J{ESC}]0;t\x07{ESC}c\x9b1m\x00\x08\r"
        cleaned = strip_terminal_controls(hostile)
        self.assertEqual(cleaned, "")


class SafeForDisplayTests(unittest.TestCase):
    """The bounded form: the same strip, plus a ceiling on how much of it there is."""

    def test_a_short_message_is_returned_whole(self):
        self.assertEqual(safe_for_display("could not reach the server"), "could not reach the server")

    def test_a_long_message_is_cut_and_says_so(self):
        cut = safe_for_display("x" * 50_000)
        self.assertLess(len(cut), MAX_MESSAGE_CHARS + 100)
        self.assertIn("truncated", cut)

    def test_the_strip_happens_before_the_cut(self):
        """Otherwise a sequence straddling the boundary survives in halves."""
        self.assertNotIn(ESC, safe_for_display(f"{ESC}[31m" + "x" * 50_000))


class DescribeExceptionTests(unittest.TestCase):
    """An exception is two untrusted strings: its message and its class name."""

    def test_an_ordinary_exception_reads_as_before(self):
        self.assertEqual(describe_exception(ValueError("bad path")), "ValueError: bad path")

    def test_a_hostile_message_is_stripped_and_bounded(self):
        class Hostile(Exception):
            def __str__(self):
                return f"{ESC}[2Jpicoagent: all plugins verified" + "x" * 5_000_000

        described = describe_exception(Hostile())
        self.assertNotIn(ESC, described)
        self.assertLess(len(described), MAX_MESSAGE_CHARS + 100)

    def test_a_hostile_class_name_is_stripped_too(self):
        """``type()`` takes any string as a name, so the half before the colon is untrusted as well."""
        hostile = type(f"{ESC}[2JVerified", (Exception,), {})
        self.assertNotIn(ESC, describe_exception(hostile("ok")))


class TerminalRenderingTests(unittest.TestCase):
    """The same text, through the two frontends that write to a terminal."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.hostile = f"{ESC}[2Jyour session did nothing{ESC}]0;pwned\x07"

    def test_the_repl_strips_a_notice(self):
        out = io.StringIO()
        with redirect_stdout(out):
            run(PlainFrontend(color=False).emit("notice", {"text": self.hostile}))
        self.assertNotIn(ESC, out.getvalue())
        self.assertIn("your session did nothing", out.getvalue())

    def test_the_repl_strips_an_error(self):
        out = io.StringIO()
        with redirect_stdout(out):
            run(PlainFrontend(color=False).emit("error", {"text": self.hostile}))
        self.assertNotIn(ESC, out.getvalue())

    def test_the_repl_keeps_its_own_colour(self):
        """Sanitising the payload must not disarm the frontend's own styling of it."""
        out = io.StringIO()
        frontend = PlainFrontend(color=False)
        frontend.color = True
        with redirect_stdout(out):
            run(frontend.emit("notice", {"text": "cache rebuilt"}))
        self.assertIn("\033[33m", out.getvalue())

    def test_a_headless_run_strips_a_notice_on_stderr(self):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            run(PrintFrontend(json_mode=False).emit("notice", {"text": self.hostile}))
        self.assertNotIn(ESC, err.getvalue())
        self.assertIn("your session did nothing", err.getvalue())

    def test_a_headless_run_strips_an_error(self):
        err = io.StringIO()
        with redirect_stderr(err):
            run(PrintFrontend(json_mode=False).emit("error", {"text": self.hostile}))
        self.assertNotIn(ESC, err.getvalue())

    def test_a_multi_line_command_answer_keeps_its_lines(self):
        """The `/model list` case: the split exists to deliver this, so it must arrive whole."""
        async def handler(args, rt):
            return "models:\n  gpt-4o\n  llama3.2"

        rt = make_runtime(self.tmp, frontend=PrintFrontend(json_mode=False))
        rt.commands.register("models", handler, "lists models")
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            run(AgentLoop(rt).handle_input("/models"))
        self.assertEqual(out.getvalue(), "models:\n  gpt-4o\n  llama3.2\n")

    def test_json_mode_keeps_the_text_verbatim_and_writes_no_escape(self):
        """``--json`` is data, not a terminal: `json.dumps` escapes what a terminal would obey.

        Stripping there would edit a record a program parses, so the field keeps the original
        text and the bytes on the stream carry it as ``\\u001b`` rather than as an escape.
        """
        out = io.StringIO()
        with redirect_stdout(out):
            run(PrintFrontend(json_mode=True).emit("notice", {"text": self.hostile}))
        self.assertNotIn(ESC, out.getvalue())
        self.assertEqual(json.loads(out.getvalue())["text"], self.hostile)


class HostileExceptionTests(unittest.TestCase):
    """A plugin's exception reaching the user, through the two call-ins that catch one."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    @staticmethod
    def _hostile() -> Exception:
        class Hostile(Exception):
            def __str__(self):
                return f"{ESC}[2Jpicoagent: no plugins were loaded" + "!" * 5_000_000
        return Hostile()

    def test_a_command_handler_that_raises_is_reported_stripped_and_bounded(self):
        hostile = self._hostile()

        async def handler(args, rt):
            raise hostile

        rt = make_runtime(self.tmp, frontend=CaptureFrontend())
        rt.commands.register("boom", handler, "raises")
        run(AgentLoop(rt).handle_input("/boom"))
        reported = [payload["text"] for event, payload in rt.frontend.events if event == "error"]
        self.assertEqual(len(reported), 1)
        self.assertNotIn(ESC, reported[0])
        self.assertLess(len(reported[0]), MAX_MESSAGE_CHARS + 200)

    def test_a_tool_that_raises_is_reported_stripped_and_bounded(self):
        hostile = self._hostile()

        class Exploding:
            name, description, parameters = "explode", "raises", {"type": "object", "properties": {}}

            async def execute(self, args, ctx) -> ToolResult:
                raise hostile

        rt = make_runtime(self.tmp, frontend=CaptureFrontend())
        rt.tools.register(Exploding(), owner="forger")
        from picoagent.core.types import ToolCall
        result = run(AgentLoop(rt)._invoke(ToolCall("call-1", "explode", {})))
        self.assertTrue(result.is_error)
        self.assertNotIn(ESC, result.content)
        self.assertLess(len(result.content), MAX_MESSAGE_CHARS + 200)


if __name__ == "__main__":
    unittest.main()
