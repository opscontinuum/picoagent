"""doc-ooxml: the parsing decisions that are wrong in the obvious implementation.

A ``.docx`` and an ``.xlsx`` are zips of XML, so the naive reader is short and subtly wrong in
three ways. Each has a class here, and each corresponds to a comment in ``ooxml.py``:

* ``BodyOrderTests`` - a ``w:body`` interleaves paragraphs and tables, and a table's cells hold
  paragraphs of their own. ``root.iter(w:p)`` therefore reads table text twice and out of order,
  which puts section boundaries in the wrong place and makes a heading appear to own content that
  belongs to the section before it.
* ``SheetResolutionTests`` - a sheet's XML part is named by a relationship, not by its position.
  Sorting ``xl/worksheets/*.xml`` agrees often enough to look correct in testing and mislabels
  every cell in a report when it does not.
* ``UsedRangeTests`` - ``<dimension>`` is optional. The FedRAMP Rev. 5 baseline workbook omits it.

``EvidenceTests`` covers the contract shared with ``doc-pdf``: a quotation must resolve to a
location or be reported absent, and template instructions must be distinguishable from content.

Fixtures are written here as real OOXML zips, so the suite exercises the parser without carrying
a binary and without a third-party writer.
"""
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from helpers import ROOT, run, tool_ctx

PLUGIN = ROOT / "examples/plugins/doc-ooxml"

import sys
sys.path.insert(0, str(PLUGIN))
import doc_ooxml     # noqa: E402
import ooxml         # noqa: E402

WNS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
SNS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
RNS = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
PKGNS = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'


def para(text, style=None, italic=False):
    props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    rpr = "<w:rPr><w:i/></w:rPr>" if italic else ""
    return f'<w:p>{props}<w:r>{rpr}<w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def table(rows):
    body = "".join(
        "<w:tr>" + "".join(f"<w:tc>{para(cell)}</w:tc>" for cell in row) + "</w:tr>"
        for row in rows)
    return f"<w:tbl>{body}</w:tbl>"


def write_docx(path: Path, body: str) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f'<w:document {WNS}><w:body>{body}</w:body></w:document>')
    return path


def write_xlsx(path: Path, sheets, shared=(), rels=None) -> Path:
    """``sheets`` is [(name, part filename, cells xml)]; ``rels`` overrides the id->part mapping."""
    with zipfile.ZipFile(path, "w") as z:
        entries = "".join(
            f'<sheet name="{name}" sheetId="{n}" r:id="rId{n}"/>'
            for n, (name, _, _) in enumerate(sheets, start=1))
        z.writestr("xl/workbook.xml", f'<workbook {SNS} {RNS}><sheets>{entries}</sheets></workbook>')
        mapping = rels or {f"rId{n}": part for n, (_, part, _) in enumerate(sheets, start=1)}
        links = "".join(f'<Relationship Id="{i}" Target="{t}" Type="x"/>' for i, t in mapping.items())
        z.writestr("xl/_rels/workbook.xml.rels", f"<Relationships {PKGNS}>{links}</Relationships>")
        for _, part, cells in sheets:
            z.writestr(f"xl/{part}", f'<worksheet {SNS}><sheetData>{cells}</sheetData></worksheet>')
        if shared:
            items = "".join(f"<si>{s}</si>" for s in shared)
            z.writestr("xl/sharedStrings.xml", f"<sst {SNS}>{items}</sst>")
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def tool(self, cls, **args):
        return run(cls({}).execute(args, tool_ctx(self.tmp)))


