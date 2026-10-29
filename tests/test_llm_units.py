import pytest
from pydantic import BaseModel

from app.llm.redact import redact
from app.llm.schema import strict_schema


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("mail pat.oneil+ops@airline.example.com now", "mail [EMAIL] now"),
        ("ring +1 (555) 123-4567", "ring [PHONE]"),
        ("ring 555.123.4567 or +44 20 7946 0958", "ring [PHONE] or [PHONE]"),
        ("ssn 123-45-6789", "ssn [SSN]"),
        ("card 4111 1111 1111 1111 on file", "card [CARD] on file"),
    ],
)
def test_redacts_personal_data(text: str, expected: str) -> None:
    assert redact(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "AD 2026-09-03 and AD 2025-12858 apply",
        "JASC 3230, SDR SCNA20250106001",
        "P/N 65C27571-3, S/N 892345, MX-007, N12345",
        "torque 25-30 in-lb per AC 43.13-1B chapter 7",
        "Boeing 737-800 (737823), 4111111111111112 is not a valid card",
        "effective 2026-10-29, 1,250 cycles",
    ],
)
def test_leaves_aviation_identifiers_alone(text: str) -> None:
    assert redact(text) == text


class Inner(BaseModel):
    x: int = 1


class Outer(BaseModel):
    name: str
    note: str | None = None
    inner: Inner


def test_strict_schema_closes_objects_and_requires_every_property() -> None:
    s = strict_schema(Outer)
    assert s["additionalProperties"] is False
    assert s["required"] == ["name", "note", "inner"]
    inner = s["$defs"]["Inner"]
    assert inner["additionalProperties"] is False and inner["required"] == ["x"]
    assert "default" not in inner["properties"]["x"]
