"""Real tokens for tests: one EC P-256 key pair per session, the same verification path as production."""

import time
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import SecretStr

from app.config import Settings

KEY = ec.generate_private_key(ec.SECP256R1())
PUBLIC_PEM = KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode()  # fmt: skip
ISSUER, AUDIENCE = "https://idp.test", "maintenance-copilot"


def with_auth(s: Settings, **extra: Any) -> Settings:
    """Settings that verify this module's tokens. model_copy skips validation, so secrets must be SecretStr."""
    return s.model_copy(update={"env": "test", "jwt_public_key": PUBLIC_PEM, "jwt_issuer": ISSUER,
                                "jwt_audience": AUDIENCE, "dev_jwt_key_file": None,
                                "user_hash_salt": SecretStr("test-salt-0123456789"), **extra})  # fmt: skip


def token(sub: str = "tech-1", role: str = "technician", key: Any = KEY, alg: str = "ES256", **claims: Any) -> str:
    now = int(time.time())
    body = {"sub": sub, "role": role, "iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 600} | claims
    return jwt.encode({k: v for k, v in body.items() if v is not None}, key, algorithm=alg)


def bearer(sub: str = "tech-1", role: str = "technician", **claims: Any) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(sub, role, **claims)}"}
