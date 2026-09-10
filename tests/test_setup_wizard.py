"""``picoagent setup``: what it asks, what it writes, and what it refuses to do.

A first run with nothing configured used to print an OpenAI 401 body - text about an
``api_key`` request parameter, naming no file picoagent reads and no command that would fix it.
This command is the fix, so the things worth pinning are the ones that make it safe to point at
somebody's config file: it edits rather than rewrites, it never replaces a value without being
told to, it never shows a stored secret in full, it refuses rather than hangs when there is
nobody to answer, and the file it leaves behind is readable only by its owner.

The wizard is driven here through a scripted frontend rather than a terminal, and the model
call it verifies with goes to the fake server in ``picoagent.testing.fakes``. Nothing here
touches the network.
"""
from __future__ import annotations

import contextlib
import io
import os
import stat
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import run, temp_dir
from picoagent import cli, setup
from picoagent.core.config import load_config
from picoagent.core.loop import Runtime
from picoagent.core.session import Session
from picoagent.core.toml_write import TomlEditError, apply_edits, render_key, render_value
from picoagent.core.tools import is_windows
from picoagent.core.vertex import VertexProvider
from picoagent.testing.fakes import FakeServer


class ScriptedFrontend:
    """Answers questions from a list, in order, and remembers what it was asked."""

    def __init__(self, answers: list):
        self.answers = list(answers)
        self.asked: list[tuple[str, str, dict]] = []
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event, payload):
        self.events.append((event, payload))

    async def ask(self, kind, prompt, **kw):
        self.asked.append((kind, prompt, kw))
        if not self.answers:
            raise AssertionError(f"the wizard asked more than the script answers: {kind} {prompt}")
        return self.answers.pop(0)

    async def read_input(self):
        return None

    async def run(self, agent):
        pass

    def prompts(self) -> str:
        return "\n".join(prompt for _, prompt, _ in self.asked)

    def text(self) -> str:
        return "\n".join(payload.get("text", "") for _, payload in self.events)


class WizardCase(unittest.TestCase):
    """A temp home and project, and a fake model server the wizard can verify against."""

    def setUp(self):
        self.tmp = temp_dir()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.project = self.tmp / "project"
        self.project.mkdir()
        self.scratch = self.tmp / "scratch"
        self.scratch.mkdir()
        self.sessions = 0
        previous = os.environ.get("PICOAGENT_HOME")
        os.environ["PICOAGENT_HOME"] = str(self.home)
        self.addCleanup(self._restore, previous)
        self.server = FakeServer("openai").start()
        self.addCleanup(self.server.stop)

    def _restore(self, previous):
        if previous is None:
            os.environ.pop("PICOAGENT_HOME", None)
        else:
            os.environ["PICOAGENT_HOME"] = previous

    @property
    def config_file(self) -> Path:
        return self.home / "config.toml"

    def runtime(self, overrides: dict, frontend) -> Runtime:
        self.sessions += 1
        cfg = load_config(self.project, overrides)
        rt = Runtime(cfg, self.project, Session(self.scratch / f"{self.sessions}.jsonl", self.project))
        cli.register_core(rt)
        rt.frontend = frontend
        return rt

    def wizard(self, answers: list, provider: str | None = "openai") -> tuple[int, ScriptedFrontend]:
        """Drive one whole run. Its stderr is kept on ``self.stderr`` rather than the terminal.

        "Which provider?" is now always the first question - the list carries a "create a new
        one" option, so there is no single-provider shortcut to skip it - and every script here
        would otherwise open with the same answer. ``provider`` supplies it; pass ``None`` to
        script that question yourself, which is what the tests about *creating* a provider do.
        """
        frontend = ScriptedFrontend(answers if provider is None else [provider, *answers])
        first = self.runtime({}, frontend)
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            code = run(setup.run(first, lambda overrides: self.runtime(overrides, frontend)))
        self.stderr = captured.getvalue()
        return code, frontend

    def written(self) -> dict:
        return tomllib.loads(self.config_file.read_text())

    @staticmethod
    def options_of(frontend: ScriptedFrontend, opening: str) -> list:
        """The options of the first ``select`` whose prompt starts with ``opening``.

        By prompt rather than by position: the wizard asks several questions from a list now -
        which provider, which wire format, which model - and an index would silently start
        asserting about a different question the next time one is added.
        """
        for kind, prompt, kw in frontend.asked:
            if kind == "select" and prompt.startswith(opening):
                return list(kw.get("options") or [])
        raise AssertionError(f"the wizard never asked a select starting {opening!r}")


