"""doc-ooxml: read a Word or Excel document as evidence, with the standard library only.

The organisation's contingency plan is a ``.docx``. The control baseline it is judged against is
an ``.xlsx``. Neither is markdown, and an audit that can only read markdown can only audit
repositories that were written for it - which is to say, not anyone's actual plan.

The contract is deliberately the same as ``doc-pdf``'s, because the discipline is the same: a
quotation is worth nothing unless someone can find it again. ``ooxml_find`` is the tool that
matters; the rest support it. What differs is the address a citation carries. A PDF cites a page.
A Word document cites the heading it sits under, which is more useful, because headings survive
reformatting and page numbers do not. A spreadsheet cites a sheet and a cell.

Two document-specific hazards this plugin exists to handle:

* **Template instructions read as content.** FedRAMP's ISCP template marks the text the author is
  supposed to delete in *italic* - 328 of its 874 runs, opening with "Delete this Template
  Revision History page and all other instructional text from your final version". A reader that
  cannot see italic will quote FedRAMP's instructions back as the organisation's own commitments.
  ``ooxml_outline`` and ``ooxml_read`` mark them, and ``ooxml_find`` says so on every hit.
* **The empty template answered as though it were filled.** A heading with nothing under it but a
  ``<Insert CSO Name>`` placeholder is not a completed section. ``ooxml_outline`` reports the
  content under each heading so a blank one is visible as blank.
"""
from __future__ import annotations

import re
from pathlib import Path

from picoagent.core.tools import PathRefused, resolve_path, truncate
from picoagent.core.types import ToolResult

import ooxml

PROMPT_NOTE = """\
doc-ooxml is loaded, for .docx and .xlsx. Before quoting one, confirm the sentence with
ooxml_find; a quote that does not resolve to a heading or a cell is not evidence. Text the tools
mark [italic] in a Word template is usually the template's own instruction to the author, not
content of the document - do not quote it as the organisation's words. Placeholders of the form
<Insert ...> mean the section is unfilled, whatever else is around them."""

_WHITESPACE = re.compile(r"\s+")
_PLACEHOLDER = re.compile(r"<\s*[Ii]nsert[^>]*>|\{[a-z ]+\}")


def normalise(text: str) -> str:
    """Collapse whitespace runs to one space, on both sides of every comparison."""
    return _WHITESPACE.sub(" ", text).strip()


def result(ctx, text: str, is_error: bool = False, **details) -> ToolResult:
    body, cut = truncate(text, ctx.config["tool_output_max_bytes"], ctx.config["tool_output_max_lines"])
    return ToolResult(ctx.tool_call_id, body + ("\n[truncated]" if cut else ""), is_error=is_error,
                      details=details)


class _OoxmlTool:
    """Base: resolves the path argument, turns expected failures into error results."""

    def __init__(self, config: dict):
        self.config = config

    async def execute(self, args: dict, ctx) -> ToolResult:
        try:
            return self.run(args, ctx)
        except ooxml.OoxmlError as exc:
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


class InfoTool(_OoxmlTool):
    name = "ooxml_info"
    description = ("What an Office file is and how big: docx or xlsx decided by content not "
                   "extension, md5 to pin the edition, and the shape - headings and blocks for a "
                   "document, sheets and used ranges for a workbook.")
    parameters = {"type": "object", "properties": {
        "path": {"type": "string"}}, "required": ["path"]}

    def run(self, args, ctx):
        path = self.path(args, ctx)
        which = ooxml.kind(path)
        digest = ooxml.file_digest(path)
        lines = [str(path), f"  type  {which}", f"  md5   {digest}"]
        if which == "docx":
            blocks = ooxml.docx_blocks(path)
            headings = [b for b in blocks if b.level]
            italic = [b for b in blocks if b.italic]
            lines += [f"  blocks     {len(blocks)} "
                      f"({sum(1 for b in blocks if b.kind == 'paragraph')} paragraphs, "
                      f"{sum(1 for b in blocks if b.kind == 'table')} tables)",
                      f"  headings   {len(headings)}",
                      f"  italic     {len(italic)} block(s) entirely italic - likely template "
                      f"instructions, not content"]
            return result(ctx, "\n".join(lines), kind=which, md5=digest, blocks=len(blocks),
                          headings=len(headings), italic_blocks=len(italic))
        sheets = ooxml.xlsx_sheets(path)
        lines.append(f"  sheets     {len(sheets)}")
        for name in sheets:
            cells = ooxml.xlsx_cells(path, name)
            lines.append(f"    {name!r}: {len(cells)} non-empty cell(s), used range "
                         f"{ooxml.used_range(cells)}")
        return result(ctx, "\n".join(lines), kind=which, md5=digest, sheets=sheets)


