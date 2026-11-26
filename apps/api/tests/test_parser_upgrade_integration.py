"""INPROOM-172: upgrade queued parsers without stale failures or duplicate work."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select

from app.content import producers
from app.content.parsing import ParseError
from app.db_schema import derivatives, source_objects
from tests import parsing_fixtures as fx
from tests import test_confluence_sources_integration as conf
from tests.test_document_api_integration import document, drain, failures
from tests.test_documents_integration import upload

pytestmark = pytest.mark.integration
env = conf.env


def old_parser_failure(data, media_type):
    raise ParseError("PARSE_MALFORMED", "a failure in the previous parser")


@pytest.mark.parametrize("old_status", ["READY", "PENDING", "FAILED"])
def test_upgrading_a_parser_retires_old_work_without_false_failures(env, monkeypatch, old_status):
    e = env
    current = producers.PRODUCERS["parse"]
    old = replace(current, version=1)
    if old_status == "FAILED":
        old = replace(old, build=old_parser_failure)
    monkeypatch.setitem(producers.PRODUCERS, "parse", old)
    result = upload(e, "upgrade.md", b"# Guide\n\n## Setup\n\nValid content")
    assert result.status_code == 201, result.text
    doc_id = result.json()["document_id"]
    if old_status != "PENDING":
        drain(e)
    events_before = e.events("content.parse.failed")
    monkeypatch.setitem(producers.PRODUCERS, "parse", current)
    payload = {
        "kind": "upload",
        "source_id": e.pid,
        "external_id": doc_id,
        "producer": "parse",
        "reason": "Upgrade existing content to parse@2",
    }
    endpoint = f"/v1/projects/{e.pid}/content/invalidate"
    rebuilt = e.client.post(endpoint, json=payload, headers=e.h())
    assert rebuilt.status_code == 202, rebuilt.text
    assert (rebuilt.json()["invalidated"], rebuilt.json()["queued"], rebuilt.json()["pending"]) == (1, 1, 1)
    again = e.client.post(endpoint, json=payload, headers=e.h())
    assert again.status_code == 202, again.text
    assert (again.json()["invalidated"], again.json()["queued"], again.json()["pending"]) == (0, 0, 1)
    drain(e)
    parsed = document(e, "upload", e.pid, doc_id)
    assert parsed.status_code == 200, parsed.text
    assert parsed.json()["producer_version"] == current.version
    assert failures(e).json()["items"] == []
    assert e.events("content.parse.failed") == events_before
    history = e.client.get(
        f"/v1/projects/{e.pid}/content/upload/{e.pid}/{doc_id}/revisions/1/derivatives", headers=e.h()
    )
    parse_history = [d for d in history.json()["items"] if d["producer"] == "parse"]  # chunks: INPROOM-203
    assert [(d["producer_version"], d["status"]) for d in parse_history] == [
        (1, "INVALIDATED"),
        (current.version, "READY"),
    ]


def test_obsolete_failures_are_not_actionable_but_current_failures_still_are(env, owner_engine):
    e = env
    valid = upload(e, "valid.md", b"# Guide\n\nValid content").json()["document_id"]
    scanned = upload(e, "scan.pdf", fx.pdf([None])).json()["document_id"]
    drain(e)
    with owner_engine.begin() as conn:
        revision = conn.execute(
            select(source_objects.c.current_revision_id).where(
                source_objects.c.tenant_id == e.tenant,
                source_objects.c.project_id == e.pid,
                source_objects.c.external_id == valid,
            )
        ).scalar_one()
        # Historical pollution left by an old worker before the upgrade fix was deployed.
        conn.execute(
            insert(derivatives).values(
                tenant_id=e.tenant,
                project_id=e.pid,
                revision_id=revision,
                kind="document",
                producer="parse",
                producer_version=1,
                status="FAILED",
                created_at=datetime.now(UTC) - timedelta(days=1),
                error={"code": "UNKNOWN_PRODUCER", "message": "parse@1"},
            )
        )
    listed = failures(e)
    assert listed.status_code == 200, listed.text
    assert [(x["external_id"], x["code"]) for x in listed.json()["items"]] == [(scanned, "NO_TEXT_LAYER")]
    assert document(e, "upload", e.pid, valid).status_code == 200
