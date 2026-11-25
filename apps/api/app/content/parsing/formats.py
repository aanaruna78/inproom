"""One parser per format, all producing the shared model (INPROOM-164 / story INPROOM-28).

| Format              | Sections               | Anchors          | Tables | References                |
|---------------------|------------------------|------------------|--------|---------------------------|
| PDF                 | one per page           | page-N           | -      | -                         |
| DOCX                | heading styles         | heading slug     | yes    | -                         |
| Markdown            | headings               | heading slug     | GFM    | links                     |
| HTML / Confluence   | h1-h6                  | heading slug     | yes    | ri:attachment, links      |
| Plain text          | paragraph groups       | para-N           | -      | -                         |
| Jira issue record   | summary, description.. | field-<name>     | -      | -                         |

Nothing in a document is fetched or executed. DOCX XML is parsed by python-docx's parser, which does not
resolve entities (no XXE); the zip is size- and ratio-checked before it is opened (no zip bombs).
"""

import io
import json
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from app.content.parsing import model
from app.content.parsing.model import Builder, ParseError

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MARKDOWN = "text/markdown"
HTML = "text/html"
TEXT = "text/plain"
CONFLUENCE = "application/vnd.atlassian.confluence.storage+xml"
JIRA = "application/vnd.inproom.jira-issue+json"
PARA_GROUP_CHARS = 4_000


def _utf8(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ParseError("PARSE_MALFORMED", f"not UTF-8 text (byte {exc.start})") from exc


# ---------------------------------------------------------------- PDF


def pdf(data: bytes) -> dict[str, Any]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):  # an owner-password-only PDF still opens
            raise ParseError("PDF_ENCRYPTED", "the PDF needs a password to open")
        count = len(reader.pages)
    except ParseError:
        raise
    except Exception as exc:  # noqa: BLE001 - any library failure on untrusted input is "malformed"
        if "decrypt" in str(exc).lower() or "encrypt" in str(exc).lower():
            raise ParseError("PDF_ENCRYPTED", "the PDF is encrypted") from exc
        raise ParseError("PARSE_MALFORMED", f"unreadable PDF ({type(exc).__name__})") from exc
    if count == 0:
        raise ParseError("EMPTY_DOCUMENT", "the PDF has no pages")
    if count > model.MAX_PAGES:
        raise ParseError("PARSE_LIMIT_EXCEEDED", f"{count} pages (limit {model.MAX_PAGES})")
    title = None
    try:
        title = (reader.metadata or {}).get("/Title") or None
    except Exception:  # noqa: BLE001 - metadata is optional
        title = None
    b = Builder("pdf", str(title) if title else None)
    empty = []
    for number in range(1, count + 1):
        try:
            text = reader.pages[number - 1].extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            detail = f"page {number} is unreadable ({type(exc).__name__})"
            raise ParseError("PARSE_MALFORMED", detail) from exc
        b.section(f"page-{number}", f"Page {number}", page=number)
        if text.strip():
            b.text(text)
        else:
            empty.append(number)
    if len(empty) == count:
        raise ParseError("NO_TEXT_LAYER", "no page has a text layer (scanned?); OCR is not available")
    if empty:
        b.warn("PAGES_WITHOUT_TEXT", pages=empty[:100], count=len(empty))
    return b.result()


# ---------------------------------------------------------------- DOCX


def docx(data: bytes) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
            names = {i.filename for i in infos}
    except zipfile.BadZipFile as exc:
        raise ParseError("PARSE_MALFORMED", "not a valid DOCX (zip) file") from exc
    unzipped = sum(i.file_size for i in infos)
    if unzipped > model.MAX_UNZIPPED_BYTES or unzipped > model.MAX_ZIP_RATIO * max(len(data), 1):
        raise ParseError("PARSE_LIMIT_EXCEEDED", f"expands to {unzipped} bytes (possible zip bomb)")
    if "word/document.xml" not in names:
        raise ParseError("PARSE_MALFORMED", "a zip file, but not a Word document")
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        doc = Document(io.BytesIO(data))
        title = doc.core_properties.title or None
    except Exception as exc:  # noqa: BLE001
        raise ParseError("PARSE_MALFORMED", f"unreadable DOCX ({type(exc).__name__})") from exc
    b = Builder("docx", title)
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            para = Paragraph(child, doc)
            try:
                style = para.style.name if para.style is not None else ""
            except Exception:  # noqa: BLE001 - a dangling style reference
                style = ""
            text = para.text
            if style == "Title":
                b.title = b.title or text.strip() or None
                b.heading(0, text)  # above "Heading 1", so it heads the whole path
            elif style.startswith("Heading ") and style[8:].isdigit():
                b.heading(int(style[8:]), text)
            else:
                b.text(text)
        elif child.tag == qn("w:tbl"):
            table = Table(child, doc)
            b.table([[cell.text for cell in row.cells] for row in table.rows])
    return b.result()


