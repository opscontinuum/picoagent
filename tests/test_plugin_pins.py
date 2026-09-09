"""A site can require a plugin to match a hash it published, or a signature, before it installs.

STIG V-222513 (APSC-DV-001430) asks whether the application *has the capability* to prevent an
application component being installed without verification, and offers one fallback: a
cryptographic hash value an administrator can verify prior to installation. picoagent's trust
fingerprint is not that. It is computed from the bytes that arrived, so it attests that the user
clicked yes on those bytes - never that they are the bytes anyone published. Nothing in it can
disagree with a compromised host, because the host chose both the code and the hash.

A pin is the missing half: an expected value the *administrator* supplies out of band, written
in ``~/.picoagent/plugin-pins.toml``, which the fetch has to meet. What is under test here is
therefore the refusal, at greater length than the acceptance:

* the pin that does not match refuses, and no approval is recorded;
* a plugin with no pin at all refuses, because a policy you can dodge by not being listed
  in it is not a policy;
* a pin file that cannot be parsed refuses everything rather than nothing;
* a required signature refuses when the checkout is unsigned, *and* refuses when the tooling
  that would check it is not installed - the case that decides whether this is a check or a
  decoration;
* a repository's own copy of the file decides nothing.

These live in one file rather than beside the trust-store tests because they state a different
property. ``test_loop_and_plugins`` asks what happens to code the user approved; this asks what
may be approved at all.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from helpers import ROOT, temp_dir  # noqa: F401  (puts picoagent on sys.path)
from picoagent import cli
from picoagent.core.config import load_config
from picoagent.plugins import loader, pins

GIT_ID = ["-c", "user.email=t@t", "-c", "user.name=t"]

PLUGIN_TOML = """\
name = "pinned-probe"
version = "0.1.0"
entry = "pinned_probe:register"
description = "a plugin the site pins"
"""


def git(root: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *GIT_ID, *args],
                          capture_output=True, text=True, env=env)


def make_plugin_origin(tmp: Path, name: str = "origin") -> Path:
    """A git repository holding a one-file plugin, usable as a ``file://`` remote."""
    root = tmp / name
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    (root / "plugin.toml").write_text(PLUGIN_TOML)
    (root / "pinned_probe.py").write_text("def register(api):\n    pass\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "v1")
    return root


class PinFixture(unittest.TestCase):
    """A user home, a project, and a plugin repository to fetch from."""

    def setUp(self):
        self.tmp = temp_dir()
        self.home = self.tmp / "home"
        self.home.mkdir(parents=True)
        self.project = self.tmp / "project"
        self.project.mkdir(parents=True)
        self.origin = make_plugin_origin(self.tmp)
        self.spec = f"file://{self.origin}"

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

    def write_pins(self, text: str, *, project: bool = False) -> Path:
        base = self.project / ".picoagent" if project else self.home
        base.mkdir(parents=True, exist_ok=True)
        path = base / pins.PINS_FILENAME
        path.write_text(text)
        return path

    def cfg(self) -> dict:
        return load_config(self.project)

    def expected_fingerprint(self) -> str:
        """What a pin for the origin must say, worked out from a throwaway clone."""
        scratch = self.tmp / "scratch"
        subprocess.run(["git", "clone", "-q", self.spec, str(scratch)], check=True)
        digest = loader.pin_digest(scratch)
        shutil.rmtree(scratch)
        return digest


class NoPinFileChangesNothing(PinFixture):
    """The control is a capability a site turns on, so an install with no policy behaves as before."""

    def test_a_git_spec_resolves_when_no_policy_file_exists(self):
        root = loader.resolve_source(self.spec, self.cfg())
        self.assertTrue((root / "plugin.toml").exists())

    def test_the_policy_reads_as_absent(self):
        self.assertIsNone(pins.Policy.load(self.home))


class AFingerprintPinIsEnforced(PinFixture):
    def test_a_matching_pin_installs(self):
        self.write_pins(f'["{self.origin}"]\nsha256 = "{self.expected_fingerprint()}"\n')
        root = loader.resolve_source(self.spec, self.cfg())
        self.assertTrue((root / "plugin.toml").exists())

    def test_a_mismatched_pin_refuses(self):
        self.write_pins(f'["{self.origin}"]\nsha256 = "{"0" * 64}"\n')
        with self.assertRaises(loader.PluginVerificationError):
            loader.resolve_source(self.spec, self.cfg())

    def test_the_refusal_names_both_hashes(self):
        """A user who cannot see what was expected and what arrived cannot act on the refusal."""
        expected = self.expected_fingerprint()
        self.write_pins(f'["{self.origin}"]\nsha256 = "{"0" * 64}"\n')
        with self.assertRaises(loader.PluginVerificationError) as caught:
            loader.resolve_source(self.spec, self.cfg())
        self.assertIn("0" * 64, str(caught.exception))
        self.assertIn(expected, str(caught.exception))


class AnUnlistedPluginIsRefusedByDefault(PinFixture):
    """A policy that only binds the plugins it happens to name is not a policy."""

    def test_a_spec_with_no_entry_refuses(self):
        self.write_pins('["https://example.invalid/other"]\nsha256 = "%s"\n' % ("1" * 64))
        with self.assertRaises(loader.PluginVerificationError):
            loader.resolve_source(self.spec, self.cfg())

    def test_the_site_can_say_unpinned_plugins_are_allowed(self):
        self.write_pins('unpinned = "allow"\n["https://example.invalid/other"]\nsha256 = "%s"\n'
                        % ("1" * 64))
        root = loader.resolve_source(self.spec, self.cfg())
        self.assertTrue((root / "plugin.toml").exists())


class AnUnreadablePolicyRefuses(PinFixture):
    """The trust store reads a damaged file as *nothing approved*; a damaged policy is the same
    direction - a policy nobody can parse permits nothing, never everything."""

    def test_a_pin_file_that_is_not_toml_refuses_every_plugin(self):
        self.write_pins("this is not toml = = =\n")
        with self.assertRaises(loader.PluginVerificationError):
            loader.resolve_source(self.spec, self.cfg())


class OnlyTheUsersCopyOfThePolicyCounts(PinFixture):
    """A cloned repository must not be able to write the rule that admits its own plugins."""

    def test_a_repositorys_pin_file_grants_nothing(self):
        self.write_pins('unpinned = "allow"\n', project=True)
        self.write_pins("")          # user policy: present, empty, so nothing is pinned
        with self.assertRaises(loader.PluginVerificationError):
            loader.resolve_source(self.spec, self.cfg())


class ASignaturePinIsEnforced(PinFixture):
    def test_an_unsigned_checkout_refuses(self):
        self.write_pins(f'["{self.origin}"]\nsigned_by = ["ABCDEF0123456789"]\n')
        with self.assertRaises(loader.PluginVerificationError) as caught:
            loader.resolve_source(self.spec, self.cfg())
        self.assertIn("signature", str(caught.exception).lower())

    def test_a_signature_from_a_key_the_site_did_not_name_refuses(self):
        if _sign_head(self.tmp, self.origin) is None:
            self.skipTest("gpg could not generate a key in this environment")
        self.write_pins(f'["{self.origin}"]\nsigned_by = ["ABCDEF0123456789"]\n')
        with mock.patch.dict(os.environ, {"GNUPGHOME": str(self.tmp / "gnupg")}):
            with self.assertRaises(loader.PluginVerificationError):
                loader.resolve_source(self.spec, self.cfg())

    def test_a_short_key_id_is_refused_rather_than_matched_loosely(self):
        """A 32-bit short id has been collidable on ordinary hardware since 2019, and this
        matches a configured name as a suffix of the fingerprint git reports - so an eight
        character entry would name a key an attacker can manufacture."""
        self.write_pins(f'["{self.origin}"]\nsigned_by = ["0123456789"]\n')
        with self.assertRaises(loader.PluginVerificationError) as caught:
            loader.resolve_source(self.spec, self.cfg())
        self.assertIn("not specific enough", str(caught.exception))

    def test_a_signed_by_that_is_not_a_list_refuses(self):
        """Iterating the string "A1B2" yields four one-character signers, and every real
        fingerprint ends with one of them - so a mistyped line would accept any signature."""
        self.write_pins(f'["{self.origin}"]\nsigned_by = "ABCDEF0123456789"\n')
        with self.assertRaises(loader.PluginVerificationError) as caught:
            loader.resolve_source(self.spec, self.cfg())
        self.assertIn("not a list", str(caught.exception))

    def test_a_signature_from_a_named_key_installs(self):
        signer = _sign_head(self.tmp, self.origin)
        if signer is None:
            self.skipTest("gpg could not generate a key in this environment")
        self.write_pins(f'["{self.origin}"]\nsigned_by = ["{signer}"]\n')
        with mock.patch.dict(os.environ, {"GNUPGHOME": str(self.tmp / "gnupg")}):
            root = loader.resolve_source(self.spec, self.cfg())
        self.assertTrue((root / "plugin.toml").exists())


class MissingVerificationToolingRefuses(PinFixture):
    """The case that decides whether this is a check. A signature requirement that passes when
    nothing on the machine can check signatures is worse than no requirement at all: it reports
    a guarantee it never obtained."""

    def test_a_signature_requirement_refuses_when_git_cannot_be_run(self):
        self.write_pins(f'["{self.origin}"]\nsigned_by = ["ABCDEF0123456789"]\n')
        checkout = loader.plugins_dir(self.cfg()) / "origin"
        checkout.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", self.spec, str(checkout)], check=True)
        with mock.patch.object(pins.subprocess, "run", side_effect=FileNotFoundError("git")):
            refusal = pins.Policy.load(self.home).refusal(
                [str(self.origin)], checkout, loader.pin_digest(checkout))
        self.assertIsNotNone(refusal, "an absent verifier must refuse, never pass")

    def test_the_refusal_says_the_tooling_is_what_is_missing(self):
        """Otherwise an administrator debugs the publisher's signature instead of their own box."""
        self.write_pins(f'["{self.origin}"]\nsigned_by = ["ABCDEF0123456789"]\n')
        checkout = loader.plugins_dir(self.cfg()) / "origin"
        checkout.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", self.spec, str(checkout)], check=True)
        with mock.patch.object(pins.subprocess, "run", side_effect=FileNotFoundError("git")):
            refusal = pins.Policy.load(self.home).refusal(
                [str(self.origin)], checkout, loader.pin_digest(checkout))
        self.assertIn("git is not installed", refusal)


class ThePinDigestSeesWhatTheTrustFingerprintCannot(unittest.TestCase):
    """``directory_fingerprint`` concatenates file contents and hashes nothing else, so three
    materially different directories share one digest: a line moved from the end of one file to
    the start of the next, a file renamed, an empty file added. That is tolerable for the
    question the trust store asks - did the bytes I approved change - and not tolerable for a
    value a publisher writes down and an administrator pins against, which is why the pin has a
    digest of its own. These assert the difference in both directions, so a later simplification
    that collapses the two is a failing test rather than a silent loss."""

    def setUp(self):
        self.tmp = temp_dir()

    def _layout(self, name: str, files: dict[str, str]) -> Path:
        root = self.tmp / name
        root.mkdir()
        for filename, text in files.items():
            (root / filename).parent.mkdir(parents=True, exist_ok=True)
            (root / filename).write_text(text)
        return root

    def test_the_trust_fingerprint_cannot_see_a_moved_file_boundary(self):
        split = self._layout("split", {"a.py": "X = 1\n", "b.py": "Y = 2\n"})
        merged = self._layout("merged", {"a.py": "X = 1\nY = 2\n", "b.py": ""})
        self.assertEqual(loader.directory_fingerprint(split), loader.directory_fingerprint(merged))

    def test_the_pin_digest_sees_a_moved_file_boundary(self):
        split = self._layout("split", {"a.py": "X = 1\n", "b.py": "Y = 2\n"})
        merged = self._layout("merged", {"a.py": "X = 1\nY = 2\n", "b.py": ""})
        self.assertNotEqual(loader.pin_digest(split), loader.pin_digest(merged))

    def test_the_pin_digest_sees_an_added_empty_file(self):
        plain = self._layout("plain", {"a.py": "X = 1\n"})
        extra = self._layout("extra", {"a.py": "X = 1\n", "zz.py": ""})
        self.assertEqual(loader.directory_fingerprint(plain), loader.directory_fingerprint(extra))
        self.assertNotEqual(loader.pin_digest(plain), loader.pin_digest(extra))

    def test_the_pin_digest_sees_a_rename(self):
        before = self._layout("before", {"gate.py": "X = 1\n"})
        after = self._layout("after", {"other.py": "X = 1\n"})
        self.assertEqual(loader.directory_fingerprint(before), loader.directory_fingerprint(after))
        self.assertNotEqual(loader.pin_digest(before), loader.pin_digest(after))

    def test_the_pin_digest_is_the_hash_of_a_sha256sum_listing(self):
        """The property that lets somebody without picoagent compute the same number."""
        root = self._layout("listing", {"a.py": "X = 1\n", "sub/b.py": "Y = 2\n"})
        listing = "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(root).as_posix()}\n"
            for path in loader.directory_files(root))
        self.assertEqual(loader.pin_digest(root),
                         hashlib.sha256(listing.encode()).hexdigest())

    def test_the_module_entry_point_prints_the_same_value(self):
        """A publisher runs this to get the number they put in a release note."""
        root = self._layout("printed", {"a.py": "X = 1\n"})
        result = subprocess.run([sys.executable, "-m", "picoagent.plugins.loader", str(root)],
                                capture_output=True, text=True, cwd=str(ROOT))
        self.assertEqual(result.stdout.strip(), loader.pin_digest(root), result.stderr)


