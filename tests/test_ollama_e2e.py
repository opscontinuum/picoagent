"""End-to-end tests against a real Ollama server, off by default.

Every other test in this suite drives ``ScriptedProvider`` or a fake HTTP server, which
proves the loop's logic but never the wire. These tests close that gap: a real model on a
real server, picoagent's own ``OpenAICompatProvider``, and the built-in tools writing to a
real temp directory. What they catch is the class of bug a fake cannot have - a request body
Ollama rejects, tool-call fragments reassembled wrongly, an SSE frame shape we never modelled.

Opt in explicitly::

    export PICOAGENT_E2E_OLLAMA=1
    export PICOAGENT_E2E_OLLAMA_URL=http://localhost:11434   # WSL -> Windows host: the gateway IP
    export PICOAGENT_E2E_OLLAMA_MODEL=devstral:24b
    python -m unittest discover -s tests -v

Without ``PICOAGENT_E2E_OLLAMA=1`` every test here skips, so ``discover`` stays offline and
fast by default and CI is unaffected.

**Assertions are on side effects and protocol, never on the model's prose.** A live model
phrases things differently every run, so asserting on wording buys flakiness and proves
nothing. What is deterministic is what the *tools* did: a file exists on disk, a tool result
carries a token the tool itself read, a ``shell`` call ran. Those hold across models,
temperatures and versions. A green run means the wiring works; it is not a quality benchmark.

Skip means the infrastructure is absent. Failure means the infrastructure is present and
picoagent or the model misbehaved. Nothing here passes silently when it did not run.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from helpers import CaptureFrontend, make_runtime, run
from picoagent.core.loop import AgentLoop, Runtime
from picoagent.core.provider import OpenAICompatProvider

#: The switch. Anything other than unset/0/false turns these tests on.
ENABLED = os.environ.get("PICOAGENT_E2E_OLLAMA", "").lower() not in ("", "0", "false", "no")
#: Ollama's root. Not the ``/v1`` OpenAI-compatible path - both are derived from this.
URL = os.environ.get("PICOAGENT_E2E_OLLAMA_URL", "http://localhost:11434").rstrip("/")
#: Must be a tool-calling model. One that cannot emit tool calls fails these tests correctly.
MODEL = os.environ.get("PICOAGENT_E2E_OLLAMA_MODEL", "devstral:24b")
#: Wall clock for one agent run. A 24B model on a warm GPU answers well inside this.
TIMEOUT = float(os.environ.get("PICOAGENT_E2E_TIMEOUT", "180"))
#: Bound on model/tool round trips. See :func:`_cap_turns` for why this is not optional.
MAX_TURNS = int(os.environ.get("PICOAGENT_E2E_MAX_TURNS", "8"))


# --------------------------------------------------------------------------- the gate

def _get_json(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _installed_models() -> list[str]:
    """Model tags the server has pulled, via Ollama's native ``/api/tags``."""
    return [entry["name"] for entry in _get_json(f"{URL}/api/tags").get("models", [])]


def _matches(installed: str, wanted: str) -> bool:
    """``devstral`` should match an installed ``devstral:latest``; a tagged name must match exactly."""
    return installed == wanted or (":" not in wanted and installed.split(":")[0] == wanted)


def require_live_ollama() -> None:
    """Skip with an actionable message unless a usable Ollama is actually there.

    Three separate causes, three different fixes, so they are reported separately rather than
    as one "not available". Being told the server is up but the model is missing is the
    difference between a one-line fix and a hunt.
    """
    if not ENABLED:
        raise unittest.SkipTest("set PICOAGENT_E2E_OLLAMA=1 to run the live Ollama tests")
    try:
        installed = _installed_models()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise unittest.SkipTest(
            f"no Ollama at {URL} ({exc}). Start it, or set PICOAGENT_E2E_OLLAMA_URL. "
            "From WSL to a Windows host use the gateway address in `ip route show default`, "
            "and start Ollama with OLLAMA_HOST=0.0.0.0 so it listens beyond loopback."
        ) from None
    if not any(_matches(name, MODEL) for name in installed):
        raise unittest.SkipTest(
            f"model '{MODEL}' is not installed on {URL} (has: {', '.join(installed) or 'nothing'}). "
            f"Run `ollama pull {MODEL}`, or set PICOAGENT_E2E_OLLAMA_MODEL to one listed."
        )


