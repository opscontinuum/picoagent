"""Three bypasses that were one defect: the gate and the tool named different files.

A ``tool_call`` guard decides "may this path be touched?" from the model's *argument string*,
and the tool then opens whatever :func:`picoagent.core.tools.resolve_path` makes of that string.
Every guard that resolved the string itself drifted from that function, and each drift is a way
past the guard:

* credential-guard resolved against the *process* directory and never stripped the leading
  ``@`` that ``resolve_path`` strips, so ``@<credentials>`` was inspected as a filename with an
  ``@`` in it - which nothing opens - while ``read`` opened the credentials file and put the key
  in a tool result, and the session log replays that into the next prompt.
* permission-gate matched ``protected`` patterns against the spelling, so ``.git/hooks/pre-commit``
  was refused and the absolute spelling of that same file was not.
* ``confine_to_project`` compared a textually normalised path when the file did not exist yet,
  so the OS followed a symlink on the write that the check had not followed.

These tests are written against calls that exist on both sides of the fix, so each one fails
where the defect is rather than at an import. The seam itself is covered in
``test_path_gate_seam.py``.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import CaptureFrontend, ROOT, ScriptedProvider, call, make_runtime, run, text, tool_ctx
from picoagent.core.loop import AgentLoop
from picoagent.core.tools import ReadTool, WriteTool
from picoagent.plugins import loader

sys.path.insert(0, str(ROOT / "examples/plugins/credential-guard"))
sys.path.insert(0, str(ROOT / "examples/plugins/permission-gate"))
import credential_guard as cg          # noqa: E402
import permission_gate as pg           # noqa: E402

PLUGINS = ROOT / "examples/plugins"


def load(rt, name):
    return loader.load_plugin(PLUGINS / name, rt, loader.TrustStore(rt.cwd / "home"),
                              allow_untrusted=True)


class FakeRuntime:
    """What a ``tool_call`` handler actually gets: the runtime, whose ``cfg`` is the same
    dictionary the tool will receive as ``ctx.config`` - ``_cwd`` and confinement included."""

    def __init__(self, cwd: Path, user_dir: Path | None = None):
        self.cwd = cwd
        self.cfg = {"_cwd": str(cwd), "_user_dir": str(user_dir or cwd)}


class CredentialGuardSpellingTests(unittest.TestCase):
    """Symptom A: the guard read one file's name while the tool opened another."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.project = self.tmp / "project"
        self.project.mkdir()
        self.user_dir = self.tmp / "home"
        self.creds = cg.credentials_path(self.user_dir)
        cg.write_credential(self.creds, "openai", "sk-supersecret123456")

    def guard(self, name, args):
        return run(cg.guard_tool_call({"name": name, "args": args},
                                      FakeRuntime(self.project, self.user_dir)))

    def test_a_plain_path_is_blocked(self):
        self.assertTrue(self.guard("read", {"path": str(self.creds)}))

    def test_an_at_prefixed_path_is_blocked(self):
        """``resolve_path`` strips a leading ``@`` because models copy it from ``@file``
        mentions. A guard that does not strip it is guarding a file nothing will open."""
        self.assertTrue(self.guard("read", {"path": "@" + str(self.creds)}))

    def test_a_relative_path_is_resolved_against_the_session_directory(self):
        """``picoagent -C project`` moves the session, not the process, so a guard resolving
        against its own working directory checks a file the tool will never open."""
        self.assertTrue(self.guard("read", {"path": os.path.relpath(self.creds, self.project)}))

    def test_the_key_never_reaches_a_tool_result(self):
        """The whole point of the plugin: a tool result is appended to the session and replayed
        as prompt context on the next turn, so a key that reaches one has leaked."""
        creds = cg.credentials_path(self.project / "home")   # where make_runtime points PICOAGENT_HOME
        cg.write_credential(creds, "openai", "sk-supersecret123456")
        rt = make_runtime(self.project, provider=ScriptedProvider(
            [[call("read", path="@" + str(creds))], [text("ok")]]))
        self.assertEqual(rt.cfg["_user_dir"], str(creds.parent))
        load(rt, "credential-guard")
        run(AgentLoop(rt).run("read the credentials"))
        body = rt.frontend.tool_results()[0].content
        self.assertNotIn("sk-supersecret123456", body)
        self.assertIn("Blocked", body)


