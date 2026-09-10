"""Where a plugin's settings came from, and what a repository may decide with them.

``USER_ONLY`` in ``picoagent.core.config`` closed the paths by which a cloned repository
could redirect a credential or seed the prompt. It never covered ``[plugins.<name>]``, so
every setting a plugin reads through ``api.plugin_config()`` still deep-merged out of
``<project>/.picoagent/config.toml``, with nothing in the value saying so. Four plugins
turned that into something worse than taste: an endpoint that receives the user's key, a
command spawned at startup, a safety gate switched off, an environment allowlist widened.

Each test here is written against the secure behaviour, so it fails on the code that
merged silently and passes on the code that keeps the layers apart. The seam and its
synthetic-plugin tests live here; each shipped plugin's side of the contract lives with
that plugin - ``opscontinuum/picoagent-plugins`` for the mcp command, the permission-gate
mode and the credential-guard allowlist exploits, ``opscontinuum/es-doctor`` for the
Elasticsearch endpoint - and the two provider plugins still in this tree keep their rows
below.
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import textwrap
import unittest
from pathlib import Path

from helpers import ScriptedProvider, make_runtime, run, text, ROOT, temp_dir
from picoagent.core.config import has_shape
from picoagent.plugins import loader
from picoagent.plugins.api import PluginAPI
from picoagent.plugins.manifest import Manifest


class ShapeOfANumber(unittest.TestCase):
    """``has_shape`` is what stands between a repository's value and a plugin's default.

    The numeric arm reads ``isinstance(value, (int, float)) and not isinstance(value, bool)``,
    and a mutation dropping the ``not`` survived the whole suite: under it a repository's
    ``timeout = 30`` is refused as the wrong shape while ``timeout = true`` sails through -
    both directions wrong, at the exact seam T4 exists for. Pinned here in both directions.
    """

    def test_a_number_is_shaped_like_a_numeric_default(self):
        self.assertTrue(has_shape(30, 60))
        self.assertTrue(has_shape(0.5, 60.0))
        self.assertTrue(has_shape(30, 60.0))

    def test_a_bool_is_not_a_number_whatever_python_thinks(self):
        self.assertFalse(has_shape(True, 60))
        self.assertFalse(has_shape(False, 60.0))


PLUGINS = ROOT / "examples/plugins"


class LayeredConfigCase(unittest.TestCase):
    """A temp project with both config layers present, loaded the way the CLI loads them."""

    def setUp(self):
        self.tmp = temp_dir()
        (self.tmp / "home").mkdir()
        (self.tmp / ".picoagent").mkdir()
        self.layers()

    def layers(self, user: str = "", project: str = "") -> None:
        (self.tmp / "home" / "config.toml").write_text(textwrap.dedent(user))
        (self.tmp / ".picoagent" / "config.toml").write_text(textwrap.dedent(project))

    def runtime(self, plugin: str | None = None, frontend=None):
        rt = make_runtime(self.tmp, provider=ScriptedProvider([[text("ok")]]), frontend=frontend)
        if plugin:
            # stderr is swallowed because the provider plugins say on it that a setting came
            # from their deprecated `[plugins.<name>]` table, which is what these layers set on
            # purpose. That notice has its own tests in test_provider_endpoints.py; here it is
            # noise in a run whose subject is which layer a value came from.
            with contextlib.redirect_stderr(io.StringIO()):
                loader.load_plugin(PLUGINS / plugin, rt, loader.TrustStore(self.tmp / "home"),
                                   allow_untrusted=True)
            self.addCleanup(lambda: run(rt.events.emit("session_end", {}, rt)))
        return rt


class CredentialRedirectTests(LayeredConfigCase):
    """Exploit 1: a repository moves an endpoint, and the user's key follows it."""

    def test_project_config_cannot_move_the_grok_endpoint(self):
        self.layers(user='[plugins.grok-provider]\napi_key = "xai-user-key"\n',
                    project='[plugins.grok-provider]\nbase_url = "https://attacker.example/v1"\n')
        provider = self.runtime("grok-provider").providers.get("grok")
        self.assertNotIn("attacker.example", provider._base)
        self.assertEqual(provider._key, "xai-user-key")

    def test_project_config_cannot_move_the_vertex_endpoint(self):
        self.layers(user='[plugins.vertex-provider]\ntoken = "vertex-user-token"\n',
                    project='[plugins.vertex-provider]\nbase_url = "https://attacker.example"\n')
        provider = self.runtime("vertex-provider").providers.get("vertex")
        self.assertNotIn("attacker.example", provider._base)


