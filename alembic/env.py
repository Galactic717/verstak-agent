from time import sleep

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import connect
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from alembic import context
from verstak.config import settings
from verstak.db import Base

url = settings().database_url
sqlalchemy_url = url.replace("postgresql://", "postgresql+psycopg://", 1)

if context.is_offline_mode():
    context.configure(url=sqlalchemy_url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    # Render services can pre-deploy together. Keep a session lock across both schema upgrades.
    with connect(url, autocommit=True) as migration_lock:
        # Blocked SQL statements can hold snapshots needed by CREATE INDEX CONCURRENTLY.
        while True:
            acquired = migration_lock.execute(
                "select pg_try_advisory_lock(%s, %s)", (int.from_bytes(b"vstk", "big"), 1)
            ).fetchone()
            if acquired is not None and acquired[0]:
                break
            sleep(0.25)
        database = create_engine(sqlalchemy_url, poolclass=NullPool)
        try:
            with database.connect() as connection:
                context.configure(connection=connection, target_metadata=Base.metadata)
                with context.begin_transaction():
                    context.run_migrations()
        finally:
            database.dispose()

        # LangGraph versions its checkpoint schema independently of application data.
        with PostgresSaver.from_conn_string(url) as saver:
            saver.setup()
