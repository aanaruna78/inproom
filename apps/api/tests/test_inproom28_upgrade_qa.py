"""QA re-test probe for INPROOM-28 at 695eb27 (INPROOM-28-retest-695eb27.zip: qa_inproom28_upgrade.py),
committed as a regression: the parse@1 -> parse@2 rebuild described in the runbook leaves no stale failure.
The PENDING case is a strict xfail naming INPROOM-172 until fixed.
"""

from dataclasses import replace

import pytest

from app.content import producers
from tests import test_confluence_sources_integration as conf
from tests.test_document_api_integration import document, drain, failures
from tests.test_documents_integration import upload

env = conf.env


@pytest.mark.integration
@pytest.mark.parametrize(
    "old_status",
    [
        "READY",
        pytest.param(
            "PENDING", marks=pytest.mark.xfail(strict=True, reason="INPROOM-172: stale parse@1 failure")
        ),
    ],
)
def test_old_parser_revision_rebuild_has_no_stale_failure(env, monkeypatch, old_status):
    e = env
    current = producers.PRODUCERS["parse"]
    monkeypatch.setitem(producers.PRODUCERS, "parse", replace(current, version=1))
    result = upload(e, "upgrade.md", b"# Guide\n\n## Setup\n\nValid content")
    assert result.status_code == 201, result.text
    doc_id = result.json()["document_id"]
    if old_status == "READY":
        drain(e)
    monkeypatch.setitem(producers.PRODUCERS, "parse", current)
    response = e.client.post(
        f"/v1/projects/{e.pid}/content/invalidate",
        json={
            "kind": "upload",
            "source_id": e.pid,
            "external_id": doc_id,
            "producer": "parse",
            "reason": "Upgrade existing content to parse@2",
        },
        headers=e.h(),
    )
    assert response.status_code == 202, response.text
    drain(e)
    parsed = document(e, "upload", e.pid, doc_id)
    assert parsed.status_code == 200, parsed.text
    assert parsed.json()["producer_version"] == 2
    listed = failures(e)
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"] == [], listed.text
