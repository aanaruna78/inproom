"""INPROOM-214 (story INPROOM-33 AC 6, 8): the golden retrieval set (tests/data/golden_retrieval.json).

Plants a small corpus of runbooks, ADRs, incident and issue notes and Confluence pages in one project, plus
leak bait where it must never be returned (a restricted page, a deleted upload, another project's document),
then measures every mode: recall@5 and MRR over answerable queries, evidence verdicts, and leaks. Hybrid
must meet the thresholds, recall at least as much as either single mode, judge evidence correctly for every
query, and leak nothing. CI runs the deterministic local embedder, whose "semantic" list is lexical hashing:
its semantic-only scores are low by design; a hosted model is evaluated with the same command.

`make eval-retrieval` runs this module and writes the report to build/retrieval-eval.md.
"""

import json
import os
from pathlib import Path

import pytest

from tests import test_confluence_sources_integration as conf
from tests.test_document_api_integration import drain
from tests.test_documents_integration import upload

pytestmark = pytest.mark.integration
env, selected = conf.env, conf.selected

GOLDEN = json.loads((Path(__file__).parent / "data" / "golden_retrieval.json").read_text())
MODES = ("keyword", "semantic", "hybrid")


def retrieve(e, query, mode, **body):
    payload = {"query": query, "mode": mode, "top_k": 10} | body
    r = e.client.post(f"/v1/projects/{e.pid}/knowledge/retrieve", json=payload, headers=e.h())
    assert r.status_code == 200, r.text
    return r.json()


def label(hit, names):
    c = hit["citation"]
    if c["kind"] == "upload":
        return names.get(c["external_id"], c["external_id"])
    return f"{'conf' if c['kind'] == 'confluence' else c['kind']}:{c['external_id']}"


def metrics(ranked: list[list[str]], expected: list[list[str]]) -> dict[str, float]:
    """recall@5 and MRR (by the first expected hit within the returned list), averaged over queries."""
    recall = [len(set(r[:5]) & set(x)) / len(x) for r, x in zip(ranked, expected, strict=True)]
    rr = [
        next((1 / i for i, h in enumerate(r, 1) if h in x), 0.0)
        for r, x in zip(ranked, expected, strict=True)
    ]
    return {"recall_at_5": round(sum(recall) / len(recall), 3), "mrr": round(sum(rr) / len(rr), 3)}


def report(lines: list[str]) -> None:
    text = "\n".join(lines) + "\n"
    print("\n" + text)
    if path := os.environ.get("INPROOM_RETRIEVAL_REPORT"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text)


@pytest.fixture
def corpus(selected):
    e = selected
    names = {}
    for name, body in GOLDEN["documents"].items():
        r = upload(e, name, body.encode())
        assert r.status_code == 201, r.text
        names[r.json()["document_id"]] = name
    for pid, page in GOLDEN["confluence"].items():
        e.conf.add_page(int(pid), "ENG", page["title"], page["html"], parent=100, labels=["approved"])
    leaks = GOLDEN["leaks"]
    bait = leaks["restricted_page"]
    e.conf.add_page(int(bait["id"]), "ENG", bait["title"], bait["html"], parent=100, labels=["approved"])
    e.conf.pages[int(bait["id"])].groups = ["bots"]  # restricted at the source: withheld
    gone = leaks["deleted_upload"]
    deleted = upload(e, gone["name"], gone["text"].encode()).json()["document_id"]
    assert e.sync().status_code == 200
    drain(e)
    assert e.client.delete(f"/v1/projects/{e.pid}/documents/{deleted}", headers=e.h()).status_code == 204
    ws = e.client.get(f"/v1/projects/{e.pid}", headers=e.h()).json()["workspace_id"]
    other = e.client.post(
        f"/v1/workspaces/{ws}/projects", json={"key": "OTH", "name": "Other"}, headers=e.h()
    ).json()["id"]
    leak = leaks["other_project"]
    r = e.client.post(
        f"/v1/projects/{other}/documents",
        files={"file": (leak["name"], leak["text"].encode())},
        headers=e.h(),
    )
    assert r.status_code == 201, r.text
    drain(e)
    bad = {
        f"conf:{bait['id']}",
        "conf:103",
        deleted,
        r.json()["document_id"],
    }  # 103: the fixture's restricted page
    return e, names, bad


def test_golden_retrieval_set(corpus):
    e, names, bad = corpus
    queries = GOLDEN["queries"]
    expected = [q["expect"] for q in queries]
    lines = [
        f"# Retrieval evaluation (golden set v{GOLDEN['version']})",
        "",
        "| Mode | recall@5 | MRR | " "evidence correct | leaks |",
        "|---|---|---|---|---|",
    ]
    results = {}
    for mode in MODES:
        ranked, verdicts = [], 0
        for q in queries:
            out = retrieve(e, q["q"], mode)
            ranked.append([label(h, names) for h in out["items"]])
            verdicts += out["evidence"]["status"] == "sufficient"
        for q in GOLDEN["unanswerable"]:
            verdicts += retrieve(e, q, mode)["evidence"]["status"] == "insufficient"
        leaked = set()
        probes = [q["q"] for q in queries] + GOLDEN["unanswerable"]
        probes += [x["phrase"] for x in GOLDEN["leaks"].values()] + ["org plans"]
        for q in probes:
            hits = retrieve(e, q, mode, top_k=50, include_unsupported=True)["items"]
            leaked |= {label(h, names) for h in hits} & bad
            leaked |= {o["citation"]["external_id"] for h in hits for o in h["also_in"]} & bad
        m = metrics(ranked, expected) | {
            "evidence": f"{verdicts}/{len(queries) + len(GOLDEN['unanswerable'])}",
            "leaks": sorted(leaked),
        }
        results[mode] = m
        lines.append(f"| {mode} | {m['recall_at_5']} | {m['mrr']} | {m['evidence']} | {len(leaked)} |")
        misses = [q["q"] for q, r in zip(queries, ranked, strict=True) if not set(r[:5]) & set(q["expect"])]
        if misses:
            lines.append(f"|  | misses: {'; '.join(misses)} | | | |")
    report(lines)

    hybrid, limits = results["hybrid"], GOLDEN["thresholds"]
    assert all(not r["leaks"] for r in results.values()), results  # nothing withheld, in any mode
    assert hybrid["recall_at_5"] >= limits["recall_at_5"] and hybrid["mrr"] >= limits["mrr"], hybrid
    # Story AC8 as amended (2026-11-27): hybrid recalls at least as much as either single mode, and ranks
    # within `mrr_margin` of the better one. With the local hash embedder the semantic list is lexical, so
    # hybrid cannot strictly beat keyword here; a hosted model shows a strict gain (retrieval.md).
    for single in ("keyword", "semantic"):
        assert hybrid["recall_at_5"] >= results[single]["recall_at_5"], results
        assert hybrid["mrr"] >= results[single]["mrr"] - limits["mrr_margin"], results
    total = len(queries) + len(GOLDEN["unanswerable"])
    assert (
        hybrid["evidence"] == f"{total}/{total}"
    ), hybrid  # answerable: sufficient; unanswerable: insufficient
