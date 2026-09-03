"""PyMuPDF, behind one module.

Every ``import pymupdf`` in this plugin is here. That is deliberate: ``doc-pdf`` is the only
plugin in the repository with a third-party dependency, so the failure mode when it is missing
has to be one clear message rather than an ImportError from wherever the model happened to call
first. It also keeps the licence surface visible - a reader asking "what does the AGPL component
touch?" reads this file and nothing else.

What the rest of the plugin needs from a PDF, and nothing more:

* text with page boundaries intact, because a citation without a page is not a citation;
* the font of each run of text, because NIST prints its guidance in italic and its template
  content in regular type, and a generator that cannot tell them apart will publish NIST's
  advice as if the organisation had written it;
* the page count and a hash, because an audit that does not pin the edition it read is not
  reproducible.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

INSTALL_HINT = ("doc-pdf needs PyMuPDF: pip install 'pymupdf>=1.24'. It is not a picoagent "
                "core dependency - the core is standard library only - and it is licensed "
                "AGPL-3.0 or commercially by Artifex, so it is installed deliberately.")

# PyMuPDF span flag bits, confirmed against 1.28.2 on SP 800-34 Rev. 1: an italic run
# (TimesNewRomanPS-ItalicMT) reports flags 6, a regular run (TimesNewRomanPSMT) reports 4,
# and a bold run (TimesNewRomanPS-BoldMT) reports 20. Bit 4 is "serifed" and is set on all
# three, which is why the test is against the specific bit and not against the whole value.
_ITALIC, _BOLD = 0b10, 0b10000


class PdfError(Exception):
    """Anything the caller should see as a tool error rather than a traceback."""


def _pymupdf():
    try:
        import pymupdf  # noqa: PLC0415 - deliberately lazy; see the module docstring
    except ImportError as exc:                      # pragma: no cover - environment-dependent
        raise PdfError(INSTALL_HINT) from exc
    return pymupdf


@dataclass(frozen=True)
class Span:
    """One run of text that shares a font. ``page`` is 1-based, as a reader counts."""
    page: int
    text: str
    font: str
    size: float
    flags: int

    @property
    def italic(self) -> bool:
        return bool(self.flags & _ITALIC) or "italic" in self.font.lower()

    @property
    def bold(self) -> bool:
        return bool(self.flags & _BOLD) or "bold" in self.font.lower()


@dataclass(frozen=True)
class Page:
    """One page's text. ``number`` is the PDF page, 1-based."""
    number: int
    text: str


def file_digest(path: Path) -> str:
    """md5 of the file, for pinning an edition. Not a security claim - an identity one."""
    digest = hashlib.md5()                          # noqa: S324 - identifies an edition
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _open(path: Path):
    pymupdf = _pymupdf()
    if not path.exists():
        raise PdfError(f"no such file: {path}")
    try:
        return pymupdf.open(path)
    except Exception as exc:                        # pymupdf raises several unrelated types
        raise PdfError(f"cannot open {path} as a PDF: {exc}") from exc


def info(path: Path) -> dict:
    """Page count, hash and document metadata - what an access log has to record."""
    with _open(path) as document:
        meta = {k: v for k, v in (document.metadata or {}).items() if v}
        return {"path": str(path), "pages": document.page_count, "md5": file_digest(path),
                "encrypted": bool(document.is_encrypted), "metadata": meta}


def pages(path: Path, first: int = 1, last: int | None = None) -> list[Page]:
    """Text per page, boundaries intact. ``first``/``last`` are 1-based and inclusive."""
    with _open(path) as document:
        count = document.page_count
        if first < 1:
            raise PdfError(f"first page is 1, not {first}")
        stop = count if last is None else min(last, count)
        if first > count:
            raise PdfError(f"{path.name} has {count} pages; asked to start at {first}")
        return [Page(number=n + 1, text=document[n].get_text()) for n in range(first - 1, stop)]


def spans(path: Path, page_number: int) -> list[Span]:
    """Every run of text on one page, with its font - the italic/regular distinction."""
    with _open(path) as document:
        if not 1 <= page_number <= document.page_count:
            raise PdfError(f"page {page_number} is outside 1..{document.page_count}")
        page = document[page_number - 1]
        out: list[Span] = []
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", ()):
                for span in line["spans"]:
                    if span["text"].strip():
                        out.append(Span(page=page_number, text=span["text"], font=span["font"],
                                        size=round(span["size"], 2), flags=span["flags"]))
        return out


def iter_pages(path: Path) -> Iterator[Page]:
    """Streaming form of :func:`pages`, for dumping a large document without holding it all."""
    with _open(path) as document:
        for n in range(document.page_count):
            yield Page(number=n + 1, text=document[n].get_text())
