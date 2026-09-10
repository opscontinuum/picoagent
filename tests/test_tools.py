"""Built-in tools: read/write/edit/glob/grep/shell, truncation, and the per-file mutation lock."""
import asyncio, unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from helpers import run, tool_ctx, temp_dir
import signal
from picoagent.core.tools import (ShellTool, EditTool, GlobTool, GrepTool, ReadTool, ToolRegistry,
                                  WriteTool, truncate, spawn_shell, kill_process_tree, tool_result,
                                  is_windows, own_process_group, _signal_group, _glob_matcher,
                                  GREP_MATCH_LIMIT, GREP_MAX_FILE_BYTES, GREP_MAX_LINE_CHARS)


class ReadToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir(); (self.tmp / "f.txt").write_text("a\nb\nc\nd\n")

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
        self.tmp = temp_dir()

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

    def test_edit_of_a_missing_file_is_an_error(self):
        """Flipping this result's ``is_error`` survived a mutation run: nothing pinned that an
        edit of a file that is not there *fails*, and a model reading success retries nothing."""
        r = run(EditTool().execute({"path": "nope.py", "old_text": "a", "new_text": "b"}, tool_ctx(self.tmp)))
        self.assertTrue(r.is_error)
        self.assertIn("not found", r.content)

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
        self.tmp = temp_dir()

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

    def test_output_that_fits_is_handed_over_whole_with_no_note_and_no_spill_file(self):
        """The other side of the truncation branch, which nothing was asserting.

        Announcing a cut that did not happen is not cosmetic: the model is told the output it
        can see is a fragment, so it goes looking for the rest, and the footer hands it a temp
        file path to go looking in. Every short command would also leave a spill file behind.
        """
        result = run(ShellTool().execute({"command": "echo hi"}, tool_ctx(self.tmp)))
        self.assertEqual(result.content, "hi\n\n[exit code 0]")


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
        return temp_dir()


class SignalGroupContractTests(unittest.TestCase):
    """``_signal_group``'s answer is what separates escalation from a pointless second signal.

    ``kill_process_tree`` only waits out the SIGTERM grace when the send reported a live group;
    a mutation making it report ``False`` for a live group survived the suite, and under it
    every graceful kill went straight to SIGKILL - the grace contract, silently gone. The
    timing of the grace window itself is not asserted (that test would be a race); the return
    value that gates it is deterministic and is what this pins.
    """

    @unittest.skipIf(is_windows(), "process groups and killpg are POSIX")
    def test_a_live_group_reports_true_and_a_finished_one_false(self):
        async def probe():
            proc = await asyncio.create_subprocess_exec(
                "sleep", "30", stdout=asyncio.subprocess.DEVNULL, **own_process_group())
            alive = _signal_group(proc, signal.SIGKILL)
            await proc.wait()
            return alive, _signal_group(proc, signal.SIGKILL)
        alive, gone = run(probe())
        self.assertTrue(alive, "a signal delivered to a live group must report it was")
        self.assertFalse(gone, "a group that is gone must not read as one worth escalating on")


class TruncateAndRegistryTests(unittest.TestCase):
    def test_truncate_head_and_tail(self):
        t = "\n".join(str(i) for i in range(10))
        self.assertEqual(truncate(t, 1000, 3, "head")[0].strip().splitlines(), ["0", "1", "2"])
        self.assertEqual(truncate(t, 1000, 3, "tail")[0].strip().splitlines(), ["7", "8", "9"])
        self.assertFalse(truncate("short", 1000, 10)[1])

    def test_the_byte_cut_keeps_the_same_end_the_line_cut_would(self):
        """``truncate`` cuts twice - by lines, then by bytes - and only the first was pinned.

        One long line reaches the second cut without the first having anything to do, so this is
        the case that tells the two ends apart there. Getting it backwards is quiet in exactly the
        way that matters: a command's output would be cut to its opening banner rather than to the
        error it ended on, and a file read to its last page rather than its first, with the
        ``[truncated]`` note reading the same either way.
        """
        one_line = "HEADHEAD" + "." * 50 + "TAILTAIL"
        self.assertEqual(truncate(one_line, 8, 10, "head"), ("HEADHEAD", True))
        self.assertEqual(truncate(one_line, 8, 10, "tail"), ("TAILTAIL", True))

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




