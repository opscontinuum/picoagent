"""Finding, installing, trusting and loading plugins.

Where plugins come from
-----------------------
* ``[plugins].enabled`` in config: ``git:github.com/user/repo@tag``, ``https://...git@ref``,
  or a local path (relative to the project).
* Anything already sitting in ``~/.picoagent/plugins/`` or ``<project>/.picoagent/plugins/``.
* ``-e path`` on the command line (trusted for that run only).

Load order is exactly that: user config, project config, user dir, project dir, CLI.
Because registrations override by name, a project-level plugin beats a user-level one.

Which layer a spec came from
---------------------------
``[plugins].enabled`` is concatenated rather than overridden - the user's list, then the
repository's - so a cloned repository can suggest a plugin. Resolving a spec is not a read:
it clones and it runs ``git checkout``. A repository's spec allowed to resolve into the
user's plugin directory could therefore name a *different ref of a plugin the user already
trusts*, move that checkout, and leave the user's approved plugin failing its fingerprint
and silently not loading - one line of committed config disabling a security plugin.

So a spec carries its layer as far as the directory it may write. The user's specs own
``~/.picoagent/plugins``; a repository's own ``<project>/.picoagent/plugins`` and nothing
else. Suggesting a plugin still works, and still faces the trust prompt; suggesting one is
just no longer a way to write into code the user approved.

A spec whose layer cannot be established is not resolved at all. See ``enabled_by_layer``.

Trust
-----
Plugin code runs with your full privileges, so nothing loads until you've said yes.
``picoagent plugin add`` shows the manifest and asks; the answer is stored in
``~/.picoagent/trust.json`` as a hash over *every file in the plugin directory*. If any of
them changes - a new version, an edit, or something tampered with - the hash no longer
matches and the plugin is skipped with a warning until you trust it again.

Hashing the whole directory rather than just the entry module is deliberate: the entry
imports its siblings, so a narrower fingerprint let ``helper.py`` be rewritten while the
plugin still reported *trusted*. What the record is filed against is the directory, not the
name in the manifest, for the same reason: the name is a line the replacing code also writes.

A plugin that supplies a security control can say that being skipped is not an acceptable
outcome - ``required = true`` in its ``plugin.toml``, or ``api.declare_required`` at runtime.
Replacing code the user approved for such a plugin stops the session rather than printing a
line about it, and so does removing it. See ``RequiredPluginError`` for what that covers and
what it deliberately does not.

One namespace per plugin
------------------------
Everything a plugin imports from its own directory is loaded under that plugin's package and
nowhere else, so two plugins can both ship a ``utils.py``. They used to share one: the plugin
directory went on ``sys.path``, siblings landed in ``sys.modules`` under bare top-level names,
and the second plugin to ask for ``utils`` got the first one's - code approved under another
fingerprint, running unannounced. See ``_SiblingImporter``.
"""
from __future__ import annotations

import builtins
import hashlib
import importlib.util
import json
import logging
import re
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..core.loop import Runtime
from .api import PluginAPI
from .manifest import Manifest

log = logging.getLogger("picoagent.plugins")
# file:// counts: an air-gapped site may mirror plugins onto a shared mount rather than
# run a git server, and git treats such a path as a remote like any other.
_GIT_SPEC = re.compile(r"^(?:git:|https?://|git@|ssh://|file://)")


# ------------------------------------------------------------------------ provenance

class PluginOwnershipError(RuntimeError):
    """A spec from one config layer tried to write a checkout another layer owns."""


class PluginProvenanceError(RuntimeError):
    """Which layer a ``[plugins].enabled`` spec came from could not be established."""


#: The layer a spec or a discovered directory came from.
USER, PROJECT, CLI = "user", "project", "cli"


def project_enabled(cfg: dict) -> list[str] | None:
    """The specs the *repository's* config.toml adds to ``[plugins].enabled``, or ``None``.

    Read from the file rather than from the merged config, because the merge deliberately
    loses the distinction: the two lists are concatenated so a repository can suggest a
    plugin, and after that nothing downstream can say which entries the repository wrote.

    ``[]`` and ``None`` are different answers and were once the same one. ``[]`` means the
    repository asked for nothing, which is knowable: there is no config file, or it has no
    ``[plugins].enabled``. ``None`` means nobody can say what it asked for - no ``_cwd`` to look
    under, a file that will not open, a file that will not parse. Collapsing the second into the
    first attributed every spec to the user, the layer allowed to write into the user's own
    plugin directory, which is precisely the attribution this module exists to withhold.
    """
    cwd = cfg.get("_cwd")
    if cwd is None:
        return None
    path = Path(cwd) / ".picoagent" / "config.toml"
    if not path.exists():
        return []                    # a repository that wrote no config asked for nothing
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    plugins = data.get("plugins")
    enabled = plugins.get("enabled") if isinstance(plugins, dict) else None
    return [spec for spec in enabled if isinstance(spec, str)] if isinstance(enabled, list) else []


def _is_spec(spec: object, cfg: dict) -> bool:
    """Is this ``[plugins].enabled`` entry something that could name a plugin at all?

    Says where to look when it is not. Either config file can hold the offending line and the
    merged list no longer remembers which, so both are named: better two paths to check than a
    value quoted with no file attached.
    """
    if isinstance(spec, str):
        return True
    cwd = cfg.get("_cwd")
    where = f"{Path(cwd) / '.picoagent' / 'config.toml'} or your own config" if cwd else "your config"
    log.error("ignoring [plugins].enabled entry %r: a plugin spec is a string (a git url or a "
              "path). Check %s.", spec, where)
    return False


