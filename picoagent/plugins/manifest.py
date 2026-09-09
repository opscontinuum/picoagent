"""``plugin.toml`` - the small manifest every plugin repo carries at its root.

Example::

    name = "permission-gate"
    version = "0.1.0"
    entry = "permission_gate:register"   # module path (relative to the repo) : function
    description = "Ask before destructive commands"
    python_deps = []                      # pip-installed on `picoagent plugin add`
    skills = ["skills"]                   # directories of SKILL.md to expose
    requires = ["picoagent>=0.1"]         # warned about when this picoagent does not meet it
    required = true                       # this session should not run without me
    required_reason = "the only check on destructive commands"

``requires`` is read at load time by :func:`unmet_requirements` and answered with a warning
naming the plugin, the constraint and the running version - never with a refusal. It was
informational for a long time, which means eight shipped manifests wrote it against nothing
that checked it, so the first reader of a field like that meets declarations nobody has tested.
Warning tells the author what is wrong; refusing would invent a new way for a user to lose a
plugin over a line they may not have written.
"""
from __future__ import annotations

import operator
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..core.text import describe_exception

#: A ``requires`` entry: a name, and optionally a comparison against a numeric version. This is
#: the grammar the shipped manifests write and the one the docs show, and no more of PEP 508 than
#: that - a grammar nothing writes is a grammar nothing has ever checked.
_REQUIREMENT = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)"
                          r"(?:\s*(?P<op><=|>=|==|!=|<|>)\s*(?P<version>[0-9]+(?:\.[0-9]+)*))?$")

_COMPARE: dict[str, Callable[[tuple[int, ...], tuple[int, ...]], bool]] = {
    ">=": operator.ge, ">": operator.gt, "<=": operator.le,
    "<": operator.lt, "==": operator.eq, "!=": operator.ne,
}


class ManifestError(Exception):
    """A ``plugin.toml`` that cannot be turned into a :class:`Manifest`, worded for a person.

    One type for every way the read can fail, because every caller is asking the same question -
    *can I have this plugin's manifest?* - and none of them has a different answer for "the file
    is not there" than for "the file is not UTF-8". Enumerating the exception types the read can
    produce was the previous shape and it did not hold: ``plugin add`` and ``plugin trust`` caught
    ``FileNotFoundError`` and ``KeyError``, and a manifest that was not UTF-8 raised
    ``UnicodeDecodeError`` past both of them. A manifest arrives with a clone, so the list of ways
    it can fail is chosen by whoever wrote it, and a caller cannot be asked to keep up with that.
    """


def _string_list(value: object) -> list[str]:
    """The strings in ``value``, or an empty list if it is not a list of them.

    Forgiving rather than refusing, because these fields are names of things to fetch or expose
    and a value of the wrong shape names none of them: ``python_deps = 3`` asks for no package,
    and a table in ``skills`` names no directory. Dropping is also the safe direction - what falls
    out is something not installed, not something installed unasked. Refused instead, a stray
    value in an optional list would cost the user the whole plugin.
    """
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


