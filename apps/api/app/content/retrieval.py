"""Hybrid retrieval over the project's chunks (INPROOM-33): keyword and semantic candidates, fused.

Every candidate query, keyword or semantic, starts from `scope()`: chunks of the **current** revision of
**visible** items (the INPROOM-187 rule) of one project, narrowed by kinds and sources. Filters therefore run
inside each candidate query, so withheld content can never take a candidate slot.

Keyword search uses the generated `knowledge_chunks.search_tsv` (migration 0024). The query is parsed with a
web-search-like syntax (`parse_query`): bare words and "quoted phrases" are *units*, and units are OR-ed for
recall (ranking rewards chunks that match more of them); `-word` / `-"a phrase"` excludes; the word `or` is
accepted and ignored. Each unit matches stemmed (`english`) or verbatim (`simple`), so prose matches across
word forms and identifiers, issue keys and error codes match exactly. Exclusions apply in every mode.

Semantic candidates are the nearest chunks under the active embedding model. `fuse` combines both lists with
Reciprocal Rank Fusion, collapses identical text and caps chunks per item; every hit keeps the evidence of
why it ranked where it did (the `explanation` of the API).
"""

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Connection,
    Select,
    Text,
    and_,
    cast,
    func,
    literal,
    literal_column,
    select,
    text,
)
from sqlalchemy.sql.elements import ColumnElement

from app.db_schema import Vector, chunk_embeddings, knowledge_chunks, source_objects, source_revisions

MAX_UNITS = 12  # query units (words and phrases) considered; the rest are ignored
_TOKEN = re.compile(r'(-?)"([^"]*)"|(\S+)')
_EDGE = re.compile(r"^[^\w]+|[^\w]+$")

# The chunk columns a hit needs (never the tsvector itself).
HIT_COLUMNS = (
    "id",
    "object_id",
    "revision_id",
    "source_kind",
    "source_id",
    "external_id",
    "seq",
    "anchor",
    "path",
    "page",
    "line_start",
    "line_end",
    "context",
    "text",
    "sha256",
)


@dataclass(frozen=True)
class ParsedQuery:
    units: list[str] = field(default_factory=list)  # words and phrases, OR-ed
    excluded: list[str] = field(default_factory=list)  # must not match
    ignored: list[str] = field(
        default_factory=list
    )  # stop words only ("the", "of"): they would match anything


def parse_query(query: str) -> ParsedQuery:
    """Split a query into units and exclusions. Pure (unit-tested). Duplicates collapse case-insensitively;
    punctuation at a word's edges is dropped ("restart," is "restart"), inner punctuation is kept (PROJ-12,
    get_user, E1001.x)."""
    units: list[str] = []
    excluded: list[str] = []
    seen: set[tuple[bool, str]] = set()
    for minus, phrase, word in _TOKEN.findall(query):
        negative = bool(minus)
        if word:
            if word.lower() == "or":
                continue
            if word.startswith("-") and len(word) > 1:
                negative, word = True, word[1:]
            unit = _EDGE.sub("", word)
        else:
            unit = " ".join(phrase.split())
        if not unit or (negative, unit.lower()) in seen:
            continue
        seen.add((negative, unit.lower()))
        (excluded if negative else units).append(unit)
    return ParsedQuery(units[:MAX_UNITS], excluded[:MAX_UNITS])


def without_stop_words(conn: Connection, parsed: ParsedQuery) -> ParsedQuery:
    """Move units that are stop words only to `ignored`: the verbatim index keeps stop words, so "the" would
    otherwise match nearly every chunk. Postgres' own English dictionary decides."""
    if not parsed.units:
        return parsed
    sql = "SELECT numnode(phraseto_tsquery('english'::regconfig, u)) FROM unnest(CAST(:units AS text[])) AS u"
    sizes = [n for (n,) in conn.execute(text(sql), {"units": parsed.units})]
    keep = [u for u, n in zip(parsed.units, sizes, strict=True) if n]
    gone = [u for u, n in zip(parsed.units, sizes, strict=True) if not n]
    return ParsedQuery(keep, parsed.excluded, parsed.ignored + gone)


