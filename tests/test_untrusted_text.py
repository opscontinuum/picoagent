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
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from helpers import CaptureFrontend, make_runtime, run, temp_dir
from picoagent.core.commands import COMMAND_SOURCE
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

    def test_a_sequence_with_an_intermediate_byte_takes_its_final_byte(self):
        """``ESC [ 3 1 SP h`` is one complete sequence, not a truncated one and then ``hello``.

        It reads as a letter eaten off the front of the text, and it is not. A CSI sequence is
        parameters (0x30-0x3F), then intermediates (0x20-0x2F), then one final byte (0x40-0x7E);
        ``h`` is a final byte, so a terminal's own parser consumes it too and the ``h`` never
        reached the screen either way. Stripping loses exactly what obeying would have lost, and
        only in text that already contained an ESC. Refusing the intermediates - the one change
        that would keep the ``h`` - would leave the tail of every real one, like the `` q`` of a
        cursor-style sequence, sitting in the notice as text.
        """
        self.assertEqual(strip_terminal_controls(f"{ESC}[31 hello"), "ello")
        self.assertEqual(strip_terminal_controls(f"{ESC}[0 qcursor"), "cursor")

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
        self.tmp = temp_dir()
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
        self.tmp = temp_dir()

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


# --------------------------------------------------------------------------- real streams

def encoding_stream(encoding: str = "utf-8"):
    """A stream that really encodes, the way a terminal does and ``io.StringIO`` does not.

    The rendering tests above capture into a ``StringIO``, which accepts any ``str`` and never
    turns it into bytes. A character that no codec can encode therefore passes every one of them
    and crashes the first real write. The reachable one is a lone surrogate: ``json.loads`` builds
    one out of a ``"\\ud800"`` in an MCP server's reply, and nothing between there and the
    frontend ever encodes. So these tests capture into a ``TextIOWrapper`` over a ``BytesIO``,
    where the encode happens inside ``write`` exactly as it does in front of a person.
    """
    return io.TextIOWrapper(io.BytesIO(), encoding=encoding, newline="")


def written(stream) -> str:
    """What actually reached the bytes, decoded back."""
    stream.flush()
    return stream.buffer.getvalue().decode(stream.encoding)


#: A ``str`` that is not text: half of a UTF-16 pair, on its own. No codec can encode it, UTF-8
#: included, so it is the one input that turns a write into a traceback.
SURROGATE = "\udc80"


class UnencodableTextTests(unittest.TestCase):
    """Text that cannot become bytes, on its way to a stream that must make it bytes."""

    def setUp(self):
        self.tmp = temp_dir()
        self.hostile = f"plugin says {SURROGATE} everything is fine"

    # ---------------------------------------------------------------- the REPL
    def test_the_repl_writes_a_notice_it_cannot_encode(self):
        out = encoding_stream()
        with redirect_stdout(out):
            run(PlainFrontend(color=False).emit("notice", {"text": self.hostile}))
        self.assertIn("everything is fine", written(out))

    def test_the_repl_writes_an_error_it_cannot_encode(self):
        out = encoding_stream()
        with redirect_stdout(out):
            run(PlainFrontend(color=False).emit("error", {"text": self.hostile}))
        self.assertIn("everything is fine", written(out))

    def test_the_repl_writes_a_tool_result_it_cannot_encode(self):
        """A tool result is stripped nowhere, so only the write itself can save it."""
        out = encoding_stream()
        with redirect_stdout(out):
            run(PlainFrontend(color=False).emit(
                "tool_result", {"call": None, "result": ToolResult("c1", self.hostile)}))
        self.assertIn("everything is fine", written(out))

    def test_the_repl_writes_a_model_delta_it_cannot_encode(self):
        out = encoding_stream()
        with redirect_stdout(out):
            run(PlainFrontend(color=False).emit("assistant_delta", {"text": self.hostile}))
        self.assertIn("everything is fine", written(out))

    # ---------------------------------------------------------------- headless
    def test_a_headless_run_writes_a_notice_it_cannot_encode(self):
        err = encoding_stream()
        with redirect_stdout(encoding_stream()), redirect_stderr(err):
            run(PrintFrontend(json_mode=False).emit("notice", {"text": self.hostile}))
        self.assertIn("everything is fine", written(err))

    def test_a_headless_run_writes_an_error_it_cannot_encode(self):
        err = encoding_stream()
        with redirect_stderr(err):
            run(PrintFrontend(json_mode=False).emit("error", {"text": self.hostile}))
        self.assertIn("everything is fine", written(err))

    def test_a_headless_answer_it_cannot_encode_is_still_written(self):
        """``assistant_delta`` is unstripped by design. It must still not kill the write."""
        out = encoding_stream()
        with redirect_stdout(out):
            run(PrintFrontend(json_mode=False).emit("assistant_delta", {"text": self.hostile}))
        self.assertIn("everything is fine", written(out))

    def test_json_mode_writes_a_record_it_cannot_encode(self):
        out = encoding_stream()
        with redirect_stdout(out):
            run(PrintFrontend(json_mode=True).emit("notice", {"text": self.hostile}))
        self.assertEqual(json.loads(written(out))["text"], self.hostile)

    def test_a_command_answer_it_cannot_encode_reaches_stdout(self):
        """The whole ``-p`` path: a slash command's output is the answer, and it must arrive."""
        async def handler(args, rt):
            return f"models:\n  gpt-4o {SURROGATE}"

        rt = make_runtime(self.tmp, frontend=PrintFrontend(json_mode=False))
        rt.commands.register("models", handler, "lists models")
        out = encoding_stream()
        with redirect_stdout(out), redirect_stderr(encoding_stream()):
            run(AgentLoop(rt).handle_input("/models"))
        self.assertIn("gpt-4o", written(out))

    def test_a_question_about_text_it_cannot_encode_can_be_asked(self):
        """``input()`` writes its prompt to the stream too, and a plugin chooses the prompt."""
        out = encoding_stream()

        def fake_input(prompt=""):
            out.write(prompt)
            return ""

        with mock.patch("builtins.input", fake_input):
            run(PlainFrontend(color=False).ask("input", f"overwrite {SURROGATE}?"))
        self.assertIn("overwrite", written(out))

    # ---------------------------------------------------------------- what the escape costs
    def test_the_character_is_named_rather_than_dropped(self):
        out = encoding_stream()
        with redirect_stdout(out):
            run(PlainFrontend(color=False).emit("notice", {"text": self.hostile}))
        self.assertIn("\\udc80", written(out))

    def test_printable_text_keeps_its_lines_and_its_accents(self):
        """The reason this is not ``repr``: a listing must still arrive as a listing."""
        out = encoding_stream()
        with redirect_stdout(out), redirect_stderr(encoding_stream()):
            run(PrintFrontend(json_mode=False).emit(
                "notice", {"text": "models:\n  café · 日本語\n  llama3.2", "source": COMMAND_SOURCE}))
        self.assertEqual(written(out), "models:\n  café · 日本語\n  llama3.2\n")

    def test_a_terminal_that_cannot_encode_the_text_still_gets_the_line(self):
        """An ASCII-only console is the other half of the same crash, and it is not hostile."""
        out = encoding_stream("ascii")
        with redirect_stdout(out), redirect_stderr(encoding_stream("ascii")):
            run(PrintFrontend(json_mode=False).emit(
                "notice", {"text": "models: 日本語", "source": COMMAND_SOURCE}))
        self.assertIn("models: ", written(out))