def enabled_by_layer(cfg: dict) -> list[tuple[str, str]]:
    """``[plugins].enabled`` paired with the layer each spec came from.

    ``load_config`` builds the list user-first, so the repository's specs are its tail. A
    config assembled some other way - a test, an embedder wiring its own ``Runtime`` - is
    attributed by membership instead, which errs towards calling a spec the repository's:
    that is the safe direction, because that is the one that may not write.

    When the repository's own list cannot be read at all, neither rule applies and this raises
    rather than picking one. Guessing was the bug: an unreadable ``.picoagent/config.toml``
    produced an empty project list, an empty project list put every spec in the user layer, and
    a user-layer spec resolves with the off-limits check switched off - so a config the loader
    could not read got the one privilege reading it was meant to decide. The alternative, calling
    unplaceable specs the repository's, is safe for that check and quietly wrong everywhere else:
    it relocates the user's own installs into ``<project>/.picoagent/plugins`` and sends them
    back to the trust prompt, a second silent failure to fix the first. Raising says which file
    could not be read. It costs nothing in the shipped CLI, where ``load_config`` sets ``_cwd``
    and already refuses a project config that will not parse; an embedder assembling ``enabled``
    by hand gets told to set ``_cwd`` instead of being handed a security decision made by
    coin flip.

    Every pair this returns has a string in it, which is what everything downstream assumes. A
    ``[plugins].enabled`` entry that is not a string names no plugin, so it is dropped here and
    said out loud rather than carried. It used to be carried: ``project_enabled`` dropped such
    entries and ``load_config`` kept them, so the two lists no longer lined up, the membership
    fallback handed the stray value to the *user* layer, and ``discover`` passed it to
    ``resolve_source``, where matching a bool against the git-spec pattern raised a ``TypeError``
    nothing was catching. ``enabled = [true]`` committed to a repository stopped every session
    opened in it. A config that is wrong about one line is a config to report, not a session to
    end, and dropping the entry keeps the tail comparison aligned so its neighbours still place.
    """
    enabled = [spec for spec in cfg.get("plugins", {}).get("enabled") or [] if _is_spec(spec, cfg)]
    project = project_enabled(cfg)
    if project is None:
        if not enabled:
            return []                # nothing to place, so nothing to be wrong about
        cwd = cfg.get("_cwd")
        source = (str(Path(cwd) / ".picoagent" / "config.toml") if cwd
                  else "the project config (no '_cwd' set)")
        raise PluginProvenanceError(
            f"cannot tell which config layer these plugin specs came from: "
            f"{', '.join(map(str, enabled))}. Reading {source} failed, and a spec whose layer is "
            "unknown is not resolved: the layer decides which plugin directory it may write to. "
            "Fix or remove that file, or set '_cwd' on the config.")
    boundary = len(enabled) - len(project)
    if boundary < 0 or (project and enabled[boundary:] != project):
        return [(spec, PROJECT if spec in project else USER) for spec in enabled]
    return ([(spec, USER) for spec in enabled[:boundary]]
            + [(spec, PROJECT) for spec in enabled[boundary:]])


# ------------------------------------------------------------------------ locations

def plugins_dir(cfg: dict, project: bool = False) -> Path:
    """``~/.picoagent/plugins`` or ``<project>/.picoagent/plugins``."""
    base = Path(cfg["_cwd"]) / ".picoagent" if project else Path(cfg["_user_dir"])
    return base / "plugins"


def resolve_source(spec: str, cfg: dict, project: bool = False) -> Path:
    """Turn a plugin spec into a local directory, cloning git sources on first use.

    ``project=True`` means the spec came from the repository rather than from the user, and it
    decides two things: the clone lands in the repository's own plugin directory, and it may
    not touch the user's. ``picoagent plugin add`` is the user acting, so it passes ``False``
    and keeps the whole upgrade path - an ``add`` on an installed plugin still moves it.
    """
    if _GIT_SPEC.match(spec):
        rewrites = cfg.get("plugins", {}).get("rewrite") or {}
        return _clone_or_update(spec, plugins_dir(cfg, project), rewrites,
                                off_limits=plugins_dir(cfg) if project else None)
    path = Path(spec).expanduser()
    return path if path.is_absolute() else Path(cfg["_cwd"]) / path


def parse_spec(spec: str, rewrites: dict[str, str] | None = None) -> tuple[str, str]:
    """Split a git spec into ``(url, ref)``. ``ref`` is ``""`` when none was given.

    Splitting on the *last* ``@`` rather than the first, because SSH remotes contain one:
    ``git@host:team/repo.git@main`` has two, and taking the first produced the url ``git``
    and a ref of everything else - so SSH specs, the usual form for an internal server,
    could never work. A trailing segment containing ``/`` or ``:`` is part of the address
    rather than a ref, which is what distinguishes ``git@host:team/repo.git`` (no ref) from
    ``git@host:team/repo.git@v1`` (ref ``v1``).

    ``rewrites`` maps a url prefix to a replacement, so a site can point every spec at an
    internal mirror without editing each one.
    """
    url, separator, ref = spec.rpartition("@")
    if not separator or "/" in ref or ":" in ref:
        url, ref = spec, ""
    url = url.removeprefix("git:")
    for prefix, replacement in (rewrites or {}).items():
        if url.startswith(prefix):
            url = replacement + url[len(prefix):]
            break
    if not url.startswith(("http://", "https://", "git@", "ssh://", "file://", "/")):
        url = "https://" + url
    return url, ref


def checkout_name(url: str) -> str:
    """Directory name for a cloned plugin: the repository name, however the url spells it."""
    return url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1].removesuffix(".git")


def _clone_or_update(spec: str, dest_root: Path, rewrites: dict[str, str] | None = None,
                     *, off_limits: Path | None = None) -> Path:
    """Clone ``spec`` under ``dest_root`` and move it to ``ref``.

    Fast-forwards rather than only checking out. ``git checkout <branch>`` on a branch that
    already exists locally does nothing with what ``fetch`` just retrieved, so a plugin
    tracking a branch stayed frozen at the commit it was first cloned at - there was no
    upgrade path at all. Merging with ``--ff-only`` moves it, and refuses rather than
    inventing a merge commit if the checkout has diverged.
    """
    url, ref = parse_spec(spec, rewrites)
    dest = checkout_path(dest_root, url, off_limits)
    dest_root.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        subprocess.run(["git", "clone", "-q", url, str(dest)], check=True)
    else:
        subprocess.run(["git", "-C", str(dest), "fetch", "--tags", "-q"], check=False)
    if ref:
        subprocess.run(["git", "-C", str(dest), "checkout", "-q", ref], check=True)
    fast_forward(dest)
    return dest


