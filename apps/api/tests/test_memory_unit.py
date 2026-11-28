"""INPROOM-224: the memory renderer is deterministic, cites every entry, states each section's rule and
scrubs secrets before anything is hashed."""

import uuid

from app.content.memory import (
    GENERATOR,
    SECTIONS,
    Entry,
    first_sentence,
    is_decision,
    markdown_of,
    render,
    sha256_of,
)

ITEM = uuid.UUID(int=1)


def cite(label, seq=1):
    return {"kind": "jira", "source_id": "s", "external_id": "1", "seq": seq, "label": label}


def entries():
    return [
        Entry("facts", ("jira", "b"), "- ABC-2: B", cite("jira:ABC-2"), ITEM, ITEM, [ITEM]),
        Entry("facts", ("jira", "a"), "- ABC-1: A", cite("jira:ABC-1"), ITEM, ITEM, [ITEM]),
        Entry("sources", ("jira", "ABC"), "- jira ABC: trust trusted, sync fresh"),
    ]


def test_rendering_is_deterministic_ordered_and_cited():
    lines, redactions = render("Payments", entries(), {"facts": ["1 item not asserted"]})
    again, _ = render("Payments", list(reversed(entries())), {"facts": ["1 item not asserted"]})
    md = markdown_of(lines)
    assert md == markdown_of(again) and sha256_of(md) == sha256_of(markdown_of(again))  # order-independent
    assert md.startswith("# Project memory: Payments\n") and GENERATOR in md
    for _, title, rule in SECTIONS:
        assert f"## {title}" in md and f"_Rule: {rule}_" in md
    assert md.index("ABC-1: A [jira:ABC-1@1]") < md.index("ABC-2: B [jira:ABC-2@1]")
    assert "_1 item not asserted_" in md and md.count("_None._") == 3  # decisions, links, warnings
    cited = [x for x in lines if x["citation"]]
    assert len(cited) == 2 and all(x["object_id"] == ITEM and x["depends_on"] == [ITEM] for x in cited)
    assert redactions == {}


def test_secrets_are_scrubbed_before_hashing():
    secret = Entry(
        "facts", ("x",), "- Doc with key AKIA" + "ABCDEFGHIJKLMNOP", cite("doc:x"), ITEM, ITEM, [ITEM]
    )
    lines, redactions = render("P", [secret], {})
    md = markdown_of(lines)
    assert "AKIAABCDEFGHIJKLMNOP" not in md and "[REDACTED:AWS_ACCESS_KEY]" in md
    assert redactions == {"AWS_ACCESS_KEY": 1}


def test_decisions_and_summaries():
    assert is_decision("ADR-007 Event sourcing", []) and is_decision("adr_12.md", [])
    assert is_decision("PAY-9: Decision on refunds", []) and is_decision("Notes", ["ADR"])
    assert not is_decision("Release notes", ["approved"]) and not is_decision("Addressing ADRs later", [])
    assert first_sentence("We use events.  Snapshots every 1000.") == "We use events."
    long = first_sentence("x" * 500)
    assert len(long) == 200 and long.endswith("…")
