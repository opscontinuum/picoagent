"""Which config layer a `[plugins].enabled` spec came from, and what it may touch.

`[plugins].enabled` is concatenated - the user's list, then the repository's - so a cloned
repository can suggest a plugin. Resolving a spec clones and `git checkout`s, which is a
*write*. If a repository's spec is allowed to write into the user's own plugin directory it
can move a plugin the user trusts onto a ref of the repository's choosing: the fingerprint
no longer matches, the plugin loads as `changed`, and the user's security plugin is off.

These run real git against local `file://` remotes, no network.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import CaptureFrontend  # noqa: F401  (puts picoagent on sys.path)
from picoagent.core.config import load_config
from picoagent.core.loop import Runtime
from picoagent.core.session import Session
from picoagent.plugins import loader
from picoagent.plugins.manifest import Manifest

GIT_ID = ["-c", "user.email=t@t", "-c", "user.name=t"]

PLUGIN_TOML = 'name = "gate"\nentry = "gate:register"\nversion = "1.0"\n'
ENTRY = 'MARK = "{mark}"\n\n\ndef register(api):\n    pass\n'


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *GIT_ID, *args],
                          capture_output=True, text=True).stdout.strip()


def make_plugin_repo(root: Path, name: str = "gate") -> Path:
    """A plugin repo with two refs: tag `v0` and tag `v2`, differing in one file."""
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    (root / "plugin.toml").write_text(PLUGIN_TOML.replace("gate", name))
    (root / f"{name}.py").write_text(ENTRY.format(mark="V0"))
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "v0")
    git(root, "tag", "v0")
    (root / f"{name}.py").write_text(ENTRY.format(mark="V2"))
    git(root, "commit", "-qam", "v2")
    git(root, "tag", "v2")
    return root


class ProjectSpecFixture(unittest.TestCase):
    """A user who trusts `gate` at `v2`, and a repository whose config names `gate` at `v0`."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = make_plugin_repo(self.tmp / "gate")
        self.home = self.tmp / "home"
        self.project = self.tmp / "project"
        (self.project / ".picoagent").mkdir(parents=True)
        self.home.mkdir()
        os.environ["PICOAGENT_HOME"] = str(self.home)
        (self.home / "config.toml").write_text(
            f'[plugins]\nenabled = ["file://{self.origin}@v2"]\n')
        (self.project / ".picoagent" / "config.toml").write_text(
            f'[plugins]\nenabled = ["file://{self.origin}@v0"]\n')

        cfg = load_config(self.project)
        self.user_checkout = loader.resolve_source(f"file://{self.origin}@v2", cfg)
        loader.TrustStore(self.home).trust(Manifest.load(self.user_checkout))

    def _load(self) -> loader.LoadReport:
        cfg = load_config(self.project)
        rt = Runtime(cfg, self.project, Session(self.project / "session.jsonl", self.project))
        rt.frontend = CaptureFrontend()
        return loader.load_all(rt)


class ProjectSpecVersusUserCheckout(ProjectSpecFixture):
    """The attack: one line of committed config disabling a plugin the user approved."""

    def test_the_users_checkout_stays_on_the_ref_the_user_chose(self):
        self._load()
        self.assertIn('MARK = "V2"', (self.user_checkout / "gate.py").read_text())

    def test_the_users_trusted_plugin_still_loads(self):
        report = self._load()
        self.assertIn("gate", [m.name for m in report.loaded])

    def test_the_users_plugin_is_still_trusted_afterwards(self):
        self._load()
        store = loader.TrustStore(self.home)
        self.assertEqual(store.status(Manifest.load(self.user_checkout)), "trusted")


