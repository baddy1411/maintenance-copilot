"""Agent schemas (docs/AGENT.md, DESIGN §6.3). Tool arguments, and the structured outputs of the diagnose and
draft steps. Graph state stores these as plain dicts (JSON-safe checkpoints; app/agent/graph.py) and they're
validated with these models at each step."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator

REGISTRATION = r"^MX-\d{3}$"  # fictional fleet only (DATA.md)


class SearchDocumentsArgs(BaseModel):
    """Hybrid search over the FAA AMT handbooks, AC 43.13-1B and Airworthiness Directives since 2020."""

    query: str = Field(min_length=3, max_length=500, description="What to look for, in technical terms")
    top_k: int = Field(default=5, ge=1, le=8, description="How many passages to return")


class GetAircraftArgs(BaseModel):
    """One fleet aircraft: type, engines, hours, cycles, and its latest work orders."""

    registration: str = Field(pattern=REGISTRATION)


class QuerySdrArgs(BaseModel):
    """FAA Service Difficulty Reports, 2024 to now: what operators reported going wrong on their aircraft.
    `list` returns matching reports, newest first. The count modes tell how often something happens: the
    most common JASC codes, parts, or reports per month."""

    mode: Literal["list", "count_by_jasc", "count_by_part", "count_by_month"]
    aircraft: str | None = Field(
        default=None, max_length=60,
        description="Model or family as people say it: '737-800', '737' (all 737s), 'A320', 'CRJ900', 'E175'",
    )  # fmt: skip
    jasc: str | None = Field(default=None, pattern=r"^\d{2,4}$",
                             description="JASC/ATA code or its prefix: '32' landing gear, '3242' brakes")  # fmt: skip
    text: str | None = Field(default=None, min_length=3, max_length=200,
                             description="Words to find in the report narrative, e.g. 'brake overheat'")  # fmt: skip
    part: str | None = Field(default=None, min_length=2, max_length=60, description="Part name contains, e.g. 'valve'")
    date_from: date | None = None
    date_to: date | None = None
    limit: int = Field(default=10, ge=1, le=20, description="Reports (list) or groups (counts) to return")

    @model_validator(mode="after")
    def _needs_a_filter(self) -> "QuerySdrArgs":
        if not any((self.aircraft, self.jasc, self.text, self.part)):
            raise ValueError("give at least one of aircraft, jasc, text or part")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from is after date_to")
        return self


class Citation(BaseModel):
    chunk_id: str = Field(description="The [id] of a passage from search_documents or a report from query_sdr")
    quote: str = Field(min_length=10, max_length=300, description="Exact words copied from that passage")


class Diagnosis(BaseModel):
    likely_causes: list[str] = Field(min_length=1, max_length=5)
    confidence: Literal["low", "medium", "high"]
    reasoning: str = Field(max_length=1500)
    citations: list[Citation] = Field(min_length=1, max_length=8)
    evidence_incomplete: bool
    safety_notes: list[str] = Field(max_length=5, description="E.g. a possibly relevant AD and what it requires")


class WorkOrderDraft(BaseModel):
    registration: str = Field(pattern=REGISTRATION)
    title: str = Field(min_length=5, max_length=120)
    priority: Literal["low", "medium", "high", "urgent"]
    diagnosis_summary: str = Field(max_length=1500)
    steps: list[str] = Field(min_length=1, max_length=20)
    citations: list[Citation] = Field(min_length=1, max_length=8)


PRIORITY_ORDER = ["low", "medium", "high", "urgent"]