# --------------------------------------------------------------------------- driving a live model

class DeterministicProvider(OpenAICompatProvider):
    """The real client with sampling pinned to temperature 0.

    Sampling temperature is not part of what these tests exercise; the request body, tool-call
    reassembly and tool execution are. At the server's default temperature the same prompt makes
    the model call a tool on one run and answer from memory on the next, which turns a wiring
    test into a coin flip. Pinning it removes that variance without changing anything under test:
    the body still goes through ``_request`` unmodified in every other respect.

    picoagent has no temperature setting to configure, hence the subclass rather than a config
    key. Nothing else in the suite needs one, so the knob stays here rather than in core.
    """

    def _request(self, *args, **kwargs) -> urllib.request.Request:
        request = super()._request(*args, **kwargs)
        body = json.loads(request.data.decode("utf-8"))
        body["temperature"] = 0
        request.data = json.dumps(body).encode("utf-8")   # the setter drops the stale Content-length
        return request


def _cap_turns(runtime: Runtime) -> None:
    """Abort the run after :data:`MAX_TURNS` model/tool round trips.

    ``AgentLoop._turns`` loops until the model answers without tool calls, bounded only by
    ``rt.abort``. A scripted provider always runs out of script, so the existing suite never
    needs a cap. A live model can keep calling tools indefinitely, which would hang the suite
    rather than fail it, so the bound is imposed here instead of in core.
    """
    turns = 0

    def on_turn_end(payload: dict, _runtime: Runtime) -> None:
        nonlocal turns
        turns += 1
        if turns >= MAX_TURNS:
            runtime.abort.set()

    runtime.events.on("turn_end", on_turn_end)


def live_runtime(tmp: Path) -> Runtime:
    """A Runtime wired exactly as the CLI wires it, pointing at the live server and a temp cwd."""
    runtime = make_runtime(tmp, provider=DeterministicProvider(base_url=f"{URL}/v1", api_key="ollama"))
    runtime.provider_name = "openai"
    runtime.model = MODEL
    _cap_turns(runtime)
    return runtime


async def _drive(runtime: Runtime, prompt: str) -> None:
    try:
        await asyncio.wait_for(AgentLoop(runtime).run(prompt), TIMEOUT)
    except asyncio.TimeoutError:
        runtime.abort.set()          # stop the in-flight turn before the test reports
        raise


class LiveOllamaTest(unittest.TestCase):
    """Base class: the gate, a temp working directory, and one helper that runs a prompt."""

    @classmethod
    def setUpClass(cls) -> None:
        require_live_ollama()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def agent(self, prompt: str) -> CaptureFrontend:
        """Run one prompt to settle and return the frontend that recorded it."""
        runtime = live_runtime(self.tmp)
        try:
            run(_drive(runtime, prompt))
        except asyncio.TimeoutError:
            self.fail(f"'{MODEL}' did not settle within {TIMEOUT:.0f}s. Raise PICOAGENT_E2E_TIMEOUT "
                      "if the model is large or cold, or check `ollama ps` for a CPU fallback.")
        return runtime.frontend

    # ---------------------------------------------------------------- assertions on side effects
    def tool_names(self, frontend: CaptureFrontend) -> list[str]:
        return [payload["call"].name for event, payload in frontend.events if event == "tool_start"]

    def assertNoErrors(self, frontend: CaptureFrontend) -> None:
        errors = [payload["text"] for event, payload in frontend.events if event == "error"]
        self.assertEqual(errors, [], f"the provider reported: {errors}")

    def assertCalled(self, frontend: CaptureFrontend, tool: str) -> None:
        names = self.tool_names(frontend)
        self.assertIn(tool, names, f"'{MODEL}' never called '{tool}' (it called: {names or 'nothing'}). "
                                   "A model that cannot emit tool calls fails this correctly.")

    def assertToolOutputContains(self, frontend: CaptureFrontend, needle: str) -> None:
        """Assert a token appears in what a tool returned, not in what the model said about it."""
        outputs = [result.content or "" for result in frontend.tool_results()]
        self.assertTrue(any(needle in out for out in outputs),
                        f"no tool result contained {needle!r}; results were {outputs}")


