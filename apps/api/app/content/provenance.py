"""Provenance records: where a cited passage came from (INPROOM-182 / story INPROOM-30).

A record is COMPUTED at read time from the authoritative rows - revisions, derivatives, catalog, sync state,
policies - so it cannot drift from the data it describes. Unknown facts are null and listed under
`completeness.missing` with a reason; nothing is guessed.

Derivation vocabulary: "verbatim" (the stored original bytes), "extracted" (a deterministic producer - parse
sections, code symbols), "inferred" (model-generated content, E05+; not produced yet).
"""

import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from sqlalchemy import Connection, select

from app.content import store as blobs
from app.db_schema import (
    code_symbols,
    confluence_pages,
    derivatives,
    project_confluence_sources,
    project_jira_sources,
    project_repositories,
    source_connections,
    source_revisions,
)

DEFAULT_TRUST = {"jira": "trusted", "git": "trusted", "confluence": "trusted", "upload": "reference"}
DEFAULT_CLASSIFICATION = "internal"
LINES = re.compile(r"^L(\d+)(?:-L(\d+))?$")
CODE_MEDIA = ("text/x-python", "text/javascript", "text/x-typescript", "text/x-go", "text/x-java")


class AnchorNotFound(LookupError):
    pass


def policy_of(conn: Connection, kind: str, source_id) -> dict[str, Any]:
    """Trust tier and classification of a source (defaults by kind; explicit policies: INPROOM-183)."""
    return {
        "tier": DEFAULT_TRUST.get(kind, "reference"),
        "classification": DEFAULT_CLASSIFICATION,
        "origin": "default",
        "reason": None,
        "version": 0,
    }


def _site(base_url: str) -> str:
    """Confluence Cloud serves pages under /wiki; Data Center at the site root."""
    base = base_url.rstrip("/")
    host = base.split("://", 1)[-1].split("/", 1)[0]
    return base + "/wiki" if host.endswith(".atlassian.net") and not base.endswith("/wiki") else base


def _web_base(api_base: str) -> str:
    if api_base.rstrip("/") == "https://api.github.com":
        return "https://github.com"
    return api_base.rstrip("/").removesuffix("/api/v3")


def _source(conn: Connection, obj: dict[str, Any]) -> dict[str, Any]:
    """name, base URL and (for git) repository facts of the item's source."""
    kind = obj["source_kind"]
    tables = {
        "confluence": project_confluence_sources,
        "jira": project_jira_sources,
        "git": project_repositories,
    }
    if kind not in tables:
        return {"name": "Uploaded documents", "base_url": None}
    table = tables[kind]
    row = (
        conn.execute(
            select(table, source_connections.c.base_url)
            .join(
                source_connections,
                (source_connections.c.tenant_id == table.c.tenant_id)
                & (source_connections.c.id == table.c.connection_id),
            )
            .where(table.c.id == obj["source_id"])
        )
        .mappings()
        .first()
    )
    if row is None:
        return {"name": None, "base_url": None}
    name = {"confluence": row.get("space_key"), "jira": row.get("jira_key"), "git": row.get("full_name")}[
        kind
    ]
    return {"name": name, "base_url": row["base_url"], "full_name": row.get("full_name")}


