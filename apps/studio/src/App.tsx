import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, type Agent, type Health } from './api'
import { Icon, type IconName } from './components/Icon'
import { Palette, type Command } from './components/Palette'
import { Toasts, useToasts } from './components/Toasts'
import { Dashboard } from './views/Dashboard'
import { LiveCall } from './views/LiveCall'
import { Agents } from './views/Agents'
import { Knowledge } from './views/Knowledge'
import { Calls } from './views/Calls'
import { Appointments } from './views/Appointments'
import { Numbers } from './views/Numbers'
import { FlowStudio } from './views/FlowStudio'
import { Benchmark } from './views/Benchmark'
import { Compliance } from './views/Compliance'

export type Route =
  | { view: 'dashboard' }
  | { view: 'live' }
  | { view: 'agents' }
  | { view: 'knowledge' }
  | { view: 'flow' }
  | { view: 'calls'; callId?: string }
  | { view: 'appointments' }
  | { view: 'numbers' }
  | { view: 'benchmark' }
  | { view: 'compliance' }

const NAV: { group: string; items: { view: Route['view']; label: string; icon: IconName }[] }[] = [
  {
    group: 'Operate',
    items: [
      { view: 'dashboard', label: 'Dashboard', icon: 'grid' },
      { view: 'live', label: 'Live call', icon: 'phone' },
      { view: 'appointments', label: 'Appointments', icon: 'calendar' },
      { view: 'calls', label: 'Call history', icon: 'list' },
    ],
  },
  {
    group: 'Build',
    items: [
      { view: 'agents', label: 'Agents', icon: 'user' },
      { view: 'knowledge', label: 'Knowledge', icon: 'book' },
      { view: 'flow', label: 'Conversation flow', icon: 'flow' },
      { view: 'numbers', label: 'Phone numbers', icon: 'hash' },
    ],
  },
  {
    group: 'Verify',
    items: [
      { view: 'benchmark', label: 'Turn-taking', icon: 'gauge' },
      { view: 'compliance', label: 'Compliance', icon: 'shield' },
    ],
  },
]

const TITLES: Record<Route['view'], string> = {
  dashboard: 'Dashboard',
  live: 'Live call',
  appointments: 'Appointments',
  calls: 'Call history',
  agents: 'Agents',
  knowledge: 'Knowledge',
  flow: 'Conversation flow',
  numbers: 'Phone numbers',
  benchmark: 'Turn-taking',
  compliance: 'Compliance',
}