class ShadowedRatherThanChanged(ProjectSpecFixture):
    """The repository's own copy is a different plugin directory, not a change to the user's."""

    def test_the_repository_copy_is_reported_as_shadowed(self):
        report = self._load()
        self.assertEqual([(name, reason) for name, reason, _ in report.skipped],
                         [("gate", "shadowed")])

    def test_no_false_alarm_that_the_users_plugin_changed(self):
        report = self._load()
        self.assertEqual(report.urgent(), [])

    def test_the_repository_copy_lands_in_the_repositorys_own_plugin_directory(self):
        report = self._load()
        self.assertEqual(report.skipped[0][2],
                         self.project / ".picoagent" / "plugins" / "gate")

    def test_the_notice_names_the_copy_that_is_running(self):
        report = self._load()
        self.assertIn(str(self.user_checkout), report.lines()[0])


class ARepositoryMaySuggestAPlugin(unittest.TestCase):
    """The feature the concatenation exists for. The trust prompt is its control."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = make_plugin_repo(self.tmp / "helper", name="helper")
        self.home = self.tmp / "home"
        self.project = self.tmp / "project"
        (self.project / ".picoagent").mkdir(parents=True)
        self.home.mkdir()
        os.environ["PICOAGENT_HOME"] = str(self.home)
        (self.project / ".picoagent" / "config.toml").write_text(
            f'[plugins]\nenabled = ["file://{self.origin}@v2"]\n')
        cfg = load_config(self.project)
        rt = Runtime(cfg, self.project, Session(self.project / "session.jsonl", self.project))
        rt.frontend = CaptureFrontend()
        self.report = loader.load_all(rt)

    def test_the_suggested_plugin_is_discovered(self):
        self.assertEqual([name for name, _, _ in self.report.skipped], ["helper"])

    def test_it_waits_for_the_trust_prompt(self):
        self.assertEqual([reason for _, reason, _ in self.report.skipped], ["new"])

    def test_it_is_cloned_where_the_repository_owns_it(self):
        self.assertTrue((self.project / ".picoagent" / "plugins" / "helper" / "plugin.toml").exists())

    def test_it_is_not_cloned_into_the_users_plugin_directory(self):
        self.assertFalse((self.home / "plugins" / "helper").exists())


class TheUpgradePathStillMovesACheckout(unittest.TestCase):
    """`picoagent plugin add` on an installed plugin is an upgrade: the user may move it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = make_plugin_repo(self.tmp / "gate")
        self.project = self.tmp / "project"
        self.project.mkdir()
        self.cfg = {"_cwd": str(self.project), "_user_dir": str(self.tmp / "home"),
                    "plugins": {"enabled": []}}

    def test_a_user_spec_moves_the_checkout_to_the_new_ref(self):
        checkout = loader.resolve_source(f"file://{self.origin}@v0", self.cfg)
        self.assertIn('MARK = "V0"', (checkout / "gate.py").read_text())
        loader.resolve_source(f"file://{self.origin}@v2", self.cfg)
        self.assertIn('MARK = "V2"', (checkout / "gate.py").read_text())

    def test_a_user_spec_installs_into_the_users_plugin_directory(self):
        checkout = loader.resolve_source(f"file://{self.origin}@v0", self.cfg)
        self.assertEqual(checkout, Path(self.cfg["_user_dir"]) / "plugins" / "gate")


