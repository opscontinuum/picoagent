"""What a model-composed command can see of the user's environment.

``ShellTool`` handed every command ``{**os.environ, "PICOAGENT": "1"}``, so ``env`` - one of the
first things a model runs when it wants to know where it is - returned the user's API keys as a
tool result. The loop appends every tool result to the session log and replays it to the model on
the next turn, so one such command put a credential in a file on disk *and* in the next prompt.
DISA ASD V6R4 records that as V-222444: the application must not write sensitive data into the
application logs. Making the log owner-only (V-222500) answered who can read it and not what is
in it.

The environment allowlist that closes this existed in ``examples/plugins/credential-guard`` and
was opt-in, which is the same as absent for anyone who has not installed it - the threat model
records the closing condition for T20 as "the environment allowlist is the built-in shell's
default". These tests hold it to that: the built-in tool, no plugins, nothing configured.

They drive the real ``ShellTool`` and read the real subprocess's output rather than asserting
that some function was called, because the finding is about what comes back in a tool result.
"""
from __future__ import annotations

import os
import unittest

from helpers import run, temp_dir, tool_ctx
from picoagent.core.config import USER_ONLY, load_config
from picoagent.core.tools import SHELL_ENV_ALLOWLIST, ShellTool, is_windows, shell_env


class TheBuiltInShellStripsTheEnvironment(unittest.TestCase):
    """The real tool, the real subprocess, the real output."""

    def setUp(self):
        self.tmp = temp_dir()

    @unittest.skipIf(is_windows(), "`env` is POSIX; the allowlist itself is covered below")
    def test_a_secret_in_the_environment_does_not_come_back_in_a_tool_result(self):
        """The V-222444 check, run rather than reasoned about: grep the result for the secret."""
        os.environ["ACME_DEPLOY_KEY"] = "sk-planted-4f1a9c"
        self.addCleanup(os.environ.pop, "ACME_DEPLOY_KEY", None)
        result = run(ShellTool().execute({"command": "env"}, tool_ctx(self.tmp)))
        self.assertNotIn("sk-planted-4f1a9c", result.content)
        self.assertNotIn("ACME_DEPLOY_KEY", result.content)

    @unittest.skipIf(is_windows(), "`env` is POSIX; the allowlist itself is covered below")
    def test_the_command_still_gets_what_an_ordinary_build_needs(self):
        """An allowlist that strips PATH is a tool that cannot run ``npm test``."""
        result = run(ShellTool().execute({"command": "env"}, tool_ctx(self.tmp)))
        self.assertIn("PATH=", result.content)
        self.assertIn("PICOAGENT=1", result.content)


class TheAllowlist(unittest.TestCase):
    """``shell_env`` builds the environment; these are the rules it applies."""

    def test_names_a_denylist_of_secret_shapes_would_miss_are_dropped(self):
        leaky = ["OPENROUTER_KEY", "GH_PAT", "PRIVATE_KEY", "AWS_ACCESS_KEY_ID", "DATABASE_URL",
                 "ACME_DEPLOY", "SESSION_COOKIE", "NPM_TOKEN"]
        env = shell_env({name: "secret" for name in leaky} | {"PATH": "/usr/bin"}, {})
        self.assertEqual(env, {"PATH": "/usr/bin", "PICOAGENT": "1"})

    def test_the_variables_a_toolchain_needs_survive(self):
        base = {"PATH": "/usr/bin", "HOME": "/home/u", "LANG": "C.UTF-8", "VIRTUAL_ENV": "/venv",
                "CARGO_HOME": "/c", "TMPDIR": "/tmp"}
        self.assertEqual(shell_env(base, {}), base | {"PICOAGENT": "1"})

    def test_a_config_that_never_heard_of_the_setting_still_strips(self):
        """Every embedder that builds a config dict by hand gets the safe default."""
        self.assertNotIn("GH_PAT", shell_env({"GH_PAT": "x"}, {}))

    def test_picoagents_own_api_key_variable_is_not_in_the_list(self):
        """``PICOAGENT_API_KEY`` is a documented way to supply the key, so it is a credential."""
        self.assertNotIn("PICOAGENT_API_KEY", SHELL_ENV_ALLOWLIST)
        self.assertNotIn("OPENAI_API_KEY", SHELL_ENV_ALLOWLIST)


class TheOptOut(unittest.TestCase):
    """A user who wants the old behaviour can have it, by name, in their own config."""

    def test_shell_env_inherit_passes_the_whole_environment(self):
        env = shell_env({"GH_PAT": "x"}, {"shell_env": "inherit"})
        self.assertEqual(env, {"GH_PAT": "x", "PICOAGENT": "1"})

    def test_shell_env_allow_names_one_more_variable(self):
        env = shell_env({"ACME_BUILD_FLAG": "1"}, {"shell_env_allow": ["ACME_BUILD_FLAG"]})
        self.assertEqual(env, {"ACME_BUILD_FLAG": "1", "PICOAGENT": "1"})

    def test_a_value_that_is_not_a_recognised_mode_falls_back_to_the_allowlist(self):
        """A typo, or a repository's value that arrived some other way, must not open the door."""
        for value in ["inherit_all", "", "off", True, 1, None, ["inherit"]]:
            with self.subTest(value=value):
                self.assertNotIn("GH_PAT", shell_env({"GH_PAT": "x"}, {"shell_env": value}))

    def test_a_malformed_allow_list_is_ignored_rather_than_fatal(self):
        for value in ["PATH", 5, {"a": 1}, [1, 2]]:
            with self.subTest(value=value):
                self.assertNotIn("GH_PAT", shell_env({"GH_PAT": "x"}, {"shell_env_allow": value}))


class ARepositoryCannotWidenIt(unittest.TestCase):
    """The same rule ``[plugins.credential-guard] extra_allow_env`` already answers.

    A cloned repository naming ``DATABASE_URL`` in its own config would put it in the environment
    of the first command the model ran, and the output of that command goes to the session log.
    Choosing what a credential-carrying variable may reach is not taste.
    """

    def setUp(self):
        self.tmp = temp_dir()
        (self.tmp / ".picoagent").mkdir()
        self._old_home = os.environ.get("PICOAGENT_HOME")
        os.environ["PICOAGENT_HOME"] = str(self.tmp / "home")
        self.addCleanup(self._restore_home)

    def _restore_home(self):
        if self._old_home is None:
            os.environ.pop("PICOAGENT_HOME", None)
        else:
            os.environ["PICOAGENT_HOME"] = self._old_home

    def test_both_settings_are_user_only(self):
        self.assertIn(("shell_env",), USER_ONLY)
        self.assertIn(("shell_env_allow",), USER_ONLY)

    def test_a_repository_asking_for_the_whole_environment_is_refused_and_told_so(self):
        (self.tmp / ".picoagent" / "config.toml").write_text(
            'shell_env = "inherit"\nshell_env_allow = ["DATABASE_URL"]\n')
        cfg = load_config(self.tmp)
        self.assertEqual(cfg["shell_env"], "allowlist")
        self.assertEqual(cfg["shell_env_allow"], [])
        self.assertIn("shell_env", cfg["_ignored_project_keys"])
        self.assertIn("shell_env_allow", cfg["_ignored_project_keys"])


if __name__ == "__main__":
    unittest.main()
