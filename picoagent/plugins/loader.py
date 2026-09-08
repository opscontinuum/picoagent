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
Such a plugin failing to load stops the session rather than printing a line about it. The
declaration is read from the plugin as it is on disk, so it says nothing about a plugin that is
no longer there. See ``RequiredPluginError`` for what that covers and what it deliberately does
not.

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
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .. import __version__
from ..core.config import PROJECT_ENABLED_KEY, user_dir
from ..core.loop import Runtime
from . import pins
from .api import PluginAPI
from .manifest import Manifest, unmet_requirements

log = logging.getLogger("picoagent.plugins")
# file:// counts: an air-gapped site may mirror plugins onto a shared mount rather than
# run a git server, and git treats such a path as a remote like any other.
_GIT_SPEC = re.compile(r"^(?:git:|https?://|git@|ssh://|file://)")


def is_git_spec(spec: str) -> bool:
    """Does this ``[plugins].enabled`` entry name a git remote rather than a local path?

    Asked here by everything that asks it. The schemes used to be written out twice, once as
    this pattern and once as a tuple in ``upgrade``, and two lists that have to agree drift:
    a scheme added to one names a plugin the other cannot see, so the same spec resolves at
    load time and is invisible to ``picoagent upgrade``, or the reverse.
    """
    return bool(_GIT_SPEC.match(spec))


# ------------------------------------------------------------------------ provenance

class PluginOwnershipError(RuntimeError):
    """The loader will not hand this spec back as a directory to install from.

    Named for its first case and kept under that name deliberately: a spec from one config layer
    trying to write a checkout another layer owns. ``cli.add_plugin`` catches it to print
    *cannot install <spec>: <reason>*, which is the sentence every refusal at this seam wants,
    so a second refusal here is a subclass rather than a sibling. See
    :class:`PluginVerificationError`.
    """


class PluginVerificationError(PluginOwnershipError):
    """The checkout arrived and does not match what the site pinned for it.

    Separate from the ownership case because they answer different questions - *may this spec
    write here* against *are these the bytes the publisher published* - and a caller that wants
    only one of them should be able to say so. See :mod:`picoagent.plugins.pins`.
    """


class PluginProvenanceError(RuntimeError):
    """Which layer a ``[plugins].enabled`` spec came from could not be established."""


#: The layer a spec or a discovered directory came from.
USER, PROJECT, CLI = "user", "project", "cli"


def project_enabled(cfg: dict) -> list[str] | None:
    """The specs the *repository's* config.toml added to ``[plugins].enabled``, or ``None``.

    Read off ``cfg``, where :func:`~picoagent.core.config.load_config` put it. It is not
    derivable from the merged list - the two layers are concatenated so a repository can
    suggest a plugin, and the join is invisible afterwards - so the layer that knows travels
    with the answer instead of being asked again.

    This used to open ``<project>/.picoagent/config.toml`` itself, and being the *second*
    reader of that file was the whole fault. ``config.load_config`` reads it behind a catch
    broad enough for a file an attacker chose, on the rule that a repository you cloned must
    not be able to deny you your own tool; this one had a narrower catch, so a config that is
    not UTF-8 or is nested past the parser's stack raised straight through ``discover`` and
    ended the session in a traceback the other reader exists to prevent. The two also read at
    different moments, so a file swapped in between made them describe different bytes.

    ``[]`` and ``None`` are still different answers. ``[]`` means the repository asked for
    nothing - no config, no ``[plugins].enabled``, or a config that could not be read, in which
    case nothing of the repository's is in the concatenated list either and every spec in it is
    the user's own. ``None`` means nobody can say: a config that never came from ``load_config``
    and carries no record of the layer. Collapsing that second case into the first would
    attribute a repository's spec to the user, the layer allowed to write into the user's own
    plugin directory, which is the attribution this module exists to withhold.
    """
    specs = cfg.get(PROJECT_ENABLED_KEY)
    if specs is None:
        return None
    return [spec for spec in specs if isinstance(spec, str)]


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

    When the config carries no record of the repository's own list, neither rule applies and
    this raises rather than picking one. Guessing was the bug: an empty project list puts every
    spec in the user layer, and a user-layer spec resolves with the off-limits check switched
    off, so a list nobody could place got the one privilege placing it was meant to decide. The
    alternative, calling unplaceable specs the repository's, is safe for that check and quietly
    wrong everywhere else: it relocates the user's own installs into
    ``<project>/.picoagent/plugins`` and sends them back to the trust prompt, a second silent
    failure to fix the first.

    A repository's config that would not parse is *not* that case, and treating it as one was
    its own bug: ``load_config`` drops such a file with a notice and continues, so the
    concatenated list holds the user's specs and nothing else, and refusing to place them ended
    the session of every user with a plugin of their own the moment they cloned a repository
    with a broken config. That is the outcome the layering exists to prevent, arrived at from
    the other side. The case that remains is a config assembled some other way - a test, an
    embedder wiring its own ``Runtime`` - which is told to build it with ``load_config`` rather
    than being handed a security decision made by coin flip.

    Every pair this returns has a string in it, which is what everything downstream assumes. A
    ``[plugins].enabled`` entry that is not a string names no plugin, so it is dropped here and
    said out loud rather than carried. It used to be carried: ``project_enabled`` dropped such
    entries and ``load_config`` kept them, so the two lists no longer lined up, the membership
    fallback handed the stray value to the *user* layer, and ``discover`` passed it to
    ``resolve_source``, where matching a bool against the git-spec pattern raised a ``TypeError``
    nothing was catching. ``enabled = [true]`` committed to a repository stopped every session
    opened in it. A config that is wrong about one line is a config to report, not a session to
    end, and dropping the entry keeps the tail comparison aligned so its neighbours still place.

    Which spec belongs to which layer is ``_attributed``. This function answers the prior
    question - whether they can be placed at all - and the two are worth reading apart, because
    only one of them has an answer that ends the session.
    """
    enabled = [spec for spec in cfg.get("plugins", {}).get("enabled") or [] if _is_spec(spec, cfg)]
    project = project_enabled(cfg)
    if project is None:
        if not enabled:
            return []                # nothing to place, so nothing to be wrong about
        raise PluginProvenanceError(
            f"cannot tell which config layer these plugin specs came from: "
            f"{', '.join(map(str, enabled))}. This config carries no record of what "
            f"{Path(cfg.get('_cwd', '<project>')) / '.picoagent' / 'config.toml'} asked for, and "
            "a spec whose layer is unknown is not resolved: the layer decides which plugin "
            f"directory it may write to. Build the config with picoagent.core.config.load_config, "
            f"or set '{PROJECT_ENABLED_KEY}' on it yourself.")
    return _attributed(enabled, project)


def _attributed(enabled: list[str], project: list[str]) -> list[tuple[str, str]]:
    """Pair each spec with its layer: by position when the two lists line up, by membership when
    they do not.

    ``load_config`` builds ``enabled`` user-first, so the repository's specs are its tail and the
    boundary between the layers is a length. A config assembled some other way - a test, an
    embedder wiring its own ``Runtime`` - has no such tail, and membership is the only thing left
    to go on. It errs towards calling a spec the repository's, which is the safe direction:
    that is the layer that may not write into the user's plugin directory.
    """
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

    A path spec faces the same off-limits check as a git one, for a reason that is not about
    writing. Resolving a spec also *tags* the directory with the layer that asked for it, and
    ``discover`` keeps the first tag it is given for a directory, so a repository naming a path
    inside ``~/.picoagent/plugins`` took the user's own plugin over as project-layer code: the
    stop that protects a changed ``required`` plugin does not fire at the project layer, and the
    user was told their own edited plugin was "offered by this repository", at a path the
    repository chose. Neither is a claim a repository gets to make about a directory the user's
    plugin directory owns.
    """
    rewrites = cfg.get("plugins", {}).get("rewrite") or {}
    if is_git_spec(spec):
        root = _clone_or_update(spec, plugins_dir(cfg, project), rewrites,
                                off_limits=plugins_dir(cfg) if project else None)
    else:
        path = Path(spec).expanduser()
        path = path if path.is_absolute() else Path(cfg["_cwd"]) / path
        root = _refuse_off_limits(path, plugins_dir(cfg) if project else None)
    refuse_unverified(root, pins.Policy.load(cfg["_user_dir"]), spec=spec, rewrites=rewrites)
    return root