def checkout_path(dest_root: Path, url: str, off_limits: Path | None = None) -> Path:
    """Where a spec's checkout goes, refusing anything that is not directly under ``dest_root``.

    ``checkout_name`` is derived from a url a repository's config may have written, so it is
    checked rather than trusted: a name of ``..`` would put the fetch a level up. ``off_limits``
    is the directory this spec may not reach - the user's plugin directory, when the spec is the
    repository's - and is compared after resolving, so a symlinked ``.picoagent/plugins`` shipped
    in the repository does not get there either.
    """
    name = checkout_name(url)
    if name in ("", ".", "..") or "/" in name or "\\" in name:
        raise PluginOwnershipError(f"refusing plugin checkout directory {name!r} from {url}")
    dest = dest_root / name
    if off_limits is not None and _within(dest, off_limits):
        raise PluginOwnershipError(
            f"a plugin spec from this repository's config would write to {dest}, which your own "
            f"plugin directory owns; a repository's plugins install under its own .picoagent/plugins")
    return dest


def _within(path: Path, root: Path) -> bool:
    """Is ``path`` ``root`` itself or inside it, symlinks resolved?"""
    try:
        path, root = path.resolve(), root.resolve()
    except OSError:
        return False
    return path == root or root in path.parents


def fast_forward(root: Path) -> None:
    """Move a checkout to whatever its upstream now points at, when that is safe.

    Only when the branch has an upstream, the tree is clean, and the move is a fast-forward.
    Anything else is left alone: a plugin the user has edited, or a branch that has diverged,
    is not something to silently rewrite during a routine load.
    """
    if subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                      capture_output=True, text=True).stdout.strip():
        return
    upstream = subprocess.run(["git", "-C", str(root), "rev-parse", "--abbrev-ref", "@{u}"],
                              capture_output=True, text=True)
    if upstream.returncode != 0:
        return                       # detached HEAD or a tag: nothing to follow
    subprocess.run(["git", "-C", str(root), "merge", "--ff-only", "-q", upstream.stdout.strip()],
                   check=False)


def install_deps(manifest: Manifest) -> None:
    """pip-install a plugin's declared ``python_deps`` (no-op when empty)."""
    if manifest.python_deps:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", *manifest.python_deps], check=False)


# ------------------------------------------------------------------------ trust

#: Never part of a fingerprint: build artefacts and VCS metadata that a plugin ships without
#: meaning to, and that change without the plugin changing.
_UNTRUSTED_NOISE = {"__pycache__", ".git", ".hg", ".svn", ".mypy_cache", ".pytest_cache"}


def plugin_files(manifest: Manifest) -> list[Path]:
    """Every file a trust decision covers - the whole plugin directory, not just the entry.

    Fingerprinting only ``plugin.toml`` and the entry module left a hole wide enough to drive
    a plugin through: the entry module imports its siblings, so rewriting ``helper.py`` changed
    what executed while the fingerprint still matched and the plugin still reported *trusted*.
    Every multi-module plugin in ``examples/`` was affected, which is most of them.

    Skills are included too. They are not executed, but they are injected into the model's
    prompt, and text that steers the model is as much a part of what the user approved as code
    that runs. A README is included for the same reason it is cheap to: the alternative is a
    rule about which files matter, and that rule is what failed the first time.
    """
    files = []
    for path in sorted(manifest.root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _UNTRUSTED_NOISE for part in path.relative_to(manifest.root).parts):
            continue
        if path.suffix in (".pyc", ".pyo"):
            continue
        files.append(path)
    return files


def plugin_fingerprint(manifest: Manifest) -> str:
    """sha256 over the manifest and entry module - the thing the user actually approved."""
    digest = hashlib.sha256()
    for path in plugin_files(manifest):
        digest.update(path.read_bytes())
    return digest.hexdigest()


