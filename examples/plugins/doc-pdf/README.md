# doc-pdf

Read a PDF as **evidence**, not as prose.

A model asked to quote a 500-page standard will produce sentences that read perfectly, cite a
plausible page, and are not in the document. That failure is invisible to a reader who does not
have the PDF open, which is every reader of a compliance report. This plugin exists to make the
quotation checkable before it is written down.

## Tools

| Tool | Answers |
|---|---|
| `pdf_find` | *Is this sentence actually in the document, and on which page?* |
| `pdf_info` | *Which edition am I reading?* — page count, md5, metadata |
| `pdf_pages` | *What does page N say?* |
| `pdf_spans` | *Was this printed as the document's guidance, or as its content?* |
| `pdf_dump` | *Write the text out so it can be grepped later, without the PDF* |

`pdf_find` is the one that matters. The others support it.

## The three problems it solves

**A quotation spanning a line break.** Extracted PDF text carries the typesetter's line breaks,
so a real sentence rarely matches literally. Every comparison here normalises whitespace on both
sides. Without that, a verifier reports every true quotation as a fabrication, and a reader who
is told "unverified" about sentences that are plainly there stops reading the verifier at all.

**The printed page is not the file page.** A reader cites the number printed on the paper; a tool
addresses the page by its position in the file. Front matter offsets them — by 14 in NIST
SP 800-34 Rev. 1, by 27 in SP 800-53 Rev. 5. Pass `printed_offset` and every result carries both:

```
printed p.26 (PDF p.40)
```

Set it once per document in config instead of on every call:

```toml
[plugins.doc-pdf]
printed_offset = 14
```

**Guidance printed as italic.** NIST prints its advice about how to fill a template in italic and
the template's own content in regular type. A generator that cannot tell them apart publishes
NIST's advice as though the organisation had written it. `pdf_spans` reports the font of every run:

```
printed p.17 (PDF p.31) - 1 run(s), italic only
  [italic  TimesNewRomanPS-ItalicMT 9.0] Guide for Mapping Types of Information and Information Systems to Security Categories
```

## Dependency and licence

This is the only plugin in the repository with a third-party dependency:

```toml
python_deps = ["pymupdf>=1.24"]
```

**PyMuPDF is AGPL-3.0 or commercially licensed by Artifex.** That is a question a federal or DoD
site should answer deliberately rather than inherit. It is confined to one plugin so the answer
can be "we do not install doc-pdf" without losing anything else: `doc-ooxml` reads `.docx` and
`.xlsx` with the standard library alone, and the picoagent core has no dependencies at all.

Everything that imports PyMuPDF lives in `extract.py`. If you are auditing the licence surface,
that file is the whole of it. When the package is absent, every tool returns one error naming the
package and the licence rather than a traceback.

`pip install` of the declared dependency happens on `picoagent plugin add`; installing it yourself
is equivalent.

## Worked example

Confirming a citation before it goes into a report:

```
pdf_info  path=nist80034r1.pdf
  -> pages 149, md5 4086cebd881a90b19d97204c27f47130

pdf_find  path=nist80034r1.pdf printed_offset=14
          quote="Team leaders should have a designated alternate to act as the
                 leader if the primary leader is unavailable."
  -> FOUND on 1 page(s)
     printed p.26 (PDF p.40)
```

That is a citation someone can look up. Compare:

```
pdf_find  path=nist80034r1.pdf
          quote="Each team leader must appoint at least two alternates before the plan is approved."
  -> NOT FOUND. The sentence is not in this document as extracted. Do not quote it.
```

## What it does not do

* **It does not read scanned pages.** There is no OCR here. A PDF of photographed paper extracts
  as nothing, and `pdf_find` will report a true quotation as absent. Check `pdf_pages` output
  before trusting a NOT FOUND on a document you have not extracted before.
* **It does not reconstruct tables.** Text comes out in reading order, which for a table means
  cell text without the grid.
* **It does not fetch.** Getting the PDF is `instrument-cache`'s job; this plugin reads what is
  already on disk.