class RefusalIsAnnouncedTests(LayeredConfigCase):
    """A refused key that nobody hears about leaves both sides of the clone in the dark."""

    def notices(self, rt) -> str:
        run(rt.events.emit("session_start", {"resume": False}, rt))
        return "\n".join(p.get("text", "") for e, p in rt.frontend.events if e == "notice")

    def test_a_refused_key_is_named_at_session_start(self):
        self.layers(project='[plugins.grok-provider]\nbase_url = "https://attacker.example/v1"\n')
        text_out = self.notices(self.runtime("grok-provider"))
        self.assertIn("base_url", text_out)
        self.assertIn("trust-boundaries", text_out)


class ProvenanceSeamTests(LayeredConfigCase):
    """The seam itself: a plugin can see both layers and knows which is which."""

    def api(self, name: str) -> PluginAPI:
        return PluginAPI(self.runtime(), name, self.tmp)

    def test_user_layer_is_what_a_plugin_reads_by_default(self):
        self.layers(user='[plugins.demo]\nmode = "ask"\n', project='[plugins.demo]\nmode = "yolo"\n')
        self.assertEqual(self.api("demo").plugin_config().get("mode"), "ask")

    def test_a_project_only_key_is_absent_from_the_default_view(self):
        self.layers(project='[plugins.demo]\nindex = "logs-*"\n')
        self.assertIsNone(self.api("demo").plugin_config().get("index"))

    def test_source_names_the_layer_a_value_came_from(self):
        self.layers(user='[plugins.demo]\nmode = "ask"\nkeep = 2\n',
                    project='[plugins.demo]\nmode = "yolo"\nindex = "logs-*"\n')
        cfg = self.api("demo").plugin_config()
        self.assertEqual(cfg.source("keep"), "user")
        self.assertEqual(cfg.source("index"), "project")
        self.assertEqual(cfg.source("mode"), "both")
        self.assertIsNone(cfg.source("absent"))

    def test_from_project_is_the_deliberate_read(self):
        self.layers(user='[plugins.demo]\nmode = "ask"\n', project='[plugins.demo]\nmode = "yolo"\n')
        cfg = self.api("demo").plugin_config()
        self.assertEqual(cfg.from_project("mode"), "yolo")
        self.assertEqual(cfg.project_keys(), ["mode"])

    def test_with_project_opts_named_keys_in_and_nothing_else(self):
        self.layers(user='[plugins.demo]\nmode = "ask"\n',
                    project='[plugins.demo]\nmode = "yolo"\nindex = "logs-*"\n')
        merged = self.api("demo").plugin_config().with_project("index")
        self.assertEqual(merged["index"], "logs-*")
        self.assertEqual(merged["mode"], "ask")

    def test_a_project_value_of_the_wrong_shape_is_refused(self):
        """The default a plugin passes is also the shape it declares."""
        self.layers(project='[plugins.demo]\npatterns = 5\n')
        cfg = self.api("demo").plugin_config()
        self.assertEqual(cfg.from_project("patterns", []), [])

    def test_a_project_list_of_the_wrong_element_type_is_refused(self):
        self.layers(project='[plugins.demo]\npatterns = [1, 2]\n')
        cfg = self.api("demo").plugin_config()
        self.assertEqual(cfg.from_project("patterns", []), [])

    def test_a_well_shaped_project_value_still_arrives(self):
        self.layers(project='[plugins.demo]\npatterns = ["*.pem"]\n')
        cfg = self.api("demo").plugin_config()
        self.assertEqual(cfg.from_project("patterns", []), ["*.pem"])

    def test_with_project_refuses_a_value_that_does_not_match_its_default(self):
        self.layers(project='[plugins.demo]\nindex = 5\n')
        merged = self.api("demo").plugin_config().with_project(index="logs-*")
        self.assertEqual(merged["index"], "logs-*")

    def test_with_project_refuses_a_value_that_does_not_match_the_user_layer(self):
        self.layers(user='[plugins.demo]\nindex = "mine-*"\n',
                    project='[plugins.demo]\nindex = 5\n')
        merged = self.api("demo").plugin_config().with_project("index")
        self.assertEqual(merged["index"], "mine-*")

    def test_a_refusal_is_recorded_rather_than_swallowed(self):
        self.layers(project='[plugins.demo]\npatterns = 5\n')
        cfg = self.api("demo").plugin_config()
        cfg.from_project("patterns", [])
        self.assertTrue(any("patterns" in message for message in cfg.refusals))

    def test_a_directly_injected_table_counts_as_the_user_layer(self):
        """Tests and embedders that write rt.cfg by hand are acting as the user, not a repo."""
        rt = self.runtime()
        rt.cfg["plugins"]["demo"] = {"mode": "yolo"}
        self.assertEqual(PluginAPI(rt, "demo", self.tmp).plugin_config().get("mode"), "yolo")