def plugin_identities(root: Path, spec: str = "", rewrites: dict[str, str] | None = None) -> list[str]:
    """The names a pin may be written against for this plugin, most specific first.

    Three of them, because three are in play and an administrator should not have to guess which
    one picoagent will use. The url actually fetched from comes first; the url the spec named
    before any ``[plugins].rewrite`` redirected it comes next, so pointing a site at an internal
    mirror does not shed a pin written against the upstream address; the directory on disk comes
    last, which is the only identity a hand-placed plugin has.

    A checkout reached without a spec - ``picoagent plugin trust <dir>``, or a directory sitting
    in the plugin folder - still gets its remote back out of git, so the same pin covers a plugin
    however the user arrived at it. Without that, ``plugin trust`` on a cloned directory would be
    a way to install by url and be judged as a path.
    """
    names: list[str] = []
    if spec and is_git_spec(spec):
        names.append(parse_spec(spec, rewrites)[0])
        names.append(parse_spec(spec)[0])
    else:
        remote = git_output(root, "remote", "get-url", "origin", timeout=LOCAL_GIT_TIMEOUT)
        if remote:
            names.append(remote)
    names.append(TrustStore.key(root))
    return list(dict.fromkeys(name for name in names if name))


def refuse_unverified(root: Path, policy: "pins.Policy | None", *, spec: str = "",
                      rewrites: dict[str, str] | None = None) -> None:
    """Raise :class:`PluginVerificationError` unless this checkout satisfies the site's policy.

    A no-op when ``policy`` is ``None`` - there is no ``plugin-pins.toml`` - which is the common
    case and the reason this can sit on the resolve path at all: the fingerprint is only
    computed once a site has asked for one.

    The policy is passed in rather than loaded here because the two callers know different user
    directories and must not be made to agree by accident. ``TrustStore`` was handed one when it
    was built and reads its own; ``resolve_source`` has a config and reads that config's.

    Called from those two places on purpose. :func:`resolve_source` covers ``plugin add`` and
    every spec in ``[plugins].enabled``; :meth:`TrustStore.trust` covers the path that has no
    spec at all, ``picoagent plugin trust <directory>``. A gate on only the first would be a
    gate with a documented way round it - clone by hand, then approve.
    """
    if policy is None:
        return
    identities = plugin_identities(root, spec, rewrites)
    refusal = policy.refusal(identities, root, pin_digest(root)) or _dependency_refusal(root, policy)
    if refusal is None:
        return
    # Logged as well as raised: the callers that answer this with a sentence do so on stdout,
    # and a session refusing a spec at startup has only the log to say why.
    log.error("%s", refusal)
    raise PluginVerificationError(refusal)


def _dependency_refusal(root: Path, policy: "pins.Policy") -> str | None:
    """What the site's ``python_deps`` rule says about the manifest sitting at ``root``.

    Asked here, on the resolve path, and not only where pip is actually run. ``plugin add`` runs
    :func:`install_deps` *after* the consent prompt and after the approval is recorded - which is
    the right order for pip, because consent has to come before a source distribution executes
    its build script - and it means a refusal raised there leaves a plugin approved and its
    dependencies absent. Asking before consent turns that into an ordinary refusal with nothing
    written anywhere.

    A manifest that will not parse is not this function's complaint. ``plugin add`` reads it
    immediately afterwards and says so far better than a verification error could.
    """
    try:
        manifest = Manifest.load(root)
    except Exception:  # noqa: BLE001 - the caller reads this manifest next and reports on it
        return None
    return policy.dependency_refusal(manifest.python_deps) if manifest.python_deps else None


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
    return _refuse_off_limits(dest_root / name, off_limits)