@dataclass
class Manifest:
    name: str
    entry: str
    version: str = "0.0.0"
    description: str = ""
    python_deps: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    #: Version constraints on picoagent itself, read by :func:`unmet_requirements` and reported
    #: as a warning at load time. Not a gate: see that function for why it cannot become one.
    requires: list[str] = field(default_factory=list)
    #: Declared here rather than only through ``api.declare_required`` because the loader has to
    #: know before it runs anything. A plugin the trust check refuses never reaches ``register()``,
    #: so a declaration made inside ``register()`` cannot cover the one case where the plugin is
    #: absent and nobody chose that. See ``loader.RequiredPluginError``.
    required: bool = False
    required_reason: str = ""
    root: Path = Path(".")

    @property
    def entry_module(self) -> str:
        return self.entry.partition(":")[0]

    @property
    def entry_function(self) -> str:
        return self.entry.partition(":")[2] or "register"

    def entry_path(self) -> Path:
        """Filesystem path of the entry module (``foo.bar`` -> ``foo/bar.py`` or ``foo/bar/__init__.py``)."""
        relative = self.entry_module.replace(".", "/")
        candidate = self.root / f"{relative}.py"
        return candidate if candidate.exists() else self.root / relative / "__init__.py"

    @staticmethod
    def load(root: Path) -> "Manifest":
        """Parse ``<root>/plugin.toml``, or raise :class:`ManifestError` saying why it could not.

        Read as bytes and parsed rather than ``read_text()`` and parsed: the second decodes with
        the platform's default encoding before ``tomllib`` sees anything, so a file that is not
        UTF-8 - what a Windows editor writes when somebody re-saves this one, and what a
        repository would commit on purpose - failed inside ``pathlib`` rather than inside the
        parser, out of reach of anything watching for a parse error.

        The catch is broad because the file is content: a repository's ``.picoagent/plugins``
        chooses these bytes, so it also chooses which exception the parser raises. ``MemoryError``
        is re-raised - the interpreter is out of memory, which is not a fact about this manifest.
        """
        path = root / "plugin.toml"
        if not path.exists():
            raise ManifestError(f"{root} has no plugin.toml")
        try:
            with path.open("rb") as fh:
                data = tomllib.load(fh)
        except MemoryError:
            raise
        except Exception as exc:  # noqa: BLE001 - a file a clone brought may fail any way it likes
            raise ManifestError(f"{path} could not be read: {describe_exception(exc)}") from None
        return Manifest._from_data(path, data)

    @staticmethod
    def _from_data(path: Path, data: dict) -> "Manifest":
        """Build a manifest from parsed TOML, refusing what the rest of the loader cannot use.

        ``name`` and ``entry`` are the two the loader has no default for: the first is what an
        approval is filed against and what every message about this plugin calls it, the second is
        the module it imports. Missing or of the wrong type, they used to arrive as a ``KeyError``
        two callers caught and a third did not, or as an ``int`` that reached a trust record and a
        format string.
        """
        for key in ("name", "entry"):
            if not isinstance(data.get(key), str) or not data[key].strip():
                raise ManifestError(f"{path} has no usable '{key}': a plugin manifest needs "
                                    "'name' and 'entry', both non-empty strings")
        version, description = data.get("version", "0.0.0"), data.get("description", "")
        reason = data.get("required_reason")
        return Manifest(name=data["name"], entry=data["entry"],
                        version=version if isinstance(version, str) else "0.0.0",
                        description=description if isinstance(description, str) else "",
                        python_deps=_string_list(data.get("python_deps")),
                        skills=_string_list(data.get("skills")),
                        requires=_string_list(data.get("requires")),
                        # `is True` rather than a truth test: this field decides whether a session
                        # refuses to start, so `required = "no"` must not read as yes.
                        required=data.get("required") is True,
                        required_reason=reason if isinstance(reason, str) else "",
                        root=path.parent)


def _release(version: str) -> tuple[int, ...] | None:
    """``"0.1.0"`` -> ``(0, 1, 0)``; ``None`` for anything that is not plain dotted numbers."""
    parts = version.split(".")
    return tuple(int(part) for part in parts) if all(part.isdigit() for part in parts) else None


def _padded(release: tuple[int, ...], width: int) -> tuple[int, ...]:
    """Trailing zeros, so ``0.1`` and ``0.1.0`` compare as the same version rather than as
    a shorter tuple that sorts below the longer one."""
    return release + (0,) * (width - len(release))


def _satisfies(running: str, op: str | None, wanted: str) -> bool:
    """Whether ``running`` meets ``<op><wanted>``.

    True whenever there is nothing to compare: a bare name asks for any version at all, and a
    running version that is not plain numbers is picoagent's own doing - a plugin author is not
    the person to tell about it.
    """
    if op is None:
        return True
    left = _release(running)
    if left is None:
        return True
    right = _release(wanted) or ()
    width = max(len(left), len(right))
    return _COMPARE[op](_padded(left, width), _padded(right, width))


def unmet_requirements(manifest: Manifest, running: str) -> list[str]:
    """What is worth saying about ``manifest.requires``, as sentences about this plugin.

    Two kinds of entry earn a line: one the running picoagent does not satisfy, and one that
    cannot be read as a constraint on picoagent at all. The second is a value the author wrote
    and nothing has ever answered, so it is reported rather than dropped - a constraint that is
    silently ignored is indistinguishable from one that passed.

    Neither is a refusal, and that is the decision rather than an omission. ``requires`` shipped
    as informational; eight manifests wrote it against nothing, so the first check on it meets
    declarations no one has tested. Refusing on that would invent a way for a user to lose a
    plugin over a line they may not have written - possibly the plugin that carries the fix.
    A warning names the mismatch to the person who can correct it, which is the whole of what
    this field has standing to do.
    """
    complaints: list[str] = []
    for entry in manifest.requires:
        match = _REQUIREMENT.match(entry.strip())
        if match is None or match["name"].lower() != "picoagent":
            complaints.append(f"declares requires = {entry!r}, which is not a constraint on "
                              "picoagent's own version (a package your code imports belongs in "
                              "python_deps)")
        elif not _satisfies(running, match["op"], match["version"]):
            complaints.append(f"requires {entry!r} and this is picoagent {running}")
    return complaints
