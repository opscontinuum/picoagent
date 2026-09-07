"""``resolve_tool_path``: the one place a model-supplied path becomes a real file.

The three bypasses in ``test_gate_path_bypasses.py`` all came from a guard resolving a path its
own way. This file covers the shared answer they now call instead: what it returns for a path a
tool will refuse, where a relative path resolves against, and which file a not-yet-existing
path under a symlink names.
"""
import os
import unittest
from pathlib import Path

from helpers import run, tool_ctx, temp_dir
from picoagent.core.tools import PathRefused, ReadTool, resolve_path, resolve_tool_path


class SeamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir()
        self.project = self.tmp / "project"
        self.project.mkdir()
        self.cfg = {"_cwd": str(self.project)}

    def test_a_refused_path_still_names_the_file_it_would_have_opened(self):
        """A gate handed ``None`` for a refused path reads it as "there is no file here", which
        is the one answer that must never come back: it is indistinguishable from a harmless
        argument, and the tool may still be about to open something."""
        outside = self.tmp / "outside.txt"
        outside.write_text("out\n")
        answer = resolve_tool_path(str(outside), {**self.cfg, "confine_to_project": True})
        self.assertEqual(answer.path, outside.resolve())
        self.assertIn("outside the project", answer.refusal or "")

    def test_an_accepted_path_has_no_refusal(self):
        answer = resolve_tool_path("inside.txt", {**self.cfg, "confine_to_project": True})
        self.assertIsNone(answer.refusal)
        self.assertEqual(answer.path, self.project / "inside.txt")

    def test_an_at_prefix_is_stripped_the_way_the_tool_strips_it(self):
        self.assertEqual(resolve_tool_path("@notes.md", self.cfg).path, self.project / "notes.md")

    def test_a_tilde_expands_the_way_the_tool_expands_it(self):
        self.assertEqual(resolve_tool_path("~/x.txt", self.cfg).path,
                         Path(os.path.expanduser("~/x.txt")).resolve())

    def test_a_relative_path_resolves_against_the_session_not_the_process(self):
        self.assertEqual(resolve_tool_path("sub/f.txt", self.cfg).path,
                         self.project / "sub" / "f.txt")

    def test_a_new_file_under_a_symlinked_directory_names_the_real_directory(self):
        real = self.tmp / "real"
        real.mkdir()
        os.symlink(real, self.project / "link")
        self.assertEqual(resolve_tool_path("link/new.txt", self.cfg).path, real / "new.txt")

    def test_parents_that_do_not_exist_yet_still_resolve(self):
        """``write`` creates parents, so several levels of a path can be missing at once."""
        real = self.tmp / "real"
        real.mkdir()
        os.symlink(real, self.project / "link")
        self.assertEqual(resolve_tool_path("link/a/b/c.txt", self.cfg).path, real / "a/b/c.txt")

    def test_a_symlink_loop_is_a_refusal_and_not_a_raise(self):
        os.symlink(self.project / "b", self.project / "a")
        os.symlink(self.project / "a", self.project / "b")
        answer = resolve_tool_path("a/x.txt", self.cfg)
        self.assertIsNotNone(answer.refusal)
        self.assertTrue(answer.path.is_absolute())

    def test_a_symlink_loop_is_an_error_result_and_not_a_traceback(self):
        os.symlink(self.project / "b", self.project / "a")
        os.symlink(self.project / "a", self.project / "b")
        result = run(ReadTool().execute({"path": "a/x.txt"}, tool_ctx(self.project)))
        self.assertTrue(result.is_error)

    def test_a_config_without_a_session_directory_still_answers(self):
        """A gate must get an answer from any config, including one assembled by an embedder
        that never called ``load_config``. Relative paths then mean what the process means."""
        answer = resolve_tool_path("f.txt", {})
        self.assertEqual(answer.path, Path.cwd().resolve() / "f.txt")

    def test_the_tool_opens_exactly_the_file_a_gate_was_told_about(self):
        """The property the whole seam exists for. If these two ever disagree, a guard is
        inspecting one file while the tool opens another, which is what the bypasses were."""
        real = self.tmp / "real"
        real.mkdir()
        os.symlink(real, self.project / "link")
        ctx = tool_ctx(self.project)
        for raw in ("@notes.md", "link/new.txt", "sub/../notes.md", "~/x.txt",
                    str(self.tmp / "outside.txt")):
            with self.subTest(raw=raw):
                self.assertEqual(resolve_path(ctx, raw),
                                 resolve_tool_path(raw, ctx.config, ctx.cwd).path)

    def test_the_tool_raises_where_the_seam_reports_a_refusal(self):
        """Tools keep the exception - three of them catch ``PathRefused`` - and gates keep the
        value. Same decision, taken once."""
        outside = self.tmp / "outside.txt"
        ctx = tool_ctx(self.project, confine_to_project=True)
        with self.assertRaises(PathRefused):
            resolve_path(ctx, str(outside))
        self.assertIsNotNone(resolve_tool_path(str(outside), ctx.config, ctx.cwd).refusal)


if __name__ == "__main__":
    unittest.main()
