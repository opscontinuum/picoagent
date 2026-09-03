"""doc-pdf: the properties that decide whether a citation can be trusted.

The plugin's reason to exist is that a model asked to quote a long PDF will produce sentences
that read perfectly and are not in the document. Three things have to hold for ``pdf_find`` to
catch that, and each has a class here:

* ``NormalisationTests`` - extracted PDF text carries the typesetter's line breaks, so a real
  quotation spanning a line break must still match. A verifier that reports every true quote as
  a fabrication is worse than no verifier: it trains the reader to ignore it.
* ``FindTests`` - a sentence that is absent must be reported as absent, in words that tell the
  caller not to quote it. A near-miss (a paraphrase) must not pass.
* ``PageArithmeticTests`` - a citation names the page a reader can look up, which is not the
  page the file is addressed by. Front matter offsets the two.

These run without PyMuPDF installed: ``extract`` is stubbed at the seam. The dependency is
exercised separately by ``PyMuPDFTests``, which skips when it is absent.
"""
import shutil
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT, run, tool_ctx

PLUGIN = ROOT / "examples/plugins/doc-pdf"

import sys
sys.path.insert(0, str(PLUGIN))
import doc_pdf          # noqa: E402
import extract          # noqa: E402


class FakePage:
    def __init__(self, number, text):
        self.number, self.text = number, text


def with_pages(pages):
    """Replace the extraction seam with fixed pages; returns a restore callable."""
    original = extract.iter_pages
    extract.iter_pages = lambda path: iter(pages)
    return lambda: setattr(extract, "iter_pages", original)


# A sentence broken the way a PDF extractor breaks one: mid-clause, with the indentation of
# the next line still attached. Taken from the shape of NIST SP 800-34 Rev. 1 Sec. 3.4.6.
WRAPPED = FakePage(40, "3.4.6 Roles and Responsibilities\n\nTeam leaders should have a\n"
                       "designated alternate to act as the   leader if the primary leader\nis "
                       "unavailable.\n\n26\n")


class NormalisationTests(unittest.TestCase):
    def test_collapses_every_whitespace_run_to_one_space(self):
        self.assertEqual(doc_pdf.normalise("a\n  b\t\tc\r\nd"), "a b c d")

    def test_strips_the_ends(self):
        self.assertEqual(doc_pdf.normalise("\n  padded  \n"), "padded")

    def test_a_quote_spanning_a_line_break_matches(self):
        """The property the whole tool rests on."""
        restore = with_pages([WRAPPED])
        self.addCleanup(restore)
        out = run(doc_pdf.FindTool({}).execute(
            {"path": "x.pdf", "quote": "Team leaders should have a designated alternate to act "
                                       "as the leader if the primary leader is unavailable."},
            tool_ctx(Path("/tmp"))))
        self.assertFalse(out.is_error)
        self.assertTrue(out.details["found"])
        self.assertEqual(out.details["pages"], [40])

    def test_a_quote_retyped_with_different_spacing_still_matches(self):
        restore = with_pages([WRAPPED])
        self.addCleanup(restore)
        out = run(doc_pdf.FindTool({}).execute(
            {"path": "x.pdf", "quote": "  designated   alternate\n to act as the leader "},
            tool_ctx(Path("/tmp"))))
        self.assertTrue(out.details["found"])


class FindTests(unittest.TestCase):
    def test_an_absent_sentence_is_reported_absent_and_tells_the_caller_not_to_quote_it(self):
        restore = with_pages([WRAPPED])
        self.addCleanup(restore)
        out = run(doc_pdf.FindTool({}).execute(
            {"path": "x.pdf", "quote": "Every team leader must appoint two deputies."},
            tool_ctx(Path("/tmp"))))
        self.assertFalse(out.details["found"])
        self.assertIn("NOT FOUND", out.content)
        self.assertIn("Do not quote it", out.content)

    def test_a_paraphrase_does_not_pass(self):
        """The failure mode this tool exists to catch: plausible, close, not in the document."""
        restore = with_pages([WRAPPED])
        self.addCleanup(restore)
        out = run(doc_pdf.FindTool({}).execute(
            {"path": "x.pdf", "quote": "Team leaders must have a designated alternate"},
            tool_ctx(Path("/tmp"))))
        self.assertFalse(out.details["found"], "'must' is not 'should'; that is a different claim")

    def test_reports_every_page_a_quote_appears_on(self):
        restore = with_pages([FakePage(3, "a boilerplate footer"), FakePage(9, "a boilerplate footer")])
        self.addCleanup(restore)
        out = run(doc_pdf.FindTool({}).execute(
            {"path": "x.pdf", "quote": "boilerplate footer"}, tool_ctx(Path("/tmp"))))
        self.assertEqual(out.details["pages"], [3, 9])

    def test_an_empty_quote_is_refused_rather_than_matching_everything(self):
        out = run(doc_pdf.FindTool({}).execute(
            {"path": "x.pdf", "quote": "   "}, tool_ctx(Path("/tmp"))))
        self.assertTrue(out.is_error)
        self.assertIn("quote is required", out.content)


