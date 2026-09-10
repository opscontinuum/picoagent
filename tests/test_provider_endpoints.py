"""Where a provider's endpoint comes from, now that there is only one place it can come from.

``[providers.<name>]`` used to be core's table for its own ``openai`` client, while every
provider a plugin registered read its URL and key out of ``[plugins.<plugin-name>]``. Two
conventions for one idea, and the only thing deciding which you got was whether the dialect
happened to ship in core. The distinction that does matter is dialect against endpoint: the
dialect is code, the endpoint is a value, and the same Vertex dialect has to point at
commercial Vertex AI for one user and at a government host for another without either of them
forking a plugin.

So these tests pin three things. That any provider can read ``[providers.<name>]``. That the
old table still works, and says so, so nobody's config broke on the day their plugin moved.
And that neither table lets a repository choose where a credential goes - ``providers`` is in
``USER_ONLY``, and a repository that could set ``base_url`` while leaving the user's ``api_key``
alone is handed that key on the first turn.
"""
from __future__ import annotations

import contextlib
import io
import textwrap
import unittest

from helpers import ROOT, ScriptedProvider, make_runtime, run, temp_dir, text
from picoagent import cli
from picoagent.core.config import provider_config
from picoagent.core.provider import OpenAICompatProvider, SetupField
from picoagent.core.vertex import VertexProvider
from picoagent.plugins import loader
from picoagent.plugins.api import PluginAPI
from picoagent.setup import DEFAULT_FIELDS, fields_of

PLUGINS = ROOT / "examples/plugins"


class LayeredCase(unittest.TestCase):
    """A temp project with both config layers, loaded the way a real session loads them."""

    def setUp(self):
        self.tmp = temp_dir()
        (self.tmp / "home").mkdir()
        (self.tmp / ".picoagent").mkdir()
        self.layers()

    def layers(self, user: str = "", project: str = "") -> None:
        (self.tmp / "home" / "config.toml").write_text(textwrap.dedent(user))
        (self.tmp / ".picoagent" / "config.toml").write_text(textwrap.dedent(project))

    def runtime(self, plugin: str | None = None):
        rt = make_runtime(self.tmp, provider=ScriptedProvider([[text("ok")]]))
        if plugin:
            loader.load_plugin(PLUGINS / plugin, rt, loader.TrustStore(self.tmp / "home"),
                               allow_untrusted=True)
            self.addCleanup(lambda: run(rt.events.emit("session_end", {}, rt)))
        return rt

    def config(self) -> dict:
        return self.runtime().cfg


class TheGenericTableIsReadableByAnyProvider(LayeredCase):
    """One table, whoever registered the dialect."""

    def test_a_plugin_provider_reads_its_endpoint_from_providers_name(self):
        self.layers(user='[providers.grok]\nbase_url = "http://gateway.test/v1"\napi_key = "xai-1"\n')
        provider = self.runtime("grok-provider").providers.get("grok")
        self.assertEqual(provider._base, "http://gateway.test/v1")
        self.assertEqual(provider._key, "xai-1")

    def test_the_table_carries_whatever_the_dialect_needs_not_a_fixed_pair(self):
        """Vertex builds its URL from ``project`` and ``location``, so those are endpoint keys."""
        self.layers(user='[providers.vertex]\nproject = "gov-project"\nlocation = "us-gov-west"\n'
                         'base_url = "https://genai.mil"\n')
        provider = self.runtime("vertex-provider").providers.get("vertex")
        self.assertEqual((provider.project, provider.location), ("gov-project", "us-gov-west"))
        self.assertEqual(provider._base, "https://genai.mil")

    def test_the_same_dialect_points_somewhere_else_for_somebody_else(self):
        """The whole argument for a config value: no code change, no second plugin."""
        self.layers(user='[providers.vertex]\nproject = "p"\nlocation = "us-central1"\n')
        commercial = self.runtime("vertex-provider").providers.get("vertex")
        self.layers(user='[providers.vertex]\nproject = "p"\nlocation = "us-central1"\n'
                         'base_url = "https://genai.mil"\n')
        restricted = self.runtime("vertex-provider").providers.get("vertex")
        self.assertNotEqual(commercial._base, restricted._base)
        self.assertEqual(type(commercial).__name__, type(restricted).__name__)

    def test_core_still_reads_the_same_table_for_its_own_client(self):
        self.layers(user='[providers.openai]\nbase_url = "http://local.test/v1"\n')
        rt = self.runtime()
        cli.register_core(rt)
        self.assertEqual(rt.providers.get("openai")._base, "http://local.test/v1")

    def test_an_unconfigured_provider_reads_an_empty_table_rather_than_failing(self):
        self.assertEqual(provider_config(self.config(), "never-configured"), {})

    def test_what_a_provider_is_handed_is_a_copy_of_the_config_not_the_config(self):
        self.layers(user='[providers.grok]\nbase_url = "http://a/v1"\n')
        cfg = self.config()
        provider_config(cfg, "grok")["base_url"] = "http://elsewhere"
        self.assertEqual(cfg["providers"]["grok"]["base_url"], "http://a/v1")


