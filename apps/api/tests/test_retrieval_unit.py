"""INPROOM-211: how a retrieval query is understood (words, phrases, exclusions)."""

from app.content.retrieval import MAX_UNITS, ParsedQuery, parse_query


def test_words_phrases_and_exclusions():
    assert parse_query('restart the API, "ledger lock" -cache -"old runbook" or PAY-17') == ParsedQuery(
        units=["restart", "the", "API", "ledger lock", "PAY-17"], excluded=["cache", "old runbook"]
    )
    # inner punctuation is kept (identifiers, keys, codes); edge punctuation and duplicates are dropped
    assert parse_query("get_user() E1042. get_user OR (E1042)").units == ["get_user", "E1042"]
    assert parse_query('  "  spaced   phrase " ').units == ["spaced phrase"]


def test_nothing_searchable_and_limits():
    assert parse_query("-only -excluded").units == []
    assert parse_query('or "" - ... ').units == []
    assert len(parse_query(" ".join(f"w{i}" for i in range(40))).units) == MAX_UNITS
