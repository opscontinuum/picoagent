"""Spec parsing, upgrade detection and fast-forwarding, against real local git repos.

`file://` and plain paths are remotes like any other as far as git is concerned, so these
exercise the real commands - ls-remote, fetch, merge --ff-only - with no network.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT  # noqa: F401  (puts picoagent on sys.path)
from picoagent.plugins import loader, upgrade

GIT_ID = ["-c", "user.email=t@t", "-c", "user.name=t"]


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *GIT_ID, *args],
                            capture_output=True, text=True)
    return result.stdout.strip()


def make_origin(tmp: Path, name: str = "origin") -> Path:
    root = tmp / name
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    (root / "version.txt").write_text("v1\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "v1")
    return root


class ParseSpecTests(unittest.TestCase):
    """The ref separator and the SSH user separator are both '@'."""

    def test_git_prefixed_https_spec(self):
        self.assertEqual(loader.parse_spec("git:github.com/o/r@main"),
                         ("https://github.com/o/r", "main"))

    def test_internal_https_host(self):
        self.assertEqual(loader.parse_spec("git:git.internal.corp/team/r@v1.2"),
                         ("https://git.internal.corp/team/r", "v1.2"))

    def test_ssh_spec_with_a_ref(self):
        """Splitting on the first '@' produced the url 'git' and made SSH specs unusable."""
        self.assertEqual(loader.parse_spec("git@git.internal.corp:team/r.git@main"),
                         ("git@git.internal.corp:team/r.git", "main"))

    def test_ssh_spec_without_a_ref(self):
        self.assertEqual(loader.parse_spec("git@git.internal.corp:team/r.git"),
                         ("git@git.internal.corp:team/r.git", ""))

    def test_explicit_https_url_is_left_alone(self):
        self.assertEqual(loader.parse_spec("https://git.internal.corp/t/r.git@main"),
                         ("https://git.internal.corp/t/r.git", "main"))

    def test_no_ref_means_the_remote_default(self):
        self.assertEqual(loader.parse_spec("git:github.com/o/r"), ("https://github.com/o/r", ""))

    def test_a_rewrite_redirects_to_an_internal_mirror(self):
        url, ref = loader.parse_spec("git:github.com/opscontinuum/picoagent-tools@main",
                                     {"github.com/opscontinuum": "git.internal.corp/mirrors"})
        self.assertEqual((url, ref), ("https://git.internal.corp/mirrors/picoagent-tools", "main"))

    def test_a_rewrite_that_does_not_match_changes_nothing(self):
        url, _ = loader.parse_spec("git:github.com/other/r@main",
                                   {"github.com/opscontinuum": "git.internal.corp/mirrors"})
        self.assertEqual(url, "https://github.com/other/r")

    def test_checkout_name_for_each_url_shape(self):
        for url, expected in [("https://github.com/o/repo", "repo"),
                              ("https://github.com/o/repo.git", "repo"),
                              ("git@host:team/repo.git", "repo")]:
            self.assertEqual(loader.checkout_name(url), expected, url)


class UpgradeDetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = make_origin(self.tmp)
        self.checkout = self.tmp / "checkout"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.checkout)], check=True)

    def _publish(self, text: str = "v2") -> None:
        (self.origin / "version.txt").write_text(text + "\n")
        git(self.origin, "commit", "-qam", text)

    def test_a_current_checkout_is_not_outdated(self):
        status = upgrade.check("p", str(self.origin), "main", self.checkout)
        self.assertIsNone(status.error)
        self.assertFalse(status.outdated)
        self.assertIn("up to date", status.describe())

    def test_a_new_upstream_commit_is_detected(self):
        self._publish()
        status = upgrade.check("p", str(self.origin), "main", self.checkout)
        self.assertTrue(status.outdated)

    def test_an_unreachable_remote_is_an_error_not_a_crash(self):
        status = upgrade.check("p", str(self.tmp / "does-not-exist"), "main", self.checkout)
        self.assertIsNotNone(status.error)
        self.assertFalse(status.outdated)

    def test_a_missing_checkout_reports_not_installed(self):
        status = upgrade.check("p", str(self.origin), "main", None)
        self.assertIn("not installed", status.describe())

    def test_upgrade_fast_forwards_the_checkout(self):
        """The bug this fixes: fetch + checkout left the tree on the old commit."""
        self._publish()
        status = upgrade.check("p", str(self.origin), "main", self.checkout)
        changed, message = upgrade.upgrade(status)
        self.assertTrue(changed, message)
        self.assertEqual((self.checkout / "version.txt").read_text().strip(), "v2")

    def test_upgrading_says_the_plugin_must_be_re_trusted(self):
        self._publish()
        _, message = upgrade.upgrade(upgrade.check("p", str(self.origin), "main", self.checkout))
        self.assertIn("plugin trust", message)

    def test_upgrade_refuses_a_dirty_checkout(self):
        self._publish()
        (self.checkout / "version.txt").write_text("local edit\n")
        changed, message = upgrade.upgrade(upgrade.check("p", str(self.origin), "main", self.checkout))
        self.assertFalse(changed)
        self.assertIn("uncommitted changes", message)
        self.assertEqual((self.checkout / "version.txt").read_text().strip(), "local edit")

    def test_upgrade_refuses_a_diverged_checkout(self):
        self._publish()
        (self.checkout / "other.txt").write_text("local commit\n")
        git(self.checkout, "add", "-A")
        git(self.checkout, "commit", "-q", "-m", "local")
        changed, message = upgrade.upgrade(upgrade.check("p", str(self.origin), "main", self.checkout))
        self.assertFalse(changed)
        self.assertIn("diverged", message)

    def test_upgrading_something_already_current_is_a_no_op(self):
        changed, message = upgrade.upgrade(upgrade.check("p", str(self.origin), "main", self.checkout))
        self.assertFalse(changed)
        self.assertIn("already up to date", message)


class ClonePathTests(unittest.TestCase):
    """resolve_source clones on first use and moves forward on later ones."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = make_origin(self.tmp)
        self.cfg = {"_cwd": str(self.tmp / "project"), "_user_dir": str(self.tmp / "home"),
                    "plugins": {"enabled": []}}
        (self.tmp / "project").mkdir()

    def test_first_use_clones_and_later_use_fast_forwards(self):
        spec = f"file://{self.origin}@main"
        dest = loader.resolve_source(spec, self.cfg)
        self.assertEqual((dest / "version.txt").read_text().strip(), "v1")

        (self.origin / "version.txt").write_text("v2\n")
        git(self.origin, "commit", "-qam", "v2")

        dest_again = loader.resolve_source(spec, self.cfg)
        self.assertEqual(dest_again, dest)
        self.assertEqual((dest / "version.txt").read_text().strip(), "v2",
                         "a second resolve must move the checkout forward")

    def test_a_local_edit_is_not_overwritten_by_a_later_resolve(self):
        spec = f"file://{self.origin}@main"
        dest = loader.resolve_source(spec, self.cfg)
        (dest / "version.txt").write_text("mine\n")
        (self.origin / "version.txt").write_text("v2\n")
        git(self.origin, "commit", "-qam", "v2")
        loader.resolve_source(spec, self.cfg)
        self.assertEqual((dest / "version.txt").read_text().strip(), "mine")


class CheckPluginsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = make_origin(self.tmp)
        self.cfg = {"_cwd": str(self.tmp), "_user_dir": str(self.tmp / "home"),
                    "plugins": {"enabled": [f"file://{self.origin}@main"]}}

    def test_local_path_plugins_are_skipped(self):
        self.cfg["plugins"]["enabled"] = ["./some/local/plugin"]
        self.assertEqual(upgrade.check_plugins(self.cfg), [])

    def test_a_configured_git_plugin_is_reported(self):
        statuses = upgrade.check_plugins(self.cfg)
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0].name, "origin")

    def test_the_app_is_not_checked_without_config(self):
        self.assertIsNone(upgrade.check_app(self.cfg))


if __name__ == "__main__":
    unittest.main()
