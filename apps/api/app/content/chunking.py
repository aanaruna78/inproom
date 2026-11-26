"""Citable chunks for retrieval: the `chunks@1` producer (INPROOM-203 / story INPROOM-32).

Pure and deterministic (bytes in, bytes out), so a rebuild is byte-identical and the producer contract
(versioning, invalidation, retention) applies unchanged:
- documents, Confluence pages and Jira issues chunk along their `parse@2` sections. Each chunk keeps its
  section anchor (a citation resolves through provenance) and heading path, has about TARGET_TOKENS tokens,
  and overlaps the previous chunk of the same section by about 15%. A table stays whole when it fits;
  otherwise it is split by rows, with the header row repeated;
- source files chunk along their `symbols@1` definitions: a definition that fits is one chunk (anchored by
  its qualified name); a larger class is split into its methods; the remaining lines (imports, module code)
  become line windows anchored to the file.
Each chunk's `context` (title and heading path, or file and symbol) is embedded with its text; `sha256`
covers exactly what is embedded, so identical chunks are embedded once.
"""

import hashlib
import json
from typing import Any

from app.content.parsing.model import ParseError

SCHEMA = 1
TARGET_TOKENS = 400
MAX_TOKENS = 512  # hard ceiling; embedding providers accept far more, so nothing is ever truncated
OVERLAP = 0.15
CHARS_PER_TOKEN = 4  # estimate used for budgeting (exact tokenization is provider-specific)
WINDOW_LINES = 60