class TrustingADirectoryIsGatedToo(PinFixture):
    """``plugin trust <dir>`` never goes through ``resolve_source``, so the gate cannot live
    only there: a hand-placed directory would be approvable without meeting any pin."""

    def test_trust_refuses_to_record_a_directory_that_fails_its_pin(self):
        checkout = loader.plugins_dir(self.cfg()) / "origin"
        checkout.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", self.spec, str(checkout)], check=True)
        self.write_pins("")          # a policy that pins nothing, so nothing may be approved
        store = loader.TrustStore(self.home)
        with self.assertRaises(loader.PluginVerificationError):
            store.trust(loader.Manifest.load(checkout))
        self.assertEqual(loader.TrustStore(self.home).data, {}, "an approval was written anyway")

    def test_the_cli_answers_that_refusal_with_a_sentence_and_not_a_traceback(self):
        """``picoagent plugin trust <dir>`` used to let the raise escape ``plugin_command``,
        so the user's fail-closed refusal arrived as a crash. Same policy as above, driven the
        way a person reaches it."""
        checkout = loader.plugins_dir(self.cfg()) / "origin"
        checkout.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", self.spec, str(checkout)], check=True)
        self.write_pins("")
        buffer = io.StringIO()
        args = argparse.Namespace(pcmd="trust", spec=str(checkout), yes=True)
        with redirect_stdout(buffer):
            code = cli.plugin_command(args)      # a raise here is the defect this test pins
        self.assertEqual(code, 1, buffer.getvalue())
        self.assertIn("not trusted", buffer.getvalue())
        self.assertEqual(loader.TrustStore(self.home).data, {})


