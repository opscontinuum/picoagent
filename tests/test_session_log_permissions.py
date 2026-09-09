"""Who else on the machine can read the conversation.

The session log is the whole conversation: every prompt, every command the model ran, every
tool result, and whatever those results contained. It was created under the process umask -
0644 in the file and 0755 in the directories above it on a standard install - so on any host
with more than one account, every account could read every session. The codebase already knew
better everywhere else: the credentials file is opened at 0600, the trust store is republished
with ``mkstemp``'s owner-only mode, spilled tool output goes to a 0600 temp file. The log was
missed. DISA ASD V6R4 records it three times over: V-222500 (audit information must be
protected from unauthorised read access), V-222587 (confidentiality of stored information),
and V-222444, whose sensitive data lands in this file.

These tests read the mode back off the filesystem rather than asserting that some function was
called, because the finding is about the bits on disk. They run with ``umask(0)`` so a passing
result cannot be the umask's doing: with no mask at all, anything picoagent does not set
explicitly comes out 0666 and 0777.

POSIX only. Mode bits are not access control on Windows - NTFS uses ACLs - so these skip
there rather than assert something the platform does not mean; ``docs/security/threat-model.md``
records that as the open half of T23.
"""
from __future__ import annotations

import os
import stat
import unittest
from pathlib import Path

from helpers import temp_dir
from picoagent import cli
from picoagent.core.config import load_config
from picoagent.core.session import Session
from picoagent.core.tools import is_windows
from picoagent.core.types import Message


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@unittest.skipIf(is_windows(), "mode bits are not access control on Windows; see T23")
class _OwnerOnly(unittest.TestCase):
    """A temp home with no umask, so every mode observed was set on purpose."""

    def setUp(self):
        self.tmp = temp_dir()
        self.project = self.tmp / "project"
        self.project.mkdir()
        previous = os.umask(0)
        self.addCleanup(os.umask, previous)


class NewSessionFilesAreOwnerOnly(_OwnerOnly):
    """What a fresh session leaves on disk."""

    def test_the_session_file_is_readable_only_by_its_owner(self):
        path = self.tmp / "sessions" / "project" / "s.jsonl"
        Session(path, self.project)
        self.assertEqual(mode_of(path), 0o600)

    def test_appending_does_not_widen_the_file(self):
        """Every append reopens the file; one of them opening it 0666 would undo the rest."""
        path = self.tmp / "sessions" / "project" / "s.jsonl"
        session = Session(path, self.project)
        session.append_message(Message(role="user", text="hello"))
        session.append_custom("probe", {"ok": True})
        self.assertEqual(mode_of(path), 0o600)

    def test_every_directory_made_to_hold_it_is_owner_only(self):
        """Not just the last one. The names in ``sessions/`` are the projects this user has run
        the agent in, which is not a detail to leave world-listable."""
        path = self.tmp / "sessions" / "project" / "s.jsonl"
        Session(path, self.project)
        self.assertEqual(mode_of(path.parent), 0o700)
        self.assertEqual(mode_of(path.parent.parent), 0o700)


class ResumedSessionFilesAreTightened(_OwnerOnly):
    """A log written before this change is still the file the next turn is appended to."""

    def test_resuming_a_world_readable_log_narrows_it_first(self):
        path = self.tmp / "sessions" / "project" / "s.jsonl"
        Session(path, self.project).append_message(Message(role="user", text="hello"))
        os.chmod(path, 0o644)
        Session(path, self.project, resume=True)
        self.assertEqual(mode_of(path), 0o600)

    def test_the_entries_survive_being_tightened(self):
        path = self.tmp / "sessions" / "project" / "s.jsonl"
        Session(path, self.project).append_message(Message(role="user", text="hello"))
        os.chmod(path, 0o644)
        resumed = Session(path, self.project, resume=True)
        self.assertEqual([message.text for message in resumed.messages()], ["hello"])


class TheCliPathIsWhatActuallyRuns(_OwnerOnly):
    """A unit test that builds ``Session`` by hand can pass while the CLI makes the directory
    some other way. This opens the session the way ``picoagent`` does."""

    def _config(self) -> dict:
        os.environ["PICOAGENT_HOME"] = str(self.tmp / "home")
        return load_config(self.project, {"model": "test", "provider": "scripted"})

    def test_a_session_opened_the_way_the_cli_opens_it_is_owner_only(self):
        session = cli.open_session(self._config(), self.project, None)
        self.assertEqual(mode_of(session.path), 0o600)
        self.assertEqual(mode_of(session.path.parent), 0o700)

    def test_the_sessions_directory_itself_is_not_world_listable(self):
        session = cli.open_session(self._config(), self.project, None)
        self.assertEqual(mode_of(session.path.parent.parent), 0o700)

    def test_logs_written_before_the_upgrade_are_tightened_too(self):
        """Otherwise the fix is cosmetic: the new file is 0600 in a directory of 0644 ones."""
        cfg = self._config()
        directory = cli.session_dir(cfg, self.project)
        directory.mkdir(parents=True)
        old = directory / "1000.jsonl"
        old.write_text('{"kind": "header", "id": "a", "parent": null}\n')
        os.chmod(old, 0o644)
        os.chmod(directory, 0o755)
        os.chmod(directory.parent, 0o755)
        cli.open_session(cfg, self.project, None)
        self.assertEqual(mode_of(old), 0o600)
        self.assertEqual(mode_of(directory), 0o700)
        self.assertEqual(mode_of(directory.parent), 0o700,
                         "the sessions root is where the project names are")

    def test_resuming_last_still_finds_the_session(self):
        """The tightening runs on the directory ``-r last`` then lists; it must not disturb it."""
        cfg = self._config()
        first = cli.open_session(cfg, self.project, None)
        first.append_message(Message(role="user", text="hello"))
        resumed = cli.open_session(cfg, self.project, "last")
        self.assertEqual(resumed.path, first.path)
        self.assertEqual([message.text for message in resumed.messages()], ["hello"])


if __name__ == "__main__":
    unittest.main()
