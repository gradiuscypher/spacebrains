"""Short-horizon decisions via TypeSafe's Jev (System One).

Jev returns typed, calibrated judgments instead of text, so it is cheap and fast enough to be
consulted every tick. Code owns the workflow and the candidate sets; Jev picks among them.
"""

from __future__ import annotations

import logging
from typing import Any

from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, RetryPolicy, TypeSafeError

from spacebrains.db import Database

log = logging.getLogger("spacebrains.jev")

JEV_USD_PER_INPUT_TOKEN = 42 / 1e9  # $42 per billion input tokens; output is free

ROLE_CRITERIA: dict[str, str] = {
    "contract": (
        "Work the active contract: mine or buy the required goods and deliver them to the "
        "contract destination."
    ),
    "mine": "Extract ore at an asteroid and sell it at the best nearby market.",
    "trade": "Run the best known buy-low/sell-high route between markets in the system.",
    "haul": (
        "Shuttle: wait in orbit at the mining site, collect cargo from the miners so they never "
        "stop extracting, then sell it (or deliver contract goods) and return."
    ),
    "scout": "Visit marketplaces to refresh price data (ideal for probes and idle ships).",
    "idle": "Stay put and do nothing this cycle.",
}


class JevBrain:
    def __init__(self, api_key: str, db: Database, model: str = "jev-latest") -> None:
        self._db = db
        self.model = model
        self._client = AsyncTypeSafeClient(
            api_key=api_key, model=model, retry=RetryPolicy(max_retries=3)
        )

    async def aclose(self) -> None:
        await self._client.__aexit__(None, None, None)

    async def _ask(
        self,
        state: dict[str, Any],
        questions: dict[str, Any],
        *,
        agent: str,
        purpose: str,
    ) -> Any | None:
        try:
            resp = await self._client.system_one(state, questions, model=self.model)
        except TypeSafeError as e:
            log.warning("jev %s failed: %s", purpose, e)
            return None
        in_tok = resp.usage.input_tokens or 0
        out_tok = resp.usage.output_tokens or 0
        rows: list[tuple[str, str, str, float | None, int]] = []
        for qid, q in questions.items():
            instr = getattr(q, "instructions", "")
            question = f"{qid}: {instr}" if isinstance(instr, str) else qid
            if qid in resp.choices:
                a = resp.choices[qid]
                rows.append((purpose, question, a.choice, float(a.confidence), in_tok))
            elif qid in resp.nouls:
                rows.append((purpose, question, f"p(yes)={resp.nouls[qid].noul:.2f}", None, in_tok))
            elif qid in resp.scores:
                sc = resp.scores[qid]
                rows.append((purpose, question, f"{sc.score:.2f}", float(sc.confidence), in_tok))
        if rows:
            await self._db.add_decisions(agent, rows)
        await self._db.add_usage(
            agent=agent,
            provider="typesafe",
            model=resp.model,
            purpose=purpose,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=in_tok * JEV_USD_PER_INPUT_TOKEN,
        )
        return resp

    # ------------------------------------------------------------ decisions
    async def assign_roles(
        self,
        *,
        agent: str,
        fleet: list[dict[str, Any]],
        goals: list[dict[str, Any]],
        context: dict[str, Any],
        allowed: dict[str, list[str]],
    ) -> dict[str, tuple[str, float]]:
        """One request; one Choice question per ship, restricted to the roles that ship can do.

        Returns {ship: (role, confidence)}. Missing ships fall back to the caller's heuristic.
        """
        state = {
            "fleet": fleet,
            "long_term_goals": goals,
            "situation": context,
            "rules": [
                "Prioritise the contract while `situation.contract_economics.worth_it` is true; "
                "if the market pays clearly more for the same goods, mine-and-sell instead.",
                "Probes cannot carry cargo or mine, they should scout markets.",
                "A cargo ship without a mining laser is most valuable as a haul shuttle when "
                "there are two or more miners; otherwise as a trader.",
                "Trading needs fresh price data with a real spread; otherwise mining is safer.",
                "Do not leave every ship idle when there is any productive option.",
            ],
        }
        questions: dict[str, Any] = {}
        for ship in fleet:
            sym = ship["symbol"]
            options = allowed.get(sym) or ["idle"]
            if len(options) == 1:
                continue
            questions[sym] = Choice(
                instructions=(
                    f"Given the fleet, the long-term goals and the situation, which role should "
                    f"ship `fleet[symbol={sym}]` take for the next few minutes?"
                ),
                criteria={r: ROLE_CRITERIA[r] for r in options},
            )
        if not questions:
            return {}
        resp = await self._ask(state, questions, agent=agent, purpose="assign_roles")
        if resp is None:
            return {}
        return {sym: (a.choice, a.confidence) for sym, a in resp.choices.items()}

    async def choose_option(
        self,
        *,
        agent: str,
        purpose: str,
        instructions: str,
        situation: dict[str, Any],
        options: dict[str, str],
    ) -> tuple[str, float] | None:
        """Generic single Choice over code-generated candidates."""
        if len(options) == 1:
            return next(iter(options)), 1.0
        resp = await self._ask(
            {"situation": situation},
            {"pick": Choice(instructions=instructions, criteria=options)},
            agent=agent,
            purpose=purpose,
        )
        if resp is None:
            return None
        a = resp.choices["pick"]
        return a.choice, a.confidence

    async def judge(
        self,
        *,
        agent: str,
        purpose: str,
        instructions: str,
        situation: dict[str, Any],
        yes: str,
        no: str,
    ) -> float | None:
        resp = await self._ask(
            {"situation": situation},
            {"q": Noul(instructions=instructions, criteria={"true": yes, "false": no})},
            agent=agent,
            purpose=purpose,
        )
        return None if resp is None else float(resp.nouls["q"].noul)
