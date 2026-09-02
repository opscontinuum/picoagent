"""Layered configuration.

Values are resolved from lowest to highest precedence:

1. :data:`DEFAULTS` below
2. ``~/.picoagent/config.toml``            (user-wide; ``PICOAGENT_HOME`` overrides the directory)
3. ``<project>/.picoagent/config.toml``   (checked into the repo, shared with the team)
4. CLI flags / programmatic overrides

Layer 3 travels inside repositories, so it is **not** trusted with everything layer 2 can
set. See :data:`USER_ONLY` for what a repository may not decide on your behalf, and
:func:`load_endpoints` for where an endpoint's URL and key actually live.

Dictionaries deep-merge, scalars override. The one special case is
``[plugins].enabled``: user and project lists are *concatenated* (user first) so a
project can add plugins - and, because later registrations win, override tools that
a user-level plugin registered.

Plugins read their own settings from ``[plugins.<name>]`` via ``api.plugin_config()``.
"""
from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "model": os.environ.get("PICOAGENT_MODEL", "gpt-4o-mini"),
    "provider": "openai",            # the built-in OpenAI-compatible client; others come from plugins
    "providers": {"openai": {}},     # per-provider overrides: base_url / api_key / headers
    "max_tokens": 8192,
    "thinking": "off",               # off | low | medium | high - each provider maps this to its own knob
    "parallel_tools": True,          # run sibling tool calls concurrently (file edits are still serialised)
    "tool_output_max_bytes": 50_000, # tool output larger than this is truncated (Pi's limits)
    "tool_output_max_lines": 2000,
    "shell_timeout": 120,            # seconds, unless the model passes its own timeout
    # Interop with other harnesses' context/skill locations is off. Add "CLAUDE.md" and
    # ".claude/skills" back to these lists (or set them in config.toml) to re-enable it -
    # both are read-only conventions, so nothing else has to change.
    # Off by default: a coding agent legitimately edits sibling repos and files outside the
    # directory it started in. On, read/write/edit refuse anything outside the project.
    "confine_to_project": False,
    "context_files": ["AGENTS.md", ".picoagent/AGENTS.md"],
    "skill_dirs": ["skills", ".picoagent/skills", ".agents/skills"],
    "plugins": {"enabled": []},   # also: [plugins].rewrite maps a url prefix to a mirror
    "upgrade": {"check_on_startup": False, "app_repo": ""},
    "frontend": "plain",
}


#: Settings only *your* config may set, never a repository's.
#:
#: A cloned repository carries its author's ``.picoagent/config.toml``, and picoagent reads it
#: before you have looked at anything. That is fine for taste - which model, how many tokens -
#: and not fine for anything that decides where your credentials go or what enters your prompt.
#: A repo that set ``providers.openai.base_url`` would receive the API key from *your* config
#: on the first turn; one that set ``context_files`` could name your credentials file and read
#: it into the system prompt. Both were reachable before this list existed.
USER_ONLY: tuple[tuple[str, ...], ...] = (
    ("providers",),           # endpoint and key: a repo must not choose where your key is sent
    ("context_files",),       # absolute paths allowed, and contents enter the system prompt
    ("skill_dirs",),          # skill text is injected into the prompt
    ("confine_to_project",),  # a repo must not be able to switch off its own confinement
    ("plugins", "rewrite"),   # redirects a plugin clone to another host before you approve it
    ("upgrade",),             # redirects where picoagent upgrades *itself* from
)


def _strip_user_only(project_cfg: dict) -> tuple[dict, list[str]]:
    """Drop the keys in :data:`USER_ONLY` from a repository's config.

    Returns the survivors and the dotted names removed, so the caller can say plainly that a
    repository tried to set them. Silently ignoring them would be its own bug: the user would
    wonder why their project settings did nothing.
    """
    cleaned = copy.deepcopy(project_cfg)
    ignored: list[str] = []
    for path in USER_ONLY:
        node = cleaned
        for part in path[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                node = None
                break
            node = child
        if node is None or path[-1] not in node:
            continue
        del node[path[-1]]
        ignored.append(".".join(path))
    return cleaned, ignored


def load_endpoints(directory: Path) -> dict[str, dict]:
    """Read ``<user dir>/endpoints/*.toml`` - one file per external service.

    Each file holds one endpoint's ``base_url`` and its own ``api_key``, so a git host, an
    artifact repository and the model gateway never share a credential and none of them sits
    in the config file that a project might try to override. The filename is the endpoint name.
    """
    endpoints: dict[str, dict] = {}
    folder = directory / "endpoints"
    if not folder.is_dir():
        return endpoints
    for path in sorted(folder.glob("*.toml")):
        endpoints[path.stem] = _read_toml(path)
    return endpoints


def user_dir() -> Path:
    """Where user-level state lives (config, plugins, sessions, trust store)."""
    return Path(os.environ.get("PICOAGENT_HOME", Path.home() / ".picoagent"))


def _deep_merge(base: dict, override: dict) -> dict:
    """Return ``base`` updated by ``override``; nested dicts merge instead of replacing."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_toml(path: Path) -> dict:
    """Parse a TOML file, or return ``{}`` if it does not exist."""
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def load_config(cwd: Path, overrides: dict | None = None) -> dict:
    """Build the effective config for a session rooted at ``cwd``.

    ``overrides`` come from the CLI; ``None`` values are ignored so unset flags
    do not clobber file settings. The result also carries two private keys,
    ``_user_dir`` and ``_cwd``, so other modules don't need to recompute them.
    """
    user_cfg = _read_toml(user_dir() / "config.toml")
    project_cfg, ignored = _strip_user_only(_read_toml(cwd / ".picoagent" / "config.toml"))

    cfg = _deep_merge(_deep_merge(DEFAULTS, user_cfg), project_cfg)
    cfg["plugins"]["enabled"] = (
        list(user_cfg.get("plugins", {}).get("enabled", []))
        + list(project_cfg.get("plugins", {}).get("enabled", []))
    )
    if overrides:
        cfg = _deep_merge(cfg, {k: v for k, v in overrides.items() if v is not None})

    cfg["endpoints"] = load_endpoints(user_dir())
    cfg["_user_dir"] = str(user_dir())
    cfg["_cwd"] = str(cwd)
    cfg["_ignored_project_keys"] = ignored
    return cfg
