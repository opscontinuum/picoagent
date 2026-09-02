"""Skills, session log, config layering, and slash-command parsing."""
import os, tempfile, textwrap, unittest
from pathlib import Path
from helpers import ROOT  # noqa: F401
from picoagent.core.commands import CommandRegistry
from picoagent.core.config import load_config
from picoagent.core.session import Session
from picoagent.core.skills import SkillRegistry
from picoagent.core.types import Message, ToolResult


def write_skill(root: Path, name: str, desc: str, body: str, extra: str = "") -> None:
    d = root / name; d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n{extra}---\n{body}\n")


class SkillTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()); self.reg = SkillRegistry()
        write_skill(self.tmp, "deploy", "Ship it", "Run deploy for $ARGUMENTS")
        write_skill(self.tmp, "secret", "Hidden", "x", "disable-model-invocation: true\n")
        self.reg.add_dir(self.tmp, "project")

    def test_discovers_skills_and_parses_frontmatter(self):
        s = self.reg.get("deploy")
        self.assertEqual(s.description, "Ship it"); self.assertEqual(s.body, "Run deploy for $ARGUMENTS")

    def test_prompt_section_hides_user_only_skills(self):
        sec = self.reg.prompt_section()
        self.assertIn("deploy: Ship it", sec); self.assertNotIn("secret", sec)

    def test_expand_substitutes_arguments(self):
        out = self.reg.expand("/skill:deploy prod")
        self.assertIn("Run deploy for prod", out); self.assertIn('name="deploy"', out)

    def test_expand_ignores_non_skill_input(self):
        self.assertIsNone(self.reg.expand("hello"))

    def test_expand_unknown_skill_lists_available(self):
        self.assertIn("deploy", self.reg.expand("/skill:nope"))

    def test_later_source_overrides_earlier(self):
        other = Path(tempfile.mkdtemp()); write_skill(other, "deploy", "Better", "v2")
        self.reg.add_dir(other, "user")
        self.assertEqual(self.reg.get("deploy").description, "Better")


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()); self.path = self.tmp / "s.jsonl"

    def test_append_and_resume_round_trip(self):
        s = Session(self.path, self.tmp)
        s.append_message(Message(role="user", text="hi"))
        s.append_message(Message(role="assistant", text="yo"))
        s2 = Session(self.path, self.tmp, resume=True)
        self.assertEqual([m.text for m in s2.messages()], ["hi", "yo"])

    def test_entries_form_a_chain(self):
        s = Session(self.path, self.tmp)
        a = s.append_message(Message(role="user", text="a")); b = s.append_message(Message(role="user", text="b"))
        self.assertEqual(b["parent"], a["id"]); self.assertEqual(s.leaf, b["id"])

    def test_set_leaf_branches_history(self):
        s = Session(self.path, self.tmp)
        a = s.append_message(Message(role="user", text="a")); s.append_message(Message(role="user", text="b"))
        s.set_leaf(a["id"]); s.append_message(Message(role="user", text="c"))
        self.assertEqual([m.text for m in s.messages()], ["a", "c"])

    def test_compaction_replaces_older_messages_with_summary(self):
        s = Session(self.path, self.tmp)
        for t in "abcd":
            s.append_message(Message(role="user", text=t))
        keep_from = [e for e in s.branch() if e["kind"] == "message"][-2]["id"]
        s.append_compaction("SUMMARY", keep_from)
        texts = [m.text for m in s.messages()]
        self.assertEqual(texts[0], "[Conversation summary]\nSUMMARY"); self.assertEqual(texts[1:], ["c", "d"])

    def test_custom_entries_are_not_messages(self):
        s = Session(self.path, self.tmp)
        s.append_custom("todo", {"items": [1]})
        self.assertEqual(s.messages(), []); self.assertEqual(next(s.custom("todo"))["data"], {"items": [1]})

    def test_tool_results_survive_serialisation(self):
        s = Session(self.path, self.tmp)
        s.append_message(Message(role="tool", tool_results=[ToolResult("t1", "out", details={"k": 1})]))
        m = Session(self.path, self.tmp, resume=True).messages()[0]
        self.assertEqual(m.tool_results[0].details, {"k": 1})


class ConfigTests(unittest.TestCase):
    def test_project_overrides_user_and_plugin_lists_concatenate(self):
        home, proj = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
        os.environ["PICOAGENT_HOME"] = str(home)
        import importlib, picoagent.core.config as c; importlib.reload(c)
        (home / "config.toml").write_text('model="user-model"\nbash_timeout=5\n[plugins]\nenabled=["git:a/b"]\n')
        (proj / ".picoagent").mkdir(); (proj / ".picoagent/config.toml").write_text('model="proj-model"\n[plugins]\nenabled=["./x"]\n')
        cfg = c.load_config(proj, {"thinking": "high", "model": None})
        self.assertEqual(cfg["model"], "proj-model"); self.assertEqual(cfg["bash_timeout"], 5)
        self.assertEqual(cfg["plugins"]["enabled"], ["git:a/b", "./x"]); self.assertEqual(cfg["thinking"], "high")


class CommandTests(unittest.TestCase):
    def test_parse_splits_name_and_args(self):
        reg = CommandRegistry()
        async def h(a, rt): return a
        reg.register("model", h, "set model")
        cmd, args = reg.parse("/model  gpt-x ")
        self.assertEqual((cmd.name, args), ("model", "gpt-x"))

    def test_parse_ignores_plain_text_and_skill_invocations(self):
        reg = CommandRegistry()
        self.assertIsNone(reg.parse("hello")); self.assertIsNone(reg.parse("/skill:x")); self.assertIsNone(reg.parse("/unknown"))


if __name__ == "__main__":
    unittest.main()