class PageArithmeticTests(unittest.TestCase):
    def test_offset_renders_the_printed_page_the_reader_cites(self):
        self.assertEqual(doc_pdf._printed(40, 14), "printed p.26 (PDF p.40)")

    def test_no_offset_reports_the_pdf_page_only_rather_than_inventing_one(self):
        self.assertEqual(doc_pdf._printed(40, 0), "PDF p.40")

    def test_find_reports_both_numbers(self):
        restore = with_pages([WRAPPED])
        self.addCleanup(restore)
        out = run(doc_pdf.FindTool({}).execute(
            {"path": "x.pdf", "quote": "Roles and Responsibilities", "printed_offset": 14},
            tool_ctx(Path("/tmp"))))
        self.assertEqual(out.details["printed"], [26])
        self.assertIn("printed p.26 (PDF p.40)", out.content)

    def test_offset_can_be_defaulted_per_document_in_plugin_config(self):
        restore = with_pages([WRAPPED])
        self.addCleanup(restore)
        out = run(doc_pdf.FindTool({"printed_offset": 14}).execute(
            {"path": "x.pdf", "quote": "Roles and Responsibilities"}, tool_ctx(Path("/tmp"))))
        self.assertEqual(out.details["printed"], [26])

    def test_a_non_integer_offset_is_refused_rather_than_silently_ignored(self):
        out = run(doc_pdf.FindTool({}).execute(
            {"path": "x.pdf", "quote": "x", "printed_offset": "fourteen"}, tool_ctx(Path("/tmp"))))
        self.assertTrue(out.is_error)
        self.assertIn("printed_offset", out.content)


class PathTests(unittest.TestCase):
    def test_a_missing_path_is_an_error_result_not_an_exception(self):
        out = run(doc_pdf.InfoTool({}).execute({}, tool_ctx(Path("/tmp"))))
        self.assertTrue(out.is_error)
        self.assertIn("path is required", out.content)

    def test_a_nul_byte_in_the_path_is_refused(self):
        out = run(doc_pdf.InfoTool({}).execute({"path": "a\x00b.pdf"}, tool_ctx(Path("/tmp"))))
        self.assertTrue(out.is_error)
        self.assertIn("NUL", out.content)


class SpanStyleTests(unittest.TestCase):
    """Flag values confirmed against PyMuPDF 1.28.2 reading SP 800-34 Rev. 1 printed p.17."""

    def test_italic_run_is_italic(self):
        span = extract.Span(page=31, text="Guide for Mapping Types", font="TimesNewRomanPS-ItalicMT",
                            size=11.0, flags=6)
        self.assertTrue(span.italic)
        self.assertFalse(span.bold)

    def test_regular_run_is_neither(self):
        span = extract.Span(page=31, text="body text", font="TimesNewRomanPSMT", size=11.0, flags=4)
        self.assertFalse(span.italic)
        self.assertFalse(span.bold)

    def test_bold_run_is_bold(self):
        span = extract.Span(page=31, text="Maximum Tolerable Downtime (MTD).",
                            font="TimesNewRomanPS-BoldMT", size=11.0, flags=20)
        self.assertTrue(span.bold)
        self.assertFalse(span.italic)

    def test_font_name_is_a_fallback_when_flags_are_absent(self):
        """Not every producer sets the flags; the name usually still says so."""
        span = extract.Span(page=1, text="x", font="SomeFont-Italic", size=10.0, flags=0)
        self.assertTrue(span.italic)


class MissingDependencyTests(unittest.TestCase):
    def test_the_install_hint_names_the_package_and_the_licence(self):
        """doc-pdf is the only plugin with a dependency; the failure has to explain itself."""
        self.assertIn("pymupdf", extract.INSTALL_HINT)
        self.assertIn("AGPL", extract.INSTALL_HINT)

    def test_a_missing_pymupdf_surfaces_as_a_tool_error_not_a_traceback(self):
        original = extract._pymupdf
        extract._pymupdf = lambda: (_ for _ in ()).throw(extract.PdfError(extract.INSTALL_HINT))
        self.addCleanup(lambda: setattr(extract, "_pymupdf", original))
        out = run(doc_pdf.InfoTool({}).execute({"path": "x.pdf"}, tool_ctx(Path("/tmp"))))
        self.assertTrue(out.is_error)
        self.assertIn("pip install", out.content)