# --------------------------------------------------------------------------- the tests

class AvailabilityTests(LiveOllamaTest):
    """The gate, made explicit, plus picoagent's own model listing against a real server."""

    def test_the_server_lists_the_configured_model(self):
        models = run(OpenAICompatProvider(base_url=f"{URL}/v1", api_key="ollama").list_models())
        self.assertTrue(any(_matches(name, MODEL) for name in models),
                        f"{URL}/v1/models did not offer '{MODEL}'; it offered {models}")


class ChatTests(LiveOllamaTest):
    """A turn that needs no tools: streaming works and the loop terminates."""

    def test_a_plain_turn_streams_text(self):
        frontend = self.agent("Reply with a one-sentence greeting. Do not use any tools.")
        self.assertNoErrors(frontend)
        deltas = [event for event, _ in frontend.events if event == "assistant_delta"]
        self.assertTrue(deltas, "no assistant_delta events: nothing streamed back")
        self.assertTrue(frontend.text.strip(), "the model streamed only whitespace")


class ToolLoopTests(LiveOllamaTest):
    """The real point of these tests: tool calls survive the round trip to Ollama and back."""

    def test_the_model_reads_a_file(self):
        (self.tmp / "secret.txt").write_text("the passphrase is PICO_READ_7742\n")
        # Phrased so the answer is impossible without the file. Asking it to "read secret.txt and
        # tell me what it says" leaves a plausible non-tool reply available, and devstral takes it
        # about one run in five ("I don't have the capability to access or read files directly").
        frontend = self.agent("What is the passphrase stored in secret.txt in the current "
                              "directory? Read the file, then quote the passphrase exactly.")
        self.assertNoErrors(frontend)
        self.assertCalled(frontend, "read")
        # The tool did the reading, so the token in its result is deterministic even though
        # whatever the model says about it afterwards is not.
        self.assertToolOutputContains(frontend, "PICO_READ_7742")

    def test_the_model_writes_a_file(self):
        frontend = self.agent("Use the write tool to create a file named result.txt in the current "
                              "directory whose entire contents are exactly: PICO_WRITE_1908")
        self.assertNoErrors(frontend)
        self.assertCalled(frontend, "write")
        written = self.tmp / "result.txt"
        self.assertTrue(written.exists(), f"result.txt was not created (cwd holds: "
                                          f"{[p.name for p in self.tmp.iterdir()]})")
        self.assertIn("PICO_WRITE_1908", written.read_text())

    def test_a_read_then_edit_takes_more_than_one_turn(self):
        target = self.tmp / "config.txt"
        target.write_text("mode = PICO_OLD_5521\nkeep_this = yes\n")
        frontend = self.agent("Read config.txt in the current directory, then use the edit tool to "
                              "replace PICO_OLD_5521 with PICO_NEW_6630. Change nothing else.")
        self.assertNoErrors(frontend)
        contents = target.read_text()
        self.assertIn("PICO_NEW_6630", contents, "the edit never landed on disk")
        self.assertNotIn("PICO_OLD_5521", contents, "the old value is still there")
        self.assertIn("keep_this = yes", contents, "the edit clobbered a line it should not have")
        turn_ends = [event for event, _ in frontend.events if event == "assistant_end"]
        self.assertGreaterEqual(len(turn_ends), 2, "a read-then-edit should take at least two turns")

    def test_the_model_runs_a_shell_command(self):
        frontend = self.agent("Use the shell tool to run exactly this command: echo PICO_SHELL_3364")
        self.assertNoErrors(frontend)
        self.assertCalled(frontend, "shell")
        self.assertToolOutputContains(frontend, "PICO_SHELL_3364")


if __name__ == "__main__":
    unittest.main()