class PermissionGateSpellingTests(unittest.TestCase):
    """Symptom B: the gate matched how the model spelled the path, not which file it named."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".git" / "hooks").mkdir(parents=True)
        self.gate = pg.PermissionGate(FakeApi())

    def blocked(self, raw: str, name: str = "write") -> bool:
        verdict = run(self.gate.on_tool_call({"name": name, "args": {"path": raw}},
                                             FakeRuntime(self.tmp)))
        return bool(verdict and verdict.get("block"))

    def test_an_absolute_spelling_of_a_protected_path_is_protected(self):
        """A ``write`` into ``.git/hooks/`` plants code that runs on the user's next commit."""
        self.assertTrue(self.blocked(str(self.tmp / ".git/hooks/pre-commit")))

    def test_a_dot_slash_spelling_is_protected(self):
        self.assertTrue(self.blocked("./.git/config"))

    def test_a_traversal_spelling_is_protected(self):
        self.assertTrue(self.blocked("src/../.git/config"))

    def test_an_at_prefixed_spelling_is_protected(self):
        self.assertTrue(self.blocked("@.git/config"))

    def test_the_spellings_that_already_worked_still_work(self):
        for raw in (".env", ".env.local", ".git/config", "keys/server.pem", "keys/id_rsa"):
            with self.subTest(raw=raw):
                self.assertTrue(self.blocked(raw), f"{raw} stopped being protected")

    def test_reads_of_a_protected_path_are_refused_too(self):
        self.assertTrue(self.blocked(str(self.tmp / ".env"), name="read"))

    def test_an_ordinary_file_is_not_protected(self):
        for raw in ("README.md", "src/main.py", str(self.tmp / "src/main.py")):
            with self.subTest(raw=raw):
                self.assertFalse(self.blocked(raw))

    def test_a_hook_cannot_be_planted_by_absolute_path(self):
        rt = make_runtime(self.tmp, provider=ScriptedProvider(
            [[call("write", path=str(self.tmp / ".git/hooks/pre-commit"), content="pwned")],
             [text("ok")]]), frontend=CaptureFrontend())
        load(rt, "permission-gate")
        run(AgentLoop(rt).run("plant a hook"))
        self.assertTrue(rt.frontend.tool_results()[0].is_error)
        self.assertFalse((self.tmp / ".git/hooks/pre-commit").exists())


class ConfinedSymlinkTests(unittest.TestCase):
    """Symptom C: confinement checked a textual path for a file that did not exist yet."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.project = self.tmp / "confined"
        self.project.mkdir()
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        os.symlink(self.outside, self.project / "link")

    def ctx(self):
        return tool_ctx(self.project, confine_to_project=True)

    def test_a_write_through_a_symlink_is_refused(self):
        result = run(WriteTool().execute({"path": "link/escaped.txt", "content": "pwned"}, self.ctx()))
        self.assertTrue(result.is_error, result.content)
        self.assertFalse((self.outside / "escaped.txt").exists(), "the write escaped the project")

    def test_directories_are_not_created_through_a_symlink(self):
        """``write`` creates parents, so a refusal that arrives after the ``mkdir`` still wrote."""
        run(WriteTool().execute({"path": "link/a/b/c.txt", "content": "x"}, self.ctx()))
        self.assertFalse((self.outside / "a").exists())

    def test_reading_an_existing_file_through_a_symlink_is_still_refused(self):
        (self.outside / "secret.txt").write_text("s\n")
        result = run(ReadTool().execute({"path": "link/secret.txt"}, self.ctx()))
        self.assertTrue(result.is_error)
        self.assertNotIn("s\n", result.content)

    def test_a_symlink_that_stays_inside_the_project_still_works(self):
        (self.project / "sub").mkdir()
        os.symlink(self.project / "sub", self.project / "inner")
        result = run(WriteTool().execute({"path": "inner/f.txt", "content": "x"}, self.ctx()))
        self.assertFalse(result.is_error, result.content)
        self.assertEqual((self.project / "sub/f.txt").read_text(), "x")


class NoGuardResolvesAPathItself(unittest.TestCase):
    """A guard that resolves a path itself has drifted from the tool by definition.

    Static, because the drift is invisible from the outside until someone finds the spelling
    that separates the two. Every behavioural test above passes just as well against a guard
    that has re-implemented resolution correctly *today*.
    """

    def test_the_shipped_guards_go_through_the_seam(self):
        for module in (cg, pg):
            source = Path(module.__file__).read_text()
            with self.subTest(module=module.__name__):
                self.assertIn("resolve_tool_path(", source)
                self.assertNotIn(".expanduser(", source, "resolve a model path with the seam")
                self.assertNotIn(".resolve()", source, "resolve a model path with the seam")


class FakeApi:
    """The two calls ``PermissionGate.__init__`` makes, without a runtime behind them."""
    ui = None

    def plugin_config(self):
        return FakeConfig()

    def warn_about_project_config(self, *accepted):
        pass


class FakeConfig(dict):
    def from_project(self, key, default):
        return default


if __name__ == "__main__":
    unittest.main()
