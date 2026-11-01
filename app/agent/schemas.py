"""Agent schemas (docs/AGENT.md, DESIGN §6.3). Tool arguments, and the structured outputs of the diagnose and
draft steps. Graph state stores these as plain dicts (JSON-safe checkpoints; app/agent/graph.py) and they're
validated with these models at each step."""

from typing import Literal

from pydantic import BaseModel, Field

REGISTRATION = r"^MX-\d{3}$"  # fictional fleet only (DATA.md)


class SearchDocumentsArgs(BaseModel):
    """Hybrid search over the FAA AMT handbooks, AC 43.13-1B and Airworthiness Directives since 2020."""

    query: str = Field(min_length=3, max_length=500, description="What to look for, in technical terms")
    top_k: int = Field(default=5, ge=1, le=8, description="How many passages to return")


class GetAircraftArgs(BaseModel):
    """One fleet aircraft: type, engines, hours, cycles, and its latest work orders."""

    registration: str = Field(pattern=REGISTRATION)


class Citation(BaseModel):
    chunk_id: str = Field(description="The [chunk_id] of a passage returned by search_documents")
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
