"""Repository probes: gather *evidence* for a STIG rule, never a determination.

Four probe kinds and no fifth
-----------------------------
``grep``, ``exists``, ``manifest`` and ``ci`` (see :class:`Probe`). Adding a fifth is a design
change, not a config change, because each kind is a promise about what the probe can and
cannot conclude, and the skills document those promises to the model. Everything is
``pathlib`` and ``re``: no ``subprocess``, no ``git``, no network, nothing that could execute
repository content. A probe that finds ``verify=False`` has found a string in a file; whether
that string means the rule is Open is a human's call, and the tool layer says so in its output.

Path containment
----------------
Probes run against a repository root the model supplied, so containment is the security
property that matters. :func:`walk` resolves the root once and then, for every candidate,
resolves the path and requires ``is_relative_to(root)``. That check runs on the *resolved*
path, so a symlink pointing outside the tree is skipped no matter how it was reached, and
``os.walk`` is called with ``followlinks=False`` so a symlinked directory is never descended
into. Callers still resolve the root itself through picoagent's ``resolve_path`` first, which
is what honours a deployment's ``confine_to_project``; this module's job is the second fence,
inside the root.

Caps, so a probe cannot be turned into a denial of service by the repository it reads: files
over 1 MiB are skipped, so are files with a NUL byte in their first 8 KiB (binary), so are the
directories in :data:`SKIP_DIRS`, and the walk stops after :data:`MAX_FILES` files with a note
saying it did.
"""
from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

#: Directories never walked: version-control internals and vendored or generated trees. ``.git``
#: matters most - it holds credentials in ``config`` and every deleted secret in its objects.
SKIP_DIRS = frozenset({".git", ".hg", ".svn", "node_modules", "vendor", "dist", "build",
                       "__pycache__", ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache",
                       "target", ".idea", ".gradle"})

MAX_FILE_BYTES = 1024 * 1024      #: files larger than this are skipped, not read
MAX_FILES = 20_000                #: total files examined before the walk gives up
BINARY_SNIFF_BYTES = 8192         #: a NUL in this prefix means "binary, skip"
EXCERPT_CHARS = 200               #: per-hit excerpt cap, before masking

#: Package-manager markers: ``manifest`` glob -> lockfile globs that would pin it.
MANIFESTS = {
    "requirements*.txt": ("requirements*.lock", "*.txt.lock"),
    "pyproject.toml": ("poetry.lock", "uv.lock", "pdm.lock", "requirements.lock"),
    "package.json": ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json"),
    "pom.xml": (),
    "build.gradle": ("gradle.lockfile",),
    "build.gradle.kts": ("gradle.lockfile",),
    "go.mod": ("go.sum",),
    "Cargo.toml": ("Cargo.lock",),
    "*.csproj": ("packages.lock.json",),
    "Gemfile": ("Gemfile.lock",),
    "composer.json": ("composer.lock",),
}

#: Pipeline definitions the ``ci`` probe looks for.
CI_FILES = (".github/workflows/*.yml", ".github/workflows/*.yaml", ".gitlab-ci.yml",
            "azure-pipelines.yml", "Jenkinsfile", ".circleci/config.yml",
            "bitbucket-pipelines.yml", ".drone.yml", "cloudbuild.yaml")

#: Tools whose presence in a pipeline file is evidence of a security gate in the build.
SECURITY_TOOLS = re.compile(
    r"codeql|semgrep|sonar|snyk|dependabot|trivy|grype|bandit|owasp|dependency-check|"
    r"checkmarx|fortify|veracode|gitleaks|trufflehog|npm audit|pip-audit|govulncheck|cargo audit",
    re.I)

#: A name that looks like a credential, followed by its value. Used only for masking output.
_SECRET_ASSIGNMENT = re.compile(
    r"""((?:pass(?:word|wd)?|secret|token|credential|api[_-]?key|access[_-]?key|private[_-]?key)
         \s*[:=]\s*)(["']?)([^"'\s,;)}\]]{4,})""",
    re.IGNORECASE | re.VERBOSE)


@dataclass(frozen=True)
class Probe:
    """One automated check, and the Check_Content sentence it exists to serve.

    ``serves`` is not decoration: it is what lets the model (and a reviewer reading the finding
    details later) tell which part of the check procedure this probe covers - and, by omission,
    which parts it does not.
    """
    kind: str                              #: grep | exists | manifest | ci
    serves: str                            #: the Check_Content sentence this addresses
    pattern: str = ""                      #: regex source, for ``grep``
    globs: tuple[str, ...] = ()            #: filename patterns, for ``grep`` and ``exists``
    ignore_case: bool = True

    def describe(self) -> str:
        if self.kind == "grep":
            return f"grep /{self.pattern}/ over {', '.join(self.globs) or 'all files'}"
        if self.kind == "exists":
            return f"exists {', '.join(self.globs)}"
        return {"manifest": "dependency manifests and lockfiles",
                "ci": "build pipeline definitions and security gates"}[self.kind]