def _modified(conn: Connection, obj: dict[str, Any], rev: dict[str, Any], missing: list) -> dict[str, Any]:
    kind, at, by = obj["source_kind"], None, None
    if kind == "confluence" and not obj["external_id"].startswith("att:"):
        page = conn.execute(
            select(confluence_pages.c.last_modified_at, confluence_pages.c.author).where(
                confluence_pages.c.source_id == obj["source_id"],
                confluence_pages.c.page_id == obj["external_id"],
            )
        ).first()
        if page is not None:
            at, by = page.last_modified_at, page.author
        if at is None:
            missing.append({"field": "modified.at", "reason": "Confluence did not report the version time"})
        if by is None:
            missing.append({"field": "modified.by", "reason": "Confluence did not report the version author"})
    elif kind == "confluence":
        missing.append(
            {"field": "modified.at", "reason": "Confluence does not report attachment change times"}
        )
        missing.append({"field": "modified.by", "reason": "Confluence does not report attachment authors"})
    elif kind == "jira":
        at = rev["provider_version"]  # the issue's "updated" timestamp is its provider version
        if at is None:
            missing.append({"field": "modified.at", "reason": "the Jira record had no updated time"})
        missing.append(
            {"field": "modified.by", "reason": "Jira reports who created an issue, not who last changed it"}
        )
    elif kind == "git":
        missing.append({"field": "modified.at", "reason": "commit times are not retrieved by the code index"})
        missing.append(
            {"field": "modified.by", "reason": "commit authors are not retrieved by the code index"}
        )
    else:  # upload: the upload is the change
        at = rev["fetched_at"].isoformat() if rev["fetched_at"] else None
        by = (rev.get("acl") or {}).get("uploaded_by")
        if by is None:
            missing.append({"field": "modified.by", "reason": "the uploader was not recorded"})
    return {"at": at, "by": by}


def _url(obj: dict[str, Any], src: dict[str, Any], missing: list) -> str | None:
    kind = obj["source_kind"]
    if kind == "upload":
        missing.append({"field": "source.url", "reason": "uploaded documents have no provider location"})
        return None
    if kind == "git":
        commit = obj.get("verified_ref")
        if not (src.get("base_url") and src.get("full_name") and commit):
            missing.append({"field": "source.url", "reason": "no verified commit for this file"})
            return None
        path = obj["external_id"].split(":", 1)[1]
        return f"{_web_base(src['base_url'])}/{src['full_name']}/blob/{commit}/{quote(path, safe='/')}"
    if not obj.get("uri") or not src.get("base_url"):
        missing.append({"field": "source.url", "reason": "the provider gave no web location"})
        return None
    base = _site(src["base_url"]) if kind == "confluence" else src["base_url"].rstrip("/")
    return obj["uri"] if obj["uri"].startswith("http") else base + obj["uri"]


