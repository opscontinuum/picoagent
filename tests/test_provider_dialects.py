"""One provider per ``[providers.<name>]`` table, and the dialect that table names.

The change these pin: a dialect is code and an endpoint is a value, so core ships the two wire
formats and a config file supplies the names and the URLs. ``[providers.grok] base_url =
"https://api.x.ai/v1"`` is a working provider with nothing installed, because the plugin it used
to need was the built-in client renamed. What still needs a plugin is a *third* dialect, and the
seam for that is unchanged - a plugin registering a provider replaces whatever a table built,
which is the last class of test here.

The refusal matters as much as the registration. An unknown ``dialect`` must not fall back to
the OpenAI client: against a strict server that is a 400 about a request field nobody can trace
to a typo, and against a proxy that accepts both formats it is worse - it works, with the wrong
mapping, and the model quietly stops seeing half of what it was sent.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import textwrap
import unittest
from pathlib import Path

from helpers import CaptureFrontend, ROOT, run, temp_dir
from picoagent import cli
from picoagent.core.config import load_config
from picoagent.core.dialects import (DEFAULT_DIALECT, DIALECTS, UnknownDialect, build_provider,
                                     providers_from_config)
from picoagent.core.loop import AgentLoop, Runtime
from picoagent.core.provider import OpenAICompatProvider
from picoagent.core.session import Session
from picoagent.core.vertex import VertexProvider
from picoagent.plugins import loader
from picoagent.testing.fakes import FakeServer

EXAMPLES = ROOT / "examples/plugins"


class DialectCase(unittest.TestCase):
    """A temp home whose ``config.toml`` this test writes, wired the way a session wires one."""

    def setUp(self):
        self.tmp = temp_dir()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.set_env("PICOAGENT_HOME", str(self.home))
        # The Google and xAI fallbacks are read at build time, and other files in this suite
        # export them without putting them back. Pinning them here keeps a test that is about a
        # config table from depending on which file ran before it.
        for name in ("VERTEX_BASE_URL", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION",
                     "GOOGLE_OAUTH_ACCESS_TOKEN", "XAI_BASE_URL", "XAI_API_KEY",
                     "PICOAGENT_BASE_URL", "PICOAGENT_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_KEY"):
            self.set_env(name, None)

    def set_env(self, name: str, value: str | None) -> None:
        previous = os.environ.get(name)
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
        self.addCleanup(self._restore, name, previous)

    @staticmethod
    def _restore(name: str, previous: str | None) -> None:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous

    def runtime(self, user: str = "", plugin: str | Path | None = None) -> Runtime:
        """A runtime with ``user`` as the config file, core registered, and stderr captured."""
        (self.home / "config.toml").write_text(textwrap.dedent(user))
        cfg = load_config(self.tmp, {})
        rt = Runtime(cfg, self.tmp, Session(self.tmp / "session.jsonl", self.tmp))
        rt.frontend = CaptureFrontend()
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            cli.register_core(rt)
            if plugin is not None:
                path = plugin if isinstance(plugin, Path) else EXAMPLES / plugin
                loader.load_plugin(path, rt, loader.TrustStore(self.home), allow_untrusted=True)
        self.stderr = captured.getvalue()
        return rt


class ATableWithNoDialectIsAnOpenAICompatibleEndpoint(DialectCase):
    """The default, because it is the format most servers speak and the one a first run needs."""

    GROK = '''
        [providers.grok]
        base_url = "https://api.x.ai/v1"
        api_key  = "xai-configured"
    '''

    def test_the_table_name_becomes_a_provider_name(self):
        self.assertIn("grok", self.runtime(self.GROK).providers.names())

    def test_it_is_the_built_in_client(self):
        self.assertIsInstance(self.runtime(self.GROK).providers.get("grok"), OpenAICompatProvider)

    def test_it_points_where_the_table_points_it(self):
        provider = self.runtime(self.GROK).providers.get("grok")
        self.assertEqual(provider._base, "https://api.x.ai/v1")

    def test_it_carries_the_key_from_its_own_table(self):
        provider = self.runtime(self.GROK).providers.get("grok")
        self.assertEqual(provider._key, "xai-configured")

    def test_it_answers_to_its_own_name_and_not_to_the_class_default(self):
        """One class, several identities: a failure from this one must not say 'openai'."""
        self.assertEqual(self.runtime(self.GROK).providers.get("grok").name, "grok")

    def test_the_setup_default_offered_is_this_tables_url(self):
        fields = {f.key: f.default for f in self.runtime(self.GROK).providers.get("grok").setup_fields}
        self.assertEqual(fields["base_url"], "https://api.x.ai/v1")

    def test_extra_headers_reach_the_client(self):
        rt = self.runtime('[providers.gw]\nheaders = { "X-Tenant" = "acme" }\n')
        self.assertEqual(rt.providers.get("gw")._headers, {"X-Tenant": "acme"})

    def test_a_keyless_local_server_is_an_ordinary_table(self):
        rt = self.runtime('[providers.local]\nbase_url = "http://localhost:11434/v1"\n')
        self.assertEqual((rt.providers.get("local")._base, rt.providers.get("local")._key),
                         ("http://localhost:11434/v1", ""))


class TheVertexDialectIsSelectedByName(DialectCase):
    """``dialect = "vertex"`` is the one key that changes which wire format is spoken."""

    MIL = '''
        [providers.milgemini]
        dialect  = "vertex"
        base_url = "https://genai.example"
        project  = "mil-project"
        location = "us-gov-west1"
    '''

    def test_the_table_builds_the_gemini_client(self):
        self.assertIsInstance(self.runtime(self.MIL).providers.get("milgemini"), VertexProvider)

    def test_it_is_registered_under_the_tables_name_not_under_vertex(self):
        names = self.runtime(self.MIL).providers.names()
        self.assertIn("milgemini", names)
        self.assertNotIn("vertex", names)

    def test_the_same_dialect_points_at_a_host_that_is_not_googles(self):
        """The whole reason the dialect and the endpoint are separate things."""
        self.assertEqual(self.runtime(self.MIL).providers.get("milgemini")._base,
                         "https://genai.example")

    def test_project_and_location_are_endpoint_settings_like_the_url(self):
        provider = self.runtime(self.MIL).providers.get("milgemini")
        self.assertEqual((provider.project, provider.location), ("mil-project", "us-gov-west1"))

    def test_the_url_it_builds_carries_both_of_them(self):
        url = self.runtime(self.MIL).providers.get("milgemini")._url("gemini-2.5-pro")
        self.assertEqual(url, "https://genai.example/v1/projects/mil-project/locations/us-gov-west1"
                              "/publishers/google/models/gemini-2.5-pro:streamGenerateContent?alt=sse")

    def test_no_base_url_derives_the_commercial_host_from_the_location(self):
        rt = self.runtime('[providers.vertex]\ndialect = "vertex"\nlocation = "europe-west4"\n')
        self.assertEqual(rt.providers.get("vertex")._base,
                         "https://europe-west4-aiplatform.googleapis.com")

    def test_it_asks_setup_for_what_vertex_needs_rather_than_a_url_and_a_key(self):
        fields = [f.key for f in self.runtime(self.MIL).providers.get("milgemini").setup_fields]
        self.assertEqual(fields, ["project", "location", "base_url"])

    def test_it_asks_for_no_secret_at_all(self):
        """Its credential is an OAuth token minted per call; a stored one dies within the hour."""
        provider = self.runtime(self.MIL).providers.get("milgemini")
        self.assertEqual([f.key for f in provider.setup_fields if f.secret], [])

    def test_a_non_http_endpoint_is_refused_as_an_error_event_not_a_file_read(self):
        rt = self.runtime('[providers.v]\ndialect = "vertex"\nbase_url = "file:///etc"\n')

        async def collect():
            return [event async for event in rt.providers.get("v").stream(
                system="s", messages=[], tools=[], model="m", max_tokens=8, thinking="off")]

        events = run(collect())
        self.assertEqual([e.type for e in events], ["error"])
        self.assertIn("must be http or https", events[0].error)


class AnUnknownDialectIsRefusedRatherThanGuessedAt(DialectCase):
    """Never a fallback: the wrong wire format against a configured endpoint is the worst end."""

    TYPO = '''
        [providers.gem]
        dialect  = "gemini"
        base_url = "https://genai.example"
    '''

    def test_nothing_is_registered_under_that_name(self):
        self.assertNotIn("gem", self.runtime(self.TYPO).providers.names())

    def test_it_does_not_quietly_become_an_openai_client(self):
        rt = self.runtime(self.TYPO)
        self.assertNotIn("gem", [p for p in rt.providers.names()])
        with self.assertRaises(KeyError):
            rt.providers.get("gem")

    def test_the_refusal_names_the_provider_the_value_and_the_dialects_that_exist(self):
        self.runtime(self.TYPO)
        for expected in ("'gem'", "'gemini'", "openai", "vertex"):
            self.assertIn(expected, self.stderr)

    def test_startup_is_not_stopped_for_the_providers_that_do_work(self):
        """A half-configured table must not take the tool down with it."""
        rt = self.runtime(self.TYPO + '\n[providers.local]\nbase_url = "http://127.0.0.1:1/v1"\n')
        self.assertIn("local", rt.providers.names())
        self.assertIn("openai", rt.providers.names())

    def test_a_dialect_that_is_not_even_a_string_is_refused_the_same_way(self):
        self.runtime('[providers.odd]\ndialect = 7\n')
        self.assertIn("'odd'", self.stderr)

    def test_build_provider_raises_rather_than_returning_nothing(self):
        with self.assertRaises(UnknownDialect):
            build_provider("gem", {"dialect": "gemini"})

    def test_the_refusal_is_collected_rather_than_logged_from_the_builder(self):
        """The caller decides where a startup notice goes; the builder only says what happened."""
        providers, refusals = providers_from_config({"providers": {"gem": {"dialect": "gemini"}}})
        self.assertEqual(providers, [])
        self.assertEqual(len(refusals), 1)


class SeveralTablesAreSeveralProviders(DialectCase):
    """The config file is the list of providers, so the list can be as long as somebody needs."""

    MANY = '''
        [providers.grok]
        base_url = "https://api.x.ai/v1"
        [providers.local]
        base_url = "http://localhost:11434/v1"
        [providers.milgemini]
        dialect  = "vertex"
        base_url = "https://genai.example"
        project  = "p"
        location = "us-gov-west1"
    '''

    def test_every_table_is_registered_and_openai_is_still_there(self):
        self.assertEqual(sorted(self.runtime(self.MANY).providers.names()),
                         ["grok", "local", "milgemini", "openai"])

    def test_two_tables_of_one_dialect_are_two_separate_clients(self):
        rt = self.runtime(self.MANY)
        self.assertNotEqual(rt.providers.get("grok")._base, rt.providers.get("local")._base)

    def test_the_dialects_are_mixed_freely_in_one_file(self):
        rt = self.runtime(self.MANY)
        self.assertIsInstance(rt.providers.get("grok"), OpenAICompatProvider)
        self.assertIsInstance(rt.providers.get("milgemini"), VertexProvider)

    def test_none_of_it_needed_a_plugin(self):
        self.runtime(self.MANY)
        self.assertEqual(self.stderr, "")


class TheZeroConfigPathIsUnchanged(DialectCase):
    """``DEFAULTS`` carries ``providers = {"openai": {}}``, and an empty table is a whole answer."""

    def test_an_empty_config_still_registers_openai(self):
        self.assertEqual(self.runtime().providers.names(), ["openai"])

    def test_it_is_the_built_in_client_pointed_at_the_vendor(self):
        provider = self.runtime().providers.get("openai")
        self.assertIsInstance(provider, OpenAICompatProvider)
        self.assertEqual(provider._base, "https://api.openai.com/v1")

    def test_the_environment_fallbacks_still_win_over_the_empty_table(self):
        """A table that sets nothing must not read as a table that sets the empty string."""
        self.set_env("PICOAGENT_BASE_URL", "http://from-env/v1")
        self.set_env("PICOAGENT_API_KEY", "sk-from-env")
        provider = self.runtime().providers.get("openai")
        self.assertEqual((provider._base, provider._key), ("http://from-env/v1", "sk-from-env"))

    def test_a_configured_base_url_still_beats_the_environment(self):
        self.set_env("PICOAGENT_BASE_URL", "http://from-env/v1")
        provider = self.runtime('[providers.openai]\nbase_url = "http://from-file/v1"\n').providers.get("openai")
        self.assertEqual(provider._base, "http://from-file/v1")

    def test_nothing_is_said_on_stderr_about_any_of_it(self):
        self.runtime()
        self.assertEqual(self.stderr, "")

    def test_openai_is_the_dialect_a_table_gets_by_not_asking(self):
        self.assertEqual(DEFAULT_DIALECT, "openai")
        self.assertEqual(sorted(DIALECTS), ["openai", "vertex"])


class APluginStillWinsOverATable(DialectCase):
    """Later registration replaces, and plugins load after core - so a plugin can still override."""

    def plugin_dir(self, name: str, body: str) -> Path:
        directory = self.tmp / name
        directory.mkdir()
        (directory / "plugin.toml").write_text(
            f'name = "{name}"\nversion = "0.1.0"\nentry = "{name}:register"\n'
            'description = "test provider"\n')
        (directory / f"{name}.py").write_text(textwrap.dedent(body))
        return directory

    MARKER = '''
        class Marker:
            name = "grok"
            registered_by = "plugin"

            async def stream(self, **kw):
                return
                yield

        def register(api):
            api.register_provider(Marker())
    '''

    def test_the_plugins_object_is_the_one_in_the_registry(self):
        plugin = self.plugin_dir("markerplugin", self.MARKER)
        rt = self.runtime('[providers.grok]\nbase_url = "http://from-config/v1"\n', plugin=plugin)
        self.assertEqual(getattr(rt.providers.get("grok"), "registered_by", None), "plugin")

    def test_the_table_is_not_refused_it_is_simply_replaced(self):
        """Core builds it, the plugin lands on the same name; neither of them complains."""
        plugin = self.plugin_dir("markerplugin", self.MARKER)
        rt = self.runtime('[providers.grok]\nbase_url = "http://from-config/v1"\n', plugin=plugin)
        self.assertEqual(self.stderr, "")
        self.assertIn("grok", rt.providers.names())

    def test_a_shipped_plugins_dialect_replaces_the_table_built_client(self):
        """``[providers.vertex]`` with no dialect builds an OpenAI client; the plugin corrects it."""
        rt = self.runtime('[providers.vertex]\nproject = "p"\nlocation = "us-central1"\n',
                          plugin="vertex-provider")
        self.assertNotIsInstance(rt.providers.get("vertex"), OpenAICompatProvider)

    def test_the_providers_the_plugin_did_not_name_are_untouched(self):
        plugin = self.plugin_dir("markerplugin", self.MARKER)
        rt = self.runtime('[providers.local]\nbase_url = "http://localhost:11434/v1"\n', plugin=plugin)
        self.assertEqual(rt.providers.get("local")._base, "http://localhost:11434/v1")


class TheExamplePluginsStillLoad(DialectCase):
    """Both are redundant now. Neither may break for somebody who still has them enabled."""

    def test_grok_provider_loads_and_registers_its_provider(self):
        rt = self.runtime(plugin="grok-provider")
        self.assertEqual(rt.providers.get("grok").name, "grok")

    def test_grok_provider_still_reads_its_endpoint_from_the_providers_table(self):
        rt = self.runtime('[providers.grok]\nbase_url = "http://mine/v1"\napi_key = "xai-mine"\n',
                          plugin="grok-provider")
        provider = rt.providers.get("grok")
        self.assertEqual((provider._base, provider._key), ("http://mine/v1", "xai-mine"))

    def test_vertex_provider_loads_and_registers_its_own_class(self):
        rt = self.runtime(plugin="vertex-provider")
        self.assertEqual(type(rt.providers.get("vertex")).__name__, "VertexProvider")
        self.assertNotIsInstance(rt.providers.get("vertex"), VertexProvider)

    def test_the_two_of_them_load_together_without_colliding(self):
        rt = self.runtime(plugin="grok-provider")
        with contextlib.redirect_stderr(io.StringIO()):
            loader.load_plugin(EXAMPLES / "vertex-provider", rt, loader.TrustStore(self.home),
                               allow_untrusted=True)
        self.assertEqual(sorted(rt.providers.names()), ["grok", "openai", "vertex"])

    def test_a_config_table_and_the_example_plugin_are_not_a_double_registration_failure(self):
        rt = self.runtime('[providers.vertex]\ndialect = "vertex"\nproject = "p"\n',
                          plugin="vertex-provider")
        self.assertEqual(rt.providers.names().count("vertex"), 1)


class TheVertexDialectTalksToAVertexServer(DialectCase):
    """The dialect against the fake in ``picoagent.testing.fakes``, driven from a config table.

    End to end rather than by unit: the point of the move is that a table alone produces a
    working provider, so the test that proves it has to start at the table and finish at a
    request the fake accepts - path, ``alt=sse``, bearer token, Gemini's schema restrictions.
    """

    def round_trip(self, server: FakeServer) -> Runtime:
        self.set_env("GOOGLE_OAUTH_ACCESS_TOKEN", "ya29.test")
        rt = self.runtime(f'''
            [providers.milgemini]
            dialect  = "vertex"
            base_url = "{server.url}"
            project  = "p"
            location = "us-central1"
        ''')
        rt.provider_name = "milgemini"
        run(AgentLoop(rt).run("hello"))
        return rt

    def test_a_table_alone_produces_a_working_provider(self):
        with FakeServer("vertex") as server:
            rt = self.round_trip(server)
            self.assertIn("tool said: from-server", rt.frontend.text)

    def test_the_tool_call_came_back_and_ran(self):
        with FakeServer("vertex") as server:
            rt = self.round_trip(server)
            results = rt.frontend.tool_results()
            self.assertEqual(len(results), 1)
            self.assertIn("from-server", results[0].content)

    def test_the_tool_result_went_back_as_a_gemini_function_response(self):
        with FakeServer("vertex") as server:
            self.round_trip(server)
            self.assertIn("functionResponse", json.dumps(server.requests[1]["body"]["contents"]))

    def test_the_tool_schemas_were_cleaned_for_gemini(self):
        with FakeServer("vertex") as server:
            self.round_trip(server)
            self.assertNotIn("additionalProperties", json.dumps(server.requests[0]["body"]["tools"]))

    def test_the_request_went_to_the_host_the_table_named(self):
        with FakeServer("vertex") as server:
            self.round_trip(server)
            self.assertTrue(server.requests[0]["path"].startswith("/v1/projects/p/locations/us-central1/"))


class SelectingAProviderThatIsNotThereIsRefusedNotCrashed(DialectCase):
    """The second half of refusing a dialect: what happens when the user then selects it.

    A refusal that registers nothing is only honest if selecting that name says so. Left alone,
    ``AgentLoop._model_turn`` reached ``ProviderRegistry.get`` and the ``KeyError`` came out of
    ``main`` as a stack trace - after the REPL had drawn, naming a registry rather than the
    config line that chose the name. So the check happens where the runtime is built, and the
    same answer covers a typo in ``--provider``.
    """

    def refuse(self, user: str = "", provider: str = "broken") -> tuple[int, str]:
        (self.home / "config.toml").write_text(textwrap.dedent(user))
        args = cli.build_parser().parse_args(["--cwd", str(self.tmp), "--provider", provider, "-p", "hi"])
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured), self.assertRaises(SystemExit) as caught:
            cli.build_runtime_or_refuse(args)
        return caught.exception.code, captured.getvalue()

    BROKEN = '[providers.broken]\ndialect = "anthropic"\n'

    def test_a_refused_dialect_selected_by_name_does_not_reach_a_traceback(self):
        code, stderr = self.refuse(self.BROKEN)
        self.assertEqual(code, cli.EXIT_NO_SUCH_PROVIDER)
        self.assertIn("no provider called 'broken' is registered", stderr)

    def test_the_dialect_refusal_is_still_the_first_thing_said(self):
        """Two lines, in the order the user needs: why it is missing, then that it is missing."""
        _, stderr = self.refuse(self.BROKEN)
        self.assertLess(stderr.index("not a wire format"), stderr.index("nothing to send a prompt to"))

    def test_it_lists_what_is_registered_and_names_the_command_that_fixes_it(self):
        _, stderr = self.refuse(self.BROKEN)
        self.assertIn("Registered: openai", stderr)
        self.assertIn("picoagent setup", stderr)

    def test_a_typo_in_the_provider_flag_gets_the_same_answer(self):
        code, stderr = self.refuse(provider="nope")
        self.assertEqual(code, cli.EXIT_NO_SUCH_PROVIDER)
        self.assertIn("no provider called 'nope'", stderr)

    def test_a_provider_that_is_there_is_not_refused(self):
        args = cli.build_parser().parse_args(["--cwd", str(self.tmp), "--provider", "openai", "-p", "hi"])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.build_runtime_or_refuse(args).provider_name, "openai")

    def test_the_code_is_its_own_so_a_wrapper_need_not_read_the_english(self):
        self.assertNotIn(cli.EXIT_NO_SUCH_PROVIDER,
                         (cli.EXIT_REQUIRED_PLUGIN, cli.EXIT_PLUGIN_PROVENANCE, cli.EXIT_MODEL_ERROR))


if __name__ == "__main__":
    unittest.main()