export function App() {
  const [route, setRoute] = useState<Route>({ view: 'dashboard' })
  const [health, setHealth] = useState<Health | null>(null)
  const [agents, setAgents] = useState<Agent[]>([])
  const [agentId, setAgentId] = useState('')
  const [paletteOpen, setPaletteOpen] = useState(false)
  const toasts = useToasts()

  // Poll health while the model loads, then settle to a slow heartbeat. A fixed fast interval
  // would keep hammering a server that has nothing new to say; a fixed slow one would leave the
  // "starting" badge stale for half a minute after it went green.
  useEffect(() => {
    let cancelled = false
    let timer: number

    const tick = async () => {
      try {
        const h = await api.health()
        if (cancelled) return
        setHealth(h)
        timer = window.setTimeout(tick, h.status === 'ready' ? 15000 : 1500)
      } catch {
        if (cancelled) return
        setHealth(null)
        timer = window.setTimeout(tick, 3000)
      }
    }
    void tick()
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [])

  const loadAgents = useCallback(async () => {
    try {
      const { agents: list } = await api.agents()
      setAgents(list)
      setAgentId((current) => current || list[0]?.id || '')
    } catch {
      /* the health poll already reports the server being unreachable */
    }
  }, [])

  useEffect(() => {
    void loadAgents()
  }, [loadAgents, health?.status])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setPaletteOpen((open) => !open)
      }
      if (e.key === 'Escape') setPaletteOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const commands = useMemo<Command[]>(
    () => [
      ...NAV.flatMap((g) =>
        g.items.map((i) => ({
          id: `go:${i.view}`,
          label: i.label,
          hint: g.group,
          icon: i.icon,
          run: () => setRoute({ view: i.view }),
        })),
      ),
      ...agents.map((a) => ({
        id: `agent:${a.id}`,
        label: `Switch to ${a.name}`,
        hint: a.business,
        icon: 'user' as IconName,
        run: () => {
          setAgentId(a.id)
          toasts.push(`Now editing ${a.name}`)
        },
      })),
      {
        id: 'call:new',
        label: 'Start a call',
        hint: 'Live',
        icon: 'phone',
        run: () => setRoute({ view: 'live' }),
      },
    ],
    [agents, toasts],
  )

  const agent = agents.find((a) => a.id === agentId) ?? null
  const state = health ? health.status : 'offline'

  const shared = {
    agent,
    agents,
    agentId,
    setAgentId,
    reloadAgents: loadAgents,
    toast: toasts.push,
    navigate: setRoute,
    ready: health?.status === 'ready',
    offline: state === 'offline',
  }

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="side-head">
          <div className="logo">d</div>
          <div>
            <div className="logo-text">dialtone</div>
            <div className="logo-sub">AI call centre</div>
          </div>
        </div>

        <nav className="side-nav">
          {NAV.map((group) => (
            <div key={group.group}>
              <div className="side-group">{group.group}</div>
              {group.items.map((item) => (
                <button
                  key={item.view}
                  className="side-link"
                  data-active={route.view === item.view}
                  onClick={() => setRoute({ view: item.view })}
                >
                  <Icon name={item.icon} className="ico" />
                  {item.label}
                  {item.view === 'live' && (health?.live_calls ?? 0) > 0 && (
                    <span className="badge">{health?.live_calls}</span>
                  )}
                </button>
              ))}
            </div>
          ))}
        </nav>

        <div className="side-foot">
          <div className="status-pill" title={health?.model ?? ''}>
            <i className="dot" data-state={state} />
            <span>
              {state === 'ready'
                ? 'Model ready'
                : state === 'starting'
                  ? 'Loading model…'
                  : 'Gateway offline'}
            </span>
          </div>
        </div>
      </aside>

      <div className="main">
        <header className="topbar">
          <div className="crumb">
            <b>{TITLES[route.view]}</b>
            {agent && (
              <>
                <span>/</span>
                {agent.name}
              </>
            )}
          </div>
          <div className="spacer" />
          <button className="btn btn-ghost btn-sm" onClick={() => setPaletteOpen(true)}>
            <Icon name="search" className="ico" />
            Search
            <span className="kbd">⌘K</span>
          </button>
        </header>

        <div className="content">
          {state === 'offline' && <Offline />}
          {state === 'starting' && <Starting />}
          {route.view === 'dashboard' && <Dashboard {...shared} />}
          {route.view === 'live' && <LiveCall {...shared} />}
          {route.view === 'appointments' && <Appointments {...shared} />}
          {route.view === 'calls' && <Calls {...shared} initialCallId={route.callId} />}
          {route.view === 'agents' && <Agents {...shared} />}
          {route.view === 'knowledge' && <Knowledge {...shared} />}
          {route.view === 'flow' && <FlowStudio {...shared} />}
          {route.view === 'numbers' && <Numbers {...shared} />}
          {route.view === 'benchmark' && <Benchmark />}
          {route.view === 'compliance' && <Compliance />}
        </div>
      </div>

      {paletteOpen && <Palette commands={commands} onClose={() => setPaletteOpen(false)} />}
      <Toasts items={toasts.items} />
    </div>
  )
}

/* The gateway is not answering.
 *
 * A BANNER RATHER THAN A REPLACEMENT SCREEN. Taking over the content area would throw away a
 * transcript the moment a connection wobbled, and the call is the thing a user would most mind
 * losing. This sits above whatever was already there.
 *
 * It exists because the alternative was worse than an error: with the gateway down the dashboard
 * showed four loading skeletons FOREVER, and the only clue was a small grey pill at the bottom of
 * the sidebar. A loading state that never resolves is the least honest thing a UI can do — it
 * tells the user to keep waiting for something that is never going to arrive.
 */
function Offline() {
  return (
    <div className="banner" data-t="bad">
      <Icon name="alert" size={17} />
      <div>
        <b>The gateway is not answering.</b>
        <p>
          Nothing on this page is live. Start it with <code>dialtone serve</code>{' '}
          in <code>services/gateway</code> — this page will reconnect on its own.
        </p>
      </div>
    </div>
  )
}

/* Loading weights takes about twenty seconds on first start. Saying so is the difference between
 * "it is broken" and "it is nearly ready". */
function Starting() {
  return (
    <div className="banner" data-t="warn">
      <Icon name="clock" size={17} />
      <div>
        <b>The model is loading.</b>
        <p>About twenty seconds on first start. Everything except making a call already works.</p>
      </div>
    </div>
  )
}

export interface ViewProps {
  agent: Agent | null
  agents: Agent[]
  agentId: string
  setAgentId: (id: string) => void
  reloadAgents: () => Promise<void>
  toast: (message: string, tone?: 'good' | 'bad') => void
  navigate: (route: Route) => void
  ready: boolean
  /** The gateway is not answering. Views that load data should say so rather than spin. */
  offline: boolean
}