class RequiredPluginTests(LayeredConfigCase):
    """A plugin that provides a security control may say that skipping it is not an option."""

    def fragile(self, body: str) -> Path:
        root = self.tmp / "fragile"
        root.mkdir()
        (root / "plugin.toml").write_text('name = "fragile"\nentry = "fragile:register"\n')
        (root / "fragile.py").write_text(textwrap.dedent(body))
        return root

    def load(self, root: Path):
        rt = make_runtime(self.tmp, provider=ScriptedProvider([[text("ok")]]))
        return loader.load_all(rt, extra_paths=[str(root)])

    def test_a_plugin_that_did_not_declare_itself_required_is_still_skipped(self):
        root = self.fragile("""
            def register(api):
                raise RuntimeError("boom")
        """)
        report = self.load(root)
        self.assertEqual([entry[:2] for entry in report.skipped], [("fragile", "failed")])

    def test_a_required_plugin_that_fails_stops_the_session(self):
        root = self.fragile("""
            def register(api):
                api.declare_required("this plugin is the only thing guarding the shell")
                raise RuntimeError("boom")
        """)
        with self.assertRaises(loader.RequiredPluginFailed) as caught:
            self.load(root)
        self.assertIn("fragile", str(caught.exception))
        self.assertIn("guarding the shell", str(caught.exception))


class RequiredPluginFixture(LayeredConfigCase):
    """A plugin that declares itself required, installed where the user's own plugins live."""

    def user_plugins(self) -> Path:
        return self.tmp / "home" / "plugins"

    def write(self, parent: Path, *, required: str = "required = true\n",
              body: str = "def register(api):\n    pass\n") -> Path:
        root = parent / "guard"
        root.mkdir(parents=True)
        (root / "plugin.toml").write_text(
            'name = "guard"\nentry = "guard:register"\n'
            'required_reason = "the only thing guarding the shell"\n' + required)
        (root / "guard.py").write_text(textwrap.dedent(body))
        return root

    def approve(self, root: Path) -> None:
        loader.TrustStore(self.tmp / "home").trust(Manifest.load(root))

    def edit(self, root: Path) -> None:
        (root / "guard.py").write_text("def register(api):\n    pass  # not what was approved\n")

    def load(self, extra: list[str] | None = None):
        rt = make_runtime(self.tmp, provider=ScriptedProvider([[text("ok")]]))
        return loader.load_all(rt, extra_paths=extra or [])

class RequiredInTheManifestTests(RequiredPluginFixture):
    """`required = true` in plugin.toml: the same declaration, readable without running anything.

    `api.declare_required` is a statement made from inside `register()`, so it cannot cover the
    case it most needs to. A plugin the trust check refuses is skipped *before* `load_plugin`
    runs: `register()` never executes, the declaration never happens, and the session carries on
    with the control absent. A trusted credential-guard with one file edited left the built-in
    shell in place and said so on one line of stderr.
    """

    def test_a_required_plugin_replaced_since_approval_stops_the_session(self):
        root = self.write(self.user_plugins())
        self.approve(root)
        self.edit(root)
        with self.assertRaises(loader.RequiredPluginError) as caught:
            self.load()
        self.assertIn("guard", str(caught.exception))
        self.assertIn("guarding the shell", str(caught.exception))

    def test_the_refusal_says_how_to_get_the_session_back(self):
        root = self.write(self.user_plugins())
        self.approve(root)
        self.edit(root)
        with self.assertRaises(loader.RequiredPluginError) as caught:
            self.load()
        self.assertIn("plugin trust", str(caught.exception))

    def test_a_first_run_of_a_newly_added_required_plugin_is_not_fatal(self):
        """`new` is the ordinary path: install a plugin, run, approve it. Fatal there is a brick."""
        self.write(self.user_plugins())
        report = self.load()
        self.assertEqual([entry[:2] for entry in report.skipped], [("guard", "new")])

    def test_an_unapproved_required_plugin_is_still_announced_as_urgent(self):
        self.write(self.user_plugins())
        self.assertEqual([n.name for n in self.load().urgent()], ["guard"])

    def test_the_repositorys_own_required_plugin_cannot_stop_the_users_session(self):
        """`required` is a repository-controlled string; halting on it hands a repo a kill switch."""
        root = self.write(self.tmp / ".picoagent" / "plugins")
        self.approve(root)
        self.edit(root)
        report = self.load()
        self.assertEqual([entry[:2] for entry in report.skipped], [("guard", "changed")])

    def test_a_manifest_requirement_covers_a_register_that_raises(self):
        """One mechanism: the manifest field is read into the same declaration `declare_required` sets."""
        root = self.write(self.tmp / "cli", body="""
            def register(api):
                raise RuntimeError("boom")
        """)
        with self.assertRaises(loader.RequiredPluginFailed) as caught:
            self.load(extra=[str(root)])
        self.assertIn("guarding the shell", str(caught.exception))

    def test_a_required_plugin_the_user_approved_still_loads(self):
        root = self.write(self.user_plugins())
        self.approve(root)
        self.assertEqual([m.name for m in self.load().loaded], ["guard"])

    def test_a_plugin_that_says_nothing_is_still_skipped_when_it_changes(self):
        root = self.write(self.user_plugins(), required="")
        self.approve(root)
        self.edit(root)
        report = self.load()
        self.assertEqual([entry[:2] for entry in report.skipped], [("guard", "changed")])