class HostileClassNameTests(unittest.TestCase):
    """The name half of ``describe_exception``: reading it can run a plugin's code."""

    def setUp(self):
        self.tmp = temp_dir()

    @staticmethod
    def _name_raises(error: BaseException | None = None):
        """An exception class whose ``__name__`` raises when anything reads it."""
        failure = error or RuntimeError("name access denied by plugin")

        class Denied(type):
            @property
            def __name__(cls):
                raise failure

        return Denied("Hostile", (Exception,), {})

    def test_a_class_name_that_raises_is_reported_rather_than_re_raised(self):
        described = describe_exception(self._name_raises()("the real message"))
        self.assertIn("the real message", described)

    def test_a_class_name_that_raises_a_keyboard_interrupt_is_contained_too(self):
        """The raiser picks the exception, so ``Exception`` alone is not a guard against them."""
        described = describe_exception(self._name_raises(KeyboardInterrupt())("still reported"))
        self.assertIn("still reported", described)

    def test_a_class_name_that_is_not_text_cannot_break_the_report(self):
        """Whatever comes back from the property is the raiser's object too, not a string."""
        class Odd(type):
            @property
            def __name__(cls):
                class Unformattable(str):
                    def __format__(self, spec):
                        raise RuntimeError("format denied by plugin")
                return Unformattable("Hostile")

        described = describe_exception(Odd("Hostile", (Exception,), {})("the real message"))
        self.assertIn("the real message", described)

    def test_a_class_name_that_raises_does_not_escape_a_tool_call(self):
        """``_invoke`` promises to convert any exception into an error result."""
        from picoagent.core.types import ToolCall
        hostile = self._name_raises()("tool failed")

        class Exploding:
            name, description, parameters = "explode", "raises", {"type": "object", "properties": {}}

            async def execute(self, args, ctx) -> ToolResult:
                raise hostile

        rt = make_runtime(self.tmp, frontend=CaptureFrontend())
        rt.tools.register(Exploding(), owner="forger")
        result = run(AgentLoop(rt)._invoke(ToolCall("call-1", "explode", {})))
        self.assertTrue(result.is_error)
        self.assertIn("tool failed", result.content)

    def test_a_class_name_that_raises_does_not_escape_a_command(self):
        hostile = self._name_raises()("command failed")

        async def handler(args, rt):
            raise hostile

        rt = make_runtime(self.tmp, frontend=CaptureFrontend())
        rt.commands.register("boom", handler, "raises")
        run(AgentLoop(rt).handle_input("/boom"))
        reported = [payload["text"] for event, payload in rt.frontend.events if event == "error"]
        self.assertEqual(len(reported), 1)
        self.assertIn("command failed", reported[0])


if __name__ == "__main__":
    unittest.main()
