"""OOXML with the standard library and nothing else.

A ``.docx`` and a ``.xlsx`` are both a zip of XML parts, so ``zipfile`` and
``xml.etree.ElementTree`` are the whole toolchain. That is the point: picoagent's core is
standard library only, and keeping document ingestion there too means an air-gapped or
dependency-audited site can read the Word plan and the control workbook it was sent without
installing anything. ``doc-pdf`` needs a package because PDF is a page-description format with
no text model; Office documents have one.

Three things here are not the obvious implementation, and each is a bug avoided:

* **The body is walked in order.** A ``w:body`` interleaves paragraphs and tables, and a table's
  cells contain paragraphs of their own. Iterating every ``w:p`` in the tree reads table text a
  second time and out of sequence, so section boundaries land in the wrong place.
* **Sheets are resolved through the relationship part.** ``xl/workbook.xml`` names sheets and
  gives each an ``r:id``; the file it lives in comes from ``xl/_rels/workbook.xml.rels``. Sorting
  ``xl/worksheets/*.xml`` usually agrees and is not guaranteed to, and when it disagrees every
  cell reference in a report names the wrong sheet.
* **The used range is computed, not read.** ``<dimension>`` is optional - the FedRAMP Rev. 5
  baseline workbook omits it - so the extent comes from the cell references actually present.
"""
from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"

_CELL_REF = re.compile(r"^([A-Z]+)([0-9]+)$")


class OoxmlError(Exception):
    """Anything the caller should see as a tool error rather than a traceback."""


@dataclass(frozen=True)
class Block:
    """One paragraph or one table from a document body, in the order it is printed.

    ``level`` is the heading level (1 for ``Heading1``) or 0 for body text. ``italic`` is true
    when every run carrying text in the block is italic, which is how Word templates mark the
    instructions the author is meant to delete rather than content of the document.
    """
    index: int
    kind: str                 # "paragraph" | "table"
    text: str
    style: str = ""
    level: int = 0
    italic: bool = False
    rows: list[list[str]] = field(default_factory=list)


@dataclass(frozen=True)
class Cell:
    sheet: str
    ref: str                  # "B14"
    value: str

    @property
    def column(self) -> str:
        return _CELL_REF.match(self.ref).group(1)

    @property
    def row(self) -> int:
        return int(_CELL_REF.match(self.ref).group(2))


def file_digest(path: Path) -> str:
    """md5 of the file, for pinning an edition. Identity, not a security claim."""
    digest = hashlib.md5()                          # noqa: S324 - identifies an edition
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _zip(path: Path) -> zipfile.ZipFile:
    if not path.exists():
        raise OoxmlError(f"no such file: {path}")
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise OoxmlError(f"{path.name} is not an Office Open XML file (not a zip): {exc}") from exc


def kind(path: Path) -> str:
    """``"docx"`` or ``"xlsx"``, decided by what the archive holds rather than by extension."""
    with _zip(path) as archive:
        names = set(archive.namelist())
    if "word/document.xml" in names:
        return "docx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    if "ppt/presentation.xml" in names:
        raise OoxmlError(f"{path.name} is a PowerPoint file; doc-ooxml reads .docx and .xlsx")
    raise OoxmlError(f"{path.name} is a zip but holds neither word/document.xml nor xl/workbook.xml")


def _parse(archive: zipfile.ZipFile, part: str):
    try:
        return ET.fromstring(archive.read(part))
    except KeyError as exc:
        raise OoxmlError(f"missing part {part}") from exc
    except ET.ParseError as exc:
        raise OoxmlError(f"{part} is not well-formed XML: {exc}") from exc


# --------------------------------------------------------------------------- word

def _runs(element):
    """Every run under ``element`` that carries text, with its italic state."""
    for run in element.iter(W + "r"):
        text = "".join(node.text or "" for node in run.iter(W + "t"))
        if text.strip():
            properties = run.find(W + "rPr")
            yield text, properties is not None and properties.find(W + "i") is not None


def _paragraph_text(paragraph) -> str:
    return "".join(node.text or "" for node in paragraph.iter(W + "t"))


def _style_of(paragraph) -> str:
    properties = paragraph.find(W + "pPr")
    style = properties.find(W + "pStyle") if properties is not None else None
    return style.get(W + "val", "") if style is not None else ""


def _level_of(style: str) -> int:
    match = re.fullmatch(r"Heading(\d+)", style or "")
    return int(match.group(1)) if match else 0


