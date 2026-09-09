"""Line-coverage statistics for a release, using only the standard library.

picoagent declares no third-party runtime dependencies, and the promise a contributor should
be able to make is that a clone plus a Python is the whole toolchain. A coverage tool is a
development dependency rather than a runtime one, so ``coverage.py`` would not have broken
that promise - but it would have broken the smaller one that matters here: this number is
evidence for an assessor, and evidence nobody can reproduce without a package index (an
air-gapped or accredited machine has none) is evidence with a footnote on it. ``trace`` ships
with CPython, so this runs wherever the suite runs.

What it does: runs the whole ``tests`` suite under :class:`trace.Trace`, then reports, per
module, how many of that module's executable lines were reached. Three corpora are reported
separately, because they answer different questions - ``picoagent/`` is the application,
``examples/plugins/`` is code the repository ships for people to load, and
``picoagent/testing/`` is the fake servers the suite runs against, which would flatter the
application's number if they were averaged into it.

    python3 tools/coverage_report.py                # the whole suite
    python3 tools/coverage_report.py -p 'test_to*'  # one file, while you work on it

Nothing here fails a build. There is no threshold, and adding one would turn a number that
is read into a number that is gamed; a module list that shows *which* code the release
exercised is what the statistic is for.

Three things the numbers do not count, stated because an unqualified percentage invites
over-reading:

* Lines executed in a child process. Threads are counted (this installs
  :func:`threading.settrace` alongside :func:`sys.settrace`, so the fake HTTP servers the
  tests run are traced), but a subprocess gets its own interpreter and is not. That is why
  ``picoagent/testing/fake_mcp.py`` reads 0% while ``tests/test_mcp_plugin.py`` exercises it
  hard: it is spawned over a pipe.
* Import-time lines of a module no test ever imports still count in the denominator, which
  is the honest direction: a module nothing imports reads as 0%, not as absent.
* The few lines that run after tracing is lost and before the next test re-arms it. See
  :class:`RearmingResult`; the count of losses is printed rather than swallowed.

Line coverage is also not branch coverage. A line that ran is not a line whose every
outcome was tested.
"""

from __future__ import annotations

import argparse
import sys
import threading
import trace
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def executable_lines(path: Path) -> set[int]:
    """Line numbers in ``path`` that can produce a trace event.

    Taken from the compiled code rather than from the text, so a continuation line, a
    docstring and a blank line are not counted as code that went untested. ``co_lines``
    reports the line each range of bytecode belongs to; nested code objects (functions,
    comprehensions, classes) carry their own and are walked too.
    """
    code = compile(path.read_text(encoding="utf-8"), str(path), "exec")
    lines: set[int] = set()
    pending = [code]
    while pending:
        current = pending.pop()
        for _, _, line in current.co_lines():
            if line:
                lines.add(line)
        pending.extend(k for k in current.co_consts if isinstance(k, types.CodeType))
    return lines


def source_files(directory: Path, skip: Path | None = None) -> list[Path]:
    """Every ``.py`` file under ``directory`` that ships, in a stable order."""
    return sorted(p for p in directory.rglob("*.py")
                  if "__pycache__" not in p.parts and not (skip and skip in p.parents))


class RearmingResult(unittest.TextTestResult):
    """A result that puts the trace function back before each test, and counts the losses.

    CPython removes a trace function that raises, and a Python-level trace function called on
    an already-exhausted stack raises :exc:`RecursionError` - so a test that deliberately
    recurses to the limit (``test_config_refusals`` nests TOML past the parser's stack, which
    is a defended behaviour of the product) turns tracing off for the rest of the process.
    Measured, and it is not subtle: without this, every module first executed after that test
    reported 0% - ``rules``, ``es_doctor``, ``stig_runner`` and ``compaction`` all read as
    untested code that four test files exercise.

    Re-arming per test cannot recover the lines executed between the loss and the next test
    starting, so the count is reported rather than swallowed.
    """

    tracefunc: object = None
    rearmed = 0

    def startTest(self, test: unittest.TestCase) -> None:
        if sys.gettrace() is not self.tracefunc:
            sys.settrace(self.tracefunc)  # type: ignore[arg-type]
            type(self).rearmed += 1
        super().startTest(test)


