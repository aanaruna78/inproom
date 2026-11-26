"""One visibility rule for every read path (INPROOM-187 / story INPROOM-31).

`access_reason` (app/modules/content.py) decides for ONE item, and it serves raw content, documents, code
files and provenance. `visible(conn, project_id)` is the same rule as a SQL predicate over `source_objects`,
for the paths that select MANY items: `derivatives.live()`, code search and provenance conflicts. An item is
visible when:
- it is CURRENT (not deleted, excluded, restricted or too large);
- its source is ACTIVE in the project (a git item's branch must also be selected; uploads must be an allowed
  kind in the project settings);
- its source's classification is within the project's cap.
A consistency test checks that both forms agree for every withholding reason.
"""

import uuid

from sqlalchemy import ColumnElement, Connection, and_, false, func, not_, or_, select, true, tuple_

from app.db_schema import (
    project_confluence_sources,
    project_jira_sources,
    project_repositories,
    source_objects,
)

o = source_objects


def _source_active(project_id: uuid.UUID) -> ColumnElement[bool]:
    # Aliased and explicitly correlated to source_objects only: callers may already join the same source
    # tables (code search joins project_repositories), and auto-correlation would empty the subquery.
    repo = project_repositories.alias("visible_repo")
    branch = func.split_part(o.c.external_id, ":", 1)
    clauses = [
        and_(
            o.c.source_kind == "git",
            select(repo.c.id)
            .where(
                repo.c.id == o.c.source_id,
                repo.c.project_id == project_id,
                repo.c.status == "ACTIVE",
                branch == func.any(repo.c.branches),
            )
            .correlate(o)
            .exists(),
        )
    ]
    for kind, base in (("jira", project_jira_sources), ("confluence", project_confluence_sources)):
        table = base.alias(f"visible_{kind}")
        clauses.append(
            and_(
                o.c.source_kind == kind,
                o.c.source_id.in_(
                    select(table.c.id)
                    .where(table.c.project_id == project_id, table.c.status == "ACTIVE")
                    .correlate_except(table)
                ),
            )
        )
    return or_(*clauses)


def visible(conn: Connection, project_id: uuid.UUID) -> ColumnElement[bool]:
    """A predicate over source_objects: the item may be served to the project now."""
    from app.content import policies
    from app.modules.content import _source_status

    uploads = _source_status(conn, {"source_kind": "upload", "project_id": project_id}) == "ACTIVE"
    clause = and_(
        o.c.project_id == project_id,
        o.c.status == "CURRENT",
        or_(_source_active(project_id), and_(o.c.source_kind == "upload", true() if uploads else false())),
    )
    over = policies.over_cap(conn, project_id)
    if over:
        clause = and_(clause, not_(tuple_(o.c.source_kind, o.c.source_id).in_(sorted(over, key=str))))
    return clause
