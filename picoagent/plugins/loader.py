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

Trust
-----
Plugin code runs with your full privileges, so nothing loads until you've said yes.
``picoagent plugin add`` shows the manifest and asks; the answer is stored in
``~/.picoagent/trust.json`` as a hash over *every file in the plugin directory*. If any of
them changes - a new version, an edit, or something tampered with - the hash no longer
matches and the plugin is skipped with a warning until you trust it again.

Hashing the whole directory rather than just the entry module is deliberate: the entry
imports its siblings, so a narrower fingerprint let ``helper.py`` be rewritten while the
plugin still reported *trusted*.
"""
from __future__ import annotations

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


#: The layer a spec or a discovered directory came from.
USER, PROJECT, CLI = "user", "project", "cli"


def project_enabled(cfg: dict) -> list[str]:
    """The specs the *repository's* config.toml adds to ``[plugins].enabled``.

    Read from the file rather than from the merged config, because the merge deliberately
    loses the distinction: the two lists are concatenated so a repository can suggest a
    plugin, and after that nothing downstream can say which entries the repository wrote.
    """
    try:
        with (Path(cfg["_cwd"]) / ".picoagent" / "config.toml").open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return []
    plugins = data.get("plugins")
    enabled = plugins.get("enabled") if isinstance(plugins, dict) else None
    return [spec for spec in enabled if isinstance(spec, str)] if isinstance(enabled, list) else []


def enabled_by_layer(cfg: dict) -> list[tuple[str, str]]:
    """``[plugins].enabled`` paired with the layer each spec came from.

    ``load_config`` builds the list user-first, so the repository's specs are its tail. A
    config assembled some other way - a test, an embedder wiring its own ``Runtime`` - is
    attributed by membership instead, which errs towards calling a spec the repository's:
    that is the safe direction, because that is the one that may not write.
    """
    enabled = list(cfg.get("plugins", {}).get("enabled") or [])
    project = project_enabled(cfg)
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
    """What the user approved, per plugin.

    Deliberately stores more than a fingerprint. A bare hash can only say *that* something
    changed, which leaves the user with one blunt option - re-approve and hope. Per-file
    hashes name the file that moved, and the commit (for a git checkout) lets the CLI show
    the incoming commits before asking. Records written by older versions are a plain
    fingerprint string; they still load, and simply can't describe a change in detail.
    """

    def __init__(self, user_dir: Path):
        self.path = user_dir / "trust.json"
        raw = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.data: dict[str, dict] = {name: {"fingerprint": rec} if isinstance(rec, str) else rec
                                      for name, rec in raw.items()}

    def is_trusted(self, manifest: Manifest) -> bool:
        record = self.data.get(manifest.name)
        return bool(record) and record.get("fingerprint") == plugin_fingerprint(manifest)

    def status(self, manifest: Manifest) -> str:
        """``trusted`` (approved, unchanged), ``changed`` (approved, but not this version),
        or ``new`` (never approved). ``changed`` is the interesting one: it means code the
        user vetted has been replaced by code they haven't."""
        if manifest.name not in self.data:
            return "new"
        return "trusted" if self.is_trusted(manifest) else "changed"

    def approved_commit(self, manifest: Manifest) -> str | None:
        """The commit the user approved, when the record has one."""
        return (self.data.get(manifest.name) or {}).get("commit")

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
        record = self.data.get(manifest.name) or {}
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
        self.data[manifest.name] = {"fingerprint": plugin_fingerprint(manifest),
                                    "files": plugin_file_hashes(manifest),
                                    "commit": plugin_commit(manifest.root),
                                    "approved_at": int(time.time())}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2))


# ------------------------------------------------------------------------ loading

def _import_register(manifest: Manifest):
    """Import the entry module from its file and return the ``register`` callable."""
    path = manifest.entry_path()
    spec = importlib.util.spec_from_file_location(f"picoagent_plugin_{manifest.name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot import plugin entry {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    if str(manifest.root) not in sys.path:     # let the plugin import its sibling modules
        sys.path.insert(0, str(manifest.root))
    spec.loader.exec_module(module)
    return getattr(module, manifest.entry_function)


class RequiredPluginFailed(RuntimeError):
    """A plugin that declared itself required did not finish registering.

    Skipping is the loader's normal answer to a failing ``register()``, and for most plugins it
    is the right one: a broken formatter should not stop a session. For a plugin that *is* a
    security control it is the wrong one, because the session then runs with the control absent
    and one line on stderr to say so - the difference between "the guard refused this command"
    and "there is no guard" is invisible from inside the session.

    The loader cannot tell the two kinds of plugin apart, so the plugin says which it is:
    ``api.declare_required(reason)`` as the first statement of ``register()``. Anything that
    fails after that stops startup rather than being noted and passed over.
    """

    def __init__(self, name: str, reason: str, cause: BaseException):
        super().__init__(f"required plugin '{name}' failed to load: {cause}. {reason}. "
                         "Fix what it objected to, or remove it from [plugins].enabled if you "
                         "no longer want it - it will not be skipped silently.")
        self.name, self.reason, self.cause = name, reason, cause


def load_plugin(root: Path, rt: Runtime, trust: TrustStore, *, allow_untrusted: bool = False) -> Manifest | None:
    """Load one plugin directory into ``rt``. Returns its manifest, or ``None`` if refused."""
    manifest = Manifest.load(root)
    if not (allow_untrusted or trust.is_trusted(manifest)):
        log.warning("plugin '%s' is not trusted (new or changed). Run: picoagent plugin trust %s",
                    manifest.name, root)
        return None
    register = _import_register(manifest)
    api = PluginAPI(rt, manifest.name, manifest.root)
    try:
        register(api)
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
    reason: str           # new | changed | shadowed | invalid | failed
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
    reason: str            # new | changed | shadowed | invalid | failed
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
    off = "and was NOT LOADED - whatever it enforces is off for this session."
    if skip.reason == "new":
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
        (log.error if notice.urgent else log.warning)("%s", notice.text)

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
    return report
