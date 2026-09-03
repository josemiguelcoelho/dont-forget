from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    claim: str
    source: str
    observed_at: datetime
    confidence: float = Field(ge=0, le=1)


class Source(BaseModel):
    kind: Literal["url", "repository"]
    value: str
    observed_at: datetime


class Requirement(BaseModel):
    description: str
    status: Literal["unknown", "satisfied", "missing"] = "unknown"
    evidence: list[Evidence] = Field(default_factory=list)


class NextAction(BaseModel):
    description: str
    mode: Literal["user", "agent"]
    action_type: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    status: Literal["proposed", "completed", "failed"] = "proposed"


class Intention(BaseModel):
    id: str
    objective: str
    original_message: str
    status: Literal["active", "blocked", "completed", "abandoned"] = "active"
    sources: list[Source]
    deadline_at: datetime
    requirements: list[Requirement]
    current_state: str
    most_important_unresolved_requirement: str | None = None
    next_action: NextAction | None
    next_check_at: datetime | None
    confidence: float = Field(ge=0, le=1)
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    version: int = 1


class MessageContext(BaseModel):
    source_url: str
    repository: str


class RepositoryEvidence(BaseModel):
    repository: str
    is_public: bool
    has_demo: bool
    has_readme: bool
    has_useful_setup: bool
    setup_commands: list[str] = Field(default_factory=list)


class CheckAssessment(BaseModel):
    deadline_at: datetime
    deadline_near: bool
    repository: RepositoryEvidence
