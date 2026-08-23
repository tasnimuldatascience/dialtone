import { useCallback, useRef, useState } from 'react'

export interface Toast { id: number; text: string; tone: 'good' | 'bad' | 'plain' }

/** Transient confirmations. Auto-dismiss, because nobody dismisses these by hand. */
export function useToasts() {
  const [items, setItems] = useState<Toast[]>([])
  const next = useRef(1)

  const push = useCallback((text: string, tone: 'good' | 'bad' = 'good') => {
    const id = next.current++
    setItems((current) => [...current, { id, text, tone }])
    // Failures stay longer: they usually carry something the reader has to act on.
    window.setTimeout(() => setItems((c) => c.filter((t) => t.id !== id)), tone === 'bad' ? 6000 : 3200)
  }, [])

  return { items, push }
}

export function Toasts({ items }: { items: Toast[] }) {
  if (!items.length) return null
  return (
    <div className="toast-wrap">
      {items.map((t) => (
        <div key={t.id} className="toast" data-t={t.tone}>{t.text}</div>
      ))}
    </div>
  )
}
