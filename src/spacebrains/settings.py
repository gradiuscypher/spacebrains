"""Runtime settings, editable from the web UI and persisted in the database.

Two levels: global `Settings` and optional per-agent `AgentOverrides`. `effective()` merges them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Settings(BaseModel):
    # --- Long-horizon brain (OpenRouter) ---
    strategist_model: str = Field(
        default="deepseek/deepseek-v4-flash",
        description="OpenRouter model that writes each agent's long-term goals.",
    )
    critic_model: str = Field(
        default="google/gemini-3.1-flash-lite",
        description="OpenRouter model that critiques the strategist's plan on extra thinking rounds.",
    )
    thinking_rounds: int = Field(
        default=1,
        ge=1,
        le=5,
        description="1 = strategist only. N>1 adds N-1 critique/revise rounds with the critic model.",
    )
    strategist_interval_minutes: float = Field(
        default=30,
        ge=1,
        le=1440,
        description="How often each agent re-plans its long-term goals.",
    )
    strategist_max_tokens: int = Field(default=2500, ge=200, le=8000)
    strategist_reasoning: Literal["off", "low", "medium", "high"] = Field(
        default="off",
        description="Reasoning effort for hybrid models. 'off' is cheapest; higher burns output tokens.",
    )
    monthly_llm_budget_usd: float = Field(
        default=40.0,
        ge=0,
        description="Strategist pauses (keeping current goals) once tracked OpenRouter spend passes this.",
    )

    # --- Short-horizon brain (TypeSafe Jev) ---
    jev_model: str = Field(default="jev-latest")
    jev_enabled: bool = Field(
        default=True,
        description="When off, ship roles and tactical choices fall back to code heuristics only.",
    )

    # --- Game loop ---
    tick_seconds: float = Field(default=5.0, ge=1, le=60, description="Per-agent loop cadence.")
    max_ships_to_buy: int = Field(
        default=6, ge=0, le=50, description="Fleet size cap the strategist may not exceed."
    )
    min_credit_reserve: int = Field(
        default=20_000, ge=0, description="Credits kept back from ship purchases and trade buys."
    )
    max_agents: int = Field(default=3, ge=1, le=20)
    paused: bool = Field(default=False, description="Pause every agent's game loop.")


class AgentOverrides(BaseModel):
    """Per-agent overrides; `None` means inherit the global value."""

    strategist_model: str | None = None
    critic_model: str | None = None
    thinking_rounds: int | None = Field(default=None, ge=1, le=5)
    strategist_interval_minutes: float | None = Field(default=None, ge=1, le=1440)
    jev_enabled: bool | None = None
    max_ships_to_buy: int | None = Field(default=None, ge=0, le=50)
    min_credit_reserve: int | None = Field(default=None, ge=0)
    paused: bool | None = None
    operator_notes: str = Field(
        default="",
        description="Free-text guidance from the operator, injected into the strategist prompt.",
    )


def effective(base: Settings, overrides: AgentOverrides | None) -> Settings:
    if overrides is None:
        return base
    data = base.model_dump()
    for key, value in overrides.model_dump(exclude={"operator_notes"}).items():
        if value is not None:
            data[key] = value
    return Settings.model_validate(data)