def tokens(text: str) -> int:
    return max(1, -(-len(text) // CHARS_PER_TOKEN))


def _budget_chars(limit: int = TARGET_TOKENS) -> int:
    return limit * CHARS_PER_TOKEN


def _sentences(text: str, size: int) -> list[str]:
    """Split an over-long paragraph into sentence units, each at most size chars (an over-long sentence is
    cut at word boundaries, an unbroken token such as a URL or hash at size)."""
    units: list[str] = []
    for sentence in (x.strip() for x in text.replace(". ", ".\n").split("\n")):
        current = ""
        for word in sentence.split(" "):
            while len(word) > size:
                if current:
                    units.append(current)
                    current = ""
                units.append(word[:size])
                word = word[size:]
            if current and len(current) + 1 + len(word) > size:
                units.append(current)
                current = ""
            current = f"{current} {word}" if current else word
        if current:
            units.append(current)
    return [u for u in units if u]


def _pack(paragraphs: list[str], size: int) -> list[str]:
    """Greedy packing into ~size-char chunks; each chunk after the first repeats the tail of the previous
    one (about OVERLAP of size, whole paragraphs or sentences only)."""
    units: list[str] = []
    for p in paragraphs:
        units.extend(_sentences(p, size) if len(p) > size else [p])
    chunks: list[list[str]] = []
    current: list[str] = []
    length = 0
    for unit in units:
        if current and length + len(unit) + 1 > size:
            chunks.append(current)
            tail: list[str] = []
            tail_len = 0
            for prev in reversed(current):
                if tail_len + len(prev) > size * OVERLAP:
                    break
                tail.insert(0, prev)
                tail_len += len(prev) + 1
            current, length = list(tail), tail_len
        current.append(unit)
        length += len(unit) + 1
    if current:
        chunks.append(current)
    return ["\n".join(c) for c in chunks]


def _table_text(rows: list[list[str]]) -> list[str]:
    return [" | ".join(cell.strip() for cell in row) for row in rows]


def _table_chunks(rows: list[list[str]], size: int) -> list[str]:
    lines = _table_text(rows)
    whole = "\n".join(lines)
    if len(whole) <= size or len(lines) <= 1:
        return [whole] if whole.strip() else []
    header, body = lines[0], lines[1:]
    out: list[str] = []
    current = [header]
    for line in body:
        if len(current) > 1 and len("\n".join([*current, line])) > size:
            out.append("\n".join(current))
            current = [header]
        current.append(line)
    if len(current) > 1:
        out.append("\n".join(current))
    return out


def _chunk(context: str, text: str, **extra: Any) -> dict[str, Any]:
    embedded = f"{context}\n\n{text}" if context else text
    return {
        "context": context,
        "text": text,
        "tokens": tokens(embedded),
        "sha256": hashlib.sha256(embedded.encode()).hexdigest(),
        **extra,
    }


def document_chunks(doc: dict[str, Any]) -> list[dict[str, Any]]:
    size = _budget_chars()
    title = (doc.get("title") or "").strip()
    out: list[dict[str, Any]] = []
    for section in doc.get("sections") or []:
        path = [p for p in section.get("path") or [] if p]
        context = " > ".join(([title] if title and path[:1] != [title] else []) + path)
        where = {"anchor": section["anchor"], "path": path, "page": section.get("page")}
        paragraphs = [p.strip() for p in (section.get("text") or "").split("\n") if p.strip()]
        for text in _pack(paragraphs, size) if paragraphs else []:
            out.append(_chunk(context, text, **where))
        for table in section.get("tables") or []:
            for text in _table_chunks(table, size):
                out.append(_chunk(context, text, **where, table=True))
    return out


def code_chunks(source: str, symbols: dict[str, Any], path: str) -> list[dict[str, Any]]:
    lines = source.splitlines()
    size = _budget_chars(MAX_TOKENS)
    definitions = symbols.get("definitions") or []
    covered = [False] * (len(lines) + 1)
    out: list[dict[str, Any]] = []

    def span(d: dict[str, Any]) -> str:
        return "\n".join(lines[d["line_start"] - 1 : d["line_end"]])

    def emit(d: dict[str, Any]) -> None:
        name = f"{d['container']}.{d['name']}" if d.get("container") else d["name"]
        context = " ".join(x for x in (path, d["kind"], name) if x)
        text = span(d)
        if len(text) > size:  # too long even as one definition: line windows inside it, same anchor
            for start in range(d["line_start"], d["line_end"] + 1, WINDOW_LINES):
                end = min(d["line_end"], start + WINDOW_LINES - 1)
                piece = "\n".join(lines[start - 1 : end])
                out.append(_chunk(context, piece, anchor=name, line_start=start, line_end=end))
        else:
            out.append(
                _chunk(
                    context,
                    text,
                    anchor=name,
                    line_start=d["line_start"],
                    line_end=d["line_end"],
                )
            )
        for n in range(d["line_start"], d["line_end"] + 1):
            if n < len(covered):
                covered[n] = True

    top = [d for d in definitions if not d.get("container")]
    for d in top:
        members = [m for m in definitions if m.get("container") == d["name"]]
        if len(span(d)) <= size or not members:
            emit(d)
        else:
            for m in members:
                emit(m)
            # the class line(s) before its first member, as their own small chunk
            head_end = min(m["line_start"] for m in members) - 1
            if head_end >= d["line_start"]:
                emit(d | {"line_end": head_end})
    # Module-level code (imports, constants, scripts) outside every definition: windows of WINDOW_LINES
    # over each run of uncovered lines, trimmed of blank lines at both ends.
    groups: list[list[int]] = [[]]
    for n in range(1, len(lines) + 1):
        if covered[n]:
            groups.append([])
        else:
            groups[-1].append(n)
    for group in groups:
        for start in range(0, len(group), WINDOW_LINES):
            window = [n for n in group[start : start + WINDOW_LINES]]
            while window and not lines[window[0] - 1].strip():
                window.pop(0)
            while window and not lines[window[-1] - 1].strip():
                window.pop()
            if window:
                text = "\n".join(lines[window[0] - 1 : window[-1]])
                out.append(_chunk(path, text, anchor=None, line_start=window[0], line_end=window[-1]))
    return sorted(out, key=lambda c: (c["line_start"], c["line_end"]))


def chunk_document(data: bytes, media_type: str, path: str = "") -> dict[str, Any]:
    from app.content.code.symbols import MEDIA_LANGUAGE, symbols_document
    from app.content.parsing import parse

    base = media_type.split(";", 1)[0].strip()
    if base in MEDIA_LANGUAGE:
        try:
            source = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ParseError("PARSE_MALFORMED", "not UTF-8 text") from exc
        chunks = code_chunks(source, symbols_document(data, media_type), path)
        method = "symbols"
    else:
        chunks = document_chunks(json.loads(parse(data, media_type)))
        method = "sections"
    for i, c in enumerate(chunks):
        c["ordinal"] = i
    return {
        "schema": SCHEMA,
        "method": method,
        "target_tokens": TARGET_TOKENS,
        "chunks": chunks,
        "stats": {"chunks": len(chunks), "tokens": sum(c["tokens"] for c in chunks)},
    }


def _encode(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def is_code(media_type: str) -> bool:
    from app.content.code.symbols import MEDIA_LANGUAGE

    return media_type.split(";", 1)[0].strip() in MEDIA_LANGUAGE


def from_parsed(parsed: bytes) -> bytes:
    """Chunks of a document from its READY parse@2 output: the derive job's path for documents, so a document
    is parsed once and a parse failure is reported once (by parse), not again by chunks."""
    chunks = document_chunks(json.loads(parsed))
    for i, c in enumerate(chunks):
        c["ordinal"] = i
    return _encode(
        {
            "schema": SCHEMA,
            "method": "sections",
            "target_tokens": TARGET_TOKENS,
            "chunks": chunks,
            "stats": {"chunks": len(chunks), "tokens": sum(c["tokens"] for c in chunks)},
        }
    )


def build(data: bytes, media_type: str) -> bytes:
    """From the original bytes: source files always; documents only when no parse output exists."""
    return _encode(chunk_document(data, media_type))
