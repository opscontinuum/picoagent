"""Which config layer a `[plugins].enabled` spec came from, and what it may touch.

`[plugins].enabled` is concatenated - the user's list, then the repository's - so a cloned
repository can suggest a plugin. Resolving a spec clones and `git checkout`s, which is a
*write*. If a repository's spec is allowed to write into the user's own plugin directory it
can move a plugin the user trusts onto a ref of the repository's choosing: the fingerprint
no longer matches, the plugin loads as `changed`, and the user's security plugin is off.

These run real git against local `file://` remotes, no network.
"""
from __future__ import annotations

import argparse
import io
import os
import subprocess
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from helpers import CaptureFrontend, temp_dir  # noqa: F401  (puts picoagent on sys.path)
from picoagent import cli
from picoagent.core.config import PROJECT_ENABLED_KEY, load_config
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
        self.tmp = temp_dir()
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
        self.tmp = temp_dir()
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
        self.tmp = temp_dir()
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
        self.tmp = temp_dir()
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
        self.tmp = temp_dir()
        (self.tmp / ".picoagent").mkdir(parents=True)
        os.environ["PICOAGENT_HOME"] = str(self.tmp / "home")
        (self.tmp / ".picoagent" / "config.toml").write_text(
            '[plugins]\nenabled = ["./repo-one", "./repo-two"]\n')

    def _layers(self, enabled):
        """A real config, with the merged list swapped for the one under test.

        Built by `load_config` because that is where the repository's own list is recorded;
        the loader reads it off the config rather than opening the file a second time.
        """
        cfg = load_config(self.tmp)
        cfg["plugins"]["enabled"] = enabled
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

    def test_a_repository_config_without_a_plugins_table_owns_nothing(self):
        (self.tmp / ".picoagent" / "config.toml").write_text('model = "x"\n')
        self.assertEqual(self._layers(["./mine"]), [("./mine", "user")])


class ProvenanceThatCannotBeEstablished(unittest.TestCase):
    """A config whose layers cannot be told apart must not hand out the user's privileges.

    An empty project list attributes every spec to the *user* layer - the one allowed to write
    into `~/.picoagent/plugins`, with the off-limits check switched off - so "the repository
    asked for nothing" and "nobody can say what the repository asked for" must not arrive as the
    same answer. The first is knowable and is recorded by the read that built the list. The
    second is a config that never came from `load_config` and carries no record of the layer:
    every case below is one of those, assembled by hand the way an embedder would.

    A repository's config that will not parse is deliberately *not* one of them: `load_config`
    drops such a file, so the list holds the user's specs and nothing else, and refusing to place
    them would let one broken committed file end the session of every user with a plugin of their
    own. See `ARepositoryConfigThatWillNotParse`.
    """

    def setUp(self):
        self.tmp = temp_dir()
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

    def test_a_config_that_carries_the_record_is_answerable_without_a_cwd(self):
        """The escape hatch the refusal names: an embedder says what the repository asked for
        and gets the same attribution the shipped path gets."""
        self.assertEqual(loader.enabled_by_layer(
            {PROJECT_ENABLED_KEY: [], "plugins": {"enabled": [self.spec]}}), [(self.spec, "user")])

    def test_a_record_naming_the_spec_puts_it_in_the_repositorys_layer(self):
        self.assertEqual(loader.enabled_by_layer(
            {PROJECT_ENABLED_KEY: [self.spec], "plugins": {"enabled": [self.spec]}}),
            [(self.spec, "project")])


class MovedVersusEdited(unittest.TestCase):
    """A trusted plugin that stops loading: did the user edit it, or did something move it?"""

    def setUp(self):
        self.tmp = temp_dir()
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

    def test_the_users_own_copy_is_the_urgent_one(self):
        """The plugin that stopped running is the one the user was relying on."""
        self._break_the_users_copy()
        self.assertEqual([(n.name, n.root) for n in self._load().urgent()],
                         [("gate", self.user_checkout)])

    def test_the_repository_copy_is_announced_as_one_the_user_never_approved(self):
        """An approval covers a directory, and this is not the directory the user approved.

        It shares a name with their plugin and nothing else. Reporting it as "not the version
        you approved" credits a repository's own checkout with an approval it never had, and
        buries the notice that matters - their copy, at their path, not running - under a
        second alarm about a copy that was never going to run in the first place.
        """
        self._break_the_users_copy()
        project_copy = self.project / ".picoagent" / "plugins" / "gate"
        notice = next(n for n in self._load().notices if n.root == project_copy)
        self.assertEqual(notice.reason, "new")