class OutlineTool(_OoxmlTool):
    name = "ooxml_outline"
    description = ("The heading tree of a .docx, with how much content sits under each heading and "
                   "whether that content is template instruction or unfilled placeholder. This is "
                   "how you tell a completed section from an empty one.")
    parameters = {"type": "object", "properties": {
        "path": {"type": "string"},
        "max_level": {"type": "integer", "description": "deepest heading level to show (default 3)"}},
        "required": ["path"]}

    def run(self, args, ctx):
        path = self.path(args, ctx)
        if ooxml.kind(path) != "docx":
            raise ValueError("ooxml_outline reads .docx; use ooxml_info for a workbook")
        blocks = ooxml.docx_blocks(path)
        deepest = args.get("max_level", 3)
        headings = [b for b in blocks if b.level and b.level <= deepest]
        if not headings:
            return result(ctx, "no headings: this document uses no Heading styles, so it has no "
                               "outline to walk. Use ooxml_find and ooxml_read by block index.",
                          headings=0)
        lines, empty = [], 0
        for position, heading in enumerate(headings):
            start = heading.index + 1
            stop = headings[position + 1].index if position + 1 < len(headings) else len(blocks)
            body = [b for b in blocks[start:stop] if not b.level]
            words = sum(len(b.text.split()) for b in body)
            instruction = sum(1 for b in body if b.italic)
            placeholder = sum(1 for b in body if _PLACEHOLDER.search(b.text))
            notes = []
            if instruction:
                notes.append(f"{instruction} italic (instruction)")
            if placeholder:
                notes.append(f"{placeholder} with placeholders")
            if not body or words == 0:
                notes.append("EMPTY")
                empty += 1
            lines.append(f"{'  ' * (heading.level - 1)}[{heading.index}] H{heading.level} "
                         f"{heading.text.strip()}  - {len(body)} block(s), {words} word(s)"
                         + (f"  [{'; '.join(notes)}]" if notes else ""))
        lines.append(f"\n{len(headings)} heading(s); {empty} with no content under them.")
        return result(ctx, "\n".join(lines), headings=len(headings), empty=empty)


class FindTool(_OoxmlTool):
    name = "ooxml_find"
    description = ("Locate a quotation and report where it lives - the heading path and block index "
                   "in a .docx, the sheet and cell in an .xlsx - comparing with whitespace "
                   "normalised. Run it on every sentence before quoting it; no location means the "
                   "sentence is not in the document.")
    parameters = {"type": "object", "properties": {
        "path": {"type": "string"},
        "quote": {"type": "string", "description": "the sentence, phrase or cell text to confirm"},
        "sheet": {"type": "string", "description": "xlsx only: limit to one sheet"},
        "limit": {"type": "integer", "description": "most hits to report (default 20)"}},
        "required": ["path", "quote"]}

    def run(self, args, ctx):
        quote = args.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            raise ValueError("quote is required")
        needle, path = normalise(quote), self.path(args, ctx)
        limit = args.get("limit", 20)
        which = ooxml.kind(path)

        if which == "docx":
            blocks = ooxml.docx_blocks(path)
            hits = [b for b in blocks if needle in normalise(b.text)]
            if not hits:
                return self._absent(ctx, path, quote)
            lines = [f"FOUND in {len(hits)} block(s) of {path.name}:"]
            for block in hits[:limit]:
                where = ooxml.heading_path(blocks, block.index) or "(before the first heading)"
                mark = "  [italic - template instruction, not content]" if block.italic else ""
                lines.append(f"\n  block {block.index} ({block.kind}) under: {where}{mark}"
                             f"\n    {normalise(block.text)[:400]}")
            return result(ctx, "\n".join(lines), found=True,
                          blocks=[b.index for b in hits],
                          italic=[b.index for b in hits if b.italic])

        cells = ooxml.xlsx_cells(path, args.get("sheet"))
        hits = [c for c in cells if needle in normalise(c.value)]
        if not hits:
            return self._absent(ctx, path, quote)
        lines = [f"FOUND in {len(hits)} cell(s) of {path.name}:"]
        lines += [f"  {c.sheet}!{c.ref}: {normalise(c.value)[:200]}" for c in hits[:limit]]
        return result(ctx, "\n".join(lines), found=True,
                      cells=[f"{c.sheet}!{c.ref}" for c in hits])

    @staticmethod
    def _absent(ctx, path: Path, quote: str) -> ToolResult:
        return result(ctx, f"NOT FOUND in {path.name}: {quote[:120]!r}\n"
                           f"The text is not in this document as extracted. Do not quote it. Check "
                           f"the edition with ooxml_info, and check you have not paraphrased.",
                      found=False, quote=quote)


