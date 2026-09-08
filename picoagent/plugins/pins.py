"""What a site requires of a plugin before it may be installed - ``~/.picoagent/plugin-pins.toml``.

Why this exists, and what it is not
-----------------------------------
The trust store records a SHA-256 over every file in a plugin directory at the moment the user
approves it. That is a real control, and it is a control over *change*: it can say the code you
approved has been replaced. It cannot say the code you approved was ever the code anyone
published, because the value it compares against was computed from the bytes that arrived. A
host serving different bytes serves a different fingerprint with them, and the prompt reads
exactly as it does on a good day.

A *pin* is the value the trust store cannot have: one the administrator obtained somewhere other
than from the download, and wrote down before the fetch. DISA STIG V-222513 asks for a signature
check and names this as the fallback in the same breath - "a cryptographic hash value that can be
verified by a system administrator prior to installation" - and it is the half of that rule this
project can satisfy without leaving the standard library.

The other half, ``signed_by``, shells out to ``git verify-commit`` / ``git verify-tag``. That is
what git-native signing actually is, and it is honest about its cost: it needs the ``git`` binary,
it needs GnuPG, and it needs the signer's key already in the keyring the site controls. When any
of those is missing the answer is a refusal, never a pass - see :func:`_signature_refusal`. An
OpenPGP key is not a certificate from an approved CA, so this does not meet the rule's primary
clause; it meets the thing that clause was written to obtain.

The file
--------
::

    # ~/.picoagent/plugin-pins.toml
    unpinned = "refuse"                 # or "allow"; "refuse" is the default
    python_deps = "require-hashes"      # or "allow-unpinned"; "require-hashes" is the default

    ["https://github.com/opscontinuum/permission-gate"]
    sha256 = "5f2b..."                  # over every file the trust fingerprint covers
    signed_by = ["A1B2C3D4E5F60718"]    # key fingerprints or long key ids

Absent, the file decides nothing and picoagent installs as it always has: this is a capability a
site turns on, not a new obligation on everyone who has a plugin. Present, its defaults are the
strict ones. An administrator who writes this file is stating a policy, and a policy whose default
is "and anything not mentioned is fine" is the shape that has to be argued for, not assumed.

Only the user's copy is read. A repository's ``.picoagent/config.toml`` is merged into the
running config before you have looked at anything in the clone, so a policy that lived there
would be a policy the code it admits gets to write. This file is never read from a project.
"""
from __future__ import annotations

import logging
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("picoagent.plugins")

#: Read from the user directory and nowhere else. See the module docstring.
PINS_FILENAME = "plugin-pins.toml"

#: Seconds a verification command gets. Wider than the loader's local-git bound because
#: ``verify-commit`` starts GnuPG, which may have an agent to spawn first.
VERIFY_TIMEOUT: float = 20

#: Shortest ``signed_by`` entry that identifies a key. A 32-bit short id is eight characters and
#: has been collidable on ordinary hardware since 2019, so accepting one would let an attacker
#: present a key of their own that the policy file names. Sixteen is the long key id; forty is
#: the full fingerprint. Refusing the short form is a refusal to install, not a warning: a
#: policy line that cannot pick out one key is not a policy line.
MIN_KEY_ID: int = 16

_REFUSE, _ALLOW = "refuse", "allow"
_REQUIRE_HASHES, _ALLOW_UNPINNED = "require-hashes", "allow-unpinned"

#: Stripped from both sides before an identity is compared, so an administrator writing
#: ``github.com/o/r`` pins the plugin that a spec spells ``git:github.com/o/r@v1``. Forgiving in
#: the direction of *finding* a pin, which is the fail-closed direction: the cost of a near miss
#: is a refusal the administrator has to widen, not an install nobody checked.
_SCHEMES = ("git:", "https://", "http://", "ssh://", "file://", "git@")


def identity(value: str) -> str:
    """One spelling of "which plugin is this", reduced so two spellings of it compare equal."""
    text = value.strip()
    for scheme in _SCHEMES:
        text = text.removeprefix(scheme)
    return text.rstrip("/").removesuffix(".git")


