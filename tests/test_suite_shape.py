"""Invariants about the test files themselves, so a green run means what it says.

Everything else here tests picoagent. This file tests the suite: the ways a test can be
written that make it silently not run. A test that never executes is worse than a missing
one, because the missing test is visible in the coverage of a file nobody wrote and this
one is invisible behind an OK.
"""
import pathlib
import re
import unittest

TESTS = pathlib.Path(__file__).resolve().parent
MAIN_BLOCK = re.compile(r'^if __name__ == "__main__":', re.M)
CLASS_LINE = re.compile(r"^class ", re.M)
RAW_MKDTEMP = re.compile(r"\btempfile\.mkdtemp\b")


def test_files() -> list[pathlib.Path]:
    return sorted(TESTS.glob("test_*.py"))


class NothingIsDefinedBelowTheEntryPoint(unittest.TestCase):
    """``unittest.main()`` runs where it is written, not at the end of the file.

    Discovery imports a module, so every class in it exists before the runner looks. Running
    the file directly executes top to bottom and calls ``unittest.main()`` at the line it
    appears on, which never returns - so a class written below it is never defined and its
    tests never run. Both runs print OK; only the counts differ, and nobody compares them.

    This bit us for real: nine files had drifted this way, hiding sixteen classes including
    every one covering path confinement, the endpoint scheme check, and project-config
    privilege. Each was appended to the end of a file whose entry point was already there.
    """

    def test_no_test_class_is_written_below_the_main_block(self):
        offenders = []
        for path in test_files():
            text = path.read_text()
            entry = MAIN_BLOCK.search(text)
            if entry and CLASS_LINE.search(text[entry.end():]):
                offenders.append(path.name)
        self.assertEqual(offenders, [], "move the __main__ block to the end of these files")

    def test_every_file_has_an_entry_point_to_be_last(self):
        """The invariant above is vacuous for a file with no ``__main__`` block at all."""
        missing = [p.name for p in test_files() if not MAIN_BLOCK.search(p.read_text())]
        self.assertEqual(missing, [], "these files cannot be run directly")


class TemporaryDirectoriesAreResolved(unittest.TestCase):
    """``mkdtemp`` hands back the path as ``TMPDIR`` spells it, links included.

    picoagent resolves the paths it reports, on purpose and in several places - the gate/tool
    seam, the trust store's key, the evidence scanner's containment check. So an assertion
    comparing something picoagent reported against a raw ``mkdtemp`` string compares two
    spellings of one directory. It passes wherever ``TMPDIR`` has no link in it and fails
    wherever it does, which on macOS is everywhere: ``mkdtemp`` returns ``/var/folders/...``
    and ``/var`` is a symlink to ``/private/var``. Twenty-two assertions here did that.

    ``helpers.temp_dir()`` resolves at the point the directory is made, which is the only place
    the decision has to be taken once. This is a tripwire rather than a convention because the
    failure it prevents is invisible on the machine the test is written on.

    ``tempfile.TemporaryDirectory`` is deliberately not banned. Those sites own their cleanup
    and use the directory as scratch space rather than comparing its path with a reported one,
    and they pass under both shapes of ``TMPDIR`` today. The rule is *resolve a path before
    comparing it*; if one of them ever compares, ``.resolve()`` belongs at that site.
    """

    def test_no_test_file_calls_mkdtemp_directly(self):
        offenders = [path.name for path in test_files() if RAW_MKDTEMP.search(path.read_text())]
        self.assertEqual(offenders, [], "use helpers.temp_dir() so the path is symlink-resolved")


if __name__ == "__main__":
    unittest.main()