def _unit(value: str) -> ColumnElement:
    """One unit as a tsquery: its stemmed phrase OR its verbatim phrase."""
    english = func.phraseto_tsquery(literal_column("'english'::regconfig"), literal(value, Text))
    simple = func.phraseto_tsquery(literal_column("'simple'::regconfig"), literal(value, Text))
    return english.op("||")(simple)


def _any(values: list[str]) -> ColumnElement:
    out = _unit(values[0])
    for value in values[1:]:
        out = out.op("||")(_unit(value))
    return out


def scope(
    conn: Connection,
    project_id: uuid.UUID,
    *,
    kinds=None,
    source_ids=None,
    excluded: list[str] | None = None,
    changed_after: datetime | None = None,
    without_sources=None,
) -> Select:
    """Chunks of the current revision of visible items of the project (columns: HIT_COLUMNS, title,
    retrieved_at), narrowed by kinds and source ids, without chunks matching an `excluded` word or phrase.
    Freshness: `changed_after` keeps items whose current revision was retrieved after it (a revision is only
    created when the content changes, so this is when INPROOM last saw the item change); `without_sources`
    drops sources (e.g. those whose sync is stale). Callers add their own ranking column and limit."""
    from app.content.visibility import visible

    k, o, r = knowledge_chunks, source_objects, source_revisions
    stmt = (
        select(*(k.c[c] for c in HIT_COLUMNS), o.c.title, r.c.fetched_at.label("retrieved_at"))
        .join(o, and_(o.c.id == k.c.object_id, o.c.current_revision_id == k.c.revision_id))
        .join(r, r.c.id == k.c.revision_id)
        .where(k.c.project_id == project_id, visible(conn, project_id))
    )
    if kinds:
        stmt = stmt.where(k.c.source_kind.in_(list(kinds)))
    if source_ids:
        stmt = stmt.where(k.c.source_id.in_(list(source_ids)))
    if excluded:
        stmt = stmt.where(~k.c.search_tsv.bool_op("@@")(_any(excluded)))
    if changed_after is not None:
        stmt = stmt.where(r.c.fetched_at > changed_after)
    if without_sources:
        stmt = stmt.where(k.c.source_id.not_in(list(without_sources)))
    return stmt


def _row(row) -> dict[str, Any]:
    return {c: row[c] for c in (*HIT_COLUMNS, "title", "retrieved_at")}


def keyword_candidates(conn: Connection, base: Select, units: list[str], limit: int) -> list[dict[str, Any]]:
    """The `limit` best keyword matches of `base` (a `scope()` select) for any of `units`, best first. Each
    row adds `keyword_score` (ts_rank_cd) and `terms` (the units the chunk matched, in query order)."""
    if not units:
        return []
    k = knowledge_chunks
    query = _any(units)
    score = func.ts_rank_cd(k.c.search_tsv, query, 1).label("keyword_score")
    flags = [
        k.c.search_tsv.op("@@", return_type=Boolean)(_unit(u)).label(f"m{i}") for i, u in enumerate(units)
    ]
    stmt = (
        base.add_columns(score, *flags)
        .where(k.c.search_tsv.bool_op("@@")(query))
        .order_by(score.desc(), k.c.id)
        .limit(limit)
    )
    rows = []
    for row in conn.execute(stmt).mappings():
        item = _row(row)
        item["keyword_score"] = float(row["keyword_score"])
        item["terms"] = [u for i, u in enumerate(units) if row[f"m{i}"]]
        rows.append(item)
    return rows


def semantic_candidates(
    conn: Connection, base: Select, model: str, dimensions: int, vector: str, limit: int
) -> list[dict[str, Any]]:
    """The `limit` chunks of `base` nearest to `vector` (pgvector text) under `model`, nearest first. Each row
    adds `similarity` (cosine, in [-1, 1]). Uses the model's HNSW index expression `embedding::vector(n)`."""
    k, e = knowledge_chunks, chunk_embeddings
    typed = Vector(int(dimensions))
    distance = cast(e.c.embedding, typed).op("<=>")(cast(literal(vector, Text), typed)).label("distance")
    stmt = (
        base.add_columns(distance)
        .join(e, and_(e.c.tenant_id == k.c.tenant_id, e.c.model == model, e.c.sha256 == k.c.sha256))
        .order_by(distance, k.c.id)
        .limit(limit)
    )
    rows = []
    for row in conn.execute(stmt).mappings():
        item = _row(row)
        item["similarity"] = round(1.0 - float(row["distance"]), 6)
        rows.append(item)
    return rows


