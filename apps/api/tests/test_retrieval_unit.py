"""INPROOM-211: how a retrieval query is understood (words, phrases, exclusions)."""

from app.content.retrieval import MAX_UNITS, ParsedQuery, evidence, fuse, parse_query, supported


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


def _row(i, sha=None, obj="a", **extra):
    return {"id": i, "sha256": sha or f"s{i}", "object_id": obj, "title": "", **extra}


def kw(*ids, **kw_rows):
    return [
        _row(i, keyword_score=1.0 / n, terms=["t"], **kw_rows.get(str(i), {})) for n, i in enumerate(ids, 1)
    ]


def sem(*ids):
    return [_row(i, similarity=1.0 - n / 10) for n, i in enumerate(ids, 1)]


def test_rrf_rewards_agreement_and_explains_both_lists():
    out = fuse(kw(1, 2, 3), sem(3, 4, 1), k=60, top_k=10, max_per_document=10)
    assert [r["id"] for r in out] == [1, 3, 2, 4]  # in both lists first; ties broken by best single rank
    first = out[0]
    assert first["keyword"] == {"rank": 1, "score": 1.0, "terms": ["t"]} and first["semantic"]["rank"] == 3
    assert first["rrf"] == round(1 / 61 + 1 / 63, 6)
    assert out[2]["semantic"] is None and out[3]["keyword"] is None
    assert [r["id"] for r in fuse(kw(1, 2), [], k=60, top_k=1, max_per_document=10)] == [1]  # keyword only
    assert fuse([], [], k=60, top_k=5, max_per_document=3) == []


def test_identical_text_once_with_its_other_locations_and_a_per_document_cap():
    rows = [_row(1, "same", "a"), _row(2, "same", "b"), _row(3, obj="a"), _row(4, obj="a"), _row(5, obj="a")]
    keyword = [r | {"keyword_score": 1.0, "terms": []} for r in rows]
    out = fuse(keyword, [], k=60, top_k=10, max_per_document=2)
    assert [r["id"] for r in out] == [1, 3]  # 2 is a duplicate of 1; object "a" capped at 2
    assert [o["id"] for o in out[0]["also_in"]] == [2]


def test_supported_means_a_term_match_or_enough_similarity():
    assert supported({"keyword": {"terms": ["x"]}, "semantic": None}, 0.9)
    assert not supported({"keyword": {"terms": []}, "semantic": {"similarity": 0.2}}, 0.35)
    assert supported({"keyword": None, "semantic": {"similarity": 0.35}}, 0.35)
    assert not supported({"keyword": None, "semantic": None}, 0.0)


def test_evidence_reasons_in_order():
    facts = {"has_content": True, "filtered_would_match": False, "candidates": 4, "degraded": []}
    assert evidence(2, **facts) == {"status": "sufficient", "reason": None, "detail": "2 supported result(s)"}
    assert evidence(0, **facts)["reason"] == "LOW_RELEVANCE"
    assert evidence(0, **facts | {"candidates": 0})["reason"] == "NO_MATCH"
    assert evidence(0, **facts | {"candidates": 0, "filtered_would_match": True})["reason"] == "ALL_FILTERED"
    empty = facts | {"has_content": False, "filtered_would_match": True}
    assert evidence(0, **empty)["reason"] == "NO_CONTENT"
    degraded = evidence(0, **facts | {"degraded": ["semantic"]})
    assert degraded["status"] == "insufficient" and "semantic retrieval was unavailable" in degraded["detail"]
