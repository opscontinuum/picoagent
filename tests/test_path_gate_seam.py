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
from picoagent.core.tools import (PathRefused, ReadTool, resolve_path, resolve_path_inside_project,
                                  resolve_tool_path, resolve_tool_path_inside_project)


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


class InsideProjectTests(unittest.TestCase):
    """The stricter rule on top of the seam: a *relative* path must land in the project.

    Two shipped plugins each wrote this for themselves and the two copies disagreed, which is
    how the gate/tool drift bug in this repository started. These pin the shared answer,
    including the two things one of the copies got wrong.
    """

    def setUp(self):
        self.tmp = temp_dir()
        self.project = self.tmp / "project"
        self.project.mkdir()
        self.cfg = {"_cwd": str(self.project)}

    def refusal(self, raw, **cfg):
        return resolve_tool_path_inside_project(raw, {**self.cfg, **cfg}).refusal

    def test_a_relative_path_inside_the_project_is_allowed(self):
        answer = resolve_tool_path_inside_project("docs/out", self.cfg)
        self.assertIsNone(answer.refusal)
        self.assertEqual(answer.path, self.project / "docs" / "out")

    def test_the_project_directory_itself_is_inside_it(self):
        self.assertIsNone(self.refusal("."))

    def test_a_relative_escape_is_refused(self):
        self.assertIn("outside the project directory", self.refusal("../escape") or "")

    def test_an_at_prefixed_relative_escape_is_refused_the_same_way(self):
        """``Path("@../..").is_absolute()`` is False and so is ``Path("../..")``'s - but the
        first also looks like a plain name to anything reading the string before the ``@`` is
        stripped, which is how one plugin let it through."""
        self.assertIn("outside the project directory", self.refusal("@../escape") or "")

    def test_a_relative_path_leaving_through_a_symlink_is_refused(self):
        """Judged on the resolved path, not on the text: a link inside the project is textually
        a child of it and points wherever it points."""
        outside = self.tmp / "outside"
        outside.mkdir()
        os.symlink(outside, self.project / "link")
        self.assertIn("outside the project directory", self.refusal("link/main.tf") or "")

    def test_an_absolute_path_outside_the_project_is_the_users_own_choice(self):
        """The rule is about the model's constructions. An absolute path is somebody naming a
        place on purpose - the sibling repository - and refusing it makes the tool useless."""
        self.assertIsNone(self.refusal(str(self.tmp / "sibling" / "main.tf")))

    def test_a_tilde_path_counts_as_absolute(self):
        self.assertIsNone(self.refusal("~/notes.md"))

    def test_confinement_still_refuses_that_absolute_path_when_it_is_on(self):
        """The security boundary underneath is unchanged; this rule only adds to it."""
        self.assertIn("outside the project",
                      self.refusal(str(self.tmp / "sibling"), confine_to_project=True) or "")

    def test_a_path_the_os_cannot_resolve_is_a_refusal_and_not_a_raise(self):
        """A NUL byte reached ``path.exists()`` as an unhandled ValueError once. It is a path
        nothing can open, which is what a refusal says."""
        answer = resolve_tool_path_inside_project("out\x00", self.cfg)
        self.assertIsNotNone(answer.refusal)
        self.assertTrue(answer.path.is_absolute())

    def test_the_raising_twin_raises_exactly_where_the_value_form_refuses(self):
        ctx = tool_ctx(self.project)
        for raw in ("../escape", "@../escape", "out\x00"):
            with self.subTest(raw=raw):
                self.assertIsNotNone(resolve_tool_path_inside_project(raw, ctx.config, ctx.cwd).refusal)
                with self.assertRaises(PathRefused):
                    resolve_path_inside_project(ctx, raw)

    def test_the_raising_twin_returns_the_same_path_where_it_does_not(self):
        ctx = tool_ctx(self.project)
        for raw in ("docs/out", "@notes.md", "~/x.txt", str(self.tmp / "sibling")):
            with self.subTest(raw=raw):
                self.assertEqual(resolve_path_inside_project(ctx, raw),
                                 resolve_tool_path_inside_project(raw, ctx.config, ctx.cwd).path)

    def test_an_allowed_path_is_the_one_the_plain_seam_names(self):
        """This adds a refusal and never a different file. A guard resolving the argument with
        ``resolve_tool_path`` and a tool taking the stricter rule must still agree on which
        file is in question, or the drift is back."""
        ctx = tool_ctx(self.project)
        for raw in ("docs/out", "@notes.md", "sub/../notes.md", "~/x.txt", str(self.tmp / "sibling")):
            with self.subTest(raw=raw):
                self.assertEqual(resolve_tool_path_inside_project(raw, ctx.config, ctx.cwd).path,
                                 resolve_tool_path(raw, ctx.config, ctx.cwd).path)


if __name__ == "__main__":
    unittest.main()
