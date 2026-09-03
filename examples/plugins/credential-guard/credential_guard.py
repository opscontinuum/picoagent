"""credential-guard - stdlib-only credential storage and leak prevention.

Two concerns, kept separate:

1. Storage: ``~/.picoagent/credentials`` - a plain ``provider=key`` file, restricted to the
   owner from the moment it exists (no encryption: same trust model as ``~/.netrc`` or
   ``~/.aws/credentials`` - OS access control, not a hand-rolled cipher). POSIX: real ``chmod
   0600``. Windows: NTFS doesn't use Unix permission bits at all, so this shells out to
   ``icacls`` instead to strip inherited access and grant only the current user - if that fails,
   ``/secrets set`` says so rather than silently claiming protection it can't confirm. The
   ``/secrets``
   command writes to it via ``getpass`` (never echoed, never a command argument, so it never
   becomes a session-logged user message - see core/loop.py: slash commands short-circuit
   ``handle_input`` before ``session.append_message`` is ever called). On load, the resolved
   key for the built-in ``openai``-named provider is re-registered with it, exactly like the
   existing env-var path: the key only ever becomes an HTTP ``Authorization`` header inside
   ``OpenAICompatProvider._request`` - it never enters a ``Message``, so it can't reach the
   session log or a later prompt through that path either.

2. Leak prevention: even with storage handled, the key still lives in ``os.environ`` fallbacks
   and gets loaded into the process either way - and picoagent's built-in ``shell`` tool passes
   the *entire* environment to every command the model runs. If the model runs ``env`` (or, on
   Windows, ``$env:PICOAGENT_API_KEY``), that output becomes a ``ToolResult`` - which the loop
   appends to the session and sends back as prompt context on the next turn. That is the actual
   leak path this plugin closes: a replacement ``shell`` tool strips secret-looking env vars
   before the command ever sees them, and a ``tool_call`` guard blocks ``read``/``write``/
   ``edit``/``grep_search`` (and a shell ``cat``/``Get-Content``-style command) from touching
   the credentials file.
"""
from __future__ import annotations

import asyncio
import getpass
import logging
import os
import re
import sys
import threading
from pathlib import Path

log = logging.getLogger("credential_guard")

_WRITE_LOCK = threading.Lock()   # read-modify-write on the store must not interleave

_UNRESTRICTED_WARNING = (" - WARNING: could not confirm the file is restricted to your user "
                         "account; it may be readable by other accounts on this machine")

_LINE = re.compile(r"^([A-Za-z0-9_-]+)=(.*)$")
_DEFAULT_DENY = re.compile(r"(?i)(api[_-]?key|token|secret|password|passwd|credential|auth|bearer|cookie)")
_CAT_LIKE = re.compile(r"\b(cat|less|more|head|tail|type|bat|Get-Content|gc)\b", re.I)

# Variables a subprocess genuinely needs, and that don't carry credentials. Everything else is
# dropped: a denylist of secret-shaped *names* can never be complete (OPENROUTER_KEY, GH_PAT,
# PRIVATE_KEY, DATABASE_URL and AWS_ACCESS_KEY_ID all sail through one), so this fails closed
# instead. Add project-specific names via `extra_allow` in [plugins.credential-guard].
_ALLOWED_ENV = {
    # POSIX
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ",
    "TMPDIR", "PWD", "DISPLAY",
    # Windows
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "APPDATA",
    "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "SYSTEMDRIVE",
    "NUMBER_OF_PROCESSORS", "OS", "PROCESSOR_ARCHITECTURE", "USERNAME", "COMPUTERNAME",
    # toolchain locations (paths, not credentials)
    "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX", "JAVA_HOME", "GOPATH", "GOROOT",
    "CARGO_HOME", "RUSTUP_HOME", "NODE_PATH", "NVM_DIR", "DOTNET_ROOT",
}


# --------------------------------------------------------------------------- storage

def credentials_path(user_dir: Path) -> Path:
    return user_dir / "credentials"


def read_credentials(path: Path) -> dict[str, str]:
    """Parse the credentials file. Missing file or bad lines are simply absent, not errors."""
    if not path.is_file():
        return {}
    creds: dict[str, str] = {}
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _LINE.match(line)
        if match:
            creds[match.group(1)] = match.group(2)
    return creds


def write_credential(path: Path, provider: str, key: str) -> bool:
    """Set one provider's key, preserving the others. Returns whether owner-only access was
    actually confirmed (see ``_restrict_to_owner`` - on Windows this can fail silently at the
    OS level, so callers should surface a ``False`` return to the user rather than assume it).

    Raises ``ValueError`` on a key containing a newline: the file is one ``name=value`` per
    line, so a newline would silently split the entry and corrupt the store.
    """
    if "\n" in key or "\r" in key:
        raise ValueError("a key cannot contain a newline")
    with _WRITE_LOCK:
        creds = read_credentials(path)
        creds[provider] = key
        return _write_all(path, creds)


