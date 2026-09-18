# spacebrains

An autonomous [SpaceTraders.io](https://spacetraders.io) player that mixes two kinds of AI:

- **Long horizon — OpenRouter LLMs.** A *strategist* model reads a compact game summary every
  N minutes (or after a contract / ship purchase) and writes a JSON plan: goals, a fleet-growth
  request and per-ship role hints. Optional extra *thinking rounds* have a second, different
  *critic* model review the plan before the strategist revises it.
- **Short horizon — TypeSafe's Jev.** Every few seconds, typed System One questions decide the
  tactical stuff: which role each ship should take, whether a non-obvious contract is worth
  accepting, which market or trade route to use, whether to buy a ship *now*.
- **Code** owns everything with a known answer: navigation and fuel, cooldowns, contract math,
  arbitrage scoring, rate limiting, and the monthly LLM budget guard.

Multiple agents run in one process from a single SpaceTraders **account** token, and a local
web UI (bound to `0.0.0.0`) shows credits, ships, goals, plans, events, spend, and lets you tweak
models / thinking rounds / budgets globally or per agent.

## Quick start

```sh
cp .env.example .env          # fill in the three API keys
uv sync                       # backend deps
(cd frontend && npm install && npm run build)
uv run spacebrains            # http://0.0.0.0:8080
```

Then open the UI and **Add agent** (pick a symbol + faction). The account token registers the
agent; its agent token is stored in `data/spacebrains.sqlite3`. You can also *import* an
existing agent token.

Dev loop: `uv run spacebrains` for the API, and `cd frontend && npm run dev` for a hot-reloading
UI on :5173 (proxied to :8080).

## Layout

```
src/spacebrains/
  config.py        secrets/paths from .env          settings.py   UI-editable runtime settings
  db.py            SQLite (agents, goals, events, usage, markets)
  events.py        event bus → DB + SSE
  st/              SpaceTraders client (shared token-bucket limiter) + pydantic models
  brain/openrouter.py   chat client, cost tracking, budget guard
  brain/strategist.py   plan schema + strategist/critic rounds
  brain/jev.py          typed tactical questions (Choice / Noul)
  game/world.py    per-system knowledge, trade math
  game/pilot.py    per-ship behaviour loop (contract / mine / trade / scout / purchase)
  game/agent.py    per-agent supervisor: contracts, roles, replanning, fleet growth
  orchestrator.py  shared services + agent lifecycle
  api/app.py       FastAPI: JSON API, /api/stream (SSE), serves frontend/dist
frontend/          Vite + React + TS dashboard (Overview · Agent · Settings)
```

## Ship roles

| role | what the pilot does |
| --- | --- |
| `contract` | mine (or buy from the cheapest known exporter) the contract good, deliver, fulfill |
| `mine` | extract at the nearest engineered asteroid, sell at the best known market (Jev picks among the top 3) |
| `trade` | run the best known buy-low/sell-high route (Jev picks among candidates) |
| `scout` | visit the stalest marketplace, record prices + shipyard listings (probes do this for free) |
| `idle` | nothing |

Roles come from, in priority order: operator override (UI) → strategist `role_hints` → Jev
`assign_roles` (every 5 min, only overrides a strategist hint when confident) → code default.

## Cost controls

- `monthly_llm_budget_usd` (default $40): strategist calls stop once tracked OpenRouter spend
  passes it; the fleet keeps executing its last plan.
- `strategist_reasoning: off` by default — hybrid models otherwise burn the output budget thinking.
- Obvious decisions never hit a model (e.g. a minable contract with a week-long deadline is
  accepted in code). Jev calls cost ~$0.00002 each.
- The Settings page shows spend by model and OpenRouter's own view of the key's usage.

## Running unattended

- `deploy/spacebrains.service` — systemd unit (edit `User`/`WorkingDirectory`).
- `docker compose up -d --build` — container with `./data` mounted for the SQLite DB.
- The orchestrator polls the server every 10 min; when the universe **resets** (new `resetDate`)
  every agent token is dead, so it disables all agents and shows a banner in the UI. Acknowledge
  it, remove the stale agents, register new ones.
- Ships repair at a shipyard when hull/engine condition drops (opportunistically < 60%, by
  making the trip < 30%).

## Checks

```sh
uv run ruff check src tests && uv run ruff format --check src tests
uv run ty check src
uv run pytest
(cd frontend && npm run build)
```