class BodyOrderTests(Base):
    def test_a_table_is_one_block_and_its_cells_are_not_read_again_as_paragraphs(self):
        path = write_docx(self.tmp / "a.docx",
                          para("before") + table([["x", "y"]]) + para("after"))
        blocks = ooxml.docx_blocks(path)
        self.assertEqual([b.kind for b in blocks], ["paragraph", "table", "paragraph"])
        self.assertEqual([b.text for b in blocks[::2]], ["before", "after"])
        self.assertEqual(blocks[1].rows, [["x", "y"]])

    def test_blocks_keep_printed_order_across_a_table(self):
        path = write_docx(self.tmp / "b.docx",
                          para("H", "Heading1") + table([["cell"]]) + para("tail"))
        blocks = ooxml.docx_blocks(path)
        self.assertEqual([b.index for b in blocks], [0, 1, 2])
        self.assertEqual(ooxml.heading_path(blocks, 2), "H")

    def test_an_empty_paragraph_is_dropped_rather_than_shifting_every_index(self):
        path = write_docx(self.tmp / "c.docx", para("one") + "<w:p/>" + para("two"))
        self.assertEqual([b.text for b in ooxml.docx_blocks(path)], ["one", "two"])


class HeadingPathTests(Base):
    def setUp(self):
        super().setUp()
        self.path = write_docx(self.tmp / "h.docx",
                               para("1 Intro", "Heading1") + para("body a")
                               + para("1.1 Scope", "Heading2") + para("body b")
                               + para("2 Ops", "Heading1") + para("body c"))
        self.blocks = ooxml.docx_blocks(self.path)

    def test_reports_the_chain_in_force(self):
        self.assertEqual(ooxml.heading_path(self.blocks, 3), "1 Intro > 1.1 Scope")

    def test_a_new_top_level_heading_clears_the_deeper_one(self):
        self.assertEqual(ooxml.heading_path(self.blocks, 5), "2 Ops")

    def test_read_by_heading_stops_at_the_next_heading_of_the_same_level(self):
        out = self.tool(doc_ooxml.ReadTool, path=str(self.path), heading=0)
        self.assertIn("body b", out.content)
        self.assertNotIn("body c", out.content, "section 1 must not swallow section 2")

    def test_reading_a_block_that_is_not_a_heading_is_refused(self):
        out = self.tool(doc_ooxml.ReadTool, path=str(self.path), heading=1)
        self.assertTrue(out.is_error)
        self.assertIn("not a heading", out.content)


class SheetResolutionTests(Base):
    def test_sheets_resolve_through_the_relationship_not_the_filename(self):
        """The order on disk deliberately disagrees with the workbook order."""
        path = write_xlsx(self.tmp / "s.xlsx",
                          sheets=[("Second", "worksheets/sheet2.xml", '<row r="1"><c r="A1" t="inlineStr"><is><t>from sheet2</t></is></c></row>'),
                                  ("First", "worksheets/sheet1.xml", '<row r="1"><c r="A1" t="inlineStr"><is><t>from sheet1</t></is></c></row>')],
                          rels={"rId1": "worksheets/sheet2.xml", "rId2": "worksheets/sheet1.xml"})
        self.assertEqual(ooxml.xlsx_sheets(path), ["Second", "First"])
        cells = ooxml.xlsx_cells(path, "Second")
        self.assertEqual(cells[0].value, "from sheet2",
                         "a filename-ordered reader would return sheet1's content here")

    def test_an_unknown_sheet_name_lists_the_ones_that_exist(self):
        path = write_xlsx(self.tmp / "t.xlsx",
                          sheets=[("Only", "worksheets/sheet1.xml", "")])
        out = self.tool(doc_ooxml.ReadTool, path=str(path), sheet="Missing")
        self.assertTrue(out.is_error)
        self.assertIn("Only", out.content)


class SharedStringTests(Base):
    def test_a_shared_string_split_into_formatting_runs_is_joined(self):
        path = write_xlsx(self.tmp / "r.xlsx",
                          sheets=[("S", "worksheets/sheet1.xml", '<row r="1"><c r="A1" t="s"><v>0</v></c></row>')],
                          shared=["<r><t>Access </t></r><r><t>Control</t></r>"])
        self.assertEqual(ooxml.xlsx_cells(path)[0].value, "Access Control")

    def test_an_out_of_range_shared_string_index_is_empty_not_a_crash(self):
        path = write_xlsx(self.tmp / "o.xlsx",
                          sheets=[("S", "worksheets/sheet1.xml", '<row r="1"><c r="A1" t="s"><v>99</v></c></row>')],
                          shared=["<t>only one</t>"])
        self.assertEqual(ooxml.xlsx_cells(path), [])


