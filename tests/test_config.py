from app.config import Settings

PASSWORDS = {f"db_password_{r}": "x" for r in ("migrator", "app_rw", "tools_ro", "ingest_rw", "mcp_rw")}


def test_dsn_quotes_passwords_with_reserved_characters() -> None:
    s = Settings(**{**PASSWORDS, "db_password_app_rw": "p@ss/w:rd#1"})
    assert s.dsn("app_rw") == "postgresql://app_rw:p%40ss%2Fw%3Ard%231@localhost:5432/copilot"


def test_secrets_never_appear_in_repr() -> None:
    s = Settings(**{**PASSWORDS, "db_password_migrator": "hunter2-secret"})
    assert "hunter2-secret" not in repr(s)
    assert "hunter2-secret" not in str(s.model_dump())
