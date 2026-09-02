"""Finding, installing, trusting and loading plugins.

Where plugins come from
-----------------------
* ``[plugins].enabled`` in config: ``git:github.com/user/repo@tag``, ``https://...git@ref``,
  or a local path (relative to the project).
* Anything already sitting in ``~/.picoagent/plugins/`` or ``<project>/.picoagent/plugins/``.
* ``-e path`` on the command line (trusted for that run only).

Load order is exactly that: user config, project config, user dir, project dir, CLI.
Because registrations override by name, a project-level plugin beats a user-level one.

Trust
-----
Plugin code runs with your full privileges, so nothing loads until you've said yes.
``picoagent plugin add`` shows the manifest and asks; the answer is stored as a hash
of ``plugin.toml`` + the entry module in ``~/.picoagent/trust.json``. If either file
changes (a new version, or something tampered with it) the hash no longer matches
and the plugin is skipped with a warning until you trust it again.
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
from dataclasses import dataclass, field
from pathlib import Path

from ..core.loop import Runtime
from .api import PluginAPI
from .manifest import Manifest

log = logging.getLogger("picoagent.plugins")
_GIT_SPEC = re.compile(r"^(?:git:|https?://|git@)")


# ------------------------------------------------------------------------ locations

def plugins_dir(cfg: dict, project: bool = False) -> Path:
    """``~/.picoagent/plugins`` or ``<project>/.picoagent/plugins``."""
    base = Path(cfg["_cwd"]) / ".picoagent" if project else Path(cfg["_user_dir"])
    return base / "plugins"


def resolve_source(spec: str, cfg: dict, project: bool = False) -> Path:
    """Turn a plugin spec into a local directory, cloning git sources on first use."""
    if _GIT_SPEC.match(spec):
        return _clone_or_update(spec, plugins_dir(cfg, project))
    path = Path(spec).expanduser()
    return path if path.is_absolute() else Path(cfg["_cwd"]) / path


def _clone_or_update(spec: str, dest_root: Path) -> Path:
    """Clone ``spec`` (``git:host/user/repo@ref``) under ``dest_root`` and check out ``ref``."""
    url, _, ref = spec.partition("@")
    url = url.removeprefix("git:")
    if not url.startswith(("http", "git@")):
        url = "https://" + url
    name = url.rstrip("/").split("/")[-1].removesuffix(".git")
    dest = dest_root / name
    dest_root.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        subprocess.run(["git", "-C", str(dest), "fetch", "--tags", "-q"], check=False)
    else:
        subprocess.run(["git", "clone", "-q", url, str(dest)], check=True)
    if ref:
        subprocess.run(["git", "-C", str(dest), "checkout", "-q", ref], check=True)
    return dest


def install_deps(manifest: Manifest) -> None:
    """pip-install a plugin's declared ``python_deps`` (no-op when empty)."""
    if manifest.python_deps:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", *manifest.python_deps], check=False)


# ------------------------------------------------------------------------ trust

def plugin_files(manifest: Manifest) -> list[Path]:
    """The files a trust decision covers: the manifest and the entry module."""
    files = [manifest.root / "plugin.toml"]
    entry = manifest.entry_path()
    if entry.exists():
        files.append(entry)
    return files


def plugin_fingerprint(manifest: Manifest) -> str:
    """sha256 over the manifest and entry module - the thing the user actually approved."""
    digest = hashlib.sha256()
    for path in plugin_files(manifest):
        digest.update(path.read_bytes())
    return digest.hexdigest()


def plugin_file_hashes(manifest: Manifest) -> dict[str, str]:
    """Per-file digests, so a re-approval can say *which* file moved, not just that one did."""
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in plugin_files(manifest)}


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


def load_plugin(root: Path, rt: Runtime, trust: TrustStore, *, allow_untrusted: bool = False) -> Manifest | None:
    """Load one plugin directory into ``rt``. Returns its manifest, or ``None`` if refused."""
    manifest = Manifest.load(root)
    if not (allow_untrusted or trust.is_trusted(manifest)):
        log.warning("plugin '%s' is not trusted (new or changed). Run: picoagent plugin trust %s",
                    manifest.name, root)
        return None
    register = _import_register(manifest)
    register(PluginAPI(rt, manifest.name, manifest.root))
    for skills_dir in manifest.skills:
        rt.skills.add_dir(manifest.root / skills_dir, source=f"plugin:{manifest.name}")
    log.info("loaded plugin %s %s", manifest.name, manifest.version)
    return manifest


def discover_roots(cfg: dict, extra_paths: list[str]) -> list[Path]:
    """All plugin directories in load order (see module docstring)."""
    roots: list[Path] = []
    for spec in cfg["plugins"]["enabled"]:
        try:
            roots.append(resolve_source(spec, cfg))
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("cannot resolve plugin %s: %s", spec, exc)
    for directory in (plugins_dir(cfg), plugins_dir(cfg, project=True)):
        if directory.is_dir():
            roots += [p for p in sorted(directory.iterdir()) if (p / "plugin.toml").exists() and p not in roots]
    roots += [Path(p).expanduser().resolve() for p in extra_paths]
    return roots


@dataclass
class LoadReport:
    """What happened during startup, so the CLI can tell the user rather than only logging it.

    ``skipped`` matters most: a plugin the user installed and expected to be running is now
    silently absent, and "it changed since you approved it" needs a different response from
    "you never approved it".
    """
    loaded: list[Manifest] = field(default_factory=list)
    skipped: list[tuple[str, str, Path]] = field(default_factory=list)   # (name, reason, root)

    def needs_review(self) -> list[tuple[str, str, Path]]:
        return [entry for entry in self.skipped if entry[1] == "changed"]


def load_all(rt: Runtime, extra_paths: list[str] | None = None,
             allow_untrusted: bool = False) -> LoadReport:
    """Load every discovered plugin. CLI ``-e`` paths are implicitly trusted for this run."""
    extra = [Path(p).expanduser().resolve() for p in (extra_paths or [])]
    trust = TrustStore(Path(rt.cfg["_user_dir"]))
    report = LoadReport()
    for root in discover_roots(rt.cfg, extra_paths or []):
        implicitly_trusted = allow_untrusted or root.resolve() in extra
        try:
            manifest = Manifest.load(root)
        except Exception as exc:  # noqa: BLE001 - a broken manifest must not stop the others
            log.error("cannot read plugin at %s: %s", root, exc)
            report.skipped.append((str(root), "invalid", root))
            continue
        if not implicitly_trusted and not trust.is_trusted(manifest):
            status = trust.status(manifest)   # "changed" or "new"
            log.warning("plugin '%s' not loaded: %s. Run: picoagent plugin trust %s",
                        manifest.name, status, root)
            report.skipped.append((manifest.name, status, root))
            continue
        try:
            load_plugin(root, rt, trust, allow_untrusted=True)   # trust already decided above
        except Exception as exc:  # noqa: BLE001 - one bad plugin must not stop the others
            log.exception("failed to load plugin at %s: %s", root, exc)
            report.skipped.append((manifest.name, "failed", root))
            continue
        report.loaded.append(manifest)
    return report
