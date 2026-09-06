"""End-to-end tests against a real MCP server, off by default.

Every other MCP test drives ``picoagent/testing/fake_mcp.py``, a server written from the same
reading of the specification as the client. That proves the two agree; it cannot prove either
one matches the protocol, because a misreading is a mistake both sides would share and every
test would agree with. These tests close that gap by talking to a server nobody here wrote:
``@modelcontextprotocol/server-everything``, the protocol's own reference server, run in a
container so it is the same server on every machine.

Opt in explicitly::

    export PICOAGENT_E2E_MCP=1
    python -m unittest discover -s tests -v

Without ``PICOAGENT_E2E_MCP=1`` every test here skips, so ``discover`` stays offline and fast
by default and CI is unaffected on a machine that happens to have Docker.

**Assertions are on protocol facts and side effects, never on prose.** The handshake completed
and recorded a negotiated revision, tools came back from ``tools/list`` and were registered, a
named tool ran and returned content the server produced, and the built-in ``read`` is still
``ReadTool`` afterwards. Those hold across server versions. A green run means the wire format
is right; it says nothing about how many tools the reference server happens to ship this month.

Three things can be missing, and each skips with its own fix: the switch is off, Docker is not
installed, or the daemon is not reachable. Skip means the infrastructure is absent. Failure
means it was present and picoagent misbehaved.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from helpers import ROOT, ScriptedProvider, make_runtime, run, text, tool_ctx
from picoagent.core.loop import Runtime
from picoagent.core.tools import ReadTool
from picoagent.core.types import ToolResult
from picoagent.plugins import loader

PLUGIN = ROOT / "examples/plugins/mcp"
sys.path.insert(0, str(PLUGIN))         # the plugin's own modules, as tests/test_mcp_plugin.py does
from mcp_stdio import PROTOCOL_VERSION  # noqa: E402

#: The switch. Anything other than unset/0/false turns these tests on.
ENABLED = os.environ.get("PICOAGENT_E2E_MCP", "").lower() not in ("", "0", "false", "no")
#: The container the server runs in. Overridable for a mirror or an air-gapped registry.
IMAGE = os.environ.get("PICOAGENT_E2E_MCP_IMAGE", "node:22-alpine")
#: The reference server. Any stdio MCP server ``npx`` can run works, but the assertions below
#: name ``everything_echo``, so a different package needs different names.
PACKAGE = os.environ.get("PICOAGENT_E2E_MCP_PACKAGE", "@modelcontextprotocol/server-everything")
#: Seconds for the handshake and ``tools/list``. High on purpose, and configurable, because this
#: budget covers ``npx`` fetching the package inside a fresh container: ``--rm`` throws the
#: container away, so every run starts on a cold npm cache and pays the download again. On a
#: warm image and a fast link the handshake lands in well under a minute; the ceiling is here to
#: fail a stuck start rather than to pace a healthy one.
STARTUP_TIMEOUT = float(os.environ.get("PICOAGENT_E2E_MCP_STARTUP_TIMEOUT", "300"))
#: Seconds for one ``tools/call``, once the server is up and answering.
CALL_TIMEOUT = float(os.environ.get("PICOAGENT_E2E_MCP_TIMEOUT", "60"))
#: Label key stamped on every container these tests start, so ``docker ps`` finds exactly ours.
RUN_LABEL = "picoagent-e2e-mcp"


# --------------------------------------------------------------------------- the gate

def _docker(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def require_live_mcp() -> None:
    """Skip with an actionable message unless a usable Docker is actually there.

    Three separate causes, three different fixes, so they are reported separately rather than as
    one "not available". "You did not opt in", "Docker is not installed" and "the daemon is not
    running" send you to three different places.
    """
    if not ENABLED:
        raise unittest.SkipTest("set PICOAGENT_E2E_MCP=1 to run the live MCP tests")
    if shutil.which("docker") is None:
        raise unittest.SkipTest(
            "docker is not on PATH. Install Docker, then re-run. The server runs in a container "
            "on purpose: a container is a stdio command like any other, so it needs nothing "
            "installed on the host and behaves the same everywhere."
        )
    try:
        probe = _docker("info", "--format", "{{.ServerVersion}}", timeout=30.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise unittest.SkipTest(f"`docker info` never returned ({exc}); the Docker daemon looks "
                                "wedged. Restart it, then re-run.") from None
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout).strip().splitlines()
        raise unittest.SkipTest(
            "docker is installed but its daemon is not reachable "
            f"({detail[0] if detail else 'no detail'}). Start Docker Desktop, or under WSL "
            "`sudo service docker start`, then re-run."
        )


# --------------------------------------------------------------------------- containers

def new_label() -> str:
    """A value unique to one connection, so a cleanup check cannot see a sibling's container."""
    return f"{os.getpid()}-{uuid.uuid4().hex[:12]}"


