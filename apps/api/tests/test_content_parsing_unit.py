"""INPROOM-164 (story INPROOM-28 AC 1, 2, 4, 5): every format parses into anchored sections, or fails with a
specific code; output is deterministic and bounded."""

import json

import pytest

from app.content import producers
from app.content.parsing import ParseError, model, parse, parse_document
from app.content.parsing import formats as f
from tests import parsing_fixtures as fx


def anchors(doc):
    return [s["anchor"] for s in doc["sections"]]


def code_of(data, media_type):
    with pytest.raises(ParseError) as exc:
        parse_document(data, media_type)
    return exc.value.code


# ---------------------------------------------------------------- formats (AC 1)


def test_pdf_has_one_section_per_page_with_page_anchors():
    data = fx.pdf(["Introduction\nScope of the system", "Design\nTwo services", "Café façade"], title="Spec")
    doc = parse_document(data, f.PDF)
    assert (doc["format"], doc["title"], doc["stats"]["pages"]) == ("pdf", "Spec", 3)
    assert anchors(doc) == ["page-1", "page-2", "page-3"]
    assert [s["page"] for s in doc["sections"]] == [1, 2, 3]
    assert "Two services" in doc["sections"][1]["text"] and "Café façade" in doc["sections"][2]["text"]


def test_pdf_pages_without_text_are_a_warning_not_a_failure():
    doc = parse_document(fx.pdf(["Text page", None]), f.PDF)
    assert doc["warnings"] == [{"code": "PAGES_WITHOUT_TEXT", "pages": [2], "count": 1}]


def test_pdf_with_an_owner_password_only_still_parses():
    assert (
        parse_document(fx.pdf(["Open to read"], password=""), f.PDF)["sections"][0]["text"] == "Open to read"
    )


def test_docx_headings_paragraphs_and_tables():
    def build(d):
        d.add_heading("Requirements", 0)  # style "Title"
        d.add_paragraph("Before the first section.")
        d.add_heading("Login", 1)
        d.add_paragraph("Users sign in with SSO.")
        t = d.add_table(rows=2, cols=2)
        t.cell(0, 0).text, t.cell(0, 1).text = "Field", "Rule"
        t.cell(1, 0).text, t.cell(1, 1).text = "email", "required"
        d.add_heading("Errors", 2)
        d.add_paragraph("Shown inline.")
        d.add_heading("Login", 1)

    doc = parse_document(fx.docx(build), f.DOCX)
    assert doc["title"] == "Requirements"
    # heading-path anchors (INPROOM-168); the document title is not a prefix
    assert anchors(doc) == ["requirements", "login", "login--errors", "login-2"]
    login = doc["sections"][1]
    assert login["text"] == "Users sign in with SSO." and login["tables"] == [
        [["Field", "Rule"], ["email", "required"]]
    ]
    assert doc["sections"][2]["path"] == ["Requirements", "Login", "Errors"]


def test_markdown_headings_tables_code_and_links():
    md = (
        "Intro line.\n\n# Guide\n\nSee [docs](https://example.com/d).\n\n## Setup\n\n"
        "| Step | Cmd |\n|---|---|\n| 1 | `make up` |\n\n```\nmake test\n```\n"
    )
    doc = parse_document(md.encode(), f.MARKDOWN)
    assert anchors(doc) == ["preamble", "guide", "guide--setup"]
    assert doc["sections"][2]["tables"] == [[["Step", "Cmd"], ["1", "make up"]]]
    assert "make test" in doc["sections"][2]["text"] and doc["links"] == ["https://example.com/d"]
    assert doc["title"] == "Guide"


