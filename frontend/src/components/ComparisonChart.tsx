import { useState } from 'react'
import { fmtCredits } from '../api'

export interface Series {
  symbol: string
  models: string
  starting_credits: number
  points: { ts: number; credits: number }[]
}

// Categorical palette, fixed slot order (never cycled): blue, orange, aqua, yellow, magenta, green.
const LIGHT = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300']
const DARK = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300']

function useDark(): boolean {
  return typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: dark)').matches
}

/** Every agent's credit delta since start on one axis, with legend, direct labels and crosshair. */
export function ComparisonChart({ series }: { series: Series[] }) {
  const dark = useDark()
  const colors = dark ? DARK : LIGHT
  const [hoverTs, setHoverTs] = useState<number | null>(null)
  const W = 900
  const H = 200
  const padL = 8
  const padR = 90
  const padT = 10
  const padB = 18
  const lines = series.filter((s) => s.points.length >= 2).slice(0, 6)
  if (lines.length === 0) return <div className="muted">Collecting history…</div>

  const allTs = lines.flatMap((s) => s.points.map((p) => p.ts))
  const x0 = Math.min(...allTs)
  const x1 = Math.max(...allTs)
  const deltas = lines.flatMap((s) => s.points.map((p) => p.credits - s.starting_credits))
  const yMin = Math.min(0, ...deltas)
  const yMax = Math.max(0, ...deltas)
  const ySpan = yMax - yMin || 1
  const sx = (t: number) => padL + ((t - x0) / (x1 - x0 || 1)) * (W - padL - padR)
  const sy = (v: number) => padT + (1 - (v - yMin) / ySpan) * (H - padT - padB)
  const fmtT = (ts: number) => new Date(ts * 1000).toLocaleTimeString('en-GB', { hour12: false })

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect()
    const x = ((e.clientX - rect.left) / rect.width) * W
    setHoverTs(x0 + ((x - padL) / (W - padL - padR)) * (x1 - x0))
  }
  const nearest = (s: Series, ts: number) =>
    s.points.reduce((best, p) => (Math.abs(p.ts - ts) < Math.abs(best.ts - ts) ? p : best), s.points[0])

  return (
    <div className="chart">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" style={{ height: H }} onMouseMove={onMove} onMouseLeave={() => setHoverTs(null)}>
        <line x1={padL} x2={W - padR} y1={sy(0)} y2={sy(0)} stroke="var(--border)" strokeWidth={1} />
        {lines.map((s, i) => {
          const d = s.points.map((p, j) => `${j ? 'L' : 'M'}${sx(p.ts).toFixed(1)},${sy(p.credits - s.starting_credits).toFixed(1)}`).join(' ')
          const last = s.points[s.points.length - 1]
          return (
            <g key={s.symbol}>
              <path d={d} fill="none" stroke={colors[i]} strokeWidth={2} strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
              <text x={sx(last.ts) + 6} y={sy(last.credits - s.starting_credits) + 4} fontSize={11} fill="var(--text-2)">
                {s.symbol.replace('SPACEBRAINS', 'SB')} {fmtCredits(last.credits - s.starting_credits)}
              </text>
            </g>
          )
        })}
        {hoverTs !== null && (
          <>
            <line x1={sx(hoverTs)} x2={sx(hoverTs)} y1={padT} y2={H - padB} stroke="var(--text-3)" strokeWidth={1} vectorEffect="non-scaling-stroke" />
            {lines.map((s, i) => {
              const p = nearest(s, hoverTs)
              return <circle key={s.symbol} cx={sx(p.ts)} cy={sy(p.credits - s.starting_credits)} r={4} fill={colors[i]} stroke="var(--surface)" strokeWidth={2} vectorEffect="non-scaling-stroke" />
            })}
          </>
        )}
        <text x={padL} y={H - 4} fontSize={10} fill="var(--text-3)">
          {fmtT(x0)}
        </text>
        <text x={W - padR} y={H - 4} fontSize={10} fill="var(--text-3)" textAnchor="end">
          {fmtT(x1)}
        </text>
      </svg>
      {hoverTs !== null && (
        <div className="tip" style={{ left: `${(sx(hoverTs) / W) * 100}%`, top: 0 }}>
          {fmtT(hoverTs)}
          {lines.map((s) => {
            const p = nearest(s, hoverTs)
            return (
              <div key={s.symbol}>
                {s.symbol}: {fmtCredits(p.credits - s.starting_credits)}
              </div>
            )
          })}
        </div>
      )}
      <div className="row" style={{ marginTop: 6 }}>
        {lines.map((s, i) => (
          <span key={s.symbol} className="pill" title={s.models}>
            <span style={{ display: 'inline-block', width: 10, height: 10, borderRadius: 2, background: colors[i], marginRight: 6, verticalAlign: 'middle' }} />
            {s.symbol} · <code>{s.models}</code>
          </span>
        ))}
      </div>
    </div>
  )
}