@dataclass(frozen=True)
class Policy:
    """The site's rules, as the file states them.

    ``unreadable`` is kept rather than folded into an empty policy because the two call for
    opposite answers and only the read can tell them apart. A file that is not there is a site
    with no policy, and everything installs. A file that is there and cannot be parsed is a site
    *with* a policy that nobody can currently apply, and nothing installs - the same direction
    the trust store takes when its own file is damaged, for the same reason: a control that
    fails open is a control an attacker only has to break rather than defeat.
    """
    entries: dict[str, dict]
    unpinned: str = _REFUSE
    python_deps: str = _REQUIRE_HASHES
    unreadable: bool = False
    path: Path | None = None

    @staticmethod
    def load(user_dir: Path) -> "Policy | None":
        """The policy in ``<user_dir>/plugin-pins.toml``, or ``None`` when there is no such file."""
        path = Path(user_dir) / PINS_FILENAME
        if not path.exists():
            return None
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
        except MemoryError:
            raise
        except Exception as exc:  # noqa: BLE001 - any parse failure is the same fact here
            log.error("%s could not be read (%s), so no plugin can be installed until it parses",
                      path, exc)
            return Policy(entries={}, unreadable=True, path=path)
        entries = {identity(key): value for key, value in data.items() if isinstance(value, dict)}
        return Policy(entries=entries,
                      unpinned=_choice(data.get("unpinned"), (_REFUSE, _ALLOW), _REFUSE),
                      python_deps=_choice(data.get("python_deps"),
                                          (_REQUIRE_HASHES, _ALLOW_UNPINNED), _REQUIRE_HASHES),
                      path=path)

    def entry(self, identities: list[str]) -> dict | None:
        """The rule covering a plugin known by any of these names, or ``None``.

        Identities are tried in the order the caller gives them, which is most-specific first:
        the url actually fetched from, then the url the spec asked for before any
        ``[plugins].rewrite`` redirected it, then the directory on disk. A mirror is therefore
        pinnable by either name, and redirecting a spec at one cannot shed a pin written against
        the other.
        """
        for name in identities:
            found = self.entries.get(identity(name))
            if found is not None:
                return found
        return None

    def refusal(self, identities: list[str], root: Path, fingerprint: str) -> str | None:
        """Why this checkout may not be installed, or ``None`` when it satisfies the policy.

        A sentence rather than a boolean because every caller has to show it to somebody: a
        refusal a user cannot act on sends them to turn the control off.
        """
        if self.unreadable:
            return (f"{self.path} states this site's plugin verification policy and could not be "
                    "parsed, so no plugin can be verified and none is installed. Repair or remove "
                    "that file.")
        rule = self.entry(identities)
        if rule is None:
            if self.unpinned == _ALLOW:
                return None
            return (f"no entry in {self.path} covers {identities[0]}, and that file says unpinned "
                    f'plugins are refused. Add [\"{identities[0]}\"] with the sha256 the publisher '
                    'gives for this release, or set unpinned = "allow" to install unverified '
                    "plugins again.")
        expected = rule.get("sha256")
        # `isinstance(..., list)` before iterating, not just per element: iterating the string
        # `signed_by = "A1B2"` yields four single-character "signers", every one of which a real
        # fingerprint ends with, so a typed-wrong policy line would accept any signature at all.
        declared = rule.get("signed_by")
        signers = [s for s in declared if isinstance(s, str)] if isinstance(declared, list) else []
        if declared is not None and not signers:
            return (f"the entry for {identities[0]} in {self.path} has a signed_by that is not a "
                    "list of key fingerprints, so no signature could be required of this plugin")
        if not isinstance(expected, str) and not signers:
            return (f"the entry for {identities[0]} in {self.path} sets neither sha256 nor "
                    "signed_by, so it states nothing to verify against")
        if isinstance(expected, str):
            if expected.strip().lower() != fingerprint.lower():
                return (f"{identities[0]} does not match the hash pinned in {self.path}. "
                        f"expected {expected.strip().lower()}, fetched {fingerprint}. "
                        "The code was NOT installed.")
        if signers:
            return _signature_refusal(root, signers, self.path)
        return None

    def dependency_refusal(self, deps: list[str]) -> str | None:
        """Why these ``python_deps`` may not be installed, or ``None``.

        Under ``require-hashes`` every entry has to be an ordinary pip requirement line that
        pins an exact version *and* carries at least one ``--hash``. That is pip's own
        ``--require-hashes`` contract, which also means the manifest must list the whole
        transitive set - pip refuses to install anything unhashed once the flag is on. That cost
        is the control: an author who lists only the top-level package has not said which
        artifacts the install may fetch.
        """
        if self.unreadable:
            return (f"{self.path} could not be parsed, so dependency verification cannot be "
                    "applied and no package is installed")
        if self.python_deps == _ALLOW_UNPINNED:
            return None
        loose = [dep for dep in deps if "--hash=" not in dep or "==" not in dep]
        if not loose:
            return None
        return (f'{self.path} sets python_deps = "require-hashes", and '
                f"{', '.join(repr(dep) for dep in loose)} "
                f"{'is' if len(loose) == 1 else 'are'} not pinned to an exact version with a "
                "--hash. Nothing was installed. A dependency line looks like "
                "'requests==2.31.0 --hash=sha256:<digest>'.")