def _refuse_off_limits(dest: Path, off_limits: Path | None) -> Path:
    """``dest``, unless it is inside a directory this spec's layer does not own.

    One function for the git and the path spellings of the same claim, because two of them is
    how the path spelling came to have no check at all. ``off_limits`` is ``None`` when the
    spec is the user's own, and their plugin directory when it is the repository's.
    """
    if off_limits is not None and _within(dest, off_limits):
        raise PluginOwnershipError(
            f"a plugin spec from this repository's config names {dest}, which your own plugin "
            f"directory owns; a repository's plugins live under its own .picoagent/plugins")
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


def install_deps(manifest: Manifest, policy: "pins.Policy | None" = None) -> None:
    """pip-install a plugin's declared ``python_deps`` (no-op when empty).

    With no ``plugin-pins.toml`` this is the plain install it has always been: the names the
    manifest wrote, resolved by pip at whatever versions the index offers today. A site that has
    written that file gets pip's ``--require-hashes`` contract instead, and the deps go through a
    requirements file because that is the only place pip accepts a ``--hash`` per requirement.

    The refusal is raised, not warned, and it is raised before pip is invoked at all. A source
    distribution runs its build script during install, so "install it and complain" would be
    complaining about code that has already executed with the user's privileges.

    ``policy`` is a parameter so a caller that has already loaded one is not made to read the
    file twice; it is loaded here when absent because ``cli.add_plugin`` calls this with a
    manifest and nothing else.
    """
    if not manifest.python_deps:
        return
    policy = pins.Policy.load(user_dir()) if policy is None else policy
    if policy is not None:
        refusal = policy.dependency_refusal(manifest.python_deps)
        if refusal is not None:
            log.error("%s", refusal)
            raise PluginVerificationError(refusal)
    if policy is None or policy.python_deps == "allow-unpinned":
        _pip(manifest.python_deps, *manifest.python_deps)
        return
    with tempfile.TemporaryDirectory() as scratch:
        requirements = Path(scratch) / "requirements.txt"
        requirements.write_text("\n".join(manifest.python_deps) + "\n")
        _pip(manifest.python_deps, "--require-hashes", "-r", str(requirements))


def _pip(deps: list[str], *arguments: str) -> None:
    """Run ``pip install`` and say so when it did not work.

    ``check=False``, as it has always been: a dependency that will not install is a plugin that
    will fail to import, and ``load_all`` reports that far better than a traceback out of
    ``plugin add`` would - which would also leave the approval already written. What is new is
    that the failure is said out loud. Silently ignoring pip's exit code means a
    ``--require-hashes`` install whose artifact did not match its hash finishes with *trusted*
    on screen and no other sign at all, which is the one outcome this whole path exists to make
    visible.
    """
    result = subprocess.run([sys.executable, "-m", "pip", "install", "-q", *arguments], check=False)
    if result.returncode != 0:
        log.error("pip install failed (exit %s) for %s; the plugin is approved but its "
                  "dependencies are not installed and it will not import",
                  result.returncode, ", ".join(deps))


# ------------------------------------------------------------------------ trust

#: Never part of a fingerprint: build artefacts and VCS metadata that a plugin ships without
#: meaning to, and that change without the plugin changing.
_UNTRUSTED_NOISE = {"__pycache__", ".git", ".hg", ".svn", ".mypy_cache", ".pytest_cache"}


def directory_files(root: Path) -> list[Path]:
    """Every file under ``root`` that a trust decision or a pin covers.

    Takes a directory rather than a manifest because a pin is checked before there is a
    manifest to take: ``resolve_source`` has a checkout and a spec and nothing else, and the
    whole point of the check is to answer before anything in that directory has been read as
    configuration. :func:`plugin_files` is the same walk asked the other way round.
    """
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _UNTRUSTED_NOISE for part in path.relative_to(root).parts):
            continue
        if path.suffix in (".pyc", ".pyo"):
            continue
        files.append(path)
    return files


def directory_fingerprint(root: Path) -> str:
    """sha256 over the contents of every file in ``root``, in path order.

    What the trust store has always recorded, kept exactly as it was. Note what it is blind to:
    the bytes are concatenated with nothing between them and the names are never hashed, so
    moving a line from the end of ``a.py`` to the start of ``b.py`` produces the same digest,
    and adding an empty file produces the same digest. That is enough for the question the trust
    store asks - *did the bytes I approved change* - and it is not enough for a value an
    administrator publishes as a pin, which is why :func:`pin_digest` exists rather than this
    one being quietly redefined. Redefining it would send every approval on every machine back
    to the trust prompt at once, including the ``required`` ones that stop a session.
    """
    digest = hashlib.sha256()
    for path in directory_files(root):
        digest.update(path.read_bytes())
    return digest.hexdigest()


def pin_digest(root: Path) -> str:
    """The value ``plugin-pins.toml`` is written against: sha256 over a list of file digests.

    One line per file, ``"<sha256 of the file>  <path relative to root>"``, sorted by path,
    newline-terminated, and the whole list hashed. Names and file boundaries are inside the
    digest, so the three things :func:`directory_fingerprint` cannot see - a boundary moved
    between two files, a file renamed, an empty file added - each change it.

    Deliberately the shape ``sha256sum`` already prints, so a publisher or an administrator can
    reproduce it with the tools already on the machine rather than having to trust this
    function. The portable way::

        python3 -m picoagent.plugins.loader <plugin directory>

    and the way that needs no picoagent at all, which is the one that matters when the question
    is whether to install picoagent's idea of this plugin::

        cd <plugin> && find . -type f \\
            -not -path '*/.git/*' -not -path '*/__pycache__/*' -not -path '*/.hg/*' \\
            -not -path '*/.svn/*' -not -path '*/.mypy_cache/*' -not -path '*/.pytest_cache/*' \\
            -not -name '*.pyc' -not -name '*.pyo' \\
          | sed 's|^\\./||' | LC_ALL=C sort | tr '\\n' '\\0' | xargs -0 sha256sum | sha256sum

    The exclusions are :data:`_UNTRUSTED_NOISE` and the compiled-Python suffixes, spelled out
    because a recipe that quietly covers a different set of files than the code does is worse
    than no recipe. ``LC_ALL=C`` is not decoration: a locale-aware sort orders the listing
    differently and produces a different number.

    A hash nobody can compute independently is not a hash anybody can check, and the rule this
    serves (DISA V-222513) asks for one an administrator can verify before installing.
    """
    lines = [f"{hashlib.sha256(path.read_bytes()).hexdigest()}  "
             f"{path.relative_to(root).as_posix()}\n" for path in directory_files(root)]
    return hashlib.sha256("".join(lines).encode()).hexdigest()


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
    return directory_files(manifest.root)


