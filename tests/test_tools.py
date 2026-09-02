"""Built-in tools: read/write/edit/shell, truncation, and the per-file mutation lock."""
import asyncio, tempfile, unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from helpers import run, tool_ctx
from picoagent.core.tools import (ShellTool, EditTool, ReadTool, ToolRegistry, WriteTool, truncate,
                                  spawn_shell, kill_process_tree)


class ReadToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()); (self.tmp / "f.txt").write_text("a\nb\nc\nd\n")

    def test_numbers_lines(self):
        r = run(ReadTool().execute({"path": "f.txt"}, tool_ctx(self.tmp)))
        self.assertIn("     1\ta", r.content); self.assertFalse(r.is_error)

    def test_offset_and_limit(self):
        r = run(ReadTool().execute({"path": "f.txt", "offset": 2, "limit": 2}, tool_ctx(self.tmp)))
        self.assertEqual([l.split("\t")[1] for l in r.content.splitlines()], ["b", "c"])

    def test_missing_file_is_an_error_not_an_exception(self):
        r = run(ReadTool().execute({"path": "nope.txt"}, tool_ctx(self.tmp)))
        self.assertTrue(r.is_error)

    def test_directory_lists_entries(self):
        r = run(ReadTool().execute({"path": "."}, tool_ctx(self.tmp)))
        self.assertIn("f.txt", r.content)

    def test_strips_leading_at_from_path(self):
        r = run(ReadTool().execute({"path": "@f.txt"}, tool_ctx(self.tmp)))
        self.assertFalse(r.is_error)

    def test_large_file_is_truncated_with_hint(self):
        (self.tmp / "big.txt").write_text("\n".join(str(i) for i in range(5000)))
        r = run(ReadTool().execute({"path": "big.txt"}, tool_ctx(self.tmp, tool_output_max_lines=100)))
        self.assertIn("truncated", r.content); self.assertIn("offset/limit", r.content)


class WriteEditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_write_creates_parents(self):
        run(WriteTool().execute({"path": "a/b/c.txt", "content": "x"}, tool_ctx(self.tmp)))
        self.assertEqual((self.tmp / "a/b/c.txt").read_text(), "x")

    def test_edit_replaces_unique_match(self):
        (self.tmp / "f.py").write_text("x = 1\ny = 2\n")
        r = run(EditTool().execute({"path": "f.py", "old_text": "y = 2", "new_text": "y = 3"}, tool_ctx(self.tmp)))
        self.assertFalse(r.is_error); self.assertEqual((self.tmp / "f.py").read_text(), "x = 1\ny = 3\n")

    def test_edit_refuses_ambiguous_match(self):
        (self.tmp / "f.py").write_text("a\na\n")
        r = run(EditTool().execute({"path": "f.py", "old_text": "a", "new_text": "b"}, tool_ctx(self.tmp)))
        self.assertTrue(r.is_error); self.assertIn("2 times", r.content)

    def test_edit_replace_all(self):
        (self.tmp / "f.py").write_text("a\na\n")
        run(EditTool().execute({"path": "f.py", "old_text": "a", "new_text": "b", "replace_all": True}, tool_ctx(self.tmp)))
        self.assertEqual((self.tmp / "f.py").read_text(), "b\nb\n")

    def test_edit_missing_text_is_error(self):
        (self.tmp / "f.py").write_text("a\n")
        r = run(EditTool().execute({"path": "f.py", "old_text": "zzz", "new_text": "b"}, tool_ctx(self.tmp)))
        self.assertTrue(r.is_error)

    def test_parallel_edits_to_same_file_serialize(self):
        """Two concurrent edits must both land (no lost update)."""
        (self.tmp / "f.txt").write_text("one two")
        async def both():
            await asyncio.gather(
                EditTool().execute({"path": "f.txt", "old_text": "one", "new_text": "1"}, tool_ctx(self.tmp)),
                EditTool().execute({"path": "f.txt", "old_text": "two", "new_text": "2"}, tool_ctx(self.tmp)))
        run(both())
        self.assertEqual((self.tmp / "f.txt").read_text(), "1 2")


class ShellToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_captures_output_and_exit_code(self):
        r = run(ShellTool().execute({"command": "echo hi; exit 3"}, tool_ctx(self.tmp)))
        self.assertIn("hi", r.content); self.assertTrue(r.is_error); self.assertEqual(r.details["exit_code"], 3)

    def test_runs_in_project_cwd(self):
        r = run(ShellTool().execute({"command": "pwd"}, tool_ctx(self.tmp)))
        self.assertIn(str(self.tmp.resolve()), r.content)

    def test_timeout_is_reported(self):
        r = run(ShellTool().execute({"command": "sleep 5", "timeout": 1}, tool_ctx(self.tmp)))
        self.assertTrue(r.is_error); self.assertIn("timed out", r.content)

    def test_long_output_is_truncated_from_the_tail_and_spilled(self):
        r = run(ShellTool().execute({"command": "seq 1 5000"}, tool_ctx(self.tmp, tool_output_max_lines=50)))
        self.assertIn("5000", r.content); self.assertNotIn("\n1\n", r.content); self.assertIn("full output:", r.content)