class ToolResultTests(unittest.TestCase):
    """``tool_result``: the last line of a tool that returns text somebody else sized.

    Four shipped plugins each had this function; the boundary is what a copy gets wrong, so it
    is pinned here - at the limit is not truncated, one line or one byte past it is.
    """

    def setUp(self):
        self.ctx = tool_ctx(temp_dir(), tool_output_max_bytes=100, tool_output_max_lines=3)

    def test_short_text_passes_through_with_the_call_id(self):
        answer = tool_result(self.ctx, "all good")
        self.assertEqual(answer.content, "all good")
        self.assertEqual(answer.tool_call_id, "t1")
        self.assertFalse(answer.is_error)
        self.assertEqual(answer.details, {})

    def test_text_exactly_at_the_line_limit_is_not_marked_truncated(self):
        self.assertNotIn("[truncated]", tool_result(self.ctx, "a\nb\nc").content)

    def test_one_line_past_the_limit_is_cut_and_says_so(self):
        answer = tool_result(self.ctx, "a\nb\nc\nd")
        self.assertEqual(answer.content, "a\nb\nc\n\n[truncated]")

    def test_text_exactly_at_the_byte_limit_is_not_marked_truncated(self):
        self.assertNotIn("[truncated]", tool_result(self.ctx, "x" * 100).content)

    def test_one_byte_past_the_limit_is_cut_and_says_so(self):
        self.assertIn("[truncated]", tool_result(self.ctx, "x" * 101).content)

    def test_it_keeps_the_head_which_is_what_a_document_needs(self):
        self.assertTrue(tool_result(self.ctx, "first\nsecond\nthird\nfourth").content.startswith("first"))

    def test_keyword_arguments_become_details_the_model_never_sees(self):
        answer = tool_result(self.ctx, "wrote it", path="/tmp/x", lines=3)
        self.assertEqual(answer.details, {"path": "/tmp/x", "lines": 3})
        self.assertNotIn("/tmp/x", answer.content)

    def test_an_expected_failure_is_a_flagged_result_not_a_raise(self):
        self.assertTrue(tool_result(self.ctx, "no such index", is_error=True).is_error)


class ConfinementTests(unittest.TestCase):
    """`confine_to_project` is off by default because a coding agent legitimately edits
    sibling repos and files outside its start directory. On, it refuses them."""

    def setUp(self):
        self.tmp = temp_dir()
        (self.tmp / "inside.txt").write_text("in\n")
        self.outside = temp_dir() / "outside.txt"
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


class SearchTreeCase(unittest.TestCase):
    """A small repository-shaped tree, shared by the ``glob`` and ``grep`` tests.

    It has the three things both tools have to get right: source at more than one depth, a
    non-source file, and two directories from :data:`IGNORED_DIRECTORIES` holding files that
    match everything the tests search for. A tool that walks the noise passes nothing here.
    """

    def setUp(self):
        self.tmp = temp_dir()
        self.write("a.py", "import os\nNEEDLE = 1\n")
        self.write("pkg/b.py", "def needle():\n    return 2\n")
        self.write("pkg/notes.txt", "needle in the text file\n")
        self.write("pkg/deep/c.py", "# nothing here\n")
        self.write(".git/objects/hidden.py", "NEEDLE\n")
        self.write("node_modules/dep/index.py", "NEEDLE\n")

    def write(self, relative: str, content: str) -> Path:
        path = self.tmp / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path