# ---------------------------------------------------------------- Markdown


def markdown(data: bytes) -> dict[str, Any]:
    from markdown_it import MarkdownIt

    source = _utf8(data)
    tokens = MarkdownIt("commonmark", {"html": False}).enable("table").parse(source)
    b = Builder("markdown")

    def inline(token) -> str:
        out = []
        for child in token.children or []:
            if child.type in ("text", "code_inline"):
                out.append(child.content)
            elif child.type in ("softbreak", "hardbreak"):
                out.append(" ")
            elif child.type == "link_open" and str(child.attrGet("href") or "").startswith(
                ("http://", "https://")
            ):
                b.links.append(str(child.attrGet("href")))
        return "".join(out)

    rows: list[list[str]] | None = None
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.type == "heading_open":
            b.heading(int(t.tag[1]), inline(tokens[i + 1]))
            i += 2
        elif t.type == "table_open":
            rows = []
        elif t.type == "tr_open" and rows is not None:
            rows.append([])
        elif t.type in ("th_open", "td_open") and rows is not None:
            rows[-1].append(inline(tokens[i + 1]))
            i += 1
        elif t.type == "table_close" and rows is not None:
            b.table(rows)
            rows = None
        elif t.type == "inline" and rows is None:
            b.text(inline(t))
        elif t.type in ("fence", "code_block"):
            b.text(t.content)
        i += 1
    b.title = next((s.heading for s in b.sections if s.level == 1), None)
    return b.result()


# ---------------------------------------------------------------- HTML and Confluence storage format

_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_BLOCKS = {"p", "div", "li", "pre", "blockquote", "br", "ul", "ol", "section", "article", "dd", "dt"}
_SKIP = {"script", "style", "ac:parameter", "head", "title", "noscript"}


@dataclass
class _Table:
    """One open <table>: its rows, the cell being read, and text inside the table but outside any cell."""

    rows: list[list[str]] = field(default_factory=list)
    cell: list[str] | None = None
    loose: list[str] = field(default_factory=list)

    def close_cell(self) -> None:
        if self.cell is not None:
            if not self.rows:  # a cell without <tr>
                self.rows.append([])
            self.rows[-1].append("".join(self.cell))
            self.cell = None

    def text(self) -> str:
        return " ".join(" ".join(c for c in row if c.strip()) for row in self.rows if row)


