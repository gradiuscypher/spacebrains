import { useEffect, useState } from 'react'
import { api, type ModelInfo } from './api'
import { usePoll } from './hooks'
import { OverviewPage } from './pages/Overview'
import { AgentPage } from './pages/AgentPage'
import { SettingsPage } from './pages/SettingsPage'

type Route = { page: 'overview' } | { page: 'agent'; symbol: string } | { page: 'settings' }

function routeFromHash(): Route {
  const h = location.hash.replace(/^#\/?/, '')
  if (h === 'settings') return { page: 'settings' }
  if (h.startsWith('agent/')) return { page: 'agent', symbol: h.slice(6) }
  return { page: 'overview' }
}

export default function App() {
  const [route, setRoute] = useState<Route>(routeFromHash)
  const [models, setModels] = useState<ModelInfo[]>([])
  const overview = usePoll(() => api.overview(), 5000)

  useEffect(() => {
    const onHash = () => setRoute(routeFromHash())
    window.addEventListener('hashchange', onHash)
    api.models().then((m) => setModels(m.models)).catch(() => undefined)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const go = (hash: string) => {
    location.hash = hash
  }

  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">🧠 spacebrains</span>
        <nav>
          <button className={route.page === 'overview' ? 'active' : ''} onClick={() => go('/')}>
            Overview
          </button>
          {overview.data?.agents.map((a) => (
            <button key={a.symbol} className={route.page === 'agent' && route.symbol === a.symbol ? 'active' : ''} onClick={() => go(`/agent/${a.symbol}`)}>
              {a.symbol}
            </button>
          ))}
          <button className={route.page === 'settings' ? 'active' : ''} onClick={() => go('/settings')}>
            Settings
          </button>
        </nav>
        <span className="spacer" />
        {overview.error ? <span className="live off">backend unreachable</span> : <span className="live on">connected</span>}
      </header>
      <main>
        {route.page === 'overview' && <OverviewPage data={overview.data} reload={() => void overview.reload()} onOpen={(s) => go(`/agent/${s}`)} />}
        {route.page === 'agent' && <AgentPage symbol={route.symbol} models={models} onBack={() => go('/')} />}
        {route.page === 'settings' && <SettingsPage models={models} onSaved={() => void overview.reload()} />}
      </main>
    </div>
  )
}
