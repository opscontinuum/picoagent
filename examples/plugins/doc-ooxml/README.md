# doc-ooxml

Read `.docx` and `.xlsx` as **evidence**, with the standard library and nothing else.

The organisation's contingency plan is a Word file. The control baseline it is judged against is
a spreadsheet. Neither is markdown, and a tool that can only read markdown can only audit
repositories written for it — which is to say, nobody's actual plan.

## Tools

| Tool | Answers |
|---|---|
| `ooxml_find` | *Is this text actually in the document, and where?* |
| `ooxml_outline` | *What sections exist, and which are still empty?* |
| `ooxml_info` | *What is this file, which edition, how big?* |
| `ooxml_read` | *What does this section / these rows say?* |
| `ooxml_dump` | *Write it out as text so it can be grepped later* |

Same contract as [`doc-pdf`](../doc-pdf/), because the discipline is the same: a quotation is
worth nothing unless someone can find it again. What differs is the address. A PDF cites a page.
A Word document cites the **heading** it sits under — better, because headings survive
reformatting and page numbers do not. A spreadsheet cites a sheet and a cell.

## No dependency, and that is the point

```toml
python_deps = []
```

A `.docx` and an `.xlsx` are zips of XML, so `zipfile` and `xml.etree.ElementTree` are the whole
toolchain. `doc-pdf` needs a package because PDF is a page-description format with no text model;
Office documents have one. Keeping this half dependency-free means a site that declines
`doc-pdf` over its AGPL licence still reads the Word plan and the control workbook it was sent.

## Two hazards it exists to handle

**Template instructions read as content.** FedRAMP's ISCP template marks the text the author is
meant to delete in *italic* — 148 of its 315 blocks, opening with *"Delete this Template Revision
History page and all other instructional text from your final version"*. A reader that cannot see
italic quotes FedRAMP's instructions back as the organisation's own commitments. Every tool here
marks them:

```
block 6 (paragraph) under: (before the first heading)  [italic - template instruction, not content]
  Delete this Template Revision History page and all other instructional text...
```

**An empty template answered as though it were filled.** A heading with nothing under it but
`<Insert CSO Name>` is not a completed section. `ooxml_outline` shows the difference:

```
[18] H1 Introduction and Purpose  - 3 block(s), 218 word(s)  [2 with placeholders]
  [24] H2 FedRAMP Requirements and Guidance  - 3 block(s), 16 word(s)  [2 italic (instruction)]
  [38] H2 Assumptions  - 10 block(s), 175 word(s)  [8 italic (instruction); 2 with placeholders]
```

Three sections, none of them actually written yet.

## Three parsing decisions that are wrong in the obvious implementation

Each is a bug avoided rather than a preference, and each has a test class:

**The body is walked in order.** A `w:body` interleaves paragraphs and tables, and a table's cells
contain paragraphs of their own. `root.iter(w:p)` reads table text a second time and out of
sequence, so section boundaries land in the wrong place.

**Sheets resolve through the relationship part.** `xl/workbook.xml` names sheets and gives each an
`r:id`; the part it lives in comes from `xl/_rels/workbook.xml.rels`. Sorting `xl/worksheets/*.xml`
agrees often enough to look correct in testing, and when it disagrees every cell reference in a
report names the wrong sheet.

**The used range is computed.** `<dimension>` is optional, and the FedRAMP Rev. 5 baseline workbook
omits it, so the extent comes from the cell references actually present.

## Worked example

```
ooxml_info path=SSP-Appendix-G-ISCP-Template.docx
  -> docx, md5 59ac4c888f51bb5359903d47839c125d
     315 blocks (264 paragraphs, 51 tables), 40 headings
     148 block(s) entirely italic - likely template instructions, not content

ooxml_find path=LEGACY_FedRAMP_Security_Controls_Baseline.xlsx quote="LEGACY NOTICE"
  -> FOUND in 1 cell(s)
     COVERSHEET!A1: LEGACY NOTICE June 23, 2026 This is a legacy document...
```

## What it does not do

* **No `.doc`, `.xls` or `.pptx`.** The pre-2007 binary formats are not XML; PowerPoint is
  recognised and refused by name rather than mis-parsed.
* **No styling beyond italic and heading level.** Enough to separate instruction from content and
  to walk the outline; not a rendering engine.
* **No formula evaluation.** A formula cell yields the value Excel last cached, which is what an
  auditor wants and is not the same as recomputing.
* **No fetching.** Getting the file is `instrument-cache`'s job.
