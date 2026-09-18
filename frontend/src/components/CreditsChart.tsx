import { useState } from 'react'
import { fmtCredits } from '../api'

interface Point {
  ts: number
  credits: number
}

/** Single-series credits-over-time line with a crosshair tooltip. */
export function CreditsChart({ points }: { points: Point[] }) {
  const [hover, setHover] = useState<number | null>(null)
  const W = 600
  const H = 120
  const padL = 8
  const padR = 8
  const padT = 10
  const padB = 18
  if (points.length < 2) return <div className="muted">Collecting history…</div>

  const xs = points.map((p) => p.ts)
  const ys = points.map((p) => p.credits)
  const x0 = Math.min(...xs)
  const x1 = Math.max(...xs)
  const yMin = Math.min(...ys)
  const yMax = Math.max(...ys)
  const ySpan = yMax - yMin || 1
  const sx = (t: number) => padL + ((t - x0) / (x1 - x0 || 1)) * (W - padL - padR)
  const sy = (v: number) => padT + (1 - (v - yMin) / ySpan) * (H - padT - padB)
  const d = points.map((p, i) => `${i ? 'L' : 'M'}${sx(p.ts).toFixed(1)},${sy(p.credits).toFixed(1)}`).join(' ')

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect()
    const x = ((e.clientX - rect.left) / rect.width) * W
    let best = 0
    let bestD = Infinity
    points.forEach((p, i) => {
      const dd = Math.abs(sx(p.ts) - x)
      if (dd < bestD) {
        bestD = dd
        best = i
      }
    })
    setHover(best)
  }
  const hp = hover === null ? null : points[hover]
  const fmtT = (ts: number) => new Date(ts * 1000).toLocaleTimeString('en-GB', { hour12: false })

  return (
    <div className="chart">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        <line x1={padL} x2={W - padR} y1={sy(yMin)} y2={sy(yMin)} stroke="var(--border)" strokeWidth={1} />
        <path d={d} fill="none" stroke="var(--series-1)" strokeWidth={2} strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
        {hp && (
          <>
            <line x1={sx(hp.ts)} x2={sx(hp.ts)} y1={padT} y2={H - padB} stroke="var(--text-3)" strokeWidth={1} vectorEffect="non-scaling-stroke" />
            <circle cx={sx(hp.ts)} cy={sy(hp.credits)} r={4} fill="var(--series-1)" stroke="var(--surface)" strokeWidth={2} vectorEffect="non-scaling-stroke" />
          </>
        )}
        <text x={padL} y={H - 4} fontSize={10} fill="var(--text-3)">
          {fmtT(x0)}
        </text>
        <text x={W - padR} y={H - 4} fontSize={10} fill="var(--text-3)" textAnchor="end">
          {fmtT(x1)}
        </text>
      </svg>
      {hp && (
        <div className="tip" style={{ left: `${(sx(hp.ts) / W) * 100}%`, top: `${(sy(hp.credits) / H) * 100}%` }}>
          {fmtT(hp.ts)} · {fmtCredits(hp.credits)}
        </div>
      )}
      <div className="row between sub">
        <span>min {fmtCredits(yMin)}</span>
        <span>max {fmtCredits(yMax)}</span>
      </div>
    </div>
  )
}
