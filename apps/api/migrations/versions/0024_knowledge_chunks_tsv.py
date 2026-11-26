"""Lexical index on retrieval chunks (INPROOM-211 / story INPROOM-33).

Revision ID: 0024_knowledge_chunks_tsv
Revises: 0023_chunk_embeddings
Create Date: 2026-11-26

`knowledge_chunks.title` copies the item's title at projection time (a page without headings has no title in
its chunk context). `knowledge_chunks.search_tsv` is a stored generated tsvector, so it can never drift from
the chunk and leaves with its row. Weights: the title, external id and context (heading path, file and
symbol) are A, the text B. Each part is indexed twice, stemmed (`english`) and verbatim (`simple`), so
prose matches across word forms while identifiers, issue keys and error codes match exactly. A GIN index
serves `@@`. Existing rows get their title from source_objects, and the ALTER computes their tsvector.
"""

from alembic import op

revision = "0024_knowledge_chunks_tsv"
down_revision = "0023_chunk_embeddings"
branch_labels = None
depends_on = None

TSV = (
    "setweight(to_tsvector('simple'::regconfig, title || ' ' || external_id || ' ' || context), 'A') || "
    "setweight(to_tsvector('english'::regconfig, title || ' ' || context), 'A') || "
    "setweight(to_tsvector('english'::regconfig, text), 'B') || "
    "setweight(to_tsvector('simple'::regconfig, text), 'B')"
)


def upgrade() -> None:
    op.execute("ALTER TABLE knowledge_chunks ADD COLUMN title text NOT NULL DEFAULT ''")
    op.execute(
        "UPDATE knowledge_chunks k SET title = o.title FROM source_objects o "
        "WHERE o.id = k.object_id AND o.title <> ''"
    )
    op.execute(
        f"ALTER TABLE knowledge_chunks ADD COLUMN search_tsv tsvector GENERATED ALWAYS AS ({TSV}) STORED"
    )
    op.execute("CREATE INDEX ix_knowledge_chunks_tsv ON knowledge_chunks USING gin (search_tsv)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_tsv")
    op.execute("ALTER TABLE knowledge_chunks DROP COLUMN search_tsv")
    op.execute("ALTER TABLE knowledge_chunks DROP COLUMN title")
