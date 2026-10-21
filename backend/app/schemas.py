"""请求/响应模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class RunCreate(BaseModel):
    skill_id: str
    title: str = ""
    brief: dict[str, Any] = Field(default_factory=dict)
    channel: Literal["fast", "slow"] = "fast"
    preset_id: str | None = None
    autostart: bool = True


class GateDecisionIn(BaseModel):
    status: Literal["approved", "rejected", "revised"] = "approved"
    feedback: str = ""
    patches: dict[str, Any] = Field(default_factory=dict)


class RunOut(BaseModel):
    id: str
    skill_id: str
    skill_name: str = ""
    title: str = ""
    status: str
    channel: str = "fast"
    progress: float = 0.0
    current_stage_id: str | None = None
    tokens_used: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    created_at: str | None = None