class TheIdentityOfAnApproval(RequiredPluginFixture):
    """What a trust record is a record *of*, when the plugin's own name is under suspicion.

    Keyed by the name in `plugin.toml`, an approval is only as durable as a field the replacing
    code gets to rewrite. Change the name and the same directory reads as a plugin nobody has
    ever seen: `new` rather than `changed`, first-run chatter rather than "the code you approved
    has been replaced". Nobody needs local access for it; a `fast_forward` onto a rewritten
    upstream does it.
    """

    def rename(self, root: Path, name: str) -> None:
        """Replace the plugin in place: other code, under another name, same directory."""
        (root / "guard.py").unlink()
        (root / f"{name}.py").write_text("def register(api):\n    pass  # not what was approved\n")
        (root / "plugin.toml").write_text(f'name = "{name}"\nentry = "{name}:register"\n')

    def test_a_renamed_replacement_is_still_the_approved_plugin_having_changed(self):
        root = self.write(self.user_plugins())
        self.approve(root)
        self.rename(root, "guard2")
        self.assertEqual(loader.TrustStore(self.tmp / "home").status(Manifest.load(root)), "changed")

    def test_a_second_copy_of_a_plugin_is_a_separate_approval(self):
        """Two checkouts may share a name; approving one is not approving the other."""
        root = self.write(self.user_plugins())
        self.approve(root)
        other = self.write(self.tmp / ".picoagent" / "plugins")
        self.assertEqual(loader.TrustStore(self.tmp / "home").status(Manifest.load(other)), "new")
        self.assertEqual(loader.TrustStore(self.tmp / "home").status(Manifest.load(root)), "trusted")

    def test_an_approval_recorded_by_an_older_version_still_trusts_the_plugin(self):
        """Records written before the key changed name nothing else; they must keep working."""
        root = self.write(self.user_plugins())
        self.approve(root)
        self.as_an_older_version_wrote_it()
        self.assertEqual([m.name for m in self.load().loaded], ["guard"])

    def test_a_record_that_names_no_directory_is_taken_over_by_the_first_that_asks(self):
        """Left as a standing name match, one old record answers for every directory that claims
        the name - including a copy the user has never seen. Adopted, it answers for the directory
        it was found in, and the next one is the ordinary first-approval prompt."""
        root = self.write(self.user_plugins())
        self.approve(root)
        self.as_an_older_version_wrote_it()
        store = loader.TrustStore(self.tmp / "home")
        self.assertEqual(store.status(Manifest.load(root)), "trusted")
        other = self.write(self.tmp / ".picoagent" / "plugins")
        self.assertEqual(store.status(Manifest.load(other)), "new")

    def as_an_older_version_wrote_it(self) -> None:
        """Rewrite the store the way it was written before a record said which directory it covers."""
        path = self.tmp / "home" / "trust.json"
        data = json.loads(path.read_text())
        path.write_text(json.dumps(
            {record["name"]: {k: v for k, v in record.items() if k not in ("name", "root")}
             for record in data.values()}))


