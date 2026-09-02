"""``plugin.toml`` - the small manifest every plugin repo carries at its root.

Example::

    name = "permission-gate"
    version = "0.1.0"
    entry = "permission_gate:register"   # module path (relative to the repo) : function
    description = "Ask before destructive commands"
    python_deps = []                      # pip-installed on `picoagent plugin add`
    skills = ["skills"]                   # directories of SKILL.md to expose
    requires = ["picoagent>=0.1"]         # informational for now
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Manifest:
    name: str
    entry: str
    version: str = "0.0.0"
    description: str = ""
    python_deps: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
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
        """Parse ``<root>/plugin.toml``. Raises ``FileNotFoundError`` if it's missing."""
        path = root / "plugin.toml"
        if not path.exists():
            raise FileNotFoundError(f"{root} has no plugin.toml")
        data = tomllib.loads(path.read_text())
        return Manifest(name=data["name"], entry=data["entry"], version=data.get("version", "0.0.0"),
                        description=data.get("description", ""), python_deps=data.get("python_deps", []),
                        skills=data.get("skills", []), prompts=data.get("prompts", []),
                        requires=data.get("requires", []), root=root)
