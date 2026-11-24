from langgraph.checkpoint.postgres import PostgresSaver
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
    database = create_engine(sqlalchemy_url, poolclass=NullPool)
    with database.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
    database.dispose()

    # LangGraph versions its checkpoint schema independently of application data.
    with PostgresSaver.from_conn_string(url) as saver:
        saver.setup()