def plugin_fingerprint(manifest: Manifest) -> str:
    """sha256 over every file in the plugin directory - the thing the user actually approved.

    The same number :func:`directory_fingerprint` computes, and deliberately the same one: what
    a site pins and what an approval records have to agree, or a publisher's hash would be a
    value the trust prompt never shows and the user could never check by hand.
    """
    return directory_fingerprint(manifest.root)


def plugin_file_hashes(manifest: Manifest) -> dict[str, str]:
    """Per-file digests, so a re-approval can say *which* file moved, not just that one did."""
    return {str(path.relative_to(manifest.root)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in plugin_files(manifest)}


#: Seconds a git command about a *local* checkout gets. These run while a session is starting,
#: so the bound is there to stop a wedged git holding the session open rather than to let a slow
#: one finish. Anything that talks to a remote states its own, wider, bound - see ``upgrade``.
LOCAL_GIT_TIMEOUT: float = 5


def _file_change_lines(approved: dict[str, str], current: dict[str, str]) -> list[str]:
    """Which files differ between two sets of per-file digests, one line each.

    An empty ``approved`` is a record written before per-file hashes were kept: it can say that
    something changed and not what. Saying so is the honest answer, where an empty list would
    read as "nothing changed" about a plugin that plainly did.
    """
    if not approved:
        return ["approved before per-file records were kept - cannot say which file changed"]
    lines = []
    for name in sorted(set(approved) | set(current)):
        if approved.get(name) == current.get(name):
            continue
        state = "added" if name not in approved else "removed" if name not in current else "modified"
        lines.append(f"{name}: {state}")
    return lines


def _commit_change_lines(root: Path, approved: str | None) -> list[str]:
    """The commit move since approval and the commits it brought in, or nothing to say.

    Nothing to say covers three cases that are one case here: the plugin is not a checkout, the
    record predates commits being stored, and the checkout has not moved.
    """
    current = plugin_commit(root)
    if not (approved and current and approved != current):
        return []
    return ([f"commit {approved[:12]} -> {current[:12]}"]
            + [f"  {line}" for line in commits_between(root, approved, current)])


def plugin_commit(root: Path) -> str | None:
    """The checked-out commit of a git-sourced plugin, or ``None`` for a plain directory."""
    return git_output(root, "rev-parse", "HEAD", timeout=LOCAL_GIT_TIMEOUT)


def commits_between(root: Path, old: str, new: str, limit: int = 10) -> list[str]:
    """``git log --oneline old..new`` - what an upgrade is actually bringing in."""
    output = git_output(root, "log", "--oneline", f"{old}..{new}", timeout=LOCAL_GIT_TIMEOUT)
    return output.splitlines()[:limit] if output else []


def git_output(root: Path | None, *args: str, timeout: float = 30) -> str | None:
    """Run git and answer with its stdout, or ``None`` for any failure at all. Never raises.

    One implementation for both modules that read a checkout. There were two, alike enough to
    look interchangeable and not quite: only one of them could be pointed at something that is
    not a checkout, so a change to how a failed git call is answered was a change only one
    caller got. ``root`` is ``None`` for a command that names its own remote - ``ls-remote``.

    The timeout is the caller's to state rather than a shared constant, because the two kinds of
    call are not alike: reading a local checkout wants a short bound, and a command that crosses
    the network needs a wide one to answer at all.
    """
    command = ["git", *(["-C", str(root)] if root is not None else []), *args]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
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
    directory read as ``new`` rather than ``changed`` - a plugin nobody has ever seen, announced
    as ordinary first-run chatter, in place of "code you approved has been replaced". A
    ``fast_forward`` onto a rewritten upstream does that without an attacker touching the
    machine. The directory is not the manifest's to rewrite: it is where the user pointed when
    they approved, and it is where the loader reads the code from.

    So a record matches a plugin when its ``root`` is that plugin's directory, whatever either
    of them is called, and two checkouts that share a name get an approval each. A record from a
    version that stored no ``root`` is adopted by the first directory that asks for it, so an
    upgrade sends nobody back to the trust prompt and the record stops answering for directories
    the user never pointed at - see ``record``.
    """

    def __init__(self, user_dir: Path):
        self.path = user_dir / "trust.json"
        #: True when the file exists and could not be read, so ``data`` is empty because nothing
        #: could be parsed rather than because nothing was ever approved. Both leave an empty
        #: store, and they call for opposite responses from the user - one is a first run, the
        #: other is a file to restore or delete before every plugin comes back as new. The
        #: distinction is only knowable here, at the read, so it is answered here instead of by
        #: each caller opening the file again and possibly disagreeing with this one.
        self.unreadable = False
        #: The site's verification policy, read from the same directory this store lives in, or
        #: ``None`` when the site has not written one. Held here because ``trust`` is the last
        #: gate before code becomes loadable and it has no config to consult - see
        #: :meth:`trust` and :func:`refuse_unverified`.
        self.policy = pins.Policy.load(user_dir)
        raw = self._read()
        self.data: dict[str, dict] = {key: {"fingerprint": rec} if isinstance(rec, str) else rec
                                      for key, rec in raw.items()}

    def _read(self) -> dict:
        """The store as it is on disk, or an empty one when it cannot be read.

        Empty, never "everything is still approved". The store is the record of what the user
        allowed to run with their privileges, and a file nobody can parse says nothing about
        that; reading a damaged one as permissive would let a truncated write grant what only
        the user may grant. Empty is the other direction: every plugin reads as ``new``, nothing
        loads until it is approved again, and a recorded ``required`` stop cannot fire from a
        record nobody can read either - so the session starts and the CLI can put it right.

        Which is the point of not raising here. ``TrustStore`` is the first thing every session,
        ``plugin list`` and ``plugin untrust`` construct, so an exception out of this
        constructor takes the recovery commands with it, and
        ``docs/security/trust-boundaries.md`` stakes the ``required`` stop on ``plugin untrust``
        being one command away from any refusal. A crash or a full disk between the first byte
        and the last of a write is enough to leave a file that parses nowhere.

        Said out loud at ``error``, because an empty store and a first run look identical from
        the outside and are not the same thing: one of them needs every approval given again.
        The damaged bytes are left where they are - overwriting them is the next ``trust`` or
        ``untrust``'s business, and only after the user has asked for one.

        Recorded on ``unreadable`` as well as logged, because a log line is not what someone
        running a CLI command reads. A caller describing this store has to be able to say which
        of the two happened in its own output; ``untrust`` said "records nothing at all" for
        both, which is true of the parsed store and false of the file the user then goes and
        opens.
        """
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text())
            # Shape checked here rather than at every reader: a file that parses as JSON and is
            # not a table of approvals is as unusable as one that does not parse, and finding
            # that out in `record()` puts the same crash one call further from the explanation.
            if not isinstance(raw, dict) or not all(isinstance(rec, (str, dict)) for rec in raw.values()):
                raise ValueError("not a table of plugin approvals")
        except (OSError, ValueError) as exc:
            self.unreadable = True
            log.error("%s could not be read (%s), so no plugin counts as approved this run. "
                      "Every plugin will report as new until you approve it again: "
                      "picoagent plugin list", self.path, exc)
            return {}
        return raw

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
        """The approval covering this directory, or a rootless one that still matches byte for byte.

        A record written before approvals named a directory can only be found by the plugin's
        name, and that name is a line in the ``plugin.toml`` of whatever is sitting in that
        directory now. Left as a standing name match it answers for every directory that claims
        the name, which is how one old record came to vouch for a plugin nobody had approved.

        So the first directory to ask takes it: the record gains that root and is a record of a
        directory from then on, like every other. The user is not sent back to the prompt for a
        plugin they did approve, and the second directory of that name gets ``new``, which is
        what an approval that was never given looks like. The adoption is not written back,
        because a load is a read and the store belongs to the user's decisions; approving the
        plugin once records the directory on disk.
        """
        root = self.key(manifest.root)
        for record in self.data.values():
            if record.get("root") == root:
                return record
        filed = self.data.get(manifest.name)
        if filed is None or filed.get("root"):
            return {}
        filed["root"] = root
        return filed

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
        """Lines describing what moved since approval, for a human deciding whether to accept.

        Two independent answers to two questions - which files differ, and which commits came
        in - so each is worked out on its own and this reads as the order they are shown in.
        """
        record = self.record(manifest)
        return (_file_change_lines(record.get("files") or {}, plugin_file_hashes(manifest))
                + _commit_change_lines(manifest.root, record.get("commit")))

    def trust(self, manifest: Manifest) -> None:
        """Record this directory's current code as approved, replacing whatever covered it.

        Any earlier record of this directory goes, however it was labelled, so a re-approval
        after a rename leaves one record rather than two disagreeing ones. A rootless record
        filed under this name goes too: this one supersedes it, and leaving both would file two
        records for one approval.

        Refuses outright when the site pinned this plugin and the code in front of it does not
        match - :class:`PluginVerificationError`, and no record written. Approval is the act that
        makes a plugin loadable, so it is the last place a verification policy can still mean
        anything; ``plugin trust <directory>`` reaches here without going through
        :func:`resolve_source`, and a policy that only bound the fetch would be one a user
        walks past by cloning the repository themselves.
        """
        refuse_unverified(manifest.root, self.policy)
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
            # Recorded because the plugin cannot state it once it is gone, and read back for
            # nothing else: `approved_requirements` looks for absences, while a plugin that is
            # present is asked directly. The reason travels with it so the sentence a user reads
            # at a stop is the one they were shown when they approved.
            "required": manifest.required,
            "required_reason": manifest.required_reason,
            "approved_at": int(time.time())}
        self._save()

    def approved_requirements(self) -> list[tuple[str, Path, str]]:
        """Every recorded requirement as ``(name, directory, reason)`` - what must still be there.

        Read for one question only: is a plugin the user approved as required absent altogether?
        Whether a plugin *in front of us* is required is asked of that plugin, not of this list, so
        the store and the manifest cannot disagree about code that is present. The store is
        consulted for the case where there is no manifest left to ask.

        Only records that name a directory. A record from a version that stored none cannot say
        where the code it approved lived, and a bare name cannot distinguish "the plugin is gone"
        from "this project does not use it" - the second would stop sessions with nothing wrong
        with them. Such a record is adopted by the first directory that asks for it (see
        ``record``), and re-approving brings it under this check with a directory of its own.
        """
        return [(record.get("name") or key, Path(record["root"]),
                 record.get("required_reason") or _REQUIRED_UNSTATED)
                for key, record in self.data.items()
                if record.get("required") and record.get("root")]

    def withdraw(self, label: str) -> dict:
        """Remove one approval, rewrite the store, and return the record that was removed.

        Here rather than in the CLI because the shape of the file is this class's to know. The
        store is rewritten whole, which also normalises a record an older version wrote as a bare
        fingerprint string: both forms are read on the way in, so writing one form back loses
        nothing. Takes a label rather than a plugin, because the record that most needs
        withdrawing is the one whose directory is gone and which has no manifest left to load.
        """
        record = self.data.pop(label)
        self._save()
        return record

    def _save(self) -> None:
        """Write the store so that a reader sees either all of it or none of it.

        Overwriting the file in place gives a window in which it holds a prefix of the new
        content and none of the old, and a crash or a full disk inside that window leaves the
        file that every session and both recovery commands open unparseable. The read above
        survives that; it survives it by discarding every approval, which is a real cost to pay
        for a write that went wrong.

        So the new content is written beside the store and renamed over it. ``os.replace`` is
        atomic on POSIX and on Windows, and the temp file is in the same directory so the rename
        stays on one filesystem. ``fsync`` before the rename because the rename can otherwise be
        durable while the bytes it publishes are not: after a power loss the store would name a
        file of zeros. Whatever fails, the previous store is still there and still says what the
        user approved.

        The published file carries ``mkstemp``'s owner-only mode rather than the umask's, which
        is the mode a record of security decisions should have had all along.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=self.path.parent, prefix=".trust-", suffix=".json")
        try:
            with os.fdopen(handle, "w") as fh:
                fh.write(json.dumps(self.data, indent=2))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp, self.path)
        except BaseException:
            # The half-written file is this method's litter, not a state anybody can use, and
            # leaving it behind would put a second trust-shaped file next to the store.
            Path(temp).unlink(missing_ok=True)
            raise

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

    Which is why every module of the plugin is loaded *here*, one component at a time, rather
    than handing a dotted name to ``importlib``. A module this class does not load itself gets
    ordinary builtins, and a plugin that keeps its code in a subpackage then had ``pkg/mod.py``
    resolving its own ``import utils`` against ``sys.path`` - the same crossing, one level down
    and harder to see. Walking the name means the boundary is where the plugin directory is,
    not where the first dot is.

    One spelling stays outside it. ``importlib.import_module`` calls ``_bootstrap._gcd_import``
    and never consults ``__import__``, so nothing installed on a module's builtins can answer
    it; it is served by the standard machinery against ``sys.path``. That is a limitation of
    where this hooks in rather than something to work around, so it is documented for plugin
    authors and pinned by a test instead of being half-closed and called closed.
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
        if level:
            base = self._base(globals or {}, level)
            if base is None:
                return builtins.__import__(name, globals, locals, fromlist, level)
            deepest = self._walk(base, name) if name else sys.modules[base]
            self._attach(deepest, fromlist)
            return deepest
        head = name.partition(".")[0]
        top = self._child(self.package, head) if head.isidentifier() else None
        if top is None:
            return builtins.__import__(name, globals, locals, fromlist, level)
        deepest = self._walk(self.package, name)
        self._attach(deepest, fromlist)
        return deepest if fromlist else top

    def _base(self, globals: dict, level: int) -> str | None:
        """The package a relative import counts down from, or ``None`` when it is not this plugin's.

        Handled for the plugin's subpackages and not only for its top level: ``from . import mod``
        inside ``pkg/`` served by the ordinary machinery produces a module with ordinary builtins,
        and whatever that module imports by a bare name leaves the plugin. Counting above the
        plugin's own package answers ``None`` and lets the real ``__import__`` raise the error it
        already has words for.
        """
        base = globals.get("__package__") or ""
        for _ in range(level - 1):
            base = base.rpartition(".")[0]
        return base if base == self.package or base.startswith(f"{self.package}.") else None

    def _walk(self, base: str, name: str):
        """Import ``base.name`` a component at a time, so every module on the way is loaded here.

        Raises rather than falling back once the first component is the plugin's: a plugin that
        ships ``pkg`` and not ``pkg.mod`` is asking for something that does not exist, and
        answering it from ``sys.path`` would import a stranger's ``pkg.mod`` instead.
        """
        module = sys.modules.get(base)
        for part in name.split("."):
            module = self._child(base, part)
            if module is None:
                raise ModuleNotFoundError(f"plugin package '{base}' has no module '{part}'",
                                          name=f"{base}.{part}")
            base = f"{base}.{part}"
        return module

    def _attach(self, module, fromlist) -> None:
        """Import the modules a ``from X import a, b`` names, which the importer has to do itself.

        ``from pkg import mod`` binds a module that nothing has imported yet: the byte-code looks
        ``mod`` up as an attribute of ``pkg`` and only then falls back to ``sys.modules``, so an
        importer that returns the package alone leaves the name unbound and the plugin sees
        ``cannot import name 'mod'``. Names that are not modules stay plain attributes.
        """
        if not fromlist or not hasattr(module, "__path__"):
            return
        names = list(fromlist)
        if "*" in fromlist:
            names += list(getattr(module, "__all__", ()) or ())
        for item in names:
            if isinstance(item, str) and item != "*" and not hasattr(module, item):
                self._child(module.__name__, item)

    def _child(self, parent: str, head: str):
        """``parent``'s own ``head``, imported on first ask, or ``None`` if it ships no such module.

        A file the plugin ships wins over an installed distribution of the same name, which is
        what the ``sys.path`` insert did too - it went to the front. What changes is that the
        win is now confined to the plugin that shipped the file.
        """
        full = f"{parent}.{head}"
        if full in sys.modules:
            return sys.modules[full]
        for directory in self._locations(parent):
            for source in (directory / f"{head}.py", directory / head / "__init__.py"):
                if source.is_file():
                    return self._load(full, head, source)
        return None

    def _locations(self, parent: str) -> list[Path]:
        """Where ``parent``'s submodules are looked for.

        The entry package answers from ``self.root`` rather than from its ``__path__``, because
        this importer is built to be installed *into* that module and is asked questions while it
        is still being executed. Everything below it has a ``__path__`` by then.
        """
        if parent == self.package:
            return [self.root]
        module = sys.modules.get(parent)
        return [Path(entry) for entry in getattr(module, "__path__", ()) or ()]

    def _load(self, full: str, head: str, source: Path):
        """Execute one module under ``full``, with this plugin's import behaviour in place."""
        locations = [str(source.parent)] if source.name == "__init__.py" else None
        spec = importlib.util.spec_from_file_location(full, source, submodule_search_locations=locations)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot import plugin module {source}")
        module = importlib.util.module_from_spec(spec)
        module.__dict__["__builtins__"] = self.namespace
        sys.modules[full] = module          # before executing, so an import cycle sees the partial
        parent = sys.modules.get(full.rpartition(".")[0])
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

    Both spellings are read from the plugin as it stands on disk, which bounds what this can
    promise. A plugin that is deleted declares nothing, and neither does a replacement that drops
    the line: such a replacement is still refused as code the user never approved, loudly and as
    urgent, but the session goes on. Recording the requirement at approval instead was tried and
    withdrawn. It made the trust store the authority on which plugins must exist, and a store
    that is wrong about that - a plugin the user deliberately deleted, a record from a machine
    whose plugin directory moved - refuses every session, including the one the user would have
    needed to fix it. A control that can brick the tool it protects is not a control anybody
    keeps switched on.

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
            f"picoagent plugin trust {root} - or take the decision back with "
            f"picoagent plugin untrust {name}, which leaves the plugin in place and unapproved.")
        self.name, self.reason, self.root, self.change = name, reason, root, change


