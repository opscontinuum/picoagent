"""What a program driving ``picoagent -p`` can tell from the outside.

The headless modes exist to be called by something that is not a person: a script, a CI job,
another agent's shell tool. Such a caller reads two things, the exit code and the stream, and
it resumes work by directory. So the two facts these tests pin are that a run which failed
says so in its status, and that the session ``-r`` resumes belongs to the project it was
started in.
"""
from __future__ import annotations

import io
import json
import os
import socket
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from helpers import run, temp_dir
from picoagent import cli
from picoagent.core.config import load_config
from picoagent.testing.fakes import FakeServer

ANSWERED = {"text": "all done", "tool_calls": []}
SILENT = {"text": "", "tool_calls": []}


def closed_port() -> int:
    """A port nothing is listening on, so connecting to it is refused rather than slow."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class HeadlessExitCodeTests(unittest.TestCase):
    """``-p`` and ``--json`` returned 0 even when the model call died.

    A provider failure is reported as an ``error`` event and nothing else: the loop stops, the
    frontend writes the sentence to stderr, and the process exits successfully. A caller then
    cannot tell "the model had nothing to add" from "the run never happened", which is exactly
    the distinction a scripted caller needs and the one a person reading stderr gets for free.
    """

    def setUp(self):
        self.tmp = temp_dir()
        self.project = self.tmp / "project"
        self.project.mkdir()
        os.environ["PICOAGENT_HOME"] = str(self.tmp / "home")
        (self.tmp / "home").mkdir()

    def _configure(self, base_url: str) -> None:
        (self.tmp / "home" / "config.toml").write_text(
            f'[providers.openai]\nbase_url = "{base_url}"\napi_key = "k"\n')

    def _run(self, *flags: str) -> tuple[int, str, str]:
        args = cli.build_parser().parse_args(["-p", "hello", "-C", str(self.project), *flags])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = run(cli.run_agent(args))
        return code, out.getvalue(), err.getvalue()

    def test_a_run_whose_only_model_call_failed_does_not_report_success(self):
        self._configure(f"http://127.0.0.1:{closed_port()}/v1")
        code, _, err = self._run()
        self.assertEqual(code, cli.EXIT_MODEL_ERROR)
        self.assertTrue(err.strip(), "the person at the terminal still gets the sentence")

    def test_the_json_stream_carries_the_same_verdict(self):
        self._configure(f"http://127.0.0.1:{closed_port()}/v1")
        code, out, _ = self._run("--json")
        self.assertEqual(code, cli.EXIT_MODEL_ERROR)
        events = [json.loads(line)["event"] for line in out.splitlines() if line.strip()]
        self.assertIn("error", events)

    def test_an_answered_prompt_still_exits_zero(self):
        with FakeServer("openai", script=ANSWERED) as server:
            self._configure(server.url + "/v1")
            code, out, _ = self._run()
        self.assertEqual(code, 0)
        self.assertIn("all done", out)

    def test_a_model_that_chose_to_say_nothing_is_not_a_failure(self):
        """Empty is an answer. Only a turn that errored is a failed run."""
        with FakeServer("openai", script=SILENT) as server:
            self._configure(server.url + "/v1")
            code, _, _ = self._run()
        self.assertEqual(code, 0)

    def test_the_code_is_its_own_and_not_borrowed_from_another_refusal(self):
        """A wrapper should not have to read English to tell a dead provider from a refused
        plugin or a bad ``-r`` path."""
        codes = {cli.EXIT_REQUIRED_PLUGIN, cli.EXIT_PLUGIN_PROVENANCE, cli.EXIT_MODEL_ERROR, 0, 1, 2}
        self.assertEqual(len(codes), 6)


class SessionDirectoryTests(unittest.TestCase):
    """Two projects must not share a session directory.

    The name was the project path with every ``/`` turned into ``--``, which is not reversible:
    ``/a/b--c`` and ``/a/b/c`` produce the same string. ``-r last`` in one of them then resumes
    the other's session, and that history is replayed into the model - a private repository's
    transcript arriving in a run started somewhere else.
    """

    def setUp(self):
        self.tmp = temp_dir()
        os.environ["PICOAGENT_HOME"] = str(self.tmp / "home")
        self.cfg = load_config(self.tmp)

    def _dir(self, path: str) -> Path:
        return cli.session_dir(self.cfg, Path(path))

    def test_two_projects_that_used_to_collide_get_their_own_directories(self):
        self.assertNotEqual(self._dir("/a/b--c"), self._dir("/a/b/c"))

    def test_resume_last_stays_inside_the_project_it_was_asked_about(self):
        first, second = Path("/a/b--c"), Path("/a/b/c")
        cli.open_session(self.cfg, first, None).append_custom("marker", "first project")
        resumed = cli.open_session(self.cfg, second, "last")
        self.assertEqual(list(resumed.custom("marker")), [])

    def test_the_name_still_says_which_project_it_is(self):
        """Readable on purpose: people go looking in this directory with a file manager."""
        self.assertIn("home--dev--picoagent", self._dir("/home/dev/picoagent").name)

    def test_sessions_written_under_the_old_name_are_still_found(self):
        """Renaming the scheme must not orphan the history somebody already has."""
        legacy = Path(self.cfg["_user_dir"]) / "sessions" / "home--dev--picoagent"
        legacy.mkdir(parents=True)
        cwd = Path("/home/dev/picoagent")
        session = cli.open_session(self.cfg, cwd, None)
        self.assertEqual(session.path.parent, legacy)
        session.append_custom("marker", "written before the rename")
        resumed = cli.open_session(self.cfg, cwd, "last")
        self.assertEqual([entry["data"] for entry in resumed.custom("marker")],
                         ["written before the rename"])


if __name__ == "__main__":
    unittest.main()