class TheWizardWritesWhatItWasTold(WizardCase):

    def test_a_first_run_writes_the_provider_the_model_and_the_endpoint(self):
        code, _ = self.wizard([self.server.url + "/v1", "sk-typed", "fake-small"])
        self.assertEqual(code, 0)
        written = self.written()
        self.assertEqual(written["provider"], "openai")
        self.assertEqual(written["model"], "fake-small")
        self.assertEqual(written["providers"]["openai"],
                         {"base_url": self.server.url + "/v1", "api_key": "sk-typed"})

    def test_the_model_list_comes_from_the_server_it_was_just_pointed_at(self):
        _, frontend = self.wizard([self.server.url + "/v1", "sk-typed", "fake-small"])
        options = self.options_of(frontend, "Which model?")
        self.assertIn("fake-large", options)
        self.assertIn("fake-small", options)

    def test_the_verification_is_a_real_completion_against_the_new_endpoint(self):
        """Not a rehearsal: the provider that answers is the one a real session would build."""
        self.wizard([self.server.url + "/v1", "sk-typed", "fake-small"])
        posts = [r for r in self.server.requests if r["path"].endswith("/chat/completions")]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["body"]["model"], "fake-small")
        self.assertEqual(posts[0]["headers"]["Authorization"], "Bearer sk-typed")

    def test_a_successful_verification_is_reported(self):
        _, frontend = self.wizard([self.server.url + "/v1", "sk-typed", "fake-small"])
        self.assertIn("fake-small answered", frontend.text())

    def test_a_model_the_server_did_not_list_can_still_be_typed_in(self):
        code, _ = self.wizard([self.server.url + "/v1", "sk-typed", setup.TYPE_IT_IN, "gpt-5"])
        self.assertEqual(code, 0)
        self.assertEqual(self.written()["model"], "gpt-5")

    def test_the_path_and_the_mode_are_printed(self):
        """Somebody's file changed, and picoagent chose its mode; both are said out loud."""
        self.wizard([self.server.url + "/v1", "sk-typed", "fake-small"])
        self.assertIn(str(self.config_file), self.stderr)
        if not is_windows():
            self.assertIn("0600", self.stderr)

    @unittest.skipIf(is_windows(), "mode bits are not access control on Windows; see T23")
    def test_the_file_it_wrote_the_key_into_is_readable_only_by_its_owner(self):
        """Read back off the filesystem, because the claim is about the file and not the call."""
        previous = os.umask(0)
        self.addCleanup(os.umask, previous)
        self.wizard([self.server.url + "/v1", "sk-typed", "fake-small"])
        self.assertEqual(stat.S_IMODE(self.config_file.stat().st_mode), 0o600)


