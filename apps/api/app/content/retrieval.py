"""Retrieval over the project's chunks (INPROOM-33): keyword candidates, and the filter they all share.

Every candidate query, keyword or semantic, starts from `scope()`: chunks of the **current** revision of
**visible** items (the INPROOM-187 rule) of one project, narrowed by kinds and sources. Filters therefore run
inside each candidate query, so withheld content can never take a candidate slot.

Keyword search uses the generated `knowledge_chunks.search_tsv` (migration 0024). The query is parsed with a
web-search-like syntax (`parse_query`): bare words and "quoted phrases" are *units*, and units are OR-ed for
recall (ranking rewards chunks that match more of them); `-word` / `-"a phrase"` excludes; the word `or` is
accepted and ignored. Each unit matches stemmed (`english`) or verbatim (`simple`), so prose matches across
word forms and identifiers, issue keys and error codes match exactly.
"""

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Boolean, Connection, Select, and_, bindparam, func, literal_column, select, text
from sqlalchemy.sql.elements import ColumnElement

from app.db_schema import knowledge_chunks, source_objects, source_revisions

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


def _unit(name: str) -> ColumnElement:
    """One unit as a tsquery: its stemmed phrase OR its verbatim phrase."""
    english = func.phraseto_tsquery(literal_column("'english'::regconfig"), bindparam(name))
    simple = func.phraseto_tsquery(literal_column("'simple'::regconfig"), bindparam(name))
    return english.op("||")(simple)


def _any(names: list[str]) -> ColumnElement:
    out = _unit(names[0])
    for name in names[1:]:
        out = out.op("||")(_unit(name))
    return out


def scope(conn: Connection, project_id: uuid.UUID, *, kinds=None, source_ids=None) -> Select:
    """Chunks of the current revision of visible items of the project (columns: HIT_COLUMNS, title,
    retrieved_at), narrowed by kinds and source ids. Callers add their own ranking column and limit."""
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
    return stmt


def keyword_candidates(
    conn: Connection, base: Select, parsed: ParsedQuery, limit: int
) -> list[dict[str, Any]]:
    """The `limit` best keyword matches of `base` (a `scope()` select), best first. Each row adds
    `keyword_score` (ts_rank_cd) and `terms` (the query units the chunk matched, in query order)."""
    if not parsed.units:
        return []
    k = knowledge_chunks
    params = {f"u{i}": u for i, u in enumerate(parsed.units)} | {
        f"x{i}": u for i, u in enumerate(parsed.excluded)
    }
    units = [f"u{i}" for i in range(len(parsed.units))]
    query = _any(units)
    matches = k.c.search_tsv.bool_op("@@")(query)
    if parsed.excluded:
        matches = and_(
            matches, ~k.c.search_tsv.bool_op("@@")(_any([f"x{i}" for i in range(len(parsed.excluded))]))
        )
    score = func.ts_rank_cd(k.c.search_tsv, query, 1).label("keyword_score")
    flags = [k.c.search_tsv.op("@@", return_type=Boolean)(_unit(u)).label(f"m_{u}") for u in units]
    stmt = base.add_columns(score, *flags).where(matches).order_by(score.desc(), k.c.id).limit(limit)
    rows = []
    for row in conn.execute(stmt, params).mappings():
        item = {c: row[c] for c in (*HIT_COLUMNS, "title", "retrieved_at")}
        item["keyword_score"] = float(row["keyword_score"])
        item["terms"] = [parsed.units[i] for i, u in enumerate(units) if row[f"m_{u}"]]
        rows.append(item)
    return rows