class ManifestTests(unittest.TestCase):
    def test_the_manifest_declares_the_dependency_it_needs(self):
        import tomllib
        manifest = tomllib.loads((PLUGIN / "plugin.toml").read_text())
        self.assertEqual(manifest["python_deps"], ["pymupdf>=1.24"])
        self.assertEqual(manifest["entry"], "doc_pdf:register")

    def test_every_declared_tool_registers(self):
        registered = []
        class FakeAPI:
            def plugin_config(self): return {}
            def register_tool(self, tool): registered.append(tool.name)
            def register_system_prompt_section(self, name, render): pass
        doc_pdf.register(FakeAPI())
        self.assertEqual(sorted(registered),
                         ["pdf_dump", "pdf_find", "pdf_info", "pdf_pages", "pdf_spans"])


def _pymupdf_available():
    try:
        import pymupdf  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(_pymupdf_available(), "PyMuPDF is not installed")
class PyMuPDFTests(unittest.TestCase):
    """The real extraction path, on a PDF built here so the suite carries no fixture binary."""

    @classmethod
    def setUpClass(cls):
        import pymupdf
        cls.tmp = Path(tempfile.mkdtemp())
        cls.pdf = cls.tmp / "sample.pdf"
        doc = pymupdf.open()
        page = doc.new_page()
        # Deliberately broken across two lines, as a typesetter would break it.
        page.insert_text((72, 100), "Team leaders should have a designated", fontname="tiro", fontsize=11)
        page.insert_text((72, 116), "alternate to act as the leader.", fontname="tiro", fontsize=11)
        page.insert_text((72, 150), "This italic line is guidance.", fontname="tiit", fontsize=9)
        doc.new_page().insert_text((72, 100), "Second page body.", fontname="tiro", fontsize=11)
        doc.save(cls.pdf)
        doc.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_info_reports_the_page_count_and_a_stable_hash(self):
        out = run(doc_pdf.InfoTool({}).execute({"path": str(self.pdf)}, tool_ctx(self.tmp)))
        self.assertEqual(out.details["pages"], 2)
        self.assertEqual(out.details["md5"], extract.file_digest(self.pdf))

    def test_a_quote_broken_across_lines_in_a_real_pdf_is_found(self):
        out = run(doc_pdf.FindTool({}).execute(
            {"path": str(self.pdf),
             "quote": "Team leaders should have a designated alternate to act as the leader."},
            tool_ctx(self.tmp)))
        self.assertTrue(out.details["found"])
        self.assertEqual(out.details["pages"], [1])

    def test_italic_and_regular_runs_are_distinguished_in_a_real_pdf(self):
        out = run(doc_pdf.SpansTool({}).execute(
            {"path": str(self.pdf), "page": 1, "only": "italic"}, tool_ctx(self.tmp)))
        self.assertEqual(out.details["runs"], 1)
        self.assertIn("guidance", out.content)

    def test_dump_writes_one_file_per_page_and_records_the_source(self):
        out_dir = self.tmp / "pages"
        out = run(doc_pdf.DumpTool({}).execute(
            {"path": str(self.pdf), "out_dir": str(out_dir)}, tool_ctx(self.tmp)))
        self.assertEqual(out.details["written"], 2)
        self.assertTrue((out_dir / "page-0001.txt").exists())
        self.assertIn(extract.file_digest(self.pdf), (out_dir / "SOURCE.txt").read_text())

    def test_a_second_dump_skips_what_it_already_wrote(self):
        out_dir = self.tmp / "pages-twice"
        args = {"path": str(self.pdf), "out_dir": str(out_dir)}
        run(doc_pdf.DumpTool({}).execute(args, tool_ctx(self.tmp)))
        again = run(doc_pdf.DumpTool({}).execute(args, tool_ctx(self.tmp)))
        self.assertEqual(again.details["written"], 0)
        self.assertEqual(again.details["skipped"], 2)

    def test_a_page_outside_the_document_is_an_error_result(self):
        out = run(doc_pdf.SpansTool({}).execute(
            {"path": str(self.pdf), "page": 99}, tool_ctx(self.tmp)))
        self.assertTrue(out.is_error)
        self.assertIn("outside 1..2", out.content)

    def test_a_file_that_is_not_a_pdf_is_an_error_result(self):
        junk = self.tmp / "not.pdf"
        junk.write_text("plain text, not a PDF")
        out = run(doc_pdf.InfoTool({}).execute({"path": str(junk)}, tool_ctx(self.tmp)))
        self.assertTrue(out.is_error)
        self.assertIn("cannot open", out.content)


if __name__ == "__main__":
    unittest.main()
