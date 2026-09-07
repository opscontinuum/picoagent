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
Those tables do **not** deep-merge across layers, because a merged value carries no
record of who wrote it: a repository's ``base_url`` and the user's ``api_key`` arrived
in the same dictionary and the plugin sent one to the other. Layer 3's plugin tables are
lifted out into :data:`PROJECT_PLUGIN_KEY` and handed to plugins through
:class:`PluginConfig`, which reads as the user layer and names the layer of every value.
"""
from __future__ import annotations

import copy
import logging
import os
import tomllib
from pathlib import Path
from typing import Any

from .text import describe_exception

log = logging.getLogger("picoagent.config")

DEFAULTS: dict[str, Any] = {
    "model": os.environ.get("PICOAGENT_MODEL", "gpt-4o-mini"),
    "provider": "openai",            # the built-in OpenAI-compatible client; others come from plugins
    "providers": {"openai": {}},     # per-provider overrides: base_url / api_key / headers
    "max_tokens": 8192,
    "temperature": None,             # None leaves sampling to the server; 0.0 is a value, not "unset"
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


#: Where a repository's ``[plugins.<name>]`` tables live once they are out of the merge.
PROJECT_PLUGIN_KEY = "_project_plugin_config"

#: Keys under ``[plugins]`` that belong to the loader, not to a plugin's own settings table.
PLUGINS_RESERVED: tuple[str, ...] = ("enabled", "rewrite")

#: The sentence explaining why the repository's config.toml was dropped, or ``None`` when it parsed.
#: A repository you cloned must not be able to stop your tool starting, so an unusable one is
#: ignored rather than fatal - and ignoring it silently would leave the user running under settings
#: they can see in the file and cannot find in the session. The wording lives here so a frontend can
#: show it verbatim; :func:`load_config` also logs it, which is what puts it on stderr today.
UNREADABLE_PROJECT_CONFIG_KEY = "_unreadable_project_config"

#: Where refused project values are collected, per plugin name, so the session can report them.
#: A list per plugin rather than a return value, because the code that reads a setting and the
#: code that talks to the user are not the same call: the read happens inside ``register()``,
#: the notice goes out at ``session_start``.
REFUSED_PROJECT_VALUES_KEY = "_refused_project_values"

#: The TOML types a repository may put inside a list. A list of tables is not one of the settings
#: a plugin opts into here, and letting one through is how a ``dict`` reaches ``re.compile``.
_SCALARS: tuple[type, ...] = (str, int, float, bool)

#: Shape names as they read inside "a list of ...".
_PLURALS = {"a string": "strings", "a number": "numbers", "a table": "tables",
            "true or false": "true/false values"}


def _shape_name(value: Any) -> str:
    """Describe a value's shape to somebody reading a config file, not a traceback."""
    if value is None:
        return "a value"
    if isinstance(value, bool):
        return "true or false"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, dict):
        return "a table"
    if isinstance(value, list):
        kinds = sorted({_PLURALS.get(_shape_name(item), "values") for item in value})
        return f"a list of {' or '.join(kinds)}" if kinds else "a list of strings"
    return type(value).__name__


def _element_types(expected: list) -> tuple[type, ...]:
    """The element types a list-shaped default declares; an empty default declares strings.

    Every list a repository may contribute to today - protected paths, deny patterns - is a list
    of strings, and an empty list is the only way a plugin can spell "added to nothing". Reading
    that as "and the elements are strings" costs a plugin wanting numbers one non-empty default,
    and saves every other one from ``fnmatch(path, 5)``.
    """
    return tuple({type(item) for item in expected}) or (str,)