def running_containers(label: str) -> list[str]:
    return _docker("ps", "--quiet", "--filter", f"label={RUN_LABEL}={label}").stdout.split()


def settle(label: str, seconds: float = 30.0) -> list[str]:
    """Poll until nothing carrying ``label`` is running, then report what is left.

    Removal is the daemon's work and it finishes after the client exits, so reading ``docker ps``
    the instant the child is gone would report a container that is already on its way out.
    Polling separates "slow to tidy up" from "still running", which is the thing under test.
    """
    deadline = time.monotonic() + seconds
    while True:
        alive = running_containers(label)
        if not alive or time.monotonic() >= deadline:
            return alive
        time.sleep(0.5)


def force_remove(label: str) -> None:
    """Safety net: whatever a failed test leaves behind, the machine does not keep."""
    ids = _docker("ps", "--all", "--quiet", "--filter", f"label={RUN_LABEL}={label}").stdout.split()
    if ids:
        _docker("rm", "--force", *ids)


# --------------------------------------------------------------------------- the connection

def server_entry(label: str) -> dict:
    """One ``[plugins.mcp.servers.<name>]`` table, as a user would write it.

    ``-i`` keeps stdin open, which is the whole transport. There is deliberately no ``-t``: a tty
    would put line discipline between the client and the server and corrupt the framing.
    ``--label`` is the one addition these tests make to the command a user would write, so
    ``docker ps`` can find exactly the containers this run started and the cleanup assertion can
    be proved rather than assumed.
    """
    return {
        "command": "docker",
        "args": ["run", "-i", "--rm", "--label", f"{RUN_LABEL}={label}", IMAGE, "npx", "-y", PACKAGE],
        "startup_timeout": STARTUP_TIMEOUT,
        "timeout": CALL_TIMEOUT,
    }


def connect(tmp: Path, label: str, name: str = "everything") -> Runtime:
    """A Runtime with the mcp plugin loaded against a live containerised server."""
    runtime = make_runtime(tmp, provider=ScriptedProvider([[text("ok")]]))
    runtime.cfg["plugins"]["mcp"] = {"servers": {name: server_entry(label)}}
    loader.load_plugin(PLUGIN, runtime, loader.TrustStore(tmp / "home"), allow_untrusted=True)
    return runtime


def status_report(runtime: Runtime) -> str:
    """``/mcp``: what connected, what it identified itself as, what it refused."""
    return run(runtime.commands.get("mcp").handler("", runtime))


def end_session(runtime: Runtime) -> None:
    run(runtime.events.emit("session_end", {}, runtime))


# --------------------------------------------------------------------------- the tests

