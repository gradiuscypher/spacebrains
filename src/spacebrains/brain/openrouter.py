"""Minimal OpenRouter chat client with per-call cost tracking and a monthly budget guard."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from spacebrains.db import Database

log = logging.getLogger("spacebrains.openrouter")


class BudgetExceededError(Exception):
    pass


@dataclass(slots=True)
class ChatResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float

    def json(self) -> dict[str, Any]:
        return extract_json(self.text)


def month_start_ts() -> float:
    now = datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def extract_json(text: str) -> dict[str, Any]:
    """Tolerant JSON extraction: handles ```json fences and leading prose."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    return json.loads(text)


class OpenRouter:
    def __init__(self, api_key: str, base_url: str, db: Database) -> None:
        self._db = db
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=120,
            headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://github.com/gradiuscypher/spacebrains",
                "X-Title": "spacebrains",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def month_spend(self) -> float:
        return await self._db.usage_total("openrouter", month_start_ts())

    async def key_status(self) -> dict[str, Any]:
        """OpenRouter's own view of this key's usage/limit."""
        try:
            resp = await self._http.get("/key")
            resp.raise_for_status()
            return resp.json().get("data", {})
        except (httpx.HTTPError, ValueError) as e:
            return {"error": str(e)}

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        purpose: str,
        agent: str | None,
        budget_usd: float,
        max_tokens: int = 1500,
        temperature: float = 0.4,
        json_mode: bool = True,
        reasoning: str = "off",
    ) -> ChatResult:
        spent = await self.month_spend()
        if budget_usd > 0 and spent >= budget_usd:
            msg = f"monthly OpenRouter budget exhausted (${spent:.2f} >= ${budget_usd:.2f})"
            raise BudgetExceededError(msg)

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "usage": {"include": True},
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        # Hybrid reasoning models otherwise spend the whole output budget thinking.
        payload["reasoning"] = (
            {"enabled": False} if reasoning == "off" else {"effort": reasoning, "exclude": True}
        )

        t0 = time.monotonic()
        resp = await self._http.post("/chat/completions", json=payload)
        if resp.status_code >= 400:
            # Some models reject response_format; retry once without it.
            if resp.status_code in (400, 404):
                payload.pop("response_format", None)
                payload.pop("reasoning", None)
                resp = await self._http.post("/chat/completions", json=payload)
            if resp.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"openrouter {resp.status_code}: {resp.text[:400]}",
                    request=resp.request,
                    response=resp,
                )
        body = resp.json()
        if "error" in body:
            msg = f"openrouter error: {body['error']}"
            raise RuntimeError(msg)
        choice = body["choices"][0]
        text = choice["message"].get("content") or ""
        usage = body.get("usage", {})
        result = ChatResult(
            text=text,
            model=body.get("model", model),
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            cost_usd=float(usage.get("cost", 0.0)),
        )
        await self._db.add_usage(
            agent=agent,
            provider="openrouter",
            model=result.model,
            purpose=purpose,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=result.cost_usd,
        )
        log.info(
            "%s/%s: %d+%d tok $%.5f in %.1fs",
            result.model,
            purpose,
            result.input_tokens,
            result.output_tokens,
            result.cost_usd,
            time.monotonic() - t0,
        )
        return result
