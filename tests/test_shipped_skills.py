"""Every skill that ships is loadable, invocable, and actually reaches the model.

Nothing exercised these before. The suite covered SKILL.md parsing in the abstract and it
covered the plugin tools, but no test invoked a skill that ships. A skill whose frontmatter
does not parse, or whose body never reaches the model, is invisible until a user types
``/skill:<name>`` and gets nothing back.

These are deliberately model-free. Skill expansion is deterministic, so driving it through
``ScriptedProvider`` proves the wiring without a live model and without the flakiness one
brings: a small model failing to follow a skill tells you about the model, not about the skill.
Whether a model *obeys* a skill is a separate question and needs a live run to answer.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from helpers import ROOT, ScriptedProvider, make_runtime, run, text
from picoagent.core.loop import AgentLoop
from picoagent.core.skills import load_skill

SKILL_FILES = sorted(ROOT.glob("examples/plugins/*/skills/*/SKILL.md"))


class ShippedSkillInventory(unittest.TestCase):
    """The repository ships skills; this is the guard that it keeps shipping working ones."""

    def test_there_are_skills_to_check(self):
        """A glob that silently matches nothing would make every test below vacuously pass."""
        self.assertGreater(len(SKILL_FILES), 0, "no shipped SKILL.md files found; the glob is wrong")

    def test_every_shipped_skill_loads(self):
        for skill_md in SKILL_FILES:
            with self.subTest(skill=skill_md.parent.name):
                skill = load_skill(skill_md, "project")
                self.assertIsNotNone(skill, "SKILL.md did not load")
                self.assertTrue(skill.name.strip(), "skill has no name")
                self.assertTrue(skill.description.strip(), "skill has no description")
                self.assertTrue(skill.body.strip(), "skill has an empty body")

    def test_every_shipped_skill_is_advertised_to_the_model(self):
        """Descriptions go into the system prompt every turn; a skill nobody can see is dead."""
        for skill_md in SKILL_FILES:
            with self.subTest(skill=skill_md.parent.name):
                skill = load_skill(skill_md, "project")
                runtime = make_runtime(self.tmp, provider=ScriptedProvider([[text("ok")]]))
                runtime.skills.add_dir(skill_md.parents[1], "project")
                self.assertIn(skill.name, runtime.skills.prompt_section(),
                              "the skill is not advertised in the system prompt section")

    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)


class ShippedSkillInvocation(unittest.TestCase):
    """`/skill:<name>` reaches the model with the skill's body attached."""

    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _invoke(self, skill_md: Path, argument: str = "") -> str:
        """Invoke one shipped skill and return the user text the provider actually received."""
        provider = ScriptedProvider([[text("ok")]])
        runtime = make_runtime(self.tmp, provider=provider)
        runtime.provider_name = "scripted"
        runtime.skills.add_dir(skill_md.parents[1], "project")
        skill = load_skill(skill_md, "project")
        run(AgentLoop(runtime).handle_input(f"/skill:{skill.name} {argument}".strip()))
        self.assertTrue(provider.calls, f"{skill.name}: the model was never called")
        sent = provider.calls[0]["messages"]
        return "\n".join(m.text or "" for m in sent if m.role == "user")

    def test_every_shipped_skill_delivers_its_body_to_the_model(self):
        for skill_md in SKILL_FILES:
            with self.subTest(skill=skill_md.parent.name):
                skill = load_skill(skill_md, "project")
                delivered = self._invoke(skill_md)
                self.assertIn(f'name="{skill.name}"', delivered,
                              "the skill envelope did not reach the model")
                # A distinctive slice of the real body, not the whole thing: bodies are long and
                # an exact match would fail on any harmless reformatting.
                probe = next(line.strip() for line in skill.body.splitlines() if len(line.strip()) > 40)
                self.assertIn(probe, delivered, "the skill body did not reach the model")

    def test_an_unknown_skill_reports_itself_rather_than_failing_silently(self):
        provider = ScriptedProvider([[text("ok")]])
        runtime = make_runtime(self.tmp, provider=provider)
        runtime.provider_name = "scripted"
        runtime.skills.add_dir(SKILL_FILES[0].parents[1], "project")
        run(AgentLoop(runtime).handle_input("/skill:does-not-exist"))
        delivered = "\n".join(m.text or "" for m in provider.calls[0]["messages"] if m.role == "user")
        self.assertIn("unknown skill", delivered.lower())

    def test_arguments_are_substituted_where_a_skill_uses_them(self):
        """Only one shipped skill takes $ARGUMENTS today, so this guards the mechanism itself."""
        using = [p for p in SKILL_FILES if "$ARGUMENTS" in p.read_text()]
        self.assertTrue(using, "no shipped skill uses $ARGUMENTS; this test needs rewriting")
        for skill_md in using:
            with self.subTest(skill=skill_md.parent.name):
                delivered = self._invoke(skill_md, "PICO_ARG_TOKEN")
                self.assertIn("PICO_ARG_TOKEN", delivered, "$ARGUMENTS was not substituted")
                self.assertNotIn("$ARGUMENTS", delivered, "the placeholder survived substitution")


class SkillToolReferences(unittest.TestCase):
    """A skill must not tell the model to call a tool that nothing registers.

    That failure is invisible today: the skill parses, loads, reaches the model, and then the
    model issues a call that comes back "Unknown or inactive tool". Nothing fails until a user
    is watching it happen. This passes at the time of writing; its value is the day somebody
    renames a tool and forgets the skill that names it.
    """

    CORE_TOOLS = {"read", "write", "edit", "shell"}
    #: A tool name in prose looks like `es_logs`: backticked, snake_case, no spaces.
    CANDIDATE = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`")

    def _registered_tools(self, plugin_dir: Path) -> set:
        """Tool names the plugin's own modules declare, plus the core four it can also call."""
        names = set(self.CORE_TOOLS)
        for source in plugin_dir.glob("*.py"):
            names |= set(re.findall(r'name\s*=\s*"([a-z_]+)"', source.read_text()))
        return names

    def test_no_skill_references_a_tool_that_does_not_exist(self):
        checked = 0
        for plugin_dir in sorted(ROOT.glob("examples/plugins/*/")):
            skills = sorted(plugin_dir.glob("skills/*/SKILL.md"))
            if not skills:
                continue
            registered = self._registered_tools(plugin_dir)
            # Only a tool whose own name is namespaced claims a prefix. A single-word tool such
            # as `agent` must not claim all of `agent_*`, or an event name like `agent_settled`
            # written in a skill reads as a tool nothing registers.
            namespaces = tuple(f"{n.split('_')[0]}_" for n in registered - self.CORE_TOOLS
                               if "_" in n)
            if not namespaces:
                continue
            for skill_md in skills:
                with self.subTest(skill=skill_md.parent.name):
                    referenced = set(self.CANDIDATE.findall(skill_md.read_text()))
                    # Only names in this plugin's namespace. Prose mentions plenty of other
                    # snake_case identifiers (field names, settings) that are not tool calls.
                    plausible = {r for r in referenced if r.startswith(namespaces)}
                    unknown = sorted(plausible - registered)
                    self.assertEqual(unknown, [], f"references tools nothing registers: {unknown}")
                    checked += 1
        self.assertGreater(checked, 0, "no skills were checked; the namespace filter is too narrow")


if __name__ == "__main__":
    unittest.main()