def _ready(conn: Connection, revision_id, producer: str) -> dict[str, Any] | None:
    row = (
        conn.execute(
            select(derivatives)
            .where(
                derivatives.c.revision_id == revision_id,
                derivatives.c.producer == producer,
                derivatives.c.status == "READY",
                derivatives.c.purged_at.is_(None),
            )
            .order_by(derivatives.c.producer_version.desc(), derivatives.c.built_at.desc())
            .limit(1)
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


def _derivation(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "method": "extracted",
        "producer": row["producer"],
        "producer_version": row["producer_version"],
        "built_at": row["built_at"].isoformat() if row["built_at"] else None,
    }


def _passage(conn: Connection, obj, rev, anchor: str, missing: list) -> tuple[dict | None, dict]:
    """(passage, derivation) for an anchor: a code symbol (L<n>[-L<m>] or a symbol name) or a parsed section
    (document anchors, Jira field-*). AnchorNotFound when the revision has no such passage."""
    if obj["source_kind"] == "git" and rev["media_type"] in CODE_MEDIA:
        parsed = _ready(conn, rev["id"], "symbols")
        if parsed is None:
            missing.append({"field": "passage", "reason": "the file's symbols are not built yet"})
            return None, {
                "method": "extracted",
                "producer": "symbols",
                "producer_version": None,
                "built_at": None,
            }
        rows = [
            dict(r)
            for r in conn.execute(
                select(code_symbols).where(code_symbols.c.derivative_id == parsed["id"])
            ).mappings()
        ]
        lines = LINES.match(anchor)
        if lines:
            start = int(lines.group(1))
            hit = next((r for r in rows if r["line_start"] == start), None)
        else:
            hit = next(
                (
                    r
                    for r in rows
                    if anchor in (r["name"], f"{r['container']}.{r['name']}" if r["container"] else None)
                ),
                None,
            )
        if hit is None:
            raise AnchorNotFound(anchor)
        passage = {
            "kind": "symbol",
            "anchor": anchor,
            "name": hit["name"],
            "symbol_kind": hit["kind"],
            "container": hit["container"],
            "line_start": hit["line_start"],
            "line_end": hit["line_end"],
        }
        return passage, _derivation(parsed)
    parsed = _ready(conn, rev["id"], "parse")
    if parsed is None:
        missing.append({"field": "passage", "reason": "the document is not parsed yet"})
        return None, {"method": "extracted", "producer": "parse", "producer_version": None, "built_at": None}
    try:
        doc = json.loads(blobs.read(blobs.get_store(), parsed["blob_key"]))
    except (blobs.ObjectStoreUnavailable, blobs.BlobMissing, blobs.BlobCorrupt) as exc:
        missing.append({"field": "passage", "reason": f"the parsed document is unavailable ({exc.code})"})
        return None, _derivation(parsed)
    section = next((s for s in doc.get("sections") or [] if s["anchor"] == anchor), None)
    if section is None:
        raise AnchorNotFound(anchor)
    passage = {
        "kind": "section",
        "anchor": anchor,
        "heading": section["heading"],
        "path": section["path"],
        "page": section["page"],
        "characters": len(section["text"]),
        "tables": len(section["tables"]),
    }
    return passage, _derivation(parsed)


def _current_seq(conn: Connection, obj) -> int | None:
    if obj["current_revision_id"] is None:
        return None
    return conn.execute(
        select(source_revisions.c.seq).where(source_revisions.c.id == obj["current_revision_id"])
    ).scalar()


def build(conn: Connection, obj: dict[str, Any], rev: dict[str, Any], anchor: str | None) -> dict[str, Any]:
    """The provenance record of one revision (and optionally one passage of it)."""
    missing: list[dict[str, str]] = []
    src = _source(conn, obj)
    url = _url(obj, src, missing)
    modified = _modified(conn, obj, rev, missing)
    if rev["provider_version"] is None:
        missing.append(
            {"field": "revision.provider_version", "reason": "the provider has no version for this item"}
        )
    passage, derivation = (
        None,
        {"method": "verbatim", "producer": None, "producer_version": None, "built_at": None},
    )
    if anchor:
        passage, derivation = _passage(conn, obj, rev, anchor, missing)
    current = _current_seq(conn, obj)
    freshness = {"status": "current", "reasons": [], "current_seq": current}
    if current is not None and current != rev["seq"]:
        freshness = {
            "status": "superseded",
            "reasons": [{"code": "NEWER_REVISION", "detail": f"revision {current} is current"}],
            "current_seq": current,
        }
    known_fields = 6 + (
        1 if anchor else 0
    )  # url, modified.at, modified.by, provider_version, fetched_at, acl, passage
    score = round(max(0, known_fields - len(missing)) / known_fields, 2)
    return {
        "as_of": datetime.now(UTC).isoformat(),
        "source": {
            "kind": obj["source_kind"],
            "id": str(obj["source_id"]),
            "name": src.get("name"),
            "item_id": obj["external_id"],
            "title": obj["title"],
            "url": url,
        },
        "revision": {
            "seq": rev["seq"],
            "provider_version": rev["provider_version"],
            "content_sha256": rev["content_sha256"],
            "media_type": rev["media_type"],
            "size_bytes": rev["size_bytes"],
            "retrieved_at": rev["fetched_at"].isoformat() if rev["fetched_at"] else None,
            "retrieved_by_job": str(rev["job_id"]) if rev["job_id"] else None,
            "verified_ref": obj.get("verified_ref"),
            "content_stored": rev["blob_key"] is not None and rev["purged_at"] is None,
        },
        "modified": modified,
        "trust": policy_of(conn, obj["source_kind"], obj["source_id"]),
        "access": {
            "fetched_under": rev.get("acl") or {},
            "item_status": obj["status"],
        },
        "freshness": freshness,
        "derivation": derivation,
        "passage": passage,
        "completeness": {"score": score, "missing": missing},
        "conflicts": [],
        "duplicates": [],
    }