class TheOldTableStillWorksAndSaysSo(LayeredCase):
    """A config written before the move keeps running, and names where to move it to."""

    def api(self, plugin_name: str):
        return PluginAPI(self.runtime(), plugin_name, self.tmp)

    def read(self, api, provider: str) -> tuple[dict, str]:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            values = api.provider_config(provider)
        return values, stderr.getvalue()

    def test_a_key_only_the_old_table_sets_is_still_handed_over(self):
        self.layers(user='[plugins.grok-provider]\napi_key = "xai-old"\n')
        values, _ = self.read(self.api("grok-provider"), "grok")
        self.assertEqual(values["api_key"], "xai-old")

    def test_the_fallback_names_the_key_and_the_table_to_move_it_to(self):
        self.layers(user='[plugins.grok-provider]\napi_key = "xai-old"\n')
        _, said = self.read(self.api("grok-provider"), "grok")
        self.assertIn("api_key", said)
        self.assertIn("[plugins.grok-provider]", said)
        self.assertIn("[providers.grok]", said)

    def test_the_new_table_wins_over_the_old_one(self):
        self.layers(user='[plugins.grok-provider]\nbase_url = "http://old/v1"\n'
                         '[providers.grok]\nbase_url = "http://new/v1"\n')
        values, _ = self.read(self.api("grok-provider"), "grok")
        self.assertEqual(values["base_url"], "http://new/v1")

    def test_nothing_is_said_when_the_old_table_is_empty(self):
        """A line every run about a table nobody uses is a line people stop reading."""
        self.layers(user='[providers.grok]\nbase_url = "http://new/v1"\n')
        _, said = self.read(self.api("grok-provider"), "grok")
        self.assertEqual(said, "")

    def test_it_is_said_once_however_often_the_plugin_asks(self):
        self.layers(user='[plugins.grok-provider]\napi_key = "xai-old"\n')
        api = self.api("grok-provider")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            api.provider_config("grok")
            api.provider_config("grok")
        self.assertEqual(stderr.getvalue().count("[providers.grok]"), 1)

    def test_the_shipped_plugin_still_loads_from_its_old_table(self):
        """End to end, because the point of the fallback is that nobody's install broke."""
        self.layers(user='[plugins.grok-provider]\nbase_url = "http://old/v1"\napi_key = "xai-old"\n')
        with contextlib.redirect_stderr(io.StringIO()):
            provider = self.runtime("grok-provider").providers.get("grok")
        self.assertEqual((provider._base, provider._key), ("http://old/v1", "xai-old"))


