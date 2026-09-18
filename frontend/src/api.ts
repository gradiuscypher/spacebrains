// Thin typed client for the spacebrains backend.

export interface Settings {
  strategist_model: string
  critic_model: string
  thinking_rounds: number
  strategist_interval_minutes: number
  strategist_max_tokens: number
  strategist_reasoning: string
  monthly_llm_budget_usd: number
  jev_model: string
  jev_enabled: boolean
  tick_seconds: number
  allow_ship_purchases: boolean
  max_ships_to_buy: number
  min_credit_reserve: number
  max_agents: number
  paused: boolean
}

export interface SchemaField {
  name: string
  type: 'string' | 'integer' | 'number' | 'boolean'
  description: string
  default: unknown
  minimum?: number | null
  maximum?: number | null
  enum?: string[] | null
}

export interface Ship {
  symbol: string
  role: string
  role_source: string
  status: string
  target: string | null
  frame: string
  ship_role: string
  waypoint: string
  nav_status: string
  flight_mode: string
  arrival_in: number
  fuel: { current: number; capacity: number }
  cargo: { units: number; capacity: number; inventory: Record<string, number> }
  cooldown: number
  condition: number
  pin: { role: string; until_credits?: number; until_ts?: number } | null
  can_mine: boolean
  can_siphon: boolean
  last_error: string | null
}

export interface ContractSummary {
  id: string
  type: string
  accepted: boolean
  fulfilled: boolean
  deadline: string
  payment: number
  deliver: string[]
}

export interface Goal {
  kind: string
  description: string
  priority: number
  params: Record<string, unknown>
  status?: string
  id?: number
  horizon?: string
}

export interface Plan {
  assessment: string
  goals: Goal[]
  ship_purchase: { ship_type: string; count: number; reason: string; when_credits_above: number } | null
  role_hints: Record<string, string>
  notes_for_next_time: string
}

export interface Overrides {
  strategist_model: string | null
  critic_model: string | null
  thinking_rounds: number | null
  strategist_interval_minutes: number | null
  jev_enabled: boolean | null
  max_ships_to_buy: number | null
  min_credit_reserve: number | null
  paused: boolean | null
  operator_notes: string
}

export interface AgentSnapshot {
  symbol: string
  faction: string
  headquarters: string
  enabled: boolean
  paused: boolean
  credits: number
  starting_credits: number
  ships: Ship[]
  contracts: ContractSummary[]
  plan: Plan | null
  plan_ts: number
  plan_error: string | null
  purchase_pending: boolean
  overrides: Overrides
  effective_settings: Settings
  requests: number
}

export interface AgentDetail extends AgentSnapshot {
  goals: Goal[]
  strategist_runs: {
    id: number
    ts: number
    rounds: number
    models: string[]
    plan: Plan
    cost_usd: number
  }[]
  snapshots: { ts: number; credits: number; ships: number }[]
  trades: Trade[]
  decisions: Decision[]
  overrides_schema: SchemaField[]
}

export interface Decision {
  id: number
  ts: number
  purpose: string
  question: string
  answer: string
  confidence: number | null
  input_tokens: number
}

export interface Trade {
  id: number
  ts: number
  ship: string
  good: string
  buy_at: string
  sell_at: string
  units: number
  cost: number
  revenue: number
  predicted_margin: number
  seconds: number
}

export interface UsageRow {
  provider: string
  model: string
  calls: number
  input_tokens: number
  output_tokens: number
  cost_usd: number
}

export interface Overview {
  settings: Settings
  agents: AgentSnapshot[]
  usage: { month_openrouter_usd: number; month_typesafe_usd: number; by_model: UsageRow[] }
  api_rate: { limit_per_second: number; per_agent_last_minute: Record<string, number> }
  server: { reset_date?: string; next_reset?: string; version?: string; announcements?: string[]; checked_at?: number }
  reset_detected: string | null
}

export interface GameEvent {
  id?: number
  ts: number
  agent: string | null
  ship: string | null
  kind: string
  message: string
  data: Record<string, unknown>
}

export interface ModelInfo {
  id: string
  prompt_per_m: number
  completion_per_m: number
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { 'content-type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      detail = (await res.json()).detail ?? detail
    } catch {
      /* ignore */
    }
    throw new Error(detail)
  }
  return (await res.json()) as T
}

export const api = {
  overview: () => req<Overview>('/api/overview'),
  settings: () => req<{ values: Settings; schema: SchemaField[] }>('/api/settings'),
  patchSettings: (patch: Partial<Settings>) =>
    req<Settings>('/api/settings', { method: 'PATCH', body: JSON.stringify(patch) }),
  models: () => req<{ models: ModelInfo[] }>('/api/models'),
  usage: () => req<{ by_model: UsageRow[]; openrouter_key: Record<string, unknown> }>('/api/usage'),
  agent: (symbol: string) => req<AgentDetail>(`/api/agents/${symbol}`),
  register: (symbol: string, faction: string) =>
    req<AgentSnapshot>('/api/agents', { method: 'POST', body: JSON.stringify({ symbol, faction }) }),
  importAgent: (token: string) =>
    req<AgentSnapshot>('/api/agents/import', { method: 'POST', body: JSON.stringify({ token }) }),
  patchOverrides: (symbol: string, patch: Partial<Overrides>) =>
    req<Overrides>(`/api/agents/${symbol}/overrides`, { method: 'PATCH', body: JSON.stringify(patch) }),
  setEnabled: (symbol: string, enabled: boolean) =>
    req(`/api/agents/${symbol}/enabled`, { method: 'POST', body: JSON.stringify({ enabled }) }),
  replan: (symbol: string) => req(`/api/agents/${symbol}/replan`, { method: 'POST' }),
  setRole: (symbol: string, ship: string, role: string, opts: { until_credits?: number; until_minutes?: number } = {}) =>
    req(`/api/agents/${symbol}/ships/${ship}/role`, { method: 'POST', body: JSON.stringify({ role, ...opts }) }),
  remove: (symbol: string) => req(`/api/agents/${symbol}`, { method: 'DELETE' }),
  ackReset: () => req('/api/reset/acknowledge', { method: 'POST' }),
  comparison: (hours = 24) =>
    req<{ agents: { symbol: string; models: string; starting_credits: number; points: { ts: number; credits: number }[] }[] }>(
      `/api/comparison?hours=${hours}`,
    ),
  events: (agent?: string, limit = 200) =>
    req<GameEvent[]>(`/api/events?limit=${limit}${agent ? `&agent=${agent}` : ''}`),
}

export function fmtCredits(n: number): string {
  return n.toLocaleString('en-US') + 'c'
}

export function fmtUsd(n: number): string {
  return '$' + n.toFixed(n < 0.1 ? 4 : 2)
}

export function ago(ts: number): string {
  const s = Math.max(0, Date.now() / 1000 - ts)
  if (s < 60) return `${Math.round(s)}s ago`
  if (s < 3600) return `${Math.round(s / 60)}m ago`
  if (s < 86400) return `${(s / 3600).toFixed(1)}h ago`
  return `${(s / 86400).toFixed(1)}d ago`
}
