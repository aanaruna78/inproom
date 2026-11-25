"""Document parsing into anchored sections (INPROOM-164 / story INPROOM-28).

``parse(data, media_type)`` returns the canonical JSON bytes of the parsed document, or raises ParseError with
one of model.CODES. It is the build function of the ``parse@1`` derivative producer.
"""

import json

from app.content.parsing.formats import PARSERS
from app.content.parsing.model import CODES, ParseError

__all__ = ["CODES", "MEDIA_TYPES", "ParseError", "parse", "parse_document"]
MEDIA_TYPES = tuple(PARSERS)


def parse_document(data: bytes, media_type: str) -> dict:
    base = media_type.split(";", 1)[0].strip().lower()
    parser = PARSERS.get(base)
    if parser is None:
        raise ParseError("UNSUPPORTED_FORMAT", f"no parser for {base}")
    try:
        return parser(data)
    except ParseError:
        raise
    except RecursionError as exc:
        raise ParseError("PARSE_LIMIT_EXCEEDED", "the document nests too deeply") from exc
    except MemoryError as exc:
        raise ParseError("PARSE_LIMIT_EXCEEDED", "the document needs too much memory") from exc
    except Exception as exc:  # noqa: BLE001 - untrusted input must never crash the derive job
        raise ParseError("PARSE_MALFORMED", f"unreadable document ({type(exc).__name__})") from exc


def parse(data: bytes, media_type: str) -> bytes:
    """Canonical (sorted, UTF-8) JSON: the same revision always parses to the same bytes."""
    doc = parse_document(data, media_type)
    return json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
