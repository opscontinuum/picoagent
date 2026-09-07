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


if __name__ == "__main__":
    unittest.main()
