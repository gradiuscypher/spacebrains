import type { GameEvent } from '../api'

function time(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString('en-GB', { hour12: false })
}

export function EventFeed({ events, showAgent }: { events: GameEvent[]; showAgent?: boolean }) {
  if (events.length === 0) return <div className="muted">No events yet.</div>
  return (
    <div className="events">
      {events.map((e, i) => (
        <div className={`ev ${e.kind}`} key={e.id ?? `${e.ts}-${i}`} title={JSON.stringify(e.data)}>
          <span className="t">{time(e.ts)}</span>
          <span className="k">{e.kind}</span>
          <span>
            {showAgent && e.agent ? <span className="muted">{e.agent} · </span> : null}
            {e.message}
          </span>
        </div>
      ))}
    </div>
  )
}
