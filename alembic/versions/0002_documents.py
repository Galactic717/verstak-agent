"""Store tenant documents and index their searchable chunks."""

from alembic import op

revision = "0002_documents"
down_revision = "0001_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        """
        CREATE TABLE documents (
            id uuid PRIMARY KEY,
            tenant varchar(100) NOT NULL,
            filename varchar(255) NOT NULL,
            media_type varchar(120) NOT NULL,
            size_bytes integer NOT NULL CHECK (size_bytes BETWEEN 1 AND 5242880),
            content text NOT NULL,
            tabular jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant, id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE document_chunks (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tenant varchar(100) NOT NULL,
            document_id uuid NOT NULL,
            chunk_number integer NOT NULL,
            content text NOT NULL,
            embedding vector(256),
            lexemes tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
            UNIQUE (tenant, document_id, chunk_number),
            FOREIGN KEY (tenant, document_id) REFERENCES documents (tenant, id) ON DELETE CASCADE
        )
        """
    )
    op.execute("CREATE INDEX chunks_full_text ON document_chunks USING gin (lexemes)")
    op.execute(
        "CREATE INDEX chunks_vector ON document_chunks USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.drop_table("document_chunks")
    op.drop_table("documents")
