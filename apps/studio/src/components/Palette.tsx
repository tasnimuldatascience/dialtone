import { useEffect, useMemo, useRef, useState } from 'react'
import { Icon, type IconName } from './Icon'

export interface Command {
  id: string
  label: string
  hint?: string
  icon?: IconName
  run: () => void
}

/**
 * Command palette.
 *
 * Fuzzy subsequence matching rather than substring: "ldc" should find "Live call", which is how
 * anyone who uses one of these actually types. Ranked by how tightly the matched characters
 * cluster, so an exact prefix still wins.
 */
export function Palette({ commands, onClose }: { commands: Command[]; onClose: () => void }) {
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => { inputRef.current?.focus() }, [])

  const matches = useMemo(() => {
    if (!query.trim()) return commands.slice(0, 9)
    return commands
      .map((c) => ({ c, score: fuzzy(query, `${c.label} ${c.hint ?? ''}`) }))
      .filter((m) => m.score > 0)
      .sort((a, b) => b.score - a.score)
      .slice(0, 9)
      .map((m) => m.c)
  }, [commands, query])

  useEffect(() => { setSelected(0) }, [query])

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setSelected((s) => (s + 1) % Math.max(1, matches.length)) }
    if (e.key === 'ArrowUp') { e.preventDefault(); setSelected((s) => (s - 1 + matches.length) % Math.max(1, matches.length)) }
    if (e.key === 'Enter' && matches[selected]) { matches[selected].run(); onClose() }
  }

  return (
    <div className="scrim" onMouseDown={onClose}>
      <div className="palette" onMouseDown={(e) => e.stopPropagation()}>
        <input
          ref={inputRef}
          type="text"
          placeholder="Jump to, or search…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={onKey}
        />
        <div className="palette-list">
          {matches.length === 0 && <div className="empty" style={{ padding: 28 }}>Nothing matches</div>}
          {matches.map((c, i) => (
            <div
              key={c.id}
              className="palette-item"
              data-sel={i === selected}
              onMouseEnter={() => setSelected(i)}
              onClick={() => { c.run(); onClose() }}
            >
              {c.icon && <Icon name={c.icon} />}
              {c.label}
              {c.hint && <span className="where">{c.hint}</span>}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

/** Subsequence match. Adjacent hits score higher, so a prefix beats scattered letters. */
function fuzzy(needle: string, haystack: string): number {
  const n = needle.toLowerCase()
  const h = haystack.toLowerCase()
  let score = 0
  let index = -1
  let streak = 0
  for (const char of n) {
    if (char === ' ') continue
    const at = h.indexOf(char, index + 1)
    if (at === -1) return 0
    streak = at === index + 1 ? streak + 1 : 0
    score += 1 + streak * 2 + (at === 0 ? 4 : 0)
    index = at
  }
  return score
}
