"""Command-line entry point.

    picoagent                      interactive REPL in the current directory
    picoagent -p "prompt"          one-shot; prints the answer
    picoagent -p "prompt" --json   one-shot; JSONL event stream
    picoagent -r                   resume the most recent session for this directory
    picoagent -e ./my-plugin       load a plugin directory for this run
    picoagent plugin add|trust|list

The heavy lifting is delegated: :func:`build_runtime` wires registries and plugins,
:class:`~picoagent.core.loop.AgentLoop` runs prompts, the frontend drives the UI.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

from .core.config import load_config
from .core.loop import AgentLoop, Runtime
from .core.provider import OpenAICompatProvider
from .core.session import Session
from .core.tools import BUILTIN_TOOLS
from .frontends.plain import PlainFrontend
from .frontends.print import PrintFrontend
from .plugins import loader
from .plugins import upgrade as upgrade_mod


# ---------------------------------------------------------------------------- wiring

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
    """Config -> session -> core registrations -> frontend -> plugins."""
    cwd = Path(args.cwd or ".").resolve()
    cfg = load_config(cwd, {"model": args.model, "provider": args.provider, "thinking": args.thinking,
                            "temperature": args.temperature})
    rt = Runtime(cfg, cwd, open_session(cfg, cwd, args.resume))
    register_core(rt)
    headless = bool(args.prompt or args.json)
    rt.frontend = PrintFrontend(json_mode=args.json) if headless else PlainFrontend()
    report = loader.load_all(rt, extra_paths=args.extension, allow_untrusted=args.dangerously_trust_all)
    _report_skipped(report)
    report_available_upgrades(rt)
    return rt


def _report_skipped(report: loader.LoadReport) -> None:
    """Say plainly which plugins didn't load, on stderr.

    A skipped plugin is silent otherwise: the user installed it, expects it to be running, and
    only finds out when the behaviour it provides is missing. A plugin that *changed* after
    approval is the one to shout about - that is code the user vetted being replaced by code
    they haven't seen.
    """
    for name, reason, root in report.skipped:
        if reason == "changed":
            sys.stderr.write(
                f"picoagent: plugin '{name}' CHANGED since you approved it and was not loaded.\n"
                f"  Review and accept the change:  picoagent plugin trust {root}\n")
        elif reason == "new":
            sys.stderr.write(f"picoagent: plugin '{name}' is not trusted yet and was not loaded.\n"
                             f"  Review and approve it:  picoagent plugin trust {root}\n")
        else:
            sys.stderr.write(f"picoagent: plugin '{name}' failed to load ({reason}); see --verbose.\n")


async def warn_about_ignored_project_keys(rt) -> None:
    """Say so when the repository's config tried to set something only the user may set.

    Dropping these silently would leave two people confused for different reasons: whoever
    wrote the project config wondering why it did nothing, and whoever cloned the repository
    never learning it tried to point their API key somewhere else.
    """
    ignored = rt.cfg.get("_ignored_project_keys") or []
    if not ignored:
        return
    await rt.events.emit("notice", {
        "text": f"ignored {', '.join(ignored)} from this repository's .picoagent/config.toml - "
                "these are read from your own config only. See docs/security/trust-boundaries.md"},
        rt)


# ---------------------------------------------------------------------------- commands

async def run_agent(args: argparse.Namespace) -> int:
    rt = build_runtime(args)
    agent = AgentLoop(rt)
    await warn_about_ignored_project_keys(rt)
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
    """``picoagent plugin add|trust|list``."""
    cfg = load_config(Path(".").resolve())
    trust = loader.TrustStore(Path(cfg["_user_dir"]))
    if args.pcmd == "add":
        try:
            root = loader.resolve_source(args.spec, cfg, project=args.project)
        except subprocess.CalledProcessError:
            print(f"could not fetch {args.spec} (unreachable, or the ref does not exist)")
            return 1
        try:
            manifest = loader.Manifest.load(root)
        except (FileNotFoundError, KeyError) as exc:
            # A repository that isn't a plugin is a normal mistake, not a crash. Say which
            # file is missing and where it was looked for.
            print(f"{root} is not a plugin: {exc}")
            return 1
        loader.install_deps(manifest)
        print(f"installed {manifest.name} {manifest.version} -> {root}\n")
        # Same consent path as `plugin trust`: `add` on an already-installed plugin is an
        # upgrade, and an upgrade is exactly when the user needs to see what changed.
        trust_command(manifest, trust, assume_yes=args.yes)
        config_file = (Path(".picoagent") if args.project else Path(cfg["_user_dir"])) / "config.toml"
        print(f'\nEnable it by adding to {config_file}:\n[plugins]\nenabled = ["{args.spec}"]')
    elif args.pcmd == "trust":
        try:
            manifest = loader.Manifest.load(Path(args.spec).expanduser().resolve())
        except (FileNotFoundError, KeyError) as exc:
            print(f"not a plugin directory: {exc}")
            return 1
        return trust_command(manifest, trust, assume_yes=args.yes)
    elif args.pcmd == "list":
        for directory in (loader.plugins_dir(cfg), loader.plugins_dir(cfg, project=True)):
            for path in sorted(directory.iterdir()) if directory.is_dir() else []:
                if (path / "plugin.toml").exists():
                    manifest = loader.Manifest.load(path)
                    status = {"trusted": "trusted", "changed": "CHANGED", "new": "UNTRUSTED"}[trust.status(manifest)]
                    print(f"{manifest.name:20} {manifest.version:8} {status:10} {path}")
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
    plugin.add_argument("pcmd", choices=["add", "trust", "list"])
    plugin.add_argument("spec", nargs="?", help="git:host/user/repo@ref or a local path")
    plugin.add_argument("--project", action="store_true", help="install under the project instead of the user dir")
    upgrade_p = sub.add_parser("upgrade", help="check for and apply plugin updates")
    upgrade_p.add_argument("ucmd", nargs="?",
                           help="'check' to only report, a plugin name, or omit for all")
    plugin.add_argument("-y", "--yes", action="store_true",
                        help="skip the trust confirmation prompt (scripting; you are accepting the code unseen)")
    return ap


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(name)s: %(message)s")
    if args.cmd == "plugin":
        sys.exit(plugin_command(args))
    if args.cmd == "upgrade":
        sys.exit(upgrade_command(args))
    sys.exit(asyncio.run(run_agent(args)))