def fuse(
    keyword: list[dict[str, Any]],
    semantic: list[dict[str, Any]],
    *,
    k: int,
    top_k: int,
    max_per_document: int,
) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion of two best-first candidate lists, then dedupe and cap. Pure (unit-tested).

    rrf(chunk) = sum over the lists it appears in of 1 / (k + rank). Ranks only, so the unrelated scales of
    ts_rank_cd and cosine never need calibrating. Ties go to the chunk with the better single rank, then the
    lower id (deterministic). Identical text (same sha256) is returned once, at its best position; its other
    locations go to `also_in`. At most `max_per_document` chunks of one item (object) are returned.

    Each result is the candidate row plus `keyword` ({rank, score, terms} or None), `semantic` ({rank,
    similarity} or None), `rrf` and `also_in` (rows)."""
    merged: dict[int, dict[str, Any]] = {}
    for rank, row in enumerate(keyword, start=1):
        entry = merged.setdefault(row["id"], dict(row, keyword=None, semantic=None, rrf=0.0, also_in=[]))
        entry["keyword"] = {"rank": rank, "score": row["keyword_score"], "terms": row["terms"]}
        entry["rrf"] += 1.0 / (k + rank)
    for rank, row in enumerate(semantic, start=1):
        entry = merged.setdefault(row["id"], dict(row, keyword=None, semantic=None, rrf=0.0, also_in=[]))
        entry["semantic"] = {"rank": rank, "similarity": row["similarity"]}
        entry["rrf"] += 1.0 / (k + rank)

    def best(entry):
        ranks = [p["rank"] for p in (entry["keyword"], entry["semantic"]) if p]
        return (-entry["rrf"], min(ranks), entry["id"])

    out: list[dict[str, Any]] = []
    by_sha: dict[str, dict[str, Any]] = {}
    per_object: dict[Any, int] = {}
    for entry in sorted(merged.values(), key=best):
        first = by_sha.get(entry["sha256"])
        if first is not None:
            first["also_in"].append(entry)
            continue
        if per_object.get(entry["object_id"], 0) >= max_per_document:
            continue
        by_sha[entry["sha256"]] = entry
        per_object[entry["object_id"]] = per_object.get(entry["object_id"], 0) + 1
        out.append(entry)
    for entry in out:
        entry["rrf"] = round(entry["rrf"], 6)
    return out[:top_k]


def supported(entry: dict[str, Any], min_similarity: float) -> bool:
    """Whether a fused hit is evidence for the query: it matched at least one query word or phrase, or its
    semantic similarity reaches `min_similarity`. Rank fusion always yields *something*; this gate lets a
    caller tell "nothing relevant" apart from "the least irrelevant". Pure (unit-tested)."""
    if entry.get("keyword") and entry["keyword"]["terms"]:
        return True
    semantic = entry.get("semantic")
    return bool(semantic) and semantic["similarity"] >= min_similarity


REASONS = {
    "NO_CONTENT": "The project has no retrievable content in this scope (no visible, chunked items)",
    "ALL_FILTERED": "Matching content exists, but the freshness filters exclude all of it",
    "NO_MATCH": "Nothing matched the query",
    "LOW_RELEVANCE": "Candidates were found, but none matched a query term or reached the similarity floor",
}


def evidence(
    returned: int, *, has_content: bool, filtered_would_match: bool, candidates: int, degraded: list[str]
) -> dict[str, Any]:
    """The evidence verdict for a retrieval. Pure (unit-tested). `returned` counts supported hits returned;
    the other facts are only consulted when it is 0, in this order: no content at all, then content that only
    the freshness filters removed, then no candidates, then only unsupported candidates."""
    if returned:
        return {"status": "sufficient", "reason": None, "detail": f"{returned} supported result(s)"}
    if not has_content:
        reason = "NO_CONTENT"
    elif filtered_would_match:
        reason = "ALL_FILTERED"
    elif not candidates:
        reason = "NO_MATCH"
    else:
        reason = "LOW_RELEVANCE"
    detail = REASONS[reason]
    if "semantic" in degraded:
        detail += "; semantic retrieval was unavailable, so only keyword matches were considered"
    return {"status": "insufficient", "reason": reason, "detail": detail}