class LiveEverythingServerTests(unittest.TestCase):
    """Handshake, discovery, execution and namespacing against one live server.

    One container for the whole class rather than one per test. Each start pays for the package
    download again, so a class per assertion would turn a two-minute file into a ten-minute one
    while testing the same connection four times over.
    """

    @classmethod
    def setUpClass(cls) -> None:
        require_live_mcp()
        cls.label = new_label()
        cls._tmp = tempfile.TemporaryDirectory()
        # Cleanups run last-registered-first: end the session, then sweep strays, then the files.
        cls.addClassCleanup(cls._tmp.cleanup)
        cls.addClassCleanup(force_remove, cls.label)
        cls.tmp = Path(cls._tmp.name)
        cls.rt = connect(cls.tmp, cls.label)
        cls.addClassCleanup(end_session, cls.rt)

    def tool(self, name: str):
        found = self.rt.tools.get(name)
        self.assertIsNotNone(found, f"'{name}' was not registered. /mcp says:\n{status_report(self.rt)}")
        return found

    def call(self, name: str, **args) -> ToolResult:
        return run(self.tool(name).execute(args, tool_ctx(self.tmp)))

    def test_the_handshake_completes_and_the_server_identifies_itself(self):
        server = self.tool("everything_echo").server
        self.assertTrue(server.alive, f"the child is gone. /mcp says:\n{status_report(self.rt)}")
        identity = server.server_info.get("name", "")
        self.assertTrue(identity, "initialize returned no serverInfo.name, so nothing was negotiated")
        # A server answering with a different revision is tolerated by design (the plugin warns and
        # carries on, see the README). Asserting equality here is what proves the revision we
        # announce is one the reference server accepts. If that stops being true, this line is
        # where it surfaces, rather than in somebody's session.
        self.assertEqual(server.negotiated_version, PROTOCOL_VERSION,
                         f"'{identity}' answered a different protocol revision than we announced")
        self.assertIn(identity, status_report(self.rt), "/mcp does not report who answered")

    def test_the_servers_tools_are_discovered_and_registered(self):
        registered = [name for name in self.rt.tools.names() if name.startswith("everything_")]
        # The reference server shipped 13 tools when this was written. The count belongs to the
        # server and moves with its version, so what is asserted is that discovery happened at all
        # and that the tool the next test calls is among what came back.
        self.assertTrue(registered, f"tools/list gave us nothing. /mcp says:\n{status_report(self.rt)}")
        self.assertIn("everything_echo", registered)
        self.assertIn(f"{len(registered)} tool", status_report(self.rt))

    def test_a_named_tool_executes_and_returns_the_servers_content(self):
        result = self.call("everything_echo", message="PICO_MCP_LIVE")
        self.assertFalse(result.is_error, f"echo failed: {result.content}")
        # The server prefixes what it echoes, so the prefix is what proves the text came back
        # through the server instead of out of the arguments we sent.
        self.assertIn("Echo: PICO_MCP_LIVE", result.content)
        self.assertEqual(result.details.get("server"), "everything")

    def test_a_live_server_does_not_replace_a_builtin_tool(self):
        (self.tmp / "note.txt").write_text("PICO_MCP_BUILTIN\n")
        # Registries replace on name, so this is the assertion that the namespacing rule holds
        # against a server whose tool names nobody here chose.
        self.assertIsInstance(self.rt.tools.get("read"), ReadTool)
        self.assertIn("PICO_MCP_BUILTIN", self.call("read", path="note.txt").content)


class ShutdownTests(unittest.TestCase):
    """A server must not outlive the session. This one starts its own connection to prove it."""

    @classmethod
    def setUpClass(cls) -> None:
        require_live_mcp()

    def test_session_end_leaves_no_container_running(self):
        label = new_label()
        self.addCleanup(force_remove, label)     # the machine stays clean even if the assertion fails
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runtime = connect(Path(tmp.name), label)
        echo = runtime.tools.get("everything_echo")
        self.assertIsNotNone(echo, f"nothing connected. /mcp says:\n{status_report(runtime)}")
        self.assertTrue(running_containers(label), "no container carries our label, so the assertion "
                                                   "below would pass without proving anything")
        end_session(runtime)
        self.assertFalse(echo.server.alive, "the child docker process is still up after session_end")
        self.assertEqual(settle(label), [], "session_end left a container running; `docker ps` still "
                                            f"lists it under label {RUN_LABEL}={label}")


if __name__ == "__main__":
    unittest.main()
