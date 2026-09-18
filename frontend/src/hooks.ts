import { useCallback, useEffect, useRef, useState } from 'react'
import { api, type GameEvent } from './api'

/** Poll an async loader on an interval; refetch immediately with `reload()`. */
export function usePoll<T>(loader: () => Promise<T>, intervalMs: number, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const loaderRef = useRef(loader)
  loaderRef.current = loader
  const reload = useCallback(async () => {
    try {
      setData(await loaderRef.current())
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    }
  }, [])
  useEffect(() => {
    void reload()
    const id = setInterval(() => void reload(), intervalMs)
    return () => clearInterval(id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, reload, ...deps])
  return { data, error, reload }
}

/** Live event feed: seeds from history, then appends from the SSE stream. */
export function useEvents(agent?: string, limit = 200) {
  const [events, setEvents] = useState<GameEvent[]>([])
  const [live, setLive] = useState(false)
  useEffect(() => {
    let cancelled = false
    api.events(agent, limit).then((evs) => {
      if (!cancelled) setEvents(evs)
    })
    const es = new EventSource('/api/stream')
    es.onopen = () => setLive(true)
    es.onerror = () => setLive(false)
    es.onmessage = (m) => {
      const ev = JSON.parse(m.data) as GameEvent
      if (agent && ev.agent !== agent) return
      setEvents((prev) => [ev, ...prev].slice(0, limit))
    }
    return () => {
      cancelled = true
      es.close()
    }
  }, [agent, limit])
  return { events, live }
}