def plugin_file_hashes(manifest: Manifest) -> dict[str, str]:
    """Per-file digests, so a re-approval can say *which* file moved, not just that one did."""
    return {str(path.relative_to(manifest.root)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in plugin_files(manifest)}


def plugin_commit(root: Path) -> str | None:
    """The checked-out commit of a git-sourced plugin, or ``None`` for a plain directory."""
    return _git(root, "rev-parse", "HEAD")


def commits_between(root: Path, old: str, new: str, limit: int = 10) -> list[str]:
    """``git log --oneline old..new`` - what an upgrade is actually bringing in."""
    output = _git(root, "log", "--oneline", f"{old}..{new}")
    return output.splitlines()[:limit] if output else []


def _git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


class TrustStore:
    """What the user approved, and which *directory* they approved it in.

    Deliberately stores more than a fingerprint. A bare hash can only say *that* something
    changed, which leaves the user with one blunt option - re-approve and hope. Per-file
    hashes name the file that moved, and the commit (for a git checkout) lets the CLI show
    the incoming commits before asking. Records written by older versions are a plain
    fingerprint string; they still load, and simply can't describe a change in detail.

    A record is a record of a *directory*. The plugin's name is only the label it is filed
    under, because looking approvals up by name made an approval as durable as a field the
    replacing code gets to rewrite: change ``name`` in the replaced ``plugin.toml`` and the same
    directory read as ``new`` rather than ``changed``, the enforcement gate for a replaced plugin
    never fired, and the requirement recorded at approval - recorded precisely so whoever
    replaced the code could not delete it - was answered by a record nothing looked up again.
    A ``fast_forward`` onto a rewritten upstream does that without an attacker touching the
    machine. The directory is not the manifest's to rewrite: it is where the user pointed when
    they approved, and it is where the loader reads the code from.

    So a record matches a plugin when its ``root`` is that plugin's directory, whatever either
    of them is called. Two checkouts may share a name and get an approval each; a record from a
    version that stored no ``root`` is matched by name, so an upgrade sends nobody back to the
    trust prompt, and re-approving gives it a directory.
    """

    def __init__(self, user_dir: Path):
        self.path = user_dir / "trust.json"
        raw = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.data: dict[str, dict] = {key: {"fingerprint": rec} if isinstance(rec, str) else rec
                                      for key, rec in raw.items()}

    @staticmethod
    def key(root: Path) -> str:
        """A directory as an approval identifies it.

        Resolved, because the same directory is reached by several spellings - ``plugin trust``
        resolves its argument, discovery joins a configured plugin directory onto a name, and
        either may run through a symlink. An approval that only covered one spelling would send
        the other back to the prompt.
        """
        try:
            return str(Path(root).resolve())
        except OSError:
            return str(Path(root).absolute())

    def record(self, manifest: Manifest) -> dict:
        """The approval covering this directory, or an older rootless one filed under its name."""
        root = self.key(manifest.root)
        for record in self.data.values():
            if record.get("root") == root:
                return record
        filed = self.data.get(manifest.name)
        return filed if filed is not None and not filed.get("root") else {}

    def is_trusted(self, manifest: Manifest) -> bool:
        record = self.record(manifest)
        return bool(record) and record.get("fingerprint") == plugin_fingerprint(manifest)

    def status(self, manifest: Manifest) -> str:
        """``trusted`` (approved, unchanged), ``changed`` (approved, but not this version),
        or ``new`` (never approved). ``changed`` is the interesting one: it means code the
        user vetted has been replaced by code they haven't."""
        if not self.record(manifest):
            return "new"
        return "trusted" if self.is_trusted(manifest) else "changed"

    def approved_commit(self, manifest: Manifest) -> str | None:
        """The commit the user approved, when the record has one."""
        return self.record(manifest).get("commit")

    def approved_required(self, manifest: Manifest) -> bool:
        """Was this plugin ``required`` *when the user approved it*?

        Recorded at approval rather than read from the directory, because the directory is the
        thing under suspicion whenever this is asked: it is consulted for a plugin whose
        fingerprint no longer matches, and whoever replaced the code could otherwise delete the
        line that makes replacing it fatal. Records written before this field existed fall back
        to the manifest on disk - the only evidence they have - because reading them as *not
        required* would silently disarm every approval made before the feature shipped.
        """
        return bool(self.record(manifest).get("required", manifest.required))

    def approved_required_reason(self, manifest: Manifest) -> str:
        """Why the user was told this plugin had to run, as it read when they approved it.

        Recorded for the same reason the flag is: the reason is part of what was approved, and
        reading it back off a replaced manifest lets the replacement choose the sentence the
        user sees when the session stops.
        """
        record = self.record(manifest)
        return record.get("required_reason") or manifest.required_reason or _REQUIRED_UNSTATED

    def approved_requirements(self) -> list[tuple[str, Path, str]]:
        """Every recorded requirement as ``(name, directory, reason)`` - what must still be there.

        Only records that name a directory. A record from a version that stored none cannot say
        where the code it approved lived, and a bare name cannot distinguish "the plugin is
        gone" from "this project does not use it" - the second would stop sessions that have
        nothing wrong with them. Approving such a plugin once records its directory and brings
        it under this check.
        """
        return [(record.get("name") or key, Path(record["root"]),
                 record.get("required_reason") or _REQUIRED_UNSTATED)
                for key, record in self.data.items()
                if record.get("required") and record.get("root")]

    def change_kind(self, manifest: Manifest) -> str:
        """Why a *changed* plugin no longer matches: ``moved`` or ``edited``.

        ``moved`` means the checkout sits on a different commit than the one approved. Nobody
        arrives there by editing a file; git put it there, which means a spec somewhere named a
        ref and the code the user vetted was replaced wholesale by other code from the same
        repository. That deserves different words from "you edited this plugin yourself",
        because it is the case where the user did nothing and their plugin stopped running.
        """
        approved, current = self.approved_commit(manifest), plugin_commit(manifest.root)
        return "moved" if approved and current and approved != current else "edited"

    def describe_change(self, manifest: Manifest) -> list[str]:
        """Lines describing what moved since approval, for a human deciding whether to accept."""
        record = self.record(manifest)
        lines: list[str] = []
        approved, current = record.get("files") or {}, plugin_file_hashes(manifest)
        if not approved:
            lines.append("approved before per-file records were kept - cannot say which file changed")
        else:
            for name in sorted(set(approved) | set(current)):
                if approved.get(name) == current.get(name):
                    continue
                state = "added" if name not in approved else "removed" if name not in current else "modified"
                lines.append(f"{name}: {state}")
        old, new = record.get("commit"), plugin_commit(manifest.root)
        if old and new and old != new:
            lines.append(f"commit {old[:12]} -> {new[:12]}")
            lines += [f"  {line}" for line in commits_between(manifest.root, old, new)]
        return lines

    def trust(self, manifest: Manifest) -> None:
        """Record this directory's current code as approved, replacing whatever covered it.

        Any earlier record of this directory goes, however it was labelled, so a re-approval
        after a rename leaves one record rather than two disagreeing ones. A rootless record
        filed under this name goes too: it would otherwise go on vouching for whatever other
        directory next claimed the name.
        """
        root = self.key(manifest.root)
        stale = [label for label, record in self.data.items()
                 if record.get("root") == root or (label == manifest.name and not record.get("root"))]
        for label in stale:
            del self.data[label]
        self.data[self._label(manifest, root)] = {
            "name": manifest.name,
            "root": root,
            "fingerprint": plugin_fingerprint(manifest),
            "files": plugin_file_hashes(manifest),
            "commit": plugin_commit(manifest.root),
            "required": manifest.required,
            "required_reason": manifest.required_reason,
            "approved_at": int(time.time())}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2))

    def _label(self, manifest: Manifest, root: str) -> str:
        """What to file this approval under: its plugin name, or a longer form when that is taken.

        The label exists so a human reading ``trust.json`` can see what a record is about. It
        decides nothing, which is why a second checkout of the same plugin can be filed beside
        the first instead of overwriting it - approving a repository's copy of a plugin must not
        quietly withdraw the approval of the user's own.
        """
        taken = self.data.get(manifest.name)
        if taken is None or taken.get("root") == root:
            return manifest.name
        return f"{manifest.name}@{hashlib.sha256(root.encode()).hexdigest()[:8]}"


