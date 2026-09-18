import { useState } from 'react'
import { api, fmtCredits, fmtUsd, type AgentSnapshot, type Overview } from '../api'
import { useEvents, usePoll } from '../hooks'
import { EventFeed } from '../components/EventFeed'
import { ComparisonChart } from '../components/ComparisonChart'

const FACTIONS = ['COSMIC', 'VOID', 'GALACTIC', 'QUANTUM', 'DOMINION', 'ASTRO', 'CORSAIRS', 'OBSIDIAN', 'AEGIS', 'UNITED']

function AgentCard({ a, onOpen }: { a: AgentSnapshot; onOpen: () => void }) {
  const delta = a.credits - a.starting_credits
  const roles = a.ships.reduce<Record<string, number>>((acc, s) => ({ ...acc, [s.role]: (acc[s.role] ?? 0) + 1 }), {})
  const contract = a.contracts.find((c) => c.accepted && !c.fulfilled)
  const errors = a.ships.filter((s) => s.status.startsWith('error') || s.status.startsWith('crash')).length
  return (
    <div className="card clickable" onClick={onOpen}>
      <div className="row between">
        <h2 style={{ margin: 0 }}>{a.symbol}</h2>
        <span className="row">
          {a.paused && <span className="pill bad">paused</span>}
          {!a.enabled && <span className="pill bad">disabled</span>}
          {errors > 0 && <span className="pill bad">{errors} ship error(s)</span>}
          <span className="pill">{a.faction}</span>
        </span>
      </div>
      <div className="hero">{fmtCredits(a.credits)}</div>
      <div className="sub">
        {delta >= 0 ? '+' : ''}
        {fmtCredits(delta)} since start · {a.ships.length} ships · {a.requests} API calls
      </div>
      <div className="row" style={{ marginTop: 8 }}>
        {Object.entries(roles).map(([r, n]) => (
          <span key={r} className={`pill role-${r}`}>
            {n}× {r}
          </span>
        ))}
      </div>
      {contract && (
        <div className="sub" style={{ marginTop: 8 }}>
          Contract: {contract.deliver.join(', ')} · pays {fmtCredits(contract.payment)}
        </div>
      )}
      {a.plan ? (
        <div className="sub" style={{ marginTop: 8 }} title={a.plan.assessment}>
          <b>Plan:</b> {a.plan.goals.slice(0, 3).map((g) => g.description).join(' · ') || a.plan.assessment.slice(0, 160)}
        </div>
      ) : (
        <div className="sub" style={{ marginTop: 8 }}>
          {a.plan_error ? <span className="pill bad">strategist: {a.plan_error.slice(0, 80)}</span> : 'No plan yet.'}
        </div>
      )}
    </div>
  )
}

function AddAgent({ onDone }: { onDone: () => void }) {
  const [symbol, setSymbol] = useState('')
  const [faction, setFaction] = useState('COSMIC')
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    setErr(null)
    try {
      await fn()
      setSymbol('')
      setToken('')
      onDone()
    } catch (e) {
      setErr((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="card">
      <h2>Add agent</h2>
      <div className="stack">
        <div className="row">
          <input type="text" placeholder="SYMBOL (3-14 chars)" value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())} style={{ width: 200 }} />
          <select value={faction} onChange={(e) => setFaction(e.target.value)}>
            {FACTIONS.map((f) => (
              <option key={f}>{f}</option>
            ))}
          </select>
          <button className="primary" disabled={busy || symbol.length < 3} onClick={() => void run(() => api.register(symbol, faction))}>
            Register new
          </button>
        </div>
        <div className="row">
          <input type="text" placeholder="…or paste an existing agent token" value={token} onChange={(e) => setToken(e.target.value)} style={{ width: 360 }} />
          <button disabled={busy || token.length < 20} onClick={() => void run(() => api.importAgent(token))}>
            Import
          </button>
        </div>
        {err && <div className="notice bad">{err}</div>}
      </div>
    </div>
  )
}

