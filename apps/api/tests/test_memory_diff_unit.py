"""INPROOM-226: the snapshot diff reports exactly the added and removed lines, per section."""

from app.modules.memory import diff_lines


def lines(*pairs):
    return [{"section": s, "line": t} for s, t in pairs]


def test_per_section_counts_and_unified_diff():
    old = lines(("header", "# P"), ("facts", "- A"), ("facts", "- B"), ("links", "- x blocks y"))
    new = lines(("header", "# P"), ("facts", "- A"), ("facts", "- C"), ("decisions", "- D"))
    out = diff_lines(old, new, "v1", "v2")
    assert (out["added"], out["removed"]) == (2, 2)
    assert [(s.section, s.added, s.removed) for s in out["sections"]] == [
        ("decisions", 1, 0),
        ("facts", 1, 1),
        ("links", 0, 1),
    ]
    assert (
        out["unified"].startswith("--- v1\n+++ v2\n")
        and "-- B\n" in out["unified"]
        and "+- C\n" in out["unified"]
    )
    same = diff_lines(old, old, "v1", "v1")
    assert (same["added"], same["removed"], same["sections"], same["unified"]) == (0, 0, [], "")