# ------------------------------------------------------------------------ loading

def package_name(manifest: Manifest) -> str:
    """The module namespace one plugin directory owns.

    The directory digest is not decoration. Two checkouts can claim the same plugin name -
    the user's and the copy a repository ships - and ``-e`` can load both in one process.
    Keyed by name alone, the second one's ``import es_client`` would find the first one's
    already in ``sys.modules`` and quietly run code from the other checkout: the same
    collision this namespace exists to prevent, one directory narrower.
    """
    digest = hashlib.sha256(str(manifest.root.resolve()).encode()).hexdigest()[:8]
    return f"picoagent_plugin_{manifest.name}_{digest}"


class _SiblingImporter:
    """``__import__`` for one plugin: imports of its own files resolve inside its package.

    The entry module is loaded as a package rooted at the plugin directory, but ``import
    es_client`` is an absolute import and absolute imports do not consult the importing
    package. The previous answer was to push the plugin directory onto ``sys.path``, which
    made every plugin's files top-level names in one process-wide namespace. Two plugins
    shipping ``utils.py`` got whichever loaded first, so the second silently executed bytes
    the user approved under a different fingerprint and a different manifest - a trust
    boundary crossed without a word. The path entry also outlived the load and shadowed the
    standard library and site-packages for everything imported afterwards.

    Rewriting the import where it is made keeps the plugin's own spelling working and keeps
    the result out of everyone else's way. It is installed per module through
    ``__builtins__`` rather than by replacing the global ``builtins.__import__`` or adding a
    ``sys.meta_path`` finder, because both of those are process-wide and would have to guess
    which caller they are answering. A module's builtins are inherited by the functions
    defined in it, so a sibling imported lazily inside a tool call is redirected as well.
    """

    def __init__(self, package: str, root: Path):
        self.package, self.root = package, root
        #: A snapshot of ``builtins``, so a later monkeypatch of it will not reach plugin
        #: code. A live mapping would fix that, but ``__builtins__`` is documented as a dict
        #: and this package supports Python 3.11 up; a mapping that happens to work on one
        #: version is not something to build the import path on.
        self.namespace = {**vars(builtins), "__import__": self}

    def __call__(self, name, globals=None, locals=None, fromlist=(), level=0):
        """Resolve ``name`` inside the plugin when the plugin ships it, else import normally."""
        if level == 1 and (globals or {}).get("__package__") == self.package:
            return self._relative(name, fromlist)
        head = name.partition(".")[0] if level == 0 else ""
        sibling = self._sibling(head) if head.isidentifier() else None
        if sibling is None:
            return builtins.__import__(name, globals, locals, fromlist, level)
        if "." in name:
            deepest = importlib.import_module(f"{self.package}.{name}")
            return deepest if fromlist else sibling
        return sibling

    def _relative(self, name: str, fromlist) -> object:
        """``from . import helper`` / ``from .helper import thing`` in the plugin's own package.

        Worth handling even though every shipped plugin spells sibling imports absolutely: a
        plugin that mixes the two styles would otherwise get a relative import served by the
        ordinary machinery, and the module it produced would not carry this importer.
        """
        if not name:
            for item in fromlist or ():
                self._sibling(item)          # names that are not modules stay plain attributes
            return sys.modules[self.package]
        self._sibling(name.partition(".")[0])
        return importlib.import_module(f"{self.package}.{name}")

    def _sibling(self, head: str):
        """The plugin's own top-level ``head``, imported on first ask, or ``None`` if it has none.

        A file the plugin ships wins over an installed distribution of the same name, which is
        what the ``sys.path`` insert did too - it went to the front. What changes is that the
        win is now confined to the plugin that shipped the file.
        """
        full = f"{self.package}.{head}"
        if full in sys.modules:
            return sys.modules[full]
        for source in (self.root / f"{head}.py", self.root / head / "__init__.py"):
            if source.is_file():
                return self._load(full, head, source)
        return None

    def _load(self, full: str, head: str, source: Path):
        """Execute one sibling under ``full``, with this plugin's import behaviour in place."""
        locations = [str(source.parent)] if source.name == "__init__.py" else None
        spec = importlib.util.spec_from_file_location(full, source, submodule_search_locations=locations)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot import plugin module {source}")
        module = importlib.util.module_from_spec(spec)
        module.__dict__["__builtins__"] = self.namespace
        sys.modules[full] = module          # before executing, so an import cycle sees the partial
        parent = sys.modules.get(self.package)
        if parent is not None:
            setattr(parent, head, module)
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(full, None)     # a half-executed module must not answer the next ask
            raise
        return module


def _import_register(manifest: Manifest):
    """Import the entry module as the plugin's own package and return the ``register`` callable.

    ``submodule_search_locations`` makes the entry module a package rooted at the plugin
    directory, so everything the plugin ships is reachable as ``<package>.<module>`` and
    nothing it ships takes a top-level name. ``sys.path`` is deliberately untouched.
    """
    path = manifest.entry_path()
    package = package_name(manifest)
    spec = importlib.util.spec_from_file_location(package, path,
                                                  submodule_search_locations=[str(manifest.root)])
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import plugin entry {path}")
    module = importlib.util.module_from_spec(spec)
    module.__dict__["__builtins__"] = _SiblingImporter(package, manifest.root).namespace
    sys.modules[package] = module
    if "." not in manifest.entry_module:
        # A sibling that imports the entry by name must get this module, not a second copy of it.
        sys.modules[f"{package}.{manifest.entry_module}"] = module
    spec.loader.exec_module(module)
    return getattr(module, manifest.entry_function)