class UsedRangeTests(Base):
    def test_the_range_is_computed_when_the_workbook_declares_no_dimension(self):
        path = write_xlsx(self.tmp / "d.xlsx",
                          sheets=[("S", "worksheets/sheet1.xml",
                                   '<row r="2"><c r="B2" t="inlineStr"><is><t>x</t></is></c></row>'
                                   '<row r="7"><c r="D7" t="inlineStr"><is><t>y</t></is></c></row>')])
        self.assertEqual(ooxml.used_range(ooxml.xlsx_cells(path, "S")), "B2:D7")

    def test_columns_sort_by_length_then_letters_so_aa_follows_z(self):
        cells = [ooxml.Cell("S", "Z1", "z"), ooxml.Cell("S", "AA1", "aa"), ooxml.Cell("S", "B1", "b")]
        self.assertEqual(ooxml.used_range(cells), "B1:AA1")

    def test_an_empty_sheet_says_so(self):
        self.assertEqual(ooxml.used_range([]), "empty")


class EvidenceTests(Base):
    def setUp(self):
        super().setUp()
        self.docx = write_docx(self.tmp / "e.docx",
                               para("1 Scope", "Heading1")
                               + para("Delete this instructional text before publishing.", italic=True)
                               + para("The system recovers within 4 hours."))

    def test_a_quotation_resolves_to_its_heading(self):
        out = self.tool(doc_ooxml.FindTool, path=str(self.docx),
                        quote="The system recovers within 4 hours.")
        self.assertTrue(out.details["found"])
        self.assertIn("under: 1 Scope", out.content)

    def test_a_quotation_spanning_whitespace_still_matches(self):
        out = self.tool(doc_ooxml.FindTool, path=str(self.docx),
                        quote="The system\n  recovers   within 4 hours.")
        self.assertTrue(out.details["found"])

    def test_an_absent_sentence_is_reported_absent(self):
        out = self.tool(doc_ooxml.FindTool, path=str(self.docx),
                        quote="The system recovers within 2 hours.")
        self.assertFalse(out.details["found"])
        self.assertIn("Do not quote it", out.content)

    def test_template_instructions_are_flagged_as_instruction_not_content(self):
        """The defect this prevents: publishing FedRAMP's instructions as the organisation's words."""
        out = self.tool(doc_ooxml.FindTool, path=str(self.docx),
                        quote="Delete this instructional text")
        self.assertTrue(out.details["found"])
        self.assertEqual(out.details["italic"], [1])
        self.assertIn("template instruction, not content", out.content)

    def test_a_partly_italic_paragraph_is_not_called_an_instruction(self):
        path = write_docx(self.tmp / "m.docx",
                          '<w:p><w:r><w:rPr><w:i/></w:rPr><w:t>Note: </w:t></w:r>'
                          '<w:r><w:t>this is real content.</w:t></w:r></w:p>')
        self.assertFalse(ooxml.docx_blocks(path)[0].italic)

    def test_an_empty_quote_is_refused_rather_than_matching_everything(self):
        out = self.tool(doc_ooxml.FindTool, path=str(self.docx), quote="  ")
        self.assertTrue(out.is_error)


class OutlineTests(Base):
    def test_a_heading_with_no_content_is_reported_empty(self):
        path = write_docx(self.tmp / "x.docx",
                          para("1 Filled", "Heading1") + para("some words here")
                          + para("2 Blank", "Heading1"))
        out = self.tool(doc_ooxml.OutlineTool, path=str(path))
        self.assertEqual(out.details["empty"], 1)
        self.assertIn("EMPTY", out.content)

    def test_a_placeholder_is_reported_so_a_template_is_not_read_as_filled(self):
        path = write_docx(self.tmp / "p.docx",
                          para("1 Scope", "Heading1") + para("Prepared for &lt;Insert CSP Name&gt;."))
        out = self.tool(doc_ooxml.OutlineTool, path=str(path))
        self.assertIn("placeholders", out.content)

    def test_a_document_with_no_headings_says_so_instead_of_returning_nothing(self):
        path = write_docx(self.tmp / "n.docx", para("just body text"))
        out = self.tool(doc_ooxml.OutlineTool, path=str(path))
        self.assertEqual(out.details["headings"], 0)
        self.assertIn("no headings", out.content)

    def test_outline_refuses_a_workbook_with_a_useful_message(self):
        path = write_xlsx(self.tmp / "w.xlsx", sheets=[("S", "worksheets/sheet1.xml", "")])
        out = self.tool(doc_ooxml.OutlineTool, path=str(path))
        self.assertTrue(out.is_error)
        self.assertIn("ooxml_info", out.content)


