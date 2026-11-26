"""INPROOM-203 (story INPROOM-32 AC 1): chunk boundaries follow sections and symbols, stay within budget,
overlap, keep tables whole when they fit, and rebuild byte-identically."""

from app.content import chunking
from app.content.chunking import build, chunk_document

SETUP = " ".join(f"Step {i} installs part {i} of the tool." for i in range(120))
MARKDOWN = (
    "# Guide\n\nShort intro.\n\n## Setup\n\n"
    + SETUP
    + "\n\n## Limits\n\n| Name | Max |\n|---|---|\n| users | 10 |\n"
).encode()


def test_documents_chunk_by_section_within_budget_with_overlap():
    doc = chunk_document(MARKDOWN, "text/markdown")
    chunks = doc["chunks"]
    assert doc["method"] == "sections" and [c["ordinal"] for c in chunks] == list(range(len(chunks)))
    setup = [c for c in chunks if c["anchor"] == "guide--setup"]
    assert len(setup) >= 2 and all(c["tokens"] <= chunking.TARGET_TOKENS + 20 for c in setup)
    assert all(c["context"] == "Guide > Setup" for c in setup)  # the heading path travels with the text
    first, second = setup[0]["text"].splitlines(), setup[1]["text"].splitlines()
    overlap = [line for line in second if line in first]
    assert overlap and second[: len(overlap)] == first[-len(overlap) :]  # the next chunk repeats the tail
    assert len("\n".join(overlap)) <= chunking._budget_chars() * chunking.OVERLAP  # about 15%, no more
    [table] = [c for c in chunks if c.get("table")]
    assert table["anchor"] == "guide--limits" and table["text"] == "Name | Max\nusers | 10"  # whole table
    assert build(MARKDOWN, "text/markdown") == build(MARKDOWN, "text/markdown")  # byte-identical rebuild
    assert len({c["sha256"] for c in chunks}) == len(chunks)


def test_large_tables_split_by_rows_repeating_the_header():
    rows = [["Key", "Value"]] + [[f"k{i}", "v" * 60] for i in range(80)]
    parts = chunking._table_chunks(rows, 1600)
    assert len(parts) > 1 and all(p.startswith("Key | Value\n") for p in parts)
    assert sum(p.count("\nk") for p in parts) == 80  # every row exactly once


def test_code_chunks_follow_symbols_and_cover_module_code():
    body = "\n".join(f"        total += {i}" for i in range(200))
    source = (
        "import os\n\nLIMIT = 3\n\n"
        "class Small:\n    def a(self):\n        return 1\n\n"
        f"class Big:\n    def charge(self):\n        total = 0\n{body}\n        return total\n\n"
        "    def refund(self):\n        return 0\n\n"
        "def helper():\n    return os.getcwd()\n"
    ).encode()
    doc = chunk_document(source, "text/x-python")
    anchors = [(c["anchor"], c["line_start"]) for c in doc["chunks"]]
    assert doc["method"] == "symbols"
    assert (None, 1) in anchors and ("LIMIT", 3) in anchors and ("Small", 5) in anchors  # fits: one chunk
    names = {a for a, _ in anchors}
    assert ("Big", 9) in anchors and {"Big.charge", "Big.refund"} <= names  # too big: split into methods
    charge = [c for c in doc["chunks"] if c["anchor"] == "Big.charge"]
    assert len(charge) > 1 and all(c["context"] == "method Big.charge" for c in charge)  # line windows
    assert "helper" in names and "Big.a" not in names
    assert all(c["tokens"] <= chunking.MAX_TOKENS for c in doc["chunks"])


def test_unbroken_tokens_are_cut_never_dropped():
    units = chunking._sentences("x" * 5000, 1600)
    assert [len(u) for u in units] == [1600, 1600, 1600, 200]
