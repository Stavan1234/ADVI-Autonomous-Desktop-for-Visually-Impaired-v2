from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Intent(BaseModel):
    goal: str = ""
    entities: dict[str, Any] = Field(
        default_factory=dict
    )
    constraints: list[str] = Field(
        default_factory=list
    )
    confirmation_required: bool = False
    missing_information: list[str] = Field(
        default_factory=list
    )