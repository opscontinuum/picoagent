"""Skills: reusable instructions the model can pull in on demand.

A skill is a directory with a ``SKILL.md`` file whose frontmatter names and
describes it - a plain markdown convention several other harnesses share, so skills
written for them work here unchanged::

    skills/deploy/SKILL.md
    ---
    name: deploy
    description: Run the deployment checklist    # shown to the model every turn
    disable-model-invocation: false              # true => only the user can invoke it
    ---
    1. run the tests ...                         # loaded only when invoked
    Deploy target: $ARGUMENTS

Context cost is kept low: only the one-line descriptions are injected into the
system prompt; the body is added when the user types ``/skill:deploy prod`` or the
model reads the file itself.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)
_INVOCATION = re.compile(r"^/skill:([\w.-]+)\s*(.*)$", re.S)


@dataclass
class Skill:
    name: str
    description: str
    path: Path            # the SKILL.md file
    body: str             # everything after the frontmatter
    meta: dict = field(default_factory=dict)
    source: str = "project"   # "project" | "user" | "plugin:<name>" - for provenance in UIs

    @property
    def user_only(self) -> bool:
        """True when the skill should be hidden from the model (``disable-model-invocation: true``)."""
        return str(self.meta.get("disable-model-invocation", "false")).lower() == "true"


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split ``---`` frontmatter from the body. Only ``key: value`` lines are understood."""
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text
    meta: dict = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip().strip("\"'")
    return meta, text[match.end():]


def load_skill(skill_md: Path, source: str) -> Skill | None:
    """Read one SKILL.md; the directory name is the fallback for a missing ``name``."""
    try:
        meta, body = parse_frontmatter(skill_md.read_text())
    except OSError:
        return None
    return Skill(name=meta.get("name") or skill_md.parent.name, description=meta.get("description", ""),
                 path=skill_md, body=body.strip(), meta=meta, source=source)


class SkillRegistry:
    """All discovered skills, keyed by name. Later additions override earlier ones."""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def add_dir(self, directory: Path, source: str) -> int:
        """Recursively load every ``SKILL.md`` under ``directory``. Returns how many were loaded."""
        if not directory.is_dir():
            return 0
        loaded = [s for s in (load_skill(p, source) for p in sorted(directory.rglob("SKILL.md"))) if s]
        for skill in loaded:
            self.add(skill)
        return len(loaded)

    def add(self, skill: Skill) -> None:
        self._skills[skill.name] = skill

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def all(self) -> list[Skill]:
        return list(self._skills.values())

    def prompt_section(self) -> str:
        """The compact skills list that goes into the system prompt (user-only skills excluded)."""
        rows = [f"- {s.name}: {s.description} (read `{s.path}` for details, or the user runs /skill:{s.name})"
                for s in self.all() if not s.user_only]
        return "# Skills available\n" + "\n".join(rows) if rows else ""

    def expand(self, text: str) -> str | None:
        """Expand ``/skill:name args`` into the skill body.

        Returns ``None`` when ``text`` is not a skill invocation, so callers can fall
        through to normal prompt handling. ``$ARGUMENTS`` in the body is replaced.
        """
        match = _INVOCATION.match(text.strip())
        if not match:
            return None
        name, args = match.group(1), match.group(2)
        skill = self.get(name)
        if not skill:
            names = ", ".join(s.name for s in self.all()) or "(none)"
            return f"(unknown skill '{name}'; available: {names})"
        body = skill.body.replace("$ARGUMENTS", args)
        return f'<skill name="{skill.name}" path="{skill.path.parent}">\n{body}\n</skill>\n{args}'.strip()