class FileTypeTests(Base):
    def test_type_comes_from_content_not_extension(self):
        path = write_docx(self.tmp / "misnamed.xlsx", para("hello"))
        self.assertEqual(ooxml.kind(path), "docx")

    def test_a_powerpoint_is_refused_by_name(self):
        path = self.tmp / "deck.pptx"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("ppt/presentation.xml", "<p/>")
        out = self.tool(doc_ooxml.InfoTool, path=str(path))
        self.assertTrue(out.is_error)
        self.assertIn("PowerPoint", out.content)

    def test_a_file_that_is_not_a_zip_is_an_error_result(self):
        path = self.tmp / "plain.docx"
        path.write_text("not a zip")
        out = self.tool(doc_ooxml.InfoTool, path=str(path))
        self.assertTrue(out.is_error)
        self.assertIn("not a zip", out.content)

    def test_a_missing_path_is_an_error_result(self):
        out = self.tool(doc_ooxml.InfoTool)
        self.assertTrue(out.is_error)
        self.assertIn("path is required", out.content)


class DumpTests(Base):
    def test_a_docx_dump_records_the_source_and_hash(self):
        path = write_docx(self.tmp / "d2.docx", para("content"))
        out_dir = self.tmp / "out"
        out = self.tool(doc_ooxml.DumpTool, path=str(path), out_dir=str(out_dir))
        self.assertEqual(out.details["written"], 1)
        self.assertIn(ooxml.file_digest(path), (out_dir / "SOURCE.txt").read_text())
        self.assertIn("content", (out_dir / "document.txt").read_text())

    def test_an_xlsx_dump_writes_one_file_per_sheet_with_a_filesystem_safe_name(self):
        path = write_xlsx(self.tmp / "d3.xlsx",
                          sheets=[("Key to LI-SaaS/Baseline", "worksheets/sheet1.xml",
                                   '<row r="1"><c r="A1" t="inlineStr"><is><t>v</t></is></c></row>')])
        out_dir = self.tmp / "out2"
        self.tool(doc_ooxml.DumpTool, path=str(path), out_dir=str(out_dir))
        self.assertTrue((out_dir / "Key_to_LI-SaaS_Baseline.txt").exists())

    def test_a_second_dump_skips_what_it_wrote(self):
        path = write_docx(self.tmp / "d4.docx", para("x"))
        out_dir = self.tmp / "out3"
        self.tool(doc_ooxml.DumpTool, path=str(path), out_dir=str(out_dir))
        again = self.tool(doc_ooxml.DumpTool, path=str(path), out_dir=str(out_dir))
        self.assertEqual(again.details["written"], 0)
        self.assertEqual(again.details["skipped"], 1)


class ManifestTests(unittest.TestCase):
    def test_the_plugin_declares_no_dependency(self):
        import tomllib
        manifest = tomllib.loads((PLUGIN / "plugin.toml").read_text())
        self.assertEqual(manifest["python_deps"], [],
                         "doc-ooxml is the standard-library half of ingestion; a dependency here "
                         "would defeat the reason it is a separate plugin from doc-pdf")

    def test_every_declared_tool_registers(self):
        registered = []
        class FakeAPI:
            def plugin_config(self): return {}
            def register_tool(self, tool): registered.append(tool.name)
            def register_system_prompt_section(self, name, render): pass
        doc_ooxml.register(FakeAPI())
        self.assertEqual(sorted(registered),
                         ["ooxml_dump", "ooxml_find", "ooxml_info", "ooxml_outline", "ooxml_read"])


if __name__ == "__main__":
    unittest.main()