class GlobToolTests(SearchTreeCase):
    def glob(self, pattern: str, **args):
        return run(GlobTool().execute({"pattern": pattern, **args}, tool_ctx(self.tmp)))

    def test_double_star_matches_at_every_depth_including_the_root(self):
        result = self.glob("**/*.py")
        self.assertFalse(result.is_error, result.content)
        self.assertEqual(result.content.splitlines(), ["a.py", "pkg/b.py", "pkg/deep/c.py"])

    def test_a_single_star_does_not_cross_a_directory_separator(self):
        """``*.py`` is a top-level pattern, and a tool that quietly made it recursive would make
        the difference between the two patterns unexpressible."""
        self.assertEqual(self.glob("*.py").content.splitlines(), ["a.py"])

    def test_noise_directories_are_never_searched(self):
        content = self.glob("**/*.py").content
        self.assertNotIn(".git", content)
        self.assertNotIn("node_modules", content)

    def test_path_moves_the_search_root_but_results_stay_project_relative(self):
        """The two spellings are deliberately different, and this is the test that says so.

        ``*.py`` matches ``b.py`` because the pattern is applied relative to the search root -
        that is the spelling the model wrote the pattern against. The answer comes back as
        ``pkg/b.py`` because that is the spelling its next call has to use. Reporting the short
        form would read as a success and then cost a turn on the ``read`` that followed it.
        """
        result = self.glob("*.py", path="pkg")
        self.assertEqual(result.content.splitlines(), ["pkg/b.py"])
        self.assertEqual(result.details["path"], str(self.tmp / "pkg"))

    def test_every_path_glob_reports_is_one_read_can_open(self):
        """The round trip, asserted end to end rather than by inspecting the spelling.

        This is the property the relative-path choice exists for, so it is checked by actually
        making the follow-up call the model would make, for every path a narrowed search
        returned. A regression here is silent: the search still looks like it worked.
        """
        for reported in self.glob("**/*.py", path="pkg").content.splitlines():
            opened = run(ReadTool().execute({"path": reported}, tool_ctx(self.tmp)))
            self.assertFalse(opened.is_error, f"read could not open {reported!r}: {opened.content}")

    def test_a_pattern_matching_nothing_is_an_answer_not_an_error(self):
        """An ``is_error`` here invites a retry of the same call; the model needs to write a
        different pattern, and being told plainly that this one matched nothing is what does it."""
        result = self.glob("**/*.rs")
        self.assertFalse(result.is_error)
        self.assertIn("No files match", result.content)
        self.assertEqual(result.details["count"], 0)

    def test_a_path_that_is_not_a_directory_is_an_error(self):
        self.assertTrue(self.glob("*.py", path="a.py").is_error)

    def test_results_are_cut_to_the_output_limits(self):
        for index in range(200):
            self.write(f"many/f{index:03d}.py", "x\n")
        ctx = tool_ctx(self.tmp, tool_output_max_lines=20)
        result = run(GlobTool().execute({"pattern": "many/*.py"}, ctx))
        self.assertIn("[truncated]", result.content)
        self.assertEqual(len(result.content.splitlines()), 22)   # 20 matches, a blank line, the note

    def test_an_aborted_walk_stops_and_says_the_tree_was_not_finished(self):
        """An empty result from a cancelled search must not read as "nothing matches"."""
        ctx = tool_ctx(self.tmp)
        ctx.abort.set()
        result = run(GlobTool().execute({"pattern": "**/*.py"}, ctx))
        self.assertEqual(result.details["count"], 0)
        self.assertIn("search aborted", result.content)