class _Html(HTMLParser):
    def __init__(self, builder: Builder):
        super().__init__(convert_charrefs=True)
        self.b = builder
        self.buf: list[str] = []
        self.heading: tuple[int, list[str]] | None = None
        self.tables: list[_Table] = []  # open tables, innermost last
        self.skip = 0

    def flush(self) -> None:
        if self.buf:
            self.b.text(" ".join("".join(self.buf).split()))
            self.buf = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in _SKIP:
            self.skip += 1
        elif tag in _HEADINGS and not self.tables:
            self.flush()
            self.heading = (_HEADINGS[tag], [])
        elif tag == "table":
            self.flush()
            self.tables.append(_Table())
        elif tag == "tr" and self.tables:
            self.tables[-1].close_cell()
            self.tables[-1].rows.append([])
        elif tag in ("td", "th") and self.tables:
            self.tables[-1].close_cell()
            self.tables[-1].cell = []
        elif tag == "ri:attachment" and attrs.get("ri:filename"):
            self.b.attachment(attrs["ri:filename"])
        elif tag == "a" and str(attrs.get("href") or "").startswith(("http://", "https://")):
            self.b.links.append(attrs["href"])
        elif tag in _BLOCKS and self.tables and self.tables[-1].cell is not None:
            self.tables[-1].cell.append(" ")  # keep words in separate blocks of a cell apart
        elif tag in _BLOCKS and not self.tables and self.heading is None:
            self.flush()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in _HEADINGS and self.heading is not None:
            level, parts = self.heading
            self.heading = None
            self.b.heading(level, "".join(parts))
        elif tag in ("td", "th", "tr") and self.tables:
            self.tables[-1].close_cell()
        elif tag == "table" and self.tables:
            table = self.tables.pop()
            table.close_cell()
            loose = " ".join("".join(table.loose).split())
            if self.tables:
                # A nested table: its text (and any loose text) goes back into the enclosing cell, in reading
                # order, so "Before <table>Inner</table> After" keeps all three (INPROOM-171).
                outer = self.tables[-1]
                target = outer.cell if outer.cell is not None else outer.loose
                target.append(" " + " ".join(t for t in (table.text(), loose) if t) + " ")
            else:
                self.b.table(table.rows)
                if loose:  # e.g. a <caption>: kept as text after the table, never dropped
                    self.b.text(loose)
        elif tag in _BLOCKS and not self.tables and self.heading is None:
            self.flush()

    def handle_data(self, data):
        if self.skip:
            return
        if self.heading is not None:
            self.heading[1].append(data)
        elif self.tables:
            table = self.tables[-1]
            (table.cell if table.cell is not None else table.loose).append(data)
        else:
            self.buf.append(data)

    def unknown_decl(self, data):
        if data.startswith("CDATA[") and not self.skip:  # Confluence code macros keep their body in CDATA
            self.handle_data(data[6:])

    def close(self):
        super().close()
        while self.tables:  # unclosed tables at the end of the input still count
            self.handle_endtag("table")


def html(data: bytes, fmt: str = "html") -> dict[str, Any]:
    source = _utf8(data)
    b = Builder(fmt)
    parser = _Html(b)
    try:
        parser.feed(source)
        parser.close()
    except ParseError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ParseError("PARSE_MALFORMED", f"unreadable markup ({type(exc).__name__})") from exc
    parser.flush()
    b.title = next((s.heading for s in b.sections if s.level == 1), None)
    return b.result()


# ---------------------------------------------------------------- plain text and Jira records


def text(data: bytes) -> dict[str, Any]:
    source = _utf8(data).replace("\r\n", "\n")
    paragraphs = [p.strip() for p in source.split("\n\n")]
    b = Builder("text")
    size = 0
    for number, para in enumerate(paragraphs, start=1):
        if not para:
            continue
        if not b.sections or size + len(para) > PARA_GROUP_CHARS:
            b.section(f"para-{number}", None)  # anchored at the group's first paragraph
            size = 0
        b.text(para)
        size += len(para)
    return b.result()


def jira(data: bytes) -> dict[str, Any]:
    from app.modules.jira_issues import adf_text

    try:
        record = json.loads(_utf8(data))
        fields = record["fields"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ParseError("PARSE_MALFORMED", "not a Jira issue record") from exc
    b = Builder("jira", f"{record.get('key')}: {fields.get('summary') or ''}".strip(": "))
    b.section("field-summary", "Summary")
    b.text(fields.get("summary") or "")
    for name in ("description", "environment"):
        value = adf_text(fields.get(name))
        if value:
            b.section(f"field-{name}", name.capitalize())
            b.text(value)
    for name in sorted(k for k in fields if k.startswith("customfield_")):
        value = fields[name]
        if isinstance(value, str) or (isinstance(value, dict) and value.get("type") == "doc"):
            value = adf_text(value)
            if value:
                b.section(f"field-{name}", name)
                b.text(value)
    return b.result()


PARSERS = {
    PDF: pdf,
    DOCX: docx,
    MARKDOWN: markdown,
    HTML: html,
    CONFLUENCE: lambda data: html(data, "confluence"),
    TEXT: text,
    JIRA: jira,
}
