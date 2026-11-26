"""INPROOM-213 (story INPROOM-33 AC 4, 5): freshness filters, and an explicit insufficient-evidence verdict
instead of a padded list of weak passages."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from tests import test_knowledge_retrieve_integration as rt
from tests.test_document_api_integration import drain
from tests.test_documents_integration import upload
from tests.test_provenance_freshness_integration import owner

pytestmark = pytest.mark.integration
env, selected, docs, retrieve = rt.env, rt.selected, rt.docs, rt.retrieve


def ids(result):
    return [h["citation"]["external_id"] for h in result["items"]]


def test_supported_hits_or_an_explicit_insufficient_verdict(docs):
    e, errors, _ = docs
    good = retrieve(e, "E1042", mode="hybrid")
    assert good["evidence"] == {
        "status": "sufficient",
        "reason": None,
        "detail": "1 supported result(s)",
        "supported": 1,
    }
    assert ids(good) == [errors] and good["items"][0]["explanation"]["supported"] is True

    weak = retrieve(e, "zebra quantum marmalade", mode="hybrid")  # semantic candidates exist, none relevant
    assert (weak["evidence"]["status"], weak["evidence"]["reason"], weak["items"]) == (
        "insufficient",
        "LOW_RELEVANCE",
        [],
    )
    shown = retrieve(e, "zebra quantum marmalade", mode="hybrid", include_unsupported=True)
    assert shown["items"] and not any(h["explanation"]["supported"] for h in shown["items"])
    assert shown["evidence"]["status"] == "insufficient"  # showing them does not make them evidence

    assert retrieve(e, "zebra quantum marmalade", mode="keyword")["evidence"]["reason"] == "NO_MATCH"
    nothing = retrieve(e, "E1042", source_ids=[str(uuid.uuid4())])
    assert nothing["evidence"]["reason"] == "NO_CONTENT" and nothing["items"] == []
    future = retrieve(e, "E1042", changed_after=(datetime.now(UTC) + timedelta(days=1)).isoformat())
    assert future["evidence"]["reason"] == "ALL_FILTERED" and future["items"] == []


def test_freshness_filters(docs):
    e, errors, deploy = docs
    cutoff = datetime.now(UTC)
    fresh = upload(e, "fresh.md", b"# Fresh\n\nThe ledger lock now has a watchdog.").json()["document_id"]
    drain(e)
    assert set(ids(retrieve(e, "ledger lock", top_k=50))) >= {errors, fresh}
    assert ids(retrieve(e, "ledger lock", changed_after=cutoff.isoformat())) == [fresh]
    assert set(ids(retrieve(e, "ledger lock", max_age_days=1))) >= {errors, fresh}
    for bad in ({"max_age_days": 0}, {"changed_after": "yesterday"}):
        r = e.client.post(
            f"/v1/projects/{e.pid}/knowledge/retrieve", json={"query": "x"} | bad, headers=e.h()
        )
        assert r.status_code == 422, bad

    # a failing source sync makes its items stale: shown with that freshness, or dropped on request
    owner(
        e,
        "INSERT INTO source_sync_state (tenant_id, project_id, source_kind, source_id, last_success_at, "
        "consecutive_failures) SELECT tenant_id, project_id, 'confluence', id, now(), 2 "
        "FROM project_confluence_sources WHERE tenant_id=:t",
    )
    hits = retrieve(e, "restart the api", top_k=50)["items"]
    page = next(h for h in hits if h["citation"]["external_id"] == "102")
    assert (
        page["provenance"]["freshness"] == "stale" and "SOURCE_SYNC_FAILING" in page["provenance"]["reasons"]
    )
    kept = retrieve(e, "restart the api", top_k=50, exclude_stale=True)
    assert "102" not in ids(kept) and deploy in ids(kept)
    assert all(h["provenance"]["freshness"] == "current" for h in kept["items"])
    only_stale = retrieve(e, "restart the api", kinds=["confluence"], exclude_stale=True)
    assert only_stale["evidence"]["reason"] == "ALL_FILTERED"