class RequiredPluginError(RuntimeError):
    """A plugin the user is relying on is not going to run, and the session should not either.

    Skipping is the loader's normal answer to a plugin that does not load, and for most plugins
    it is the right one: a broken formatter should not stop a session. For a plugin that *is* a
    security control it is the wrong one, because the session then runs with the control absent
    and one line on stderr to say so - the difference between "the guard refused this command"
    and "there is no guard" is invisible from inside the session.

    The loader cannot tell the two kinds of plugin apart, so the plugin says which it is, and it
    says it in two places that mean the same thing and set the same field:

    * ``required = true`` in ``plugin.toml``, read before any of the plugin's code runs. This is
      the one that covers the two ways a plugin goes missing without reaching ``register()``:
      the *trust* check refused it, or importing its entry module raised.
    * ``api.declare_required(reason)`` inside ``register()``, for a plugin that only discovers at
      runtime that it cannot do its job. ``load_plugin`` seeds the same field from the manifest
      first, so a manifest declaration already covers a failing ``register()`` and the two are
      one mechanism rather than two.

    A recorded requirement outlives the manifest that stated it, so the third way - the plugin
    is not there at all - is caught from the trust store. See ``RequiredPluginMissing``.

    Subclasses name which way the plugin went missing.
    """


class RequiredPluginFailed(RequiredPluginError):
    """A required plugin never got as far as running: its import or its ``register()`` raised.

    Both, because both end the same way. A missing dependency takes a security control out of a
    session exactly as thoroughly as a control that refuses to register, and the manifest's
    ``required`` is readable before either happens.
    """

    def __init__(self, name: str, reason: str, cause: BaseException):
        super().__init__(f"required plugin '{name}' failed to load: {cause}. {reason}. "
                         "Fix what it reported, or remove it from [plugins].enabled if you "
                         "no longer want it - it will not be skipped silently.")
        self.name, self.reason, self.cause = name, reason, cause


class RequiredPluginUntrusted(RequiredPluginError):
    """A required plugin the user approved has been replaced by code they have not approved.

    Deliberately narrower than "a required plugin did not load", on two axes.

    *Not* raised for a plugin that is merely ``new``. Installing a plugin and then running is the
    ordinary first-run path, and making it fatal turns "I added a security plugin" into "picoagent
    will not start until I trust it from a session I can no longer open". Nothing the user was
    relying on has been taken away yet, so the ``new`` skip stays a notice - a loud one, since a
    required plugin that is not running is exactly what ``Notice.urgent`` is for.

    *Not* raised for a plugin the repository owns. ``required`` is a line in a ``plugin.toml``
    that a cloned repository wrote, and honouring it there would hand any repository a switch
    that stops the user's session on demand. A repository's copy that is refused is announced
    like every other repository-layer skip. The user's own plugins are the user's own call.
    """

    def __init__(self, name: str, reason: str, root: Path, change: str):
        super().__init__(
            f"required plugin '{name}' is not the version you approved ({change}) and was not "
            f"loaded, so the session is not starting. {reason}. Review what changed and decide: "
            f"picoagent plugin trust {root} - or drop 'required' from its plugin.toml if you no "
            "longer want it enforced.")
        self.name, self.reason, self.root, self.change = name, reason, root, change


class RequiredPluginMissing(RequiredPluginError):
    """A plugin approved as required is not there to run, and nothing took its place.

    The other two cases have a directory to look at and a diff to review. This one is the
    absence itself: the checkout was deleted, or its ``plugin.toml`` stopped parsing, or its
    entry no longer imports. Replacing approved code stops the session, so deleting it has to
    as well, or the shorter route around the check is the one with nothing guarding it.

    Only for a requirement recorded against a directory under the user's own plugin directory.
    That is the directory every session of theirs reads, so absence from it means the plugin is
    gone rather than unused here. A requirement recorded for a repository's copy is left alone
    for the reason ``RequiredPluginUntrusted`` leaves it alone: ``required`` is a line a
    repository wrote, and honouring it hands any repository a switch that stops the user's work.
    """

    def __init__(self, name: str, reason: str, root: Path, store: Path):
        super().__init__(
            f"required plugin '{name}' did not load and nothing is running in its place, so the "
            f"session is not starting. You approved it at {root} and it was required then: "
            f"{reason}. Put it back, or drop its entry from {store} if you meant to remove it - "
            "a plugin you approved as required is not skipped quietly.")
        self.name, self.reason, self.root, self.store = name, reason, root, store


#: Said on a plugin's behalf when its manifest requires it but names no reason of its own.
_REQUIRED_UNSTATED = "it declares itself required, so this session is not running without it"


def load_plugin(root: Path, rt: Runtime, trust: TrustStore, *, allow_untrusted: bool = False) -> Manifest | None:
    """Load one plugin directory into ``rt``. Returns its manifest, or ``None`` if refused."""
    manifest = Manifest.load(root)
    if not (allow_untrusted or trust.is_trusted(manifest)):
        log.warning("plugin '%s' is not trusted (new or changed). Run: picoagent plugin trust %s",
                    manifest.name, root)
        return None
    api = PluginAPI(rt, manifest.name, manifest.root)
    if manifest.required:
        # Seeded, not checked separately: a manifest declaration and a runtime one are the same
        # statement, so they set the same field and a plugin that spells it both ways gets the
        # more specific wording, whichever it wrote last.
        api.declare_required(manifest.required_reason or _REQUIRED_UNSTATED)
    try:
        # Imported inside the declaration, not before it. The manifest says this plugin has to
        # run and says it without executing anything, so an import that fails - a dependency
        # gone after a venv rebuild, an entry attribute that no longer exists - is covered by
        # it. Importing first left that failure to the generic catch in `load_all`, which
        # reported the plugin as an ordinary skip: not urgent, "run with --verbose", session
        # continuing with the control absent while `manifest.required` sat in hand unread.
        _import_register(manifest)(api)
    except Exception as exc:
        if api.required_reason:
            raise RequiredPluginFailed(manifest.name, api.required_reason, exc) from exc
        raise
    for skills_dir in manifest.skills:
        rt.skills.add_dir(manifest.root / skills_dir, source=f"plugin:{manifest.name}")
    log.info("loaded plugin %s %s", manifest.name, manifest.version)
    return manifest