class RequiredPluginMissing(RequiredPluginError):
    """A plugin approved as required is not there to run, and nothing took its place.

    The other two subclasses have a directory to look at and a diff to review. This one is the
    absence itself: the checkout was deleted, its ``plugin.toml`` stopped parsing, or its entry no
    longer imports. Replacing approved code stops the session, so removing it has to as well, or
    the shorter way around the check is the one with nothing guarding it. From inside a session, a
    control that is absent is indistinguishable from a control that ran and found nothing, and
    that is the whole reason ``required`` exists.

    This is the only refusal the loader raises about something that is not on disk, which is what
    made an earlier version of it a wedge: it stopped every session and told the user to hand-edit
    ``trust.json``. The message now names ``picoagent plugin untrust``, and that command is
    reachable from exactly this state - it builds a trust store from the config and stops there,
    importing no plugin and building no runtime, so the record doing the stopping is one command
    away from gone. A stop the user can undo without a session is a different thing from a lockout.

    Only for a requirement recorded against a directory inside the user's own plugin directory.
    That is the directory every session of theirs reads, so absence from it means the plugin is
    gone rather than unused in this project. A requirement recorded for a repository's copy is left
    alone for the reason ``RequiredPluginUntrusted`` leaves it alone: ``required`` is a line a
    cloned repository also gets to write, and honouring it there hands any repository a switch that
    stops the user's work.
    """

    def __init__(self, name: str, reason: str, root: Path):
        super().__init__(
            f"required plugin '{name}' did not load and nothing is running in its place, so the "
            f"session is not starting. You approved it at {root} and it was required then: "
            f"{reason}. Put it back, or take the decision back with picoagent plugin untrust "
            f"{name} - that command reads your trust store and nothing else, so it works while "
            "sessions are refusing to start.")
        self.name, self.reason, self.root = name, reason, root