def test_confluence_storage_sections_tables_attachments_and_macros():
    storage = (
        "<h1>Architecture</h1><p>Overview.</p>"
        '<ac:structured-macro ac:name="toc">'
        '<ac:parameter ac:name="maxLevel">2</ac:parameter></ac:structured-macro>'
        "<table><tbody><tr><th>Svc</th><th>Owner</th></tr><tr><td>api</td><td>Ada</td></tr></tbody></table>"
        '<h2>Diagrams</h2><ac:image><ri:attachment ri:filename="flow.pdf"/></ac:image>'
        '<ac:structured-macro ac:name="code"><ac:plain-text-body><![CDATA[print("hi")]]></ac:plain-text-body>'
        "</ac:structured-macro><script>alert(1)</script>"
    )
    doc = parse_document(storage.encode(), f.CONFLUENCE + "; charset=utf-8")
    assert anchors(doc) == ["architecture", "architecture--diagrams"]
    assert doc["sections"][0]["text"] == "Overview." and doc["sections"][0]["tables"] == [
        [["Svc", "Owner"], ["api", "Ada"]]
    ]
    assert doc["attachments"] == [{"filename": "flow.pdf", "section": "architecture--diagrams"}]
    assert (
        'print("hi")' in doc["sections"][1]["text"]
        and "alert" not in json.dumps(doc)
        and "maxLevel" not in json.dumps(doc)
    )


def test_plain_text_groups_paragraphs_under_para_anchors():
    paras = [f"Paragraph {i} " + "x" * 1500 for i in range(1, 6)]
    doc = parse_document("\n\n".join(paras).encode(), f.TEXT)
    assert anchors(doc) == ["para-1", "para-3", "para-5"]  # ~4,000 characters per section


def test_jira_record_sections_per_field():
    record = {
        "id": "1",
        "key": "ABC-1",
        "fields": {
            "summary": "Login",
            "description": {
                "type": "doc",
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": "As a user"}]}],
            },
            "customfield_100": "Given valid credentials, then signed in",
            "customfield_200": 5,
        },
    }
    doc = parse_document(json.dumps(record).encode(), f.JIRA)
    assert doc["title"] == "ABC-1: Login"
    assert anchors(doc) == ["field-summary", "field-description", "field-customfield_100"]


# ---------------------------------------------------------------- Unicode and determinism (AC 4)


def test_unicode_survives_in_text_and_anchors():
    md = "# Überblick\n\nÄußerung ✓\n\n# 設計\n\n日本語のテキスト 🚀\n\n# مرحبا\n\nنص عربي"
    doc = parse_document(md.encode(), f.MARKDOWN)
    assert anchors(doc) == ["überblick", "設計", "مرحبا"]
    assert doc["sections"][1]["text"] == "日本語のテキスト 🚀" and doc["sections"][2]["text"] == "نص عربي"


def test_output_is_canonical_and_deterministic():
    data = fx.docx(lambda d: (d.add_heading("A", 1), d.add_paragraph("b")))
    assert parse(data, f.DOCX) == parse(data, f.DOCX)
    assert parse(data, f.DOCX).decode().startswith('{"attachments":[]')


def test_parse_is_a_registered_producer_for_every_supported_format():
    assert producers.PRODUCERS["parse"].kind == "document"
    for media_type in (f.PDF, f.DOCX, f.MARKDOWN, f.HTML, f.TEXT, f.CONFLUENCE, f.JIRA):
        assert producers.PRODUCERS["parse"] in producers.for_media_type(media_type)


# ---------------------------------------------------------------- failures (AC 2)