class ASpecThatIsNotAString(unittest.TestCase):
    """One committed line of the wrong type must not be the end of every session in a project.

    The two lists stop lining up: `project_enabled` drops non-strings, `load_config` keeps them.
    So the membership fallback attributed the stray value to the *user* layer - the layer that
    may write into `~/.picoagent/plugins` - and `discover` then handed it to `resolve_source`,
    where matching a bool against the git-spec pattern raises a `TypeError` its catch list does
    not name. `enabled = [true]` in a cloned repository killed every session in it.

    A spec is a string that names a plugin. A value that is not one names nothing, so there is
    nothing to resolve and nothing to attribute: it is a malformed config, which is an expected
    failure, and the specs around it are still placed.
    """

    def setUp(self):
        self.tmp = temp_dir()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.project = self.tmp / "project"
        (self.project / ".picoagent").mkdir(parents=True)
        os.environ["PICOAGENT_HOME"] = str(self.home)
        self._write_project('enabled = [true]\n')

    def _write_project(self, body: str) -> None:
        (self.project / ".picoagent" / "config.toml").write_text(f"[plugins]\n{body}")

    def test_a_session_in_that_repository_still_starts(self):
        cfg = load_config(self.project)
        rt = Runtime(cfg, self.project, Session(self.project / "session.jsonl", self.project))
        rt.frontend = CaptureFrontend()
        self.assertEqual(loader.load_all(rt).loaded, [])

    def test_the_value_never_reaches_the_resolver(self):
        """Resolving is a clone and a checkout, so nothing unplaceable may get that far."""
        self.assertEqual(loader.discover(load_config(self.project), []), [])

    def test_a_stray_value_is_not_attributed_to_a_layer_at_all(self):
        self.assertEqual(loader.enabled_by_layer(load_config(self.project)), [])

    def test_the_specs_around_it_are_still_placed(self):
        (self.home / "config.toml").write_text('[plugins]\nenabled = ["./mine"]\n')
        self._write_project('enabled = [true, "./theirs"]\n')
        self.assertEqual(loader.enabled_by_layer(load_config(self.project)),
                         [("./mine", "user"), ("./theirs", "project")])


class ARepositoryConfigThatWillNotParse(unittest.TestCase):
    """A repository must not be able to end a session by shipping a config nobody can read.

    `core.config` reads that file behind a catch broad enough for a hostile one, and drops it
    with a notice: the settings are lost, the session runs on the user's own. The loader then
    opened the *same* file again to ask which specs the repository had contributed, behind a
    narrower catch, and undid that from the other side. A file that is not UTF-8 or is nested
    past the parser's stack raised through `discover` and ended the session with a traceback;
    one that merely failed to parse was caught, reported as "layer unknown", and refused the
    session outright as soon as the user had any plugin of their own.

    Which specs the repository contributed is not a fact to go back to the file for. The
    concatenated list was built from one read, and that read is the only one that can answer
    for it: if it failed, nothing of the repository's is in the list, so every spec in it is
    the user's own. Anything else is a second reader answering about bytes the first one
    never saw.
    """

    def setUp(self):
        self.tmp = temp_dir()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.project = self.tmp / "project"
        (self.project / ".picoagent").mkdir(parents=True)
        os.environ["PICOAGENT_HOME"] = str(self.home)
        (self.home / "config.toml").write_text('[plugins]\nenabled = ["./mine"]\n')
        self.config = self.project / ".picoagent" / "config.toml"

    def _layers(self) -> list[tuple[str, str]]:
        return loader.enabled_by_layer(load_config(self.project))

    def _load(self) -> loader.LoadReport:
        cfg = load_config(self.project)
        rt = Runtime(cfg, self.project, Session(self.project / "session.jsonl", self.project))
        rt.frontend = CaptureFrontend()
        return loader.load_all(rt)

    def test_a_config_that_is_not_utf8_does_not_end_the_session(self):
        """What a Windows editor writes when somebody re-saves the file in cp1252."""
        self.config.write_bytes(b'[plugins]\nenabled = ["caf\xe9"]\n')
        self.assertEqual(self._load().loaded, [])

    def test_a_config_nested_past_the_parsers_stack_does_not_end_the_session(self):
        self.config.write_text("v = " + "[" * 20000)
        self.assertEqual(self._load().loaded, [])

    def test_a_config_that_will_not_parse_does_not_refuse_the_users_own_plugins(self):
        """The expensive half: one broken file in a cloned repository stopped the tool for
        every user who had a plugin of their own, and told them their own spec had no layer."""
        self.config.write_text("[plugins\nenabled = [\n")
        self.assertEqual(self._layers(), [("./mine", "user")])

    def test_a_session_in_such_a_repository_still_starts(self):
        self.config.write_text("[plugins\nenabled = [\n")
        self.assertEqual(self._load().loaded, [])

    def test_the_layers_are_read_off_the_config_not_the_file_again(self):
        """The file may change between the two reads, and a second reader answers for bytes
        that never entered the list it is describing."""
        self.config.write_text('[plugins]\nenabled = ["./theirs"]\n')
        cfg = load_config(self.project)
        self.config.write_text('[plugins]\nenabled = ["./mine"]\n')
        self.assertEqual(loader.enabled_by_layer(cfg),
                         [("./mine", "user"), ("./theirs", "project")])