class ShellDispatchTests(unittest.TestCase):
    """Windows can't be run here, so these prove the *dispatch logic* is correct via mocks:
    right platform check, right executable, right arguments - not a live PowerShell process."""

    def test_posix_spawns_via_create_subprocess_shell_with_its_own_process_group(self):
        with patch("picoagent.core.tools.platform.system", return_value="Linux"), \
             patch("picoagent.core.tools.asyncio.create_subprocess_shell", new_callable=AsyncMock) as spawn:
            run(spawn_shell("echo hi", self.tmp_path(), {"PATH": "/bin"}))
        spawn.assert_awaited_once()
        args, kwargs = spawn.call_args
        self.assertEqual(args[0], "echo hi")
        self.assertTrue(kwargs["start_new_session"])

    def test_windows_spawns_powershell_with_create_new_process_group(self):
        with patch("picoagent.core.tools.platform.system", return_value="Windows"), \
             patch("picoagent.core.tools.asyncio.create_subprocess_exec", new_callable=AsyncMock) as spawn:
            run(spawn_shell("Get-ChildItem", self.tmp_path(), {"PATH": "/bin"}))
        spawn.assert_awaited_once()
        args, kwargs = spawn.call_args
        self.assertEqual(args[:5], ("powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-ChildItem"))
        self.assertIn("creationflags", kwargs)

    def test_posix_kill_process_tree_uses_killpg(self):
        proc = MagicMock(pid=1234)
        proc.wait = AsyncMock(return_value=None)
        with patch("picoagent.core.tools.platform.system", return_value="Linux"), \
             patch("picoagent.core.tools.os.killpg") as killpg:
            run(kill_process_tree(proc))
        killpg.assert_called_once()
        self.assertEqual(killpg.call_args.args[0], 1234)

    def test_windows_kill_process_tree_shells_out_to_taskkill(self):
        proc = MagicMock(pid=4321)
        proc.wait = AsyncMock(return_value=None)
        killer = MagicMock()
        killer.wait = AsyncMock(return_value=None)
        with patch("picoagent.core.tools.platform.system", return_value="Windows"), \
             patch("picoagent.core.tools.asyncio.create_subprocess_exec",
                   new_callable=AsyncMock, return_value=killer) as spawn:
            run(kill_process_tree(proc))
        spawn.assert_awaited_once_with("taskkill", "/F", "/T", "/PID", "4321",
                                       stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)

    @staticmethod
    def tmp_path() -> Path:
        return Path(tempfile.mkdtemp())


class TruncateAndRegistryTests(unittest.TestCase):
    def test_truncate_head_and_tail(self):
        t = "\n".join(str(i) for i in range(10))
        self.assertEqual(truncate(t, 1000, 3, "head")[0].strip().splitlines(), ["0", "1", "2"])
        self.assertEqual(truncate(t, 1000, 3, "tail")[0].strip().splitlines(), ["7", "8", "9"])
        self.assertFalse(truncate("short", 1000, 10)[1])

    def test_registry_override_and_active_set(self):
        reg = ToolRegistry()
        reg.register(ReadTool())
        class MyRead(ReadTool):
            description = "custom"
        reg.register(MyRead(), owner="plugin")
        self.assertEqual(reg.get("read").description, "custom")
        reg.register(ShellTool()); reg.set_active(["shell", "unknown"])
        self.assertEqual([t.name for t in reg.active()], ["shell"])
        reg.set_active(None)
        self.assertEqual(len(reg.specs()), 2)


if __name__ == "__main__":
    unittest.main()


class ConfinementTests(unittest.TestCase):
    """`confine_to_project` is off by default because a coding agent legitimately edits
    sibling repos and files outside its start directory. On, it refuses them."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "inside.txt").write_text("in\n")
        self.outside = Path(tempfile.mkdtemp()) / "outside.txt"
        self.outside.write_text("out\n")

    def test_absolute_outside_path_is_allowed_by_default(self):
        r = run(ReadTool().execute({"path": str(self.outside)}, tool_ctx(self.tmp)))
        self.assertFalse(r.is_error, r.content)

    def test_confinement_refuses_an_outside_absolute_path(self):
        r = run(ReadTool().execute({"path": str(self.outside)},
                                   tool_ctx(self.tmp, confine_to_project=True)))
        self.assertTrue(r.is_error)
        self.assertIn("outside the project", r.content)

    def test_confinement_refuses_dot_dot_traversal(self):
        r = run(ReadTool().execute({"path": "../escape.txt"},
                                   tool_ctx(self.tmp, confine_to_project=True)))
        self.assertTrue(r.is_error)
        self.assertIn("outside the project", r.content)

    def test_confinement_still_allows_paths_inside(self):
        r = run(ReadTool().execute({"path": "inside.txt"},
                                   tool_ctx(self.tmp, confine_to_project=True)))
        self.assertFalse(r.is_error, r.content)

    def test_write_is_refused_as_a_result_not_an_exception(self):
        r = run(WriteTool().execute({"path": str(self.outside), "content": "x"},
                                    tool_ctx(self.tmp, confine_to_project=True)))
        self.assertTrue(r.is_error)
        self.assertEqual(self.outside.read_text(), "out\n", "the file must not have been written")
