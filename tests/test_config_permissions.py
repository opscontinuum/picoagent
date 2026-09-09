"""Who else on the machine can read the file the API key is in.

``~/.picoagent/config.toml`` is the documented home for ``[providers.<name>] api_key`` (README,
docs/getting-started.md), and ``~/.picoagent/endpoints/*.toml`` holds one key per external
service. Both were created under whatever the user's umask gives - 0644 on a standard install,
confirmed on the assessment machine - so every account on the host could read the key out of
them, while the credentials file next to them was carefully opened 0600. DISA ASD V6R4 records
that as V-222587: the application must protect the confidentiality of stored information.

The session log answered the same finding for its own file (see
``tests/test_session_log_permissions.py``); these are the other two files that hold a credential.
The rule is the one that file settled on: narrow group and world off an existing file, never
widen, never touch the owner's own bits, and say so rather than doing it silently.

A repository's ``<project>/.picoagent/config.toml`` is deliberately not in scope. It is the
repository's file, it must not hold a credential (``providers`` is ``USER_ONLY``), and rewriting
modes inside somebody's checkout is a surprise.

These tests read the mode back off the filesystem under ``umask(0)``, so a pass cannot be the
umask's doing. POSIX only: mode bits are not access control on Windows, and this codebase does
not pretend otherwise - see T23 in ``docs/security/threat-model.md``.
"""
from __future__ import annotations

import contextlib
import io
import os
import stat
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import temp_dir
from picoagent import cli
from picoagent.core.config import HARDENED_USER_FILES_KEY, harden_user_files, load_config
from picoagent.core.tools import is_windows


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@unittest.skipIf(is_windows(), "mode bits are not access control on Windows; see T23")
class _UserDir(unittest.TestCase):
    """A temp home with no umask, so every mode observed was set on purpose."""

    def setUp(self):
        self.tmp = temp_dir()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.project = self.tmp / "project"
        self.project.mkdir()
        previous = os.umask(0)
        self.addCleanup(os.umask, previous)
        self._old_home = os.environ.get("PICOAGENT_HOME")
        os.environ["PICOAGENT_HOME"] = str(self.home)
        self.addCleanup(self._restore_home)

    def _restore_home(self):
        if self._old_home is None:
            os.environ.pop("PICOAGENT_HOME", None)
        else:
            os.environ["PICOAGENT_HOME"] = self._old_home

    def write(self, relative: str, text: str = "", mode: int = 0o644) -> Path:
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        os.chmod(path, mode)
        return path


class TheKeyFilesAreNarrowedWhenTheConfigIsRead(_UserDir):

    def test_a_world_readable_config_toml_is_narrowed(self):
        """The V-222587 finding as observed: -rw-r--r-- on a file holding ``api_key``."""
        path = self.write("config.toml", '[providers.openai]\napi_key = "sk-planted"\n')
        self.assertEqual(mode_of(path), 0o644)
        load_config(self.project)
        self.assertEqual(mode_of(path), 0o600)

    def test_every_endpoint_file_is_narrowed_too(self):
        """One file per endpoint, one key per endpoint - the same finding, in more files."""
        first = self.write("endpoints/gateway.toml", 'api_key = "sk-a"\n')
        second = self.write("endpoints/artifacts.toml", 'api_key = "sk-b"\n')
        load_config(self.project)
        self.assertEqual(mode_of(first), 0o600)
        self.assertEqual(mode_of(second), 0o600)

    def test_the_endpoints_directory_itself_is_narrowed(self):
        """Its entries are the names of every service this user holds a credential for."""
        self.write("endpoints/gateway.toml", 'api_key = "sk-a"\n')
        os.chmod(self.home / "endpoints", 0o755)
        load_config(self.project)
        self.assertEqual(mode_of(self.home / "endpoints"), 0o700)

    def test_nothing_is_widened_and_the_owners_own_bits_are_left_alone(self):
        """A file deliberately made read-only stays read-only; this removes access, it does not set it."""
        path = self.write("config.toml", "", mode=0o400)
        load_config(self.project)
        self.assertEqual(mode_of(path), 0o400)


class TheUserIsToldTheirFilesChanged(_UserDir):
    """Silently rewriting somebody's modes is how a security fix becomes a support ticket."""

    def test_the_narrowed_paths_are_recorded_on_the_config(self):
        path = self.write("config.toml")
        cfg = load_config(self.project)
        self.assertEqual(cfg[HARDENED_USER_FILES_KEY], [str(path)])

    def test_a_file_that_was_already_owner_only_is_not_reported(self):
        """Every run would otherwise announce a change it did not make."""
        self.write("config.toml", mode=0o600)
        self.assertEqual(load_config(self.project)[HARDENED_USER_FILES_KEY], [])

    def test_the_cli_says_it_on_stderr_and_names_the_file(self):
        """stdout is the agent's answer; a change to the user's own files goes to stderr."""
        path = self.write("config.toml")
        cfg = load_config(self.project)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            cli.report_hardened_user_files(cfg)
        self.assertIn(str(path), stderr.getvalue())
        self.assertIn("readable only by you", stderr.getvalue())

    def test_nothing_is_printed_when_nothing_changed(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            cli.report_hardened_user_files({HARDENED_USER_FILES_KEY: []})
        self.assertEqual(stderr.getvalue(), "")


class WhatIsOutOfScope(_UserDir):

    def test_a_repositorys_own_config_is_left_exactly_as_it_is(self):
        """Not picoagent's file to rewrite, and it must not hold a credential in the first place."""
        path = self.project / ".picoagent" / "config.toml"
        path.parent.mkdir(parents=True)
        path.write_text('model = "gpt-4o-mini"\n')
        os.chmod(path, 0o644)
        load_config(self.project)
        self.assertEqual(mode_of(path), 0o644)

    def test_a_user_directory_that_does_not_exist_yet_is_not_an_error(self):
        self.assertEqual(harden_user_files(self.tmp / "nowhere"), [])

    def test_a_file_this_user_cannot_chmod_is_reported_rather_than_fatal(self):
        """Refusing to start over a mode picoagent could not set would be the worse answer."""
        self.write("config.toml")
        with patch("picoagent.core.config.os.chmod", side_effect=PermissionError(1, "nope")):
            self.assertEqual(load_config(self.project)[HARDENED_USER_FILES_KEY], [])

    def test_it_does_nothing_on_windows_rather_than_claiming_protection(self):
        """``chmod`` there sets a read-only flag and no more; ACLs are what would be needed."""
        self.write("config.toml")
        with patch("picoagent.core.config.is_windows", return_value=True):
            self.assertEqual(harden_user_files(self.home), [])


if __name__ == "__main__":
    unittest.main()