def docx_blocks(path: Path) -> list[Block]:
    """Paragraphs and tables of the document body, in printed order."""
    with _zip(path) as archive:
        body = _parse(archive, "word/document.xml").find(W + "body")
    if body is None:
        raise OoxmlError(f"{path.name} has no document body")

    blocks: list[Block] = []
    for child in body:
        if child.tag == W + "p":
            text = _paragraph_text(child)
            if not text.strip():
                continue
            styled = list(_runs(child))
            style = _style_of(child)
            blocks.append(Block(index=len(blocks), kind="paragraph", text=text, style=style,
                                level=_level_of(style),
                                italic=bool(styled) and all(is_italic for _, is_italic in styled)))
        elif child.tag == W + "tbl":
            rows = [[_paragraph_text(cell).strip() for cell in row.findall(W + "tc")]
                    for row in child.findall(W + "tr")]
            rows = [row for row in rows if any(cell for cell in row)]
            if not rows:
                continue
            text = "\n".join(" | ".join(row) for row in rows)
            blocks.append(Block(index=len(blocks), kind="table", text=text, rows=rows))
    return blocks


def heading_path(blocks: list[Block], index: int) -> str:
    """The chain of headings in force at ``index``, as ``1 Introduction > 1.2 Scope``."""
    chain: dict[int, str] = {}
    for block in blocks[:index + 1]:
        if block.level:
            chain[block.level] = block.text.strip()
            for deeper in [lvl for lvl in chain if lvl > block.level]:
                del chain[deeper]
    return " > ".join(chain[level] for level in sorted(chain))


# --------------------------------------------------------------------------- excel

def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = _parse(archive, "xl/sharedStrings.xml")
    # A shared string may be split into formatting runs; every w:t under the item belongs to it.
    return ["".join(node.text or "" for node in item.iter(S + "t")) for item in root.iter(S + "si")]


def _sheet_parts(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    """``[(sheet name, part path)]`` resolved through the relationship part, in workbook order."""
    workbook = _parse(archive, "xl/workbook.xml")
    relationships = _parse(archive, "xl/_rels/workbook.xml.rels")
    targets = {rel.get("Id"): rel.get("Target") for rel in relationships.iter(PKG + "Relationship")}
    out = []
    for sheet in workbook.iter(S + "sheet"):
        target = targets.get(sheet.get(R + "id"))
        if not target:
            continue
        part = target[1:] if target.startswith("/") else f"xl/{target.lstrip('/')}"
        out.append((sheet.get("name", "?"), part.replace("xl/xl/", "xl/")))
    return out


def xlsx_sheets(path: Path) -> list[str]:
    with _zip(path) as archive:
        return [name for name, _ in _sheet_parts(archive)]


def xlsx_cells(path: Path, sheet: str | None = None) -> list[Cell]:
    """Non-empty cells, in row-major order. ``sheet=None`` reads every sheet."""
    with _zip(path) as archive:
        strings = _shared_strings(archive)
        parts = _sheet_parts(archive)
        known = [name for name, _ in parts]
        if sheet is not None and sheet not in known:
            raise OoxmlError(f"no sheet named {sheet!r}; sheets are {', '.join(known)}")
        out: list[Cell] = []
        for name, part in parts:
            if sheet is not None and name != sheet:
                continue
            for cell in _parse(archive, part).iter(S + "c"):
                value = _cell_value(cell, strings)
                if value.strip():
                    out.append(Cell(sheet=name, ref=cell.get("r", "?"), value=value))
        return out


def _cell_value(cell, strings: list[str]) -> str:
    cell_type = cell.get("t") or "n"
    if cell_type == "s":
        node = cell.find(S + "v")
        if node is None or node.text is None:
            return ""
        try:
            return strings[int(node.text)]
        except (ValueError, IndexError):
            return ""
    if cell_type == "inlineStr":
        item = cell.find(S + "is")
        return "".join(n.text or "" for n in item.iter(S + "t")) if item is not None else ""
    node = cell.find(S + "v")
    return node.text or "" if node is not None else ""


def used_range(cells: list[Cell]) -> str:
    """Extent from the references actually present, because ``<dimension>`` is optional."""
    if not cells:
        return "empty"
    columns = sorted({c.column for c in cells}, key=lambda c: (len(c), c))
    rows = [c.row for c in cells]
    return f"{columns[0]}{min(rows)}:{columns[-1]}{max(rows)}"