class NothingElseInTheFileMoves(WizardCase):
    """It is somebody's config file, with their comments and their settings in it."""

    EXISTING = ('# how I like it\n'
                'temperature = 0.2   # pinned on purpose\n'
                'context_files = ["AGENTS.md"]\n'
                '\n'
                '[plugins]\n'
                'enabled = ["git:example/thing@v1"]\n')

    def test_comments_and_unrelated_settings_survive(self):
        self.config_file.write_text(self.EXISTING)
        self.wizard([self.server.url + "/v1", "sk-typed", "fake-small"])
        text = self.config_file.read_text()
        self.assertIn("# how I like it", text)
        self.assertIn("# pinned on purpose", text)
        self.assertEqual(self.written()["plugins"]["enabled"], ["git:example/thing@v1"])
        self.assertEqual(self.written()["temperature"], 0.2)

    def test_a_second_run_that_changes_nothing_writes_nothing(self):
        self.wizard([self.server.url + "/v1", "sk-typed", "fake-small"])
        before = self.config_file.read_text()
        code, frontend = self.wizard(["", "", "fake-small"])
        self.assertEqual(code, 0)
        self.assertEqual(self.config_file.read_text(), before)
        self.assertIn("nothing to change", frontend.text())

    def test_empty_input_keeps_what_is_already_there(self):
        self.wizard([self.server.url + "/v1", "sk-typed", "fake-small"])
        self.wizard(["", "", "fake-large", True])
        self.assertEqual(self.written()["providers"]["openai"]["api_key"], "sk-typed")
        self.assertEqual(self.written()["model"], "fake-large")

    def test_the_stored_value_is_offered_back_as_the_default(self):
        """Re-running is an edit, not a retype."""
        self.wizard([self.server.url + "/v1", "sk-typed", "fake-small"])
        _, frontend = self.wizard(["", "", "fake-small"])
        self.assertIn(self.server.url + "/v1", frontend.prompts())

    def test_a_stored_secret_is_never_offered_back_in_full(self):
        self.wizard([self.server.url + "/v1", "sk-a-long-secret-value", "fake-small"])
        _, frontend = self.wizard(["", "", "fake-small"])
        self.assertNotIn("sk-a-long-secret-value", frontend.prompts())
        self.assertIn("alue", frontend.prompts())

    def test_the_key_is_asked_for_without_echo(self):
        _, frontend = self.wizard([self.server.url + "/v1", "sk-typed", "fake-small"])
        secret = [kw.get("secret") for kind, prompt, kw in frontend.asked
                  if kind == "input" and "API key" in prompt]
        self.assertEqual(secret, [True])


class NothingIsReplacedWithoutBeingAsked(WizardCase):

    def setUp(self):
        super().setUp()
        self.wizard([self.server.url + "/v1", "sk-first", "fake-small"])

    def test_replacing_a_stored_value_is_confirmed_first(self):
        _, frontend = self.wizard(["http://elsewhere.test/v1", False, "", "fake-small"])
        confirms = [prompt for kind, prompt, _ in frontend.asked if kind == "confirm"]
        self.assertTrue(any("base_url is already" in prompt for prompt in confirms))

    def test_a_declined_replacement_keeps_the_stored_value(self):
        self.wizard(["http://elsewhere.test/v1", False, "", "fake-small"])
        self.assertEqual(self.written()["providers"]["openai"]["base_url"], self.server.url + "/v1")

    def test_the_confirmation_shows_the_secret_masked_not_whole(self):
        _, frontend = self.wizard(["", "sk-second-key-entirely", False, "fake-small"])
        confirms = [prompt for kind, prompt, _ in frontend.asked if kind == "confirm"]
        self.assertTrue(any("api_key is already" in prompt for prompt in confirms))
        self.assertNotIn("sk-first", frontend.prompts())

    def test_changing_the_model_over_a_stored_one_is_confirmed(self):
        _, frontend = self.wizard(["", "", "fake-large", True])
        confirms = [prompt for kind, prompt, _ in frontend.asked if kind == "confirm"]
        self.assertTrue(any("model is already" in prompt for prompt in confirms))
        self.assertEqual(self.written()["model"], "fake-large")

    def test_a_declined_model_change_leaves_the_stored_one(self):
        self.wizard(["", "", "fake-large", False])
        self.assertEqual(self.written()["model"], "fake-small")


class AFailedVerificationIsTheUsersToOverride(WizardCase):

    def unreachable(self) -> str:
        """A port nothing is listening on, so the call is refused rather than slow."""
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return f"http://127.0.0.1:{sock.getsockname()[1]}/v1"

    def test_the_failure_is_reported_in_picoagents_own_words(self):
        _, frontend = self.wizard([self.unreachable(), "k", "gpt-4o-mini", False])
        self.assertIn("picoagent setup", frontend.text())
        self.assertIn("could not be reached", frontend.text())

    def test_declining_to_save_writes_nothing(self):
        code, _ = self.wizard([self.unreachable(), "k", "gpt-4o-mini", False])
        self.assertEqual(code, 1)
        self.assertFalse(self.config_file.exists())

    def test_saving_anyway_is_allowed_because_the_endpoint_may_be_down(self):
        base = self.unreachable()
        code, _ = self.wizard([base, "k", "gpt-4o-mini", True])
        self.assertEqual(code, 0)
        self.assertEqual(self.written()["providers"]["openai"]["base_url"], base)