def _choice(value: object, allowed: tuple[str, ...], default: str) -> str:
    """``value`` when it is one of ``allowed``, otherwise ``default``.

    A misspelling falls back to the strict default rather than to the permissive one. Somebody
    who writes ``unpinned = "allowed"`` meant to loosen the policy and has not; a refusal tells
    them so, where silently loosening would not.
    """
    return value if isinstance(value, str) and value in allowed else default


def _signature_refusal(root: Path, signers: list[str], path: Path | None) -> str | None:
    """Whether what is checked out at ``root`` carries a good signature from one of ``signers``.

    Both objects a git publisher can sign are tried: the commit at ``HEAD``, and any annotated
    tag pointing at it. A release is normally the tag, a development pin is normally the commit,
    and requiring the administrator to know which would make the policy file depend on how the
    publisher happens to cut releases.
    """
    wanted = {s.strip().upper().replace(" ", "") for s in signers if s.strip()}
    too_short = sorted(name for name in wanted if len(name) < MIN_KEY_ID)
    if too_short:
        return (f"{path} names {', '.join(too_short)} in signed_by, and a key id shorter than "
                f"{MIN_KEY_ID} characters is not specific enough to identify a key: 32-bit short "
                "ids can be collided on purpose. Use the long key id or the full fingerprint "
                "(gpg --list-keys --keyid-format=long). Nothing was installed.")
    attempts = [("verify-commit", "HEAD")] + [("verify-tag", tag) for tag in _tags_at_head(root)]
    problems = []
    for command, target in attempts:
        found, problem = _good_signature_keys(root, command, target)
        if problem:
            problems.append(problem)
            continue
        # A configured name matches when it is a *suffix* of a fingerprint git reported, which is
        # how a long key id names the key its fingerprint ends with. Only that direction: the
        # reverse would let a longer configured string be satisfied by a shorter reported one.
        if any(key.endswith(name) for key in found for name in wanted):
            return None
        if found:
            problems.append(f"{target} is signed by {', '.join(sorted(found))}, which "
                            f"{path} does not name")
    detail = "; ".join(dict.fromkeys(problems)) or "nothing at this checkout carries a signature"
    return (f"{root} carries no signature this site accepts, so it was not installed: {detail}. "
            f"{path} requires a signature from {', '.join(sorted(wanted))}.")


def _tags_at_head(root: Path) -> list[str]:
    """Tags pointing at ``HEAD``; empty for any failure, including git not being installed."""
    try:
        result = subprocess.run(["git", "-C", str(root), "tag", "--points-at", "HEAD"],
                                capture_output=True, text=True, timeout=VERIFY_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return []
    return result.stdout.split() if result.returncode == 0 else []


def _good_signature_keys(root: Path, command: str, target: str) -> tuple[set[str], str]:
    """``(keys with a good signature over target, why there is none)``.

    Fails closed at every step, which is the whole point of the function. ``git`` missing,
    GnuPG missing, the command timing out, a non-zero exit and a zero exit with no ``VALIDSIG``
    line all produce an empty set and a sentence - never an empty set the caller could read as
    "nothing to object to".

    ``--raw`` asks for GnuPG's machine-readable status, which git writes to stderr. It is parsed
    rather than the human text because the human text is localised. The site's own allowlist of
    keys is what decides acceptance, so GnuPG's trust database is deliberately not consulted:
    "did key X sign this" is the question, and web-of-trust ownertrust is a different one.
    """
    try:
        result = subprocess.run(["git", "-C", str(root), command, "--raw", target],
                                capture_output=True, text=True, timeout=VERIFY_TIMEOUT)
    except FileNotFoundError:
        return set(), ("git is not installed on this machine, so no signature could be checked")
    except subprocess.TimeoutExpired:
        return set(), f"git {command} did not finish within {VERIFY_TIMEOUT:g}s"
    except (OSError, subprocess.SubprocessError) as exc:
        return set(), f"git {command} could not be run ({type(exc).__name__})"
    if result.returncode != 0:
        first = next((line for line in result.stderr.splitlines()
                      if line.strip() and not line.startswith("[GNUPG:]")), "")
        return set(), f"git {command} {target} failed{': ' + first.strip() if first else ''}"
    keys = set()
    for line in result.stderr.splitlines():
        fields = line.split()
        if len(fields) > 2 and fields[:2] == ["[GNUPG:]", "VALIDSIG"]:
            keys.add(fields[2].upper())
            keys.add(fields[-1].upper())
    if not keys:
        return set(), f"git {command} {target} reported no verified signing key"
    return keys, ""