class GrepToolTests(SearchTreeCase):
    def grep(self, pattern: str, **args):
        return run(GrepTool().execute({"pattern": pattern, **args}, tool_ctx(self.tmp)))

    def test_reports_path_line_and_text_with_the_files_own_line_numbers(self):
        result = self.grep("NEEDLE")
        self.assertFalse(result.is_error, result.content)
        self.assertEqual(result.content.splitlines(), ["a.py:2:NEEDLE = 1"])

    def test_a_later_line_reports_its_own_number(self):
        self.write("pkg/deep/c.py", "one\ntwo\nthree\nNEEDLE\n")
        self.assertIn("pkg/deep/c.py:4:NEEDLE", self.grep("NEEDLE").content)

    def test_case_insensitive_widens_the_match(self):
        found = self.grep("needle", case_insensitive=True).content
        self.assertIn("a.py:2:NEEDLE = 1", found)
        self.assertIn("pkg/b.py:1:def needle():", found)

    def test_case_matters_by_default(self):
        self.assertNotIn("pkg/b.py", self.grep("NEEDLE").content)

    def test_glob_restricts_the_files_searched(self):
        self.assertEqual(self.grep("needle", glob="**/*.txt").content.splitlines(),
                         ["pkg/notes.txt:1:needle in the text file"])

    def test_noise_directories_are_never_searched(self):
        content = self.grep("NEEDLE").content
        self.assertNotIn(".git", content)
        self.assertNotIn("node_modules", content)

    def test_a_malformed_regex_is_an_error_result_not_a_raise(self):
        result = self.grep("def (unclosed")
        self.assertTrue(result.is_error)
        self.assertIn("Invalid regular expression", result.content)

    def test_an_unclosed_bracket_in_a_glob_is_a_literal_bracket_not_a_crash(self):
        """A shell reads ``[`` with no ``]`` as the character itself; a regex would refuse to
        compile it. Translating it to the literal keeps the tool from raising on a pattern the
        model has every reason to think is valid."""
        self.assertFalse(self.grep("NEEDLE", glob="[").is_error)

    def test_a_binary_file_is_skipped_rather_than_quoted_back(self):
        """A NUL-byte file decoded with ``errors='replace'`` matches on the surrounding text and
        puts a page of replacement characters in the transcript. It must not be searched at all."""
        (self.tmp / "blob.bin").write_bytes(b"NEEDLE\x00\xff\xfe binary NEEDLE\n")
        content = self.grep("NEEDLE").content
        self.assertNotIn("blob.bin", content)
        self.assertNotIn("�", content)

    def test_a_file_that_is_not_utf8_is_skipped(self):
        (self.tmp / "latin.py").write_bytes(b"NEEDLE = '\xe9'\n")
        self.assertNotIn("latin.py", self.grep("NEEDLE").content)

    def test_a_file_over_the_size_cap_is_skipped(self):
        self.write("huge.py", "NEEDLE\n" + "x" * GREP_MAX_FILE_BYTES)
        self.assertNotIn("huge.py", self.grep("NEEDLE").content)

    def test_an_unreadable_file_does_not_abort_the_rest_of_the_search(self):
        """One file's ``OSError`` is one file's problem. A dangling symlink is the version of it
        that behaves the same for root, who can open the permission-denied version."""
        try:
            (self.tmp / "dangling.py").symlink_to(self.tmp / "gone.py")
        except OSError:
            self.skipTest("this platform will not create a symlink here")
        self.assertIn("a.py:2:NEEDLE = 1", self.grep("NEEDLE").content)

    def test_a_pattern_matching_nothing_is_an_answer_not_an_error(self):
        result = self.grep("zzz-not-here")
        self.assertFalse(result.is_error)
        self.assertIn("No matches", result.content)
        self.assertEqual(result.details["matches"], 0)

    def test_an_aborted_search_says_the_tree_was_not_finished(self):
        ctx = tool_ctx(self.tmp)
        ctx.abort.set()
        result = run(GrepTool().execute({"pattern": "NEEDLE"}, ctx))
        self.assertIn("search aborted", result.content)

    def test_the_search_stops_at_the_match_cap_and_says_there_are_more(self):
        self.write("many.py", "NEEDLE\n" * (GREP_MATCH_LIMIT * 2))
        result = run(GrepTool().execute({"pattern": "NEEDLE"},
                                        tool_ctx(self.tmp, tool_output_max_lines=10_000)))
        self.assertEqual(result.details["matches"], GREP_MATCH_LIMIT)
        self.assertIn(f"stopped at {GREP_MATCH_LIMIT} matches", result.content)

    def test_a_very_long_matching_line_is_shortened(self):
        self.write("minified.py", "NEEDLE" + "x" * 5000 + "\n")
        line = [l for l in self.grep("NEEDLE").content.splitlines() if l.startswith("minified.py")][0]
        self.assertTrue(line.endswith(" ..."))
        self.assertLess(len(line), GREP_MAX_LINE_CHARS + 50)

    def test_results_are_cut_to_the_output_limits_and_the_note_survives_the_cut(self):
        self.write("many.py", "NEEDLE\n" * 100)
        result = run(GrepTool().execute({"pattern": "NEEDLE"}, tool_ctx(self.tmp, tool_output_max_lines=10)))
        self.assertIn("truncated", result.content)
        self.assertTrue(result.content.rstrip().endswith("narrow the pattern, or set path/glob]"))


    def test_every_path_grep_reports_is_one_read_can_open(self):
        """Same round trip as ``glob``'s, because ``grep`` narrowed by ``path`` has the same trap.

        ``path:line:text`` is only useful if the ``path`` half survives being handed back. The
        line number is checked too - it counts from the file's first line, not the window's, so
        a number read here is one that can be quoted in an ``edit``.
        """
        hits = self.grep("needle", path="pkg", case_insensitive=True).content.splitlines()
        self.assertTrue(hits, "expected at least one hit to round-trip")
        for hit in hits:
            reported, number, _ = hit.split(":", 2)
            opened = run(ReadTool().execute({"path": reported}, tool_ctx(self.tmp)))
            self.assertFalse(opened.is_error, f"read could not open {reported!r}: {opened.content}")
            self.assertIn(f"{int(number):6d}\t", opened.content)


