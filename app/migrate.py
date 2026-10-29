"""The one-shot `migrate` command: the only code path that runs DDL (DESIGN §5.2)."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.config import PGVECTOR_VERSION, Settings

MIGRATIONS = Path(__file__).parent / "migrations"


class ExtensionMismatch(RuntimeError):
    pass


def alembic_config(dsn: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.attributes["dsn"] = dsn
    return cfg


def check_vector_extension(dsn: str) -> str:
    """Local, CI and RDS must run the same pgvector (DESIGN §4). Fail loudly instead of drifting."""
    engine = create_engine(dsn)
    try:
        with engine.connect() as conn:
            version = conn.scalar(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'"))
    finally:
        engine.dispose()
    if version != PGVECTOR_VERSION:
        raise ExtensionMismatch(f"pgvector {version!r} installed, {PGVECTOR_VERSION!r} expected")
    return str(version)


def migrate(settings: Settings) -> str:
    dsn = settings.dsn("migrator", driver="postgresql+psycopg")
    check_vector_extension(dsn)
    command.upgrade(alembic_config(dsn), "head")
    engine = create_engine(dsn)
    try:
        with engine.connect() as conn:
            return str(conn.scalar(text("SELECT version_num FROM alembic_version")))
    finally:
        engine.dispose()
