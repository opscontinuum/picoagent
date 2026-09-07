"""Command-line entry point.

    picoagent                      interactive REPL in the current directory
    picoagent -p "prompt"          one-shot; prints the answer
    picoagent -p "prompt" --json   one-shot; JSONL event stream
    picoagent -r                   resume the most recent session for this directory
    picoagent -e ./my-plugin       load a plugin directory for this run
    picoagent plugin add|trust|untrust|list

The heavy lifting is delegated: :func:`build_runtime` wires registries and plugins,
:class:`~picoagent.core.loop.AgentLoop` runs prompts, the frontend drives the UI.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

from .core.config import UNREADABLE_PROJECT_CONFIG_KEY, load_config
from .core.loop import AgentLoop, Runtime
from .core.provider import OpenAICompatProvider
from .core.session import Session
from .core.text import describe_exception, safe_for_display
from .core.tools import BUILTIN_TOOLS
from .frontends.plain import PlainFrontend
from .frontends.print import PrintFrontend
from .plugins import loader
from .plugins import upgrade as upgrade_mod
from .plugins.manifest import ManifestError


# ---------------------------------------------------------------------------- wiring

#: Marks the startup lines a user must not scroll past: a plugin they approved is not running.
#: Routine lines (a plugin they have never approved, a repository copy that lost to their own)
#: carry no mark, so the one line worth stopping for does not look like the rest.
URGENT_MARK = "!!"

#: Exit codes for the two startup refusals the plugin loader raises. They are separate from each
#: other, and from 1 (every other failure, ``open_session``'s ``SystemExit`` refusals included) and 2
#: (argparse's usage error), because the answers differ and a wrapper should not have to read
#: English to tell them apart: 3 means a control someone approved is not going to run and a person
#: has to look at it, 4 means a config file could not be read so no plugin decision was made at all.
EXIT_REQUIRED_PLUGIN = 3
EXIT_PLUGIN_PROVENANCE = 4


def session_dir(cfg: dict, cwd: Path) -> Path:
    """Sessions live under the user dir, one folder per project path."""
    return Path(cfg["_user_dir"]) / "sessions" / cwd.as_posix().strip("/").replace("/", "--")


def open_session(cfg: dict, cwd: Path, resume: str | None) -> Session:
    """Create a fresh session, or resume ``last``/a given file.

    A ``-r`` path is checked before it is opened. The session log is *appended* to, so
    pointing ``-r`` at an arbitrary file would append JSON lines to it - harmless when a
    person types it, less so when picoagent is invoked by something else (its own shell tool
    can invoke picoagent). Requiring the file to be an existing session, or a new ``.jsonl``,
    costs nothing and removes the footgun.
    """
    directory = session_dir(cfg, cwd)
    if resume is None:
        return Session(directory / f"{int(time.time())}.jsonl", cwd)
    if resume == "last":
        existing = Session.list(directory)
        path = existing[0] if existing else directory / f"{int(time.time())}.jsonl"
        return Session(path, cwd, resume=path.exists())

    path = Path(resume).expanduser()
    if path.exists() and not looks_like_session(path):
        raise SystemExit(f"{path} is not a picoagent session file; refusing to append to it")
    if not path.exists() and path.suffix != ".jsonl":
        raise SystemExit(f"{path} does not exist and is not a .jsonl path; refusing to create it")
    return Session(path, cwd, resume=path.exists())


def looks_like_session(path: Path) -> bool:
    """True when ``path``'s first line is a picoagent session header.

    Cheap and specific: a session log always opens with a ``kind: header`` entry, so one line
    is enough to tell a real session from an unrelated file that happens to end in .jsonl.
    """
    try:
        with path.open() as handle:
            first = handle.readline()
    except OSError:
        return False
    try:
        return json.loads(first).get("kind") == "header"
    except (ValueError, AttributeError):
        return False


def register_core(rt: Runtime) -> None:
    """Built-in provider, tools, skills and commands - the part plugins can override."""
    provider_cfg = rt.cfg.get("providers", {}).get("openai", {})
    rt.providers.register(OpenAICompatProvider(base_url=provider_cfg.get("base_url"),
                                               api_key=provider_cfg.get("api_key"),
                                               extra_headers=provider_cfg.get("headers")))
    for tool_class in BUILTIN_TOOLS:
        rt.tools.register(tool_class())
    for directory in rt.cfg["skill_dirs"]:
        rt.skills.add_dir(rt.cwd / directory, "project")
    rt.skills.add_dir(Path(rt.cfg["_user_dir"]) / "skills", "user")
    register_core_commands(rt)


async def list_models(rt: Runtime) -> str:
    """Ask the active provider what it offers, marking the current model.

    ``list_models`` is optional on the Provider protocol, so a provider that can't enumerate
    (or a server that's down) produces a readable line rather than an exception - this runs
    from a slash command, where a traceback helps nobody.
    """
    provider = rt.providers.get(rt.provider_name)
    if not hasattr(provider, "list_models"):
        return f"provider '{rt.provider_name}' cannot list models"
    try:
        names = await provider.list_models()
    except Exception as exc:  # noqa: BLE001 - the provider already formatted the reason
        return f"could not list models from '{rt.provider_name}': {exc}"
    if not names:
        return f"provider '{rt.provider_name}' returned no models"
    rows = "\n".join(f"{'*' if name == rt.model else ' '} {name}" for name in names)
    plural = "model" if len(names) == 1 else "models"
    return f"{rows}\n({len(names)} {plural}, * = current; /model <name> to switch)"


def register_core_commands(rt: Runtime) -> None:
    """The handful of slash commands the core itself provides."""
    async def help_(args: str, rt: Runtime) -> str:
        commands = [f"/{c.name:14} {c.description}" for c in rt.commands.all()]
        skills = [f"/skill:{s.name:8} {s.description}" for s in rt.skills.all()]
        return "\n".join(commands + skills)

    async def tools(args: str, rt: Runtime) -> str:
        active = {t.name for t in rt.tools.active()}
        return "\n".join(f"{'*' if name in active else ' '} {name}" for name in rt.tools.names())

    async def model(args: str, rt: Runtime) -> str:
        """``/model`` shows the current one, ``/model list`` asks the provider what it offers,
        ``/model <name>`` switches."""
        argument = args.strip()
        current = f"model: {rt.provider_name}/{rt.model} thinking={rt.thinking}"
        if not argument:
            return f"{current}\n(/model list to see what this provider offers)"
        if argument == "list":
            return await list_models(rt)
        previous, rt.model = rt.model, argument
        await rt.events.emit("model_select", {"model": rt.model, "previous": previous,
                                              "provider": rt.provider_name}, rt)
        return f"model: {rt.provider_name}/{rt.model} thinking={rt.thinking}"

    async def session(args: str, rt: Runtime) -> str:
        return f"session: {rt.session.path} ({len(rt.session.entries)} entries)"

    async def new(args: str, rt: Runtime) -> str:
        rt.session = Session(rt.session.path.with_name(f"{int(time.time())}.jsonl"), rt.cwd)
        return "started a new session"

    rt.commands.register("help", help_, "list commands and skills")
    rt.commands.register("tools", tools, "list tools (* = active)")
    rt.commands.register("model", model, "show or set the model")
    rt.commands.register("session", session, "show the session file")
    rt.commands.register("new", new, "start a new session")


def build_runtime(args: argparse.Namespace) -> Runtime:
    """Config -> session -> core registrations -> frontend -> plugins.

    The load report is kept on the runtime because the two halves of telling the user about it
    happen at different times: the stderr copy right here, while the terminal is still empty, and
    the event copy once the session is running, from :func:`announce_load_report`.
    """
    cwd = Path(args.cwd or ".").resolve()
    cfg = load_config(cwd, {"model": args.model, "provider": args.provider, "thinking": args.thinking,
                            "temperature": args.temperature})
    rt = Runtime(cfg, cwd, open_session(cfg, cwd, args.resume))
    register_core(rt)
    headless = bool(args.prompt or args.json)
    rt.frontend = PrintFrontend(json_mode=args.json) if headless else PlainFrontend()
    rt.load_report = loader.load_all(rt, extra_paths=args.extension,
                                     allow_untrusted=args.dangerously_trust_all)
    _report_skipped(rt.load_report)
    report_available_upgrades(rt)
    return rt


def build_runtime_or_refuse(args: argparse.Namespace) -> Runtime:
    """:func:`build_runtime`, with the loader's startup refusals turned into a line and a code.

    Both are decisions the loader took deliberately, and both messages already name what happened,
    which plugin or file it concerns, and the command that ends it. Reaching ``main`` uncaught
    wrapped that wording in a stack trace, which reads as picoagent breaking rather than picoagent
    refusing, and buries the sentence the user needs under frames from a module they did not call.

    The notice ``load_all`` had already worded for the same plugin dies with the report it was
    building, and that is the outcome to want. It says the plugin "was NOT LOADED - whatever it
    enforces is off for this session", which is written for a session that then continues; printed
    above a line saying the session is not starting, it contradicts it. One refusal, one message.
    The loader logs every notice at ``info`` regardless, so ``--verbose`` still has them.

    ``RequiredPluginError`` is caught by its base rather than only the untrusted case, because the
    CLI owes the user the same thing either way. The ``exc_info`` line is for the other subclass:
    a required plugin whose ``register()`` raised carries that exception as its cause, and those
    frames are where a plugin author finds the actual fault, so ``--verbose`` keeps them.
    """
    try:
        return build_runtime(args)
    except loader.RequiredPluginError as exc:
        logging.getLogger("picoagent").debug("required plugin refusal", exc_info=exc)
        sys.stderr.write(f"picoagent: {exc}\n")
        raise SystemExit(EXIT_REQUIRED_PLUGIN) from None
    except loader.PluginProvenanceError as exc:
        sys.stderr.write(f"picoagent: {exc}\n")
        raise SystemExit(EXIT_PLUGIN_PROVENANCE) from None


def _report_skipped(report: loader.LoadReport) -> None:
    """Print the loader's own wording for every plugin that did not load, on stderr.

    The wording belongs to the loader, which knows why the skip happened: "you edited this",
    "something moved your checkout" and "this repository offers a version you never approved"
    call for different answers from the user. Re-deriving one line here from the reason string
    collapsed those three into one, and announced a plugin as failed when it was only shadowed by
    the user's own copy that loaded fine. The urgency travels with the notice too, so an approved
    plugin that is not running is marked rather than sitting in the same shape as the rest.
    """
    for notice in report.notices:
        head, *rest = notice.text.splitlines() or [""]
        mark = f"{URGENT_MARK} " if notice.urgent else ""
        sys.stderr.write(f"picoagent: {mark}{head}\n")
        for line in rest:
            sys.stderr.write(f"{line}\n")


async def announce_load_report(rt) -> None:
    """Put the plugin skips into the event stream as well, one event each.

    A ``--json`` consumer reads stdout, so the stderr copy above does not exist for it: the line
    saying an approved security plugin is not running arrived beside a machine-readable stream
    that never mentioned it. Each skip goes out as its own ``plugin_skipped`` event carrying
    ``urgent`` as a field, so a consumer branches on a boolean rather than parsing English, and
    an urgent skip is not another line of ``notice`` chatter. Frontends that render only the
    events they know about (the REPL, ``-p`` without ``--json``) ignore it and keep the stderr
    copy, so nobody is told twice.
    """
    report = getattr(rt, "load_report", None)
    if report is None or rt.frontend is None:
        return
    for notice in report.notices:
        await rt.frontend.emit("plugin_skipped", {"name": notice.name, "reason": notice.reason,
                                                  "root": str(notice.root), "urgent": notice.urgent,
                                                  "text": notice.text})


async def warn_about_ignored_project_keys(rt) -> None:
    """Say so when the repository's config tried to set something only the user may set.

    Dropping these silently would leave two people confused for different reasons: whoever
    wrote the project config wondering why it did nothing, and whoever cloned the repository
    never learning it tried to point their API key somewhere else.

    It goes to the frontend, not the event bus: ``notice`` on the bus is a name no subscriber
    has, so the warning was computed and thrown away in every run mode. The refused names ride
    along as a list beside the text, which costs a text frontend nothing and saves a ``--json``
    consumer from reading the sentence back apart.
    """
    ignored = rt.cfg.get("_ignored_project_keys") or []
    if not ignored:
        return
    await rt.frontend.emit("notice", {
        "text": f"ignored {', '.join(ignored)} from this repository's .picoagent/config.toml - "
                "these are read from your own config only. See docs/security/trust-boundaries.md",
        "ignored_project_keys": list(ignored)})


async def warn_about_unreadable_project_config(rt) -> None:
    """Say so when this repository's config could not be read at all.

    The neighbouring warning covers a repository that asked for something it may not have. This
    one covers the case where nothing it asked for happened: the file did not parse, so it was
    dropped whole and the session is running on the user's own settings. Silence there is worse
    than for a refused key, because the file can be sitting open in the editor with settings in it
    that the session has never seen.

    ``load_config`` already logs the sentence, which puts it on stderr in a default run. That is
    not the same as this: logging is off in a library embedder and below the level of a ``--json``
    consumer that reads stdout, and both of them are running under settings the repository did not
    choose. The sentence is stored rather than rebuilt, so the two channels cannot drift apart.
    """
    said = rt.cfg.get(UNREADABLE_PROJECT_CONFIG_KEY)
    if not said:
        return
    await rt.frontend.emit("notice", {"text": said})


# ---------------------------------------------------------------------------- commands

async def run_agent(args: argparse.Namespace) -> int:
    rt = build_runtime_or_refuse(args)
    agent = AgentLoop(rt)
    await warn_about_unreadable_project_config(rt)
    await warn_about_ignored_project_keys(rt)
    await announce_load_report(rt)
    await rt.events.emit("session_start", {"resume": bool(args.resume)}, rt)
    try:
        if args.prompt:
            prompt = sys.stdin.read() if args.prompt == "-" else args.prompt
            await agent.handle_input(prompt)
        else:
            await rt.frontend.run(agent)
    finally:
        await rt.events.emit("session_end", {}, rt)
    return 0


def upgrade_command(args: argparse.Namespace) -> int:
    """``picoagent upgrade [check|<name>|--all]``.

    Plugins are updated; picoagent itself is only reported on, because a git checkout and a
    pip install upgrade differently and guessing wrong breaks the install.
    """
    cfg = load_config(Path(args.cwd or ".").resolve())
    statuses = upgrade_mod.check_plugins(cfg)

    app = upgrade_mod.check_app(cfg)
    if app:
        print(app.describe())
        if app.outdated:
            print(f"  to upgrade picoagent yourself: {upgrade_mod.app_upgrade_hint(app)}")
    elif not cfg.get("upgrade", {}).get("app_repo"):
        print("picoagent: not checked (set [upgrade].app_repo in config.toml to enable)")

    if not statuses:
        print("no git-sourced plugins configured")
        return 0
    for status in statuses:
        print(status.describe())

    if args.ucmd == "check":
        return 0

    targets = [s for s in statuses if s.outdated and (args.ucmd in (None, "all") or s.name == args.ucmd)]
    if args.ucmd and args.ucmd not in (None, "all") and not any(s.name == args.ucmd for s in statuses):
        print(f"no configured plugin named {args.ucmd!r}")
        return 1
    if not targets:
        print("nothing to upgrade")
        return 0

    print()
    failed = False
    for status in targets:
        changed, message = upgrade_mod.upgrade(status)
        print(("upgraded " if changed else "skipped  ") + message)
        failed |= not changed
    return 1 if failed else 0


def report_available_upgrades(rt: Runtime) -> None:
    """Opt-in startup notice. Off unless ``[upgrade].check_on_startup`` is true.

    Off by default because it costs a network round trip per configured plugin at launch, and
    an air-gapped install should not reach for a remote it was never going to use. Failures
    are reported as unreachable rather than raised: an upgrade check must never stop a session
    from starting.
    """
    if not rt.cfg.get("upgrade", {}).get("check_on_startup"):
        return
    outdated = [s for s in upgrade_mod.check_plugins(rt.cfg) if s.outdated]
    app = upgrade_mod.check_app(rt.cfg)
    if app and app.outdated:
        outdated.append(app)
    for status in outdated:
        sys.stderr.write(f"picoagent: {status.describe()}\n")
    if outdated:
        sys.stderr.write("picoagent: run `picoagent upgrade` to update plugins.\n")


def plugin_command(args: argparse.Namespace) -> int:
    """``picoagent plugin add|trust|untrust|list``."""
    cfg = load_config(Path(".").resolve())
    trust = loader.TrustStore(Path(cfg["_user_dir"]))
    if args.pcmd != "list" and not args.spec:
        # Named rather than crashed on: every verb but `list` acts on something, and the three
        # that do accept different spellings of it, so the usage error says which one is missing.
        print(f"picoagent plugin {args.pcmd}: name a plugin - a directory, a git spec for `add`, "
              "or for `untrust` the name an approval was filed under (see `picoagent plugin list`)")
        return 2
    if args.pcmd == "add":
        try:
            root = loader.resolve_source(args.spec, cfg, project=args.project)
        except subprocess.CalledProcessError:
            print(f"could not fetch {args.spec} (unreachable, or the ref does not exist)")
            return 1
        try:
            manifest = loader.Manifest.load(root)
        except ManifestError as exc:
            # A repository that isn't a plugin is a normal mistake, not a crash. Say which
            # file could not be read and where it was looked for. One exception type, because
            # the ways a fetched manifest fails are chosen by whoever wrote it.
            print(f"{root} is not a plugin: {safe_for_display(str(exc))}")
            return 1
        print(f"fetched {manifest.name} {manifest.version} -> {root}\n")
        # Consent first, then pip. `python_deps` comes from a manifest nobody has read yet,
        # and a source distribution runs its build script during install - so asking after
        # installing asks about code that has already executed. The same consent path as
        # `plugin trust`, because `add` on an already-installed plugin is an upgrade, and an
        # upgrade is exactly when the user needs to see what changed.
        if trust_command(manifest, trust, assume_yes=args.yes) != 0:
            return 1
        loader.install_deps(manifest)
        config_file = (Path(".picoagent") if args.project else Path(cfg["_user_dir"])) / "config.toml"
        print(f'\nEnable it by adding to {config_file}:\n[plugins]\nenabled = ["{args.spec}"]')
    elif args.pcmd == "trust":
        try:
            manifest = loader.Manifest.load(Path(args.spec).expanduser().resolve())
        except ManifestError as exc:
            print(f"not a plugin directory: {safe_for_display(str(exc))}")
            return 1
        return trust_command(manifest, trust, assume_yes=args.yes)
    elif args.pcmd == "untrust":
        return untrust_command(args.spec, trust)
    elif args.pcmd == "list":
        listed_roots: set[str] = set()
        listed_names: set[str] = set()
        directories = (loader.plugins_dir(cfg), loader.plugins_dir(cfg, project=True))
        for directory in directories:
            for path in sorted(directory.iterdir()) if directory.is_dir() else []:
                if (path / "plugin.toml").exists():
                    try:
                        manifest = loader.Manifest.load(path)
                    except ManifestError as exc:
                        # Listed rather than skipped, and skipped rather than fatal. The manifest
                        # arrived with a clone, so one repository's unreadable file must not cost
                        # the user the listing of every plugin they do have - which is how they
                        # find the name to trust or untrust. Dropping it silently would be its own
                        # answer to a different question: a directory absent from the listing
                        # reads as a plugin that was never installed.
                        listed_roots.add(loader.TrustStore.key(path))
                        print(f"{path.name:20} {'-':8} {'UNREADABLE':10} {path}")
                        print(f"{'':20} {safe_for_display(str(exc))}")
                        continue
                    status = {"trusted": "trusted", "changed": "CHANGED", "new": "UNTRUSTED"}[trust.status(manifest)]
                    listed_roots.add(loader.TrustStore.key(path))
                    listed_names.add(manifest.name)
                    print(f"{manifest.name:20} {manifest.version:8} {status:10} {path}")
        if not listed_roots:
            # An empty listing used to print nothing at all, which reads like a command that
            # failed. The directories are named because which ones were walked is the question
            # a user with a plugin they thought was installed is actually asking.
            print(f"no plugins in {directories[0]} or {directories[1]}")
        print_unlisted_approvals(trust, listed_roots, listed_names)
    return 0


def print_unlisted_approvals(trust: loader.TrustStore, roots: set[str], names: set[str]) -> None:
    """Approvals ``plugin list`` would otherwise never mention, and how to withdraw one.

    The listing above walks the two plugin directories, so it can only show approvals that still
    have a directory in one of them. The approval most likely to need withdrawing is exactly the
    one that does not. A record outlives its directory twice over: it goes on standing for that
    path, so code that later arrives there reads as a plugin the user once vetted rather than one
    they have never seen, and if it carries a requirement it is what stops sessions until the
    plugin is back. Neither is visible in the ordinary listing. A user who cannot see a
    record cannot name it to ``untrust``, which is why this section prints the identifier rather
    than only the fact.

    A directory outside both plugin directories is listed for the same reason and marked
    ``outside`` rather than given a status: its fingerprint is not checked here, and reporting a
    trust state nobody verified would be worse than reporting none.
    """
    rows = []
    for label, record in trust.data.items():
        root = record.get("root")
        if root is None:
            if label not in names:                       # pre-``root`` record, matched by name
                rows.append((record.get("name") or label, "no directory", label))
        elif root not in roots:
            rows.append((record.get("name") or label, "outside" if Path(root).is_dir() else "MISSING", root))
    if not rows:
        return
    print("\napprovals not shown above (withdraw one with: picoagent plugin untrust <directory-or-name>):")
    for name, state, where in rows:
        print(f"  {name:20} {state:12} {where}")


def untrust_command(spec: str, trust: loader.TrustStore) -> int:
    """``picoagent plugin untrust <directory-or-name>`` - take back an approval.

    The counterpart to ``trust``, and named for it: approving is the decision, and a decision the
    user can make once and never revisit is not one they hold. Without this the only way back was
    editing ``trust.json`` by hand, which is what a refusal had to tell someone to do at the exact
    moment their session would not start - hand-editing a security file to get back to work.

    ``RequiredPluginMissing`` now names this command instead, which makes one property of the whole
    ``plugin`` verb load-bearing: it builds a config and a trust store and stops there. No runtime,
    no ``load_all``, no plugin imported. That is what lets a refusal about a recorded requirement be
    a stop the user can undo rather than a wedge, so a future verb that needs a live runtime should
    get its own entry point rather than pulling one in here.

    Matched against the *record*, not against a plugin on disk. ``trust`` resolves its argument by
    reading ``plugin.toml``, which is the right thing for approving code and the wrong thing here:
    the case that matters most is a record whose directory is gone, and there is no manifest left
    to read. So a directory is compared as ``TrustStore.key`` writes it (resolved, so the same
    directory spelled through a symlink still matches), and the name a record was filed under is
    accepted too, because after a deletion the name is all the user still has.

    A name covering two records is refused rather than resolved by guessing. Two checkouts can
    share a plugin name - the user's own and a repository's copy - and withdrawing the wrong one
    silently disarms an approval the user still wants.
    """
    wanted = loader.TrustStore.key(Path(spec).expanduser())
    labels = [label for label, record in trust.data.items() if record.get("root") == wanted]
    if not labels:
        labels = [label for label, record in trust.data.items() if spec in (label, record.get("name"))]
    if not labels:
        print(f"no approval matches {spec!r}, so nothing was withdrawn.")
        print(f"{trust.path} records nothing at all." if not trust.data
              else f"Approvals in {trust.path} - pass a directory or a name from this list:")
        for label, record in trust.data.items():
            print(f"  {record.get('name') or label:20} {record.get('root') or '(no directory recorded)'}")
        return 1
    if len(labels) > 1:
        print(f"{spec!r} names {len(labels)} approvals, so nothing was withdrawn. "
              "Pass the directory of the one you mean:")
        for label in labels:
            print(f"  {trust.data[label].get('root') or '(no directory recorded)'}")
        return 1

    record = trust.withdraw(labels[0])
    root = record.get("root")
    print(f"withdrew the approval of {record.get('name') or labels[0]} "
          f"({root or 'no directory recorded'}) from {trust.path}")
    if root and (Path(root) / "plugin.toml").exists():
        print(f"  The plugin itself is untouched and will not load until you approve it again:\n"
              f"    picoagent plugin trust {root}")
    return 0


def trust_command(manifest, trust: loader.TrustStore, assume_yes: bool = False) -> int:
    """Approve a plugin's *current* code, after showing what the user is actually approving.

    The three cases are genuinely different and shouldn't look the same:
    already-trusted is a no-op, never-trusted is a first approval, and changed-since-approval
    is the security-relevant one - code that was reviewed has been replaced. Re-approving used
    to be an unconditional overwrite with no prompt and no indication of what moved, which
    made "I edited this myself" indistinguishable from "upstream changed it while I wasn't
    looking".
    """
    status = trust.status(manifest)
    if status == "trusted":
        print(f"{manifest.name} is already trusted and unchanged - nothing to do")
        return 0

    print(f"plugin:      {manifest.name} {manifest.version}")
    print(f"entry:       {manifest.entry}")
    print(f"description: {manifest.description}")
    print(f"location:    {manifest.root}")
    if manifest.python_deps:
        print(f"pip deps:    {', '.join(manifest.python_deps)}")

    if status == "changed":
        print("\n*** This plugin CHANGED since you approved it. ***")
        print("Its code runs with your privileges, so review what moved before accepting:")
        for line in trust.describe_change(manifest):
            print(f"  {line}")
        question = "Accept the change and re-trust this plugin? [y/N] "
    else:
        print("\nThis plugin has never been trusted. Its code runs with your privileges.")
        question = "Trust this plugin? [y/N] "

    if not assume_yes and not input(f"\n{question}").lower().startswith("y"):
        print("not trusted - the plugin will not load")
        return 1
    trust.trust(manifest)
    print(f"trusted {manifest.name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="picoagent", description="minimal-core coding agent")
    ap.add_argument("-p", "--prompt", help="non-interactive: run one prompt ('-' reads stdin)")
    ap.add_argument("--json", action="store_true", help="emit JSONL events (use with -p)")
    ap.add_argument("-m", "--model")
    ap.add_argument("--provider", help="provider name (built-in: openai; others from plugins)")
    ap.add_argument("--thinking", choices=["off", "low", "medium", "high"])
    ap.add_argument("--temperature", type=float,
                    help="sampling temperature (omit to use the server's own default)")
    ap.add_argument("-r", "--resume", nargs="?", const="last", help="resume last session or a session file")
    ap.add_argument("-e", "--extension", action="append", default=[], help="load a plugin dir (trusted for this run)")
    ap.add_argument("--dangerously-trust-all", action="store_true", help="skip the trust check for every plugin")
    ap.add_argument("-C", "--cwd", help="project directory (default: current)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    plugin = sub.add_parser("plugin", help="manage plugins")
    plugin.add_argument("pcmd", choices=["add", "trust", "untrust", "list"])
    plugin.add_argument("spec", nargs="?",
                        help="git:host/user/repo@ref, a local path, or for untrust an approved name")
    plugin.add_argument("--project", action="store_true", help="install under the project instead of the user dir")
    upgrade_p = sub.add_parser("upgrade", help="check for and apply plugin updates")
    upgrade_p.add_argument("ucmd", nargs="?",
                           help="'check' to only report, a plugin name, or omit for all")
    plugin.add_argument("-y", "--yes", action="store_true",
                        help="skip the trust confirmation prompt (scripting; you are accepting the code unseen)")
    return ap


class SafeLogFormatter(logging.Formatter):
    """Renders a log line without letting the text inside it drive the terminal.

    Everything else picoagent puts on screen goes through ``picoagent.core.text``; the log did
    not, and it is the one channel where the dangerous half is not written by the call site.
    ``log.exception`` renders the traceback itself, and a traceback's last line is the exception's
    class name and ``str(exc)`` - both chosen by whoever raised, both reaching stderr verbatim
    however carefully the caller wraps its own arguments. The three places that do this are the
    three that catch plugin code so a failure does not end the session: a handler that raised, a
    slash command that raised, a tool that raised. Sanitising it here rather than at those three
    call sites is not tidiness - the traceback is built by the logging machinery, after the call
    site is done, so there is nowhere else it can be reached.

    A message that will not render is reported rather than allowed out, for the same reason
    ``describe_exception`` does it: this formatter runs on the path that exists so a failure is
    survivable, and an exception thrown while reporting one would undo that.
    """

    def formatException(self, ei) -> str:
        return safe_for_display(super().formatException(ei))

    def format(self, record: logging.LogRecord) -> str:
        record = copy.copy(record)      # other handlers get the record as it was
        try:
            message = record.getMessage()
        except Exception as exc:  # noqa: BLE001 - an argument's __str__ is the plugin's code too
            message = f"<the log message could not be rendered: {describe_exception(exc)}>"
        record.msg, record.args = safe_for_display(message), ()
        return super().format(record)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    handler = logging.StreamHandler()
    handler.setFormatter(SafeLogFormatter("%(name)s: %(message)s"))
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, handlers=[handler])
    if args.cmd == "plugin":
        sys.exit(plugin_command(args))
    if args.cmd == "upgrade":
        sys.exit(upgrade_command(args))
    sys.exit(asyncio.run(run_agent(args)))