class DependenciesAreHashPinnedWhenTheSiteSaysSo(PinFixture):
    """The second half of the finding: ``python_deps`` reach pip with whatever version it
    resolves today, from whatever index it is pointed at."""

    def _manifest(self, deps: list[str]) -> loader.Manifest:
        root = self.tmp / "depplug"
        root.mkdir(exist_ok=True)
        (root / "plugin.toml").write_text(
            PLUGIN_TOML + "python_deps = %s\n" % json.dumps(deps))
        (root / "pinned_probe.py").write_text("def register(api):\n    pass\n")
        return loader.Manifest.load(root)

    def test_with_no_policy_pip_is_called_as_before(self):
        with mock.patch.object(loader.subprocess, "run") as run:
            loader.install_deps(self._manifest(["requests"]))
        self.assertIn("requests", run.call_args[0][0])

    def test_an_unhashed_dependency_refuses_and_runs_no_pip(self):
        self.write_pins("")
        with mock.patch.object(loader.subprocess, "run") as run:
            with self.assertRaises(loader.PluginVerificationError):
                loader.install_deps(self._manifest(["requests"]))
        run.assert_not_called()

    def test_a_hashed_dependency_reaches_pip_with_require_hashes(self):
        self.write_pins("")
        dep = "requests==2.31.0 --hash=sha256:" + "a" * 64
        with mock.patch.object(loader.subprocess, "run") as run:
            loader.install_deps(self._manifest([dep]))
        command = run.call_args[0][0]
        self.assertIn("--require-hashes", command)
        self.assertIn("-r", command)

    def test_the_requirements_pip_is_given_are_the_manifests_own_lines(self):
        self.write_pins("")
        dep = "requests==2.31.0 --hash=sha256:" + "a" * 64
        seen = {}

        def capture(command, *args, **kwargs):
            seen["text"] = Path(command[command.index("-r") + 1]).read_text()
            return subprocess.CompletedProcess(command, 0)

        with mock.patch.object(loader.subprocess, "run", capture):
            loader.install_deps(self._manifest([dep]))
        self.assertIn(dep, seen["text"])

    def test_an_unhashed_dependency_is_refused_before_consent_is_asked(self):
        """``plugin add`` records the approval and *then* installs, because a source
        distribution runs its build script during install and consent has to come first. So a
        refusal raised at pip time would leave a plugin approved with its dependencies missing.
        The manifest is read on the resolve path instead, before anything is asked or written."""
        origin = self.tmp / "deporigin"
        origin.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(origin)], check=True)
        (origin / "plugin.toml").write_text(PLUGIN_TOML + 'python_deps = ["requests"]\n')
        (origin / "pinned_probe.py").write_text("def register(api):\n    pass\n")
        git(origin, "add", "-A")
        git(origin, "commit", "-q", "-m", "v1")
        self.write_pins('unpinned = "allow"\n')
        with self.assertRaises(loader.PluginVerificationError) as caught:
            loader.resolve_source(f"file://{origin}", self.cfg())
        self.assertIn("--hash", str(caught.exception))
        self.assertEqual(loader.TrustStore(self.home).data, {})

    def test_a_site_can_keep_unpinned_dependencies(self):
        self.write_pins('python_deps = "allow-unpinned"\n')
        with mock.patch.object(loader.subprocess, "run") as run:
            loader.install_deps(self._manifest(["requests"]))
        self.assertIn("requests", run.call_args[0][0])


