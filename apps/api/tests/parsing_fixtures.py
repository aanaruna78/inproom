"""Documents built in the tests (INPROOM-164): no binary fixtures in the repository."""

import io
import zipfile

from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, StreamObject


def _escape(text: str) -> bytes:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("cp1252")


def pdf(pages: list[str | None], *, title: str | None = None, password: str | None = None) -> bytes:
    """One page per entry: its text (Helvetica, Latin-1 range), or None for a page without a text layer."""
    w = PdfWriter()
    font = w._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            }
        )
    )
    for text in pages:
        page = w.add_blank_page(612, 792)
        if text is None:
            continue
        lines = b" T* ".join(b"(" + _escape(line) + b") Tj" for line in text.split("\n"))
        stream = StreamObject()
        stream.set_data(b"BT /F1 12 Tf 14 TL 72 720 Td " + lines + b" ET")
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        page[NameObject("/Contents")] = w._add_object(stream)
    if title:
        w.add_metadata({"/Title": title})
    if password is not None:
        w.encrypt(user_password=password, owner_password="owner")
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def docx(build) -> bytes:
    """build(document) fills a python-docx Document."""
    from docx import Document

    doc = Document()
    build(doc)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def zip_bomb(size: int) -> bytes:
    """A 'DOCX' whose document.xml inflates to `size` bytes of zeros."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", b"\0" * size)
    return out.getvalue()
