"""Behavioural tests for the credential-guard example plugin."""
import asyncio
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from helpers import CaptureFrontend, ScriptedProvider, call, make_runtime, run, text, ROOT
from picoagent.core.loop import AgentLoop
from picoagent.plugins import loader

sys.path.insert(0, str(ROOT / "examples/plugins/credential-guard"))
import credential_guard as cg  # noqa: E402

PLUGINS = ROOT / "examples/plugins"


def load(rt, name="credential-guard"):
    return loader.load_plugin(PLUGINS / name, rt, loader.TrustStore(rt.cwd / "home"), allow_untrusted=True)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = cg.credentials_path(self.tmp)

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(cg.read_credentials(self.path), {})

    def test_write_then_read_round_trips(self):
        cg.write_credential(self.path, "openai", "sk-abc123")
        self.assertEqual(cg.read_credentials(self.path), {"openai": "sk-abc123"})

    def test_write_preserves_other_providers(self):
        cg.write_credential(self.path, "openai", "sk-abc123")
        cg.write_credential(self.path, "grok", "grok-xyz")
        self.assertEqual(cg.read_credentials(self.path), {"openai": "sk-abc123", "grok": "grok-xyz"})

    def test_file_is_created_with_owner_only_permissions(self):
        cg.write_credential(self.path, "openai", "sk-abc123")
        mode = stat.S_IMODE(self.path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_loose_permissions_are_tightened_on_rewrite(self):
        cg.write_credential(self.path, "openai", "sk-abc123")
        os.chmod(self.path, 0o644)
        cg.write_credential(self.path, "openai", "sk-new")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_delete_removes_only_the_named_provider(self):
        cg.write_credential(self.path, "openai", "sk-abc123")
        cg.write_credential(self.path, "grok", "grok-xyz")
        self.assertTrue(cg.delete_credential(self.path, "openai"))
        self.assertEqual(cg.read_credentials(self.path), {"grok": "grok-xyz"})

    def test_delete_of_unknown_provider_returns_false(self):
        self.assertFalse(cg.delete_credential(self.path, "nope"))

    def test_mask_shows_only_last_four_characters(self):
        self.assertEqual(cg.mask("sk-abcdefgh1234"), "...1234")
        self.assertEqual(cg.mask("ab"), "...")

    def test_write_credential_reports_restriction_confirmed_on_posix(self):
        self.assertTrue(cg.write_credential(self.path, "openai", "sk-abc123"))

    def test_windows_restriction_uses_icacls_and_reports_success(self):
        with patch("picoagent.core.tools.platform.system", return_value="Windows"), \
             patch.dict(os.environ, {"USERNAME": "alice"}), \
             patch("subprocess.run", return_value=MagicMock(returncode=0)) as run:
            self.assertTrue(cg._restrict_to_owner(self.path))
        run.assert_called_once()
        args = run.call_args.args[0]
        self.assertEqual(args[0], "icacls")
        self.assertIn("alice:F", args)

    def test_windows_restriction_failure_is_reported_not_hidden(self):
        with patch("picoagent.core.tools.platform.system", return_value="Windows"), \
             patch.dict(os.environ, {"USERNAME": "alice"}), \
             patch("subprocess.run", return_value=MagicMock(returncode=1)):
            self.assertFalse(cg._restrict_to_owner(self.path))

    def test_windows_restriction_without_username_env_var_is_reported_as_unconfirmed(self):
        with patch("picoagent.core.tools.platform.system", return_value="Windows"), \
             patch.dict(os.environ, {}, clear=True):
            self.assertFalse(cg._restrict_to_owner(self.path))


class SanitizedEnvTests(unittest.TestCase):
    def test_strips_common_secret_shaped_names(self):
        env = {"PICOAGENT_API_KEY": "x", "OPENAI_API_KEY": "y", "GITHUB_TOKEN": "z",
              "DB_PASSWORD": "w", "AWS_SECRET_ACCESS_KEY": "v", "PATH": "/usr/bin", "HOME": "/home/u"}
        out = cg.sanitized_env(env)
        self.assertEqual(out, {"PATH": "/usr/bin", "HOME": "/home/u"})

    def test_extra_deny_patterns_are_applied_too(self):
        out = cg.sanitized_env({"MY_INTERNAL_FLAG": "x", "PATH": "/usr/bin"}, extra_deny=["MY_INTERNAL"])
        self.assertEqual(out, {"PATH": "/usr/bin"})

    def test_secret_names_a_denylist_would_miss_are_dropped_by_the_allowlist(self):
        """Audit regression: every one of these reached the subprocess under the old denylist."""
        leaky = ["OPENROUTER_KEY", "GROK_KEY", "XAI_KEY", "GEMINI_KEY", "GH_PAT", "PRIVATE_KEY",
                 "AWS_ACCESS_KEY_ID", "DATABASE_URL", "SESSION_COOKIE", "BEARER", "SERVICE_AUTH"]
        out = cg.sanitized_env({name: "secret" for name in leaky} | {"PATH": "/usr/bin"})
        self.assertEqual(out, {"PATH": "/usr/bin"})

    def test_toolchain_paths_still_get_through(self):
        env = {"PATH": "/usr/bin", "VIRTUAL_ENV": "/venv", "JAVA_HOME": "/jdk", "PYTHONPATH": "/src"}
        self.assertEqual(cg.sanitized_env(env), env)

    def test_extra_allow_lets_a_project_opt_a_name_back_in(self):
        out = cg.sanitized_env({"MY_BUILD_FLAG": "1", "PATH": "/usr/bin"}, extra_allow=["MY_BUILD_FLAG"])
        self.assertEqual(out, {"MY_BUILD_FLAG": "1", "PATH": "/usr/bin"})

    def test_extra_allow_cannot_re_enable_a_secret_shaped_name(self):
        out = cg.sanitized_env({"MY_API_KEY": "x", "PATH": "/usr/bin"}, extra_allow=["MY_API_KEY"])
        self.assertEqual(out, {"PATH": "/usr/bin"})

    def test_picoagent_settings_pass_but_its_api_key_does_not(self):
        out = cg.sanitized_env({"PICOAGENT_MODEL": "m", "PICOAGENT_API_KEY": "k", "PATH": "/usr/bin"})
        self.assertEqual(out, {"PICOAGENT_MODEL": "m", "PATH": "/usr/bin"})


class InlineKeyWarningTests(unittest.TestCase):
    def test_reports_providers_with_a_key_in_config(self):
        cfg = {"providers": {"openai": {"api_key": "sk-x"}, "grok": {"base_url": "https://x"}}}
        self.assertEqual(cg.providers_with_inline_keys(cfg), ["openai"])

    def test_no_providers_configured_is_not_an_error(self):
        self.assertEqual(cg.providers_with_inline_keys({}), [])


class GuardedShellToolTests(unittest.TestCase):
    def test_secret_shaped_env_var_is_invisible_to_the_command(self):
        os.environ["PICOAGENT_TEST_API_KEY"] = "totally-secret"
        try:
            tool = cg.GuardedShellTool()
            ctx = _ctx(Path(tempfile.mkdtemp()))
            result = run(tool.execute({"command": "echo $PICOAGENT_TEST_API_KEY"}, ctx))
            self.assertNotIn("totally-secret", result.content)
        finally:
            del os.environ["PICOAGENT_TEST_API_KEY"]

    def test_ordinary_command_still_works(self):
        tool = cg.GuardedShellTool()
        ctx = _ctx(Path(tempfile.mkdtemp()))
        result = run(tool.execute({"command": "echo hello"}, ctx))
        self.assertIn("hello", result.content)
        self.assertFalse(result.is_error)

    def test_timeout_is_reported_and_does_not_hang(self):
        tool = cg.GuardedShellTool()
        ctx = _ctx(Path(tempfile.mkdtemp()))
        result = run(tool.execute({"command": "sleep 5", "timeout": 1}, ctx))
        self.assertTrue(result.is_error)
        self.assertIn("timed out", result.content)


def _ctx(tmp: Path):
    from picoagent.core.tools import ToolContext
    return ToolContext(cwd=tmp, config={"shell_timeout": 10}, tool_call_id="t1", abort=asyncio.Event())


class ToolCallGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.creds_path = cg.credentials_path(self.tmp)
        cg.write_credential(self.creds_path, "openai", "sk-secret")

    def _rt(self):
        class FakeRt:
            cfg = {"_user_dir": None}
        r = FakeRt()
        r.cfg["_user_dir"] = str(self.tmp)
        return r

    def test_read_on_credentials_file_is_blocked(self):
        result = run(cg.guard_tool_call({"name": "read", "args": {"path": str(self.creds_path)}}, self._rt()))
        self.assertTrue(result and result.get("block"))

    def test_read_on_an_unrelated_file_is_allowed(self):
        result = run(cg.guard_tool_call({"name": "read", "args": {"path": "some/other/file.py"}}, self._rt()))
        self.assertIsNone(result)

    def test_shell_cat_on_credentials_file_is_blocked(self):
        result = run(cg.guard_tool_call(
            {"name": "shell", "args": {"command": f"cat {self.creds_path}"}}, self._rt()))
        self.assertTrue(result and result.get("block"))

    def test_shell_cat_elsewhere_is_allowed(self):
        result = run(cg.guard_tool_call({"name": "shell", "args": {"command": "cat README.md"}}, self._rt()))
        self.assertIsNone(result)

    def test_write_and_edit_and_grep_search_are_also_blocked(self):
        for name in ("write", "edit", "grep_search"):
            result = run(cg.guard_tool_call({"name": name, "args": {"path": str(self.creds_path)}}, self._rt()))
            self.assertTrue(result and result.get("block"), f"{name} should be blocked")

    # ---------------------------------------------------------------- audit regressions
    # Each of these was a confirmed bypass found in a security audit of this plugin.

    def test_grep_search_on_the_parent_directory_is_blocked(self):
        """It recurses, so pointing it at the containing dir dumped the file without naming it."""
        result = run(cg.guard_tool_call({"name": "grep_search", "args": {"path": str(self.tmp)}}, self._rt()))
        self.assertTrue(result and result.get("block"))

    def test_grep_search_on_a_grandparent_directory_is_also_blocked(self):
        result = run(cg.guard_tool_call(
            {"name": "grep_search", "args": {"path": str(self.tmp.parent)}}, self._rt()))
        self.assertTrue(result and result.get("block"))

    def test_grep_search_on_an_unrelated_directory_is_still_allowed(self):
        elsewhere = Path(tempfile.mkdtemp())
        result = run(cg.guard_tool_call({"name": "grep_search", "args": {"path": str(elsewhere)}}, self._rt()))
        self.assertIsNone(result)

    def test_config_toml_is_protected_like_the_credentials_file(self):
        """config.toml can hold [providers.x] api_key, so it needs the same protection."""
        config = self.tmp / "config.toml"
        config.write_text('[providers.openai]\napi_key = "sk-in-config"\n')
        for name in ("read", "grep_search"):
            result = run(cg.guard_tool_call({"name": name, "args": {"path": str(config)}}, self._rt()))
            self.assertTrue(result and result.get("block"), f"{name} on config.toml should be blocked")

    def test_a_symlink_alias_to_the_credentials_file_is_blocked(self):
        alias = self.tmp / "alias.txt"
        alias.symlink_to(self.creds_path)
        result = run(cg.guard_tool_call({"name": "read", "args": {"path": str(alias)}}, self._rt()))
        self.assertTrue(result and result.get("block"))

    def test_a_hardlink_to_the_credentials_file_is_blocked(self):
        """resolve() can't see through a hardlink, so identity is compared by inode."""
        link = self.tmp / "hard.txt"
        os.link(self.creds_path, link)
        result = run(cg.guard_tool_call({"name": "read", "args": {"path": str(link)}}, self._rt()))
        self.assertTrue(result and result.get("block"))

    def test_structured_data_cannot_pretty_print_the_key_out_of_config(self):
        """Guarding a *list of tool names* let structured_data walk straight past it."""
        config = self.tmp / "config.toml"
        config.write_text('[providers.openai]\napi_key = "sk-in-config"\n')
        result = run(cg.guard_tool_call(
            {"name": "structured_data", "args": {"path": str(config), "query": "providers.openai.api_key"}},
            self._rt()))
        self.assertTrue(result and result.get("block"))

    def test_the_path_check_applies_to_any_tool_name_including_unknown_ones(self):
        """Fail closed: a tool added later must not inherit a bypass by not being on a list."""
        result = run(cg.guard_tool_call(
            {"name": "some_plugin_tool_invented_later", "args": {"path": str(self.creds_path)}},
            self._rt()))
        self.assertTrue(result and result.get("block"))

    def test_alternate_path_argument_names_are_checked_too(self):
        result = run(cg.guard_tool_call({"name": "other", "args": {"file": str(self.creds_path)}},
                                        self._rt()))
        self.assertTrue(result and result.get("block"))


class SecretsCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _rt(self):
        return make_runtime(self.tmp, provider=ScriptedProvider([[text("ok")]]))

    def test_list_with_nothing_stored(self):
        rt = self._rt()
        self.assertIn("(none)", run(cg.secrets_command("list", rt)))

    def test_set_without_a_tty_gives_a_clear_error_rather_than_hanging(self):
        rt = self._rt()
        with patch.object(cg.sys.stdin, "isatty", return_value=False):
            result = run(cg.secrets_command("set openai", rt))
        self.assertIn("interactive", result)

    def test_set_with_a_fake_prompt_stores_and_masks(self):
        rt = self._rt()
        async def fake_prompt(prompt):
            return "sk-realvalue1234"
        with patch.object(cg, "_prompt_secret", new=fake_prompt):
            result = run(cg.secrets_command("set openai", rt))
        self.assertIn("1234", result)
        self.assertNotIn("sk-realvalue1234", result)
        creds = cg.read_credentials(cg.credentials_path(Path(rt.cfg["_user_dir"])))
        self.assertEqual(creds["openai"], "sk-realvalue1234")

    def test_set_then_show_masks_the_value(self):
        rt = self._rt()
        cg.write_credential(cg.credentials_path(Path(rt.cfg["_user_dir"])), "openai", "sk-realvalue1234")
        result = run(cg.secrets_command("show openai", rt))
        self.assertIn("1234", result)
        self.assertNotIn("sk-realvalue1234", result)

    def test_delete_then_show_reports_not_stored(self):
        rt = self._rt()
        path = cg.credentials_path(Path(rt.cfg["_user_dir"]))
        cg.write_credential(path, "openai", "sk-x")
        run(cg.secrets_command("delete openai", rt))
        self.assertIn("not stored", run(cg.secrets_command("show openai", rt)))


class PluginIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_shell_tool_is_overridden_and_secrets_command_registered(self):
        rt = make_runtime(self.tmp, provider=ScriptedProvider([[text("ok")]]))
        load(rt)
        self.assertEqual(rt.tools.get("shell").description.count("secret"), 1)
        self.assertIsNotNone(rt.commands.get("secrets"))

    def test_bundled_skill_is_registered(self):
        rt = make_runtime(self.tmp, provider=ScriptedProvider([[text("ok")]]))
        load(rt)
        self.assertEqual(rt.skills.get("secure-credential-handling").source, "plugin:credential-guard")

    def test_a_stored_key_is_wired_into_the_openai_provider_on_load(self):
        rt = make_runtime(self.tmp, provider=ScriptedProvider([[text("ok")]]))
        rt.providers.register(_DummyOpenAI())
        cg.write_credential(cg.credentials_path(Path(rt.cfg["_user_dir"])), "openai", "sk-loaded-key")
        load(rt)
        self.assertEqual(rt.providers.get("openai")._key, "sk-loaded-key")

    def test_leaked_env_var_does_not_survive_a_real_shell_tool_call_end_to_end(self):
        os.environ["PICOAGENT_TEST_SECRET_TOKEN"] = "shhh-dont-tell"
        try:
            rt = make_runtime(self.tmp, provider=ScriptedProvider(
                [[call("shell", command="echo $PICOAGENT_TEST_SECRET_TOKEN")], [text("done")]]))
            load(rt)
            run(AgentLoop(rt).run("check env"))
            self.assertNotIn("shhh-dont-tell", rt.frontend.tool_results()[0].content)
        finally:
            del os.environ["PICOAGENT_TEST_SECRET_TOKEN"]


class _DummyOpenAI:
    name = "openai"
    _key = "unset"

    async def stream(self, **kw):
        return
        yield  # pragma: no cover - never reached, keeps this an async generator


if __name__ == "__main__":
    unittest.main()


class ArgumentNameCoverageTests(unittest.TestCase):
    """The guard must not depend on tools naming their path argument "path".

    An audit found stig_evidence takes ``repo`` and so walked the credentials directory
    untouched. ``root``, ``directory`` and ``target`` were equally free. Enumerating argument
    names is the same mistake as enumerating tools, one level down.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.creds = cg.credentials_path(self.tmp)
        self.creds.parent.mkdir(parents=True, exist_ok=True)
        cg.write_credential(self.creds, "openai", "sk-secret")

    def _rt(self):
        class FakeRt:
            cfg = {"_user_dir": str(self.tmp)}
        return FakeRt()

    def guard(self, name, args):
        return run(cg.guard_tool_call({"name": name, "args": args}, self._rt()))

    def test_any_argument_naming_the_credentials_file_is_blocked(self):
        for argument in ("path", "repo", "root", "directory", "target", "src", "wherever"):
            with self.subTest(argument=argument):
                self.assertTrue(self.guard("some_tool", {argument: str(self.creds)}),
                                f"{argument!r} let the credentials file through")

    def test_a_path_inside_a_list_argument_is_blocked(self):
        self.assertTrue(self.guard("some_tool", {"paths": ["README.md", str(self.creds)]}))

    def test_a_recursive_tool_pointed_at_the_containing_directory_is_blocked(self):
        self.assertTrue(self.guard("stig_evidence", {"repo": str(self.creds.parent)}))

    def test_unrelated_arguments_are_still_allowed(self):
        self.assertIsNone(self.guard("stig_set", {"status": "Open",
                                                  "finding_details": "reviewed, looks fine"}))
        self.assertIsNone(self.guard("read", {"path": "some/other/file.py"}))

    def test_non_string_arguments_do_not_raise(self):
        """Arguments are model output: ints, bools, dicts and None must not crash the guard."""
        self.assertIsNone(self.guard("t", {"n": 5, "ok": True, "d": {"a": 1}, "z": None,
                                           "items": [1, 2, None]}))