#: Said on a plugin's behalf when its manifest requires it but names no reason of its own.
_REQUIRED_UNSTATED = "it declares itself required, so this session is not running without it"


def _required_reason(manifest: Manifest) -> str:
    """Why the user is being told the session cannot run without this plugin.

    Read from the manifest at both the points that need it - seeding the declaration before the
    import, and refusing a plugin that never got that far - so the sentence the user sees does
    not depend on which of the two spoke.
    """
    return manifest.required_reason or _REQUIRED_UNSTATED


def load_plugin(root: Path, rt: Runtime, trust: TrustStore, *, allow_untrusted: bool = False) -> Manifest | None:
    """Load one plugin directory into ``rt``. Returns its manifest, or ``None`` if refused."""
    manifest = Manifest.load(root)
    if not (allow_untrusted or trust.is_trusted(manifest)):
        log.warning("plugin '%s' is not trusted (new or changed). Run: picoagent plugin trust %s",
                    manifest.name, root)
        return None
    for complaint in unmet_requirements(manifest, __version__):
        # Said before the import, so the author hears it even if `register()` then falls over on
        # the very thing the constraint was about. Said and then passed by: see
        # `unmet_requirements` for why this field does not get to refuse a plugin.
        log.warning("plugin '%s' %s; loading it anyway", manifest.name, complaint)
    api = PluginAPI(rt, manifest.name, manifest.root)
    if manifest.required:
        # Seeded, not checked separately: a manifest declaration and a runtime one are the same
        # statement, so they set the same field and a plugin that spells it both ways gets the
        # more specific wording, whichever it wrote last.
        api.declare_required(_required_reason(manifest))
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

    What did not load matters most: a plugin the user installed and expected to be running is
    now silently absent, and "it changed since you approved it" needs a different response from
    "you never approved it". ``notices`` is where those events are kept, with the wording and
    the urgency attached, because the case worth shouting about - an approved plugin that
    stopped running without the user touching it - is not distinguishable from a reason string
    alone. ``skipped`` is the same events read as bare ``(name, reason, root)``.
    """
    loaded: list[Manifest] = field(default_factory=list)
    notices: list[Notice] = field(default_factory=list)

    @property
    def skipped(self) -> list[tuple[str, str, Path]]:
        """``(name, reason, root)`` for every plugin that did not load.

        Derived, because it used to be kept: two lists appended to side by side in one function,
        holding the same three fields, with nothing but the habit of writing both keeping them in
        step. A skip that reached one list and not the other would be a plugin the report counts
        and says nothing about, or says something about and does not count. The wording and the
        urgency only ever live on the notice, so the notice is the record and this is a view.
        """
        return [(notice.name, notice.reason, notice.root) for notice in self.notices]

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

    Which is why the branches stay side by side here rather than moving behind a table of
    formatters keyed on the reason: what a reader of this function needs to compare is the six
    sentences, and a table separates each sentence from the condition that produces it. Only the
    wording and the urgency ever differ - the name, the reason and the directory are the skip's -
    so ``worded`` fixes those three and each branch is a condition and the sentence it earns.
    """
    hint = f"  Review it and decide:  picoagent plugin trust {skip.root}"
    absent = "whatever it enforces is off for this session."
    off = f"and was NOT LOADED - {absent}"

    def worded(text: str, *, urgent: bool = False) -> Notice:
        return Notice(skip.name, skip.reason, skip.root, text, urgent)

    if skip.reason == "missing":
        return worded(f"plugin '{skip.name}' is one you approved as REQUIRED and nothing loaded "
                      f"from {skip.root} - {absent}\n"
                      f"  Put it back, or withdraw the approval:  picoagent plugin untrust "
                      f"{skip.name}", urgent=True)
    if skip.reason == "new":
        if skip.manifest is not None and skip.manifest.required:
            # A first run is not fatal (see RequiredPluginUntrusted), but a plugin that says the
            # session should not run without it is not ordinary "not trusted yet" chatter either.
            return worded(f"plugin '{skip.name}' declares itself REQUIRED and you have not "
                          f"approved it yet, so it was not loaded - {absent}\n{hint}", urgent=True)
        return worded(f"plugin '{skip.name}' is not trusted yet and was not loaded.\n{hint}")
    if skip.reason == "shadowed":
        return worded(f"plugin '{skip.name}' offered by this repository was not loaded; your own "
                      f"'{skip.name}' at {skip.shadowed_by} is the one running.\n"
                      f"  The repository's copy is at {skip.root}.\n{hint}")
    if skip.reason != "changed":
        return worded(f"plugin '{skip.name}' did not load ({skip.reason}); "
                      f"run with --verbose for the detail.")
    if skip.layer == PROJECT:
        return worded(f"plugin '{skip.name}' offered by this repository is not the version you "
                      f"approved {off}\n  The repository's copy is at {skip.root}.\n{hint}",
                      urgent=True)
    if skip.manifest is not None and trust.change_kind(skip.manifest) == "moved":
        approved = (trust.approved_commit(skip.manifest) or "")[:12]
        current = (plugin_commit(skip.root) or "")[:12]
        return worded(f"plugin '{skip.name}' was MOVED to a revision you have not approved "
                      f"({approved} -> {current}) {off}\n"
                      f"  You did not edit it: something moved its checkout. Check "
                      f"[plugins].enabled in your own config and in this repository's "
                      f".picoagent/config.toml.\n{hint}", urgent=True)
    return worded(f"plugin '{skip.name}' CHANGED since you approved it {off}\n{hint}", urgent=True)


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
    extra_paths = extra_paths or []
    extra = [Path(p).expanduser().resolve() for p in extra_paths]
    trust = TrustStore(Path(rt.cfg["_user_dir"]))
    report = LoadReport()
    found = discover(rt.cfg, extra_paths)

    def will_load(root: Path, manifest: Manifest) -> bool:
        return allow_untrusted or root.resolve() in extra or trust.is_trusted(manifest)

    user_owned = _names_the_user_owns(found, will_load)

    def skip(entry: Skip) -> None:
        notice = _skip_notice(entry, trust)
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
            skip(_refusal(entry, manifest, status, user_owned.get(manifest.name)))
            # After the notice, so the reason the session is stopping has already been recorded
            # and logged the same way every other skip is.
            if _stops_the_session(entry, manifest, status):
                raise RequiredPluginUntrusted(manifest.name, _required_reason(manifest),
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

    Everything above is driven by what is on disk, so a plugin that is no longer on disk is reached
    by none of it: a deleted checkout, a ``plugin.toml`` that stopped parsing, an entry module that
    no longer imports. The trust store is the only party that still remembers the plugin was
    supposed to be running, and a required plugin that vanished leaves exactly the session
    ``required`` exists to prevent - the control absent, and nothing said about it.

    Checked after the load rather than against discovery, because "did it load" is the question the
    user cares about; a directory that was found and then failed is as absent as one that was never
    there. The notice goes out before the raise, so the reason is worded and logged the same way
    every other skip is rather than only living in an exception message.
    """
    loaded = {TrustStore.key(manifest.root) for manifest in report.loaded}
    owned = plugins_dir(cfg)
    for name, root, reason in trust.approved_requirements():
        if TrustStore.key(root) in loaded or not _within(root, owned):
            continue
        skip(Skip(name, "missing", root, USER))
        raise RequiredPluginMissing(name, reason, root)


def _stops_the_session(entry: Discovered, manifest: Manifest, status: str) -> bool:
    """Is this refusal one to end the session over, rather than one to print a line about?

    Three conditions, and each is a carve-out with a reason of its own: ``changed`` and not
    ``new``, because a first run has taken nothing away from anybody; the user's own layers and
    not the repository's, because ``required`` is a line a cloned repository also gets to write;
    and the plugin in front of us saying so, because that is the only place the statement is
    made. ``RequiredPluginUntrusted`` argues the first two, ``RequiredPluginError`` the third.
    """
    return status == "changed" and entry.layer != PROJECT and manifest.required


def _refusal(entry: Discovered, manifest: Manifest, status: str, owner: Path | None) -> Skip:
    """Which kind of not-loading this is, for a plugin the trust check turned away.

    Two of them read the same in the report and mean different things to the user. A repository
    offering a plugin the user already runs is a copy that was never going to load, and saying
    so is not the same as telling them something they approved has changed - see
    ``_names_the_user_owns`` for why a false alarm there is expensive.
    """
    if entry.layer == PROJECT and owner is not None and owner != entry.root:
        return Skip(manifest.name, "shadowed", entry.root, entry.layer, manifest, owner)
    return Skip(manifest.name, status, entry.root, entry.layer, manifest)


def _print_pin_digest(argv: list[str]) -> int:
    """``python3 -m picoagent.plugins.loader <directory>`` - print what a pin must be written to.

    Not a ``picoagent plugin`` verb, and that is a real limitation rather than a preference: the
    people who most need this are a plugin's publisher computing the value for a release note
    and an administrator checking a downloaded copy against it, and neither of them should have
    to guess at the walk. A module entry point is the smallest thing that lets both of them run
    it against a directory without a picoagent config, a project, or a trust store.
    """
    if len(argv) != 1:
        print("usage: python3 -m picoagent.plugins.loader <plugin directory>", file=sys.stderr)
        return 2
    root = Path(argv[0]).expanduser()
    if not root.is_dir():
        print(f"{root} is not a directory", file=sys.stderr)
        return 1
    print(pin_digest(root))
    return 0


if __name__ == "__main__":
    sys.exit(_print_pin_digest(sys.argv[1:]))
