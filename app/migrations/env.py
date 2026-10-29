"""Alembic environment. Always connects as `migrator`; the URL is built from Settings, never from alembic.ini."""

from alembic import context
from sqlalchemy import create_engine

from app.config import Settings

url = context.config.attributes.get("dsn") or Settings().dsn("migrator", driver="postgresql+psycopg")
engine = create_engine(url)

with engine.connect() as connection:
    context.configure(connection=connection, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
