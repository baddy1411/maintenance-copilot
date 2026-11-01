"""Who is calling (DESIGN §7.3, docs/auth.md).

Every request except /healthz needs `Authorization: Bearer <JWT>`:
  - the signature is checked against the configured public key with an algorithm allow-list (default ES256),
    so "alg: none" tokens and HMAC tokens signed with the public key as the secret are refused;
  - `exp`, `iat`, `sub`, `iss` and `aud` are required, issuer and audience must match, 30 s leeway on time claims;
  - `role` must be one of `role_access`, which also sets the principal's document access levels.
A bad token gets 401 with `WWW-Authenticate: Bearer error="invalid_token"`. A valid token without the role a
route needs gets 403.

Dev issuer: locally, `copilot dev-keys` writes an EC P-256 key to `dev_jwt_key_file`, and
`copilot dev-token --user --role` signs tokens with it. The app derives the verifying key from the same file.
Outside ENV=local/test, check_auth_config() refuses to start if a dev key is configured or if no public key is,
so a dev issuer can't reach production (security review S9).
"""

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings
from app.retrieval.search import AccessLevel

Role = Literal["technician", "supervisor", "admin"]
DEV_ENVS = {"local", "test"}
DEV_ISSUER = "maintenance-copilot-dev"
bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    user_id: str
    role: Role
    access_levels: tuple[AccessLevel, ...]


class AuthConfigError(RuntimeError):
    pass


def generate_dev_key(path: Path) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    path.parent.mkdir(parents=True, exist_ok=True)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())  # fmt: skip
    path.write_bytes(pem)
    path.chmod(0o600)


def _dev_private_key(settings: Settings) -> ec.EllipticCurvePrivateKey:
    assert settings.dev_jwt_key_file is not None
    key = serialization.load_pem_private_key(settings.dev_jwt_key_file.read_bytes(), password=None)
    return cast(ec.EllipticCurvePrivateKey, key)


def public_key(settings: Settings) -> Any:
    """The key tokens are verified with: the configured PEM, or (local/test only) one derived from the dev key."""
    if settings.jwt_public_key:
        return serialization.load_pem_public_key(settings.jwt_public_key.replace("\\n", "\n").encode())
    if settings.dev_jwt_key_file and settings.env in DEV_ENVS:
        return _dev_private_key(settings).public_key()
    raise AuthConfigError("no JWT public key configured (set JWT_PUBLIC_KEY)")


def check_auth_config(settings: Settings) -> Any:
    """Run at startup. Returns the verifying key, or raises."""
    if settings.env not in DEV_ENVS:
        if settings.dev_jwt_key_file is not None:
            raise AuthConfigError(f"the dev token issuer is not allowed with ENV={settings.env}")
        if settings.jwt_issuer == DEV_ISSUER:
            raise AuthConfigError(f"set JWT_ISSUER to your identity provider with ENV={settings.env}")
        if len(settings.user_hash_salt.get_secret_value()) < 16:
            # An empty salt makes the ledger's and limits' "pseudonymous" user ids plain hashes of user ids.
            raise AuthConfigError(f"set USER_HASH_SALT (at least 16 characters) with ENV={settings.env}")
    algs = {a.lower() for a in settings.jwt_algorithms}
    if "none" in algs or any(a.startswith("hs") for a in algs):
        raise AuthConfigError("JWT_ALGORITHMS must be asymmetric (no 'none', no HMAC)")
    return public_key(settings)


def mint_dev_token(settings: Settings, user: str, role: Role, ttl_s: int = 8 * 3600) -> str:
    """Local/test only: a token from the dev issuer."""
    if settings.env not in DEV_ENVS or settings.dev_jwt_key_file is None:
        raise AuthConfigError("dev tokens need ENV=local/test and DEV_JWT_KEY_FILE")
    now = int(time.time())
    claims = {"sub": user, "role": role, "iss": settings.jwt_issuer, "aud": settings.jwt_audience,
              "iat": now, "nbf": now, "exp": now + ttl_s}  # fmt: skip
    return jwt.encode(claims, _dev_private_key(settings), algorithm="ES256")


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(401, detail, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})


async def current_principal(
    request: Request, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
) -> Principal:
    if creds is None:
        raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    s: Settings = request.app.state.settings
    try:
        claims = jwt.decode(
            creds.credentials, request.app.state.jwt_key, algorithms=s.jwt_algorithms, audience=s.jwt_audience,
            issuer=s.jwt_issuer, leeway=s.jwt_leeway_s, options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )  # fmt: skip
    except jwt.PyJWTError as exc:
        raise _unauthorized(f"invalid token: {type(exc).__name__}") from None
    role, sub = claims.get("role"), claims.get("sub")
    if role not in s.role_access or not isinstance(sub, str) or not 0 < len(sub) <= 128:
        raise _unauthorized("invalid token: role or subject")
    levels = tuple(cast(AccessLevel, lv) for lv in s.role_access[role])
    return Principal(user_id=sub, role=cast(Role, role), access_levels=levels)


def can_decide(p: Principal) -> bool:
    return p.role in ("supervisor", "admin")


async def supervisor(p: Annotated[Principal, Depends(current_principal)]) -> Principal:
    if not can_decide(p):
        raise HTTPException(403, "supervisors only")
    return p


async def admin(p: Annotated[Principal, Depends(current_principal)]) -> Principal:
    if p.role != "admin":
        raise HTTPException(403, "admins only")
    return p