class ARepositoryConfigOfTheWrongShape(unittest.TestCase):
    """A config that parses and says something absurd is the same fault as one that will not.

    `plugins = 5` is valid TOML. It arrived at `project_cfg["plugins"].get("enabled")` as an
    `int` and raised `AttributeError` out of `load_config` itself - before the loader, before the
    first prompt, in a file the user did not write. The shape of a repository's config is the
    repository's to choose, so reading it has to be total: what it asked for is nothing.
    """

    def setUp(self):
        self.tmp = temp_dir()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.project = self.tmp / "project"
        (self.project / ".picoagent").mkdir(parents=True)
        os.environ["PICOAGENT_HOME"] = str(self.home)
        (self.home / "config.toml").write_text('[plugins]\nenabled = ["./mine"]\n')

    def _write(self, body: str) -> None:
        (self.project / ".picoagent" / "config.toml").write_text(body)

    def test_a_scalar_where_the_plugins_table_goes_does_not_end_the_session(self):
        self._write("plugins = 5\n")
        self.assertEqual(load_config(self.project)["plugins"]["enabled"], ["./mine"])

    def test_a_scalar_where_the_enabled_list_goes_asks_for_nothing(self):
        self._write('[plugins]\nenabled = "gate"\n')
        self.assertEqual(loader.enabled_by_layer(load_config(self.project)), [("./mine", "user")])


class APathSpecFromARepository(unittest.TestCase):
    """`enabled = ["/home/you/.picoagent/plugins/gate"]`, committed to a repository.

    A git spec from the repository's layer may not resolve into the user's plugin directory,
    because resolving one is a write. A path spec is not a write, and was left unchecked - but
    resolving one still *tags* the directory it names with the layer that asked for it, and
    `discover` dedups on the directory, so the repository's claim arrived first and the honest
    user-layer entry for the same directory was dropped.

    The tag is not cosmetic. `_stops_the_session` refuses to stop for a `required` plugin at
    the project layer, on the grounds that `required` is a line a cloned repository also gets
    to write - so a repository could switch off the stop protecting the user's own plugin by
    naming its directory. The wording goes the same way: the user is told their own edited
    plugin is "offered by this repository", with the repository's path in the sentence.

    So the ownership rule that already covers git specs covers path specs too: a spec from the
    repository's layer may not name a directory the user's plugin directory owns.
    """

    def setUp(self):
        self.tmp = temp_dir()
        self.home = self.tmp / "home"
        self.project = self.tmp / "project"
        (self.project / ".picoagent").mkdir(parents=True)
        (self.home / "plugins").mkdir(parents=True)
        os.environ["PICOAGENT_HOME"] = str(self.home)

    def _install(self, name: str = "gate", required: bool = False) -> Path:
        """A plugin in the user's own plugin directory, approved by the user."""
        root = self.home / "plugins" / name
        root.mkdir()
        (root / "plugin.toml").write_text(
            f'name = "{name}"\nentry = "{name}:register"\nversion = "1.0"\n'
            + ('required = true\nrequired_reason = "it is the only check on the shell tool"\n'
               if required else ""))
        (root / f"{name}.py").write_text("MARK = 'approved'\n\n\ndef register(api):\n    pass\n")
        loader.TrustStore(self.home).trust(Manifest.load(root))
        return root

    def _claim(self, spec: str) -> None:
        (self.project / ".picoagent" / "config.toml").write_text(f'[plugins]\nenabled = ["{spec}"]\n')

    def _edit(self, root: Path) -> None:
        """What makes a plugin `changed`: the user editing their own copy."""
        (root / f"{root.name}.py").write_text("MARK = 'edited'\n\n\ndef register(api):\n    pass\n")

    def _load(self) -> loader.LoadReport:
        cfg = load_config(self.project)
        rt = Runtime(cfg, self.project, Session(self.project / "session.jsonl", self.project))
        rt.frontend = CaptureFrontend()
        return loader.load_all(rt)

    def test_the_users_directory_is_discovered_as_the_users(self):
        root = self._install()
        self._claim(str(root))
        found = [entry for entry in loader.discover(load_config(self.project), []) if entry.root == root]
        self.assertEqual([entry.layer for entry in found], [loader.USER])

    def test_resolving_such_a_spec_from_the_repository_is_refused(self):
        root = self._install()
        self._claim(str(root))
        with self.assertRaises(loader.PluginOwnershipError):
            loader.resolve_source(str(root), load_config(self.project), project=True)

    def test_a_relative_spelling_of_the_same_directory_is_refused_too(self):
        """Compared after resolving, like the git rule, so `../` and a symlink are the same
        claim written differently."""
        root = self._install()
        spec = os.path.relpath(root, self.project)
        with self.assertRaises(loader.PluginOwnershipError):
            loader.resolve_source(spec, load_config(self.project), project=True)

    def test_the_users_own_spec_for_their_own_directory_still_resolves(self):
        root = self._install()
        self.assertEqual(loader.resolve_source(str(root), load_config(self.project)), root)

    def test_a_repository_may_still_name_a_directory_of_its_own(self):
        (self.project / ".picoagent" / "plugins").mkdir()
        theirs = self.project / ".picoagent" / "plugins" / "theirs"
        theirs.mkdir()
        self.assertEqual(loader.resolve_source(str(theirs), load_config(self.project), project=True),
                         theirs)

    def test_a_changed_required_plugin_of_the_users_still_stops_the_session(self):
        """The enforcement the tag gates, and it is the *right* refusal that is asserted.

        `_stops_the_session` refuses to stop at the project layer, so the claim disarmed this
        check; what caught the plugin instead was the recorded-requirement sweep at the end of
        the load, which is written for a plugin that is *gone*. The user who edited their own
        plugin was told nothing loaded from that directory and to put it back, when what they
        needed was `plugin trust`. A stop reached by the wrong route says the wrong thing.
        """
        root = self._install(required=True)
        self._claim(str(root))
        self._edit(root)
        with self.assertRaises(loader.RequiredPluginUntrusted):
            self._load()

    def test_the_user_is_not_told_their_own_plugin_came_from_the_repository(self):
        """A security notice whose attribution the attacker chose is worse than no notice."""
        root = self._install()
        self._claim(str(root))
        self._edit(root)
        notice = next(entry for entry in self._load().notices if entry.root == root)
        self.assertNotIn("this repository", notice.text)


