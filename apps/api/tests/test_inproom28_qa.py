"""Independent QA probes for INPROOM-28 at b4c7b8d (INPROOM-28-qa-evidence.zip), committed as regressions.

QA asked for these to join the normal regression collection, hence the test_ name. Unfixed cases are strict
xfails naming their defect:
- INPROOM-168: unique anchors, derived from the heading path;
- INPROOM-169: table cells (and headings) count against the text limit;
- INPROOM-170: heading-only documents are not empty;
- INPROOM-171: nested HTML tables keep every cell's text.
"""

import pytest

from app.content.parsing import ParseError, model, parse_document
from app.content.parsing import formats as f
from tests import test_confluence_sources_integration as conf
from tests.test_content_attachments_integration import att
from tests.test_document_api_integration import document, drain
from tests.test_documents_integration import upload

env = conf.env
selected = conf.selected


def test_anchors_do_not_collide_with_numbered_headings():
    doc = parse_document(b"# A\n\nfirst\n\n# A\n\nsecond\n\n# A-2\n\nthird", f.MARKDOWN)
    anchors = [s["anchor"] for s in doc["sections"]]
    assert len(anchors) == len(set(anchors)), anchors


def test_heading_path_anchors_survive_inserting_another_branch():
    original = b"# Alpha\n\n## Setup\n\nalpha instructions"
    inserted = b"# Beta\n\n## Setup\n\nbeta instructions\n\n" + original

    def alpha_anchor(data):
        return next(
            s["anchor"]
            for s in parse_document(data, f.MARKDOWN)["sections"]
            if s["path"] == ["Alpha", "Setup"]
        )

    assert alpha_anchor(original) == alpha_anchor(inserted)


@pytest.mark.xfail(strict=True, reason="INPROOM-170: heading-only documents are EMPTY_DOCUMENT")
@pytest.mark.parametrize(
    ("data", "media"),
    [(b"# A useful title", f.MARKDOWN), (b"<h1>A useful title</h1>", f.HTML)],
)
def test_heading_only_documents_are_not_empty(data, media):
    doc = parse_document(data, media)
    assert doc["sections"][0]["heading"] == "A useful title"


@pytest.mark.parametrize("media", [f.HTML, f.MARKDOWN])
def test_table_text_is_subject_to_the_real_text_limit(media):
    cell = "x" * (model.MAX_TEXT_CHARS + 1)
    data = (
        f"<table><tr><td>{cell}</td></tr></table>" if media == f.HTML else f"| Header |\n|---|\n| {cell} |\n"
    ).encode()
    with pytest.raises(ParseError) as caught:
        parse_document(data, media)
    assert caught.value.code == "PARSE_LIMIT_EXCEEDED"


@pytest.mark.xfail(strict=True, reason="INPROOM-171: nested tables lose the enclosing cell text")
def test_nested_html_table_keeps_every_cell_text():
    data = (
        b"<table><tr><td>Before<table><tr><td>Inner</td></tr></table>After</td>"
        b"<td>Sibling</td></tr></table>"
    )
    doc = parse_document(data, f.HTML)
    cells = " ".join(c for s in doc["sections"] for t in s["tables"] for r in t for c in r)
    assert all(word in cells for word in ("Before", "Inner", "After", "Sibling")), cells


@pytest.mark.xfail(strict=True, reason="INPROOM-170: heading-only upload fails to parse")
@pytest.mark.integration
def test_heading_only_upload_can_be_read_as_a_document(env):
    e = env
    response = upload(e, "title.md", b"# A useful title")
    assert response.status_code == 201, response.text
    drain(e)
    response = document(e, "upload", e.pid, response.json()["document_id"])
    assert response.status_code == 200, response.text


@pytest.mark.integration
@pytest.mark.parametrize("status", ["restricted", "excluded", "deleted", "paused"])
def test_attachment_document_is_withheld_with_its_page_or_source(selected, status):
    e = selected
    e.conf.add_attachment(102, "qaAttachment", "qa.txt", b"QA attachment content", "text/plain")
    assert e.sync().status_code == 200
    drain(e)
    assert document(e, "confluence", e.sid, "att:qaAttachment").status_code == 200
    if status == "restricted":
        e.conf.pages[102].groups = ["bots"]
    elif status == "excluded":
        e.conf.pages[102].labels = []
    elif status == "deleted":
        e.conf.pages[102].status = "trashed"
    else:
        response = e.client.post(f"/v1/connections/{e.cid}/revoke", headers=e.h())
        assert response.status_code == 200, response.text
    if status != "paused":
        assert e.sync().status_code == 200
    for response in (
        document(e, "confluence", e.sid, "att:qaAttachment"),
        att(e, "qaAttachment", "/revisions/1/raw"),
    ):
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "CONTENT_WITHHELD"