def has_shape(value: Any, expected: Any) -> bool:
    """Is ``value`` shaped like ``expected``, closely enough to be used in its place?

    ``expected`` is the default the plugin passed. That is the only declaration of intent
    available at this seam, and the one a plugin author cannot forget to write, because they
    had to supply it anyway. ``None`` declares nothing and accepts anything, which is what
    ``from_project(key)`` without a default has always meant.

    Numbers are interchangeable except for ``bool``, which is an ``int`` in Python and is not
    one in anybody's config file.
    """
    if expected is None:
        return True
    if isinstance(expected, bool):
        return isinstance(value, bool)
    if isinstance(expected, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(expected, list):
        types = _element_types(expected)
        return isinstance(value, list) and all(isinstance(item, _SCALARS) and type(item) in types
                                               for item in value)
    if isinstance(expected, dict):
        return isinstance(value, dict)
    return isinstance(value, type(expected))


class PluginConfig(dict):
    """One plugin's settings, with the config layer each value came from.

    Read it like a dict and you get the **user layer**: ``DEFAULTS``, ``~/.picoagent/config.toml``
    and the CLI flags, the layers whose author is the person running the agent. A repository's
    ``[plugins.<name>]`` values are kept beside them, never merged into them, and reachable only
    by name through :meth:`from_project` or :meth:`with_project`.

    The default is that direction because of what the merged version cost. ``USER_ONLY`` stops a
    repository choosing ``providers.openai.base_url``, but four plugins took the same decision
    from ``[plugins.<name>]`` instead: an endpoint that the user's API key was then sent to, an
    MCP server command spawned at load time, a permission gate switched to ``yolo``, an
    environment allowlist widened. None of those plugins did anything wrong with the value they
    were handed. They could not tell it apart from the user's own.

    So a plugin author who does nothing gets the user layer, and a plugin that wants a
    repository's opinion has to name the key and, by naming it, answer for it. The rule worth
    holding to when deciding: a repository may *tighten* (another protected path, another denied
    variable) and may set what is only taste (an index name, a timeout). It may not choose a
    destination, a command, or a permission.

    A named key is still an untrusted one. Naming it says the plugin wants the repository's
    opinion on *that setting*; it does not say the repository wrote a list where a list belongs.
    Nothing checked that, so ``protected = 5`` in a repository's config.toml did not shorten the
    permission gate's list of protected paths - it raised ``TypeError`` inside ``register()``,
    the loader caught it, and the whole gate was skipped for the session. Tightening-only held
    for the contents of the value and not for its type, and the fail-open was worse than the
    merge it replaced. So the shape check lives here, at the one seam every project value
    crosses, rather than in each plugin: four plugins hand-rolled it and two got it wrong.

    :meth:`from_project` and :meth:`with_project` therefore accept a project value only when it
    is shaped like the default the plugin passed. Anything else leaves the plugin with its
    default, and the refusal is recorded in :attr:`refusals` so somebody hears about it.
    """

    def __init__(self, user: dict | None = None, project: dict | None = None,
                 refusals: list[str] | None = None):
        super().__init__(copy.deepcopy(user or {}))
        self._project: dict = copy.deepcopy(project or {})
        self._refusals: list[str] = refusals if refusals is not None else []

    @property
    def refusals(self) -> list[str]:
        """Project values refused for being the wrong shape, in the words shown to the user.

        Shared across every :class:`PluginConfig` built for the same plugin in one session, so a
        value refused while ``register()`` reads it is still there to report at session start.
        """
        return list(self._refusals)

    def _refuse(self, key: str, value: Any, expected: Any) -> None:
        """Record - and log - one project value this plugin will not be given.

        Named, never silent. A repository author who wrote ``protected = 5`` and a user who
        cloned that repository both need to know it did nothing; the seam that drops the value
        is the only place that still knows why.
        """
        message = (f"ignored {key} from this repository's .picoagent/config.toml: expected "
                   f"{_shape_name(expected)}, got {_shape_name(value)} ({value!r:.60})")
        if message not in self._refusals:
            self._refusals.append(message)
        log.warning("%s", message)

    def source(self, key: str) -> str | None:
        """``"user"``, ``"project"``, ``"both"``, or ``None`` if nobody set ``key``."""
        in_user, in_project = key in self, key in self._project
        if in_user and in_project:
            return "both"
        if in_user:
            return "user"
        return "project" if in_project else None

    def from_project(self, key: str, default: Any = None) -> Any:
        """Read one value the repository set. Every call is a decision to trust it.

        ``default`` is both the answer when the repository said nothing and the shape the answer
        must have: ``from_project("protected", [])`` returns a list or it returns ``[]``. The
        caller never has to defend itself against the type, which is the point - the two callers
        that wrapped this in ``list(...)`` turned a bad value into a crash inside ``register()``,
        and a plugin whose ``register()`` raises is skipped, protection and all.
        """
        if key not in self._project:
            return copy.deepcopy(default)
        value = self._project[key]
        if not has_shape(value, default):
            self._refuse(key, value, default)
            return copy.deepcopy(default)
        return copy.deepcopy(value)

    def project_keys(self) -> list[str]:
        """Every key this repository's config.toml set for this plugin, accepted or not.

        A plugin that refuses a key should still be able to say so out loud. Silence is how the
        merged version stayed invisible for as long as it did.
        """
        return sorted(self._project)

    def with_project(self, *keys: str, **expected: Any) -> dict:
        """A plain dict of the user layer with the named keys allowed to come from the repository.

        Named keys only, and a whole value at a time: no deep merge, so a repository cannot reach
        inside a nested table the plugin thought it controlled.

        A key named as ``key=default`` also declares the shape that value must have, the same way
        :meth:`from_project` reads its default, and the returned dict always holds a value for
        it: the repository's if it is shaped right, else the user's, else the default. A key
        named as a bare string is checked against the user layer's value when there is one and
        unchecked when there is not, so a plugin with a real default should state it:
        ``with_project(timeout=30.0)`` is what stops ``timeout = "soon"`` reaching ``float()``.
        """
        merged = dict(self)
        wanted: dict[str, Any] = {key: self.get(key) for key in keys}
        wanted.update(expected)
        for key, shape in wanted.items():
            if key in expected and key not in merged:
                merged[key] = copy.deepcopy(expected[key])
            if key not in self._project:
                continue
            value = self._project[key]
            if not has_shape(value, shape):
                self._refuse(key, value, shape)
                continue
            merged[key] = copy.deepcopy(value)
        return merged


def _split_project_plugin_tables(project_cfg: dict) -> tuple[dict, dict]:
    """Lift ``[plugins.<name>]`` out of a repository's config into a layer of its own.

    Removing them from the merge is what makes the safe behaviour the default one: a plugin
    reading ``cfg["plugins"][name]``, through :meth:`PluginAPI.plugin_config` or straight off
    ``api.config``, sees only what the user's own layers put there. Nothing is discarded - the
    tables come back as the second return value and reach plugins through :class:`PluginConfig`.
    """
    plugins = project_cfg.get("plugins")
    if not isinstance(plugins, dict):
        return project_cfg, {}
    tables = {}
    for name in [key for key in plugins if key not in PLUGINS_RESERVED]:
        value = plugins.pop(name)
        if isinstance(value, dict):
            tables[name] = value
    return project_cfg, tables


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


class _ConfigFileError(Exception):
    """A config file that exists and cannot be used, worded for whoever has to fix it.

    An exception rather than a returned value, against the usual rule, because there is no
    partial answer to carry on with: a half-parsed config is not a config. What the two callers
    do with it differs - the user's own files end the session, a repository's is dropped - and
    both need the same sentence, so the sentence is what travels.
    """


def _read_toml(path: Path) -> dict:
    """Parse a TOML file, or return ``{}`` if it does not exist.

    A file that exists and cannot be turned into a dictionary raises :class:`_ConfigFileError`
    naming the file and the fault. Letting the parser's own exception out instead put a stack
    trace through ``tomllib._parser`` in front of somebody whose only mistake was an unclosed
    quote - or, for a repository's config, whose only act was cloning it.

    The catch is broad on purpose, with the two failures worth their own sentence named first.
    Naming the rest was tried and was wrong twice over: ``tomllib.load`` decodes the bytes itself
    (``s = b.decode()``), so a file that is not UTF-8 - which is what a Windows editor writes when
    somebody re-saves this one - raises ``UnicodeDecodeError``, a ``ValueError`` that neither
    named handler covers; and the parser is recursive descent, so ``v = [[[[...`` runs out of
    stack and raises ``RecursionError`` before it runs out of input. Both escaped as tracebacks,
    and both let a cloned repository deny the user their own tool, which is the one thing the
    layering below exists to prevent. The file is content an attacker may choose, so the contract
    that reading it never raises has to hold for every way a parser can fail, including the ones
    a future ``tomllib`` invents. ``MemoryError`` is re-raised because it is not a fact about this
    file: the interpreter is out of memory, and reporting that as a config-file fault would send
    whoever reads it to edit a file that is fine. Catching ``RecursionError`` is safe here because
    the stack has already unwound back to this frame by the time the handler runs, so the sentence
    below is built with the whole stack available again.
    """
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise _ConfigFileError(f"{path} is not valid TOML: {exc}") from None
    except OSError as exc:
        raise _ConfigFileError(f"{path} could not be read: {exc.strerror or exc}") from None
    except MemoryError:
        raise
    except Exception as exc:  # noqa: BLE001 - an attacker-controlled file may fail any way it likes
        raise _ConfigFileError(f"{path} could not be read: {describe_exception(exc)}") from None


def load_config(cwd: Path, overrides: dict | None = None) -> dict:
    """Build the effective config for a session rooted at ``cwd``.

    ``overrides`` come from the CLI; ``None`` values are ignored so unset flags
    do not clobber file settings. The result also carries two private keys,
    ``_user_dir`` and ``_cwd``, so other modules don't need to recompute them.

    An unusable config file is answered differently per layer, and the asymmetry is the point.
    The user's own files - ``config.toml`` and the endpoint files beside it - stop the session:
    they were written deliberately, and carrying on without them would silently run the agent
    under settings the user did not choose, confinement and gateway included. A repository's is
    dropped with a warning: it arrived with a clone, and a repository must not be able to deny
    somebody their own tool by shipping a broken file.
    """
    try:
        user_cfg = _read_toml(user_dir() / "config.toml")
        endpoints = load_endpoints(user_dir())
    except _ConfigFileError as exc:
        raise SystemExit(f"{exc}. This is your own config, so picoagent will not start under "
                         "settings you did not choose; fix it or move it aside") from None

    unreadable_project: str | None = None
    try:
        raw_project = _read_toml(cwd / ".picoagent" / "config.toml")
    except _ConfigFileError as exc:
        raw_project = {}
        unreadable_project = (f"{exc}. Ignoring this repository's config and continuing with your "
                              "own settings; fix the file to make its settings apply")
        log.warning("%s", unreadable_project)
    project_cfg, ignored = _strip_user_only(raw_project)
    project_cfg, project_plugin_cfg = _split_project_plugin_tables(project_cfg)

    # DEFAULTS is copied, not merged from: ``_deep_merge`` keeps a nested dict by reference when
    # only one side has it, so a session that wrote to ``cfg["plugins"]`` was writing into the
    # module-level default and every later session in the process inherited it. Two sessions in
    # one process is not the common case, but "a setting from somewhere else turned up in this
    # config" is the failure this module is meant to make impossible.
    cfg = _deep_merge(_deep_merge(copy.deepcopy(DEFAULTS), user_cfg), project_cfg)
    cfg["plugins"]["enabled"] = (
        list(user_cfg.get("plugins", {}).get("enabled", []))
        + list(project_cfg.get("plugins", {}).get("enabled", []))
    )
    if overrides:
        cfg = _deep_merge(cfg, {k: v for k, v in overrides.items() if v is not None})

    cfg["endpoints"] = endpoints
    cfg[PROJECT_PLUGIN_KEY] = project_plugin_cfg
    cfg[UNREADABLE_PROJECT_CONFIG_KEY] = unreadable_project
    cfg["_user_dir"] = str(user_dir())
    cfg["_cwd"] = str(cwd)
    cfg["_ignored_project_keys"] = ignored
    return cfg


def plugin_config(cfg: dict, name: str) -> PluginConfig:
    """The two layers of ``[plugins.<name>]`` for one plugin, kept apart.

    A config built by hand - a test, an embedder wiring its own ``Runtime`` - has no project
    layer, so everything under ``cfg["plugins"][name]`` counts as the user's. That is the honest
    reading: whoever wrote that dictionary in code was acting as the user, not as a repository.

    Refusals are collected in ``cfg`` rather than on the returned object, because a plugin reads
    its settings in ``register()`` and the user is told about them at ``session_start`` - two
    calls, two :class:`PluginConfig` instances, one list.
    """
    user = cfg.get("plugins", {}).get(name)
    project = cfg.get(PROJECT_PLUGIN_KEY, {}).get(name)
    refusals = cfg.setdefault(REFUSED_PROJECT_VALUES_KEY, {}).setdefault(name, [])
    return PluginConfig(user if isinstance(user, dict) else {}, project, refusals)
