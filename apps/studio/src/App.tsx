import { useState } from 'react'
import { Benchmark } from './views/Benchmark'
import { Compliance } from './views/Compliance'
import { Corpus } from './views/Corpus'
import { FlowView } from './views/FlowView'
import { Monitor } from './views/Monitor'

const VIEWS = {
  benchmark: { label: 'Benchmark', component: Benchmark },
  monitor: { label: 'Call monitor', component: Monitor },
  flow: { label: 'Flow', component: FlowView },
  corpus: { label: 'Corpus', component: Corpus },
  compliance: { label: 'Compliance', component: Compliance },
} as const

type ViewKey = keyof typeof VIEWS

export function App() {
  const [view, setView] = useState<ViewKey>('benchmark')
  const Current = VIEWS[view].component

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark">d</div>
          <div>
            <div className="brand-name">dialtone studio</div>
          </div>
          <div className="brand-tag">turn-taking, measured</div>
        </div>
        <nav className="nav">
          {(Object.keys(VIEWS) as ViewKey[]).map((key) => (
            <button key={key} data-active={view === key} onClick={() => setView(key)}>
              {VIEWS[key].label}
            </button>
          ))}
        </nav>
      </header>
      <main>
        <Current />
      </main>
    </div>
  )
}
