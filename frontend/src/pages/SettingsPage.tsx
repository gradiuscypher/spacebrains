import { api, fmtUsd, type ModelInfo } from '../api'
import { usePoll } from '../hooks'
import { SettingsForm } from '../components/SettingsForm'

export function SettingsPage({ models, onSaved }: { models: ModelInfo[]; onSaved: () => void }) {
  const settings = usePoll(() => api.settings(), 30000)
  const usage = usePoll(() => api.usage(), 30000)
  if (!settings.data) return <div className="muted">Loading…</div>
  const key = usage.data?.openrouter_key ?? {}
  return (
    <div className="grid two">
      <div className="card">
        <h2>Global settings</h2>
        <SettingsForm
          schema={settings.data.schema}
          values={settings.data.values as unknown as Record<string, unknown>}
          modelFields={['strategist_model', 'critic_model']}
          models={models}
          onSave={async (patch) => {
            await api.patchSettings(patch)
            await settings.reload()
            onSaved()
          }}
        />
      </div>
      <div className="stack">
        <div className="card">
          <h2>OpenRouter key</h2>
          {'error' in key ? (
            <div className="notice bad">{String(key.error)}</div>
          ) : (
            <div className="sub">
              <div>
                Usage this month (OpenRouter's view): <b>{fmtUsd(Number(key.usage_monthly ?? 0))}</b> of {key.limit ? fmtUsd(Number(key.limit)) : '∞'} (
                {key.limit_reset ? String(key.limit_reset) : 'no reset'})
              </div>
              <div>Remaining: {key.limit_remaining !== null && key.limit_remaining !== undefined ? fmtUsd(Number(key.limit_remaining)) : '—'}</div>
            </div>
          )}
        </div>
        <div className="card">
          <h2>Spend by model (30 days)</h2>
          <table>
            <thead>
              <tr>
                <th>Model</th>
                <th className="num">Calls</th>
                <th className="num">In tok</th>
                <th className="num">Out tok</th>
                <th className="num">Cost</th>
              </tr>
            </thead>
            <tbody>
              {(usage.data?.by_model ?? []).map((r) => (
                <tr key={r.provider + r.model}>
                  <td>
                    <code>{r.model}</code> <span className="muted">{r.provider}</span>
                  </td>
                  <td className="num">{r.calls}</td>
                  <td className="num">{r.input_tokens.toLocaleString()}</td>
                  <td className="num">{r.output_tokens.toLocaleString()}</td>
                  <td className="num">{fmtUsd(r.cost_usd)}</td>
                </tr>
              ))}
              {(usage.data?.by_model ?? []).length === 0 && (
                <tr>
                  <td colSpan={5} className="muted">
                    No LLM calls yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <div className="card">
          <h2>How the brains fit together</h2>
          <div className="sub stack">
            <div>
              <b>Strategist</b> (OpenRouter, every <i>strategist interval</i> minutes or after a contract/purchase) reads a compact game summary and writes goals, a
              fleet-growth request and role hints. <i>Thinking rounds</i> &gt; 1 adds critic rounds with the critic model, each followed by a revision.
            </div>
            <div>
              <b>Jev</b> (TypeSafe) answers typed questions every few seconds: which role each ship should take, whether to accept a non-obvious contract, which
              market or trade route to pick, whether to buy a ship now. Turn it off to fall back to plain heuristics.
            </div>
            <div>
              <b>Code</b> owns everything with a known answer: navigation, fuel, cooldowns, contract math, arbitrage scoring and the monthly budget guard.
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
