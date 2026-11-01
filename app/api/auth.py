"""Who is calling. JWT verification and the role -> access-level mapping arrive with #13. Until then a
*dev principal* stands in, and it's only allowed when ENV is local or test: create_app() refuses to start
anywhere else (DESIGN §7.3, security review S9)."""

from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Header

from app.config import Settings
from app.retrieval.search import AccessLevel

Role = Literal["technician", "supervisor", "admin"]
DEV_ENVS = {"local", "test"}


@dataclass(frozen=True)
class Principal:
    user_id: str
    role: Role
    access_levels: tuple[AccessLevel, ...]


class DevAuthNotAllowed(RuntimeError):
    pass


def check_dev_auth_allowed(settings: Settings) -> None:
    if settings.env not in DEV_ENVS:
        raise DevAuthNotAllowed(f"dev principal is not allowed with ENV={settings.env}; JWT auth comes with #13")


async def dev_principal(
    x_dev_user: Annotated[str, Header(max_length=64)] = "dev-user",
    x_dev_role: Annotated[Role, Header()] = "technician",
) -> Principal:
    """Local only. The user id feeds per-user spend limits, the ledger's salted user hash, run ownership and
    the approver identity. The role decides who may decide on runs (DESIGN §6.3). With JWT (#13), both come
    from the verified token and nothing from headers."""
    return Principal(user_id=x_dev_user, role=x_dev_role, access_levels=("public",))


def can_decide(p: Principal) -> bool:
    return p.role in ("supervisor", "admin")
