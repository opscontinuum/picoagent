"""Is anything out of date, and can it be moved forward safely.

Everything here goes through the ``git`` binary - ``ls-remote``, ``fetch``, ``merge
--ff-only``, ``rev-list`` - and never through a host's API. That is deliberate: ``ls-remote``
answers "what commit is that ref at" against GitHub, GitLab, Gitea, Bitbucket Server, a bare
SSH remote or a path on disk, identically and without a token. An API client would work for
one host and need rewriting for the next, which is the opposite of what an internal-mirror
deployment needs.

Two rules the design holds to:

**Plugins are updated, the app is only reported on.** picoagent can be a git checkout, a pip
install, or a distro package, and each upgrades differently. Guessing wrong breaks the
install, so this reports how far behind it is and leaves the command to the user.

**An upgrade never auto-approves itself.** Moving a plugin forward changes its trust
fingerprint, which is the point: the next load reports it as CHANGED and the user reviews the
incoming commits before accepting. Upgrading and trusting are separate acts.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .loader import checkout_name, parse_spec, plugins_dir


@dataclass
class Status:
    """Where one checkout stands against its remote."""
    name: str
    root: Path | None          # local checkout, None when it was never cloned
    url: str
    ref: str                   # tracked ref; "" means the remote's default branch
    local: str | None          # commit the checkout is on
    remote: str | None         # commit the remote's ref points at
    behind: int = 0            # commits the checkout is missing, 0 when unknown or current
    error: str | None = None

    @property
    def outdated(self) -> bool:
        return self.error is None and bool(self.local and self.remote and self.local != self.remote)

    def describe(self) -> str:
        if self.error:
            return f"{self.name}: {self.error}"
        if self.root is None:
            return f"{self.name}: not installed yet"
        if not self.outdated:
            return f"{self.name}: up to date"
        count = f"{self.behind} commit{'s' if self.behind != 1 else ''} behind" if self.behind else "behind"
        return f"{self.name}: {count} ({self.local[:8]} -> {self.remote[:8]})"


def _git(root: Path | None, *args: str, timeout: int = 30) -> str | None:
    """Run git, returning stdout or ``None`` on any failure. Never raises."""
    command = ["git"] + (["-C", str(root)] if root else []) + list(args)
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def remote_commit(url: str, ref: str = "") -> str | None:
    """The commit a remote ref points at, via ``git ls-remote``. Works on any git host.

    With no ref this asks for HEAD, which is the remote's default branch - so a spec that
    doesn't pin a branch still gets a meaningful answer.
    """
    output = _git(None, "ls-remote", url, ref or "HEAD")
    if not output:
        return None
    # ls-remote can return several matching refs (a tag and its ^{} peel, say); the first
    # line is the ref itself.
    return output.splitlines()[0].split()[0]


def local_commit(root: Path) -> str | None:
    return _git(root, "rev-parse", "HEAD")


def commits_behind(root: Path, remote: str) -> int:
    """How many commits the checkout is missing. 0 when it cannot be counted locally.

    The remote commit has to be present in the object store to be counted, which it is after
    a fetch and is not before one. An uncountable answer is reported as 0 rather than guessed.
    """
    output = _git(root, "rev-list", "--count", f"HEAD..{remote}")
    return int(output) if output and output.isdigit() else 0


def check(name: str, url: str, ref: str, root: Path | None) -> Status:
    """Compare one checkout to its remote without changing anything."""
    remote = remote_commit(url, ref)
    if remote is None:
        return Status(name, root, url, ref, None, None,
                      error=f"cannot reach {url} (unreachable, or the ref does not exist)")
    if root is None or not (root / ".git").is_dir():
        return Status(name, None, url, ref, None, remote)
    local = local_commit(root)
    behind = commits_behind(root, remote) if local and local != remote else 0
    return Status(name, root, url, ref, local, remote, behind)


def check_plugins(cfg: dict) -> list[Status]:
    """Every git-sourced plugin named in config. Local-path plugins have no remote to check."""
    rewrites = cfg.get("plugins", {}).get("rewrite") or {}
    results = []
    for spec in cfg["plugins"]["enabled"]:
        if not spec.startswith(("git:", "http://", "https://", "git@", "ssh://", "file://")):
            continue                      # a local path; nothing to compare it against
        url, ref = parse_spec(spec, rewrites)
        name = checkout_name(url)
        for base in (plugins_dir(cfg), plugins_dir(cfg, project=True)):
            root = base / name
            if (root / ".git").is_dir():
                break
        else:
            root = None
        results.append(check(name, url, ref, root))
    return results


def check_app(cfg: dict) -> Status | None:
    """picoagent itself, if ``[upgrade].app_repo`` names one and this is a git checkout.

    Reported, never changed. A pip install and a git checkout upgrade differently, and this
    cannot tell which one it is well enough to act.
    """
    spec = cfg.get("upgrade", {}).get("app_repo")
    if not spec:
        return None
    url, ref = parse_spec(spec, cfg.get("plugins", {}).get("rewrite") or {})
    package_root = Path(__file__).resolve().parents[2]
    root = package_root if (package_root / ".git").is_dir() else None
    return check("picoagent", url, ref, root)


def app_upgrade_hint(status: Status) -> str:
    """What the user should run, based on how picoagent is actually installed."""
    if status.root is not None:
        return f"git -C {status.root} pull --ff-only"
    return f"pip install --upgrade {status.url}" if status.url else "pip install --upgrade picoagent"


def upgrade(status: Status) -> tuple[bool, str]:
    """Fast-forward one plugin checkout. Returns ``(changed, message)``.

    Refuses on a dirty tree or a diverged branch rather than resolving either. Losing an
    edit someone was in the middle of, to apply an upgrade they did not ask for yet, is a
    worse outcome than stopping.
    """
    if status.error:
        return False, status.error
    if status.root is None:
        return False, f"{status.name} is not installed; use `picoagent plugin add`"
    if not status.outdated:
        return False, f"{status.name} is already up to date"

    dirty = _git(status.root, "status", "--porcelain")
    if dirty:
        return False, f"{status.name} has uncommitted changes; not touching it"

    if _git(status.root, "fetch", "--tags", "-q") is None:
        return False, f"{status.name}: fetch failed"
    if _git(status.root, "merge", "--ff-only", status.remote) is None:
        return False, (f"{status.name}: cannot fast-forward (the checkout has diverged from "
                       f"the remote); resolve it by hand")
    return True, (f"{status.name}: {status.local[:8]} -> {status.remote[:8]}. It will load as "
                  f"CHANGED until you review it: picoagent plugin trust {status.root}")
