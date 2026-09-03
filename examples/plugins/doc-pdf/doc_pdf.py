"""doc-pdf: read a PDF as evidence rather than as prose.

The tools here exist to make a quotation *checkable*. A compliance finding that cites a
standard is worth nothing if nobody can confirm the sentence is in the document, on that page,
in that edition - and a model asked to quote a 500-page PDF from memory will produce sentences
that read perfectly and are not there.

So the design is built around one operation, ``pdf_find``: give it the sentence you are about
to quote and it tells you which page it is on, or that it is on none. Everything else supports
that - ``pdf_info`` pins the edition, ``pdf_pages`` reads, ``pdf_dump`` writes the text out so
an external grep can check it later, and ``pdf_spans`` answers the one question plain text
cannot: whether a run of text was printed as guidance or as content.

**Whitespace.** Extracted PDF text breaks lines wherever the typesetter did, so a quotation
spanning a line break never matches literally. Every comparison here normalises runs of
whitespace to a single space on both sides. This is the difference between a verification tool
that works and one that reports every true quotation as a fabrication.

**Printed pages.** A reader cites the number printed on the page; a tool addresses the page by
its position in the file. Front matter makes those differ - in SP 800-34 Rev. 1 by 14, in
SP 800-53 Rev. 5 by 27. Pass ``printed_offset`` and every result carries both numbers, so the
citation that reaches the report is the one a human can look up.
"""
from __future__ import annotations

import re
from pathlib import Path

from picoagent.core.tools import PathRefused, resolve_path, truncate
from picoagent.core.types import ToolResult

import extract

PROMPT_NOTE = """\
doc-pdf is loaded. Before quoting any PDF in a report, finding or citation, confirm the
sentence with pdf_find; a quote that does not resolve to a page is not evidence, however
plausible it reads. Record the pdf_info md5 alongside the citation so the edition is pinned.
Where a document prints guidance in italic and content in regular type - NIST does - use
pdf_spans before treating a sentence as the document's own content."""

_WHITESPACE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """Collapse every whitespace run to one space. Applied to both sides of every comparison."""
    return _WHITESPACE.sub(" ", text).strip()


def result(ctx, text: str, is_error: bool = False, **details) -> ToolResult:
    body, cut = truncate(text, ctx.config["tool_output_max_bytes"], ctx.config["tool_output_max_lines"])
    return ToolResult(ctx.tool_call_id, body + ("\n[truncated]" if cut else ""), is_error=is_error,
                      details=details)


def _printed(page: int, offset: int) -> str:
    """Render a page as the reader cites it, with the file position alongside."""
    return f"PDF p.{page}" if not offset else f"printed p.{page - offset} (PDF p.{page})"


# --------------------------------------------------------------------------- tools

class _PdfTool:
    """Base: resolves the path argument and turns expected failures into error results."""

    def __init__(self, config: dict):
        self.config = config

    async def execute(self, args: dict, ctx) -> ToolResult:
        try:
            return self.run(args, ctx)
        except extract.PdfError as exc:
            return result(ctx, str(exc), is_error=True)
        except (PathRefused, ValueError) as exc:
            return result(ctx, f"refused: {exc}", is_error=True)

    def run(self, args: dict, ctx) -> ToolResult:   # pragma: no cover - overridden
        raise NotImplementedError

    def path(self, args: dict, ctx) -> Path:
        raw = args.get("path")
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("path is required")
        if "\x00" in raw:
            raise ValueError("path contains a NUL byte")
        return resolve_path(ctx, raw)

    def offset(self, args: dict) -> int:
        """Printed-page offset: 0 unless given here or defaulted in config."""
        given = args.get("printed_offset")
        if given is None:
            given = self.config.get("printed_offset", 0)
        if not isinstance(given, int) or isinstance(given, bool):
            raise ValueError(f"printed_offset must be a whole number, got {given!r}")
        return given


class InfoTool(_PdfTool):
    name = "pdf_info"
    description = ("Page count, md5 and metadata for a PDF. Record the md5 with any citation so "
                   "the edition that was read is pinned and the reading is reproducible.")
    parameters = {"type": "object", "properties": {
        "path": {"type": "string", "description": "path to the PDF"}}, "required": ["path"]}

    def run(self, args, ctx):
        data = extract.info(self.path(args, ctx))
        lines = [f"{data['path']}",
                 f"  pages     {data['pages']}",
                 f"  md5       {data['md5']}",
                 f"  encrypted {data['encrypted']}"]
        lines += [f"  {k:<9} {v}" for k, v in data["metadata"].items()]
        return result(ctx, "\n".join(lines), **data)


class PagesTool(_PdfTool):
    name = "pdf_pages"
    description = ("Text of a page range, one page at a time with the boundaries marked. Prefer a "
                   "narrow range: a whole standard will not fit in context and does not need to.")
    parameters = {"type": "object", "properties": {
        "path": {"type": "string"},
        "first": {"type": "integer", "description": "first PDF page, 1-based (default 1)"},
        "last": {"type": "integer", "description": "last PDF page, inclusive (default: first)"},
        "printed_offset": {"type": "integer",
                           "description": "printed page = PDF page - offset; 14 for SP 800-34 "
                                          "Rev. 1, 27 for SP 800-53 Rev. 5"}},
        "required": ["path"]}

    def run(self, args, ctx):
        first = args.get("first", 1)
        last = args.get("last", first)
        offset = self.offset(args)
        found = extract.pages(self.path(args, ctx), first=first, last=last)
        blocks = [f"--- {_printed(page.number, offset)} ---\n{page.text.rstrip()}" for page in found]
        return result(ctx, "\n\n".join(blocks), pages=[p.number for p in found])


