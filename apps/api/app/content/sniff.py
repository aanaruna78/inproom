"""Media type detection from the bytes (INPROOM-165/166): uploads and Confluence attachments alike.

PDF and DOCX by their signatures; Markdown, HTML and text by file extension, and only if the bytes are UTF-8
text. What a client or provider *claims* is never trusted.
"""

import io
import zipfile

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TEXT_TYPES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".txt": "text/plain",
    ".text": "text/plain",
    "": "text/plain",
}
SUPPORTED = "PDF, DOCX, Markdown (.md), HTML (.html) or plain text (.txt)"
DOCUMENT_EXTENSIONS = (".pdf", ".docx", *(e for e in TEXT_TYPES if e))


class Unsupported(ValueError):
    """Not a document type INPROOM parses; the message says what it was."""


def detect(data: bytes, filename: str) -> str:
    if data.startswith(b"%PDF-"):
        return PDF
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if "word/document.xml" in z.namelist():
                    return DOCX
        except zipfile.BadZipFile:
            pass
        raise Unsupported("a zip archive that is not a Word document")
    ext = "." + filename.rsplit(".", 1)[1].lower() if "." in filename else ""
    if ext not in TEXT_TYPES:
        raise Unsupported(f"files of type {ext}")
    if b"\x00" in data:
        raise Unsupported("binary content")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise Unsupported("text that is not UTF-8") from exc
    return TEXT_TYPES[ext]


def looks_parseable(filename: str) -> bool:
    """Cheap pre-check before downloading: does the name suggest a document type we parse?"""
    name = filename.lower()
    return name.endswith(DOCUMENT_EXTENSIONS)