@dataclass(frozen=True)
class Discovered:
    """A plugin directory and the config layer that put it there."""
    root: Path
    layer: str            # USER, PROJECT or CLI
    spec: str = ""


def discover(cfg: dict, extra_paths: list[str]) -> list[Discovered]:
    """Every plugin directory in load order, each tagged with the layer it came from."""
    found: list[Discovered] = []
    seen: set[Path] = set()

    def add(root: Path, layer: str, spec: str = "") -> None:
        if root in seen:
            return
        seen.add(root)
        found.append(Discovered(root, layer, spec))

    for spec, layer in enabled_by_layer(cfg):
        try:
            add(resolve_source(spec, cfg, project=layer == PROJECT), layer, spec)
        except (OSError, subprocess.SubprocessError, PluginOwnershipError) as exc:
            log.error("cannot resolve plugin %s: %s", spec, exc)
    for directory, layer in ((plugins_dir(cfg), USER), (plugins_dir(cfg, project=True), PROJECT)):
        if directory.is_dir():
            for path in sorted(directory.iterdir()):
                if (path / "plugin.toml").exists():
                    add(path, layer)
    for path in extra_paths:
        add(Path(path).expanduser().resolve(), CLI)
    return found


def discover_roots(cfg: dict, extra_paths: list[str]) -> list[Path]:
    """All plugin directories in load order (see module docstring)."""
    return [entry.root for entry in discover(cfg, extra_paths)]


@dataclass(frozen=True)
class Notice:
    """One thing a user needs told about a plugin that did not load, already phrased for them.

    The text lives here rather than in the CLI so that every frontend says the same thing, and
    so that ``urgent`` - a plugin the user approved is not running - is a property of the event
    rather than something each frontend re-derives from a reason string.
    """
    name: str
    reason: str           # new | changed | shadowed | invalid | failed | missing
    root: Path
    text: str
    urgent: bool = False


@dataclass
class LoadReport:
    """What happened during startup, so the CLI can tell the user rather than only logging it.

    ``skipped`` matters most: a plugin the user installed and expected to be running is now
    silently absent, and "it changed since you approved it" needs a different response from
    "you never approved it". ``notices`` carries the same events with the wording and the
    urgency attached, because the case worth shouting about - an approved plugin that stopped
    running without the user touching it - is not distinguishable from a reason string alone.
    """
    loaded: list[Manifest] = field(default_factory=list)
    skipped: list[tuple[str, str, Path]] = field(default_factory=list)   # (name, reason, root)
    notices: list[Notice] = field(default_factory=list)

    def needs_review(self) -> list[tuple[str, str, Path]]:
        return [entry for entry in self.skipped if entry[1] == "changed"]

    def lines(self) -> list[str]:
        """Everything worth printing, in load order. A frontend can print these verbatim."""
        return [notice.text for notice in self.notices]

    def urgent(self) -> list[Notice]:
        """Plugins the user approved that are not running - the failure mode that matters.

        A security plugin that quietly does not load looks exactly like one that loaded and
        found nothing, so this is the list a ``-p`` or ``--json`` run must still surface.
        """
        return [notice for notice in self.notices if notice.urgent]


@dataclass(frozen=True)
class Skip:
    """One plugin that did not load, carrying everything the wording depends on."""
    name: str
    reason: str            # new | changed | shadowed | invalid | failed | missing
    root: Path
    layer: str = USER
    manifest: Manifest | None = None
    shadowed_by: Path | None = None


def _skip_notice(skip: Skip, trust: TrustStore) -> Notice:
    """The line a user gets for a plugin that did not load.

    The three ``changed`` wordings are the point of this function. "You edited it", "something
    moved its checkout", and "this repository offers a version you have not approved" are three
    different situations, and a user who cannot tell them apart cannot act on any of them.
    """
    hint = f"  Review it and decide:  picoagent plugin trust {skip.root}"
    absent = "whatever it enforces is off for this session."
    off = f"and was NOT LOADED - {absent}"
    if skip.reason == "missing":
        return Notice(skip.name, skip.reason, skip.root,
                      f"plugin '{skip.name}' is one you approved as REQUIRED and nothing loaded "
                      f"from {skip.root} - {absent}\n"
                      f"  Put it back there, or drop its entry from your trust store.", urgent=True)
    if skip.reason == "new":
        if skip.manifest is not None and skip.manifest.required:
            # A first run is not fatal (see RequiredPluginUntrusted), but a plugin that says the
            # session should not run without it is not ordinary "not trusted yet" chatter either.
            return Notice(skip.name, skip.reason, skip.root,
                          f"plugin '{skip.name}' declares itself REQUIRED and you have not "
                          f"approved it yet, so it was not loaded - {absent}\n{hint}", urgent=True)
        return Notice(skip.name, skip.reason, skip.root,
                      f"plugin '{skip.name}' is not trusted yet and was not loaded.\n{hint}")
    if skip.reason == "shadowed":
        return Notice(skip.name, skip.reason, skip.root,
                      f"plugin '{skip.name}' offered by this repository was not loaded; your own "
                      f"'{skip.name}' at {skip.shadowed_by} is the one running.\n"
                      f"  The repository's copy is at {skip.root}.\n{hint}")
    if skip.reason != "changed":
        return Notice(skip.name, skip.reason, skip.root,
                      f"plugin '{skip.name}' did not load ({skip.reason}); "
                      f"run with --verbose for the detail.")
    if skip.layer == PROJECT:
        return Notice(skip.name, skip.reason, skip.root,
                      f"plugin '{skip.name}' offered by this repository is not the version you "
                      f"approved {off}\n  The repository's copy is at {skip.root}.\n{hint}",
                      urgent=True)
    if skip.manifest is not None and trust.change_kind(skip.manifest) == "moved":
        approved = (trust.approved_commit(skip.manifest) or "")[:12]
        current = (plugin_commit(skip.root) or "")[:12]
        return Notice(skip.name, skip.reason, skip.root,
                      f"plugin '{skip.name}' was MOVED to a revision you have not approved "
                      f"({approved} -> {current}) {off}\n"
                      f"  You did not edit it: something moved its checkout. Check "
                      f"[plugins].enabled in your own config and in this repository's "
                      f".picoagent/config.toml.\n{hint}", urgent=True)
    return Notice(skip.name, skip.reason, skip.root,
                  f"plugin '{skip.name}' CHANGED since you approved it {off}\n{hint}", urgent=True)


