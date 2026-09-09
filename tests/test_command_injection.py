"""Untrusted data must never reach a shell as text. Pins the property the whole tree relies on.

Command injection is data crossing into the instruction channel: a name, a URL or a path that an
attacker controls, concatenated into a string a shell then parses. The classic shape is
``os.system("git clone " + url)``, where a url of ``x; rm -rf ~`` is two commands.

picoagent is not vulnerable to that, and the reason is structural rather than careful: every
subprocess it starts is started from an argv list, so a repository-supplied plugin name, git URL
or ref is one element of that list and the shell never sees it. There is no escaping to get right
because nothing is being escaped.

Two places do hand a string to a shell, and both are the product rather than a defect:

* ``ShellTool`` - the model composing a command is what the tool is for. The model deciding to run
  ``pytest`` on the user's instruction is authorised delegation, not injection. What *would* be
  injection is content the model read from an untrusted source steering that decision, and that is
  prompt injection reaching the command channel - a different problem, documented as unsolved in
  ``docs/security/trust-boundaries.md`` and ranked as T11 in the threat model. No amount of
  argv discipline addresses it, and this file does not claim to.
* ``PlainFrontend._user_shell`` - the ``!cmd`` escape, which is the user typing their own command
  into their own shell.

So the invariant worth pinning is: those two, and nothing else. A third would almost certainly be
somebody building a command out of a name, which is the vulnerability this file exists to prevent
from reappearing. The STIG rule behind this (V-222604 / APSC-DV-002510) asks for testing evidence
that command injection has been checked for; a test that fails when the property breaks is better
evidence than a scan report, because it is re-run on every commit rather than once.
"""
import ast
import pathlib
import unittest

from helpers import ROOT          # also puts the repository root on sys.path
SOURCES = sorted(
    path for directory in ("picoagent", "examples/plugins")
    for path in (ROOT / directory).rglob("*.py")
    if "__pycache__" not in str(path)
)

#: The two places a string may reach a shell, and why each is allowed. Anything else is a finding.
SHELL_CALLS_ALLOWED = {
    ("picoagent/frontends/plain.py", "create_subprocess_shell"):
        "the `!cmd` escape: the user's own command, typed by the user",
    ("picoagent/core/tools.py", "create_subprocess_shell"):
        "ShellTool: the model's command, which is what the tool exists to run",
}

#: Fully qualified, because the leaf name alone is ambiguous: ``platform.system()`` reports the OS
#: and has nothing to do with ``os.system()``, which runs one.
SHELL_FUNCTIONS = {"os.system", "os.popen", "subprocess.getoutput", "subprocess.getstatusoutput",
                   "asyncio.create_subprocess_shell", "asyncio.subprocess.create_subprocess_shell",
                   "create_subprocess_shell"}


#: Qualified, for the same reason ``SHELL_FUNCTIONS`` is: ``self.run`` is ``AgentLoop.run``.
SPAWNERS = {"subprocess.run", "subprocess.check_output", "subprocess.check_call",
            "subprocess.Popen", "asyncio.create_subprocess_exec",
            "asyncio.subprocess.create_subprocess_exec", "create_subprocess_exec"}


def _is_string_assembly(node: ast.AST) -> bool:
    """Does this expression build a string out of parts? Those are the injection shapes."""
    if isinstance(node, (ast.BinOp, ast.JoinedStr)):        # a + b, a % b, f"..."
        return True
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("format", "join"))


def _scopes(tree: ast.AST):
    """Each function body, plus the module, so an assignment is matched to its own scope."""
    yield tree
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _string_built_names(scope: ast.AST) -> set[str]:
    """Locals in this scope assigned from a string-building expression.

    Assembling the command one line above the call is the same defect as assembling it inline,
    and it is the spelling real code uses - so a check that only reads the call's arguments
    reports the property is held while the bug sits directly above it. This is deliberately a
    single hop: it catches the shape that actually occurs without pretending to do dataflow.
    """
    built = set()
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign) and _is_string_assembly(node.value):
            built.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            if _is_string_assembly(node.value) or isinstance(node.op, ast.Add):
                built.add(node.target.id)
    return built


