"""Runtime settings. Everything comes from the environment (or `.env` locally); nothing secret has a default."""

import hashlib
import json
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

    # LLM gateway (DESIGN §7.1). Model IDs and the endpoint are config only, so any OpenAI-compatible
    # endpoint works (PLAN Phase 8b).
    openrouter_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_timeout_s: float = 30.0
    llm_max_retries: int = 2  # SDK retries on 429 / 5xx / connection errors, honouring Retry-After
    chat_model: str = "anthropic/claude-haiku-5.5"
    fallback_model: str | None = "openai/gpt-6-luna"
    cheap_model: str = "anthropic/claude-haiku-5.5"
    embed_model: str = "voyageai/voyage-4-lite"
    rerank_model: str = "cohere/rerank-4-pro"
    # Rerank is billed per search, and /models reports $0 per token for it. Measured: SPIKE §2b.
    rerank_price_per_search: float = 0.0025
    # Provider pinning for reproducible evals: an `order` list plus allow_fallbacks=false (DESIGN §7.1).
    provider_order: list[str] | None = None

    # Spend limits (DESIGN §2). Checked against the llm_calls ledger before every call.
    run_cost_cap_usd: float = 0.25
    user_daily_cap_usd: float = 1.00
    global_daily_cap_usd: float = 5.00
    user_hash_salt: SecretStr = SecretStr("")

    def config_version(self) -> str:
        """Hash of the effective runtime config, stamped on ledger rows and traces (DESIGN §7.4).
        Secrets and connection details are excluded: they don't change behaviour."""
        exclude = {k for k in type(self).model_fields if "password" in k or k.startswith("db_")}
        exclude |= {"openrouter_api_key", "user_hash_salt", "env"}
        payload = json.dumps(self.model_dump(exclude=exclude), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:12]

    def dsn(self, role: Role, driver: str = "postgresql") -> str:
        """Connection URL for one role. Contains a password: never log it."""
        password: SecretStr = getattr(self, f"db_password_{role}")
        return (
            f"{driver}://{quote(role)}:{quote(password.get_secret_value(), safe='')}"
            f"@{self.db_host}:{self.db_port}/{quote(self.db_name)}"
        )