class ARecordedRequirementWithNothingBehindIt(RequiredPluginFixture):
    """A requirement the user approved, and a directory that no longer answers for it.

    `required` stops a session when approved code has been replaced. Deleting the code instead of
    replacing it reaches the same end by a quieter route: nothing loads, nothing is reported, and
    the control the user was relying on is absent from a session that looks exactly like one where
    it ran and found nothing. The plugin cannot declare itself when it is not there, so the store
    is the only party that still remembers it was meant to be.

    The stop this raises is only defensible because it can be undone without a session: `picoagent
    plugin untrust` builds a trust store and nothing else - no config-driven plugin load - so the
    record that is stopping the user can always be withdrawn. The refusal says so.
    """

    def test_a_required_plugin_that_vanished_stops_the_session(self):
        root = self.write(self.user_plugins())
        self.approve(root)
        shutil.rmtree(root)
        with self.assertRaises(loader.RequiredPluginError) as caught:
            self.load()
        self.assertIn("guarding the shell", str(caught.exception))

    def test_the_refusal_names_the_directory_the_plugin_was_approved_in(self):
        root = self.write(self.user_plugins())
        self.approve(root)
        shutil.rmtree(root)
        with self.assertRaises(loader.RequiredPluginError) as caught:
            self.load()
        self.assertIn(str(root), str(caught.exception))

    def test_the_refusal_names_the_one_command_that_clears_it(self):
        """A refusal that ends in "edit trust.json by hand" is a lockout with extra steps."""
        root = self.write(self.user_plugins())
        self.approve(root)
        shutil.rmtree(root)
        with self.assertRaises(loader.RequiredPluginError) as caught:
            self.load()
        self.assertIn("plugin untrust guard", str(caught.exception))

    def test_withdrawing_the_record_lets_the_session_start(self):
        """The recovery the refusal promises, driven end to end: the store the CLI writes through
        is the store the next session reads, and after the withdrawal there is nothing to raise."""
        root = self.write(self.user_plugins())
        self.approve(root)
        shutil.rmtree(root)
        store = loader.TrustStore(self.tmp / "home")
        store.withdraw(next(iter(store.data)))
        self.assertEqual(self.load().loaded, [])

    def test_a_required_plugin_whose_manifest_stopped_parsing_stops_the_session(self):
        """Not loading is the same absence as not being there, and the store knows both."""
        root = self.write(self.user_plugins())
        self.approve(root)
        (root / "plugin.toml").write_text("name = \n")
        with self.assertRaises(loader.RequiredPluginError):
            self.load()

    def test_a_plugin_that_never_declared_itself_required_may_vanish_quietly(self):
        root = self.write(self.user_plugins(), required="")
        self.approve(root)
        shutil.rmtree(root)
        self.assertEqual(self.load().loaded, [])

    def test_a_requirement_recorded_for_a_repositorys_copy_does_not_stop_the_session(self):
        """`required` in a repository's plugin.toml is not a switch that stops the user's work."""
        root = self.write(self.tmp / ".picoagent" / "plugins")
        self.approve(root)
        shutil.rmtree(root)
        self.assertEqual(self.load().loaded, [])


class AnImportThatFailsBeforeRegister(RequiredPluginFixture):
    """The case between a refused trust check and a `register()` that raises.

    `required` was read from the manifest after the entry module had already been imported, so
    an import that failed reached the generic catch and was reported as a routine skip: not
    urgent, "run with --verbose". A dependency uninstalled from the environment removes a
    security control exactly as thoroughly as editing its code does.
    """

    BROKEN = """
        import a_module_this_environment_does_not_have  # noqa: F401


        def register(api):
            pass
    """

    def test_a_required_plugin_that_cannot_be_imported_stops_the_session(self):
        root = self.write(self.tmp / "cli", body=self.BROKEN)
        with self.assertRaises(loader.RequiredPluginFailed) as caught:
            self.load(extra=[str(root)])
        self.assertIn("guarding the shell", str(caught.exception))

    def test_the_refusal_names_what_the_import_could_not_find(self):
        root = self.write(self.tmp / "cli", body=self.BROKEN)
        with self.assertRaises(loader.RequiredPluginFailed) as caught:
            self.load(extra=[str(root)])
        self.assertIn("a_module_this_environment_does_not_have", str(caught.exception))

    def test_a_dependency_that_disappeared_after_approval_stops_the_session(self):
        """The real shape of it: approved code, unchanged, in an environment that moved."""
        root = self.write(self.user_plugins(), body=self.BROKEN)
        self.approve(root)
        with self.assertRaises(loader.RequiredPluginFailed):
            self.load()

    def test_a_plugin_that_says_nothing_is_still_skipped_when_it_cannot_be_imported(self):
        root = self.write(self.tmp / "cli", required="", body=self.BROKEN)
        report = self.load(extra=[str(root)])
        self.assertEqual([entry[:2] for entry in report.skipped], [("guard", "failed")])


if __name__ == "__main__":
    unittest.main()
