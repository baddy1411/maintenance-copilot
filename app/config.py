"""Runtime settings. Everything comes from the environment (or `.env` locally); nothing secret has a default."""

from typing import Literal
from urllib.parse import quote

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Database roles (DESIGN §5.2). `migrator` owns all DDL and is never held by a running service.
Role = Literal["migrator", "app_rw", "tools_ro", "ingest_rw", "mcp_rw"]

# Pinned to what RDS ships for PostgreSQL 17 (checked 2026-10-29), and to the Compose/testcontainers image.
POSTGRES_IMAGE = "pgvector/pgvector:0.8.2-pg17@sha256:feb68f4f15446397d8cac7f4fe48fe4586de83160d1fc48b46283312d1a33966"
PGVECTOR_VERSION = "0.8.2"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["local", "test", "staging", "prod"] = "local"

    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "copilot"
    db_password_migrator: SecretStr
    db_password_app_rw: SecretStr
    db_password_tools_ro: SecretStr
    db_password_ingest_rw: SecretStr
    db_password_mcp_rw: SecretStr

    def dsn(self, role: Role, driver: str = "postgresql") -> str:
        """Connection URL for one role. Contains a password: never log it."""
        password: SecretStr = getattr(self, f"db_password_{role}")
        return (
            f"{driver}://{quote(role)}:{quote(password.get_secret_value(), safe='')}"
            f"@{self.db_host}:{self.db_port}/{quote(self.db_name)}"
        )
