import { useState } from 'react'
import { ago, api, fmtCredits, fmtUsd, type ModelInfo, type Ship } from '../api'
import { useEvents, usePoll } from '../hooks'
import { EventFeed } from '../components/EventFeed'
import { CreditsChart } from '../components/CreditsChart'
import { SettingsForm } from '../components/SettingsForm'

const ROLES = ['contract', 'mine', 'trade', 'scout', 'haul', 'idle']

function ShipRow({ s, agent, onChanged }: { s: Ship; agent: string; onChanged: () => void }) {
  const [busy, setBusy] = useState(false)
  const setRole = async (role: string) => {
    setBusy(true)
    try {
      await api.setRole(agent, s.symbol, role)
      onChanged()
    } catch (e) {
      alert((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  const cargo = Object.entries(s.cargo.inventory)
    .map(([k, v]) => `${v} ${k}`)
    .join(', ')
  return (
    <tr>
      <td>
        <div className="mono">{s.symbol}</div>
        <div className="sub">
          {s.frame} · {s.ship_role.toLowerCase()}
          {s.condition < 0.6 ? <span className="pill bad"> hull {Math.round(s.condition * 100)}%</span> : null}
        </div>
      </td>
      <td>
        <select value={s.role_source === 'operator' ? s.role : 'auto'} disabled={busy} onChange={(e) => void setRole(e.target.value)}>
          <option value="auto">auto ({s.role})</option>
          {ROLES.map((r) => (
            <option key={r} value={r}>
              pin: {r}
            </option>
          ))}
        </select>
        <div className="sub">via {s.role_source}</div>
      </td>
      <td>
        <div>{s.status}</div>
        {s.last_error && <div className="sub" style={{ color: 'var(--bad)' }}>{s.last_error}</div>}
      </td>
      <td>
        <div className="mono">{s.waypoint}</div>
        <div className="sub">
          {s.nav_status.toLowerCase().replace('_', ' ')} · {s.flight_mode.toLowerCase()}
          {s.cooldown > 0 ? ` · cooldown ${s.cooldown}s` : ''}
        </div>
      </td>
      <td className="num">
        {s.fuel.capacity ? `${s.fuel.current}/${s.fuel.capacity}` : '—'}
      </td>
      <td className="num">
        <div>
          {s.cargo.units}/{s.cargo.capacity}
        </div>
        {cargo && <div className="sub">{cargo}</div>}
      </td>
    </tr>
  )
}

export function AgentPage({ symbol, models, onBack }: { symbol: string; models: ModelInfo[]; onBack: () => void }) {
  const { data, error, reload } = usePoll(() => api.agent(symbol), 5000, [symbol])
  const { events } = useEvents(symbol, 200)
  const [busy, setBusy] = useState(false)

  if (error) return <div className="notice bad">{error}</div>
  if (!data) return <div className="muted">Loading…</div>

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true)
    try {
      await fn()
      await reload()
    } catch (e) {
      alert((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  const activeGoals = data.goals.filter((g) => g.status === 'active')
  const contracts = data.contracts.filter((c) => !c.fulfilled)
  const done = data.contracts.filter((c) => c.fulfilled).length

  return (
    <div className="stack" style={{ gap: 12 }}>
      <div className="row between">
        <div className="row">
          <button onClick={onBack}>← Overview</button>
          <h1 style={{ margin: 0, fontSize: 20 }}>{data.symbol}</h1>
          <span className="pill">{data.faction}</span>
          <span className="pill mono">{data.headquarters}</span>
          {data.paused && <span className="pill bad">paused</span>}
        </div>
        <div className="row">
          <button disabled={busy} onClick={() => void act(() => api.replan(symbol))}>
            Re-plan now
          </button>
          <button disabled={busy} onClick={() => void act(() => api.setEnabled(symbol, !data.enabled))}>
            {data.enabled ? 'Disable' : 'Enable'}
          </button>
          <button
            className="danger"
            disabled={busy}
            onClick={() => {
              if (confirm(`Remove ${symbol} from spacebrains? The in-game agent keeps existing; its token is deleted locally.`)) void act(() => api.remove(symbol)).then(onBack)
            }}
          >
            Remove
          </button>
        </div>
      </div>

      <div className="grid cards">
        <div className="card">
          <h2>Credits</h2>
          <div className="hero">{fmtCredits(data.credits)}</div>
          <div className="sub">
            {data.credits - data.starting_credits >= 0 ? '+' : ''}
            {fmtCredits(data.credits - data.starting_credits)} since start · {done} contract(s) fulfilled
          </div>
          <div style={{ marginTop: 8 }}>
            <CreditsChart points={data.snapshots} />
          </div>
        </div>
        <div className="card">
          <h2>Long-term plan {data.plan_ts ? <span className="muted">· {ago(data.plan_ts)}</span> : null}</h2>
          {data.plan_error && <div className="notice bad">{data.plan_error}</div>}
          {data.plan ? (
            <div className="stack">
              <div className="sub">{data.plan.assessment}</div>
              {data.plan.ship_purchase && (
                <div className="sub">
                  <b>Buy:</b> {data.plan.ship_purchase.count}× {data.plan.ship_purchase.ship_type}
                  {data.plan.ship_purchase.when_credits_above ? ` when credits > ${fmtCredits(data.plan.ship_purchase.when_credits_above)}` : ''}
                  {data.purchase_pending ? <span className="pill"> pending</span> : <span className="pill good"> done/cancelled</span>}
                  <div className="muted">{data.plan.ship_purchase.reason}</div>
                </div>
              )}
              {data.plan.notes_for_next_time && (
                <div className="sub">
                  <b>Memo:</b> {data.plan.notes_for_next_time}
                </div>
              )}
            </div>
          ) : (
            <div className="muted">Waiting for the strategist.</div>
          )}
        </div>
        <div className="card">
          <h2>Goals</h2>
          {activeGoals.length === 0 && <div className="muted">None.</div>}
          <ol style={{ margin: 0, paddingLeft: 18 }}>
            {activeGoals.map((g) => (
              <li key={g.id}>
                <span className="pill">{g.kind}</span> {g.description}
              </li>
            ))}
          </ol>
          {contracts.length > 0 && (
            <>
              <h3 style={{ marginTop: 12 }}>Contracts</h3>
              {contracts.map((c) => (
                <div key={c.id} className="sub">
                  <span className={`pill ${c.accepted ? 'good' : ''}`}>{c.accepted ? 'accepted' : 'offered'}</span> {c.deliver.join(', ')} · {fmtCredits(c.payment)} · due{' '}
                  {new Date(c.deadline).toLocaleString()}
                </div>
              ))}
            </>
          )}
        </div>
      </div>

      <div className="card">
        <h2>Ships</h2>
        <div style={{ overflowX: 'auto' }}>
          <table>
            <thead>
              <tr>
                <th>Ship</th>
                <th>Role</th>
                <th>Status</th>
                <th>Location</th>
                <th className="num">Fuel</th>
                <th className="num">Cargo</th>
              </tr>
            </thead>
            <tbody>
              {data.ships.map((s) => (
                <ShipRow key={s.symbol} s={s} agent={symbol} onChanged={() => void reload()} />
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {data.trades.length > 0 && (
        <div className="card">
          <h2>Trades (realised vs predicted)</h2>
          <div style={{ overflowX: 'auto' }}>
            <table>
              <thead>
                <tr>
                  <th>When</th>
                  <th>Ship</th>
                  <th>Good</th>
                  <th>Route</th>
                  <th className="num">Units</th>
                  <th className="num">Cost</th>
                  <th className="num">Revenue</th>
                  <th className="num">Profit</th>
                  <th className="num">Predicted</th>
                  <th className="num">Time</th>
                </tr>
              </thead>
              <tbody>
                {data.trades.map((t) => {
                  const profit = t.revenue - t.cost
                  return (
                    <tr key={t.id}>
                      <td>{ago(t.ts)}</td>
                      <td className="mono">{t.ship}</td>
                      <td>{t.good}</td>
                      <td className="mono">
                        {t.buy_at} → {t.sell_at}
                      </td>
                      <td className="num">{t.units}</td>
                      <td className="num">{fmtCredits(t.cost)}</td>
                      <td className="num">{fmtCredits(t.revenue)}</td>
                      <td className="num" style={{ color: profit >= 0 ? 'var(--good)' : 'var(--bad)' }}>
                        {profit >= 0 ? '+' : ''}
                        {fmtCredits(profit)}
                      </td>
                      <td className="num">{fmtCredits(t.predicted_margin)}</td>
                      <td className="num">{Math.round(t.seconds / 60)}m</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="grid two">
        <div className="card">
          <h2>Events</h2>
          <EventFeed events={events} />
        </div>
        <div className="stack">
          <div className="card">
            <h2>Agent overrides</h2>
            <div className="sub" style={{ marginBottom: 8 }}>
              Blank = inherit the global setting. Operator notes are injected into the strategist prompt.
            </div>
            <SettingsForm
              schema={data.overrides_schema}
              values={data.overrides as unknown as Record<string, unknown>}
              inherited={data.effective_settings as unknown as Record<string, unknown>}
              nullable
              modelFields={['strategist_model', 'critic_model']}
              models={models}
              onSave={async (patch) => {
                await api.patchOverrides(symbol, patch)
                await reload()
              }}
            />
          </div>
          <div className="card">
            <h2>Strategist history</h2>
            {data.strategist_runs.length === 0 && <div className="muted">No runs yet.</div>}
            {data.strategist_runs.map((r) => (
              <details key={r.id}>
                <summary>
                  {ago(r.ts)} · {r.models.join(' → ')} · {r.rounds} round(s) · {fmtUsd(r.cost_usd)}
                </summary>
                <pre>{JSON.stringify(r.plan, null, 2)}</pre>
              </details>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