class ThereHasToBeSomebodyToAsk(unittest.TestCase):
    """A wizard that blocks on ``input()`` inside a CI job is a hung build with no output."""

    def test_the_flag_refuses_and_says_where_to_write_the_settings_instead(self):
        refusal = setup.refusal_without_a_terminal(True)
        self.assertIn("--non-interactive", refusal)
        self.assertIn("[providers.<name>]", refusal)

    def test_a_redirected_stdin_refuses_for_its_own_reason(self):
        with patch("picoagent.setup.sys.stdin", io.StringIO("")):
            refusal = setup.refusal_without_a_terminal(False)
        self.assertIn("not a terminal", refusal)

    def test_a_terminal_and_no_flag_is_not_refused(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True

        with patch("picoagent.setup.sys.stdin", Tty()):
            self.assertIsNone(setup.refusal_without_a_terminal(False))

    def test_the_command_exits_non_zero_and_says_so_on_stderr(self):
        args = cli.build_parser().parse_args(["setup", "--non-interactive"])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = cli.setup_command(args)
        self.assertEqual(code, 1)
        self.assertIn("picoagent setup", stderr.getvalue())

    def test_it_refuses_before_reading_anything(self):
        """The answer does not depend on the config, so nothing is opened to produce it."""
        args = cli.build_parser().parse_args(["setup", "--non-interactive"])
        with patch("picoagent.cli.load_config", side_effect=AssertionError("read the config")):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.setup_command(args), 1)


class TheFileIsEditedNotRewritten(unittest.TestCase):
    """``tomllib`` has no writer, so this module has one. What it must never do is corrupt."""

    def test_a_key_is_replaced_in_place_and_its_comment_kept(self):
        out = apply_edits('model = "old"  # why\n', {(): {"model": "new"}})
        self.assertEqual(out, 'model = "new"  # why\n')

    def test_a_new_key_lands_inside_the_table_it_belongs_to(self):
        out = apply_edits('[providers.openai]\nbase_url = "u"\n\n[plugins]\nenabled = []\n',
                          {("providers", "openai"): {"api_key": "k"}})
        self.assertIn('base_url = "u"\napi_key = "k"\n', out)

    def test_a_top_level_key_lands_above_the_first_table(self):
        out = apply_edits('[plugins]\nenabled = []\n', {(): {"model": "m"}})
        self.assertTrue(out.startswith('model = "m"\n[plugins]'))

    def test_a_table_the_file_does_not_have_is_added_at_the_end(self):
        out = apply_edits('model = "m"\n', {("providers", "vertex"): {"project": "p"}})
        self.assertEqual(tomllib.loads(out)["providers"]["vertex"], {"project": "p"})
        self.assertTrue(out.endswith("\n"))

    def test_a_comment_introducing_the_next_table_is_not_written_into(self):
        source = '[a]\nx = 1\n\n# about openai\n[providers.openai]\nbase_url = "u"\n'
        out = apply_edits(source, {("a",): {"y": 2}})
        self.assertIn("x = 1\ny = 2\n", out)
        self.assertIn("# about openai\n[providers.openai]", out)

    def test_a_value_spanning_several_lines_is_replaced_whole(self):
        out = apply_edits('skill_dirs = [\n  "a",\n  "b",\n]\nmodel = "m"\n',
                          {(): {"skill_dirs": ["a", "b", "c"]}})
        self.assertEqual(tomllib.loads(out)["skill_dirs"], ["a", "b", "c"])
        self.assertIn('model = "m"', out)

    def test_an_array_of_tables_is_left_alone(self):
        source = '[[servers]]\nname = "one"\n'
        out = apply_edits(source, {("providers", "openai"): {"api_key": "k"}})
        self.assertEqual(tomllib.loads(out)["servers"], [{"name": "one"}])

    def test_a_hash_inside_a_string_is_not_read_as_a_comment(self):
        out = apply_edits('base_url = "http://h/v1#frag"\n', {(): {"model": "m"}})
        self.assertEqual(tomllib.loads(out)["base_url"], "http://h/v1#frag")

    def test_nothing_is_changed_when_there_is_nothing_to_change(self):
        self.assertEqual(apply_edits("model = 'm'\n", {}), "model = 'm'\n")

    def test_a_file_that_is_not_toml_is_refused_rather_than_appended_to(self):
        with self.assertRaises(TomlEditError):
            apply_edits("[[[not toml\n", {(): {"model": "m"}})

    def test_a_value_toml_cannot_hold_is_refused_rather_than_guessed_at(self):
        with self.assertRaises(TomlEditError):
            apply_edits("", {(): {"model": {"nested": object()}}})

    def test_a_shape_the_scanner_does_not_handle_refuses_rather_than_corrupts(self):
        """The check exists for the file this scanner reads wrongly, and there will be one.

        A table declared as a dotted key at the top level is that file today: nothing here sees
        ``[providers.openai]``, so the edit would append a second declaration of it. What must
        not happen is that the result is written; what happens instead is that ``tomllib``
        refuses to read it back and the config is left exactly as it was.
        """
        with self.assertRaises(TomlEditError) as caught:
            apply_edits('providers.openai.base_url = "u"\n', {("providers", "openai"): {"api_key": "k"}})
        self.assertIn("unchanged", str(caught.exception))

    def test_a_key_whose_name_is_not_a_table_is_refused(self):
        with self.assertRaises(TomlEditError):
            apply_edits('providers = "a string"\n', {("providers", "openai"): {"api_key": "k"}})


