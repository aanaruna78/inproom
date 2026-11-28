"""INPROOM-224 (story INPROOM-35 AC 1, 2, 5): a snapshot of a real project lists sources, decisions, facts and
approved links; every cited line resolves through provenance; the same knowledge gives the same bytes; secrets
never appear."""

import pytest

from app.content import memory
from app.db import tenant_session
from tests import test_confluence_sources_integration as conf
from tests import test_jira_sources_integration as jira
from tests.sync_helpers import run_sync
from tests.test_document_api_integration import drain
from tests.test_documents_integration import upload
from tests.test_provenance_integration import resolve

pytestmark = pytest.mark.integration
env, selected = conf.env, conf.selected
jira_env = jira.env

ADR = b"# ADR-3 Queues for ingestion\n\n## Decision\n\nWe ingest through a durable queue. It retries."


def snapshot(e):
    with tenant_session(e.tenant) as conn:
        return memory.generate(conn, e.pid)


def resolves(e, lines):
    cited = [x["citation"] for x in lines if x["citation"]]
    assert cited
    body = [{k: c[k] for k in ("kind", "source_id", "external_id", "seq")} for c in cited]
    out = resolve(e, *body)
    assert out["counts"]["resolved"] == len(cited), out["counts"]


def test_a_snapshot_of_pages_documents_and_decisions(selected):
    e = selected
    e.conf.add_page(
        150,
        "ENG",
        "ADR-3 Queues",
        "<h1>Decision</h1><p>We ingest through a queue. It retries.</p>",
        parent=100,
        labels=["approved"],
    )
    upload(e, "adr-4.md", ADR)
    e.conf.add_page(151, "ENG", "Ops password=hunter22", "<p>Steps.</p>", parent=100, labels=["approved"])
    assert e.sync().status_code == 200
    drain(e)
    s = snapshot(e)
    md = s.markdown
    assert "- confluence ENG: trust trusted, sync" in md and "- upload documents: trust reference" in md
    assert "- **Approved** ADR-3 Queues: We ingest through a queue. [confluence:150@1]" in md
    assert "- **Proposed** adr-4.md: We ingest through a durable queue." in md  # uploads are reference
    assert "- Page: Architecture [confluence:101@1]" in md and "- Page: Runbooks [confluence:102@1]" in md
    assert "hunter22" not in md and s.redactions == {"ASSIGNED_SECRET": 1}
    assert "- Page: Ops password=[REDACTED:ASSIGNED_SECRET] [confluence:151@1]" in md
    assert "items from reference or untrusted sources are not asserted here" in md
    assert "Leads only" not in md  # restricted at the source: never visible, never in memory
    resolves(e, s.lines)
    assert s.counts["decisions"] == 2 and s.feed_head > 0

    again = snapshot(e)
    assert again.markdown == md and again.sha256 == s.sha256  # same knowledge, same bytes
    assert e.sync().status_code == 200  # a sync that changes nothing: sync times move, the body does not
    drain(e)
    assert snapshot(e).sha256 == s.sha256
    e.conf.pages[102].add_version("<p>Restart the api, then the worker.</p>")
    assert e.sync().status_code == 200
    drain(e)
    changed = snapshot(e)
    assert changed.sha256 != s.sha256 and "[confluence:102@2]" in changed.markdown


def test_jira_facts_and_approved_links(jira_env):
    e = jira_env
    e.jira.projects["ABC"].issues[1]["fields"]["components"] = [{"name": "Billing"}]
    e.cid = e.connect().json()["id"]
    e.sid = e.select().json()["id"]
    run_sync({"jira": e.jira.transport()}, e.tenant)
    upload(e, "notes.md", b"# Notes\n\nSee ABC-2.")
    drain(e)
    s = snapshot(e)
    md = s.markdown
    assert "- ABC-1: Invoicing (" in md
    assert (
        "  - ABC-2: Create invoice (story; In Progress; component Billing; parent ABC-1) [jira:ABC-2@1]" in md
    )
    nested = next(x for x in s.lines if "ABC-2: Create invoice" in x["line"])
    assert len(nested["depends_on"]) == 2  # the line names its parent: it depends on both items
    assert "- jira:ABC-2 **blocks** jira:ABC-3 (validated) [jira:ABC-2@1]" in md
    assert "1 unconfirmed inferred links are not listed" in md  # notes.md mentions ABC-2: inferred only
    resolves(e, s.lines)
