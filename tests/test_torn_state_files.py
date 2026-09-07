"""State files caught mid-write: the trust store and the session log.

Both are written by appending or overwriting a file that something else reads on the next
start, so both have the same failure: the machine goes down, or the disk fills, between the
first byte and the last. What is left is a file that exists, is not JSON, and is read by code
that assumed it would be.

The two ends of that are not symmetrical, and the tests say which is which.

* ``trust.json`` is the file every session, ``plugin list`` and ``plugin untrust`` open. A
  torn one used to raise ``JSONDecodeError`` out of ``TrustStore.__init__``, which takes out
  the recovery command as well as the session - and ``docs/security/trust-boundaries.md``
  stakes the ``required`` stop on ``plugin untrust`` being one command away. Recovering has
  to mean approvals are *gone*, never "still approved": a store nobody can read must not
  vouch for anything.
* The session log is appended to a line at a time, so a torn *last* line is the ordinary
  shape of a crash and is dropped. A line that will not parse anywhere else is not that
  shape, and continuing would replay a history with a hole in it to the model.
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from helpers import ROOT, temp_dir  # noqa: F401  - puts the package on sys.path
from picoagent import cli
from picoagent.core.session import Session
from picoagent.core.types import Message
from picoagent.plugins import loader

PLUGIN_TOML = """\
name = "torn-probe"
version = "0.1.0"
entry = "torn_probe:register"
description = "a plugin whose approval outlives a bad write"
"""


class _TornTrustStore(unittest.TestCase):
    """A user home with one approved plugin, and a way to damage the store behind it."""

    def setUp(self):
        self.tmp = temp_dir()
        self.home = self.tmp / "home"
        self.project = self.tmp / "project"
        self.project.mkdir(parents=True)
        self._old_home = os.environ.get("PICOAGENT_HOME")
        self._old_cwd = Path.cwd()
        os.environ["PICOAGENT_HOME"] = str(self.home)
        os.chdir(self.project)
        self.root = self._install()

    def tearDown(self):
        os.chdir(self._old_cwd)
        if self._old_home is None:
            os.environ.pop("PICOAGENT_HOME", None)
        else:
            os.environ["PICOAGENT_HOME"] = self._old_home

    def _install(self, name: str = "torn-probe") -> Path:
        root = self.home / "plugins" / name
        root.mkdir(parents=True)
        (root / "plugin.toml").write_text(PLUGIN_TOML)
        (root / "torn_probe.py").write_text("def register(api):\n    pass\n")
        loader.TrustStore(self.home).trust(loader.Manifest.load(root))
        return root

    @property
    def store_path(self) -> Path:
        return self.home / "trust.json"

    def _tear(self) -> None:
        """Leave what a crash halfway through a write leaves: a prefix of the JSON."""
        self.store_path.write_text(self.store_path.read_text()[:40])

    def _run(self, pcmd: str, spec: str | None = None) -> tuple[int, str]:
        buffer = io.StringIO()
        args = argparse.Namespace(pcmd=pcmd, spec=spec, project=False, yes=False)
        with redirect_stdout(buffer):
            code = cli.plugin_command(args)
        return code, buffer.getvalue()


class ATornTrustStoreIsReadable(_TornTrustStore):
    """Opening the store is what every entry point does first, so it may not raise."""

    def test_opening_a_torn_store_does_not_raise(self):
        self._tear()
        loader.TrustStore(self.home)

    def test_a_store_nobody_can_read_vouches_for_nothing(self):
        """Fail closed. The other direction - carrying on as if everything were still
        approved - would let a truncating write grant what only the user may grant."""
        self._tear()
        self.assertEqual(loader.TrustStore(self.home).data, {})

    def test_the_plugin_it_covered_reads_as_never_approved(self):
        self._tear()
        store = loader.TrustStore(self.home)
        self.assertEqual(store.status(loader.Manifest.load(self.root)), "new")

    def test_the_user_is_told_the_store_was_unreadable(self):
        """Silently starting from an empty store looks exactly like a first run, and the
        answer to the two is not the same: one needs the approvals given again."""
        self._tear()
        logging.disable(logging.NOTSET)          # `helpers` silences logging for every other test
        try:
            with self.assertLogs("picoagent.plugins", level="ERROR") as caught:
                loader.TrustStore(self.home)
        finally:
            logging.disable(logging.CRITICAL)
        self.assertIn(str(self.store_path), "\n".join(caught.output))

    def test_the_recovery_commands_still_run(self):
        """`plugin untrust` is what the docs promise is always one command away."""
        self._tear()
        listed, _ = self._run("list")
        self.assertEqual(listed, 0)
        code, out = self._run("untrust", "torn-probe")
        self.assertEqual(code, 1)                  # no record left to withdraw, and no traceback
        self.assertIn("torn-probe", out)


class ATrustStoreIsPublishedInOneStep(_TornTrustStore):
    """The write half. A store that is overwritten in place is a store that can be torn."""

    def test_a_failure_at_the_last_moment_leaves_the_previous_store(self):
        before = self.store_path.read_text()
        with mock.patch("os.replace", side_effect=OSError("no space left")):
            with self.assertRaises(OSError):
                loader.TrustStore(self.home).trust(loader.Manifest.load(self._install("second")))
        self.assertEqual(self.store_path.read_text(), before)

    def test_a_withdrawal_is_published_the_same_way(self):
        before = self.store_path.read_text()
        store = loader.TrustStore(self.home)
        label = next(iter(store.data))
        with mock.patch("os.replace", side_effect=OSError("no space left")):
            with self.assertRaises(OSError):
                store.withdraw(label)
        self.assertEqual(self.store_path.read_text(), before)

    def test_a_completed_write_leaves_nothing_beside_the_store(self):
        """A temp file in the same directory is how the rename stays on one filesystem; it is
        not something to leave behind for the next reader to wonder about."""
        loader.TrustStore(self.home).trust(loader.Manifest.load(self._install("third")))
        self.assertEqual([p.name for p in sorted(self.home.iterdir()) if p.is_file()],
                         ["trust.json"])
        self.assertEqual(len(json.loads(self.store_path.read_text())), 2)


class ATornSessionLog(unittest.TestCase):
    """`-r` is wanted exactly when the last run crashed, which is when the log is torn."""

    def setUp(self):
        self.tmp = temp_dir()
        self.path = self.tmp / "session.jsonl"
        session = Session(self.path, self.tmp)
        session.append_message(Message(role="user", text="first"))
        session.append_message(Message(role="assistant", text="second"))

    def _tear_the_tail(self) -> None:
        """What an interrupted append leaves: a line that stops mid-way, with no newline."""
        with self.path.open("a") as handle:
            handle.write('{"kind": "message", "id": "x", "par')

    def test_a_partial_last_line_still_resumes(self):
        self._tear_the_tail()
        self.assertEqual(len(Session(self.path, self.tmp, resume=True).entries), 3)

    def test_the_history_before_the_crash_is_intact(self):
        self._tear_the_tail()
        texts = [m.text for m in Session(self.path, self.tmp, resume=True).messages()]
        self.assertEqual(texts, ["first", "second"])

    def test_the_dropped_tail_is_announced(self):
        self._tear_the_tail()
        logging.disable(logging.NOTSET)          # `helpers` silences logging for every other test
        try:
            with self.assertLogs("picoagent.session", level="WARNING") as caught:
                Session(self.path, self.tmp, resume=True)
        finally:
            logging.disable(logging.CRITICAL)
        self.assertIn(str(self.path), "\n".join(caught.output))

    def test_appending_after_a_resume_does_not_glue_onto_the_torn_line(self):
        """The half-written bytes cannot become valid, so they go rather than get a newline
        stitched in front of them: left in place they turn the next append into a line in the
        *middle* that will not parse, which is the fault below and not this one."""
        self._tear_the_tail()
        session = Session(self.path, self.tmp, resume=True)
        session.append_message(Message(role="user", text="third"))
        reopened = Session(self.path, self.tmp, resume=True)
        self.assertEqual([m.text for m in reopened.messages()], ["first", "second", "third"])

    def test_a_log_torn_on_its_very_first_write_opens_as_a_session(self):
        """Nothing recovered is still a session file, and the header is what says so."""
        self.path.write_text('{"kind": "hea')
        session = Session(self.path, self.tmp, resume=True)
        self.assertEqual([entry["kind"] for entry in session.entries], ["header"])

    def test_a_line_that_will_not_parse_in_the_middle_is_not_dropped(self):
        """An append cannot produce this, so something else did. Carrying on would hand the
        model a branch with a hole in it and never say so."""
        lines = self.path.read_text().splitlines()
        lines.insert(1, '{"kind": "message", "id": "x"')
        self.path.write_text("\n".join(lines) + "\n")
        with self.assertRaises(SystemExit) as caught:
            Session(self.path, self.tmp, resume=True)
        self.assertIn(str(self.path), str(caught.exception))
        self.assertIn("line 2", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
