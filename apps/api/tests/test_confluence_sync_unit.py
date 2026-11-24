"""INPROOM-135: catalog sync planning - filters, ACL inheritance, versions, tombstones, truncation."""

from app.connectors.confluence import PageListing, PageMeta
from app.modules.confluence_sync import Filters, plan_sync


def page(pid, version=1, ancestors=(), labels=(), groups=(), title=None):
    return PageMeta(
        id=pid,
        title=title or f"P{pid}",
        space_key="ENG",
        parent_id=ancestors[-1] if ancestors else None,
        ancestors=list(ancestors),
        version=version,
        author="Ada",
        created_at="2026-11-20",
        message=None,
        labels=list(labels),
        restrictions={"users": [], "groups": list(groups)},
        attachments=[],
        content_sha256=f"h{pid}-{version}",
        web_url=None,
    )


def catalog(plan):
    """Existing rows as the database would hold them after applying `plan` to an empty catalog."""
    return {r["page_id"]: dict(r) for r in plan.inserts}


def test_filters_and_inherited_restrictions():
    pages = [
        page("1", labels=["approved"]),
        page("2", ancestors=["1"], labels=["approved"], groups=["leads"]),
        page("3", ancestors=["1", "2"], labels=["approved"]),
        page("4", ancestors=["1"]),
        page("5", labels=["approved"]),
    ]
    plan = plan_sync(
        {}, PageListing(pages), Filters.of({"root_page_ids": ["1"], "required_labels": ["Approved"]})
    )
    rows = {r["page_id"]: r for r in plan.inserts}
    assert sorted(rows) == ["1", "2", "3"]  # 4: no label, 5: outside the tree
    assert (rows["1"]["status"], rows["2"]["status"], rows["3"]["status"]) == (
        "CURRENT",
        "RESTRICTED",
        "RESTRICTED",
    )
    assert rows["3"]["restrictions"]["inherited"] == [
        {"page_id": "2", "title": "P2", "users": [], "groups": ["leads"]}
    ]
    assert plan.counts["added"] == 1 and plan.counts["restricted"] == 2
    assert [e[0] for e in plan.events] == ["source.confluence.page.changed"]
    included = plan_sync(
        {}, PageListing(pages), Filters.of({"required_labels": ["approved"], "include_restricted": True})
    )
    assert {r["status"] for r in included.inserts} == {"CURRENT"}


def test_versions_tombstones_restores_and_noop():
    first = plan_sync({}, PageListing([page("1"), page("2")]), Filters())
    existing = catalog(first)
    assert not plan_sync(existing, PageListing([page("1"), page("2")]), Filters()).changed
    edited = plan_sync(existing, PageListing([page("1", version=2), page("2")]), Filters())
    assert edited.counts["updated"] == 1 and [v["version"] for v in edited.versions] == [2]
    gone = plan_sync(existing, PageListing([page("1")]), Filters())
    assert (
        gone.updates
        == [{"page_id": "2", "status": "DELETED", "status_reason": gone.updates[0]["status_reason"]}]
        and gone.events[0][0] == "source.confluence.page.deleted"
    )
    existing["2"]["status"] = "DELETED"
    back = plan_sync(existing, PageListing([page("1"), page("2")]), Filters())
    assert back.counts["restored"] == 1 and back.versions == []  # same version: history not duplicated
    narrowed = plan_sync(
        catalog(first),
        PageListing([page("1", labels=["approved"]), page("2")]),
        Filters.of({"required_labels": ["approved"]}),
    )
    assert (
        narrowed.counts["excluded"] == 1 and narrowed.events[-1][0] == "source.confluence.page.out_of_scope"
    )


def test_truncated_listing_never_tombstones():
    existing = catalog(plan_sync({}, PageListing([page("1"), page("2")]), Filters()))
    plan = plan_sync(existing, PageListing([page("1")], truncated=True), Filters())
    assert plan.truncated and plan.updates == [] and plan.counts["deleted"] == 0
