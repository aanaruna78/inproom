"""Derivative producers (INPROOM-159 / story INPROOM-27).

A producer turns one revision's original bytes into one derived artefact. It is identified by name@version;
bumping the version means "rebuild everything": invalidation creates PENDING rows for the new version.
Producers are pure (bytes in, bytes out) so a rebuild of the same revision is byte-identical.

text@1 is the built-in proof of the contract: plain text from Confluence storage XHTML and from canonical Jira
records. Real parsers (PDF, DOCX, sections, tables) arrive as further producers in E03-S02; chunks and
embeddings in E04.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser

CONFLUENCE = "application/vnd.atlassian.confluence.storage+xml"
JIRA = "application/vnd.inproom.jira-issue+json"


class ProducerError(Exception):
    """The input cannot be turned into this derivative (permanent for this revision)."""

    code = "PRODUCER_FAILED"


@dataclass(frozen=True)
class Producer:
    name: str
    version: int
    kind: str
    media_types: tuple[str, ...]  # input media types (parameters such as charset are ignored)
    output_media_type: str
    build: Callable[[bytes, str], bytes]

    def accepts(self, media_type: str) -> bool:
        return media_type.split(";", 1)[0].strip() in self.media_types


_BLOCKS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "table"}
_CELLS = {"td", "th"}
_SKIP = {"ac:parameter", "script", "style"}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip += 1
        elif tag in _BLOCKS:
            self.parts.append("\n")
        elif tag in _CELLS:
            self.parts.append(" ")

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def _tidy(text: str) -> str:
    """One line per block, whitespace collapsed, no empty lines: stable across markup-only edits."""
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line) + "\n"


def _text(data: bytes, media_type: str) -> bytes:
    try:
        source = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProducerError("input is not UTF-8 text") from exc
    base = media_type.split(";", 1)[0].strip()
    if base == JIRA:
        from app.modules.jira_issues import adf_text

        try:
            record = json.loads(source)
        except ValueError as exc:
            raise ProducerError("the Jira record is not valid JSON") from exc
        fields = record.get("fields") or {}
        parts = [f"{record.get('key')}: {fields.get('summary') or ''}"]
        for name in ("description", "environment"):
            value = adf_text(fields.get(name))
            if value:
                parts.append(value)
        return _tidy("\n\n".join(parts)).encode()
    parser = _Text()
    parser.feed(source)
    parser.close()
    return _tidy("".join(parser.parts)).encode()


def _parse(data: bytes, media_type: str) -> bytes:
    from app.content.parsing import parse  # late: parsing.model -> producers

    return parse(data, media_type)


PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TEXT = Producer("text", 1, "text", (CONFLUENCE, JIRA), "text/plain; charset=utf-8", _text)
# INPROOM-164: anchored sections, tables and references for every supported format.
# Version 2 (INPROOM-168..171, before release): heading-path anchors, unique per document; headings and table
# cells count against the text limit; heading-only documents parse; nested tables keep their text. Outputs of
# version 1 are rebuilt by invalidating producer "parse".
PARSE = Producer(
    "parse",
    2,
    "document",
    (PDF, DOCX, "text/markdown", "text/html", "text/plain", CONFLUENCE, JIRA),
    "application/vnd.inproom.document+json",
    _parse,
)
PRODUCERS: dict[str, Producer] = {PARSE.name: PARSE, TEXT.name: TEXT}


def for_media_type(media_type: str) -> list[Producer]:
    return [p for p in PRODUCERS.values() if p.accepts(media_type)]