class CheckoutOwnership(unittest.TestCase):
    """Where a spec's checkout is allowed to land."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.user_plugins = self.tmp / "home" / "plugins"
        self.user_plugins.mkdir(parents=True)

    def test_a_repository_spec_may_not_write_into_the_users_plugin_directory(self):
        with self.assertRaises(loader.PluginOwnershipError):
            loader.checkout_path(self.user_plugins, "file:///anywhere/gate", self.user_plugins)

    def test_a_symlinked_project_plugin_directory_does_not_get_there_either(self):
        link = self.tmp / "project" / ".picoagent" / "plugins"
        link.parent.mkdir(parents=True)
        link.symlink_to(self.user_plugins)
        with self.assertRaises(loader.PluginOwnershipError):
            loader.checkout_path(link, "file:///anywhere/gate", self.user_plugins)

    def test_a_traversing_checkout_name_is_refused(self):
        with self.assertRaises(loader.PluginOwnershipError):
            loader.checkout_path(self.user_plugins, "file:///anywhere/plugins/..")

    def test_an_ordinary_name_lands_directly_under_the_destination(self):
        self.assertEqual(loader.checkout_path(self.user_plugins, "file:///anywhere/gate.git"),
                         self.user_plugins / "gate")


class SpecProvenance(unittest.TestCase):
    """Which layer wrote which entry in the concatenated `enabled` list."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".picoagent").mkdir(parents=True)
        (self.tmp / ".picoagent" / "config.toml").write_text(
            '[plugins]\nenabled = ["./repo-one", "./repo-two"]\n')

    def _layers(self, enabled):
        cfg = {"_cwd": str(self.tmp), "plugins": {"enabled": enabled}}
        return loader.enabled_by_layer(cfg)

    def test_the_repositorys_specs_are_the_tail_of_the_list(self):
        self.assertEqual(self._layers(["./mine", "./repo-one", "./repo-two"]),
                         [("./mine", "user"), ("./repo-one", "project"), ("./repo-two", "project")])

    def test_a_config_assembled_by_hand_falls_back_to_membership(self):
        self.assertEqual(self._layers(["./repo-two", "./mine"]),
                         [("./repo-two", "project"), ("./mine", "user")])

    def test_a_repository_with_no_config_owns_nothing(self):
        (self.tmp / ".picoagent" / "config.toml").unlink()
        self.assertEqual(self._layers(["./mine"]), [("./mine", "user")])