class ReadTool(_OoxmlTool):
    name = "ooxml_read"
    description = ("Read part of a document: a .docx heading section by its block index (from "
                   "ooxml_outline) or a block range, or a range of rows from one .xlsx sheet. "
                   "Italic blocks are marked, because in a template they are instructions.")
    parameters = {"type": "object", "properties": {
        "path": {"type": "string"},
        "heading": {"type": "integer", "description": "docx: block index of a heading; reads to the next heading of the same or higher level"},
        "first": {"type": "integer", "description": "docx: first block index (default 0)"},
        "last": {"type": "integer", "description": "docx: last block index, inclusive"},
        "sheet": {"type": "string", "description": "xlsx: sheet name (default the first)"},
        "first_row": {"type": "integer", "description": "xlsx: first row (default 1)"},
        "last_row": {"type": "integer", "description": "xlsx: last row, inclusive (default first_row + 40)"}},
        "required": ["path"]}

    def run(self, args, ctx):
        path = self.path(args, ctx)
        if ooxml.kind(path) == "xlsx":
            return self._read_sheet(args, ctx, path)

        blocks = ooxml.docx_blocks(path)
        if not blocks:
            return result(ctx, f"{path.name} has no body content", blocks=0)
        heading = args.get("heading")
        if heading is not None:
            if not 0 <= heading < len(blocks):
                raise ValueError(f"block {heading} is outside 0..{len(blocks) - 1}")
            start = blocks[heading]
            if not start.level:
                raise ValueError(f"block {heading} is not a heading; it is a {start.kind}")
            first = heading
            last = len(blocks) - 1
            for block in blocks[heading + 1:]:
                if block.level and block.level <= start.level:
                    last = block.index - 1
                    break
        else:
            first = args.get("first", 0)
            last = args.get("last", min(first + 40, len(blocks) - 1))
        first, last = max(0, first), min(last, len(blocks) - 1)

        lines = [f"{path.name} blocks {first}-{last} "
                 f"(under: {ooxml.heading_path(blocks, first) or 'document root'})"]
        for block in blocks[first:last + 1]:
            tag = f"H{block.level}" if block.level else ("table" if block.kind == "table" else "")
            mark = " [italic]" if block.italic else ""
            prefix = f"  [{block.index}]{(' ' + tag) if tag else ''}{mark} "
            lines.append(prefix + block.text.strip().replace("\n", "\n" + " " * len(prefix)))
        return result(ctx, "\n".join(lines), first=first, last=last)

    @staticmethod
    def _read_sheet(args, ctx, path: Path) -> ToolResult:
        sheet = args.get("sheet") or ooxml.xlsx_sheets(path)[0]
        cells = ooxml.xlsx_cells(path, sheet)
        first_row = args.get("first_row", 1)
        last_row = args.get("last_row", first_row + 40)
        rows: dict[int, dict[str, str]] = {}
        for cell in cells:
            if first_row <= cell.row <= last_row:
                rows.setdefault(cell.row, {})[cell.column] = cell.value
        if not rows:
            return result(ctx, f"{sheet}: no non-empty cells in rows {first_row}-{last_row} "
                               f"(sheet used range {ooxml.used_range(cells)})", rows=0)
        lines = [f"{path.name} sheet {sheet!r}, rows {first_row}-{last_row}"]
        for number in sorted(rows):
            body = "  ".join(f"{col}={value}" for col, value in sorted(rows[number].items()))
            lines.append(f"  r{number}: {body}")
        return result(ctx, "\n".join(lines), sheet=sheet, rows=len(rows))


class DumpTool(_OoxmlTool):
    name = "ooxml_dump"
    description = ("Write the document out as plain text so it can be grepped later without the "
                   "Office file - one file per .docx, one per sheet for an .xlsx, plus a SOURCE.txt "
                   "recording the path and md5 the dump came from.")
    parameters = {"type": "object", "properties": {
        "path": {"type": "string"},
        "out_dir": {"type": "string", "description": "default <file name>.text next to the source"},
        "overwrite": {"type": "boolean"}}, "required": ["path"]}

    def run(self, args, ctx):
        path = self.path(args, ctx)
        which = ooxml.kind(path)
        raw_out = args.get("out_dir")
        out_dir = resolve_path(ctx, raw_out) if raw_out else path.with_suffix(".text")
        out_dir.mkdir(parents=True, exist_ok=True)

        written, skipped = 0, 0
        for name, body in self._parts(path, which):
            target = out_dir / name
            if target.exists() and not args.get("overwrite"):
                skipped += 1
                continue
            target.write_text(body, encoding="utf-8")
            written += 1
        digest = ooxml.file_digest(path)
        (out_dir / "SOURCE.txt").write_text(
            f"source: {path}\nmd5: {digest}\ntype: {which}\n", encoding="utf-8")
        return result(ctx, f"{out_dir}\n  wrote   {written} file(s)\n  skipped {skipped}\n"
                           f"  md5     {digest}", out_dir=str(out_dir), written=written,
                      skipped=skipped, md5=digest)

    @staticmethod
    def _parts(path: Path, which: str):
        if which == "docx":
            blocks = ooxml.docx_blocks(path)
            yield "document.txt", "\n\n".join(
                f"[{b.index}]{' H' + str(b.level) if b.level else ''}"
                f"{' [italic]' if b.italic else ''} {b.text}" for b in blocks)
            return
        for sheet in ooxml.xlsx_sheets(path):
            cells = ooxml.xlsx_cells(path, sheet)
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", sheet)
            yield f"{safe}.txt", "\n".join(f"{c.ref}\t{c.value}" for c in cells)


def register(api):
    config = api.plugin_config()
    for tool_class in (InfoTool, OutlineTool, FindTool, ReadTool, DumpTool):
        api.register_tool(tool_class(config))
    api.register_system_prompt_section("doc-ooxml", lambda: PROMPT_NOTE)