class RefusingAnAddOnTheCommandLine(unittest.TestCase):
    """The ownership refusal reaching the person who typed the command.

    ``resolve_source`` is the check; this is what the user sees when it fires. ``plugin add``
    caught only ``CalledProcessError`` around it, so both spellings that raise
    ``PluginOwnershipError`` - a ``--project`` path aimed inside the user's own plugin directory,
    and a git spec whose checkout name would land there or traverse out of the destination - came
    back as an uncaught traceback. A traceback is not a refusal: it says picoagent broke rather
    than that the command was declined, and it buries the one sentence explaining where a
    repository's plugins are allowed to live.
    """

    def setUp(self):
        self.tmp = temp_dir()
        self.home = self.tmp / "home"
        self.project = self.tmp / "project"
        self.project.mkdir(parents=True)
        self.user_plugin = self.home / "plugins" / "gate"
        self.user_plugin.mkdir(parents=True)
        (self.user_plugin / "plugin.toml").write_text(PLUGIN_TOML)
        (self.user_plugin / "gate.py").write_text(ENTRY.format(mark="V0"))
        self._old_home = os.environ.get("PICOAGENT_HOME")
        self._old_cwd = Path.cwd()
        os.environ["PICOAGENT_HOME"] = str(self.home)
        os.chdir(self.project)

    def tearDown(self):
        os.chdir(self._old_cwd)
        if self._old_home is None:
            os.environ.pop("PICOAGENT_HOME", None)
        else:
            os.environ["PICOAGENT_HOME"] = self._old_home

    def _add(self, spec: str, project: bool = True) -> tuple[int, str]:
        args = argparse.Namespace(pcmd="add", spec=spec, project=project, yes=True)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = cli.plugin_command(args)
        return code, buffer.getvalue()

    def test_a_project_path_inside_the_users_plugin_directory_is_refused_not_raised(self):
        code, out = self._add(str(self.user_plugin))
        self.assertEqual(code, 1)
        self.assertIn(str(self.user_plugin), out)

    def test_the_refusal_says_where_a_repositorys_plugins_belong(self):
        """The user has to be able to act on it, and the action is a different directory."""
        self.assertIn(".picoagent/plugins", self._add(str(self.user_plugin))[1])

    def test_a_relative_spelling_of_the_same_directory_is_refused_the_same_way(self):
        spec = os.path.relpath(self.user_plugin, self.project)
        self.assertEqual(self._add(spec)[0], 1)

    def test_a_git_spec_with_a_traversing_checkout_name_is_refused_too(self):
        """``checkout_path`` raises this one whether or not ``--project`` was passed, so the
        catch cannot be conditional on the flag either."""
        code, out = self._add("file:///anywhere/plugins/..", project=False)
        self.assertEqual(code, 1)
        self.assertIn("refusing", out)


if __name__ == "__main__":
    unittest.main()
