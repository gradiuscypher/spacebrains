"""Long-horizon planning with OpenRouter models.

Round 1: the strategist model turns a compact game summary into a JSON plan (goals, fleet
growth, role hints). Rounds 2..N: a critic model reviews the plan and the strategist revises.
Every round is one cheap chat call; the plan is validated with pydantic before it is applied.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from spacebrains.brain.openrouter import ChatResult, OpenRouter

log = logging.getLogger("spacebrains.strategist")

GoalKind = Literal[
    "fulfill_contract",
    "grow_fleet",
    "accumulate_credits",
    "explore_markets",
    "trade_route",
    "mine_and_sell",
    "custom",
]


class Goal(BaseModel):
    kind: GoalKind
    description: str = Field(max_length=300)
    priority: int = Field(default=5, ge=1, le=10, description="1 = most important")
    params: dict[str, Any] = Field(default_factory=dict)


class ShipPurchase(BaseModel):
    ship_type: str
    count: int = Field(default=1, ge=1, le=5)
    reason: str = ""
    when_credits_above: int = Field(default=0, ge=0)


class Plan(BaseModel):
    assessment: str = Field(default="", max_length=1200)
    goals: list[Goal] = Field(default_factory=list, max_length=8)
    ship_purchase: ShipPurchase | None = None
    role_hints: dict[str, Literal["contract", "mine", "trade", "scout", "idle"]] = Field(
        default_factory=dict
    )
    notes_for_next_time: str = Field(default="", max_length=600)


class Critique(BaseModel):
    verdict: Literal["approve", "revise"] = "approve"
    issues: list[str] = Field(default_factory=list, max_length=6)
    suggestions: list[str] = Field(default_factory=list, max_length=6)


SYSTEM_PROMPT = """You are the strategist for an autonomous fleet in SpaceTraders.io, an API space
trading game. You do NOT act; you set goals that a tactical layer executes every few seconds.

Game facts:
- Ships must be IN_ORBIT to navigate/extract and DOCKED to trade/refuel. Travel costs fuel.
- Contracts: accept -> deliver units of a good to a waypoint -> fulfill for a large payout.
  Contract goods can be mined (ores like IRON_ORE, COPPER_ORE, ALUMINUM_ORE at asteroids) or bought
  at a market that exports them. Contracts have deadlines.
- Mining ships need MOUNT_MINING_LASER; probes (FRAME_PROBE) carry no cargo but travel for free,
  so they are perfect market scouts. Markets only show prices when one of our ships is there.
- Ship types buyable at shipyards: SHIP_MINING_DRONE (~cheap, mines), SHIP_LIGHT_HAULER (cargo),
  SHIP_PROBE (scout), SHIP_ORE_HOUND, SHIP_SIPHON_DRONE, SHIP_LIGHT_SHUTTLE. Prices vary by
  shipyard; only buy what the credits comfortably allow while keeping a reserve.
- More mining drones early = faster compounding; haulers matter once trade spreads are known.

Output ONLY a JSON object of this shape:
{
  "assessment": "2-4 sentences on the situation",
  "goals": [
    {"kind": "fulfill_contract|grow_fleet|accumulate_credits|explore_markets|trade_route|mine_and_sell|custom",
     "description": "specific, checkable", "priority": 1-10, "params": {}}
  ],
  "ship_purchase": {"ship_type": "SHIP_MINING_DRONE", "count": 1, "reason": "", "when_credits_above": 0} or null,
  "role_hints": {"SHIP-SYMBOL": "contract|mine|trade|scout|idle"},
  "notes_for_next_time": "short memo to your future self"
}
Keep it concise. At most 6 goals. Use real ship symbols and waypoint symbols from the summary."""

CRITIC_PROMPT = """You review strategy plans for a SpaceTraders.io fleet. Be terse and concrete.
Look for: unaffordable purchases, ignored contracts, goals the fleet cannot execute (e.g. mining
with a probe), missing scouting when no market data exists, or vague goals.
Output ONLY JSON: {"verdict": "approve|revise", "issues": [..], "suggestions": [..]}"""


class Strategist:
    def __init__(self, router: OpenRouter) -> None:
        self._router = router

    async def plan(
        self,
        *,
        agent: str,
        summary: dict[str, Any],
        strategist_model: str,
        critic_model: str,
        rounds: int,
        budget_usd: float,
        max_tokens: int,
        reasoning: str = "off",
        operator_notes: str = "",
        previous_notes: str = "",
    ) -> tuple[Plan | None, list[str], float]:
        """Returns (plan or None if unparseable, models_used, total_cost)."""
        summary_text = json.dumps(summary, separators=(",", ":"))
        user = f"GAME SUMMARY:\n{summary_text}"
        if previous_notes:
            user += f"\n\nYOUR NOTES FROM LAST TIME:\n{previous_notes}"
        if operator_notes:
            user += f"\n\nOPERATOR GUIDANCE (must follow):\n{operator_notes}"

        messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]
        models: list[str] = []
        cost = 0.0

        result = await self._router.chat(
            model=strategist_model,
            messages=messages,
            purpose="strategist",
            agent=agent,
            budget_usd=budget_usd,
            max_tokens=max_tokens,
            reasoning=reasoning,
        )
        models.append(result.model)
        cost += result.cost_usd
        plan = _parse_plan(result)
        if plan is None:
            return None, models, cost

        for _ in range(1, rounds):
            critique_res = await self._router.chat(
                model=critic_model,
                messages=[
                    {"role": "system", "content": CRITIC_PROMPT},
                    {
                        "role": "user",
                        "content": f"{user}\n\nPROPOSED PLAN:\n{plan.model_dump_json()}",
                    },
                ],
                purpose="critic",
                agent=agent,
                budget_usd=budget_usd,
                max_tokens=600,
                temperature=0.2,
            )
            models.append(critique_res.model)
            cost += critique_res.cost_usd
            try:
                critique = Critique.model_validate(critique_res.json())
            except (ValidationError, ValueError) as e:
                log.warning("critic output unusable: %s", e)
                break
            if critique.verdict == "approve" and not critique.issues:
                break
            messages.append({"role": "assistant", "content": plan.model_dump_json()})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "A reviewer raised these issues: "
                        + json.dumps(critique.issues)
                        + "\nSuggestions: "
                        + json.dumps(critique.suggestions)
                        + "\nRevise the plan. Output ONLY the JSON object."
                    ),
                }
            )
            result = await self._router.chat(
                model=strategist_model,
                messages=messages,
                purpose="strategist_revise",
                agent=agent,
                budget_usd=budget_usd,
                max_tokens=max_tokens,
                reasoning=reasoning,
            )
            models.append(result.model)
            cost += result.cost_usd
            revised = _parse_plan(result)
            if revised is None:
                break  # keep the last good plan
            plan = revised

        return plan, models, cost


def _parse_plan(result: ChatResult) -> Plan | None:
    try:
        return Plan.model_validate(result.json())
    except (ValidationError, ValueError) as e:
        log.warning("strategist output unusable (%s): %r", e, result.text[:300])
        return None