def _calls(tree: ast.AST):
    """Every call node with the dotted name it was spelled as, e.g. ``subprocess.run``."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        parts = []
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
        if isinstance(target, ast.Name):
            parts.append(target.id)
        yield node, ".".join(reversed(parts))


class NothingBuildsACommandOutOfAString(unittest.TestCase):
    """The argv discipline, asserted rather than trusted."""

    def test_no_call_passes_shell_true(self):
        """``shell=True`` is the switch that turns an argv list back into a parsed string."""
        offenders = []
        for path in SOURCES:
            tree = ast.parse(path.read_text())
            for node, name in _calls(tree):
                for keyword in node.keywords:
                    if (keyword.arg == "shell" and isinstance(keyword.value, ast.Constant)
                            and keyword.value.value is True):
                        offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} {name}")
        self.assertEqual(offenders, [], "a shell would parse this argument, not receive it")

    def test_only_the_two_intended_places_hand_a_string_to_a_shell(self):
        """A third shell call is the thing this file exists to catch."""
        found = []
        for path in SOURCES:
            relative = str(path.relative_to(ROOT))
            tree = ast.parse(path.read_text())
            for node, name in _calls(tree):
                if name not in SHELL_FUNCTIONS:
                    continue
                leaf = name.rsplit(".", 1)[-1]
                if (relative, leaf) not in SHELL_CALLS_ALLOWED:
                    found.append(f"{relative}:{node.lineno} {name}")
        self.assertEqual(found, [], "build the command as an argv list, or add it to "
                                    "SHELL_CALLS_ALLOWED with the reason it is safe")

    def test_the_allowed_places_still_exist(self):
        """Otherwise the allowlist above rots into a list of places that used to matter."""
        seen = set()
        for path in SOURCES:
            relative = str(path.relative_to(ROOT))
            tree = ast.parse(path.read_text())
            for _, name in _calls(tree):
                if name in SHELL_FUNCTIONS:
                    leaf = name.rsplit(".", 1)[-1]
                    if (relative, leaf) in SHELL_CALLS_ALLOWED:
                        seen.add((relative, leaf))
        self.assertEqual(seen, set(SHELL_CALLS_ALLOWED), "an allowlist entry names nothing")


class UntrustedNamesReachSubprocessAsArgv(unittest.TestCase):
    """The specific values a repository controls, checked at the call sites that take them."""

    def test_no_subprocess_command_is_built_by_string_assembly(self):
        """A git URL or ref is repository-supplied; assembling one into a command string is the bug.

        The check is for the *shapes* that build a string - concatenation, an f-string, ``%``,
        ``.format()``, ``" ".join`` - rather than for a list literal. Passing a variable that holds
        a list is just as safe and is what most of these call sites do; demanding a literal would
        fail them for no reason and teach the next author to work around this test.
        """
        offenders = []
        for path in SOURCES:
            tree = ast.parse(path.read_text())
            for scope in _scopes(tree):
                assembled = _string_built_names(scope)
                for node, name in _calls(scope):
                    if name not in SPAWNERS or not node.args:
                        continue
                    first = node.args[0]
                    culprit = (_is_string_assembly(first)
                               or (isinstance(first, ast.Name) and first.id in assembled))
                    if culprit:
                        offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} {name}")
        self.assertEqual(offenders, [], "pass the command as a list of arguments instead")

    def test_a_hostile_ref_cannot_close_a_quote(self):
        """End to end: the shell metacharacters in a ref reach git as one argument, or not at all.

        Asserted against the real resolver rather than by reading it, because the property that
        matters is what the process receives, not what the source looks like.
        """
        from picoagent.plugins import loader
        hostile = 'v1.0"; touch /tmp/picoagent-injection-canary; echo "'
        canary = pathlib.Path("/tmp/picoagent-injection-canary")
        canary.unlink(missing_ok=True)
        try:
            loader.checkout_path(f"git:example.invalid/thing@{hostile}", ROOT / "nonexistent")
        except Exception:
            pass                      # unreachable host, refused spec: either is fine here
        self.assertFalse(canary.exists(), "a ref reached a shell and ran as a command")


if __name__ == "__main__":
    unittest.main()