@dataclass
class Hit:
    """One piece of evidence: where it is, and enough text to judge it by."""
    path: str          #: repository-relative, POSIX separators, so output is stable on Windows
    line: int          #: 1-based; 0 when the hit is a file's existence rather than its content
    excerpt: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.excerpt}" if self.line else f"{self.path}: {self.excerpt}"


@dataclass
class ProbeResult:
    probe: Probe
    hits: list[Hit] = field(default_factory=list)
    truncated: bool = False        #: more hits existed than ``max_hits`` allowed


@dataclass
class ScanResult:
    results: list[ProbeResult] = field(default_factory=list)
    files_scanned: int = 0
    skipped_large: int = 0
    skipped_binary: int = 0
    skipped_outside: int = 0       #: symlinks resolving outside the root
    cap_reached: bool = False


class ContainmentError(Exception):
    """The repository root itself is unusable - missing, not a directory, or unresolvable."""


# --------------------------------------------------------------------------- containment

def _inside(root: Path, candidate: Path) -> bool:
    """Is ``candidate`` inside ``root`` once both are fully resolved?

    Resolving first is the whole point: it collapses ``..`` and follows symlinks, so a link
    inside the tree that points at ``/etc`` fails this check even though its literal path is a
    child of the root.
    """
    try:
        return candidate.resolve().is_relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return False               # unresolvable (broken link, loop, permission) => not inside


def resolve_root(raw: str | Path) -> Path:
    """Resolve a repository root, or say why it cannot be used."""
    root = Path(raw).expanduser()
    try:
        resolved = root.resolve()
    except (OSError, RuntimeError) as exc:
        raise ContainmentError(f"cannot resolve {raw}: {exc}") from exc
    if not resolved.is_dir():
        raise ContainmentError(f"{resolved} is not a directory")
    return resolved


def walk(root: Path, scan: ScanResult) -> list[Path]:
    """Every readable text file inside ``root``, with the caps and skips this module promises.

    ``scan`` is updated in place with the skip counters so callers can report what was not
    looked at - "no hits" and "we never opened the file" are different answers.
    """
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        keep = []
        for name in sorted(dirnames):
            if name in SKIP_DIRS:
                continue
            if not _inside(root, current / name):
                scan.skipped_outside += 1      # a symlinked directory leaving the tree
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            if len(files) >= MAX_FILES:
                scan.cap_reached = True
                return files
            path = current / name
            if not _inside(root, path):
                scan.skipped_outside += 1
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    scan.skipped_large += 1
                    continue
            except OSError:
                continue
            files.append(path)
    return files


def read_text(path: Path, scan: ScanResult) -> str | None:
    """File contents, or ``None`` when it is binary or unreadable (both counted, not raised)."""
    try:
        with path.open("rb") as handle:
            prefix = handle.read(BINARY_SNIFF_BYTES)
            if b"\x00" in prefix:
                scan.skipped_binary += 1
                return None
            rest = handle.read()
    except OSError:
        return None
    return (prefix + rest).decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- output hygiene

def mask_secrets(text: str) -> str:
    """Keep the first four characters of a credential-shaped value, mask the rest.

    Evidence has to be specific enough to act on - which file, which line, which setting - but
    a tool result is appended to the session and sent back to the model on the next turn, so
    the value itself must not travel. Four characters is enough to recognise a value you are
    looking at in the file, and not enough to reuse it.
    """
    def replace(match: re.Match) -> str:
        prefix, quote, value = match.group(1), match.group(2), match.group(3)
        return f"{prefix}{quote}{value[:4]}****"
    return _SECRET_ASSIGNMENT.sub(replace, text)


def excerpt(line: str) -> str:
    """One line, whitespace-collapsed, capped and masked - in that order."""
    collapsed = " ".join(line.split())
    if len(collapsed) > EXCERPT_CHARS:
        collapsed = collapsed[:EXCERPT_CHARS] + "…"
    return mask_secrets(collapsed)


def _matches_globs(relative: str, name: str, globs: tuple[str, ...]) -> bool:
    """A file matches when any glob matches its name or its repository-relative path."""
    if not globs:
        return True
    return any(fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(relative, pattern)
               for pattern in globs)


# --------------------------------------------------------------------------- the probes

