"""INPROOM-203 (story INPROOM-32 AC 1): ingested content becomes citable chunks - rows of the READY chunks@1
derivative, whose citations resolve through provenance, replaced when the item changes."""

import pytest

from tests import test_confluence_sources_integration as conf
from tests.test_document_api_integration import drain
from tests.test_provenance_integration import cite, resolve

pytestmark = pytest.mark.integration
env, selected = conf.env, conf.selected


def rows(e, external_id):
    from sqlalchemy import text

    from app.db import tenant_session

    with tenant_session(e.tenant) as conn:
        return [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT seq, ordinal, anchor, path, context, text, sha256 FROM knowledge_chunks "
                    "WHERE external_id=:x ORDER BY seq, ordinal"
                ),
                {"x": external_id},
            ).mappings()
        ]


def test_pages_become_citable_chunks_replaced_on_change(selected):
    e = selected
    drain(e)
    first = rows(e, "101")
    assert first and {r["seq"] for r in first} == {1}
    assert all(r["anchor"] and r["text"] and len(r["sha256"]) == 64 for r in first)
    for r in first:  # every chunk's citation resolves to its passage (INPROOM-30)
        [result] = resolve(e, cite("confluence", e.sid, "101", anchor=r["anchor"]))["results"]
        assert result["error"] is None and result["record"]["passage"]["anchor"] == r["anchor"]
    built = e.q(
        "SELECT count(*) FROM derivatives WHERE tenant_id=:t AND producer='chunks' AND status='READY'"
    )
    assert built >= 2  # every stored page

    e.conf.pages[101].add_version("<h1>Architecture</h1><p>A completely new design.</p>")
    assert e.sync().status_code == 200
    drain(e)
    second = rows(e, "101")
    assert {r["seq"] for r in second} == {2}  # the superseded revision's chunks left with its derivative
    assert any("completely new design" in r["text"] for r in second)