def _log_notice(notice: Notice, *, alone: bool) -> None:
    """Log a notice at a level that depends on whether anything else is going to say it.

    ``load_all`` returns every notice already worded, and the CLI prints ``notice.text`` itself
    with an urgency mark. Logging the same sentence at warning level put an urgent skip on stderr
    twice: once as the loader's log line and once as the CLI's, differently formatted, saying the
    same thing. A notice that appears twice reads as two problems, which is the opposite of what
    urgency is for.

    So presentation belongs to whoever wired a frontend, and ``alone`` is that question. A caller
    with a frontend has said it will show the user things and can read the wording off the report;
    the log keeps a copy at info, which is where a ``--verbose`` run wants it anyway. A caller
    with no frontend - a library embedder driving the loop directly - has nothing that will ever
    say it, so the text goes out at a level the default handler prints rather than being filed
    somewhere nobody is looking.
    """
    if alone:
        (log.error if notice.urgent else log.warning)("%s", notice.text)
    else:
        log.info("%s", notice.text)


def _names_the_user_owns(found: list[Discovered],
                         will_load: Callable[[Path, Manifest], bool]) -> dict[str, Path]:
    """Plugin name -> the user-owned directory it is *running* from.

    A repository may offer a plugin the user already has, and that is neither an error nor
    "the plugin you approved changed". Reporting it the second way is a false alarm about a
    copy that is loading perfectly well, which is how a real alarm gets ignored. Only copies
    that actually load count, because the wording promises the user theirs is the one running.
    """
    owned: dict[str, Path] = {}
    for entry in found:
        if entry.layer == PROJECT:
            continue
        try:
            manifest = Manifest.load(entry.root)
        except Exception:  # noqa: BLE001 - the load pass below reports a broken manifest
            continue
        if will_load(entry.root, manifest):
            owned.setdefault(manifest.name, entry.root)
    return owned


def load_all(rt: Runtime, extra_paths: list[str] | None = None,
             allow_untrusted: bool = False) -> LoadReport:
    """Load every discovered plugin. CLI ``-e`` paths are implicitly trusted for this run."""
    extra = [Path(p).expanduser().resolve() for p in (extra_paths or [])]
    trust = TrustStore(Path(rt.cfg["_user_dir"]))
    report = LoadReport()
    found = discover(rt.cfg, extra_paths or [])

    def will_load(root: Path, manifest: Manifest) -> bool:
        return allow_untrusted or root.resolve() in extra or trust.is_trusted(manifest)

    user_owned = _names_the_user_owns(found, will_load)

    def skip(entry: Skip) -> None:
        notice = _skip_notice(entry, trust)
        report.skipped.append((entry.name, entry.reason, entry.root))
        report.notices.append(notice)
        _log_notice(notice, alone=rt.frontend is None)

    for entry in found:
        root = entry.root
        try:
            manifest = Manifest.load(root)
        except Exception as exc:  # noqa: BLE001 - a broken manifest must not stop the others
            log.error("cannot read plugin at %s: %s", root, exc)
            skip(Skip(str(root), "invalid", root, entry.layer))
            continue
        if not will_load(root, manifest):
            status = trust.status(manifest)   # "changed" or "new"
            owner = user_owned.get(manifest.name)
            if entry.layer == PROJECT and owner is not None and owner != root:
                skip(Skip(manifest.name, "shadowed", root, entry.layer, manifest, owner))
            else:
                skip(Skip(manifest.name, status, root, entry.layer, manifest))
            # After the notice, so the reason the session is stopping has already been recorded
            # and logged the same way every other skip is.
            if status == "changed" and entry.layer != PROJECT and trust.approved_required(manifest):
                raise RequiredPluginUntrusted(manifest.name,
                                              trust.approved_required_reason(manifest),
                                              root, trust.change_kind(manifest))
            continue
        try:
            load_plugin(root, rt, trust, allow_untrusted=True)   # trust already decided above
        except RequiredPluginFailed:
            raise                 # the plugin said skipping it is not an answer; believe it
        except Exception as exc:  # noqa: BLE001 - one bad plugin must not stop the others
            log.exception("failed to load plugin at %s: %s", root, exc)
            skip(Skip(manifest.name, "failed", root, entry.layer, manifest))
            continue
        report.loaded.append(manifest)
    _enforce_recorded_requirements(rt.cfg, trust, report, skip)
    return report


def _enforce_recorded_requirements(cfg: dict, trust: TrustStore, report: LoadReport,
                                   skip: Callable[[Skip], None]) -> None:
    """Stop the session for a requirement the user recorded that nothing answered.

    Everything above is driven by what is on disk, so a plugin that is no longer on disk is
    reached by none of it: a deleted checkout, a ``plugin.toml`` that stopped parsing, an entry
    module that no longer imports. The trust store is the only party that still remembers the
    plugin was supposed to be running, and a required plugin that vanished leaves exactly the
    session ``required`` exists to prevent - the control absent, and nothing said about it.

    Checked after the load rather than against discovery, because "did it load" is the question
    the user cares about; a directory that was found and then failed is as absent as one that
    was never there.
    """
    loaded = {TrustStore.key(manifest.root) for manifest in report.loaded}
    owned = plugins_dir(cfg)
    for name, root, reason in trust.approved_requirements():
        if TrustStore.key(root) in loaded or not _within(root, owned):
            continue
        skip(Skip(name, "missing", root, USER))
        raise RequiredPluginMissing(name, reason, root, trust.path)