class StringsComeBackOutAsTheyWentIn(unittest.TestCase):
    """A provider's name reaches ``[providers.<name>]``, and a key can be anything at all."""

    def round_trip(self, name: str, value: str) -> str:
        out = apply_edits("", {("providers", name): {"api_key": value}})
        return tomllib.loads(out)["providers"][name]["api_key"]

    def test_a_quote_and_a_backslash_in_a_value_survive(self):
        self.assertEqual(self.round_trip("p", 'k"\\end'), 'k"\\end')

    def test_a_quote_and_a_backslash_in_a_table_name_survive(self):
        name = 'we"ird\\name'
        out = apply_edits("", {("providers", name): {"api_key": "k"}})
        self.assertEqual(list(tomllib.loads(out)["providers"]), [name])

    def test_a_dot_in_a_name_stays_one_table_and_does_not_become_two(self):
        out = apply_edits("", {("providers", "a.b"): {"api_key": "k"}})
        self.assertEqual(tomllib.loads(out)["providers"], {"a.b": {"api_key": "k"}})

    def test_a_newline_and_a_tab_survive(self):
        self.assertEqual(self.round_trip("p", "line\nnext\tafter"), "line\nnext\tafter")

    def test_a_control_character_survives(self):
        self.assertEqual(self.round_trip("p", "before\x01after"), "before\x01after")

    def test_a_bare_key_is_written_bare_and_anything_else_is_quoted(self):
        self.assertEqual(render_key("api_key"), "api_key")
        self.assertEqual(render_key("api key"), '"api key"')

    def test_a_bool_is_not_written_as_the_integer_python_thinks_it_is(self):
        self.assertEqual(render_value(True), "true")
        self.assertEqual(render_value(1), "1")


class SecretsAreShownOnlyAsMuchAsTheyMustBe(unittest.TestCase):

    def test_a_long_key_keeps_only_its_ends(self):
        masked = setup.mask("sk-proj-abcdefghijklmnop")
        self.assertNotIn("abcdefghijkl", masked)
        self.assertTrue(masked.endswith("mnop"))

    def test_a_short_one_shows_nothing_at_all(self):
        self.assertEqual(setup.mask("short"), "*****")

    def test_an_empty_one_is_named_rather_than_shown_as_blank(self):
        self.assertEqual(setup.mask(""), "(empty)")

