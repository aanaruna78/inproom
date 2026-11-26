"""INPROOM-203 (story INPROOM-32 AC 1): source files chunk along their symbols; anchors resolve through
provenance to the same symbol."""

import pytest
from sqlalchemy import text

from app.db import tenant_session
from tests import test_code_search_integration as code
from tests.test_provenance_integration import cite, resolve

pytestmark = pytest.mark.integration
env, connected, repo = code.env, code.connected, code.repo


def test_code_files_chunk_by_symbol_and_cite_it(repo):
    g = repo
    rid = g.q("SELECT id FROM project_repositories WHERE tenant_id=:t AND provider_repo_id='1'")
    with tenant_session(g.tenant) as conn:
        found = conn.execute(
            text(
                "SELECT anchor, line_start, line_end, context FROM knowledge_chunks "
                "WHERE external_id='main:src/billing.py' ORDER BY ordinal"
            )
        ).all()
    assert found and all(r.line_start and r.line_end >= r.line_start for r in found)
    anchored = [r for r in found if r.anchor]
    assert anchored
    for r in anchored:
        [result] = resolve(g, cite("git", rid, "main:src/billing.py", anchor=r.anchor))["results"]
        assert result["error"] is None, result
        passage = result["record"]["passage"]
        assert passage["line_start"] <= r.line_start and r.line_end <= passage["line_end"]
