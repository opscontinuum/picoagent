"""System-prompt assembly and project context files.

The prompt is built from named *sections* so plugins can add, replace or remove
one without touching the others (``api.register_system_prompt_section``). The
built-in sections are ``base`` (the agent's standing orders), ``env`` (cwd, OS)
and ``context_files`` (``AGENTS.md`` and anything else listed in ``context_files``, found
between the git root and the working directory, root first so more specific files win).
"""
from __future__ import annotations

import platform
import subprocess
from pathlib import Path
from typing import Callable

DEFAULT_PROMPT = """You are picoagent, a coding agent working in the user's repository.
Use the tools to inspect and change code. Prefer `read` over `cat`, `edit` for surgical changes,
`write` for new files, `shell` for everything else - see the Environment section for which shell
dialect that actually runs (it differs by platform). Keep responses concise. Never fabricate file
contents; read before you edit. Report what you changed when done."""


def git_root(cwd: Path) -> Path | None:
    """The repository root for ``cwd``, or ``None`` outside a git checkout."""
    try:
        result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd,
                                capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(result.stdout.strip()) if result.returncode == 0 else None


def find_context_files(cwd: Path, names: list[str]) -> list[Path]:
    """Collect context files from the git root (or ``cwd``) down to ``cwd``, root first."""
    root = (git_root(cwd) or cwd).resolve()
    directory = cwd.resolve()
    chain: list[Path] = []
    while True:
        chain.append(directory)
        if directory == root or directory.parent == directory:
            break
        directory = directory.parent
    found: list[Path] = []
    for directory in reversed(chain):
        for name in names:
            candidate = directory / name
            if candidate.is_file() and candidate not in found:
                found.append(candidate)
    return found


class SystemPromptBuilder:
    """Composes the system prompt from ordered, replaceable sections."""

    def __init__(self, cfg: dict, cwd: Path):
        self.cfg, self.cwd = cfg, cwd
        self.sections: dict[str, Callable[[], str]] = {
            "base": lambda: DEFAULT_PROMPT,
            "env": self._environment_section,
            "context_files": self._context_files_section,
        }

    def _environment_section(self) -> str:
        shell = "PowerShell" if platform.system() == "Windows" else "sh (bash-compatible)"
        return f"# Environment\ncwd: {self.cwd}\nos: {platform.system()} {platform.release()}\nshell: {shell}"

    def _context_files_section(self) -> str:
        parts = []
        for path in find_context_files(self.cwd, self.cfg["context_files"]):
            try:
                parts.append(f"# {path}\n{path.read_text()}")
            except OSError:
                continue
        return "\n\n".join(parts)

    def set_section(self, name: str, render: Callable[[], str]) -> None:
        """Add or replace a section. Sections render lazily, at every ``build()``."""
        self.sections[name] = render

    def remove_section(self, name: str) -> None:
        self.sections.pop(name, None)

    def build(self) -> str:
        """Render all sections, skipping empty ones, joined by blank lines."""
        return "\n\n".join(text for text in (render() for render in self.sections.values()) if text)