export function OverviewPage({ data, reload, onOpen }: { data: Overview | null; reload: () => void; onOpen: (s: string) => void }) {
  const { events, live } = useEvents(undefined, 150)
  const comparison = usePoll(() => api.comparison(24), 30000)
  if (!data) return <div className="muted">Loading…</div>
  const budget = data.settings.monthly_llm_budget_usd
  const spent = data.usage.month_openrouter_usd
  const pct = budget > 0 ? Math.min(100, (spent / budget) * 100) : 0
  const total = data.agents.reduce((s, a) => s + a.credits, 0)
  return (
    <div className="stack" style={{ gap: 12 }}>
      {data.reset_detected && (
        <div className="notice bad row between">
          <span>
            <b>Universe reset detected.</b> {data.reset_detected}
          </span>
          <button onClick={() => void api.ackReset().then(reload)}>Acknowledge</button>
        </div>
      )}
      <div className="grid cards">
        <div className="card">
          <h2>Fleet credits</h2>
          <div className="hero">{fmtCredits(total)}</div>
          <div className="sub">
            {data.agents.length} agent(s) · {data.agents.reduce((s, a) => s + a.ships.length, 0)} ships
            {data.settings.paused && (
              <>
                {' '}
                · <span className="pill bad">ALL PAUSED</span>
              </>
            )}
          </div>
          {data.server.reset_date && (
            <div className="sub" style={{ marginTop: 6 }}>
              Server {data.server.version} · reset {data.server.reset_date}
              {data.server.next_reset ? ` · next ${new Date(data.server.next_reset).toLocaleDateString()}` : ''}
            </div>
          )}
        </div>
        <div className="card">
          <h2>LLM spend this month</h2>
          <div className="hero">{fmtUsd(spent)}</div>
          <div className="sub">
            of {fmtUsd(budget)} OpenRouter budget · Jev {fmtUsd(data.usage.month_typesafe_usd)}
          </div>
          <div className={`meter ${pct > 90 ? 'bad' : pct > 70 ? 'warn' : ''}`} style={{ marginTop: 8 }}>
            <div style={{ width: `${pct}%` }} />
          </div>
        </div>
        <div className="card">
          <h2>SpaceTraders API (shared per IP)</h2>
          {(() => {
            const per = data.api_rate.per_agent_last_minute
            const total = Object.values(per).reduce((s, n) => s + n, 0)
            const cap = data.api_rate.limit_per_second * 60
            const pct = Math.min(100, (total / cap) * 100)
            return (
              <>
                <div className="hero">{(total / 60).toFixed(2)} req/s</div>
                <div className="sub">of {data.api_rate.limit_per_second} req/s · last minute, fair-shared round-robin</div>
                <div className={`meter ${pct > 90 ? 'bad' : pct > 70 ? 'warn' : ''}`} style={{ marginTop: 8 }}>
                  <div style={{ width: `${pct}%` }} />
                </div>
                <div className="row" style={{ marginTop: 8 }}>
                  {Object.entries(per).map(([k, n]) => (
                    <span key={k} className="pill">
                      {k}: {(n / 60).toFixed(2)}/s
                    </span>
                  ))}
                </div>
              </>
            )
          })()}
        </div>
        <div className="card">
          <h2>Brains</h2>
          <div className="sub">
            <div>
              <b>Strategist:</b> <code>{data.settings.strategist_model}</code>
            </div>
            <div>
              <b>Critic:</b> <code>{data.settings.critic_model}</code> × {data.settings.thinking_rounds - 1} round(s)
            </div>
            <div>
              <b>Tactical:</b> <code>{data.settings.jev_model}</code> {data.settings.jev_enabled ? '' : '(disabled)'}
            </div>
            <div>
              <b>Replan every:</b> {data.settings.strategist_interval_minutes} min
            </div>
          </div>
        </div>
      </div>

      {data.agents.length > 1 && comparison.data && (
        <div className="card">
          <h2>Model comparison · credits since start (24h)</h2>
          <ComparisonChart series={comparison.data.agents} />
        </div>
      )}

      <div className="grid cards">
        {data.agents.map((a) => (
          <AgentCard key={a.symbol} a={a} onOpen={() => onOpen(a.symbol)} />
        ))}
        {data.agents.length < data.settings.max_agents && <AddAgent onDone={reload} />}
      </div>

      <div className="card">
        <div className="row between">
          <h2>Live events</h2>
          <span className={`live ${live ? 'on' : 'off'}`}>{live ? 'streaming' : 'reconnecting'}</span>
        </div>
        <EventFeed events={events} showAgent />
      </div>
    </div>
  )
}
