import secrets
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from testcontainers.community.postgres import PostgresContainer

from app.config import POSTGRES_IMAGE, Role, Settings
from app.migrate import migrate

ROOT = Path(__file__).resolve().parent.parent
ROLES: tuple[Role, ...] = ("migrator", "app_rw", "tools_ro", "ingest_rw", "mcp_rw")


@pytest.fixture(scope="session")
def pg() -> Iterator[Settings]:
    """A fresh Postgres from the pinned image, bootstrapped with the real init script (not migrated)."""
    passwords = {role: secrets.token_urlsafe(16) for role in ROLES}
    container = PostgresContainer(
        POSTGRES_IMAGE, username="postgres", password=secrets.token_urlsafe(16), dbname="copilot"
    )
    container.with_volume_mapping(str(ROOT / "infra" / "db"), "/bootstrap", "ro")
    with container:
        env = {f"DB_PASSWORD_{r.upper()}": pw for r, pw in passwords.items()}
        env |= {"POSTGRES_USER": "postgres", "POSTGRES_DB": "copilot"}
        code, out = container.get_wrapped_container().exec_run(["sh", "/bootstrap/init.sh"], environment=env)
        assert code == 0, out.decode()
        yield Settings(
            env="test",
            db_host=container.get_container_host_ip(),
            db_port=int(container.get_exposed_port(5432)),
            db_name="copilot",
            **{f"db_password_{r}": pw for r, pw in passwords.items()},
        )


@pytest.fixture(scope="session")
def db(pg: Settings) -> Settings:
    """The bootstrapped database, migrated to head."""
    migrate(pg)
    return pg


@pytest.fixture
def connect(db: Settings) -> Iterator[object]:
    opened: list[psycopg.Connection[tuple[object, ...]]] = []

    def _connect(role: Role) -> psycopg.Connection[tuple[object, ...]]:
        conn = psycopg.connect(db.dsn(role), autocommit=True)
        opened.append(conn)
        return conn

    yield _connect
    for conn in opened:
        conn.close()
