"""What the log says about how the session ended.

The file used to stop at the last thing that happened, which meant a session that exited
cleanly, a session killed by a signal, and a session still running all left the same tail.
Nobody reviewing an incident could tell them apart. DISA ASD V6R4 V-222469 asks for the
shutdown to be recorded and for the time it happened.

The record is one appended entry, the same shape as every other: a ``kind`` with an ``id`` and
a ``parent``, so the append-only tree in ``docs/architecture.md`` still holds and the entry sits
on the branch rather than beside it.

What these tests pin is both halves of the property, because only the pair is honest. A clean
exit writes the entry. An end that never reaches the exit path writes nothing, and the *absence*
is the signal - a reader who expects the record to be guaranteed will read every killed session
as a clean one.
"""
from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from helpers import CaptureFrontend, run, temp_dir
from picoagent import cli
from picoagent.core.session import Session
from picoagent.core.types import Message
from picoagent.testing.fakes import FakeServer

ANSWERED = {"text": "all done", "tool_calls": []}


class _Run(unittest.TestCase):
    """A project and a user dir, and a way to drive ``run_agent`` over a fake model."""

    def setUp(self):
        self.tmp = temp_dir()
        self.project = self.tmp / "project"
        self.project.mkdir()
        self.home = self.tmp / "home"
        self.home.mkdir()
        os.environ["PICOAGENT_HOME"] = str(self.home)

    def _configure(self, base_url: str) -> None:
        (self.home / "config.toml").write_text(
            f'[providers.openai]\nbase_url = "{base_url}"\napi_key = "k"\n')

    def _run(self, *flags: str) -> int:
        args = cli.build_parser().parse_args(["-C", str(self.project), *flags])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return run(cli.run_agent(args))

    def entries(self) -> list[dict]:
        directory = cli.session_dir(cli.load_config(self.project), self.project)
        return Session(Session.list(directory)[0], self.project, resume=True).entries


class ACleanExitIsRecorded(_Run):
    """The three exits that reach the end of ``run_agent``."""

    def test_a_one_shot_run_ends_the_log_with_a_shutdown_entry(self):
        with FakeServer("openai", script=ANSWERED) as server:
            self._configure(server.url + "/v1")
            self._run("-p", "hello")
        self.assertEqual(self.entries()[-1]["kind"], "shutdown")

    def test_the_record_says_when_and_that_it_was_a_clean_exit(self):
        with FakeServer("openai", script=ANSWERED) as server:
            self._configure(server.url + "/v1")
            self._run("-p", "hello")
        last = self.entries()[-1]
        self.assertEqual(last["reason"], "completed")
        self.assertGreater(last["ended"], 0)

    def test_a_repl_that_the_user_left_is_a_shutdown_too(self):
        self._configure("http://127.0.0.1:9/v1")
        with mock.patch.object(cli, "PlainFrontend", CaptureFrontend):
            self._run()
        self.assertEqual(self.entries()[-1]["kind"], "shutdown")

    def test_a_run_whose_model_call_failed_still_records_its_shutdown(self):
        """The exit code says the model was never reached; the log still says the process left."""
        self._configure("http://127.0.0.1:9/v1")
        self.assertEqual(self._run("-p", "hello"), cli.EXIT_MODEL_ERROR)
        self.assertEqual(self.entries()[-1]["kind"], "shutdown")


class AnInterruptedRunSaysSo(_Run):
    """Ctrl-C unwinds through the exit path, so it can be recorded - and named apart."""

    def test_an_interrupted_run_is_recorded_as_interrupted(self):
        self._configure("http://127.0.0.1:9/v1")

        class Interrupts(CaptureFrontend):
            async def run(self, agent):
                raise KeyboardInterrupt

        with mock.patch.object(cli, "PlainFrontend", Interrupts):
            with self.assertRaises(KeyboardInterrupt):
                self._run()
        self.assertEqual(self.entries()[-1]["reason"], "interrupted")

    def test_the_interruption_still_reaches_the_caller(self):
        """Recording it must not swallow it; the process still ends the way it was ending."""
        self._configure("http://127.0.0.1:9/v1")

        class Raises(CaptureFrontend):
            async def run(self, agent):
                raise RuntimeError("frontend fell over")

        with mock.patch.object(cli, "PlainFrontend", Raises):
            with self.assertRaises(RuntimeError):
                self._run()


class AnAbruptEndLeavesNoRecord(unittest.TestCase):
    """The half that the docstring has to be honest about.

    A ``SIGKILL`` writes nothing by definition, so the log of a killed session is exactly the
    log of a session mid-flight: entries up to the last append and no shutdown entry. That is
    what tells a reader the session ended some other way, and it is the only thing that can.
    """

    def setUp(self):
        self.tmp = temp_dir()
        self.path = self.tmp / "s.jsonl"

    def test_a_session_that_never_reached_its_exit_path_has_no_shutdown_entry(self):
        session = Session(self.path, self.tmp)
        session.append_message(Message(role="user", text="hello"))
        reader = Session(self.path, self.tmp, resume=True)
        self.assertNotIn("shutdown", [entry["kind"] for entry in reader.entries])

    def test_the_shutdown_entry_is_the_difference_between_the_two(self):
        session = Session(self.path, self.tmp)
        session.append_message(Message(role="user", text="hello"))
        session.append_shutdown("completed")
        reader = Session(self.path, self.tmp, resume=True)
        self.assertEqual(reader.entries[-1]["kind"], "shutdown")


class TheRecordFitsTheAppendOnlyLog(unittest.TestCase):
    """It is an entry in the tree, not a footer bolted onto the file."""

    def setUp(self):
        self.tmp = temp_dir()
        self.path = self.tmp / "s.jsonl"
        self.session = Session(self.path, self.tmp)
        self.message = self.session.append_message(Message(role="user", text="hello"))

    def test_it_hangs_off_the_last_entry_on_the_branch(self):
        shutdown = self.session.append_shutdown("completed")
        self.assertEqual(shutdown["parent"], self.message["id"])
        self.assertIn(shutdown, self.session.branch())

    def test_it_is_never_sent_to_the_model(self):
        self.session.append_shutdown("completed")
        self.assertEqual([message.text for message in self.session.messages()], ["hello"])

    def test_a_resumed_session_appends_after_it_rather_than_over_it(self):
        """Resuming works the same either way; what differs is what the record then shows -
        a shutdown entry with conversation after it is a session that was reopened."""
        self.session.append_shutdown("completed")
        resumed = Session(self.path, self.tmp, resume=True)
        resumed.append_message(Message(role="user", text="again"))
        kinds = [entry["kind"] for entry in resumed.branch()]
        self.assertEqual(kinds, ["message", "shutdown", "message"])   # the header is not on the branch
        self.assertEqual([message.text for message in resumed.messages()], ["hello", "again"])


if __name__ == "__main__":
    unittest.main()