class SearchConfinementTests(SearchTreeCase):
    """Both search tools resolve their root through the same gate the other tools use.

    Two rules meet here, and the tests keep them apart. ``confine_to_project`` is the security
    boundary and refuses an outside path however it is spelled; the relative-escape rule on top
    of it is a usability one, and refuses ``path="../.."`` while still allowing an absolute path
    somebody named on purpose.
    """

    def setUp(self):
        super().setUp()
        self.outside = temp_dir()
        (self.outside / "secret.py").write_text("NEEDLE\n")

    def test_glob_refuses_an_outside_absolute_path_under_confinement(self):
        result = run(GlobTool().execute({"pattern": "*.py", "path": str(self.outside)},
                                        tool_ctx(self.tmp, confine_to_project=True)))
        self.assertTrue(result.is_error)
        self.assertIn("outside the project", result.content)

    def test_grep_refuses_an_outside_absolute_path_under_confinement(self):
        result = run(GrepTool().execute({"pattern": "NEEDLE", "path": str(self.outside)},
                                        tool_ctx(self.tmp, confine_to_project=True)))
        self.assertTrue(result.is_error)
        self.assertIn("outside the project", result.content)
        self.assertNotIn("secret.py", result.content)

    def test_glob_refuses_a_relative_path_that_climbs_out_of_the_project(self):
        result = run(GlobTool().execute({"pattern": "*.py", "path": "../.."}, tool_ctx(self.tmp)))
        self.assertTrue(result.is_error)
        self.assertIn("outside the project", result.content)

    def test_grep_refuses_a_relative_path_that_climbs_out_of_the_project(self):
        result = run(GrepTool().execute({"pattern": "NEEDLE", "path": "../.."}, tool_ctx(self.tmp)))
        self.assertTrue(result.is_error)
        self.assertIn("outside the project", result.content)

    def test_an_absolute_outside_path_is_allowed_when_confinement_is_off(self):
        """The usability rule stops a mistake, not an attacker: naming the sibling directory on
        purpose still works, exactly as it does for ``read``."""
        result = run(GrepTool().execute({"pattern": "NEEDLE", "path": str(self.outside)}, tool_ctx(self.tmp)))
        self.assertFalse(result.is_error, result.content)
        self.assertIn("secret.py:1:NEEDLE", result.content)


class GlobPatternTranslationTests(unittest.TestCase):
    """``_glob_matcher`` is the one piece both tools share, and the shell's rules are fiddly.

    Pinned directly rather than only through the tools because a wrong answer here is quiet:
    every case below returns *a* plausible list of files, just not the one that was asked for.
    """

    def matches(self, pattern: str, path: str) -> bool:
        return _glob_matcher(pattern).fullmatch(path) is not None

    def test_double_star_slash_matches_zero_directories(self):
        self.assertTrue(self.matches("**/*.py", "a.py"))

    def test_double_star_slash_matches_many_directories(self):
        self.assertTrue(self.matches("**/*.py", "a/b/c.py"))

    def test_star_stops_at_a_separator(self):
        self.assertFalse(self.matches("*.py", "a/b.py"))

    def test_question_mark_matches_one_character_that_is_not_a_separator(self):
        self.assertTrue(self.matches("a?.py", "ab.py"))
        self.assertFalse(self.matches("a?b.py", "a/b.py"))

    def test_a_character_class_is_passed_to_the_regex(self):
        self.assertTrue(self.matches("test_[ab].py", "test_a.py"))
        self.assertFalse(self.matches("test_[ab].py", "test_c.py"))

    def test_a_negated_character_class_uses_the_shells_spelling(self):
        self.assertFalse(self.matches("test_[!ab].py", "test_a.py"))
        self.assertTrue(self.matches("test_[!ab].py", "test_c.py"))

    def test_a_dot_is_a_literal_dot_and_not_the_regexs_any_character(self):
        self.assertFalse(self.matches("a.py", "axpy"))

    def test_a_prefix_match_is_not_a_match(self):
        self.assertFalse(self.matches("*.py", "a.pyc"))


if __name__ == "__main__":
    unittest.main()