def delete_credential(path: Path, provider: str) -> bool:
    """Remove one provider's key. Returns whether there was one to remove."""
    with _WRITE_LOCK:
        creds = read_credentials(path)
        if provider not in creds:
            return False
        del creds[provider]
        _write_all(path, creds)   # restriction status is re-checked by the caller
        return True


def _restrict_to_owner(path: Path) -> bool:
    """Make the file readable/writable only by its owner. Returns whether that's confirmed.

    POSIX: ``chmod 0600`` is real, guaranteed access control.
    Windows: chmod's mode bits don't map to real security there at all - NTFS uses ACLs, not
    Unix permission bits - so this shells out to ``icacls`` (built into every Windows install)
    to strip inherited permissions and grant only the current user. If that fails (icacls
    missing, no ``USERNAME`` env var, non-NTFS volume...) this returns False rather than
    pretending the file is protected - the caller must decide what to tell the user, not this
    function.
    """
    from picoagent.core.tools import is_windows
    if is_windows():
        import subprocess
        user = os.environ.get("USERNAME")
        if not user:
            return False
        try:
            result = subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
                capture_output=True, timeout=10)
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    os.chmod(path, 0o600)
    return True


def _write_all(path: Path, creds: dict[str, str]) -> bool:
    """Write the whole file atomically-ish, then restrict it to the owner. Returns whether
    that restriction was actually confirmed (see ``_restrict_to_owner``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{name}={value}\n" for name, value in creds.items())
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)   # 0o600 is a no-op on
    try:                                                               # Windows; _restrict_to_owner
        os.write(fd, body.encode())                                   # below is what actually
    finally:                                                          # matters there.
        os.close(fd)
    return _restrict_to_owner(path)


def mask(value: str) -> str:
    """Last 4 characters only - enough to recognise which key it is, never enough to reuse it."""
    return f"...{value[-4:]}" if len(value) > 4 else "..."


# --------------------------------------------------------------------------- env sanitisation

def sanitized_env(base_env: dict[str, str], extra_deny: list[str] | None = None,
                  extra_allow: list[str] | None = None) -> dict[str, str]:
    """Build the environment a subprocess is allowed to see.

    Allowlist, not denylist: only ``_ALLOWED_ENV`` (plus any ``extra_allow`` names and
    ``PICOAGENT_*`` settings that aren't themselves secret-shaped) survive. Naming a variable
    something a denylist doesn't recognise is the single easiest way to leak a key, so the
    default is to drop anything not positively known to be safe. ``extra_deny`` still applies
    on top, so an explicitly allowed name that looks secret-shaped is still refused.
    """
    allowed = _ALLOWED_ENV | {name.upper() for name in (extra_allow or [])}
    deny = [_DEFAULT_DENY] + [re.compile(p, re.I) for p in (extra_deny or [])]

    def is_safe(name: str) -> bool:
        if any(pattern.search(name) for pattern in deny):
            return False        # secret-shaped wins, even if explicitly allowed
        return name.upper() in allowed or name.upper().startswith("PICOAGENT_")

    return {k: v for k, v in base_env.items() if is_safe(k)}


class GuardedShellTool:
    """Drop-in replacement for the built-in ``shell`` tool that never exposes secret-looking
    env vars to the command it runs. Reuses picoagent.core.tools' spawn_shell/kill_process_tree
    (the same PowerShell-on-Windows / sh-on-POSIX dispatch and timeout handling the built-in
    tool uses) rather than duplicating that logic - only the environment construction differs."""
    name = "shell"
    description = ("Run a shell command in the project directory (bash/sh on Linux and macOS, "
                   "PowerShell on Windows - detected automatically). Returns stdout+stderr and "
                   "exit code. Environment variables that look like secrets (API keys, tokens, "
                   "passwords, credentials) are NOT visible to this command, by design - if you "
                   "need one, use the `structured_data`/config tools to read a config file instead "
                   "of expecting it in the environment.")
    parameters = {"type": "object", "properties": {"command": {"type": "string"},
                  "timeout": {"type": "integer"}}, "required": ["command"]}

    def __init__(self, extra_deny: list[str] | None = None, extra_allow: list[str] | None = None):
        self.extra_deny = extra_deny or []
        self.extra_allow = extra_allow or []

    async def execute(self, args: dict, ctx) -> "ToolResult":
        from picoagent.core.tools import kill_process_tree, spawn_shell
        from picoagent.core.types import ToolResult

        timeout = int(args.get("timeout") or ctx.config.get("shell_timeout", 120))
        env = sanitized_env(os.environ, self.extra_deny, self.extra_allow)
        env["PICOAGENT"] = "1"
        proc = await spawn_shell(args["command"], ctx.cwd, env)
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await kill_process_tree(proc)
            return ToolResult(ctx.tool_call_id, f"Command timed out after {timeout}s", is_error=True)

        output = stdout.decode(errors="replace")
        body = output if len(output) < 50_000 else output[-50_000:] + "\n[output truncated]"
        body += f"\n[exit code {proc.returncode}]"
        return ToolResult(ctx.tool_call_id, body, is_error=proc.returncode != 0,
                          details={"exit_code": proc.returncode})


# --------------------------------------------------------------------------- file protection

def protected_files(rt) -> list[Path]:
    """Every file that can hold a key: the credentials store and the config files.

    ``config.toml`` matters as much as the credentials file - ``[providers.<name>] api_key``
    is a documented place to put a key (core/config.py), and unlike the credentials file
    nothing was restricting or guarding it.
    """
    user_dir = Path(rt.cfg["_user_dir"])
    paths = [credentials_path(user_dir), user_dir / "config.toml", user_dir / "trust.json"]
    cwd = rt.cfg.get("_cwd")
    if cwd:
        paths.append(Path(cwd) / ".picoagent" / "config.toml")
    return paths


def _same_file(a: Path, b: Path) -> bool:
    """Identity by inode, so a symlink or hardlink alias doesn't slip past a path comparison."""
    try:
        sa, sb = a.stat(), b.stat()
    except OSError:
        return False
    return (sa.st_ino, sa.st_dev) == (sb.st_ino, sb.st_dev)


def _resolve(raw: str) -> Path | None:
    try:
        return Path(raw).expanduser().resolve()
    except (OSError, RuntimeError):
        return None


def _path_arguments(args: dict) -> list[str]:
    """Every string argument, because guarding a list of *names* is the same mistake as
    guarding a list of tools.

    This used to check ("path", "file", "filename", "filepath"). An audit found that
    ``stig_evidence`` takes ``repo``, so it walked the credentials directory untouched - and
    ``root``, ``directory`` and ``target`` were equally free. Enumerating argument names means
    every new tool is a fresh chance to pick a name nobody listed. Resolving a string that is
    not a path is harmless: it will not equal a protected file.
    """
    found: list[str] = []
    for value in args.values():
        if isinstance(value, str) and value:
            found.append(value)
        elif isinstance(value, (list, tuple)):
            found.extend(item for item in value if isinstance(item, str) and item)
    return found


def _targets_protected_file(args: dict, protected: list[Path]) -> bool:
    """True if any path argument names a protected file (directly, or via a link alias)."""
    for raw in _path_arguments(args):
        target = _resolve(raw)
        if target is not None and any(target == _resolve(str(p)) or _same_file(target, p)
                                      for p in protected):
            return True
    return False


def _would_recurse_into_protected(args: dict, protected: list[Path]) -> bool:
    """True if a *recursive* tool is pointed at a directory containing a protected file.

    This is the hole that made the direct-path check useless: ``grep_search`` takes a
    directory and walks it, so pointing it at ``~/.picoagent`` (or any ancestor) dumped the
    credentials file's contents into a tool result without ever naming the file.
    """
    for raw in _path_arguments(args) or ["."]:
        target = _resolve(raw)
        if target is None:
            continue
        for path in protected:
            resolved = _resolve(str(path))
            if resolved is not None and target in resolved.parents:
                return True
    return False


def _shell_command_targets_protected(args: dict, protected: list[Path]) -> bool:
    """Cheap speed bump only - see the README: a shell running as you can read any file you can."""
    command = args.get("command", "")
    if not _CAT_LIKE.search(command):
        return False
    return any(str(p) in command or p.name in command for p in protected)


#: Tools that walk a directory rather than opening one file, so pointing them at a *containing*
#: directory is enough to surface a protected file. Plugins adding their own recursive tool
#: should list it under [plugins.credential-guard] recursive_tools.
_RECURSIVE_TOOLS = ("grep_search", "stig_evidence")


async def guard_tool_call(event: dict, rt) -> dict | None:
    """``tool_call`` handler: block calls that would surface a protected file's contents.

    The path check deliberately applies to *every* tool, not a list of known ones: an audit
    found that naming the tools to guard (read/write/edit/grep_search) let ``structured_data``
    read and pretty-print the key straight out of config.toml, and any tool added later would
    have inherited the same hole. Fail closed - if a call names a protected path, it's blocked
    whatever the tool is called.
    """
    name, args = event["name"], event["args"]
    protected = protected_files(rt)
    reason = "protected: it can hold an API key - use /secrets to manage keys"
    recursive = tuple(rt.cfg.get("plugins", {}).get("credential-guard", {})
                      .get("recursive_tools", _RECURSIVE_TOOLS))

    if _targets_protected_file(args, protected):
        return {"block": True, "reason": reason}
    if name in recursive and _would_recurse_into_protected(args, protected):
        return {"block": True, "reason": f"that search would recurse into a file {reason}"}
    if name == "shell" and _shell_command_targets_protected(args, protected):
        return {"block": True, "reason": reason}
    return None


# --------------------------------------------------------------------------- /secrets command

async def _prompt_secret(prompt: str) -> str | None:
    """Masked terminal input, off the event loop. None if there's no real terminal to read from."""
    if not sys.stdin.isatty():
        return None
    return await asyncio.get_running_loop().run_in_executor(None, getpass.getpass, prompt)


async def secrets_command(args: str, rt) -> str:
    action, _, rest = args.strip().partition(" ")
    provider = rest.strip()
    creds_path = credentials_path(Path(rt.cfg["_user_dir"]))

    if action == "list":
        creds = read_credentials(creds_path)
        return "stored keys: " + (", ".join(sorted(creds)) or "(none)")

    if action == "show" and provider:
        creds = read_credentials(creds_path)
        return f"{provider}: {mask(creds[provider])}" if provider in creds else f"{provider}: not stored"

    if action == "delete" and provider:
        if not delete_credential(creds_path, provider):
            return f"{provider}: not stored"
        note = "" if _restrict_to_owner(creds_path) else _UNRESTRICTED_WARNING
        return f"deleted {provider}{note}"

    if action == "set" and provider:
        key = await _prompt_secret(f"key for {provider} (hidden, not echoed): ")
        if key is None:
            return ("cannot prompt for a key without an interactive terminal - "
                    "run this from an interactive session, not -p/--json")
        if not key:
            return "empty key, nothing stored"
        try:
            restricted = write_credential(creds_path, provider, key)
        except ValueError as exc:
            return f"not stored: {exc}"
        _refresh_openai_provider(rt, creds_path)
        note = "" if restricted else _UNRESTRICTED_WARNING
        return f"stored a key for {provider} ({mask(key)}) at {creds_path}{note}"

    return "usage: /secrets set|show|delete|list [provider]"


def _refresh_openai_provider(rt, creds_path: Path) -> None:
    """Re-register the built-in 'openai' provider with a freshly stored key, if there is one."""
    from picoagent.core.provider import OpenAICompatProvider
    creds = read_credentials(creds_path)
    if "openai" not in creds:
        return
    existing = rt.cfg.get("providers", {}).get("openai", {})
    rt.providers.register(OpenAICompatProvider(base_url=existing.get("base_url"),
                                                api_key=creds["openai"],
                                                extra_headers=existing.get("headers")))


# --------------------------------------------------------------------------- startup checks

def providers_with_inline_keys(cfg: dict) -> list[str]:
    """Providers whose key is sitting in config.toml rather than the credentials store."""
    providers = cfg.get("providers") or {}
    return sorted(name for name, settings in providers.items()
                  if isinstance(settings, dict) and settings.get("api_key"))


def harden_config_files(rt) -> list[str]:
    """Restrict every existing config file to its owner. Returns those that couldn't be confirmed.

    config.toml is a documented place to put ``api_key``, but nothing ever restricted it - it
    is created at whatever the umask gives (0644 here), i.e. readable by every account on the
    machine, while the credentials file next to it was carefully locked to 0600.
    """
    unconfirmed = []
    for path in protected_files(rt):
        if path.exists() and not _restrict_to_owner(path):
            unconfirmed.append(str(path))
    return unconfirmed


async def warn_about_inline_keys(event: dict, rt) -> None:
    """``session_start`` handler: say so, once, if a key is somewhere weaker than /secrets."""
    messages = []
    inline = providers_with_inline_keys(rt.cfg)
    if inline:
        messages.append(f"credential-guard: {', '.join(inline)} has an api_key in config.toml. "
                        "Prefer `/secrets set <provider>`, which stores it owner-only and keeps "
                        "it out of a file other tools read.")
    unconfirmed = harden_config_files(rt)
    if unconfirmed:
        messages.append("credential-guard: could not confirm owner-only access on "
                        + ", ".join(unconfirmed))
    if messages and rt.frontend:
        await rt.frontend.emit("notice", {"text": "\n".join(messages)})


# --------------------------------------------------------------------------- register

def register(api):
    config = api.plugin_config()
    api.register_tool(GuardedShellTool(extra_deny=config.get("extra_deny_patterns"),
                                        extra_allow=config.get("extra_allow_env")))
    api.on("tool_call", guard_tool_call)
    api.on("session_start", warn_about_inline_keys)
    api.register_command("secrets", secrets_command,
                         "manage stored API keys: /secrets set|show|delete|list [provider]")
    _refresh_openai_provider(api.rt, credentials_path(Path(api.rt.cfg["_user_dir"])))
