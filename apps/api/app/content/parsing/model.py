"""The parsed-document model shared by every format (INPROOM-164 / story INPROOM-28).

A document is a list of sections. Each has a stable anchor, the heading path above it, its text and its
tables, so a citation can always point at a page or a section. Building is bounded (limits below):
input is untrusted.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from app.content.producers import ProducerError

SCHEMA = 1
# Bounds for untrusted input. Exceeding one is PARSE_LIMIT_EXCEEDED, never a partial success.
MAX_PAGES = 2_000
MAX_SECTIONS = 5_000
MAX_TEXT_CHARS = 10_000_000
MAX_UNZIPPED_BYTES = 100 * 1024 * 1024
MAX_ZIP_RATIO = 100
MAX_TABLE_CELLS = 100_000

CODES = (
    "PARSE_MALFORMED",
    "PDF_ENCRYPTED",
    "NO_TEXT_LAYER",
    "EMPTY_DOCUMENT",
    "UNSUPPORTED_FORMAT",
    "PARSE_LIMIT_EXCEEDED",
)


class ParseError(ProducerError):
    """A document that cannot be parsed, with a specific, documented code."""

    def __init__(self, code: str, detail: str):
        assert code in CODES, code
        super().__init__(detail)
        self.code = code


PATH_SEPARATOR = "--"  # between heading slugs in an anchor; slug() collapses any run of "-" to one


def slug(text: str) -> str:
    """URL-fragment-safe anchor from a heading: Unicode letters and digits kept, the rest collapsed to '-'."""
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[^\w]+", "-", text, flags=re.UNICODE).strip("-_")
    text = re.sub(r"-{2,}", "-", text)
    return text[:80].strip("-") or "section"


@dataclass
class Section:
    anchor: str
    heading: str | None
    path: list[str]
    level: int
    page: int | None = None
    parts: list[str] = field(default_factory=list)
    tables: list[list[list[str]]] = field(default_factory=list)

    def as_dict(self, index: int) -> dict[str, Any]:
        return {
            "id": f"s{index}",
            "anchor": self.anchor,
            "heading": self.heading,
            "path": self.path,
            "level": self.level,
            "page": self.page,
            "text": "\n".join(p for p in self.parts if p).strip(),
            "tables": self.tables,
        }


class Builder:
    """Accumulates sections in reading order and enforces the limits."""

    def __init__(self, fmt: str, title: str | None = None):
        self.format = fmt
        self.title = title
        self.sections: list[Section] = []
        self.stack: list[tuple[int, str, str]] = []  # (level, heading, anchor) of the open headings
        self.used: set[str] = set()  # every anchor emitted so far (INPROOM-168: unique per document)
        self.attachments: list[dict[str, Any]] = []
        self.links: list[str] = []
        self.warnings: list[dict[str, Any]] = []
        self.chars = 0
        self.cells = 0

    def _anchor(self, base: str) -> str:
        """`base`, or the first free `base-N`: checked against EVERY anchor already emitted, so a heading that
        is literally "A-2" can never collide with the second "A" (INPROOM-168)."""
        anchor, n = base, 1
        while anchor in self.used:
            n += 1
            anchor = f"{base}-{n}"
        self.used.add(anchor)
        return anchor

    def _new(self, section: Section) -> Section:
        if len(self.sections) >= MAX_SECTIONS:
            raise ParseError("PARSE_LIMIT_EXCEEDED", f"more than {MAX_SECTIONS} sections")
        self.sections.append(section)
        return section

    def heading(self, level: int, text: str, page: int | None = None) -> None:
        text = " ".join(text.split())
        if not text:
            return
        while self.stack and self.stack[-1][0] >= level:
            self.stack.pop()
        # The anchor follows the heading PATH: the parent's (already unique) anchor + "--" + this heading's
        # slug, so "Alpha > Setup" keeps its anchor when another "Setup" is added elsewhere, and children of
        # duplicate headings stay distinct (INPROOM-168). A document title (level 0) is not a prefix.
        parent = next((a for lvl, _, a in reversed(self.stack) if lvl > 0), None)
        base = f"{parent}{PATH_SEPARATOR}{slug(text)}" if parent and level > 0 else slug(text)
        anchor = self._anchor(base)
        self.stack.append((level, text, anchor))
        path = [h for _, h, _ in self.stack]
        self._new(Section(anchor, text, path, level, page))

    def section(self, anchor_base: str, heading: str | None, page: int | None = None) -> None:
        """A section that is not a heading (a PDF page, a paragraph, a Jira field)."""
        self.stack = []
        self._new(Section(self._anchor(anchor_base), heading, [heading] if heading else [], 0, page))

    def _current(self) -> Section:
        if not self.sections:  # content before the first heading
            self._new(Section(self._anchor("preamble"), None, [], 0))
        return self.sections[-1]

    def text(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self.chars += len(text)
        if self.chars > MAX_TEXT_CHARS:
            raise ParseError("PARSE_LIMIT_EXCEEDED", f"more than {MAX_TEXT_CHARS} characters of text")
        self._current().parts.append(text)

    def table(self, rows: list[list[str]]) -> None:
        rows = [[" ".join(c.split()) for c in row] for row in rows if row]
        if not rows:
            return
        self.cells += sum(len(r) for r in rows)
        if self.cells > MAX_TABLE_CELLS:
            raise ParseError("PARSE_LIMIT_EXCEEDED", f"more than {MAX_TABLE_CELLS} table cells")
        self._current().tables.append(rows)

    def attachment(self, filename: str) -> None:
        anchor = self.sections[-1].anchor if self.sections else None
        ref = {"filename": filename, "section": anchor}
        if ref not in self.attachments:
            self.attachments.append(ref)

    def warn(self, code: str, **detail: Any) -> None:
        self.warnings.append({"code": code} | detail)

    def result(self) -> dict[str, Any]:
        sections = [s.as_dict(i + 1) for i, s in enumerate(self.sections)]
        if not any(s["text"] or s["tables"] for s in sections):
            raise ParseError("EMPTY_DOCUMENT", "the document has no text")
        return {
            "schema": SCHEMA,
            "format": self.format,
            "title": self.title,
            "sections": sections,
            "attachments": self.attachments,
            "links": sorted(set(self.links)),
            "warnings": self.warnings,
            "stats": {
                "sections": len(sections),
                "characters": sum(len(s["text"]) for s in sections),
                "tables": sum(len(s["tables"]) for s in sections),
                "pages": max((s["page"] or 0 for s in sections), default=0) or None,
            },
        }
