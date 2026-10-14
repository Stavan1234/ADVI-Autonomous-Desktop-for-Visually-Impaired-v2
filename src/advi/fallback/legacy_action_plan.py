"""Legacy Action/ActionPlan contracts owned by the isolated David subsystem.

The native ADVI runtime uses :mod:`advi.tools.contracts` instead. These models
exist only because David's internal planner/execution loop still uses the older
plan vocabulary.
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, ConfigDict


class VerificationStatus(str, Enum):
    VERIFIED = "verified"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class PlanStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"


class Action(BaseModel):
    model_config = ConfigDict(extra="allow")

    action: str
    target: str | None = None
    focus: str | None = None
    reason: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)


class ActionPlan(BaseModel):
    model_config = ConfigDict(extra="allow")

    goal: str = ""
    actions: list[Action] = Field(default_factory=list)
    status: PlanStatus = PlanStatus.PENDING
    reason: str | None = None
    missing_information: list[str] = Field(default_factory=list)
    confirmation_required: bool = False
    confirmation_prompt: str = ""
    created_at: float = Field(default_factory=time.time)


class ExecutionResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    action: str
    success: bool
    data: Any = None
    human_readable: str | None = None
    error: str | None = None
    verified: bool = False
    verification_status: VerificationStatus = VerificationStatus.UNCERTAIN
    verification_details: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    executed_at: float = Field(default_factory=time.time)


__all__ = [
    "Action",
    "ActionPlan",
    "ExecutionResult",
    "PlanStatus",
    "VerificationStatus",
]