class AProviderCanBeInventedRatherThanOnlyEdited(WizardCase):
    """The list of registered providers can only ever be an edit, and the common case is neither.

    A fresh checkout registers one provider - the built-in client - and the person running setup
    has an xAI key, an Ollama server or a Gemini project in front of them. Before this the wizard
    said "provider: openai (the only one registered)" and then asked them to point *that* at
    api.x.ai, which writes the right endpoint under the wrong name and leaves ``--provider grok``
    meaning nothing. Now the last option in the list makes a new one, asks which wire format it
    speaks, and writes the table that rebuilds it.
    """

    def _vertex_token(self) -> None:
        previous = os.environ.get("GOOGLE_OAUTH_ACCESS_TOKEN")
        os.environ["GOOGLE_OAUTH_ACCESS_TOKEN"] = "ya29.test"
        self.addCleanup(lambda: os.environ.pop("GOOGLE_OAUTH_ACCESS_TOKEN", None)
                        if previous is None else os.environ.__setitem__(
                            "GOOGLE_OAUTH_ACCESS_TOKEN", previous))

    def new_openai(self, name: str = "grok") -> tuple[int, ScriptedFrontend]:
        return self.wizard([setup.NEW_PROVIDER, name, "openai",
                            self.server.url + "/v1", "xai-typed", "fake-small"], provider=None)

    def new_vertex(self, server: FakeServer) -> tuple[int, ScriptedFrontend]:
        self._vertex_token()
        return self.wizard([setup.NEW_PROVIDER, "milgemini", "vertex",
                            "mil-project", "us-central1", server.url, "gemini-2.5-pro"],
                           provider=None)

    def test_the_option_is_offered_even_when_one_provider_is_registered(self):
        _, frontend = self.new_openai()
        self.assertIn(setup.NEW_PROVIDER, self.options_of(frontend, "Which provider?"))

    def test_the_new_name_is_what_the_session_will_use(self):
        code, _ = self.new_openai()
        self.assertEqual((code, self.written()["provider"]), (0, "grok"))

    def test_its_settings_land_in_its_own_table(self):
        self.new_openai()
        self.assertEqual(self.written()["providers"]["grok"],
                         {"base_url": self.server.url + "/v1", "api_key": "xai-typed"})

    def test_the_default_dialect_is_not_written_out_as_a_line_saying_the_default(self):
        self.new_openai()
        self.assertNotIn("dialect", self.written()["providers"]["grok"])

    def test_the_provider_that_already_existed_is_left_alone(self):
        self.new_openai()
        self.assertNotIn("openai", self.written().get("providers", {}))

    def test_a_second_dialect_is_offered_by_name(self):
        _, frontend = self.new_openai()
        self.assertEqual(self.options_of(frontend, "Which wire format"), ["openai", "vertex"])

    def test_a_vertex_provider_writes_the_dialect_that_rebuilds_it(self):
        with FakeServer("vertex") as server:
            self.new_vertex(server)
        self.assertEqual(self.written()["providers"]["milgemini"]["dialect"], "vertex")

    def test_a_vertex_provider_is_asked_for_vertexs_settings_not_for_a_key(self):
        with FakeServer("vertex") as server:
            _, frontend = self.new_vertex(server)
        self.assertIn("Google Cloud project id", frontend.prompts())
        self.assertNotIn("API key", frontend.prompts())

    def test_a_vertex_provider_keeps_the_host_it_was_pointed_at(self):
        with FakeServer("vertex") as server:
            self.new_vertex(server)
            table = self.written()["providers"]["milgemini"]
        self.assertEqual((table["base_url"], table["project"], table["location"]),
                         (server.url, "mil-project", "us-central1"))

    def test_what_was_written_rebuilds_the_dialect_that_was_verified(self):
        """The file is the whole record: a later session reads it and gets the same client."""
        with FakeServer("vertex") as server:
            self.new_vertex(server)
        rebuilt = self.runtime({}, ScriptedFrontend([])).providers.get("milgemini")
        self.assertIsInstance(rebuilt, VertexProvider)

    def test_the_verification_really_reached_the_vertex_server(self):
        with FakeServer("vertex") as server:
            self.new_vertex(server)
            self.assertTrue(server.requests)

    def test_a_name_that_is_already_a_provider_is_refused_rather_than_silently_edited(self):
        code, frontend = self.wizard([setup.NEW_PROVIDER, "openai"], provider=None)
        self.assertEqual(code, 1)
        self.assertIn("already a provider", frontend.text())

    def test_a_refused_name_writes_nothing(self):
        self.wizard([setup.NEW_PROVIDER, "openai"], provider=None)
        self.assertFalse(self.config_file.exists())

    def test_an_empty_name_is_refused_too(self):
        code, frontend = self.wizard([setup.NEW_PROVIDER, "  "], provider=None)
        self.assertEqual(code, 1)
        self.assertIn("no name given", frontend.text())


if __name__ == "__main__":
    unittest.main()