class ProvenanceThatCannotBeEstablished(unittest.TestCase):
    """A config whose layers cannot be told apart must not hand out the user's privileges.

    `project_enabled` answered a missing `.picoagent/config.toml` and an unreadable one with the
    same empty list, and an empty project list attributes every spec to the *user* layer - the
    one allowed to write into `~/.picoagent/plugins`, with the off-limits check switched off.
    Those are two different questions: "the repository asked for nothing" is knowable, "nobody
    can read what the repository asked for" is not, and only the first is safe to act on.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".picoagent").mkdir(parents=True)
        self.config = self.tmp / ".picoagent" / "config.toml"
        self.spec = "git:evil.example/attacker/credential-guard@bad"

    def _layers(self, enabled=None):
        cfg = {"_cwd": str(self.tmp), "plugins": {"enabled": [self.spec] if enabled is None else enabled}}
        return loader.enabled_by_layer(cfg)

    def test_a_config_with_no_cwd_is_refused_rather_than_guessed_at(self):
        with self.assertRaises(loader.PluginProvenanceError):
            loader.enabled_by_layer({"plugins": {"enabled": [self.spec]}})

    def test_a_malformed_project_config_is_refused(self):
        self.config.write_text("[plugins\nenabled = [\n")
        with self.assertRaises(loader.PluginProvenanceError):
            self._layers()

    def test_a_project_config_that_cannot_be_opened_is_refused(self):
        self.config.mkdir()          # an OSError on open that needs no chmod and no non-root uid
        with self.assertRaises(loader.PluginProvenanceError):
            self._layers()

    def test_the_refusal_names_the_spec_it_would_not_place(self):
        self.config.write_text("[plugins\n")
        with self.assertRaises(loader.PluginProvenanceError) as caught:
            self._layers()
        self.assertIn(self.spec, str(caught.exception))

    def test_discovery_refuses_rather_than_resolving_an_unplaceable_spec(self):
        """The consequence: the spec never reaches `resolve_source`, so it never clones anywhere."""
        self.config.mkdir()
        cfg = {"_cwd": str(self.tmp), "_user_dir": str(self.tmp / "home"),
               "plugins": {"enabled": [self.spec]}}
        with self.assertRaises(loader.PluginProvenanceError):
            loader.discover(cfg, [])

    def test_nothing_enabled_is_nothing_to_attribute(self):
        self.config.write_text("[plugins\n")
        self.assertEqual(self._layers([]), [])

    def test_a_repository_that_wrote_no_config_is_still_answerable(self):
        self.assertEqual(self._layers(), [(self.spec, "user")])

    def test_a_config_without_a_plugins_table_is_still_answerable(self):
        self.config.write_text('model = "x"\n')
        self.assertEqual(self._layers(), [(self.spec, "user")])


class MovedVersusEdited(unittest.TestCase):
    """A trusted plugin that stops loading: did the user edit it, or did something move it?"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = make_plugin_repo(self.tmp / "gate")
        self.home = self.tmp / "home"
        self.project = self.tmp / "project"
        (self.project / ".picoagent").mkdir(parents=True)
        self.home.mkdir()
        os.environ["PICOAGENT_HOME"] = str(self.home)
        self._write_user_config("v2")
        cfg = load_config(self.project)
        self.checkout = loader.resolve_source(f"file://{self.origin}@v2", cfg)
        loader.TrustStore(self.home).trust(Manifest.load(self.checkout))

    def _write_user_config(self, ref: str) -> None:
        (self.home / "config.toml").write_text(f'[plugins]\nenabled = ["file://{self.origin}@{ref}"]\n')

    def _load(self) -> loader.LoadReport:
        cfg = load_config(self.project)
        rt = Runtime(cfg, self.project, Session(self.project / "session.jsonl", self.project))
        rt.frontend = CaptureFrontend()
        return loader.load_all(rt)

    def test_a_checkout_moved_under_the_user_says_so(self):
        self._write_user_config("v0")
        report = self._load()
        self.assertIn("MOVED", report.lines()[0])

    def test_a_checkout_moved_under_the_user_is_urgent(self):
        self._write_user_config("v0")
        self.assertEqual([n.name for n in self._load().urgent()], ["gate"])

    def test_a_moved_checkout_names_both_revisions(self):
        self._write_user_config("v0")
        approved = loader.TrustStore(self.home).approved_commit(Manifest.load(self.checkout))
        self.assertIn(approved[:12], self._load().lines()[0])

    def test_an_edit_by_hand_is_not_reported_as_a_move(self):
        (self.checkout / "gate.py").write_text('MARK = "mine"\n\n\ndef register(api):\n    pass\n')
        self.assertNotIn("MOVED", self._load().lines()[0])

    def test_an_edit_by_hand_is_still_urgent(self):
        (self.checkout / "gate.py").write_text('MARK = "mine"\n\n\ndef register(api):\n    pass\n')
        self.assertEqual([n.name for n in self._load().urgent()], ["gate"])


class ShadowedOnlyWhenTheUsersCopyIsRunning(ProjectSpecFixture):
    """The shadowed wording promises the user's copy is running. It has to be true."""

    def _break_the_users_copy(self) -> None:
        (self.user_checkout / "gate.py").write_text('MARK = "mine"\n\n\ndef register(api):\n    pass\n')

    def test_the_repository_copy_is_not_called_shadowed_when_nothing_shadows_it(self):
        self._break_the_users_copy()
        self.assertNotIn("shadowed", [reason for _, reason, _ in self._load().skipped])

    def test_the_repository_copy_is_not_reported_as_a_move_of_the_users_checkout(self):
        self._break_the_users_copy()
        project_copy = self.project / ".picoagent" / "plugins" / "gate"
        text = next(n.text for n in self._load().notices if n.root == project_copy)
        self.assertNotIn("MOVED", text)

    def test_a_repository_copy_the_user_never_approved_is_urgent(self):
        self._break_the_users_copy()
        self.assertEqual([n.name for n in self._load().urgent()], ["gate", "gate"])


if __name__ == "__main__":
    unittest.main()