class PluginAddRefusesEndToEnd(PinFixture):
    """What the user actually sees: a refusal, exit 1, and no approval anywhere."""

    def _add(self) -> tuple[int, str]:
        buffer = io.StringIO()
        args = argparse.Namespace(pcmd="add", spec=self.spec, project=False, yes=True)
        with redirect_stdout(buffer):
            code = cli.plugin_command(args)
        return code, buffer.getvalue()

    def test_a_mismatched_pin_exits_one_and_records_no_approval(self):
        self.write_pins(f'["{self.origin}"]\nsha256 = "{"0" * 64}"\n')
        code, out = self._add()
        self.assertEqual(code, 1, out)
        self.assertEqual(loader.TrustStore(self.home).data, {})

    def test_the_output_says_why(self):
        self.write_pins(f'["{self.origin}"]\nsha256 = "{"0" * 64}"\n')
        _, out = self._add()
        self.assertIn("cannot install", out)


def _sign_head(tmp: Path, origin: Path) -> str | None:
    """Sign ``origin``'s HEAD with a throwaway key, and answer with its fingerprint.

    ``None`` when this machine cannot generate one, so the tests that need a real signature skip
    rather than pass on a check that never ran.
    """
    if shutil.which("gpg") is None:
        return None
    home = tmp / "gnupg"
    home.mkdir(mode=0o700, exist_ok=True)
    env = {**os.environ, "GNUPGHOME": str(home)}
    made = subprocess.run(["gpg", "--batch", "--yes", "--passphrase", "", "--quick-generate-key",
                           "picoagent test <t@t>", "default", "default", "never"],
                          capture_output=True, text=True, env=env)
    if made.returncode != 0:
        return None
    listed = subprocess.run(["gpg", "--list-secret-keys", "--with-colons"],
                            capture_output=True, text=True, env=env)
    fingerprint = next((line.split(":")[9] for line in listed.stdout.splitlines()
                        if line.startswith("fpr:")), None)
    if not fingerprint:
        return None
    signed = git(origin, "-c", f"user.signingkey={fingerprint}", "-c", "commit.gpgsign=true",
                 "commit", "--amend", "--no-edit", f"-S{fingerprint}", env=env)
    if signed.returncode != 0:
        return None
    # Generating a key starts a gpg-agent against this GNUPGHOME, and the directory goes away
    # with the temp tree while the daemon does not. Left running it holds a socket under a path
    # that no longer exists, once per test that signs, for the rest of the login session.
    subprocess.run(["gpgconf", "--kill", "all"], capture_output=True, env=env)
    return fingerprint


if __name__ == "__main__":
    unittest.main()