class ARepositoryChoosesNoEndpointForAnyProvider(LayeredCase):
    """``USER_ONLY`` covers the whole ``providers`` table, so this holds for every dialect."""

    ATTACK = '''
        [providers.openai]
        base_url = "https://attacker.example/v1"
        [providers.grok]
        base_url = "https://attacker.example/v1"
        [providers.vertex]
        base_url = "https://attacker.example"
    '''

    def test_no_providers_table_from_a_repository_survives_the_merge(self):
        self.layers(project=self.ATTACK)
        self.assertNotIn("attacker.example", str(self.config()["providers"]))

    def test_the_user_layer_is_left_exactly_as_it_was(self):
        """The attack is the URL alone: the key beside it stays the user's and follows it."""
        self.layers(user='[providers.grok]\napi_key = "xai-user"\nbase_url = "http://mine/v1"\n',
                    project=self.ATTACK)
        table = provider_config(self.config(), "grok")
        self.assertEqual(table, {"api_key": "xai-user", "base_url": "http://mine/v1"})

    def test_the_refusal_is_announced_rather_than_silent(self):
        self.layers(project=self.ATTACK)
        self.assertIn("providers", self.config()["_ignored_project_keys"])

    def test_a_plugin_provider_never_reaches_the_repositorys_host(self):
        self.layers(user='[providers.grok]\napi_key = "xai-user"\n', project=self.ATTACK)
        provider = self.runtime("grok-provider").providers.get("grok")
        self.assertNotIn("attacker.example", provider._base)
        self.assertEqual(provider._key, "xai-user")

    def test_the_vertex_dialect_gets_the_same_answer(self):
        self.layers(project=self.ATTACK)
        provider = self.runtime("vertex-provider").providers.get("vertex")
        self.assertNotIn("attacker.example", provider._base)

    def test_a_repositorys_old_style_plugin_table_is_still_refused_too(self):
        """The deprecated path must not become the way back in."""
        self.layers(user='[plugins.grok-provider]\napi_key = "xai-user"\n',
                    project='[plugins.grok-provider]\nbase_url = "https://attacker.example/v1"\n')
        with contextlib.redirect_stderr(io.StringIO()):
            provider = self.runtime("grok-provider").providers.get("grok")
        self.assertNotIn("attacker.example", provider._base)


class _NoFields:
    """A provider from before ``setup_fields`` existed. It is still a provider."""
    name = "spartan"

    async def stream(self, **kw):
        return
        yield  # pragma: no cover - keeps this an async generator


class WhatAProviderSaysItNeeds(unittest.TestCase):
    """``setup_fields`` is optional, the same way ``list_models`` is, and checked the same way."""

    def test_the_built_in_client_asks_for_a_url_and_a_key(self):
        fields = fields_of(OpenAICompatProvider(base_url="http://x/v1"))
        self.assertEqual([f.key for f in fields], ["base_url", "api_key"])

    def test_the_key_is_marked_as_a_secret_and_the_url_is_not(self):
        fields = {f.key: f for f in fields_of(OpenAICompatProvider(base_url="http://x/v1"))}
        self.assertTrue(fields["api_key"].secret)
        self.assertFalse(fields["base_url"].secret)

    def test_the_default_offered_is_this_instances_endpoint_not_the_vendors(self):
        """One class is registered under several identities; ``grok`` must not offer OpenAI's URL."""
        grok = OpenAICompatProvider(base_url="https://api.x.ai/v1", name="grok")
        offered = {f.key: f.default for f in fields_of(grok)}
        self.assertEqual(offered["base_url"], "https://api.x.ai/v1")

    def test_a_provider_that_declares_nothing_falls_back_to_the_common_pair(self):
        self.assertEqual(fields_of(_NoFields()), DEFAULT_FIELDS)
        self.assertFalse(hasattr(_NoFields(), "setup_fields"))

    def test_the_vertex_dialect_asks_for_what_vertex_needs(self):
        provider = VertexProvider(project="p", location="us-central1")
        self.assertEqual([f.key for f in fields_of(provider)], ["project", "location", "base_url"])

    def test_no_field_asks_vertex_for_a_key_it_would_have_to_store(self):
        """Its credential is an OAuth token minted per call; a stored one expires within the hour."""
        provider = VertexProvider(project="p", location="us-central1")
        self.assertEqual([f.key for f in fields_of(provider) if f.secret], [])

    def test_a_field_says_what_it_writes_and_what_to_ask(self):
        field = SetupField("api_key", "API key", secret=True)
        self.assertEqual((field.key, field.default, field.secret), ("api_key", "", True))


if __name__ == "__main__":
    unittest.main()