class FindTool(_PdfTool):
    name = "pdf_find"
    description = ("Locate a quotation in a PDF and report the page, comparing with whitespace "
                   "normalised so a sentence broken across lines still matches. Use this on every "
                   "sentence before quoting it: no page means the sentence is not in the document.")
    parameters = {"type": "object", "properties": {
        "path": {"type": "string"},
        "quote": {"type": "string", "description": "the sentence or phrase to confirm"},
        "printed_offset": {"type": "integer", "description": "printed page = PDF page - offset"},
        "context": {"type": "integer",
                    "description": "characters of surrounding text to return per hit (default 200)"}},
        "required": ["path", "quote"]}

    def run(self, args, ctx):
        quote = args.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            raise ValueError("quote is required")
        needle = normalise(quote)
        offset, window = self.offset(args), args.get("context", 200)
        path = self.path(args, ctx)

        hits = []
        for page in extract.iter_pages(path):
            haystack = normalise(page.text)
            start = haystack.find(needle)
            if start >= 0:
                left = max(0, start - window // 2)
                hits.append((page.number, haystack[left:start + len(needle) + window // 2]))

        if not hits:
            return result(ctx, f"NOT FOUND in {path.name}: {quote[:120]!r}\n"
                               f"The sentence is not in this document as extracted. Do not quote it. "
                               f"If you believe it is there, check you have the right edition "
                               f"(pdf_info md5) and that you have not paraphrased.",
                          found=False, quote=quote)
        lines = [f"FOUND on {len(hits)} page(s) of {path.name}:"]
        for number, snippet in hits:
            lines.append(f"\n{_printed(number, offset)}\n  ...{snippet}...")
        return result(ctx, "\n".join(lines), found=True,
                      pages=[n for n, _ in hits],
                      printed=[n - offset for n, _ in hits] if offset else None)


class SpansTool(_PdfTool):
    name = "pdf_spans"
    description = ("Runs of text on one page with their font, size and italic/bold state. The "
                   "question this answers and plain text cannot: was this sentence printed as the "
                   "document's guidance about how to fill a template, or as content of it?")
    parameters = {"type": "object", "properties": {
        "path": {"type": "string"},
        "page": {"type": "integer", "description": "PDF page, 1-based"},
        "printed_offset": {"type": "integer", "description": "printed page = PDF page - offset"},
        "only": {"type": "string", "enum": ["italic", "bold", "regular"],
                 "description": "limit to one style (default: all runs)"}},
        "required": ["path", "page"]}

    def run(self, args, ctx):
        page = args.get("page")
        if not isinstance(page, int) or isinstance(page, bool):
            raise ValueError("page must be a whole number")
        wanted = args.get("only")
        found = extract.spans(self.path(args, ctx), page)
        if wanted == "italic":
            found = [s for s in found if s.italic]
        elif wanted == "bold":
            found = [s for s in found if s.bold]
        elif wanted == "regular":
            found = [s for s in found if not s.italic and not s.bold]

        offset = self.offset(args)
        lines = [f"{_printed(page, offset)} - {len(found)} run(s)"
                 + (f", {wanted} only" if wanted else "")]
        for span in found:
            style = "italic" if span.italic else "bold" if span.bold else "regular"
            lines.append(f"  [{style:<7} {span.font} {span.size}] {span.text.strip()}")
        return result(ctx, "\n".join(lines), runs=len(found),
                      italic=sum(1 for s in found if s.italic))


class DumpTool(_PdfTool):
    name = "pdf_dump"
    description = ("Write the document out as one text file per page, so the extraction can be "
                   "grepped later and re-read without the PDF. Returns the directory and what it "
                   "wrote; skips pages already written unless overwrite is set.")
    parameters = {"type": "object", "properties": {
        "path": {"type": "string"},
        "out_dir": {"type": "string",
                    "description": "directory to write into; default <pdf name>.pages next to the PDF"},
        "overwrite": {"type": "boolean", "description": "rewrite pages that already exist"}},
        "required": ["path"]}

    def run(self, args, ctx):
        path = self.path(args, ctx)
        raw_out = args.get("out_dir")
        out_dir = resolve_path(ctx, raw_out) if raw_out else path.with_suffix(".pages")
        out_dir.mkdir(parents=True, exist_ok=True)

        written, skipped = 0, 0
        for page in extract.iter_pages(path):
            target = out_dir / f"page-{page.number:04d}.txt"
            if target.exists() and not args.get("overwrite"):
                skipped += 1
                continue
            target.write_text(page.text, encoding="utf-8")
            written += 1
        digest = extract.file_digest(path)
        (out_dir / "SOURCE.txt").write_text(
            f"source: {path}\nmd5: {digest}\npages: {written + skipped}\n", encoding="utf-8")
        return result(ctx, f"{out_dir}\n  wrote   {written} page file(s)\n"
                           f"  skipped {skipped} already present\n  md5     {digest}\n"
                           f"SOURCE.txt records the edition this dump came from.",
                      out_dir=str(out_dir), written=written, skipped=skipped, md5=digest)


def register(api):
    config = api.plugin_config()
    for tool_class in (InfoTool, PagesTool, FindTool, SpansTool, DumpTool):
        api.register_tool(tool_class(config))
    api.register_system_prompt_section("doc-pdf", lambda: PROMPT_NOTE)
