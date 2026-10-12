"""Answer schemas (spec M10).

LLM-facing models have no defaults (strict structured outputs require every field). Every displayable
text field carries its own citations so it can be validated (M11).
"""

from typing import Literal

from pydantic import BaseModel, Field


# ------------------------------------------------------------------ query analysis (fast model)
class QueryPlan(BaseModel):
    intent: Literal["troubleshoot", "spec_lookup", "procedure", "history", "maintenance", "warranty", "parts",
                    "admin", "other"]
    equipment_models: list[str] = Field(description="Equipment models named or implied, e.g. HCP-500, VRS-110")
    fault_codes: list[str] = Field(description="Controller fault codes mentioned, e.g. E-31, P-07, L-03")
    needs_history: bool = Field(description="True if the question asks about past events on a unit")
    needs_procedure: bool = Field(description="True if the question asks how to do a task")
    safety_topics: list[Literal["hv", "refrigerant", "confined_space"]] = Field(
        description="Certified-work topics the question touches: work inside VFD/electrical cabinets (hv), "
                    "refrigerant circuit work (refrigerant), pit/confined space entry (confined_space)")
    rewrites: list[str] = Field(description="Two short search queries with synonyms for the same need")


# ------------------------------------------------------------------ answer (answer model)
class CitedText(BaseModel):
    text: str
    citations: list[str] = Field(description="Source ids like S3 or F1 that support this text")


class Block(BaseModel):
    type: Literal["fact", "step", "warning", "history", "note"]
    text: str
    citations: list[str]


class ConflictPosition(BaseModel):
    text: str
    citations: list[str]


class Conflict(BaseModel):
    conflict_id: str = Field(description="Id of a conflict listed under CONFLICTS, e.g. C5")
    topic: str
    positions: list[ConflictPosition]
    resolution: CitedText


class RestrictedNotice(BaseModel):
    what_you_can_do: list[CitedText]


class CitedValue(BaseModel):
    value: str
    citations: list[str]


class PartHint(BaseModel):
    part_no: str
    qty: int
    citations: list[str]


class DocRefHint(BaseModel):
    doc_number: str
    citations: list[str]


class WorkOrderHint(BaseModel):
    fault_code: CitedValue | None
    summary: CitedText | None
    observations: CitedText | None
    parts: list[PartHint]
    procedure_refs: list[DocRefHint]
    work_steps: list[CitedText]
    estimated_duration_h: CitedValue | None


class FollowUp(BaseModel):
    needed: bool
    reasons: list[Literal["parts required", "certified technician required", "OEM campaign", "return visit",
                          "escalation"]]
    work_order_hint: WorkOrderHint | None


class AnswerJSON(BaseModel):
    status: Literal["answered", "partial", "not_covered", "restricted", "clarify"]
    headline: CitedText
    blocks: list[Block]
    conflicts: list[Conflict]
    restricted_notice: RestrictedNotice | None
    clarify_question: CitedText | None
    follow_up: FollowUp
    confidence: Literal["high", "medium", "low"]
