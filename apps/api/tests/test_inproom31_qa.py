"""QA probe for INPROOM-30/31: pausing, pausing again and restoring a source keeps citations private and the
feed gap-free. Committed from QA's tests/qa_inproom303132.py (it passes)."""

import pytest

from tests import test_confluence_sources_integration as conf
from tests.test_document_api_integration import drain
from tests.test_knowledge_feed_integration import feed
from tests.test_provenance_integration import cite, resolve

env, selected = conf.env, conf.selected


@pytest.mark.integration
def test_policy_transition_preserves_citation_privacy_and_feed_order(selected):
    e = selected
    drain(e)
    citations = [cite("confluence", e.sid, "101", anchor="architecture"), cite("confluence", e.sid, "102")]
    assert resolve(e, *citations)["counts"]["resolved"] == 2
    start = feed(e)["head"]
    base = f"/v1/projects/{e.pid}/sources/confluence/{e.sid}"
    for endpoint in ("exclude", "exclude", "include"):
        assert e.client.post(f"{base}/{endpoint}", headers=e.h()).status_code == 200
        results = resolve(e, *citations)
        if endpoint == "exclude":
            assert results["counts"]["withheld"] == 2
            assert all(x["record"] is None for x in results["results"])
            assert "Architecture" not in str(results) and "Runbooks" not in str(results)
        else:
            assert results["counts"]["resolved"] == 2
    first = feed(e, start, limit=1)
    second = feed(e, first["next"], limit=1)
    assert [first["items"][0]["seq"], second["items"][0]["seq"]] == [start + 1, start + 2]
    assert second["next"] is None
    assert [first["items"][0]["change"], second["items"][0]["change"]] == ["invalidated", "restored"]
