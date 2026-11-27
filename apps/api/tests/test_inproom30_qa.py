"""QA probes for INPROOM-30 (INPROOM-210): a historical citation keeps its own revision's metadata.
Committed from QA's tests/qa_inproom303132.py; the git case is in test_inproom30_history_qa.py."""

import pytest

from tests import test_confluence_sources_integration as conf
from tests.test_provenance_integration import cite, resolve

env, selected = conf.env, conf.selected


@pytest.mark.integration
def test_old_page_citation_preserves_modified_metadata(selected):
    e = selected
    citation = cite("confluence", e.sid, "101")
    original = resolve(e, citation)["results"][0]["record"]
    e.conf.pages[101].add_version("<h1>Architecture</h1><p>Changed</p>", by="Grace")
    assert e.sync().status_code == 200
    old = resolve(e, citation)["results"][0]["record"]
    assert old["freshness"]["status"] == "superseded"
    assert old["modified"] == original["modified"]
