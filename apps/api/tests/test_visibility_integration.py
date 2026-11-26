"""INPROOM-187 (story INPROOM-31 AC 4): one visibility rule - for every withholding reason, the per-item rule
(access_reason: raw, documents, code files, provenance), the SQL rule (live(), code search, conflicts) and the
served API agree on every item of the project, and agree again once the reason is reversed."""

import pytest
from sqlalchemy import select

from app.content import derivatives
from app.content.visibility import visible
from app.db import tenant_session
from app.db_schema import source_objects, source_revisions
from app.modules.content import access_reason
from tests import test_confluence_sources_integration as conf
from tests.test_document_api_integration import drain
from tests.test_documents_integration import settings, upload

pytestmark = pytest.mark.integration
env, selected = conf.env, conf.selected


def agree(e) -> dict[str, str | None]:
    """{external_id: reason or None}, after asserting every path agrees for every item."""
    with tenant_session(e.tenant) as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                select(source_objects, source_revisions.c.seq)
                .join(source_revisions, source_revisions.c.id == source_objects.c.current_revision_id)
                .where(source_objects.c.project_id == e.pid)
            ).mappings()
        ]
        by_rule = {r["external_id"]: (access_reason(conn, r) or (None,))[0] for r in rows}
        by_sql = set(conn.execute(select(source_objects.c.external_id).where(visible(conn, e.pid))).scalars())
        live = {d["external_id"] for d in derivatives.live(conn, e.pid)}
    assert by_sql == {x for x, why in by_rule.items() if why is None}, by_rule
    assert live <= by_sql  # live() serves only visible items (those with a READY derivative)
    for r in rows:
        raw = e.client.get(
            f"/v1/projects/{e.pid}/content/{r['source_kind']}/{r['source_id']}/{r['external_id']}"
            f"/revisions/{r['seq']}/raw",
            headers=e.h(),
        )
        why = by_rule[r["external_id"]]
        assert raw.status_code == (200 if why is None else 409), (r["external_id"], why, raw.text)
        if why is not None:
            assert raw.json()["reason"] == why
    return by_rule


def test_every_read_path_agrees_for_every_reason(selected):
    e = selected
    doc = upload(e, "notes.md", b"# Notes\n\nShared notes").json()["document_id"]
    drain(e)
    base = f"/v1/projects/{e.pid}/sources/confluence/{e.sid}"
    initial = agree(e)
    assert initial["101"] is None and initial[doc] is None

    e.conf.pages[102].groups = ["bots"]  # RESTRICTED
    e.conf.pages[101].status = "trashed"  # DELETED
    assert e.sync().status_code == 200
    seen = agree(e)
    assert (seen["101"], seen["102"]) == ("DELETED", "RESTRICTED")

    assert e.client.post(f"{base}/exclude", headers=e.h()).status_code == 200  # SOURCE_PAUSED
    assert {why for x, why in agree(e).items() if x != doc} == {"DELETED", "RESTRICTED", "SOURCE_PAUSED"}
    assert e.client.post(f"{base}/include", headers=e.h()).status_code == 200

    policy = e.client.put(
        f"{base}/policy",
        json={"tier": "trusted", "classification": "confidential", "reason": "contains secrets"},
        headers=e.h(**{"If-Match": '"0"'}),
    )
    assert policy.status_code == 200, policy.text
    settings(e, allowed_kinds=["git", "jira", "confluence"])  # uploads disabled
    seen = agree(e)
    assert seen[doc] == "SOURCE_DISABLED"
    assert {why for x, why in seen.items() if x not in (doc, "101", "102")} == {
        "CLASSIFICATION_EXCEEDS_PROJECT"
    }

    settings(e, allowed_kinds=["git", "jira", "confluence", "upload"], max_classification="confidential")
    e.conf.pages[102].groups = []
    e.conf.pages[101].status = "current"
    assert e.sync().status_code == 200
    assert all(why is None for why in agree(e).values())  # every reason reversed: everything is served