def run_suite(pattern: str) -> dict[str, set[int]]:
    """Run the suite under ``trace`` and return the line numbers reached, per file."""
    # What ``python3 -m unittest discover -s tests`` does: the tests directory is not a
    # package, so it becomes the top level itself and goes on the path. The repository root
    # goes on too, ahead of it, so ``import picoagent`` reads the tree under test rather than
    # an installed copy of some other revision.
    sys.path.insert(0, str(ROOT))

    def discover_and_run() -> unittest.TestResult:
        # Discovery imports every test module, which imports the code under test, and a
        # module body runs exactly once. Tracing has to be on for that import or every
        # ``def``, ``class`` and constant in the tree reads as never executed - the
        # denominator counts them, so a run that started tracing afterwards understates
        # coverage by the whole import-time surface of the package.
        suite = unittest.TestLoader().discover(start_dir=str(ROOT / "tests"), pattern=pattern)
        return unittest.TextTestRunner(verbosity=1, stream=sys.stderr,
                                       resultclass=RearmingResult).run(suite)

    # No ``ignoredirs``. It would be the obvious way to keep the standard library out of the
    # counts, and it silently drops modules of ours: ``trace`` caches its ignore decision
    # under the file's *basename*, so once ``asyncio/events.py`` or ``unittest/loader.py`` has
    # been seen and ignored, ``picoagent/core/events.py`` and ``picoagent/plugins/loader.py``
    # are ignored too and report 0%. Measured, not theorised - that is what this script said
    # about three modules the suite plainly exercises. Everything is traced and the filtering
    # happens below, against full paths.
    #
    # ``runfunc`` installs :func:`sys.settrace` for this thread only, so the fake HTTP and MCP
    # servers the tests run in threads would go uncounted. :func:`threading.settrace` covers
    # threads started from here on, which is every one the suite creates.
    tracer = trace.Trace(count=1, trace=0)
    RearmingResult.tracefunc = tracer.globaltrace
    RearmingResult.rearmed = 0
    threading.settrace(tracer.globaltrace)
    try:
        result = tracer.runfunc(discover_and_run)
    finally:
        threading.settrace(None)
    if RearmingResult.rearmed:
        print(f"\ntracing was lost and re-armed {RearmingResult.rearmed} time(s); lines run "
              "between each loss and the next test starting are not counted", file=sys.stderr)
    if not result.wasSuccessful():
        print("\n!! the suite did not pass; the coverage below is of a failing run\n",
              file=sys.stderr)
    reached: dict[str, set[int]] = {}
    for (filename, lineno), _count in tracer.results().counts.items():
        reached.setdefault(filename, set()).add(lineno)
    return reached


def report(title: str, directory: Path, reached: dict[str, set[int]],
           skip: Path | None = None) -> tuple[int, int]:
    """Print one corpus as a table, least-covered first, and return (covered, executable)."""
    rows: list[tuple[float, str, int, int]] = []
    for path in source_files(directory, skip):
        lines = executable_lines(path)
        if not lines:
            continue
        hit = reached.get(str(path), set()) | reached.get(str(path.resolve()), set())
        # A line the interpreter reported and this script did not count as executable means
        # the denominator is wrong, and a wrong denominator is a wrong percentage rather than
        # a missing one. Say so instead of reporting the number as if it were sound.
        outside = hit - lines
        if outside:
            print(f"!! {path.relative_to(ROOT)}: {len(outside)} traced lines are not in the "
                  f"executable set ({sorted(outside)[:5]}); the denominator is wrong",
                  file=sys.stderr)
        covered = len(lines & hit)
        rows.append((covered / len(lines), str(path.relative_to(ROOT)), covered, len(lines)))
    rows.sort()
    total_covered = sum(row[2] for row in rows)
    total_lines = sum(row[3] for row in rows)
    width = max((len(row[1]) for row in rows), default=len(title))
    print(f"\n{title}\n")
    print(f"{'module'.ljust(width)}  {'cov':>6}  {'lines':>11}")
    print(f"{'-' * width}  {'-' * 6}  {'-' * 11}")
    for fraction, name, covered, lines in rows:
        print(f"{name.ljust(width)}  {fraction * 100:5.1f}%  {covered:5d}/{lines:<5d}")
    share = (total_covered / total_lines * 100) if total_lines else 0.0
    print(f"{'-' * width}  {'-' * 6}  {'-' * 11}")
    print(f"{'TOTAL'.ljust(width)}  {share:5.1f}%  {total_covered:5d}/{total_lines:<5d}")
    return total_covered, total_lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-p", "--pattern", default="test*.py",
                        help="unittest discover pattern (default: %(default)s)")
    args = parser.parse_args()

    reached = run_suite(args.pattern)
    testing = ROOT / "picoagent" / "testing"
    package = report("picoagent - the application", ROOT / "picoagent", reached, skip=testing)
    plugins = report("examples/plugins - the provider references, loaded only if the user asks",
                     ROOT / "examples" / "plugins", reached)
    fakes = report("picoagent/testing - the fake servers the suite runs against", testing, reached)

    covered = package[0] + plugins[0] + fakes[0]
    lines = package[1] + plugins[1] + fakes[1]
    print(f"\napplication: {package[0] / package[1] * 100:.1f}%   "
          f"application + shipped plugins: "
          f"{(package[0] + plugins[0]) / (package[1] + plugins[1]) * 100:.1f}%   "
          f"whole tree: {covered / lines * 100:.1f}%")
    scope = "one full suite run" if args.pattern == "test*.py" else f"tests matching {args.pattern!r}"
    print(f"Line coverage of {scope}. No threshold is applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