@pytest.mark.parametrize(
    ("data", "media_type", "code"),
    [
        (fx.pdf(["secret"], password="pw"), f.PDF, "PDF_ENCRYPTED"),
        (fx.pdf([None, None]), f.PDF, "NO_TEXT_LAYER"),
        (fx.pdf(["truncated"])[:200], f.PDF, "PARSE_MALFORMED"),
        (b"%PDF-1.7 garbage", f.PDF, "PARSE_MALFORMED"),
        (b"PK\x03\x04 not really a zip", f.DOCX, "PARSE_MALFORMED"),
        (
            fx.zip_bomb(10_000),
            f.DOCX,
            "PARSE_MALFORMED",
        ),  # a zip, but no Word document inside... after the size check
        (b"\xff\xfe\x00bad", f.TEXT, "PARSE_MALFORMED"),
        (b"   \n\n  ", f.TEXT, "EMPTY_DOCUMENT"),
        (b"<p> </p>", f.HTML, "EMPTY_DOCUMENT"),
        (b"{}", f.JIRA, "PARSE_MALFORMED"),
        (b"data", "image/png", "UNSUPPORTED_FORMAT"),
    ],
    ids=[
        "encrypted",
        "scanned",
        "truncated",
        "garbage-pdf",
        "bad-zip",
        "not-word",
        "not-utf8",
        "empty-text",
        "empty-html",
        "not-jira",
        "unsupported",
    ],
)
def test_each_failure_has_its_code(data, media_type, code):
    assert code_of(data, media_type) == code


def test_empty_pdf_and_docx_are_empty_documents():
    assert code_of(fx.docx(lambda d: None), f.DOCX) == "EMPTY_DOCUMENT"


# ---------------------------------------------------------------- limits (AC 5)


def test_zip_bombs_are_refused_before_inflating():
    assert code_of(fx.zip_bomb(5_000_000), f.DOCX) == "PARSE_LIMIT_EXCEEDED"  # ratio far above 100:1


def test_page_section_and_text_limits(monkeypatch):
    monkeypatch.setattr(model, "MAX_PAGES", 2)
    assert code_of(fx.pdf(["a", "b", "c"]), f.PDF) == "PARSE_LIMIT_EXCEEDED"
    monkeypatch.setattr(model, "MAX_SECTIONS", 3)
    assert (
        code_of("\n\n".join(f"# H{i}\n\nt" for i in range(5)).encode(), f.MARKDOWN) == "PARSE_LIMIT_EXCEEDED"
    )
    monkeypatch.setattr(model, "MAX_TEXT_CHARS", 10)
    assert code_of(b"x" * 11, f.TEXT) == "PARSE_LIMIT_EXCEEDED"


def test_large_documents_within_limits_parse_completely():
    doc = parse_document(fx.pdf([f"Page {i} body" for i in range(1, 501)]), f.PDF)
    assert doc["stats"]["pages"] == 500 and anchors(doc)[-1] == "page-500"
    md = "\n\n".join(f"## Section {i}\n\nText {i}" for i in range(1, 1001))
    assert parse_document(md.encode(), f.MARKDOWN)["stats"]["sections"] == 1000


def test_unexpected_library_errors_become_malformed(monkeypatch):
    def boom(data):
        raise KeyError("library bug")

    monkeypatch.setitem(f.PARSERS, f.TEXT, boom)
    assert code_of(b"text", f.TEXT) == "PARSE_MALFORMED"


def test_every_parse_failure_code_is_explained():
    from app.sync.errors import CATALOG

    assert set(model.CODES) <= set(CATALOG)


def test_anchors_are_unique_and_follow_the_heading_path():
    """INPROOM-168: every anchor in a document is unique; children inherit their parent's anchor."""
    md = b"Intro.\n\n# Preamble\n\na\n\n# A\n\n## Sub\n\nb\n\n# A\n\n## Sub\n\nc\n\n# A-2\n\n## Sub\n\nd"
    got = anchors(parse_document(md, f.MARKDOWN))
    assert got == ["preamble", "preamble-2", "a", "a--sub", "a-2", "a-2--sub", "a-2-2", "a-2-2--sub"]
    assert len(got) == len(set(got))
    before = parse_document(b"# Alpha\n\n## Setup\n\nx", f.MARKDOWN)
    after = parse_document(b"# Beta\n\n## Setup\n\ny\n\n# Alpha\n\n## Setup\n\nx", f.MARKDOWN)
    assert anchors(before)[1] == "alpha--setup" and "alpha--setup" in anchors(after)