def run_probes(root: Path, probes: list[Probe], max_hits: int = 20) -> ScanResult:
    """Run every probe over one walk of the repository.

    One walk, not one per probe: the file list and the file contents are shared, so adding a
    probe to a rule costs a regex, not another traversal.
    """
    scan = ScanResult(results=[ProbeResult(probe) for probe in probes])
    files = walk(root, scan)
    relatives = {path: path.relative_to(root).as_posix() for path in files}
    scan.files_scanned = len(files)

    grep_probes = [(result, re.compile(result.probe.pattern,
                                       re.IGNORECASE if result.probe.ignore_case else 0))
                   for result in scan.results if result.probe.kind == "grep"]
    exists_probes = [result for result in scan.results if result.probe.kind == "exists"]

    for result in exists_probes:
        for path in files:
            if _matches_globs(relatives[path], path.name, result.probe.globs):
                _add(result, Hit(relatives[path], 0, "present"), max_hits)

    if grep_probes:
        for path in files:
            relative = relatives[path]
            wanted = [(result, regex) for result, regex in grep_probes
                      if _matches_globs(relative, path.name, result.probe.globs)
                      and not _complete(result, max_hits)]
            if not wanted:
                continue
            content = read_text(path, scan)
            if content is None:
                continue
            for number, line in enumerate(content.splitlines(), 1):
                for result, regex in wanted:
                    if not _complete(result, max_hits) and regex.search(line):
                        _add(result, Hit(relative, number, excerpt(line)), max_hits)

    for result in scan.results:
        if result.probe.kind == "manifest":
            _probe_manifests(root, files, relatives, result, scan, max_hits)
        elif result.probe.kind == "ci":
            _probe_ci(root, files, relatives, result, scan, max_hits)
    return scan


def _complete(result: ProbeResult, max_hits: int) -> bool:
    return len(result.hits) >= max_hits


def _add(result: ProbeResult, hit: Hit, max_hits: int) -> None:
    if len(result.hits) >= max_hits:
        result.truncated = True
        return
    result.hits.append(hit)


def _probe_manifests(root: Path, files: list[Path], relatives: dict[Path, str],
                     result: ProbeResult, scan: ScanResult, max_hits: int) -> None:
    """Which package managers are in use, how many dependencies, and whether a lockfile pins them.

    An unlocked manifest is the evidence a reviewer wants for the patching rules: it means the
    build does not resolve to the same dependency versions twice.
    """
    names = {relatives[path]: path for path in files}
    for manifest_glob, lock_globs in MANIFESTS.items():
        for relative, path in sorted(names.items()):
            if not fnmatch.fnmatch(path.name, manifest_glob):
                continue
            content = read_text(path, scan)
            if content is None:
                continue
            directory = str(Path(relative).parent)
            locks = sorted(other for other in names
                           if str(Path(other).parent) == directory
                           and any(fnmatch.fnmatch(Path(other).name, glob) for glob in lock_globs))
            summary = (f"{_dependency_count(path.name, content)} declared dependencies; "
                       + (f"lockfile: {', '.join(locks)}" if locks
                          else "NO lockfile beside it - dependency versions are not pinned"))
            _add(result, Hit(relative, 0, summary), max_hits)


#: Rough per-ecosystem "how many dependencies" patterns, keyed by the shape of the manifest.
#: Everything but requirements.txt is approximate, and the output says so with a leading "~".
_JSON_ENTRY = re.compile(r'^\s*"[^"]+"\s*:\s*"[^"]*"', re.M)
_GO_REQUIRE = re.compile(r"^\s+\S+\s+v\S+", re.M)
_KEY_VALUE = re.compile(r"^\s*[A-Za-z0-9_.\-]+\s*[=:]", re.M)


def _dependency_count(name: str, content: str) -> str:
    """A rough count, honestly labelled. Exact parsing per ecosystem is not this tool's job."""
    if name.startswith("requirements") and name.endswith(".txt"):
        exact = len([line for line in content.splitlines()
                     if line.strip() and not line.lstrip().startswith("#")])
        return str(exact)
    pattern = _JSON_ENTRY if name.endswith(".json") else _GO_REQUIRE if name == "go.mod" else _KEY_VALUE
    return "~" + str(len(pattern.findall(content)))


def _probe_ci(root: Path, files: list[Path], relatives: dict[Path, str],
              result: ProbeResult, scan: ScanResult, max_hits: int) -> None:
    """Pipeline definitions, and whether any of them names a SAST or dependency scanner."""
    for path in files:
        relative = relatives[path]
        if not any(fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(path.name, pattern)
                   for pattern in CI_FILES):
            continue
        content = read_text(path, scan)
        if content is None:
            continue
        tools = sorted({match.group(0).lower() for match in SECURITY_TOOLS.finditer(content)})
        summary = (f"pipeline; security tooling named: {', '.join(tools)}" if tools
                   else "pipeline; NO SAST or dependency-scanning step named in it")
        _add(result, Hit(relative, 0, summary), max_hits)
